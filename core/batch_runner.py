"""
Batch Runner - Multi-threaded account creation with progress tracking
Uses ThreadPoolExecutor for parallel browser instances.
"""
import time
import random
import logging
import inspect
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from config.settings import Config
from core.proxy_manager import proxy_manager
from core.account_manager import account_manager
from core.retry_engine import retry_engine, RetryEngine, CreationError
from core.database import DatabaseManager
from core.secret_safety import safe_proxy_label
from core.job_ledger import JobLedger, current_worker_id, default_ledger_path
from core.operation_result import coerce_creation_result, safe_creation_result_metadata

logger = logging.getLogger('gmail_creator_batch')


def _call_runner(runner, *args, **kwargs):
    """Pass context/result flags only to runners that declare them."""
    try:
        parameters = inspect.signature(runner).parameters
    except (TypeError, ValueError):
        return runner(*args, **kwargs)
    if any(item.kind is inspect.Parameter.VAR_KEYWORD
           for item in parameters.values()):
        return runner(*args, **kwargs)
    filtered = {key: value for key, value in kwargs.items() if key in parameters}
    return runner(*args, **filtered)


def _generate_username():
    from core.selenium_runner import generate_name
    name = generate_name()
    parts = name.split()
    first = parts[0].lower() if parts else "user"
    last = parts[-1].lower() if len(parts) > 1 else "gmail"
    username = f"{first}{last}{random.randint(1000, 9999)}"
    return username, parts[0] if parts else "User", parts[-1] if len(parts) > 1 else "User"


