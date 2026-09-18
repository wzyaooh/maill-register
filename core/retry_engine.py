"""
Retry Engine - Smart retry with strategy rotation for account creation
"""
import logging
import math
import os
import random
import time
import threading
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from core.secret_safety import normalize_error_code
from core.job_ledger import current_worker_id

logger = logging.getLogger('gmail_creator_retry')

_DEFAULT_HEARTBEAT_INTERVAL = 15.0
_MAX_HEARTBEAT_INTERVAL = 300.0


def _heartbeat_interval(value=None):
    """Bound the optional attempt heartbeat period."""
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
    if period <= 0:
        return 0.0
    return min(period, _MAX_HEARTBEAT_INTERVAL)


class _AttemptHeartbeat:
    """Owner-fenced heartbeat loop shared by creation retry callers."""

    def __init__(self, ledger, attempt, worker_id, interval=None):
        self.ledger = ledger
        self.attempt = attempt if isinstance(attempt, dict) else None
        self.worker_id = worker_id or ""
        self.interval = _heartbeat_interval(interval)
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
                    return
            except BaseException as exc:
                # A liveness write must never replace the browser/provider
                # result or expose an exception payload to the retry ledger.
                logger.debug("Attempt heartbeat unavailable: %s", type(exc).__name__)

    def stop(self):
        self._stopped.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(1.0, self.interval + 0.5))
        self._thread = None


class RetryScheduler:
    """Small durable scheduler facade for retry consumers.

    The ledger owns the schedule and the atomic due-claim.  This facade keeps
    orchestration code from reconstructing cooldown timestamps in memory and
    gives a future process scheduler one stable interface to poll and claim.
    ``wait_until_due`` is provided for synchronous callers that have not yet
    been moved to an external scheduler; it always re-reads the durable row.
    """

    def __init__(self, ledger, worker_id=None):
        if ledger is None:
            raise ValueError("ledger is required")
        self.ledger = ledger
        self.worker_id = current_worker_id() if worker_id is None else worker_id or ""

    def due(self, *, now=None, limit=100):
        return self.ledger.list_due_attempts(now=now, limit=limit)

    def claim(self, attempt_id, *, now=None):
        try:
            return self.ledger.claim_due_retry(
                attempt_id, now=now, worker_id=self.worker_id or None,
            )
        except TypeError as exc:
            if not self.worker_id or "worker_id" not in str(exc):
                raise
            return self.ledger.claim_due_retry(attempt_id, now=now)

    def wait_until_due(self, attempt_id, *, cancel_event=None, max_wait=None):
        """Wait on a persisted schedule without losing it on process errors."""
        event = cancel_event or threading.Event()
        started = time.monotonic()
        while True:
            if event.is_set():
                return False
            attempt = self.ledger.get_attempt(attempt_id)
            if attempt is None:
                raise KeyError(attempt_id)
            if attempt.get("retry_decision") != "retry":
                return False
            due_at = attempt.get("next_attempt_at")
            if not due_at:
                # Every durable retry must carry a schedule.  Treat legacy,
                # missing, or malformed rows as non-runnable instead of
                # turning a persistence defect into an immediate retry.
                return False
            text = str(due_at)
            if text.endswith(("Z", "z")):
                text = text[:-1] + "+00:00"
            try:
                due = datetime.fromisoformat(text)
            except (TypeError, ValueError, OverflowError):
                return False
            if due.tzinfo is None:
                due = due.replace(tzinfo=timezone.utc)
            delay = max(0.0, (due.astimezone(timezone.utc) - datetime.now(timezone.utc)).total_seconds())
            if delay <= 0:
                return True
            if max_wait is not None:
                if isinstance(max_wait, bool) or not isinstance(max_wait, (int, float)):
                    raise ValueError("max_wait must be numeric")
                remaining = float(max_wait) - (time.monotonic() - started)
                if remaining <= 0:
                    return False
                delay = min(delay, remaining)
            event.wait(min(delay, 1.0))


def _strict_attempt_count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("attempt_count must be a non-negative integer")
    return value


