"""Run one trusted operation without blocking a web request."""
import asyncio
import inspect
import logging
import math
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from core.secret_safety import (
    SAFE_ERROR_CODES,
    normalize_error_code,
    redact_text,
    safe_network_trust_summary,
    sanitize_operation_value,
    stable_exception_code,
)
from core.job_ledger import current_worker_id
from web.tasks import ACTIVE, TaskStore, now


_worker_secret_provider = lambda: ()

_DEFAULT_HEARTBEAT_INTERVAL = 15.0
_MAX_HEARTBEAT_INTERVAL = 300.0
_DEFAULT_STALE_ATTEMPT_AGE = 300
_MAX_STALE_ATTEMPT_AGE = 86400


def _invoke_execute(action, params, report, task_id):
    """Call an operation override with ledger context when it supports it.

    ``execute`` has historically been replaceable by embedders with a
    three-argument callable.  Keep that extension point working while the
    native implementation receives the supervised task id for durable
    ledger records.
    """
    operation = execute
    try:
        parameters = inspect.signature(operation).parameters.values()
    except (TypeError, ValueError):
        # Some C-level or proxy callables do not expose a signature; the
        # native function accepts the keyword and remains the safest default.
        return operation(action, params, report, job_id=task_id)
    accepts_context = any(
        parameter.name == "job_id"
        or parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    )
    if accepts_context:
        return operation(action, params, report, job_id=task_id)
    return operation(action, params, report)


def _ledger_path():
    """Resolve the database used by the current supervised worker."""
    configured = os.environ.get("LEDGER_DB_PATH")
    if configured:
        return configured
    task_directory = os.environ.get("WEB_TASK_DIRECTORY")
    if task_directory:
        return str(Path(task_directory).parent / "database.db")
    return str(Path("data/database.db"))


def _update_task_if_status(store, task_id, expected_statuses, **values):
    """Perform a compare-and-set task update at the durable boundary."""
    updater = getattr(store, "update_if_status", None)
    if not callable(updater):
        return False
    return updater(task_id, expected_statuses, **values)


def _new_ledger(action, params, job_id=None):
    if action not in ("create", "resume", "health", "warm", "compensation"):
        return None, None
    from core.job_ledger import JobLedger
    ledger = JobLedger(_ledger_path())
    params = params if isinstance(params, dict) else {}
    requested_engine = params.get("engine")
    requested_count = params.get("num_accounts")
    # ``None`` means the caller did not provide a constraint (health/warm and
    # resume commonly omit creation-only fields).  New rows still receive the
    # schema defaults below.
    requested_engine_value = requested_engine or ""
    requested_count_value = requested_count if isinstance(requested_count, int) else 0
    resolved_id = job_id or "web-" + uuid.uuid4().hex
    try:
        job = ledger.create_job(
            "create" if action == "resume" else action,
            requested_count=requested_count_value,
            requested_engine=requested_engine_value,
            metadata={"source": "web-worker"}, job_id=resolved_id,
        )
    except ValueError as exc:
        # A resumed worker may be retried with the same task id. Reusing the
        # existing job is idempotent only when its immutable kind/configuration
        # agrees with the request; otherwise an accidental id collision must be
        # visible instead of attaching new attempts to another operation.
        existing = ledger.get_job(resolved_id)
        expected_kind = "create" if action == "resume" else action
        if not existing or existing.get("kind") != expected_kind:
            raise exc
        if action != "resume":
            if "num_accounts" in params and isinstance(requested_count, int):
                if int(existing.get("requested_count") or 0) != int(requested_count):
                    raise ValueError("job configuration conflicts with existing job")
            if requested_engine:
                if str(existing.get("requested_engine") or "") != str(requested_engine):
                    raise ValueError("job configuration conflicts with existing job")
        job = existing
    return ledger, job


def _result_error_code(result):
    if not isinstance(result, dict):
        return ""
    return normalize_error_code(
        result.get("error_code") or result.get("status") or "",
        default="", allow_empty=True,
    )


# These sets mirror the operation policy in ``RetryEngine``.  Keeping the
# small result classifier local means a malformed adapter result cannot turn
# into an implicit retry merely because it happens to contain a truthy value.
_TRANSIENT_OPERATION_ERRORS = frozenset({
    "runtime_unavailable", "network_error", "error", "profile_busy",
    "timeout", "provider_error", "provider_timeout", "activity_failed",
    "reconciliation_failed", "compensation_claimed",
})
_TERMINAL_OPERATION_ERRORS = frozenset({
    "unsupported", "cancelled", "profile_conflict", "account_mismatch",
    "challenge", "runtime_mismatch", "proxy_mismatch", "proxy_unavailable",
    "profile_unavailable", "legacy_unbound", "login_required",
    "password_changed", "locked", "suspended", "cleanup_failed",
})

_SAFE_RECONCILIATION_ERRORS = SAFE_ERROR_CODES
_MAX_RECONCILIATION_COUNT = 1000000


def _strict_reconciliation_counters(result):
    """Project a complete provider counter tuple of bounded integers.

    A missing counter is not equivalent to zero at this boundary.  The
    scheduler uses the tuple as a durable reconciliation result, so accepting
    a partial response could turn a truncated or incompatible provider reply
    into a false success.
    """
    counters = {}
    for name in ("claimed", "cancelled", "completed", "failed"):
        if name not in result:
            raise ValueError("missing reconciliation counter")
        value = result.get(name)
        # ``bool`` is an ``int`` subclass, and floats/strings can be coerced by
        # ``int``.  Neither is a valid provider counter at this boundary.
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
            or value > _MAX_RECONCILIATION_COUNT
        ):
            raise ValueError("invalid reconciliation counter")
        counters[name] = value
    return counters


def _retryable_operation_errors(action):
    """Return the retry policy owned by ``RetryEngine`` for one operation."""
    try:
        from core.retry_engine import RetryEngine
        getter = getattr(RetryEngine, "retryable_errors", None)
        if callable(getter):
            return frozenset(getter(action))
        policies = getattr(RetryEngine, "OPERATION_RETRYABLE_ERRORS", None)
        if isinstance(policies, dict) and action in policies:
            return frozenset(policies[action])
    except (ImportError, AttributeError, TypeError, ValueError):
        pass
    # Compatibility fallback for an embedding application that supplies an
    # older retry module without operation policies.
    return _TRANSIENT_OPERATION_ERRORS


