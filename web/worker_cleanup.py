"""Out-of-band cleanup for workers whose web-server parent disappeared.

The helper intentionally runs in a fresh session.  A worker that notices a
lost parent must terminate its own process group, which also terminates the
watchdog thread; only a process outside that group can wait for the group to
vanish and persist the final ``interrupted`` or ``cleanup_failed`` state.
"""

import argparse
import errno
import os
import signal
import sqlite3
import time
from pathlib import Path

from web.tasks import TaskStore, now


def _process_group_gone(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    except OSError as exc:
        return getattr(exc, "errno", None) == errno.ESRCH
    return False


def _signal_group(pid, sig):
    try:
        os.killpg(pid, sig)
        return True
    except ProcessLookupError:
        return True
    except (PermissionError, OSError):
        return False


def _wait_group_gone(pid, timeout):
    deadline = time.monotonic() + max(0.0, float(timeout))
    while True:
        if _process_group_gone(pid):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def _finalize_ledger(ledger_path, job_id, state, error_code, *, cleanup_verified):
    if not ledger_path or not job_id:
        return
    try:
        from core.job_ledger import JobLedger

        ledger = JobLedger(str(ledger_path))
        ledger.supervisor_finalize_job(
            str(job_id), state=state, error_code=error_code,
            cleanup_verified=cleanup_verified,
        )
    except (KeyError, ValueError, OSError, sqlite3.Error):
        # The task store remains authoritative when an older environment has
        # no ledger row (or is being torn down concurrently).
        return


def cleanup_lost_worker(task_id, pid, task_directory, ledger_path=None,
                        *, grace_seconds=10, kill_timeout=3):
    """Terminate a lost worker group and persist a verified terminal state.

    ``interrupted`` is written only after ``killpg(pid, 0)`` proves that no
    process remains.  Any inability to signal or observe the group is recorded
    as ``cleanup_failed`` so a caller cannot mistake uncertainty for a clean
    cancellation.
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        pid = 0

    verified = _process_group_gone(pid)
    if not verified:
        _signal_group(pid, signal.SIGTERM)
        verified = _wait_group_gone(pid, grace_seconds)
    if not verified:
        _signal_group(pid, signal.SIGKILL)
        verified = _wait_group_gone(pid, kill_timeout)

    try:
        store = TaskStore(Path(task_directory))
        current = store.get(task_id)
        # A live TaskManager may have completed/cancelled the same task while
        # this helper was starting.  Compare-and-set the active state so a late
        # helper cannot overwrite a terminal result.  ``running`` is also a
        # valid lost-parent state: the watchdog can disappear before its first
        # durable ``stopping`` update reaches SQLite.
        if current and current.get("status") in ("running", "stopping"):
            if verified:
                changed = store.update_if_status(
                    task_id,
                    (current.get("status"),),
                    status="interrupted",
                    finished_at=now(),
                    error="Worker parent exited before this task finished.",
                )
                if changed:
                    _finalize_ledger(
                        ledger_path, task_id, "interrupted", "worker_lost",
                        cleanup_verified=True,
                    )
            else:
                changed = store.update_if_status(
                    task_id,
                    (current.get("status"),),
                    status="cleanup_failed",
                    finished_at=now(),
                    error="cleanup_failed: worker process group could not be confirmed stopped",
                )
                if changed:
                    _finalize_ledger(
                        ledger_path, task_id, "failed", "cleanup_failed",
                        cleanup_verified=False,
                    )
    except (OSError, sqlite3.Error, ValueError, TypeError):
        return False
    return verified is True


def main():
    parser = argparse.ArgumentParser(description="Clean up a lost web worker")
    parser.add_argument("task_id")
    parser.add_argument("pid", type=int)
    parser.add_argument("task_directory")
    parser.add_argument("ledger_path", nargs="?", default="")
    args = parser.parse_args()
    cleanup_lost_worker(
        args.task_id,
        args.pid,
        args.task_directory,
        args.ledger_path,
    )


if __name__ == "__main__":
    main()
