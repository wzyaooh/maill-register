"""Persistent task state and isolated subprocess supervision."""
import json
import logging
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path


ACTIVE = ("running", "stopping")
ACTIONS = {"create", "health", "warm", "proxy_test", "proxy_fetch", "telegram_test",
           "sms_balance", "validate", "migrate", "resume", "voice"}


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
        result["engine"] = params.get("engine", "playwright")
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

    @staticmethod
    def decode(row):
        if row is None:
            return None
        result = dict(row)
        for key in ("params", "result", "progress"):
            result[key] = json.loads(result[key]) if result[key] else None
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
        with self.connect() as conn:
            conn.execute("INSERT INTO tasks(id,action,params,status,created_at,progress) VALUES(?,?,?,?,?,?)",
                         (task_id, action, json.dumps(params), "running", now(),
                          json.dumps({"completed": 0, "total": 1, "message": "Starting"})))
        return self.get(task_id)

    def update(self, task_id, **values):
        allowed = {"status", "finished_at", "error", "result", "progress", "pid"}
        if not values or not set(values) <= allowed:
            raise ValueError("Invalid task update")
        for key in ("result", "progress"):
            if key in values:
                values[key] = json.dumps(values[key])
        with self.connect() as conn:
            conn.execute("UPDATE tasks SET " + ",".join(key + "=?" for key in values) + " WHERE id=?",
                         (*values.values(), task_id))

    def logs(self, task_id):
        if self.get(task_id) is None:
            raise KeyError(task_id)
        path = self.directory / (task_id + ".log")
        if not path.exists():
            return ""
        with path.open("rb") as stream:
            size = stream.seek(0, 2)
            stream.seek(max(0, size - 128 * 1024))
            return stream.read().decode("utf-8", errors="replace")


class TaskManager:
    def __init__(self, root, configuration, directory=None):
        self.root = Path(root)
        self.configuration = configuration
        self.store = TaskStore(directory or self.root / "data/web")
        self.lock = threading.RLock()
        self.processes = {}
        self.stop_threads = {}
        # Workers detect a lost parent and cancel themselves. Do not allow a new
        # job to overlap one that is still shutting down after a server crash.
        for task in self.store.active():
            if not self._pid_alive(task["pid"]):
                self.store.update(task["id"], status="interrupted", finished_at=now(),
                                  error="Web server restarted before this task finished.")
            else:
                threading.Thread(target=self._watch_recovered, args=(task["id"], task["pid"]),
                                 daemon=True).start()

    def _watch_recovered(self, task_id, pid):
        while self.store.get(task_id)["status"] in ACTIVE:
            if not self._pid_alive(pid):
                self.store.update(task_id, status="interrupted", finished_at=now(),
                                  error="Recovered worker exited without a final result")
                return
            time.sleep(0.25)

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
                        "GMAIL_CONFIG_FROM_ENV": "1"})
            code_root = str(Path(__file__).resolve().parents[1])
            env["PYTHONPATH"] = os.pathsep.join(filter(None, (code_root, env.get("PYTHONPATH", ""))))
            if params.get("engine"):
                env["ENGINE_MODE"] = params["engine"]
            if action == "voice":
                if not env.get("VOICE_SERVER_TOKEN") or env["VOICE_SERVER_TOKEN"] == "changeme":
                    self.store.update(task["id"], status="failed", finished_at=now(),
                                      error="Configure VOICE_SERVER_TOKEN before starting voice service")
                    raise ValueError("Configure VOICE_SERVER_TOKEN before starting voice service")
                env["VOICE_SERVER_HOST"] = "127.0.0.1"
            # Web authentication secrets are not needed by automation workers.
            for key in ("WEB_ADMIN_PASSWORD", "WEB_SECRET_KEY"):
                env.pop(key, None)
            try:
                with (self.store.directory / (task["id"] + ".log")).open("wb") as output:
                    proc = subprocess.Popen(
                        [sys.executable, "-m", "web.worker", task["id"]],
                        cwd=self.root, env=env, stdin=subprocess.DEVNULL,
                        stdout=output, stderr=subprocess.STDOUT,
                        start_new_session=(os.name != "nt"),
                    )
            except OSError as exc:
                self.store.update(task["id"], status="failed", finished_at=now(), error=str(exc))
                raise
            self.processes[task["id"]] = proc
            self.store.update(task["id"], pid=proc.pid)
            threading.Thread(target=self._watch, args=(task["id"], proc), daemon=True).start()
            return self.store.get(task["id"])

    def _watch(self, task_id, proc):
        returncode = proc.wait()
        with self.lock:
            task = self.store.get(task_id)
            if task["status"] == "stopping":
                return  # The cancellation supervisor owns process-group cleanup.
            if task["status"] in ACTIVE:
                self.store.update(task_id, status="failed", finished_at=now(),
                                  error=f"Worker exited without a result (code {returncode})")
            self.processes.pop(task_id, None)

    def cancel(self, task_id):
        with self.lock:
            task = self.store.get(task_id)
            if task is None:
                raise KeyError(task_id)
            proc = self.processes.get(task_id)
            if task["status"] not in ACTIVE or proc is None:
                raise ValueError("Task is not running in this server")
            if task["status"] == "stopping":
                return task
            self.store.update(task_id, status="stopping")
            self._signal(proc, signal.SIGTERM)
            thread = threading.Thread(target=self._stop_task, args=(task_id, proc), daemon=True)
            self.stop_threads[task_id] = thread
            thread.start()
            return self.store.get(task_id)

    def _stop_task(self, task_id, proc):
        self._kill_later(proc)
        proc.wait()
        with self.lock:
            self.store.update(task_id, status="cancelled", finished_at=now())
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
        except ProcessLookupError:
            pass  # The worker exited between poll() and the signal.

    def _kill_later(self, proc):
        if os.name == "nt":
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
            return
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                os.killpg(proc.pid, 0)
            except ProcessLookupError:
                return
            except PermissionError:
                logging.warning("Task process group %s is no longer accessible for cleanup", proc.pid)
                return
            time.sleep(0.1)
        self._signal(proc, signal.SIGKILL)

    def close(self):
        with self.lock:
            for task_id in list(self.processes):
                if self.store.get(task_id)["status"] in ACTIVE:
                    self.cancel(task_id)
            threads = list(self.stop_threads.values())
        for thread in threads:
            thread.join(timeout=15)
            if thread.is_alive():
                logging.error("Task cancellation did not complete before server shutdown")