def _operation_result_outcome(action, result):
    """Return ``(success, finite_error_code)`` for one adapter result.

    A health snapshot is useful even when it reports a terminal account state
    (for example ``locked``), but it must not be mistaken for a transient
    browser/network failure.  Warm and compensation operations require an
    explicit successful result.
    """
    if action in ("health", "warm", "compensation") and not isinstance(result, dict):
        return False, "invalid_reconciliation_result"
    if isinstance(result, bool):
        return bool(result), "" if result else "error"
    if not isinstance(result, dict):
        return False, "error"

    code = normalize_error_code(
        result.get("error_code")
        or result.get("last_error_code")
        or "", default="", allow_empty=True,
    )

    retryable_errors = _retryable_operation_errors(action)

    if action == "health":
        if "success" in result and type(result["success"]) is not bool:
            return False, "invalid_reconciliation_result"
        status = str(result.get("status") or "").strip().lower()
        browser_status = str(result.get("browser_status") or "").strip().lower()
        mailbox_status = str(result.get("mailbox_status") or "").strip().lower()
        complete_snapshot = {
            "browser_status", "mailbox_status", "status"
        } <= set(result)
        for candidate in (code, status, browser_status, mailbox_status):
            # ``profile_busy`` in a complete snapshot is an intentional,
            # durable degraded observation.  Retrying it would discard that
            # fact and breaks the one-probe contract of the health API.
            if candidate == "profile_busy" and complete_snapshot:
                continue
            if candidate in retryable_errors:
                return False, candidate
        for candidate in (code, status, browser_status, mailbox_status):
            if candidate in _TERMINAL_OPERATION_ERRORS:
                return False, candidate
        if status in {"error", "network_error", "unknown"}:
            return False, status
        if "success" in result:
            return result["success"], code or ("error" if not result["success"] else "")
        # A complete snapshot with no error code is a successful observation;
        # ``degraded`` is an account fact, not a scheduler failure by itself.
        return True, code

    if action == "warm":
        # A warm operation is only successful after both sides of the
        # reconciliation barrier have been observed.  Checking the adapter's
        # truthy ``success`` flag alone can report a false positive when the
        # browser process or profile lease is still alive.
        success_value = result.get("success")
        if type(success_value) is not bool:
            return False, "invalid_reconciliation_result"
        cleanup_status = result.get("cleanup_status")
        browser_stopped = result.get("browser_process_stopped")
        lease_released = result.get("lease_released")
        # Explicit failures may be reported before a browser/lease was ever
        # acquired (for example ``profile_unavailable``).  Preserve that
        # finite error code; reconciliation fields are mandatory only for a
        # positive success claim.
        if not success_value:
            if cleanup_status == "failed" or code == "cleanup_failed":
                return False, "cleanup_failed"
            if not code:
                browser_status = str(result.get("browser_status") or "").strip().lower()
                code = browser_status if browser_status in (
                    retryable_errors | _TERMINAL_OPERATION_ERRORS
                ) else "error"
            return False, code

        if (
            cleanup_status != "completed"
            or type(browser_stopped) is not bool
            or type(lease_released) is not bool
            or not browser_stopped
            or not lease_released
            or code == "cleanup_failed"
        ):
            return False, "cleanup_failed"
        success = success_value
        if success:
            return True, code
        return False, code or "error"

    # Compensation results are counters.  Any malformed counter is a failed
    # protocol response, never a successful empty pass.  Do not coerce
    # strings, booleans, floats, NaN, or negative values into integers.
    try:
        counters = _strict_reconciliation_counters(result)
    except (TypeError, ValueError, OverflowError):
        return False, "invalid_reconciliation_result"
    if "success" in result and type(result["success"]) is not bool:
        return False, "invalid_reconciliation_result"
    failed = counters["failed"]
    if failed > 0 or code:
        return False, code or "reconciliation_failed"
    if "success" in result:
        return result["success"], "" if result["success"] else "reconciliation_failed"
    return True, ""


def _operation_retry_engine(ledger, job_id):
    """Build a per-job retry engine while tolerating old test/embed modules."""
    if ledger is None or not job_id:
        return None
    try:
        module = __import__("core.retry_engine", fromlist=["RetryEngine"])
    except (ImportError, AttributeError):
        return None
    engine_type = getattr(module, "RetryEngine", None)
    if callable(engine_type):
        try:
            return engine_type(
                ledger=ledger, job_id=job_id, worker_id=current_worker_id()
            )
        except TypeError:
            # Keep compatibility with a pre-ledger RetryEngine supplied by an
            # embedding application; it simply disables durable retries here.
            return None
    return None


def _safe_attempt_metadata(action, result):
    """Persist only bounded operational facts in an attempt result column."""
    if not isinstance(result, dict):
        return {"operation": action}
    metadata = {
        "operation": action,
        "success": bool(result.get("success", True)),
        "error_code": normalize_error_code(
            result.get("error_code") or result.get("last_error_code") or ""
            , default="", allow_empty=True,
        ),
        "status": str(result.get("status") or result.get("browser_status") or "")[:64],
    }
    if action == "compensation":
        metadata.pop("status", None)
        try:
            metadata.update(_strict_reconciliation_counters(result))
        except (TypeError, ValueError, OverflowError):
            pass
    return metadata


def _retry_worker_id(retry):
    """Return a non-empty retry owner token, if the adapter exposes one."""
    value = getattr(retry, "worker_id", None) if retry is not None else None
    return value if isinstance(value, str) and value.strip() else None


def _bounded_heartbeat_interval(value=None):
    """Resolve a finite heartbeat period without trusting environment input."""
    if value is None:
        value = os.environ.get(
            "LEDGER_HEARTBEAT_INTERVAL", _DEFAULT_HEARTBEAT_INTERVAL
        )
    if isinstance(value, bool):
        return _DEFAULT_HEARTBEAT_INTERVAL
    try:
        period = float(value)
    except (TypeError, ValueError, OverflowError):
        return _DEFAULT_HEARTBEAT_INTERVAL
    if not math.isfinite(period):
        return _DEFAULT_HEARTBEAT_INTERVAL
    # A non-positive explicit value is the documented way for a caller that
    # owns another liveness mechanism to disable this optional thread.
    if period <= 0:
        return 0.0
    return min(period, _MAX_HEARTBEAT_INTERVAL)


def _bounded_stale_attempt_age(value=None):
    """Resolve the stale-attempt threshold used by worker/server startup."""
    if value is None:
        value = os.environ.get(
            "LEDGER_STALE_ATTEMPT_AGE", _DEFAULT_STALE_ATTEMPT_AGE
        )
    if isinstance(value, bool):
        return _DEFAULT_STALE_ATTEMPT_AGE
    try:
        age = int(value)
    except (TypeError, ValueError, OverflowError):
        return _DEFAULT_STALE_ATTEMPT_AGE
    if age < 1:
        return _DEFAULT_STALE_ATTEMPT_AGE
    return min(age, _MAX_STALE_ATTEMPT_AGE)


class _AttemptHeartbeat:
    """Keep one running ledger attempt alive until its operation returns."""

    def __init__(self, ledger, attempt, *, worker_id=None, interval=None):
        self.ledger = ledger
        self.attempt = attempt if isinstance(attempt, dict) else None
        self.worker_id = worker_id or ""
        self.interval = _bounded_heartbeat_interval(interval)
        self._stopped = threading.Event()
        self._thread = None


    def start(self):
        if (
            self.ledger is None
            or self.attempt is None
            or not self.attempt.get("attempt_id")
            or not callable(getattr(self.ledger, "heartbeat_attempt", None))
            or self.interval <= 0
        ):
            return self
        self._thread = threading.Thread(
            target=self._run,
            name="attempt-heartbeat-%s" % self.attempt["attempt_id"],
            daemon=True,
        )
        self._thread.start()
        return self

    def _run(self):
        attempt_id = self.attempt["attempt_id"]
        while not self._stopped.wait(self.interval):
            try:
                try:
                    heartbeat = self.ledger.heartbeat_attempt(
                        attempt_id, worker_id=self.worker_id or None
                    )
                except TypeError as exc:
                    if not self.worker_id or "worker_id" not in str(exc):
                        raise
                    heartbeat = self.ledger.heartbeat_attempt(attempt_id)
                if heartbeat is not True:
                    # A terminal transition or owner loss is authoritative;
                    # stop issuing writes rather than reviving another worker's
                    # row.  The operation result remains owned by its caller.
                    return
            except BaseException as exc:
                # Heartbeats are a liveness aid, not the operation outcome.
                # Never let a transient SQLite/compatibility error replace a
                # real browser/provider result or leak its payload.
                logging.getLogger("gmail_creator_worker").debug(
                    "Attempt heartbeat unavailable: %s", type(exc).__name__
                )

    def stop(self):
        self._stopped.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            # The ledger call has a bounded SQLite timeout.  Give it one full
            # period plus a small floor to finish before finalization races it.
            thread.join(timeout=max(1.0, self.interval + 0.5))
        self._thread = None


