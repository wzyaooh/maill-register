"""Non-interactive account creation orchestration for background tasks."""
import sys
import random
import time
import logging
import inspect

from config.settings import Config
from core.progress import (
    THEME, get_progress_context, show_session_summary,
    print_success, print_error, print_warning,
)
from core.database import DatabaseManager
from core.proxy_manager import proxy_manager
from core.job_ledger import JobLedger, current_worker_id, default_ledger_path
from core.operation_result import coerce_creation_result, safe_creation_result_metadata
try:
    from core.retry_engine import RetryEngine, CreationError
except (ImportError, AttributeError):
    # Tiny compatibility doubles used by embedders/tests may expose only the
    # historical module-level ``retry_engine`` singleton.
    class _FallbackRetryEngine:
        def __init__(self, *args, **kwargs):
            self.worker_id = kwargs.get("worker_id")

        def begin_attempt(self, *args, **kwargs):
            return None

        def record_attempt(self, *args, **kwargs):
            return None

        def start_heartbeat(self, *args, **kwargs):
            return None

        @staticmethod
        def stop_heartbeat(handle):
            return None

        def wait_for_retry(self, *args, **kwargs):
            return False

    class _FallbackCreationError:
        UNKNOWN = "unknown"

    RetryEngine = _FallbackRetryEngine
    CreationError = _FallbackCreationError

if sys.platform == 'win32':
    try:
        from asyncio.proactor_events import _ProactorBasePipeTransport

        def _silence_proactor():
            def safe_del(self, _orig=getattr(_ProactorBasePipeTransport, '__del__', None)):
                try:
                    if getattr(self, '_sock', None) is None:
                        return
                    if _orig:
                        _orig(self)
                except Exception:
                    pass
            _ProactorBasePipeTransport.__del__ = safe_del

        _silence_proactor()
    except ImportError:
        pass


def _generate_username():
    from core.selenium_runner import generate_name
    name = generate_name()
    parts = name.split()
    first = parts[0].lower() if parts else "user"
    last = parts[-1].lower() if len(parts) > 1 else "gmail"
    return f"{first}{last}{random.randint(1000, 9999)}", parts