class CreationError:
    PHONE_REQUIRED = "phone_required"
    QR_BLOCKED = "qr_blocked"
    IP_FLAGGED = "ip_flagged"
    USERNAME_TAKEN = "username_taken"
    BROWSER_CRASH = "browser_crash"
    TIMEOUT = "timeout"
    CAPTCHA = "captcha"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"
    SMS_POLL_FAILED = "sms_poll_failed"
    SMS_TIMEOUT = "sms_timeout"
    SMS_NO_NUMBER = "sms_no_number"
    SMS_PHONE_REJECTED = "sms_phone_rejected"
    SMS_NO_PHONE_INPUT = "sms_no_phone_input"
    SMS_NO_CODE_PAGE = "sms_no_code_page"
    SMS_NO_CODE_INPUT = "sms_no_code_input"
    SMS_CODE_REJECTED = "sms_code_rejected"
    SMS_ERROR = "sms_error"
    SMS_FINISH_FAILED = "sms_finish_failed"
    SMS_CANCEL_FAILED = "sms_cancel_failed"
    SMS_NO_BALANCE = "sms_no_balance"
    SMS_IMPORT_ERROR = "sms_import_error"
    SMS_MISSING_ATTEMPT_CONTEXT = "sms_missing_attempt_context"
    SEND_SMS_BLOCKED = "send_sms_blocked"


