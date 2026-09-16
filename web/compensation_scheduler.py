"""Default-disabled periodic SMS compensation worker.

The Web launcher owns this process. Importing this module or constructing the
Flask application never starts background work.
"""
import argparse
import json
import os
import signal
import stat
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

from web.configuration import Configuration, parse_compensation_scheduler_settings


ROOT = Path(__file__).resolve().parents[1]
STATUS_FILENAME = "compensation-scheduler-status.json"
LOCK_FILENAME = "compensation-scheduler.lock"
COUNTER_KEYS = ("claimed", "cancelled", "completed", "failed")
TIMESTAMP_KEYS = (
    "started_at", "updated_at", "last_pass_started_at", "last_pass_finished_at",
)
SAFE_STATES = frozenset({
    "disabled", "not_started", "starting", "running", "idle", "error",
    "stopping", "stopped", "stale", "unavailable",
})
SAFE_ERROR_CODES = frozenset({
    "", "cancelled", "compensation_claim_lost", "compensation_claimed",
    "compensation_failed", "invalid_reconciliation_result", "provider_error",
    "provider_rejected", "provider_timeout", "reconciliation_failed",
    "scheduler_stale", "status_unavailable", "worker_exception",
})


class SchedulerAlreadyRunning(RuntimeError):
    """Raised when another scheduler owns the runtime lock."""


def _runtime_directory(root):
    return Path(root).resolve() / "data" / "web"


def _status_path(root):
    return _runtime_directory(root) / STATUS_FILENAME


def _open_flags(flags):
    if os.name != "nt":
        flags |= getattr(os, "O_NOFOLLOW", 0)
    return flags | getattr(os, "O_BINARY", 0)


def _validate_open_regular_file(descriptor, path, *, maximum_size=None):
    opened = os.fstat(descriptor)
    if not stat.S_ISREG(opened.st_mode):
        raise ValueError("scheduler file must be regular")
    if maximum_size is not None and opened.st_size > maximum_size:
        raise ValueError("scheduler file is too large")

    # Windows has no os.O_NOFOLLOW. Comparing the handle identity with lstat is
    # the strongest portable stdlib check available there; POSIX keeps the same
    # identity check in addition to refusing symlinks during open.
    current = os.lstat(path)
    if (
        stat.S_ISLNK(current.st_mode)
        or not stat.S_ISREG(current.st_mode)
        or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
    ):
        raise ValueError("scheduler file path changed")
    return opened