def run_creation_flow(num_accounts, warmup_minutes=10, flow_mode='standard', use_sms_api=False,
                      on_progress=None, resume_state=None, ledger=None, job_id=None):
    """
    Unified account creation flow. Routes to Playwright, Appium, or Selenium
    based on Config.ENGINE_MODE.
    """
    engine = (resume_state or {}).get("batch_config", {}).get(
        "engine", getattr(Config, 'ENGINE_MODE', 'playwright')).lower()
    if job_id is None:
        job_id = (resume_state or {}).get("batch_config", {}).get("job_id")
    ledger = ledger or JobLedger(default_ledger_path())
    if job_id is None:
        job_id = ledger.create_job(
            "create", requested_count=num_accounts, requested_engine=engine,
            metadata={"source": "serial-creation"},
        )["job_id"]
    elif ledger.get_job(job_id) is None:
        ledger.create_job(
            "create", requested_count=num_accounts, requested_engine=engine,
            metadata={"source": "serial-creation"}, job_id=job_id,
        )
    durable_retry = RetryEngine(
        ledger=ledger, job_id=job_id, worker_id=current_worker_id(),
    )
    password = Config.YOUR_PASSWORD

    if not password:
        try:
            with open("config/password.txt", "r", encoding="utf-8") as f:
                password = f.read().strip()
        except FileNotFoundError:
            pass

    use_generated_passwords = not password

    previous = (resume_state or {}).get("results", {})
    successes = previous.get("successes", 0)
    failures = previous.get("failures", 0)
    completed_indices = list((resume_state or {}).get("completed_indices", []))
    if resume_state:
        num_accounts = resume_state["batch_config"]["num_accounts"]
    pending_indices = [i for i in range(num_accounts) if i not in completed_indices]
    start_time = time.time()

    with get_progress_context() as progress:
        overall = progress.add_task(f"[{THEME['success']}]Overall Progress", total=num_accounts)
        progress.update(overall, completed=len(completed_indices))
        current = progress.add_task(f"[{THEME['primary']}]Current Account", total=100)

        for i in pending_indices:
            latest_attempt = getattr(ledger, "get_latest_attempt", None)
            recovered = (
                latest_attempt(job_id, account_id=i)
                if callable(latest_attempt) else None
            )
            if recovered and recovered.get("state") == "succeeded":
                # The browser/account commit happened before the previous
                # process could save its coarse session checkpoint.  The
                # attempt ledger is the finer-grained fact: do not create the
                # same account index again after restart.
                successes = min(num_accounts, successes + 1)
                completed_indices.append(i)
                progress.update(overall, advance=1)
                progress.update(current, completed=0)
                try:
                    from core.session_resume import session_manager
                    session_manager.save_state(
                        batch_config={
                            "num_accounts": num_accounts,
                            "flow_mode": flow_mode,
                            "use_sms_api": use_sms_api,
                            "warmup_minutes": warmup_minutes,
                            "engine": engine,
                            "job_id": job_id,
                        },
                        completed_indices=completed_indices,
                        results={"successes": successes, "failures": failures},
                    )
                except OSError:
                    raise
                if on_progress:
                    on_progress(
                        len(completed_indices), num_accounts,
                        {"successes": successes, "failures": failures},
                    )
                continue

            username, name_parts = _generate_username()
            first_name = name_parts[0] if name_parts else "User"
            last_name = name_parts[-1] if len(name_parts) > 1 else "User"

            if use_generated_passwords:
                from core.selenium_runner import generate_password
                password = generate_password()

            progress.update(current, completed=5,
                            description=f"[{THEME['primary']}]Account {i+1}/{num_accounts}...[/]")

            proxy = proxy_manager.get_best() or proxy_manager.get_next()

            success = False
            max_attempts = max(1, int(getattr(durable_retry, "MAX_RETRIES", 1)))
            strategy = flow_mode

            for attempt in range(max_attempts):
                if attempt > 0:
                    print_warning(
                        f"Retrying with strategy {strategy} (attempt {attempt + 1})..."
                    )
                    progress.update(
                        current, completed=5,
                        description=f"[{THEME['warning']}]Retry {attempt + 1} - Account {i + 1}...[/]",
                    )

                ledger_attempt = durable_retry.begin_attempt(
                    strategy,
                    subject=f"{username}@gmail.com", account_id=i,
                    engine=engine, proxy=proxy,
                )
                attempt_id = ledger_attempt.get("attempt_id", "") if isinstance(ledger_attempt, dict) else ""
                order_store = getattr(ledger, "sms_orders", None)
                error_type = None
                runner_result = None
                attempt_heartbeat = durable_retry.start_heartbeat(ledger_attempt)
                try:
                    if engine == 'appium':
                        # Appium is intentionally fail-closed until its native
                        # flow can return a verified email and persist the
                        # same account/profile contract as desktop engines.
                        error_type = CreationError.UNSUPPORTED
                        success = False
                    elif engine == 'playwright':
                        from core.runners import run_playwright_flow
                        runner_kwargs = {
                            "use_sms_api": use_sms_api,
                            "flow_mode": strategy,
                            "job_id": job_id or "",
                            "attempt_id": attempt_id,
                            "order_store": order_store,
                        }
                        try:
                            accepted = inspect.signature(run_playwright_flow).parameters
                            accepts_var_kwargs = any(
                                parameter.kind is inspect.Parameter.VAR_KEYWORD
                                for parameter in accepted.values()
                            )
                            supports_context = (
                                all(name in accepted for name in ("job_id", "attempt_id", "order_store"))
                                or accepts_var_kwargs
                            )
                            supports_result = "return_result" in accepted or accepts_var_kwargs
                        except (TypeError, ValueError):
                            # An opaque callable cannot be inspected; the
                            # native path accepts the full context and result
                            # flag, so preserve those arguments in that case.
                            supports_context = True
                            supports_result = True
                        if supports_result:
                            runner_kwargs["return_result"] = True
                        if not supports_context:
                            # A legacy embedder may provide a strict test or
                            # plugin callable.  Preserve its old signature;
                            # the native runner above receives full context.
                            runner_kwargs = {
                                "use_sms_api": use_sms_api,
                                "flow_mode": strategy,
                            }
                            if supports_result:
                                runner_kwargs["return_result"] = True
                        runner_result = run_playwright_flow(
                            i, num_accounts, username, first_name, last_name,
                            password, progress, current, proxy,
                            **runner_kwargs,
                        )
                        success, error_type = coerce_creation_result(runner_result)
                    else:
                        from core.selenium_runner import run_selenium_flow
                        selenium_kwargs = {
                            "warmup_minutes": warmup_minutes,
                            "stealth_mode": (not use_sms_api),
                            "mode": strategy,
                            "proxy": proxy,
                            "use_sms_api": use_sms_api,
                            "job_id": job_id or "",
                            "attempt_id": attempt_id,
                            "order_store": order_store,
                            "return_result": True,
                        }
                        try:
                            selenium_parameters = inspect.signature(
                                run_selenium_flow
                            ).parameters
                            selenium_var_kwargs = any(
                                parameter.kind is inspect.Parameter.VAR_KEYWORD
                                for parameter in selenium_parameters.values()
                            )
                            if "return_result" not in selenium_parameters and not selenium_var_kwargs:
                                selenium_kwargs.pop("return_result", None)
                            selenium_supports_context = (
                                all(
                                    name in selenium_parameters
                                    for name in ("use_sms_api", "job_id", "attempt_id", "order_store")
                                )
                                or selenium_var_kwargs
                            )
                            if not selenium_supports_context:
                                if use_sms_api:
                                    raise TypeError(
                                        "Selenium SMS registration requires durable attempt context"
                                    )
                                for name in ("use_sms_api", "job_id", "attempt_id", "order_store"):
                                    selenium_kwargs.pop(name, None)
                        except (TypeError, ValueError):
                            if use_sms_api:
                                raise
                        runner_result = run_selenium_flow(
                            i, num_accounts, username, password,
                            **selenium_kwargs,
                        )
                        success, error_type = coerce_creation_result(runner_result)
                except KeyboardInterrupt:
                    # Direct callers do not have the supervised worker's
                    # process-boundary safety net. Close the current durable
                    # attempt before propagating cancellation. A flow that
                    # already has resume state must remain resumable; a fresh
                    # flow with no saved state is a terminal cancellation.
                    resumable = bool(resume_state)
                    if not resumable:
                        try:
                            from core.session_resume import session_manager as _resume_manager
                            resumable = _resume_manager.has_saved_session() is True
                        except Exception:
                            resumable = False
                    attempt_state = "interrupted" if resumable else "cancelled"
                    attempt_error = "worker_lost" if resumable else "cancelled"
                    attempt_retry = "retry" if resumable else "stop"
                    if ledger_attempt:
                        try:
                            ledger.finish_attempt(
                                ledger_attempt["attempt_id"], outcome="cancelled",
                                state=attempt_state, error_code=attempt_error,
                                retry_decision=attempt_retry,
                                worker_id=durable_retry.worker_id or None,
                            )
                        except Exception as exc:
                            logging.debug(
                                "Unable to finalize cancelled creation attempt: %s",
                                type(exc).__name__,
                            )
                    try:
                        ledger.finalize_job(
                            job_id, state=attempt_state, error_code=attempt_error,
                            summary={"successes": successes, "failures": failures},
                            worker_id=durable_retry.worker_id,
                        )
                    except Exception as exc:
                        logging.debug(
                            "Unable to finalize cancelled creation job: %s",
                            type(exc).__name__,
                        )
                    raise
                except Exception as e:
                    error_type = type(e).__name__
                    print_error(f"Account {i+1} error: {type(e).__name__}")
                except BaseException as e:
                    # SystemExit and other non-Exception failures must not
                    # leave an attempt in ``running`` when this API is called
                    # outside the supervised worker.
                    error_type = type(e).__name__
                    if ledger_attempt:
                        try:
                            ledger.finish_attempt(
                                ledger_attempt["attempt_id"], outcome="failed",
                                error_code=error_type, retry_decision="stop",
                                worker_id=durable_retry.worker_id or None,
                            )
                        except Exception as exc:
                            logging.debug(
                                "Unable to finalize failed creation attempt: %s",
                                type(exc).__name__,
                            )
                    try:
                        ledger.finalize_job(
                            job_id, state="failed", error_code="worker_exception",
                            summary={"successes": successes, "failures": failures},
                            worker_id=durable_retry.worker_id,
                        )
                    except Exception as exc:
                        logging.debug(
                            "Unable to finalize failed creation job: %s",
                            type(exc).__name__,
                        )
                    raise
                finally:
                    durable_retry.stop_heartbeat(attempt_heartbeat)

                if ledger_attempt:
                    normalized_error = error_type or (
                        None if success else CreationError.UNKNOWN
                    )
                    recorded_attempt = durable_retry.record_attempt(
                        strategy, bool(success), normalized_error,
                        attempt_id=ledger_attempt["attempt_id"],
                        attempt_count=attempt + 1,
                        subject=f"{username}@gmail.com", account_id=i,
                        engine=engine, proxy=proxy,
                        metadata=safe_creation_result_metadata(runner_result),
                    )
                else:
                    normalized_error = error_type or (
                        None if success else CreationError.UNKNOWN
                    )
                    recorded_attempt = None

                if success:
                    break

                should_retry = (
                    isinstance(recorded_attempt, dict)
                    and recorded_attempt.get("retry_decision") == "retry"
                )
                if not should_retry:
                    break

                if durable_retry.should_change_proxy(normalized_error):
                    old_proxy = proxy
                    if old_proxy:
                        proxy_manager.mark_failure(old_proxy, fatal=True)
                    proxy = proxy_manager.get_next()
                    if proxy == old_proxy:
                        proxy = proxy_manager.get_random()
                if normalized_error == CreationError.USERNAME_TAKEN:
                    username, name_parts = _generate_username()
                    first_name = name_parts[0] if name_parts else "User"
                    last_name = name_parts[-1] if len(name_parts) > 1 else "User"
                    if use_generated_passwords:
                        password = generate_password()
                strategy = durable_retry.get_next_strategy(strategy, normalized_error)
                try:
                    retry_claimed = durable_retry.wait_for_retry(ledger_attempt)
                except KeyboardInterrupt:
                    resumable = bool(resume_state)
                    if not resumable:
                        try:
                            from core.session_resume import session_manager as _resume_manager
                            resumable = _resume_manager.has_saved_session() is True
                        except Exception:
                            resumable = False
                    ledger.finalize_job(
                        job_id,
                        state="interrupted" if resumable else "cancelled",
                        error_code="worker_lost" if resumable else "cancelled",
                        summary={"successes": successes, "failures": failures},
                        worker_id=durable_retry.worker_id,
                    )
                    raise
                if not retry_claimed:
                    # The durable schedule may have been cancelled, repaired,
                    # or claimed by another worker.  Never infer permission
                    # for another browser attempt from stale in-memory data.
                    break

            if success:
                successes += 1
                print_success(f"Account {i+1}/{num_accounts}: {username}@gmail.com CREATED")
                if proxy:
                    proxy_manager.mark_success(proxy)
            else:
                failures += 1
                print_error(f"Account {i+1}/{num_accounts}: {username}@gmail.com FAILED")
                if proxy:
                    proxy_manager.mark_failure(proxy)

            progress.update(overall, advance=1)
            progress.update(current, completed=0)

            # Save session state for resume capability
            try:
                from core.session_resume import session_manager
                completed_indices.append(i)
                session_manager.save_state(
                    batch_config={"num_accounts": num_accounts, "flow_mode": flow_mode,
                                  "use_sms_api": use_sms_api, "warmup_minutes": warmup_minutes,
                                  "engine": engine, "job_id": job_id},
                    completed_indices=completed_indices,
                    results={"successes": successes, "failures": failures},
                )
            except OSError as exc:
                logging.error("Unable to save resumable session: %s", exc)
                raise

            if on_progress:
                on_progress(len(completed_indices), num_accounts,
                            {"successes": successes, "failures": failures})

            if i < num_accounts - 1:
                delay = getattr(Config, 'DELAY_BETWEEN_ACCOUNTS', 30)
                time.sleep(delay)

    duration = time.time() - start_time
    show_session_summary(num_accounts, successes, failures, duration)

    db = DatabaseManager()
    db.save_session_stats(
        total_attempts=num_accounts, successes=successes, failures=failures,
        strategies_used={flow_mode: num_accounts}, errors={},
        duration_seconds=duration,
    )

    # Telegram batch notification
    try:
        from core.telegram_notifier import notifier
        notifier.notify_batch_complete(num_accounts, successes, failures, duration)
    except Exception:
        pass

    # Clear saved session on completion
    try:
        from core.session_resume import session_manager
        session_manager.clear_state()
    except Exception:
        pass

    ledger.finish_job(
        job_id, state="succeeded" if failures == 0 else "failed",
        terminal_error_code="" if failures == 0 else "creation_failed",
        summary={"successes": successes, "failures": failures},
        worker_id=durable_retry.worker_id,
    )
    return {"total": num_accounts, "successes": successes, "failures": failures,
            "duration": duration, "job_id": job_id}