def _create_single_account(index, total, engine, password, warmup_minutes,
                            flow_mode, use_sms_api, ledger=None, job_id=None,
                            ordinal=None, cancel_event=None):
    """Create a single account. Designed to run in a thread."""
    username, first_name, last_name = _generate_username()
    proxy = proxy_manager.get_best() or proxy_manager.get_next()

    success = False
    error_type = None
    durable_retry = (
        RetryEngine(ledger=ledger, job_id=job_id, worker_id=current_worker_id())
        if ledger and job_id else None
    )
    order_store = getattr(ledger, "sms_orders", None) if ledger is not None else None

    strategy = flow_mode
    max_attempts = max(1, int(getattr(durable_retry, "MAX_RETRIES", 1))) if durable_retry else 1
    for attempt_index in range(max_attempts):
        if cancel_event is not None and cancel_event.is_set():
            raise KeyboardInterrupt
        ledger_attempt = None
        if durable_retry:
            ledger_attempt = durable_retry.begin_attempt(
                strategy,
                ordinal=ordinal if attempt_index == 0 else None,
                subject=f"{username}@gmail.com", account_id=index,
                engine=engine, proxy=proxy,
            )
        attempt_id = ledger_attempt.get("attempt_id", "") if isinstance(ledger_attempt, dict) else ""
        error_type = None
        runner_result = None
        attempt_heartbeat = (
            durable_retry.start_heartbeat(ledger_attempt)
            if durable_retry and ledger_attempt else None
        )
        try:
            if engine == 'playwright':
                from core.runners import run_playwright_flow
                runner_result = _call_runner(
                    run_playwright_flow,
                    index, total, username, first_name, last_name,
                    password, None, None, proxy,
                    use_sms_api=use_sms_api, flow_mode=strategy,
                    job_id=job_id or "", attempt_id=attempt_id,
                    order_store=order_store,
                    return_result=True,
                )
                success, error_type = coerce_creation_result(runner_result)
            elif engine == 'appium':
                # Keep the task/account lifecycle durable while the native
                # mobile flow is explicitly unsupported.  Do not start an
                # external device session without a persistence contract.
                logger.warning("Appium engine is unsupported for account creation")
                success = False
                error_type = CreationError.UNSUPPORTED
            else:
                from core.selenium_runner import run_selenium_flow
                runner_result = _call_runner(
                    run_selenium_flow,
                    index, total, username, password,
                    warmup_minutes=warmup_minutes,
                    stealth_mode=(not use_sms_api),
                    mode=strategy, proxy=proxy,
                    use_sms_api=use_sms_api,
                    job_id=job_id or "", attempt_id=attempt_id,
                    order_store=order_store,
                    return_result=True,
                )
                success, error_type = coerce_creation_result(runner_result)
        except KeyboardInterrupt:
            success = False
            error_type = "cancelled"
            if durable_retry and ledger_attempt:
                try:
                    durable_retry.ledger.finish_attempt(
                        ledger_attempt["attempt_id"], outcome="cancelled",
                        state="cancelled", error_code="cancelled",
                        retry_decision="stop",
                        worker_id=durable_retry.worker_id or None,
                    )
                except Exception as exc:
                    logger.debug(
                        "Unable to finalize cancelled batch attempt: %s",
                        type(exc).__name__,
                    )
            raise
        except Exception as e:
            logger.error("Thread %s failed: %s", index, type(e).__name__)
            success = False
            error_type = type(e).__name__
        except BaseException as exc:
            # Futures retain BaseException instances (SystemExit, generator
            # shutdown, and similar failures) just like ordinary exceptions.
            # Close the durable attempt before letting the batch boundary
            # finalize the job so no direct caller can observe ``running``.
            success = False
            error_type = type(exc).__name__
            if durable_retry and ledger_attempt:
                try:
                    durable_retry.ledger.finish_attempt(
                        ledger_attempt["attempt_id"], outcome="failed",
                        error_code=error_type, retry_decision="stop",
                        worker_id=durable_retry.worker_id or None,
                    )
                except Exception as exc:
                    logger.debug(
                        "Unable to finalize failed batch attempt: %s",
                        type(exc).__name__,
                    )
            raise
        finally:
            if durable_retry:
                durable_retry.stop_heartbeat(attempt_heartbeat)

        normalized_error = error_type or (None if success else CreationError.UNKNOWN)
        recorded = None
        if durable_retry and ledger_attempt:
            recorded = durable_retry.record_attempt(
                strategy, bool(success), normalized_error,
                attempt_id=ledger_attempt["attempt_id"],
                attempt_count=attempt_index + 1,
                subject=f"{username}@gmail.com", account_id=index,
                engine=engine, proxy=proxy,
                metadata=safe_creation_result_metadata(runner_result),
            )
        if success:
            break
        if not isinstance(recorded, dict) or recorded.get("retry_decision") != "retry":
            break

        if durable_retry.should_change_proxy(normalized_error):
            old_proxy = proxy
            if old_proxy:
                proxy_manager.mark_failure(old_proxy, fatal=True)
            proxy = proxy_manager.get_next()
            if proxy == old_proxy:
                proxy = proxy_manager.get_random()
        if normalized_error == CreationError.USERNAME_TAKEN:
            username, first_name, last_name = _generate_username()
        strategy = durable_retry.get_next_strategy(strategy, normalized_error)
        if not durable_retry.wait_for_retry(
            ledger_attempt, cancel_event=cancel_event,
        ):
            # A retry only proceeds after the persisted schedule is claimed;
            # a missing/invalid/competing schedule is a durable stop.
            break

    if success and proxy:
        proxy_manager.mark_success(proxy)
    elif not success and proxy:
        proxy_manager.mark_failure(proxy)

    return {
        "index": index,
        "username": username,
        "email": f"{username}@gmail.com",
        "success": success,
        "error_type": error_type,
        "proxy_endpoint": safe_proxy_label(proxy) if proxy else "",
    }