class RetryEngine:
    STRATEGIES = ["standard", "youtube", "workspace", "mobile_ua"]

    # This value is the maximum *total number of attempts* for one logical
    # subject, including the first try.  Keep the historical name as a public
    # compatibility alias because callers and configuration patches use it.
    MAX_ATTEMPTS = 3
    MAX_RETRIES = MAX_ATTEMPTS
    COOLDOWN_BASE = 15
    # A retry policy is untrusted configuration.  Keep one hard upper bound
    # so a malformed or accidentally huge base cannot block a worker for an
    # unbounded period or overflow the durable integer column.
    MAX_COOLDOWN_SECONDS = 3600

    STRATEGY_MAP = {
        CreationError.QR_BLOCKED: ["youtube", "workspace", "mobile_ua"],
        CreationError.PHONE_REQUIRED: ["youtube", "mobile_ua", "workspace"],
        CreationError.IP_FLAGGED: ["standard", "youtube"],
        CreationError.USERNAME_TAKEN: None,
        CreationError.BROWSER_CRASH: ["standard"],
        CreationError.TIMEOUT: ["standard"],
        CreationError.CAPTCHA: ["youtube", "workspace"],
        CreationError.UNSUPPORTED: [],
        CreationError.UNKNOWN: ["youtube", "standard"],
    }

    # Browser health/warm and provider compensation use the same durable
    # attempt protocol as creation, but their error vocabulary is different.
    # Keep the allow-list explicit so a deterministic profile/identity fault
    # cannot cause an unbounded or misleading retry loop.
    OPERATION_RETRYABLE_ERRORS = {
        "health": frozenset({
            "runtime_unavailable", "network_error", "error", "profile_busy",
            "timeout", "provider_error", "provider_timeout", "identity_unavailable",
        }),
        "warm": frozenset({
            "runtime_unavailable", "network_error", "activity_failed",
            "profile_busy", "timeout", "error", "provider_error",
            "provider_timeout", "identity_unavailable",
        }),
        "compensation": frozenset({
            "provider_error", "provider_timeout", "reconciliation_failed",
            "timeout", "compensation_claimed", "error",
        }),
    }

    OPERATION_NON_RETRYABLE_ERRORS = frozenset({
        CreationError.UNSUPPORTED, "cancelled", "profile_conflict",
        "account_mismatch", "challenge", "runtime_mismatch", "proxy_mismatch",
        "proxy_unavailable", "profile_unavailable", "legacy_unbound",
        "login_required", "password_changed", "locked", "suspended",
        "cleanup_failed",
    })

    # SMS verification failures have different ownership semantics.  A poll
    # or code failure can safely acquire a fresh number on a later attempt;
    # finish/cancel/configuration failures must be left to the durable order
    # compensation queue and must not start a second account flow.
    CREATION_NON_RETRYABLE_ERRORS = frozenset({
        "sms_finish_failed", "sms_cancel_failed", "sms_no_balance",
        "sms_import_error", "sms_missing_attempt_context",
    })

    def __init__(self, ledger=None, job_id: Optional[str] = None,
                 worker_id: Optional[str] = None):
        self._attempt_history = []
        self._strategy_scores = {s: 50 for s in self.STRATEGIES}
        self.ledger = ledger
        self.job_id = job_id
        if worker_id is not None and not isinstance(worker_id, str):
            raise ValueError("worker_id must be text")
        self.worker_id = current_worker_id() if worker_id is None else worker_id.strip()

    def attach_ledger(self, ledger, job_id: str, worker_id: Optional[str] = None):
        """Bind this engine to one durable job without changing retry policy."""
        if ledger is None or not job_id:
            raise ValueError("ledger and job_id are required")
        if not isinstance(job_id, str) or not job_id.strip():
            raise ValueError("job_id must be a text identifier")
        self.ledger = ledger
        self.job_id = job_id.strip()
        if worker_id is not None:
            if not isinstance(worker_id, str):
                raise ValueError("worker_id must be text")
            self.worker_id = worker_id.strip()
        return self

    def start_heartbeat(self, attempt, interval=None):
        """Start a bounded heartbeat for one claimed durable attempt."""
        return _AttemptHeartbeat(
            self.ledger, attempt, self.worker_id, interval=interval
        ).start()

    @staticmethod
    def stop_heartbeat(handle):
        """Stop a heartbeat handle without changing the operation outcome."""
        if handle is None:
            return
        stop = getattr(handle, "stop", None)
        if callable(stop):
            stop()

    def wait_for_retry(self, attempt, *, cancel_event=None, max_wait=None):
        """Wait for and atomically claim one durable retry schedule.

        The returned attempt is in ``pending`` state and is the only durable
        hand-off accepted by a subsequent attempt.  ``None`` means the wait
        was cancelled, the timestamp was malformed, or another worker claimed
        the schedule first.  Ledger-less compatibility callers retain their
        historical immediate-progress behavior.
        """
        if self.ledger is None:
            return True
        if not isinstance(attempt, dict) or not attempt.get("attempt_id"):
            return None

        scheduler = RetryScheduler(self.ledger, worker_id=self.worker_id)
        ready = scheduler.wait_until_due(
            attempt["attempt_id"], cancel_event=cancel_event, max_wait=max_wait,
        )
        if not ready:
            return None
        try:
            return scheduler.claim(attempt["attempt_id"])
        except (KeyError, ValueError, OSError):
            # A concurrent worker or a repaired row owns the outcome.  Do not
            # reconstruct a cooldown in memory and risk a duplicate attempt.
            return None

    def resume_existing_retry(self, *, subject="", account_id=None,
                              cancel_event=None, max_wait=None):
        """Recover the latest retry row after a worker/process restart."""
        if self.ledger is None or not self.job_id:
            return None
        finder = getattr(self.ledger, "list_retry_attempts", None)
        if not callable(finder):
            return None
        candidates = finder(
            self.job_id, account_id=account_id,
            subject=subject if account_id is None else None,
        )
        if not candidates:
            return None
        claimed = self.wait_for_retry(
            candidates[0], cancel_event=cancel_event, max_wait=max_wait,
        )
        return claimed if claimed is not None else False

    @classmethod
    def retryable_errors(cls, operation: str):
        """Return a copy of the finite retry allow-list for ``operation``."""
        name = str(operation or "").strip().lower()
        return frozenset(cls.OPERATION_RETRYABLE_ERRORS.get(name, ()))

    @classmethod
    def _validate_operation(cls, operation: Optional[str]) -> Optional[str]:
        if operation is None:
            return None
        if not isinstance(operation, str):
            raise ValueError("operation must be text")
        name = operation.strip().lower()
        if name != "create" and name not in cls.OPERATION_RETRYABLE_ERRORS:
            raise ValueError("unsupported retry operation")
        return name

    def begin_attempt(self, strategy: str, *, job_id: Optional[str] = None,
                      ordinal: Optional[int] = None,
                      subject: str = "", account_id: Optional[int] = None,
                      engine: str = "", profile_id: str = "", proxy: Any = None,
                      metadata: Optional[Dict[str, Any]] = None):
        """Create a running ledger attempt when a durable job is attached."""
        ledger = self.ledger
        target_job = job_id or self.job_id
        if ledger is None or not target_job:
            return None
        return ledger.start_attempt(
            target_job, ordinal=ordinal, strategy=strategy, subject=subject, account_id=account_id,
            engine=engine, profile_id=profile_id, proxy=proxy,
            worker_id=self.worker_id, metadata=metadata or {},
        )

    def should_retry(self, error_type, attempt_count, operation=None):
        """Return whether another attempt is safe for this operation.

        ``operation`` is optional for backwards compatibility with the
        creation-only API.  When supplied, non-creation calls must opt into a
        finite retryable error allow-list; unknown/deterministic errors stop.
        ``attempt_count`` is the one-based count for one logical subject.
        """
        attempt_count = _strict_attempt_count(attempt_count)
        normalized = normalize_error_code(
            error_type or CreationError.UNKNOWN,
            default=CreationError.UNKNOWN,
            allow_empty=False,
        )
        if operation is not None and str(operation).strip().lower() != "create":
            operation_name = str(operation).strip().lower()
            if normalized in self.OPERATION_NON_RETRYABLE_ERRORS:
                return False
            allowed = self.OPERATION_RETRYABLE_ERRORS.get(operation_name)
            if allowed is None or normalized not in allowed:
                return False
        if normalized == CreationError.UNSUPPORTED:
            return False
        if normalized in self.CREATION_NON_RETRYABLE_ERRORS:
            return False
        try:
            max_attempts = int(self.MAX_RETRIES)
        except (TypeError, ValueError):
            max_attempts = 1
        if max_attempts < 1 or attempt_count >= max_attempts:
            return False
        if normalized == CreationError.USERNAME_TAKEN:
            return True
        if normalized == CreationError.IP_FLAGGED and attempt_count >= 2:
            return False
        return True

    def get_next_strategy(self, failed_strategy, error_type):
        error_type = normalize_error_code(
            error_type, default=CreationError.UNKNOWN, allow_empty=False
        )
        preferred = self.STRATEGY_MAP.get(error_type, self.STRATEGIES)
        if preferred is None:
            return failed_strategy

        candidates = [s for s in preferred if s != failed_strategy]
        if not candidates:
            candidates = [s for s in self.STRATEGIES if s != failed_strategy]
        if not candidates:
            return random.choice(self.STRATEGIES)

        return max(candidates, key=lambda s: self._strategy_scores.get(s, 50))

    def get_cooldown(self, attempt_count, error_type):
        attempt_count = _strict_attempt_count(attempt_count)
        error_type = normalize_error_code(
            error_type, default=CreationError.UNKNOWN, allow_empty=False
        )
        try:
            configured_base = float(self.COOLDOWN_BASE)
        except (TypeError, ValueError, OverflowError):
            configured_base = 0.0
        if not math.isfinite(configured_base) or configured_base < 0:
            configured_base = 0.0

        multiplier = 1.0
        if error_type == CreationError.IP_FLAGGED:
            multiplier = 3.0
        elif error_type == CreationError.QR_BLOCKED:
            multiplier = 2.0
        try:
            base = configured_base * float(attempt_count + 1) * multiplier
        except (OverflowError, ValueError):
            base = float(self.MAX_COOLDOWN_SECONDS)

        try:
            jitter = float(random.uniform(0.8, 1.5))
        except (TypeError, ValueError, OverflowError):
            jitter = 1.0
        if math.isnan(jitter):
            jitter = 1.0
        elif jitter < 0:
            jitter = 0.0
        if not math.isfinite(jitter):
            jitter = float(self.MAX_COOLDOWN_SECONDS)

        try:
            cooldown = base * jitter
        except (OverflowError, ValueError):
            cooldown = float(self.MAX_COOLDOWN_SECONDS)
        if not math.isfinite(cooldown):
            cooldown = float(self.MAX_COOLDOWN_SECONDS)
        cooldown = max(0.0, min(cooldown, float(self.MAX_COOLDOWN_SECONDS)))
        return int(cooldown)

    def record_attempt(self, strategy, success, error_type=None, *,
                       attempt_id: Optional[str] = None,
                       job_id: Optional[str] = None,
                       attempt_count: Optional[int] = None,
                       operation: Optional[str] = None,
                       subject: str = "", account_id: Optional[int] = None,
                       engine: str = "", profile_id: str = "", proxy: Any = None,
                       metadata: Optional[Dict[str, Any]] = None,
                       cooldown_cap: Optional[int] = None):
        """Record an outcome in memory and, when bound, in the durable ledger."""
        # Validate every caller-controlled decision before touching either the
        # in-memory history or the durable ledger.  In particular, ``bool``
        # coercion here would turn a malformed adapter response such as the
        # string ``"false"`` into a recorded success.
        if type(success) is not bool:
            raise ValueError("success must be a boolean")
        operation_name = self._validate_operation(operation) or "create"
        if attempt_count is not None:
            attempt_count = _strict_attempt_count(attempt_count)
        if cooldown_cap is not None:
            if isinstance(cooldown_cap, bool) or not isinstance(cooldown_cap, int) or cooldown_cap < 0:
                raise ValueError("cooldown_cap must be a non-negative integer")

        # Creation strategies are a fixed set, while health/warm/
        # compensation use operation names as their ledger strategy.  Keep
        # one scoring path for both without letting an operation-only name
        # raise a KeyError before its durable attempt is recorded.
        strategy = str(strategy or "standard")
        normalized_error = normalize_error_code(
            error_type, default=CreationError.UNKNOWN, allow_empty=False
        ) if not success else ""

        ledger = self.ledger
        target_job = job_id or self.job_id
        created_attempt = False
        attempt = None

        if ledger is not None and target_job:
            target_job = str(target_job)
            get_job = getattr(ledger, "get_job", None)
            job = get_job(target_job) if callable(get_job) else None
            if job is None:
                raise KeyError(target_job)
            if job.get("state") in {"succeeded", "failed", "cancelled", "blocked"}:
                raise ValueError("job is already finalized")

            if attempt_id is not None:
                get_attempt = getattr(ledger, "get_attempt", None)
                attempt = get_attempt(attempt_id) if callable(get_attempt) else None
                if attempt is None:
                    raise KeyError(attempt_id)
                if str(attempt.get("job_id") or "") != target_job:
                    raise ValueError("attempt does not belong to job")
            else:
                # Keep the convenience API atomic: callers that did not
                # explicitly begin an attempt get one start+finish pair.  No
                # memory statistic is changed until that pair is durable.
                attempt = self.begin_attempt(
                    strategy, job_id=target_job, subject=subject,
                    account_id=account_id, engine=engine, profile_id=profile_id,
                    proxy=proxy, metadata=metadata,
                )
                if not isinstance(attempt, dict) or not attempt.get("attempt_id"):
                    raise ValueError("ledger did not create an attempt")
                attempt_id = attempt["attempt_id"]
                created_attempt = True

            if attempt_count is None:
                if account_id is not None or subject:
                    counter = getattr(ledger, "count_attempts", None)
                    if callable(counter):
                        attempt_count = counter(
                            target_job, account_id=account_id, subject=subject,
                        )
                    else:
                        attempt_count = len(ledger.list_attempts(target_job))
                else:
                    attempt_count = len(ledger.list_attempts(target_job))
                attempt_count = _strict_attempt_count(attempt_count)

            should_retry = (not success) and self.should_retry(
                normalized_error or CreationError.UNKNOWN, attempt_count,
                operation=operation_name,
            )
            retry_decision = "retry" if should_retry else "stop"
            cooldown = (
                self.get_cooldown(max(0, attempt_count - 1), normalized_error)
                if should_retry else 0
            )
            if cooldown_cap is not None:
                cooldown = min(cooldown, cooldown_cap)
            try:
                recorded = ledger.finish_attempt(
                    attempt_id,
                    outcome="succeeded" if success else "failed",
                    error_code=normalized_error,
                    retry_decision=retry_decision,
                    cooldown_seconds=cooldown,
                    result=metadata or {},
                    worker_id=self.worker_id or None,
                )
            except Exception:
                if created_attempt:
                    # A newly claimed row must not remain ``running`` when its
                    # finalization fails.  Best effort is intentional: the
                    # original durable error remains the one surfaced to the
                    # caller, and no in-memory stats are updated.
                    try:
                        ledger.finish_attempt(
                            attempt_id,
                            state="failed", outcome="failed",
                            error_code="worker_error", retry_decision="stop",
                            result={"operation": operation_name},
                            worker_id=self.worker_id or None,
                        )
                    except Exception:
                        pass
                raise

            # Durable state is authoritative.  Only after it commits do we
            # expose this outcome through the process-local strategy stats.
            self._record_memory(strategy, success, normalized_error)
            return recorded

        # Compatibility mode for callers that intentionally run without a
        # ledger.  It retains the historical ``None`` return value.
        self._record_memory(strategy, success, normalized_error)
        return None

    def _record_memory(self, strategy: str, success: bool, normalized_error: str) -> None:
        """Apply one already-validated outcome to local retry statistics."""
        self._strategy_scores.setdefault(strategy, 50)
        entry = {
            "strategy": strategy,
            "success": success,
            "error_type": normalized_error or None,
            "timestamp": time.time(),
        }
        self._attempt_history.append(entry)
        if success:
            self._strategy_scores[strategy] = min(
                100, self._strategy_scores[strategy] + 15
            )
        else:
            self._strategy_scores[strategy] = max(
                0, self._strategy_scores[strategy] - 10
            )

    def get_best_initial_strategy(self):
        return max(self.STRATEGIES, key=lambda s: self._strategy_scores.get(s, 50))

    def get_stats(self):
        total = len(self._attempt_history)
        successes = sum(1 for a in self._attempt_history if a["success"])
        failures = total - successes
        error_counts = {}
        for a in self._attempt_history:
            if a["error_type"]:
                error_counts[a["error_type"]] = error_counts.get(a["error_type"], 0) + 1

        return {
            "total_attempts": total,
            "successes": successes,
            "failures": failures,
            "success_rate": (successes / total * 100) if total > 0 else 0,
            "strategy_scores": dict(self._strategy_scores),
            "error_breakdown": error_counts,
        }

    def should_change_proxy(self, error_type):
        return error_type in (
            CreationError.IP_FLAGGED,
            CreationError.QR_BLOCKED,
        )


retry_engine = RetryEngine()