def recover_stale_attempts(*, ledger=None, max_age_seconds=None):
    """Reconcile attempts left running by a dead worker.

    This boundary is intentionally best-effort.  A stale recovery failure must
    remain visible in diagnostics, but it must not prevent a worker from
    publishing the operation's real result or leak a database/provider error.
    """
    if ledger is None:
        try:
            from core.job_ledger import JobLedger
            ledger = JobLedger(_ledger_path())
        except (ImportError, OSError, sqlite3.Error, ValueError, TypeError):
            return 0
    try:
        return ledger.recover_stale(
            max_age_seconds=_bounded_stale_attempt_age(max_age_seconds)
        )
    except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
        logging.getLogger("gmail_creator_worker").warning(
            "Stale attempt recovery unavailable: %s", type(exc).__name__
        )
        return 0


def _finish_operation_attempt(ledger, attempt, *, success, error_code,
                              decision, cooldown, metadata, worker_id=None,
                              outcome=None, state=None):
    """Finalize an operation attempt while preserving owner fencing."""
    if ledger is None or not isinstance(attempt, dict):
        return False
    attempt_id = attempt.get("attempt_id")
    if not attempt_id:
        return False
    kwargs = {
        "outcome": outcome or ("succeeded" if success else "failed"),
        "error_code": error_code or "",
        "retry_decision": decision or "stop",
        "cooldown_seconds": cooldown,
        "result": metadata if isinstance(metadata, dict) else {},
    }
    if state is not None:
        kwargs["state"] = state
    if worker_id:
        kwargs["worker_id"] = worker_id
    try:
        ledger.finish_attempt(attempt_id, **kwargs)
        return True
    except TypeError as exc:
        # Older embedding ledgers may not expose the owner keyword.  Restrict
        # this compatibility fallback to that exact signature mismatch; other
        # TypeErrors must not be hidden as a successful finalization.
        if worker_id and "worker_id" in str(exc):
            kwargs.pop("worker_id", None)
            try:
                ledger.finish_attempt(attempt_id, **kwargs)
                return True
            except Exception:
                return False
        return False
    except Exception:
        return False


def _project_operation_result(result, success, error_code="", *, action=""):
    """Make the classifier's decision authoritative at the API boundary."""
    if action == "compensation":
        try:
            projected = _strict_reconciliation_counters(result)
        except (TypeError, ValueError, OverflowError):
            projected = {"claimed": 0, "cancelled": 0, "completed": 0, "failed": 1}
            success = False
            error_code = "invalid_reconciliation_result"
    elif isinstance(result, dict):
        projected = sanitize_operation_value(result, _worker_secret_provider())
        if not isinstance(projected, dict):
            projected = {}
    else:
        projected = {}
    projected["success"] = type(success) is bool and success
    if error_code:
        projected["error_code"] = normalize_error_code(
            error_code, default="error", allow_empty=False
        )
    elif not success:
        projected["error_code"] = "error"
    return projected


def _persisted_operation_result(action, attempt):
    """Rebuild the narrow result needed to avoid rerunning a terminal subject."""
    metadata = attempt.get("result_metadata") if isinstance(attempt, dict) else {}
    result = dict(metadata) if isinstance(metadata, dict) else {}
    success = bool(isinstance(attempt, dict) and attempt.get("state") == "succeeded")
    error_code = "" if success else normalize_error_code(
        attempt.get("error_code") if isinstance(attempt, dict) else "error",
        default="error", allow_empty=False,
    )
    if action == "warm" and success:
        # A native warm attempt is marked succeeded only after the classifier
        # observed all three cleanup facts.  Preserve that durable implication
        # when a restarted worker skips the completed subject.
        result.setdefault("browser_status", "authenticated")
        result.setdefault("cleanup_status", "completed")
        result.setdefault("browser_process_stopped", True)
        result.setdefault("lease_released", True)
    elif action == "compensation" and success:
        for key in ("claimed", "cancelled", "completed", "failed"):
            result.setdefault(key, 0)
    return _project_operation_result(result, success, error_code, action=action)


def _compensation_retry_deferred(ledger):
    store = getattr(ledger, "sms_orders", None)
    pending_error = getattr(store, "pending_compensation_error", None)
    return bool(
        callable(pending_error) and pending_error()
        and not store.list_due_compensation(limit=1)
    )


