"""Persistent task state and isolated subprocess supervision."""
import json
import io
import logging
import os
import signal
import select
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
import errno
from datetime import datetime, timezone
from pathlib import Path

from core.secret_safety import (
    is_sensitive_key,
    redact_text,
    safe_exception_message,
    sanitize_operation_value,
)


ACTIVE = ("running", "stopping")
ACTIONS = {"create", "health", "warm", "proxy_test", "proxy_fetch", "telegram_test",
           "sms_balance", "validate", "migrate", "resume", "voice"}


def _voice_token_is_configured(value):
    """Return whether the standalone voice service has a real token."""
    return (
        isinstance(value, str)
        and bool(value.strip())
        and value.strip().lower() != "changeme"
    )


def now():
    return datetime.now(timezone.utc).isoformat()


def integer(params, name, default, low, high):
    value = params.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"{name} must be an integer between {low} and {high}")
    return value


def normalize_params(action, params, default_engine):
    if not isinstance(action, str) or action not in ACTIONS:
        raise ValueError("Unknown task action")
    if not isinstance(params, dict):
        raise ValueError("params must be an object")
    result = {}
    if action == "create":
        result = {
            "engine": params.get("engine", default_engine),
            "num_accounts": integer(params, "num_accounts", 1, 1, 100),
            "warmup_minutes": integer(params, "warmup_minutes", 5, 0, 60),
            "flow_mode": params.get("flow_mode", "standard"),
            "use_sms_api": params.get("use_sms_api", False),
            "parallel": params.get("parallel", False),
            "max_threads": integer(params, "max_threads", 3, 1, 5),
        }
        if result["engine"] not in ("playwright", "selenium", "appium"):
            raise ValueError("Invalid engine")
        if result["flow_mode"] not in ("standard", "youtube", "workspace"):
            raise ValueError("Invalid flow mode")
        if any(not isinstance(result[key], bool) for key in ("parallel", "use_sms_api")):
            raise ValueError("parallel and use_sms_api must be booleans")
        if result["engine"] == "appium" and result["parallel"]:
            raise ValueError("Appium does not support parallel tasks on one device")
    if action in ("health", "warm"):
        ids = params.get("account_ids", [])
        if not isinstance(ids, list) or len(ids) > 10000 or any(type(i) is not int or i < 1 for i in ids):
            raise ValueError("account_ids must contain positive integers")
        result["account_ids"] = list(dict.fromkeys(ids))
    if action == "warm":
        # The profile manifest is authoritative for warm operations.  Keep an
        # explicitly supplied engine only as a compatibility assertion; an
        # omitted value must not silently turn every account into Playwright.
        if "engine" in params:
            result["engine"] = params["engine"]
            if result["engine"] not in ("playwright", "selenium"):
                raise ValueError("Warming requires Playwright or Selenium")
        result["duration_minutes"] = integer(params, "duration_minutes", 3, 1, 60)
    unknown = set(params) - set(result)
    if unknown:
        raise ValueError("Unknown task parameters: " + ", ".join(sorted(unknown)))
    return result