def _timestamp(value=None):
    value = value or datetime.now(timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_timestamp(value):
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > 64:
        raise ValueError("invalid scheduler timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("scheduler timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def _empty_status(enabled, state=None):
    status = {
        "enabled": bool(enabled),
        "state": state or ("not_started" if enabled else "disabled"),
        "error_code": "",
    }
    status.update({key: 0 for key in COUNTER_KEYS})
    status.update({key: None for key in TIMESTAMP_KEYS})
    return status


def _project_status(value, *, enabled=None, update_timestamp=False):
    if not isinstance(value, dict):
        raise ValueError("scheduler status must be an object")
    enabled_value = value.get("enabled") if enabled is None else enabled
    if type(enabled_value) is not bool:
        raise ValueError("scheduler enabled state must be boolean")
    projected = _empty_status(enabled_value, value.get("state"))
    if projected["state"] not in SAFE_STATES:
        raise ValueError("invalid scheduler state")
    for key in COUNTER_KEYS:
        candidate = value.get(key, 0)
        if type(candidate) is not int or not 0 <= candidate <= 1000000:
            raise ValueError("invalid scheduler counter")
        projected[key] = candidate
    error_code = value.get("error_code", "")
    if not isinstance(error_code, str) or error_code not in SAFE_ERROR_CODES:
        raise ValueError("invalid scheduler error code")
    projected["error_code"] = error_code
    for key in TIMESTAMP_KEYS:
        candidate = value.get(key)
        _parse_timestamp(candidate)
        projected[key] = candidate
    if update_timestamp:
        projected["updated_at"] = _timestamp()
    return projected


class SchedulerLock:
    def __init__(self, stream):
        self.stream = stream

    def close(self):
        stream = self.stream
        if stream is None:
            return
        self.stream = None
        try:
            if os.name == "nt":  # pragma: no cover - Windows CI unavailable.
                import msvcrt
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()

    def __enter__(self):
        return self

    def __exit__(self, _type, _value, _traceback):
        self.close()


def acquire_scheduler_lock(root):
    directory = _runtime_directory(root)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if directory.is_symlink():
        raise ValueError("scheduler runtime directory cannot be a symlink")
    path = directory / LOCK_FILENAME
    if path.is_symlink():
        raise ValueError("scheduler lock cannot be a symlink")
    descriptor = os.open(str(path), _open_flags(os.O_RDWR | os.O_CREAT), 0o600)
    try:
        _validate_open_regular_file(descriptor, path)
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)
        stream = os.fdopen(descriptor, "r+b")
        descriptor = None
    finally:
        if descriptor is not None:
            os.close(descriptor)
    try:
        if os.name == "nt":  # pragma: no cover - Windows CI unavailable.
            import msvcrt
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        stream.close()
        raise SchedulerAlreadyRunning("compensation scheduler already running") from None
    try:
        _validate_open_regular_file(stream.fileno(), path)
    except (OSError, ValueError):
        stream.close()
        raise ValueError("scheduler lock path changed") from None
    return SchedulerLock(stream)


def write_scheduler_status(root, status):
    directory = _runtime_directory(root)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if directory.is_symlink():
        raise ValueError("scheduler runtime directory cannot be a symlink")
    projected = _project_status(status, update_timestamp=True)
    destination = directory / STATUS_FILENAME
    descriptor, temporary = tempfile.mkstemp(dir=directory, prefix=".compensation-status-")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(projected, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
        os.chmod(destination, 0o600)
    finally:
        if os.path.lexists(temporary):
            os.unlink(temporary)
    return projected


def _unavailable_status(enabled):
    status = _empty_status(enabled, "unavailable")
    status["error_code"] = "status_unavailable"
    return status


def read_scheduler_status(root, *, enabled=False, stale_after_seconds=None, now=None):
    if not enabled:
        return _empty_status(False)
    path = _status_path(root)
    descriptor = None
    try:
        descriptor = os.open(
            str(path),
            _open_flags(os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)),
        )
        _validate_open_regular_file(descriptor, path, maximum_size=65536)
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            payload = stream.read(65537)
        if len(payload) > 65536:
            raise ValueError("scheduler status file is too large")
        raw = json.loads(payload.decode("utf-8"))
        status = _project_status(raw, enabled=bool(enabled))
        if stale_after_seconds is not None and status["state"] in {
            "starting", "running", "idle", "error", "stopping",
        }:
            if type(stale_after_seconds) is not int or stale_after_seconds <= 0:
                raise ValueError("invalid scheduler stale interval")
            updated = _parse_timestamp(status["updated_at"])
            current = now or datetime.now(timezone.utc)
            if updated is None or (current.astimezone(timezone.utc) - updated).total_seconds() > stale_after_seconds:
                status["state"] = "stale"
                status["error_code"] = "scheduler_stale"
        return status
    except FileNotFoundError:
        return _empty_status(enabled)
    except (OSError, ValueError, TypeError, json.JSONDecodeError, OverflowError):
        return _unavailable_status(enabled)
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _normalize_settings(settings):
    if not isinstance(settings, dict):
        raise ValueError("scheduler settings must be an object")
    if "COMPENSATION_SCHEDULER_ENABLED" in settings:
        return parse_compensation_scheduler_settings(settings)
    required = {
        "enabled", "interval_seconds", "limit", "max_attempts",
        "backoff_seconds", "time_budget_seconds",
    }
    if set(settings) != required:
        raise ValueError("invalid scheduler settings")
    if type(settings["enabled"]) is not bool:
        raise ValueError("scheduler enabled setting must be boolean")
    environment = {
        "COMPENSATION_SCHEDULER_ENABLED": "true" if settings["enabled"] else "false",
        "COMPENSATION_SCHEDULER_INTERVAL_SECONDS": str(settings["interval_seconds"]),
        "COMPENSATION_SCHEDULER_LIMIT": str(settings["limit"]),
        "COMPENSATION_SCHEDULER_MAX_ATTEMPTS": str(settings["max_attempts"]),
        "COMPENSATION_SCHEDULER_BACKOFF_SECONDS": str(settings["backoff_seconds"]),
        "COMPENSATION_SCHEDULER_TIME_BUDGET_SECONDS": str(settings["time_budget_seconds"]),
    }
    return parse_compensation_scheduler_settings(environment)


def _pass_projection(result):
    if not isinstance(result, dict):
        return {
            "claimed": 0, "cancelled": 0, "completed": 0, "failed": 1,
            "error_code": "invalid_reconciliation_result",
        }
    projected = {}
    for key in COUNTER_KEYS:
        value = result.get(key)
        if type(value) is not int or not 0 <= value <= 1000000:
            return {
                "claimed": 0, "cancelled": 0, "completed": 0, "failed": 1,
                "error_code": "invalid_reconciliation_result",
            }
        projected[key] = value
    error_code = result.get("error_code", "")
    projected["error_code"] = (
        error_code if isinstance(error_code, str) and error_code in SAFE_ERROR_CODES
        else "reconciliation_failed"
    )
    return projected


def run_scheduler(root, settings, *, cancel_event=None, run_job=None,
                  ledger_factory=None, once=False):
    settings = _normalize_settings(settings)
    if not settings["enabled"]:
        return _empty_status(False)
    cancel_event = cancel_event or threading.Event()
    if run_job is None:
        from web.worker import run_sms_compensation_job
        run_job = run_sms_compensation_job
    if ledger_factory is None:
        from core.job_ledger import JobLedger
        ledger_factory = JobLedger

    lock = acquire_scheduler_lock(root)
    status = _empty_status(True, "starting")
    status["started_at"] = _timestamp()
    status_initialized = False
    try:
        write_scheduler_status(root, status)
        status_initialized = True
        ledger = ledger_factory(str(Path(root).resolve() / "data" / "database.db"))
        while not cancel_event.is_set():
            status["state"] = "running"
            status["last_pass_started_at"] = _timestamp()
            write_scheduler_status(root, status)
            try:
                result = run_job(
                    ledger=ledger,
                    limit=settings["limit"],
                    max_attempts=settings["max_attempts"],
                    backoff_seconds=settings["backoff_seconds"],
                    source="periodic-scheduler",
                    time_budget_seconds=settings["time_budget_seconds"],
                    cancel_event=cancel_event,
                )
                projection = _pass_projection(result)
            except Exception:
                projection = {
                    "claimed": 0, "cancelled": 0, "completed": 0, "failed": 1,
                    "error_code": "worker_exception",
                }
            status.update(projection)
            status["last_pass_finished_at"] = _timestamp()
            status["state"] = "error" if status["error_code"] else "idle"
            write_scheduler_status(root, status)
            if once or cancel_event.wait(settings["interval_seconds"]):
                break
    except KeyboardInterrupt:
        cancel_event.set()
    except Exception:
        status["failed"] = max(1, status["failed"])
        status["error_code"] = "worker_exception"
        raise
    finally:
        try:
            if status_initialized:
                status["state"] = "stopped"
                if cancel_event.is_set() and not status["error_code"]:
                    status["error_code"] = "cancelled"
                write_scheduler_status(root, status)
        finally:
            lock.close()
    return _project_status(status, enabled=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Periodic SMS compensation worker")
    parser.add_argument("--root", required=True, help="Selected runtime environment root")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    configuration = Configuration(root, environment=dict(os.environ), code_root=ROOT)
    try:
        settings = configuration.compensation_scheduler_settings()
    except ValueError:
        return 2
    if not settings["enabled"]:
        return 0
    stop = threading.Event()

    def request_stop(_signum=None, _frame=None):
        stop.set()

    old_term = signal.signal(signal.SIGTERM, request_stop)
    try:
        try:
            run_scheduler(root, settings, cancel_event=stop)
        except SchedulerAlreadyRunning:
            return 3
        except (OSError, ValueError):
            return 1
        return 0
    finally:
        signal.signal(signal.SIGTERM, old_term)


if __name__ == "__main__":
    raise SystemExit(main())