def _run_retryable_operation(action, operation, *, ledger, job_id,
                             subject="", account=None, engine="",
                             profile_id="", proxy=None, cooldown_cap=None,
                             heartbeat_interval=None):
    """Run one health/warm/compensation operation through the durable ledger."""
    if action == "compensation" and ledger is not None and job_id:
        current = ledger.get_job(job_id)
        if current and current.get("state") in {
            "succeeded", "failed", "cancelled", "blocked"
        }:
            return _project_operation_result(
                current.get("summary"), current["state"] == "succeeded",
                current.get("terminal_error_code") or "", action=action,
            )
    retry = _operation_retry_engine(ledger, job_id)
    last_result = None
    account_id = (account or {}).get("id") if account else None
    # The loop count is a local guard as well as the durable policy.  It keeps
    # a broken compatibility engine from spinning forever.
    max_attempts = 1
    if retry is not None:
        try:
            max_attempts = max(1, int(getattr(retry, "MAX_RETRIES", 1)))
        except (TypeError, ValueError):
            max_attempts = 1

        # A new worker may inherit a job whose previous process finished an
        # attempt with a durable retry schedule.  Consume that schedule before
        # creating a fresh running row; otherwise the old retry would remain
        # runnable and two workers could execute the same logical operation.
        latest = None
        latest_attempt = getattr(ledger, "get_latest_attempt", None)
        if callable(latest_attempt) and (account_id is not None or subject):
            latest = latest_attempt(
                job_id, account_id=account_id,
                subject=subject if account_id is None else None,
            )
        if latest is not None:
            if latest.get("state") == "succeeded":
                return _persisted_operation_result(action, latest)
            if latest.get("retry_decision") == "stop":
                return _persisted_operation_result(action, latest)
            if latest.get("retry_decision") == "retry":
                if action == "compensation" and _compensation_retry_deferred(ledger):
                    return _persisted_operation_result(action, latest)
                if not retry.wait_for_retry(latest):
                    return _persisted_operation_result(action, latest)
            elif latest.get("retry_decision") == "pending" or latest.get("state") == "running":
                # Another live or not-yet-stale owner has the operation.  Do
                # not create a duplicate browser/provider attempt.
                busy_code = "compensation_claimed" if action == "compensation" else "profile_busy"
                busy_result = {"success": False, "error_code": busy_code}
                if action == "compensation":
                    busy_result.update(claimed=0, cancelled=0, completed=0, failed=0)
                projected = _project_operation_result(
                    busy_result, False, busy_code, action=action,
                )
                # Preserve the ownership decision across the caller boundary.
                # Re-reading after this point races with the foreign owner
                # completing its attempt and can incorrectly authorize this
                # worker to finalize a job it never owned.
                if action == "compensation":
                    projected["_foreign_owner_observed"] = True
                return projected

    for _index in range(max_attempts):
        if ledger is not None and job_id:
            current = ledger.get_job(job_id)
            if current and current.get("state") in {
                "succeeded", "failed", "cancelled", "blocked"
            }:
                if last_result is not None:
                    return last_result
                raise ValueError("job is already finalized")

        attempt = None
        if ledger is not None and job_id:
            if retry is not None:
                attempt = retry.begin_attempt(
                    action, job_id=job_id, subject=subject,
                    account_id=account_id, engine=engine,
                    profile_id=profile_id, proxy=proxy,
                    metadata={"operation": action},
                )
            else:
                attempt = ledger.start_attempt(
                    job_id, subject=subject, account_id=account_id,
                    strategy=action, engine=engine, profile_id=profile_id,
                    proxy=proxy, worker_id=current_worker_id(),
                    metadata={"operation": action},
                )

        heartbeat = _AttemptHeartbeat(
            ledger, attempt,
            worker_id=_retry_worker_id(retry) or (
                attempt.get("worker_id") if isinstance(attempt, dict) else None
            ),
            interval=heartbeat_interval,
        ).start()
        try:
            result = operation()
        except KeyboardInterrupt:
            if attempt is not None:
                _finish_operation_attempt(
                    ledger, attempt, success=False, error_code="cancelled",
                    decision="stop", cooldown=0,
                    metadata={"operation": action},
                    worker_id=_retry_worker_id(retry) or attempt.get("worker_id"),
                    outcome="cancelled", state="cancelled",
                )
            raise
        except asyncio.CancelledError:
            if attempt is not None:
                _finish_operation_attempt(
                    ledger, attempt, success=False, error_code="cancelled",
                    decision="stop", cooldown=0,
                    metadata={"operation": action},
                    worker_id=_retry_worker_id(retry) or attempt.get("worker_id"),
                    outcome="cancelled", state="cancelled",
                )
            raise
        except Exception:
            # Adapter exception payloads never enter the ledger.  ``error``
            # is deliberately finite so the operation policy can decide.
            result = {
                "success": False,
                "error_code": "error",
            }
        except BaseException:
            # Direct callers may bypass ``execute``'s outer process boundary.
            # Close the durable attempt here as well so SystemExit/GeneratorExit
            # cannot leave a row in ``running`` while the exception propagates.
            if attempt is not None:
                _finish_operation_attempt(
                    ledger, attempt, success=False, error_code="worker_exception",
                    decision="stop", cooldown=0,
                    metadata={"operation": action},
                    worker_id=_retry_worker_id(retry) or attempt.get("worker_id"),
                )
            raise
        finally:
            heartbeat.stop()

        success, error_code = _operation_result_outcome(action, result)
        # Do not return an adapter's optimistic flag after the protocol
        # classifier has rejected its reconciliation facts.  The projected
        # object is also what gets persisted in the attempt result metadata.
        result = _project_operation_result(result, success, error_code, action=action)
        last_result = result
        decision = "stop"
        cooldown = 0
        if attempt is not None:
            metadata = _safe_attempt_metadata(action, result)
            owner_id = _retry_worker_id(retry) or (
                attempt.get("worker_id")
                if isinstance(attempt.get("worker_id"), str)
                and attempt.get("worker_id").strip()
                else None
            )
            policy_error = None
            if retry is not None:
                try:
                    try:
                        recorded = retry.record_attempt(
                            action, success, error_code or None,
                            attempt_id=attempt["attempt_id"], job_id=job_id,
                            operation=action, subject=subject, account_id=account_id,
                            engine=engine, profile_id=profile_id, proxy=proxy,
                            metadata=metadata, cooldown_cap=cooldown_cap,
                        )
                    except TypeError as exc:
                        if "cooldown_cap" not in str(exc):
                            raise
                        # Older embedding retry adapters do not know the
                        # compensation cap.  Preserve their signature while
                        # keeping the native durable engine authoritative.
                        recorded = retry.record_attempt(
                            action, success, error_code or None,
                            attempt_id=attempt["attempt_id"], job_id=job_id,
                            operation=action, subject=subject, account_id=account_id,
                            engine=engine, profile_id=profile_id, proxy=proxy,
                            metadata=metadata,
                        )
                except asyncio.CancelledError:
                    _finish_operation_attempt(
                        ledger, attempt, success=False, error_code="cancelled",
                        decision="stop", cooldown=0,
                        metadata={"operation": action}, worker_id=owner_id,
                        outcome="cancelled", state="cancelled",
                    )
                    raise
                except KeyboardInterrupt:
                    _finish_operation_attempt(
                        ledger, attempt, success=False, error_code="cancelled",
                        decision="stop", cooldown=0,
                        metadata={"operation": action}, worker_id=owner_id,
                        outcome="cancelled", state="cancelled",
                    )
                    raise
                except Exception:
                    # A compatibility adapter can fail after claiming the
                    # row.  Treat its decision as malformed and close the
                    # attempt below rather than leaving it running.
                    recorded = None
                    policy_error = "invalid_reconciliation_result"
                except BaseException:
                    _finish_operation_attempt(
                        ledger, attempt, success=False,
                        error_code="worker_exception", decision="stop",
                        cooldown=0, metadata={"operation": action},
                        worker_id=owner_id,
                    )
                    raise

                if policy_error is None:
                    if recorded is None:
                        # Historical adapters returned ``None`` after doing
                        # their own in-memory bookkeeping.  It is safe to
                        # finalize with no retry decision, but never safe to
                        # assume a retry.
                        recorded = {}
                    if not isinstance(recorded, dict):
                        policy_error = "invalid_reconciliation_result"
                    else:
                        raw_decision = recorded.get("retry_decision", "stop")
                        if raw_decision not in ("retry", "stop"):
                            policy_error = "invalid_reconciliation_result"
                        else:
                            decision = raw_decision
                            raw_cooldown = recorded.get("cooldown_seconds", 0)
                            if (
                                isinstance(raw_cooldown, bool)
                                or not isinstance(raw_cooldown, int)
                                or raw_cooldown < 0
                            ):
                                policy_error = "invalid_reconciliation_result"
                            else:
                                cooldown = raw_cooldown
                                if cooldown_cap is not None:
                                    if (
                                        isinstance(cooldown_cap, bool)
                                        or not isinstance(cooldown_cap, int)
                                        or cooldown_cap < 0
                                    ):
                                        policy_error = "invalid_reconciliation_result"
                                    else:
                                        cooldown = min(cooldown, cooldown_cap)
                                try:
                                    max_cooldown = getattr(
                                        retry, "MAX_COOLDOWN_SECONDS", 3600
                                    )
                                    if (
                                        isinstance(max_cooldown, bool)
                                        or not isinstance(max_cooldown, int)
                                        or max_cooldown < 0
                                        or cooldown > max_cooldown
                                    ):
                                        policy_error = "invalid_reconciliation_result"
                                except (TypeError, ValueError, OverflowError):
                                    policy_error = "invalid_reconciliation_result"
                            if decision == "retry" and success:
                                policy_error = "invalid_reconciliation_result"

                if policy_error is not None:
                    # The adapter's policy response is not trustworthy.  The
                    # operation result becomes a finite failure and the
                    # currently claimed row is finalized before returning.
                    success = False
                    error_code = policy_error
                    result = _project_operation_result(result, False, error_code, action=action)
                    last_result = result
                    metadata = _safe_attempt_metadata(action, result)
                    _finish_operation_attempt(
                        ledger, attempt, success=False, error_code=error_code,
                        decision="stop", cooldown=0, metadata=metadata,
                        worker_id=owner_id,
                    )
                    return result

                # A legacy retry adapter may return a decision without writing
                # the durable row.  Complete that missing write here; this is
                # also an idempotent no-op after the native RetryEngine path.
                try:
                    current_attempt = ledger.get_attempt(attempt["attempt_id"])
                except Exception:
                    current_attempt = None
                if current_attempt and current_attempt.get("state") == "running":
                    _finish_operation_attempt(
                        ledger, attempt, success=success,
                        error_code=error_code, decision=decision,
                        cooldown=cooldown, metadata=metadata,
                        worker_id=owner_id,
                    )
            else:
                _finish_operation_attempt(
                    ledger, attempt, success=success, error_code=error_code,
                    decision="stop", cooldown=0, metadata=metadata,
                    worker_id=owner_id,
                )
        if success or decision != "retry":
            return result
        if action == "compensation" and _compensation_retry_deferred(ledger):
            # The order queue owns the next provider deadline. Preserve this
            # attempt's real counters instead of replacing them with an empty scan.
            return result
        if retry is not None and attempt is not None:
            wait_for_retry = getattr(retry, "wait_for_retry", None)
            if callable(wait_for_retry):
                if not wait_for_retry(attempt):
                    # A cancelled, malformed, or already-claimed durable
                    # schedule is not permission to run another operation.
                    return last_result
            elif cooldown > 0:
                # Compatibility adapters without the durable scheduler keep
                # their historical bounded wait until they are upgraded.
                try:
                    time.sleep(cooldown)
                except KeyboardInterrupt:
                    raise
        elif cooldown > 0:
            try:
                time.sleep(cooldown)
            except KeyboardInterrupt:
                raise

    return last_result