def run_batch(num_accounts, max_threads=3, warmup_minutes=5,
              flow_mode='standard', use_sms_api=False, on_result=None,
              ledger=None, job_id=None):
    """
    Run account creation in parallel using a thread pool.

    Args:
        num_accounts: Total accounts to create
        max_threads: Maximum concurrent browser instances (1-5)
        warmup_minutes: Trust building duration per account
        flow_mode: Signup URL route (standard/youtube/workspace)
        use_sms_api: Whether to use SMS API for verification
        on_result: Callback(result_dict) called after each account attempt

    Returns:
        dict with summary stats
    """
    max_threads = max(1, min(5, max_threads))
    engine = getattr(Config, 'ENGINE_MODE', 'playwright').lower()
    owns_ledger = ledger is None
    ledger = ledger or JobLedger(default_ledger_path())
    if job_id is None:
        job = ledger.create_job(
            "create", requested_count=num_accounts, requested_engine=engine,
            metadata={"source": "batch-runner"},
        )
        job_id = job["job_id"]
    elif ledger.get_job(job_id) is None:
        ledger.create_job(
            "create", requested_count=num_accounts, requested_engine=engine,
            metadata={"source": "batch-runner"}, job_id=job_id,
        )
    password = Config.YOUR_PASSWORD

    if not password:
        try:
            with open("config/password.txt", "r", encoding="utf-8") as f:
                password = f.read().strip()
        except FileNotFoundError:
            logger.error("No password configured")
            ledger.finish_job(
                job_id,
                state="failed",
                terminal_error_code="configuration_error",
                summary={"successes": 0, "failures": 0},
                worker_id=current_worker_id(),
            )
            return {
                "total": 0,
                "successes": 0,
                "failures": 0,
                "duration": 0,
                "job_id": job_id,
            }

    start_time = time.time()
    results = []
    cancel_event = threading.Event()

    logger.info(f"Starting batch: {num_accounts} accounts, {max_threads} threads, "
                f"engine={engine}, mode={flow_mode}")

    try:
        with ThreadPoolExecutor(max_workers=max_threads) as executor:
            futures = {}
            try:
                for i in range(num_accounts):
                    future = executor.submit(
                        _create_single_account,
                        i, num_accounts, engine, password,
                        warmup_minutes, flow_mode, use_sms_api,
                        ledger=ledger, job_id=job_id, ordinal=i + 1,
                        cancel_event=cancel_event,
                    )
                    futures[future] = i

                    if i < num_accounts - 1 and len(futures) >= max_threads:
                        time.sleep(random.uniform(2, 5))

                for future in as_completed(futures):
                    try:
                        result = future.result(timeout=600)
                        results.append(result)
                        if on_result:
                            on_result(result)
                    except Exception as e:
                        idx = futures[future]
                        results.append({
                            "index": idx, "username": "unknown",
                            "email": "unknown", "success": False,
                            "error_type": type(e).__name__, "proxy_endpoint": "",
                        })
            except BaseException:
                # ThreadPoolExecutor waits for workers in __exit__.  Broadcast
                # cancellation before leaving the context so durable retry
                # waits wake immediately instead of holding shutdown until the
                # persisted cooldown expires.
                cancel_event.set()
                for future in futures:
                    future.cancel()
                raise
    except KeyboardInterrupt:
        try:
            ledger.finalize_job(job_id, state="cancelled", error_code="cancelled",
                                summary={"successes": 0, "failures": len(results)},
                                worker_id=current_worker_id())
        except Exception as exc:
            logger.debug(
                "Unable to finalize cancelled batch job: %s", type(exc).__name__
            )
        raise
    except BaseException:
        try:
            ledger.finalize_job(job_id, state="failed", error_code="worker_exception",
                                summary={"successes": 0, "failures": len(results)},
                                worker_id=current_worker_id())
        except Exception as exc:
            logger.debug(
                "Unable to finalize failed batch job: %s", type(exc).__name__
            )
        raise

    duration = time.time() - start_time
    successes = sum(1 for r in results if r["success"])
    failures = len(results) - successes

    db = DatabaseManager()
    db.save_session_stats(
        total_attempts=num_accounts, successes=successes, failures=failures,
        strategies_used={flow_mode: num_accounts},
        errors={r["error_type"]: 1 for r in results if r["error_type"]},
        duration_seconds=duration,
    )

    logger.info(f"Batch complete: {successes}/{num_accounts} success, {duration:.0f}s")

    ledger.finish_job(
        job_id, state="succeeded" if failures == 0 else "failed",
        terminal_error_code="" if failures == 0 else "creation_failed",
        summary={"successes": successes, "failures": failures},
        worker_id=current_worker_id(),
    )

    return {
        "total": num_accounts,
        "successes": successes,
        "failures": failures,
        "duration": duration,
        "results": results,
        "job_id": job_id,
    }