class TaskStore:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.directory / "tasks.db"
        with self.connect() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY, action TEXT NOT NULL, params TEXT NOT NULL,
                status TEXT NOT NULL, created_at TEXT NOT NULL, finished_at TEXT,
                error TEXT, result TEXT, progress TEXT, pid INTEGER)""")

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        return conn

    def _known_secret_values(self):
        """Collect local credential values used to scrub legacy task output."""
        values = [value for key, value in os.environ.items()
                  if is_sensitive_key(key) and value]
        # Web tasks live under <root>/data/web.  Account credentials are
        # legitimate internal state, but any copy found in an old task record
        # or log must not cross the durable/public boundary.
        database_path = self.directory.parent / "database.db"
        try:
            with sqlite3.connect(database_path, timeout=1) as conn:
                values.extend(value for row in conn.execute(
                    "SELECT password, proxy FROM accounts"
                ) for value in row if value)
        except (OSError, sqlite3.Error):
            pass
        root = self.directory.parent.parent
        env_path = root / ".env"
        local_values = {}
        try:
            from dotenv import dotenv_values
            local_values = dotenv_values(env_path, interpolate=False)
            values.extend(value for key, value in local_values.items()
                          if value and is_sensitive_key(key))
        except (ImportError, OSError, TypeError, ValueError):
            pass

        # PROXY_FILE is a configurable resource; reading only the default file
        # leaves credentials in a custom environment-specific resource visible.
        proxy_file = os.environ.get("PROXY_FILE") or local_values.get("PROXY_FILE") or "config/proxies.txt"
        proxy_path = (root / str(proxy_file)).resolve()
        credential_paths = [root / "config/password.txt", root / "config/5sim_config.txt"]
        if root in proxy_path.parents and proxy_path.suffix.lower() == ".txt":
            credential_paths.append(proxy_path)
        for credential_path in credential_paths:
            try:
                if credential_path.is_file():
                    values.extend(line.strip() for line in credential_path.read_text(encoding="utf-8").splitlines()
                                  if line.strip() and not line.lstrip().startswith("#"))
            except (OSError, UnicodeError):
                pass
        return tuple(values)

    def decode(self, row):
        if row is None:
            return None
        result = dict(row)
        secrets = self._known_secret_values()
        for key in ("params", "result", "progress"):
            result[key] = json.loads(result[key]) if result[key] else None
        result["params"] = sanitize_operation_value(result["params"], secrets)
        result["result"] = sanitize_operation_value(result["result"], secrets)
        result["progress"] = sanitize_operation_value(result["progress"], secrets)
        result["error"] = redact_text(result["error"], secrets)
        return result

    def get(self, task_id):
        with self.connect() as conn:
            return self.decode(conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())

    def list(self):
        with self.connect() as conn:
            return [self.decode(row) for row in conn.execute(
                """SELECT * FROM tasks WHERE status IN ('running', 'stopping')
                   OR id IN (SELECT id FROM tasks ORDER BY created_at DESC LIMIT 100)
                   ORDER BY created_at DESC""")]

    def active(self):
        with self.connect() as conn:
            return [self.decode(row) for row in conn.execute(
                "SELECT * FROM tasks WHERE status IN ('running', 'stopping')")]

    def create(self, action, params):
        task_id = uuid.uuid4().hex
        params = sanitize_operation_value(params, self._known_secret_values())
        with self.connect() as conn:
            conn.execute("INSERT INTO tasks(id,action,params,status,created_at,progress) VALUES(?,?,?,?,?,?)",
                         (task_id, action, json.dumps(params), "running", now(),
                          json.dumps({"completed": 0, "total": 1, "message": "Starting"})))
        return self.get(task_id)

    def update(self, task_id, **values):
        allowed = {"status", "finished_at", "error", "result", "progress", "pid"}
        if not values or not set(values) <= allowed:
            raise ValueError("Invalid task update")
        secrets = self._known_secret_values()
        if "error" in values:
            values["error"] = redact_text(values["error"], secrets)
        for key in ("result", "progress"):
            if key in values:
                values[key] = json.dumps(sanitize_operation_value(values[key], secrets))
        with self.connect() as conn:
            conn.execute("UPDATE tasks SET " + ",".join(key + "=?" for key in values) + " WHERE id=?",
                         (*values.values(), task_id))

    def update_if_status(self, task_id, expected_statuses, **values):
        """Apply a task update only while its status is still expected.

        Worker cleanup and the normal worker completion path run in separate
        processes.  A read followed by an unconditional ``UPDATE`` lets a
        late cleanup callback overwrite a terminal result.  Keep the compare
        and write in one SQLite transaction so terminal ownership is explicit.
        """
        allowed = {"status", "finished_at", "error", "result", "progress", "pid"}
        if not values or not set(values) <= allowed:
            raise ValueError("Invalid task update")
        if isinstance(expected_statuses, str):
            expected_statuses = (expected_statuses,)
        if not isinstance(expected_statuses, (tuple, list, set, frozenset)):
            raise ValueError("expected_statuses must be a collection")
        expected = tuple(
            item for item in expected_statuses
            if isinstance(item, str) and item
        )
        if not expected:
            raise ValueError("expected_statuses must not be empty")
        secrets = self._known_secret_values()
        if "error" in values:
            values["error"] = redact_text(values["error"], secrets)
        for key in ("result", "progress"):
            if key in values:
                values[key] = json.dumps(sanitize_operation_value(values[key], secrets))
        placeholders = ",".join("?" for _ in expected)
        with self.connect() as conn:
            cursor = conn.execute(
                "UPDATE tasks SET " + ",".join(key + "=?" for key in values) +
                " WHERE id=? AND status IN (" + placeholders + ")",
                (*values.values(), task_id, *expected),
            )
            return cursor.rowcount > 0

    def logs(self, task_id):
        if self.get(task_id) is None:
            raise KeyError(task_id)
        path = self.directory / (task_id + ".log")
        if not path.exists():
            return ""
        # Redact before truncating.  If a secret begins just before the old
        # tail window, truncating first can leave an unmatched suffix behind.
        with path.open("rb") as stream:
            content = stream.read().decode("utf-8", errors="replace")
        safe = redact_text(content, self._known_secret_values())
        return safe[-128 * 1024:]


class TaskManager:
    def __init__(self, root, configuration, directory=None):
        self.root = Path(root)
        self.configuration = configuration
        self.store = TaskStore(directory or self.root / "data/web")
        self.lock = threading.RLock()
        self.processes = {}
        self.stop_threads = {}
        self.watch_threads = {}
        self.recovery_threads = {}
        self.log_threads = {}
        self._closed = threading.Event()
        # Server restart is the second durable recovery boundary.  Workers
        # heartbeat their own attempts, while this pass closes rows abandoned
        # by a process that died before its task watcher could persist the
        # terminal state.  Keep it best-effort so a damaged ledger cannot stop
        # the authenticated console from coming up.
        try:
            ledger_path = self.root / "data" / "database.db"
            if ledger_path.exists():
                from core.job_ledger import JobLedger
                from web.worker import recover_stale_attempts
                recover_stale_attempts(ledger=JobLedger(str(ledger_path)))
        except (ImportError, OSError, sqlite3.Error, ValueError, TypeError) as exc:
            logging.getLogger("gmail_creator_web").warning(
                "Ledger stale recovery unavailable: %s", type(exc).__name__
            )
        # Workers detect a lost parent and cancel themselves. Do not allow a new
        # job to overlap one that is still shutting down after a server crash.
        for task in self.store.active():
            if not self._pid_alive(task["pid"]):
                self._reconcile_recovered_task(task["id"], task["pid"])
            else:
                thread = threading.Thread(
                    target=self._watch_recovered,
                    args=(task["id"], task["pid"]),
                    name="task-recovery-%s" % task["id"], daemon=True,
                )
                self.recovery_threads[task["id"]] = thread
                thread.start()

    def _watch_recovered(self, task_id, pid):
        try:
            while not self._closed.is_set():
                try:
                    task = self.store.get(task_id)
                except (OSError, sqlite3.Error, ValueError):
                    # A recovered watcher is best-effort.  The database may be
                    # removed during environment teardown; do not leave a
                    # daemon thread printing an unhandled traceback.
                    return
                if not task or task["status"] not in ACTIVE:
                    return
                if not self._pid_alive(pid):
                    self._reconcile_recovered_task(task_id, pid)
                    return
                self._closed.wait(0.25)
        finally:
            with self.lock:
                self.recovery_threads.pop(task_id, None)

    def _reconcile_recovered_task(self, task_id, pid):
        """Reconcile a worker that disappeared without a final report.

        A dead worker PID is not enough evidence: browser descendants can keep
        the worker's process group (and a profile lease) alive.  Verify the
        group, escalate once when needed, and only then write a terminal task
        state.  ``stopping`` means an explicit cancellation was requested;
        other active states remain resumable as ``interrupted``.
        """
        try:
            current = self.store.get(task_id)
            if not current or current.get("status") not in ACTIVE:
                return False
            verified = self._process_group_gone(pid) if os.name != "nt" else not self._pid_alive(pid)
            process = type("RecoveredProcess", (), {"pid": pid})()
            if not verified:
                self._signal(process, signal.SIGTERM)
                verified = self._wait_for_process_group_gone(pid, timeout=10)
            if not verified:
                self._signal(process, signal.SIGKILL)
                verified = self._wait_for_process_group_gone(pid, timeout=3)

            current = self.store.get(task_id)
            if not current or current.get("status") not in ACTIVE:
                return verified is True
            if verified is True:
                expected_status = current.get("status")
                state = "cancelled" if expected_status == "stopping" else "interrupted"
                error_code = "cancelled" if state == "cancelled" else "worker_lost"
                message = (
                    "Recovered cancellation completed after worker exit"
                    if state == "cancelled"
                    else "Recovered worker exited without a final result"
                )
                changed = self.store.update_if_status(
                    task_id, (expected_status,),
                    status=state, finished_at=now(), error=message,
                )
                if changed:
                    self._finalize_ledger(task_id, state, error_code, cleanup_verified=True)
            else:
                changed = self.store.update_if_status(
                    task_id,
                    (current.get("status"),),
                    status="cleanup_failed",
                    finished_at=now(),
                    error="cleanup_failed: recovered worker process group could not be confirmed stopped",
                )
                if changed:
                    self._finalize_ledger(task_id, "failed", "cleanup_failed", cleanup_verified=False)
            return verified is True
        except BaseException as exc:
            # Recovery runs in a daemon thread during server startup/shutdown;
            # an unexpected observation error must still leave a durable,
            # conservative state whenever the row is available.
            logging.debug("Recovered task reconciliation failed: %s", type(exc).__name__)
            try:
                current = self.store.get(task_id)
                if current and current.get("status") in ACTIVE:
                    changed = self.store.update_if_status(
                        task_id,
                        (current.get("status"),),
                        status="cleanup_failed",
                        finished_at=now(),
                        error="cleanup_failed: recovered worker cleanup could not be verified",
                    )
                    if changed:
                        self._finalize_ledger(task_id, "failed", "cleanup_failed", cleanup_verified=False)
            except BaseException:
                pass
            return False

    def run_sms_compensation_once(self, *, limit=100, max_attempts=3,
                                  backoff_seconds=30):
        """Run one internal SMS-order reconciliation pass.

        Deployments with a periodic scheduler can call this method directly;
        it intentionally has no HTTP counterpart.  The store is opened from
        the web runtime root so it is the same durable boundary used by
        supervised workers and profile/account records.
        """
        from core.job_ledger import JobLedger
        from web.worker import run_sms_compensation_job

        ledger = JobLedger(str(self.root / "data" / "database.db"))
        return run_sms_compensation_job(
            ledger=ledger,
            limit=limit,
            max_attempts=max_attempts,
            backoff_seconds=backoff_seconds,
            source="internal-scheduler",
        )

    @staticmethod
    def _pid_alive(pid):
        if not pid:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def _finalize_ledger(self, job_id, state, error_code="", *, cleanup_verified):
        """Mirror process lifecycle outcomes into the durable job ledger."""
        if not job_id:
            return None
        if type(cleanup_verified) is not bool:
            raise ValueError("cleanup_verified must be a boolean")
        ledger_path = self.root / "data" / "database.db"
        if not ledger_path.exists():
            return None
        try:
            from core.job_ledger import JobLedger
            ledger = JobLedger(str(ledger_path))
            return ledger.supervisor_finalize_job(
                str(job_id), state=state, error_code=error_code,
                cleanup_verified=cleanup_verified,
            )
        except (KeyError, ValueError, OSError, sqlite3.Error) as exc:
            # Actions without a ledger job (for example validation) are still
            # valid task operations; bookkeeping must not create a second
            # failure at the process boundary.
            logging.debug("Ledger finalization skipped: %s", type(exc).__name__)
            return None

    def start(self, action, params):
        params = normalize_params(action, params, self.configuration.values()["ENGINE_MODE"])
        with self.lock:
            for task in self.store.active():
                if (task["action"] == "voice") == (action == "voice"):
                    raise ValueError("A task is already running; stop it or wait for completion")
            task = self.store.create(action, params)
            env = dict(self.configuration.environment)
            env.update(self.configuration.values())
            env.update({"PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1",
                        "WEB_TASK_DIRECTORY": str(self.store.directory.resolve()),
                        "WEB_PARENT_PID": str(os.getpid()),
                        "GMAIL_CONFIG_FROM_ENV": "1",
                        "LEDGER_DB_PATH": str((self.root / "data" / "database.db").resolve()),
                        # Make profile resolution independent of the worker's
                        # current-directory convention.
                        "PROFILE_RUNTIME_ROOT": str(self.root.resolve())})
            code_root = str(Path(__file__).resolve().parents[1])
            env["PYTHONPATH"] = os.pathsep.join(filter(None, (code_root, env.get("PYTHONPATH", ""))))
            if params.get("engine"):
                env["ENGINE_MODE"] = params["engine"]
            if action == "voice":
                if not _voice_token_is_configured(env.get("VOICE_SERVER_TOKEN")):
                    self.store.update(task["id"], status="failed", finished_at=now(),
                                      error="Configure VOICE_SERVER_TOKEN before starting voice service")
                    raise ValueError("Configure VOICE_SERVER_TOKEN before starting voice service")
                env["VOICE_SERVER_HOST"] = "127.0.0.1"
            # Web authentication secrets are not needed by automation workers.
            for key in ("WEB_ADMIN_PASSWORD", "WEB_SECRET_KEY"):
                env.pop(key, None)
            try:
                proc = subprocess.Popen(
                    [sys.executable, "-m", "web.worker", task["id"]],
                    cwd=self.root, env=env, stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    start_new_session=(os.name != "nt"),
                )
            except OSError as exc:
                self.store.update(
                    task["id"], status="failed", finished_at=now(),
                    error=safe_exception_message(exc, self.store._known_secret_values(),
                                                fallback="Unable to start worker"),
                )
                raise
            self.processes[task["id"]] = proc
            self._start_log_capture(task["id"], proc)
            self.store.update(task["id"], pid=proc.pid)
            watcher = threading.Thread(
                target=self._watch, args=(task["id"], proc),
                name="task-watch-%s" % task["id"], daemon=True,
            )
            self.watch_threads[task["id"]] = watcher
            watcher.start()
            return self.store.get(task["id"])

    def _start_log_capture(self, task_id, proc):
        """Persist only sanitizer-processed worker output.

        The worker and its descendants inherit the pipe, so writes through
        Python streams, ``os.write`` or a browser child process all cross the
        same redaction boundary before reaching the durable log file.
        """
        stream = getattr(proc, "stdout", None)
        if not isinstance(stream, io.IOBase):
            # Unit-test fakes and embedders may return a process-like object
            # without a real pipe.  They can still use TaskStore.logs(), which
            # protects manually-created legacy files on read.
            return

        path = self.store.directory / (task_id + ".log")
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            path.touch(mode=0o600, exist_ok=True)
            os.chmod(path, 0o600)
        except OSError:
            pass

        def capture():
            pending = ""
            try:
                with path.open("ab") as output:
                    while True:
                        if os.name != "nt":
                            ready, _, _ = select.select([stream], [], [], 0.5)
                            if not ready:
                                if getattr(stream, "closed", False):
                                    break
                                continue
                            chunk = os.read(stream.fileno(), 8192)
                        else:  # pragma: no cover - Windows CI is unavailable.
                            chunk = getattr(stream, "read1", stream.read)(8192)
                        if not chunk:
                            break
                        if isinstance(chunk, bytes):
                            chunk = chunk.decode("utf-8", errors="replace")
                        pending += str(chunk)
                        secrets = self.store._known_secret_values()
                        while "\n" in pending:
                            line, pending = pending.split("\n", 1)
                            output.write(redact_text(line + "\n", secrets).encode("utf-8"))
                            output.flush()
                        # Never stream a partial credential to disk.  A worker
                        # that emits an unbounded line is replaced wholesale
                        # instead of growing the parent indefinitely.
                        if len(pending) > 1024 * 1024:
                            output.write(b"[redacted oversized task-log line]\n")
                            output.flush()
                            pending = ""
                    if pending:
                        output.write(redact_text(pending, self.store._known_secret_values()).encode("utf-8"))
                        output.flush()
            except (OSError, ValueError) as exc:
                logging.warning("Task log capture failed: %s", type(exc).__name__)
            finally:
                try:
                    stream.close()
                except Exception:
                    pass

        thread = threading.Thread(target=capture, name="task-log-%s" % task_id, daemon=True)
        self.log_threads[task_id] = thread
        thread.start()

    def _finish_log_capture(self, task_id, timeout=5):
        thread = self.log_threads.pop(task_id, None)
        if thread is not None:
            thread.join(timeout=max(0, timeout))

    def _watch(self, task_id, proc):
        wait_failed = False
        try:
            returncode = proc.wait()
        except BaseException:
            # A fake or externally reaped process should not take down the
            # supervisor thread with an unhandled traceback.  The process
            # boundary is now uncertain, so cleanup must fail closed below.
            returncode = None
            wait_failed = True
        try:
            self._finish_log_capture(task_id)
            with self.lock:
                task = self.store.get(task_id)
                if not task:
                    return
                if task["status"] == "stopping":
                    return  # The cancellation supervisor owns process-group cleanup.
                # The worker can publish ``completed`` just before its own
                # process exits.  A live browser descendant at that point
                # invalidates the result just as it invalidates a running
                # task, so terminal rows must still pass the process-group
                # reconciliation boundary before they are trusted.
                terminal_states = (
                    "completed", "failed", "cancelled", "interrupted", "cleanup_failed",
                )
                if task["status"] in ACTIVE or task["status"] in terminal_states:
                    cleanup_verified = not wait_failed
                    if cleanup_verified:
                        if os.name == "nt":
                            poll = getattr(proc, "poll", None)
                            cleanup_verified = callable(poll) and poll() is not None
                        else:
                            cleanup_verified = self._process_group_gone(proc.pid)
                            if not cleanup_verified:
                                self._signal(proc, signal.SIGTERM)
                                cleanup_verified = self._wait_for_process_group_gone(
                                    proc.pid, timeout=2
                                )
                            if not cleanup_verified:
                                self._signal(proc, signal.SIGKILL)
                                cleanup_verified = self._wait_for_process_group_gone(
                                    proc.pid, timeout=2
                                )
                    if not cleanup_verified:
                        expected = task["status"]
                        if expected != "cleanup_failed":
                            changed = self.store.update_if_status(
                                task_id, (expected,), status="cleanup_failed",
                                finished_at=now(),
                                error="cleanup_failed: worker process exit could not be confirmed",
                            )
                            if changed:
                                self._finalize_ledger(task_id, "failed", "cleanup_failed", cleanup_verified=False)
                    elif task["status"] in ACTIVE:
                        changed = self.store.update_if_status(
                            task_id, ("running",), status="failed", finished_at=now(),
                            error="Worker exited without a result (code %s)" % returncode,
                        )
                        if changed:
                            self._finalize_ledger(task_id, "failed", "worker_exit", cleanup_verified=True)
        except (OSError, sqlite3.Error, ValueError, TypeError):
            # Teardown can remove the task database before this daemon watcher
            # wakes up.  Durable startup recovery remains responsible for any
            # row that still exists; there is no useful traceback to expose.
            return
        finally:
            with self.lock:
                self.processes.pop(task_id, None)
                self.watch_threads.pop(task_id, None)

    def cancel(self, task_id):
        with self.lock:
            task = self.store.get(task_id)
            if task is None:
                raise KeyError(task_id)
            proc = self.processes.get(task_id)
            if task["status"] == "stopping":
                # Cancellation is idempotent once the durable state has
                # entered stopping.  The process entry may already have been
                # removed by the supervisor while its final observation is
                # still being persisted.
                return task
            if task["status"] != "running" or proc is None:
                raise ValueError("Task is not running in this server")
            changed = self.store.update_if_status(
                task_id, ("running",), status="stopping"
            )
            if not changed:
                # The row changed after the read (normally the worker watcher
                # published a terminal result).  Do not signal a potentially
                # reused PID; return the durable winner to the caller.
                latest = self.store.get(task_id)
                if latest is None:
                    raise KeyError(task_id)
                return latest
            self._signal(proc, signal.SIGTERM)
            thread = threading.Thread(target=self._stop_task, args=(task_id, proc), daemon=True)
            self.stop_threads[task_id] = thread
            thread.start()
            return self.store.get(task_id)

    def _stop_task(self, task_id, proc):
        cleanup_verified = False
        try:
            # Only the literal boolean returned by the process-group
            # supervisor is a cleanup fact.  Do not turn an adapter/mock value
            # such as ``"false"`` into a successful cancellation.
            cleanup_verified = self._kill_later(proc) is True
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                cleanup_verified = False
                if not self._signal(proc, signal.SIGKILL):
                    cleanup_verified = False
                else:
                    try:
                        proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        cleanup_verified = False
            if os.name == "nt":
                poll = getattr(proc, "poll", None)
                # Windows has no process-group probe equivalent here; require
                # the process handle itself to report an exit after waiting.
                cleanup_verified = (
                    cleanup_verified and callable(poll) and poll() is not None
                )
            if os.name != "nt":
                cleanup_verified = cleanup_verified and self._wait_for_process_group_gone(
                    proc.pid, timeout=2
                )
        except BaseException as exc:
            # Failure to observe or signal a process is itself a cleanup
            # failure.  Never convert that uncertainty into a successful
            # cancellation.
            logging.debug("Task cleanup supervisor failed: %s", type(exc).__name__)
            cleanup_verified = False
        finally:
            self._finish_log_capture(task_id)
            with self.lock:
                try:
                    current = self.store.get(task_id)
                    if current and current["status"] in ACTIVE:
                        expected_status = current["status"]
                        if cleanup_verified:
                            changed = self.store.update_if_status(
                                task_id, (expected_status,),
                                status="cancelled", finished_at=now(),
                            )
                            if changed:
                                self._finalize_ledger(task_id, "cancelled", "cancelled", cleanup_verified=True)
                        else:
                            changed = self.store.update_if_status(
                                task_id, (expected_status,),
                                status="cleanup_failed", finished_at=now(),
                                error="cleanup_failed: task process group could not be confirmed stopped",
                            )
                            if changed:
                                self._finalize_ledger(task_id, "failed", "cleanup_failed", cleanup_verified=False)
                except (OSError, sqlite3.Error, ValueError):
                    # The store may be unavailable during shutdown.  Keep the
                    # in-memory process maps consistent and let the next
                    # startup recovery pass reconcile any durable row.
                    pass
                finally:
                    self.processes.pop(task_id, None)
                    self.stop_threads.pop(task_id, None)

    @staticmethod
    def _signal(proc, sig):
        try:
            if os.name == "nt":
                if proc.poll() is None:
                    proc.terminate() if sig == signal.SIGTERM else proc.kill()
            else:
                os.killpg(proc.pid, sig)
            return True
        except ProcessLookupError:
            return True  # The worker exited between poll() and the signal.
        except (PermissionError, OSError):
            # Permission errors are common in constrained macOS runners.  The
            # caller must see False and record cleanup_failed, never assume the
            # process group was stopped.
            return False

    def _kill_later(self, proc):
        if os.name == "nt":
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                if not self._signal(proc, signal.SIGKILL):
                    return False
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    return False
            return proc.poll() is not None
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self._process_group_gone(proc.pid):
                return True
            time.sleep(0.1)
        if not self._signal(proc, signal.SIGKILL):
            return False
        return self._wait_for_process_group_gone(proc.pid, timeout=2)

    @staticmethod
    def _process_group_gone(pid):
        """Return true only when POSIX can prove no process remains in a group."""
        try:
            os.killpg(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            logging.warning("Task process group %s is no longer accessible for cleanup", pid)
            return False
        except OSError as exc:
            if getattr(exc, "errno", None) == errno.ESRCH:
                return True
            return False
        return False

    @classmethod
    def _wait_for_process_group_gone(cls, pid, timeout=2):
        deadline = time.monotonic() + max(0, timeout)
        while True:
            if cls._process_group_gone(pid):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)

    def close(self):
        self._closed.set()
        with self.lock:
            for task_id in list(self.processes):
                try:
                    task = self.store.get(task_id)
                except (OSError, sqlite3.Error, ValueError):
                    task = None
                if task and task["status"] in ACTIVE:
                    self.cancel(task_id)
            threads = list(self.stop_threads.values()) + list(
                self.recovery_threads.values()
            ) + list(self.watch_threads.values())
        for thread in threads:
            if thread is threading.current_thread():
                continue
            thread.join(timeout=15)
            if thread.is_alive():
                logging.error("Task cancellation did not complete before server shutdown")