def _finalize_ledger(job_id, state, error_code=""):
    """Best-effort process-boundary finalization for abnormal workers."""
    if not job_id:
        return None
    path = Path(_ledger_path())
    if not path.exists():
        return None
    try:
        from core.job_ledger import JobLedger
        return JobLedger(str(path)).finalize_job(
            str(job_id), state=state, error_code=error_code,
            worker_id=current_worker_id(),
        )
    except (KeyError, ValueError, OSError) as exc:
        logging.debug("Worker ledger finalization skipped: %s", type(exc).__name__)
        return None


def _bounded_int(value, default, low, high):
    """Parse internal scheduler knobs without allowing unbounded work."""
    if isinstance(value, bool):
        return default
    try:
        return max(low, min(int(value), high))
    except (TypeError, ValueError):
        return default


def reconcile_sms_orders_once(*, order_store=None, limit=100,
                              max_attempts=3, backoff_seconds=30,
                              time_budget_seconds=None, cancel_event=None):
    """Run one bounded SMS compensation pass for workers and schedulers.

    This is an internal hook, deliberately separate from the authenticated web
    task API.  Provider exceptions are reduced to a stable error code so a
    scheduler cannot leak response payloads into logs or task state.
    """
    from services import sms_manager

    kwargs = {
        "limit": _bounded_int(limit, 100, 1, 1000),
        "max_attempts": _bounded_int(max_attempts, 3, 1, 20),
        "backoff_seconds": _bounded_int(backoff_seconds, 30, 0, 86400),
    }
    if time_budget_seconds is not None:
        kwargs["time_budget_seconds"] = time_budget_seconds
    if cancel_event is not None:
        kwargs["cancel_event"] = cancel_event
    if order_store is not None:
        kwargs["order_store"] = order_store
    try:
        result = asyncio.run(sms_manager.reconcile_expired_orders(**kwargs))
        if not isinstance(result, dict):
            return {
                "claimed": 0, "cancelled": 0, "completed": 0,
                "failed": 1, "error_code": "invalid_reconciliation_result",
            }
        try:
            counters = _strict_reconciliation_counters(result)
        except (TypeError, ValueError, OverflowError):
            return {
                "claimed": 0, "cancelled": 0, "completed": 0,
                "failed": 1, "error_code": "invalid_reconciliation_result",
            }
        # Keep only the small operational projection used by the scheduler.
        projection = dict(counters)
        error_code = str(result.get("error_code") or "").strip().lower()
        if error_code in _SAFE_RECONCILIATION_ERRORS:
            projection["error_code"] = error_code
        return projection
    except Exception as exc:
        logging.getLogger("gmail_creator_worker").warning(
            "SMS compensation pass failed: %s", type(exc).__name__
        )
        return {
            "claimed": 0, "cancelled": 0, "completed": 0,
            "failed": 1, "error_code": "reconciliation_failed",
        }


def run_sms_compensation_job(*, ledger=None, limit=100, max_attempts=3,
                             backoff_seconds=30, source="internal-scheduler",
                             time_budget_seconds=None, cancel_event=None):
    """Run one SMS compensation pass as a durable job and attempt.

    All non-HTTP compensation callers (the internal scheduler and worker
    startup recovery) use this boundary.  The provider-facing operation is
    still projected by :func:`reconcile_sms_orders_once`, while the job and
    retry lifecycle is owned by the same ledger/retry engine used by web
    tasks.  The public return value intentionally retains the historical
    reconciliation counters and finite error code, without exposing the
    scheduler-only ``success`` marker.
    """
    if ledger is None:
        from core.job_ledger import JobLedger
        ledger = JobLedger(_ledger_path())
    source = str(source or "internal-scheduler").strip()[:64]
    job = ledger.create_job(
        "compensation",
        metadata={"source": source or "internal-scheduler"},
    )
    job_id = job["job_id"]
    try:
        projected = _run_retryable_operation(
            "compensation",
            lambda: reconcile_sms_orders_once(
                order_store=ledger.sms_orders,
                limit=limit,
                max_attempts=max_attempts,
                backoff_seconds=backoff_seconds,
                time_budget_seconds=time_budget_seconds, cancel_event=cancel_event,
            ),
            ledger=ledger,
            job_id=job_id,
            subject="sms-compensation",
            # The order store persists provider backoff in next_action_at;
            # do not sleep a second time in the worker-level retry loop.
            cooldown_cap=0,
        )
        if not isinstance(projected, dict):
            projected = {
                "claimed": 0, "cancelled": 0, "completed": 0,
                "failed": 1, "error_code": "reconciliation_failed",
                "success": False,
            }
        foreign_owner_observed = projected.pop("_foreign_owner_observed", False) is True
        success = type(projected.get("success")) is bool and projected["success"]
        public_result = dict(projected)
        public_result.pop("success", None)
        if foreign_owner_observed:
            return public_result
        error_code = public_result.get("error_code", "") if not success else ""
        ledger.finish_job(
            job_id,
            state="succeeded" if success else "failed",
            terminal_error_code=error_code,
            summary=public_result,
            worker_id=current_worker_id(),
        )
        return public_result
    except (KeyboardInterrupt, asyncio.CancelledError):
        ledger.finalize_job(
            job_id, state="cancelled", error_code="cancelled",
            worker_id=current_worker_id(),
        )
        raise
    except BaseException:
        ledger.finalize_job(
            job_id, state="failed", error_code="worker_exception",
            worker_id=current_worker_id(),
        )
        raise


class _RedactingStream:
    """File-like wrapper that keeps worker stdout/stderr out of secret logs."""

    def __init__(self, stream, secret_provider):
        self._stream = stream
        self._secret_provider = secret_provider

    def write(self, value):
        if not value:
            return 0
        text = redact_text(value, self._secret_provider())
        self._stream.write(text)
        return len(value)

    def flush(self):
        return self._stream.flush()

    def isatty(self):
        return self._stream.isatty()

    def fileno(self):
        return self._stream.fileno()

    @property
    def encoding(self):
        return getattr(self._stream, "encoding", "utf-8")

    def __getattr__(self, name):
        return getattr(self._stream, name)


def _execute_impl(action, params, report, job_id=None):
    from config.settings import Config

    ledger, job = _new_ledger(action, params, job_id)
    active_job_id = job["job_id"] if job else None
    attempt_records = []
    execution_owner = current_worker_id()

    def begin(subject="", account=None, strategy="", engine="", profile_id=""):
        if ledger is None:
            return None
        return ledger.start_attempt(
            active_job_id, ordinal=len(attempt_records) + 1,
            subject=subject, account_id=(account or {}).get("id") if account else None,
            strategy=strategy, engine=engine,
            profile_id=profile_id or (account or {}).get("profile_id", ""),
            proxy=(account or {}).get("proxy") if account else None,
            worker_id=execution_owner,
        )

    def finish(attempt, success, error_code="", result=None):
        if ledger is None or attempt is None:
            return
        ledger.finish_attempt(
            attempt["attempt_id"], outcome="succeeded" if success else "failed",
            error_code=error_code, retry_decision="stop" if success else "retry",
            result=result if isinstance(result, dict) else {},
            worker_id=execution_owner,
        )
        attempt_records.append(attempt)

    def close_job(success, error_code="", summary=None):
        if ledger is None:
            return
        state = "succeeded" if success else "failed"
        current = ledger.get_job(active_job_id)
        if current and current.get("state") in {
            "succeeded", "failed", "cancelled", "blocked"
        }:
            return
        ledger.finish_job(
            active_job_id, state=state,
            terminal_error_code=error_code, summary=summary or {},
            worker_id=execution_owner,
        )

    if action == "validate":
        from core.config_validator import validate_config
        warnings, errors = validate_config()
        return {"warnings": warnings, "errors": errors}

    if action in ("create", "resume"):
        from core.creation_flow import run_creation_flow
        from core.session_resume import session_manager
        resume_state = None
        if action == "resume":
            resume_state = session_manager.load_state()
            if not resume_state or not session_manager.get_remaining(resume_state):
                raise ValueError("No unfinished session to resume")
            cfg = resume_state["batch_config"]
            params = {**cfg, "parallel": False}
            if cfg.get("engine"):
                Config.ENGINE_MODE = cfg["engine"]
        elif session_manager.has_saved_session():
            raise ValueError("An unfinished session exists; resume or clear it before creating a new batch")
        if params.get("use_sms_api") and not any((
            Config.FIVESIM_API_KEY, Config.SMS_ACTIVATE_API_KEY,
            Config.ONLINESIM_API_KEY, Config.GETSMS_API_KEY,
        )):
            raise ValueError("Premium mode requires at least one SMS API key")
        if params.get("parallel"):
            from core.batch_runner import run_batch
            completed = 0

            def on_result(result):
                nonlocal completed
                completed += 1
                report(completed, params["num_accounts"], "Account completed")

            # The serial/parallel orchestrator owns one durable attempt per
            # account.  The Web worker owns only the user-visible job; adding
            # a second orchestration attempt here causes ordinal collisions
            # and makes retry history ambiguous.
            attempt = None
            result = run_batch(
                params["num_accounts"], max_threads=params["max_threads"],
                warmup_minutes=params["warmup_minutes"], flow_mode=params["flow_mode"],
                use_sms_api=params["use_sms_api"], on_result=on_result,
                ledger=ledger, job_id=active_job_id,
            )
            if result["total"] == 0:
                finish(attempt, False, "configuration_error", result)
                close_job(False, "configuration_error", result)
                raise ValueError("Parallel creation requires YOUR_PASSWORD or config/password.txt")
        else:
            attempt = None
            result = run_creation_flow(
                params["num_accounts"], warmup_minutes=params["warmup_minutes"],
                flow_mode=params["flow_mode"], use_sms_api=params["use_sms_api"],
                on_progress=lambda done, total, stats: report(done, total, str(stats)),
                resume_state=resume_state,
                ledger=ledger, job_id=active_job_id,
            )
        from core.retry_engine import retry_engine
        result["retry_stats"] = retry_engine.get_stats()
        success = bool(result.get("failures", 0) == 0 and result.get("successes", 0) > 0)
        finish(attempt, success, "" if success else "creation_failed", result)
        close_job(success, "" if success else "creation_failed", result)
        if active_job_id:
            result["job_id"] = active_job_id
        return result

    if action in ("health", "warm"):
        from core.account_manager import account_manager
        accounts = account_manager.get_all()
        ids = set(params.get("account_ids", []))
        if ids:
            if ids - {account["id"] for account in accounts}:
                raise ValueError("One or more selected accounts no longer exist")
            accounts = [account for account in accounts if account["id"] in ids]
        if not accounts:
            raise ValueError("No accounts available")
        if ledger is not None and active_job_id:
            current = ledger.get_job(active_job_id)
            if current and current.get("state") in {
                "succeeded", "failed", "cancelled", "blocked"
            }:
                raise ValueError("job is already finalized")
        results = []
        for index, account in enumerate(accounts):
            if action == "health":
                from core.health_checker import AccountHealthChecker
                # Keep compatibility with tests/embedders that provide a
                # checker instance instead of the concrete class.  The real
                # checker supports both class and instance invocation.
                checker = AccountHealthChecker() if isinstance(AccountHealthChecker, type) else AccountHealthChecker
                profile_id = account.get("profile_id") or None
                engine = account.get("engine") or None
                proxy = account.get("proxy") or None

                def check_once():
                    if profile_id:
                        checked = checker.check_single(
                            account["email"], account["password"],
                            profile_id=profile_id, engine=engine, proxy=proxy,
                        )
                    elif account.get("profile_path"):
                        # A path-only row is migration state, not a browser
                        # identity.  Keep the IMAP fact but make the browser
                        # channel explicitly unavailable until adopt_legacy().
                        legacy_checker = getattr(checker, "check_legacy", None)
                        checked = None
                        if callable(legacy_checker):
                            checked = legacy_checker(
                                account["email"], account["password"]
                            )
                        if not isinstance(checked, dict):
                            checked = checker.check_single(
                                account["email"], account["password"]
                            )
                        if isinstance(checked, dict):
                            checked = dict(checked)
                            checked.update({
                                "browser_status": "profile_unavailable",
                                "browser_message": "Legacy browser profile requires explicit adoption",
                                "last_error_code": "legacy_unbound",
                            })
                    else:
                        checked = checker.check_single(
                            account["email"], account["password"]
                        )
                    # New snapshots contain independent channel facts and must
                    # be committed atomically.  A reduced result from an older
                    # compatibility checker still uses the legacy projection.
                    if isinstance(checked, dict) and {
                        "browser_status", "mailbox_status", "status"
                    } <= set(checked):
                        updater = getattr(account_manager.db, "update_health_snapshot", None)
                        if not callable(updater) or not updater(account["email"], checked):
                            raise RuntimeError("Unable to persist account health snapshot")
                    elif isinstance(checked, dict) and checked.get("status") not in (
                        "error", "network_error", "unknown"
                    ):
                        if not account_manager.db.update_account_status(
                            account["email"], checked.get("status", ""),
                            checked.get("message", "")
                        ):
                            raise RuntimeError("Unable to persist account health status")
                    return checked

                result = _run_retryable_operation(
                    action, check_once, ledger=ledger, job_id=active_job_id,
                    subject=account.get("email", ""), account=account,
                    engine=engine or "", profile_id=profile_id or "",
                    proxy=proxy,
                )
            else:
                from core.account_warmer import warm_account

                def warm_once():
                    return warm_account(
                        account["email"], account["password"],
                        duration_minutes=params.get("duration_minutes", 3),
                        profile_id=account.get("profile_id") or None,
                        engine=params.get("engine"),
                        proxy=account.get("proxy") or None,
                    )

                result = _run_retryable_operation(
                    action, warm_once, ledger=ledger, job_id=active_job_id,
                    subject=account.get("email", ""), account=account,
                    # Record and reuse the manifest engine, even when the UI
                    # supplied no override (or an invalid override).
                    engine=account.get("engine") or params.get("engine") or "",
                    profile_id=account.get("profile_id") or "",
                    proxy=account.get("proxy") or None,
                )
            results.append(result)
            print(
                f"{action}: {account['email']}: "
                f"{sanitize_operation_value(result, _worker_secret_provider())}",
                flush=True,
            )
            report(index + 1, len(accounts), account["email"])
        if action == "health":
            output = {"results": results, "summary": AccountHealthChecker.get_summary(results)}
        else:
            output = {"results": results, "successes": sum(r["success"] for r in results),
                      "failures": sum(not r["success"] for r in results)}
        all_success = all(bool(item.get("success", True)) for item in results)
        close_job(all_success, "" if all_success else action + "_failed", output)
        if active_job_id:
            output["job_id"] = active_job_id
        return output

    if action == "compensation":
        params = params if isinstance(params, dict) else {}
        stats = _run_retryable_operation(
            "compensation",
            lambda: reconcile_sms_orders_once(
                order_store=ledger.sms_orders if ledger is not None else None,
                limit=params.get("limit", 100),
                max_attempts=params.get("max_attempts", 3),
                backoff_seconds=params.get("backoff_seconds", 30),
            ),
            ledger=ledger, job_id=active_job_id,
            subject="sms-compensation",
            # The order store already persists ``next_action_at`` with the
            # provider backoff.  Do not block a web worker for the creation
            # cooldown a second time; durable reconciliation will retry it.
            cooldown_cap=0,
        )
        if not isinstance(stats, dict):
            stats = {"claimed": 0, "cancelled": 0, "completed": 0,
                     "failed": 1, "error_code": "reconciliation_failed"}
        foreign_owner_observed = stats.pop("_foreign_owner_observed", False) is True
        if callable(report):
            report(
                stats.get("cancelled", 0) + stats.get("completed", 0),
                max(1, stats.get("claimed", 0)),
                "SMS compensation pass completed",
            )
        failed = bool(stats.get("failed", 0) or stats.get("error_code"))
        output = dict(stats)
        output["success"] = not failed
        if active_job_id:
            output["job_id"] = active_job_id
        if foreign_owner_observed:
            return output
        close_job(
            not failed,
            stats.get("error_code", "" if not failed else "compensation_failed"),
            output,
        )
        return output

    if action == "proxy_test":
        from core.proxy_manager import proxy_manager
        from core.trust_builder import network_trust_check
        network = safe_network_trust_summary(network_trust_check())
        health = proxy_manager.check_all_health()
        return {"network": network, "health": health, "pools": proxy_manager.get_stats()}

    if action == "proxy_fetch":
        from core.proxy_fetcher import fetch_and_test, save_proxies_to_file
        working = fetch_and_test(max_proxies=20, test_count=50)
        if not working:
            raise RuntimeError("No working proxies found")
        return {"found": len(working), "saved": save_proxies_to_file(working, Config.PROXY_FILE)}

    if action == "telegram_test":
        from core.telegram_notifier import notifier
        ok, message = notifier.test_connection()
        if not ok:
            raise RuntimeError(message)
        if not notifier.send("Gmail Creator Pro: Web test notification"):
            raise RuntimeError("Connected to bot, but sending a message to the chat failed")
        return {"message": message}

    if action == "sms_balance":
        from services.sms_manager import check_balance
        result = asyncio.run(check_balance())
        if not result:
            raise ValueError("Balance checks require a configured 5sim or SMS-Activate key")
        if all(value is None for value in result.values()):
            raise RuntimeError("Unable to retrieve balances; check API keys and connection")
        return result

    if action == "migrate":
        from core.database import DatabaseManager
        return {"migrated": DatabaseManager().run_migration()}

    if action == "voice":
        from services.voice import run_server
        run_server()
        return {"message": "Voice server stopped"}
    raise ValueError("Unsupported action: " + action)


def execute(action, params, report, job_id=None):
    """Run one operation and close its durable job on every exit path.

    The subprocess entry point has a second safety net, but direct callers
    (CLI integrations and tests) must get the same ledger guarantee.  Create
    the job before entering the implementation so an exception raised before
    the first attempt cannot leave a pending record behind.
    """
    ledger_job_id = job_id
    if ledger_job_id is None and action in (
            "create", "resume", "health", "warm", "compensation"):
        _ledger, job = _new_ledger(action, params, None)
        ledger_job_id = job["job_id"] if job else None
    try:
        return _execute_impl(action, params, report, job_id=ledger_job_id)
    except (KeyboardInterrupt, asyncio.CancelledError):
        _finalize_ledger(ledger_job_id, "cancelled", "cancelled")
        raise
    except BaseException as exc:
        # Do not persist arbitrary exception text; the worker's public error
        # boundary can still render the exception type where appropriate.
        _finalize_ledger(ledger_job_id, "failed", "worker_exception")
        raise


def main():
    task_id = sys.argv[1]
    store = TaskStore(Path(os.environ["WEB_TASK_DIRECTORY"]))
    task = store.get(task_id)
    if task is None:
        raise ValueError("Task does not exist")

    global _worker_secret_provider
    _worker_secret_provider = store._known_secret_values
    sys.stdout = _RedactingStream(sys.stdout, _worker_secret_provider)
    sys.stderr = _RedactingStream(sys.stderr, _worker_secret_provider)

    def cancelled(signum, frame):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, cancelled)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout)], force=True)

    parent_pid = int(os.environ.get("WEB_PARENT_PID", os.getppid()))
    finished = threading.Event()

    def mark_cleanup_failed(reason):
        """Record cleanup uncertainty only while this worker owns the row."""
        try:
            current = store.get(task_id)
            if not current or current.get("status") not in ACTIVE:
                return False
            changed = _update_task_if_status(
                store, task_id, (current.get("status"),),
                status="cleanup_failed", error=reason, finished_at=now(),
            )
            if changed:
                _finalize_ledger(task_id, "failed", "cleanup_failed")
            return changed
        except (OSError, sqlite3.Error, ValueError, TypeError):
            return False

    def watch_parent():
        while not finished.wait(1):
            if os.getppid() != parent_pid:
                try:
                    current = store.get(task_id)
                    if not current or current.get("status") not in ACTIVE:
                        return
                    if current.get("status") == "running":
                        changed = _update_task_if_status(
                            store, task_id, ("running",), status="stopping"
                        )
                        if not changed:
                            # A normal completion/cancellation won the race;
                            # never signal a PID after ownership was lost.
                            current = store.get(task_id)
                            if not current or current.get("status") != "stopping":
                                return
                except (OSError, ValueError):
                    return
                # This worker is normally a fresh process group.  Once it
                # signals that group, the watchdog thread dies with it, so an
                # in-group thread cannot persist the final process/lease fact.
                # Hand the wait-and-record responsibility to a detached helper
                # first; it survives the group kill and writes interrupted or
                # cleanup_failed only after an explicit group observation.
                helper_started = False
                try:
                    helper_command = [
                        sys.executable, "-m", "web.worker_cleanup", task_id,
                        str(os.getpid()), os.environ.get("WEB_TASK_DIRECTORY", ""),
                        # The web server always injects LEDGER_DB_PATH, but
                        # direct/legacy launches may only provide the task
                        # directory.  Resolve the same fallback as the worker
                        # itself so the detached helper can finalize the
                        # corresponding ledger job instead of silently
                        # dropping the durable terminal fact.
                        os.environ.get("LEDGER_DB_PATH") or _ledger_path(),
                    ]
                    subprocess.Popen(
                        helper_command,
                        cwd=Path(__file__).resolve().parents[1],
                        env=os.environ.copy(),
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        close_fds=True,
                        start_new_session=(os.name != "nt"),
                    )
                    helper_started = True
                except (OSError, ValueError):
                    # If the detached helper cannot be started, record a
                    # conservative cleanup failure before killing this group;
                    # no surviving process can prove a later cancellation.
                    mark_cleanup_failed(
                        "cleanup_failed: detached worker cleanup could not start"
                    )
                try:
                    if os.name == "nt":
                        os.kill(os.getpid(), signal.SIGTERM)
                    else:
                        os.killpg(os.getpid(), signal.SIGTERM)
                        if not helper_started:
                            # The fallback above already recorded failure; the
                            # worker still exits promptly so it cannot continue
                            # touching a profile after its parent disappeared.
                            return
                except ProcessLookupError:
                    # The worker group exited between the two signals.
                    return
                except (PermissionError, OSError):
                    # A supervisor must not claim cancellation succeeded when
                    # it cannot signal the process group.  Keep the durable
                    # task/ledger state explicit and avoid a thread traceback.
                    mark_cleanup_failed(
                        "cleanup_failed: worker process group could not be signalled"
                    )
                return

    # A lost parent must not let this cleanup thread disappear with the main
    # worker before resistant browser descendants receive the final signal.
    watchdog = threading.Thread(target=watch_parent)
    watchdog.start()

    def report(completed, total, message):
        store.update(task_id, progress={"completed": completed, "total": total, "message": message})

    def _finish_running(status, *, result=None, error=None):
        """Claim the running row before publishing a worker terminal state.

        A detached cleanup helper or a cancellation supervisor can finalize the
        same task while the operation is unwinding.  The worker must never
        overwrite that observation with a late ``completed``/``failed`` row.
        Returning ``False`` means another owner already won the terminal-state
        race (or the durable store is unavailable).
        """
        values = {"status": status, "finished_at": now()}
        if result is not None:
            values["result"] = result
        if error is not None:
            values["error"] = error
        try:
            return _update_task_if_status(store, task_id, ("running",), **values)
        except (OSError, ValueError, TypeError):
            return False

    try:
        # A process can disappear after claiming an attempt but before it can
        # publish a terminal task row.  Recover those durable rows before this
        # worker claims new work; a live operation will keep its row fresh via
        # ``_AttemptHeartbeat`` below.
        if task["action"] in ("create", "resume", "health", "warm", "compensation"):
            recover_stale_attempts()
        # Reconcile abandoned SMS orders before starting a new operation.  The
        # dedicated compensation action performs its own pass below, so it is
        # excluded here to avoid duplicate provider calls in one worker.
        if task["action"] != "compensation":
            run_sms_compensation_job(source="startup-reconciliation")
        result = _invoke_execute(task["action"], task["params"], report, task_id)
        current = store.get(task_id)
        if current and current.get("status") == "running":
            if (current.get("progress") or {}).get("completed", 0) == 0:
                report(1, 1, "Completed; inspect the result for operation errors")
            claimed = _finish_running("completed", result=result)
            current = store.get(task_id) if not claimed else None
            status = "completed" if claimed else (current or {}).get("status", "cleanup_failed")
        else:
            # Another lifecycle owner already wrote a terminal state, or the
            # row disappeared.  Preserve that fact and do not publish a late
            # operation result.
            status = (current or {}).get("status", "cleanup_failed")
        print("Task " + status, flush=True)
    except (KeyboardInterrupt, asyncio.CancelledError):
        current = None
        try:
            current = store.get(task_id)
        except (OSError, ValueError, TypeError):
            pass
        if current and current.get("status") == "running":
            if _finish_running("cancelled"):
                _finalize_ledger(task_id, "cancelled", "cancelled")
        print("Task cancelled. Completed serial accounts remain resumable.", flush=True)
    except BaseException as exc:
        # This is the process boundary: retain only a stable exception type in
        # durable task/ledger state.  Provider payloads and tracebacks belong
        # nowhere in a user-visible worker record.
        code = stable_exception_code(exc, "worker_error")
        if _finish_running("failed", error=code):
            _finalize_ledger(task_id, "failed", "worker_exception")
        print(code, flush=True)
        sys.exit(1)
    finally:
        finished.set()


if __name__ == "__main__":
    main()
