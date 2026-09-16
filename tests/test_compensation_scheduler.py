import json
import os
import queue
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from web.app import create_app
from web.configuration import Configuration


ROOT = Path(__file__).resolve().parents[1]
ADMIN_PASSWORD = "test-only-scheduler-password"
SESSION_KEY = "test-only-scheduler-session-key"


def scheduler_environment(**overrides):
    values = {
        "COMPENSATION_SCHEDULER_ENABLED": "true",
        "COMPENSATION_SCHEDULER_INTERVAL_SECONDS": "300",
        "COMPENSATION_SCHEDULER_LIMIT": "100",
        "COMPENSATION_SCHEDULER_MAX_ATTEMPTS": "3",
        "COMPENSATION_SCHEDULER_BACKOFF_SECONDS": "30",
        "COMPENSATION_SCHEDULER_TIME_BUDGET_SECONDS": "30",
    }
    values.update(overrides)
    return values


class SchedulerFixture(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix=".test-scheduler-", dir=ROOT)
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name) / "runtime"
        (self.root / "data" / "web").mkdir(parents=True)

    def start_helper_process(self, source):
        process = subprocess.Popen(
            [sys.executable, "-u", "-c", source],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.addCleanup(self.cleanup_helper_process, process)
        lines = queue.Queue()
        reader = threading.Thread(
            target=lambda: lines.put(process.stdout.readline()), daemon=True,
        )
        reader.start()
        try:
            ready = lines.get(timeout=5)
        except queue.Empty:
            self.fail("helper process did not report readiness")
        self.assertEqual(ready, b"ready\n")
        return process

    @staticmethod
    def cleanup_helper_process(process):
        if process.poll() is None:
            process.kill()
        try:
            process.wait(timeout=5)
        finally:
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()


class SchedulerConfigurationTests(SchedulerFixture):
    def test_defaults_are_disabled_bounded_and_visible_as_nonsecrets(self):
        configuration = Configuration(self.root, environment={}, code_root=ROOT)

        self.assertEqual(configuration.compensation_scheduler_settings(), {
            "enabled": False,
            "interval_seconds": 300,
            "limit": 100,
            "max_attempts": 3,
            "backoff_seconds": 30,
            "time_budget_seconds": 30,
        })
        fields = {
            field["key"]: field for field in configuration.fields()
            if field["key"].startswith("COMPENSATION_SCHEDULER_")
        }
        self.assertEqual(len(fields), 6)
        self.assertTrue(all(not field["secret"] for field in fields.values()))

    def test_scheduler_values_are_strictly_validated(self):
        invalid = {
            "COMPENSATION_SCHEDULER_ENABLED": ("1", "yes", ""),
            "COMPENSATION_SCHEDULER_INTERVAL_SECONDS": ("0", "86401", "1.5"),
            "COMPENSATION_SCHEDULER_LIMIT": ("0", "1001", "bad"),
            "COMPENSATION_SCHEDULER_MAX_ATTEMPTS": ("0", "21", "bad"),
            "COMPENSATION_SCHEDULER_BACKOFF_SECONDS": ("-1", "86401", "bad"),
            "COMPENSATION_SCHEDULER_TIME_BUDGET_SECONDS": ("0", "301", "bad"),
        }
        for key, values in invalid.items():
            for value in values:
                with self.subTest(key=key, value=value):
                    configuration = Configuration(
                        self.root, environment={key: value}, code_root=ROOT,
                    )
                    with self.assertRaisesRegex(ValueError, key):
                        configuration.compensation_scheduler_settings()

    def test_scheduler_configuration_is_isolated_per_runtime_root(self):
        dev = self.root / "dev"
        prod = self.root / "prod"
        dev.mkdir()
        prod.mkdir()
        (dev / ".env").write_text(
            "COMPENSATION_SCHEDULER_ENABLED=true\n"
            "COMPENSATION_SCHEDULER_INTERVAL_SECONDS=11\n",
            encoding="utf-8",
        )
        (prod / ".env").write_text(
            "COMPENSATION_SCHEDULER_ENABLED=false\n"
            "COMPENSATION_SCHEDULER_INTERVAL_SECONDS=22\n",
            encoding="utf-8",
        )

        dev_settings = Configuration(dev, environment={}, code_root=ROOT).compensation_scheduler_settings()
        prod_settings = Configuration(prod, environment={}, code_root=ROOT).compensation_scheduler_settings()

        self.assertTrue(dev_settings["enabled"])
        self.assertFalse(prod_settings["enabled"])
        self.assertEqual(dev_settings["interval_seconds"], 11)
        self.assertEqual(prod_settings["interval_seconds"], 22)


class SchedulerRuntimeTests(SchedulerFixture):
    def test_normalized_settings_require_exact_boolean_enabled_value(self):
        from web.compensation_scheduler import run_scheduler

        settings = {
            "enabled": "false",
            "interval_seconds": 300,
            "limit": 100,
            "max_attempts": 3,
            "backoff_seconds": 30,
            "time_budget_seconds": 30,
        }
        run_job = Mock()

        with self.assertRaises(ValueError):
            run_scheduler(
                self.root,
                settings,
                run_job=run_job,
                ledger_factory=Mock(),
                once=True,
            )

        run_job.assert_not_called()

    def test_runtime_lock_is_nonoverlapping_and_loser_cannot_replace_status(self):
        from web.compensation_scheduler import (
            SchedulerAlreadyRunning,
            acquire_scheduler_lock,
            read_scheduler_status,
            run_scheduler,
            write_scheduler_status,
        )

        write_scheduler_status(self.root, {"enabled": True, "state": "idle", "claimed": 7})
        before = read_scheduler_status(self.root, enabled=True)
        lock = acquire_scheduler_lock(self.root)
        self.addCleanup(lock.close)
        run_job = Mock(side_effect=AssertionError("lock loser must not perform provider work"))

        with self.assertRaises(SchedulerAlreadyRunning):
            run_scheduler(
                self.root, scheduler_environment(), run_job=run_job,
                ledger_factory=Mock(), once=True,
            )

        run_job.assert_not_called()
        self.assertEqual(read_scheduler_status(self.root, enabled=True), before)

    @unittest.skipIf(os.name == "nt", "POSIX symlink semantics")
    def test_runtime_lock_rejects_symlink_without_touching_target_or_running_pass(self):
        from web.compensation_scheduler import run_scheduler

        target = self.root / "lock-target"
        target.write_bytes(b"target-must-stay-unchanged")
        target.chmod(0o640)
        lock_path = self.root / "data" / "web" / "compensation-scheduler.lock"
        lock_path.symlink_to(target)
        run_job = Mock(side_effect=AssertionError("lock rejection must precede provider work"))

        with self.assertRaises(ValueError):
            run_scheduler(
                self.root,
                scheduler_environment(),
                run_job=run_job,
                ledger_factory=Mock(),
                once=True,
            )

        self.assertEqual(target.read_bytes(), b"target-must-stay-unchanged")
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o640)
        self.assertFalse(
            (self.root / "data" / "web" / "compensation-scheduler-status.json").exists()
        )
        run_job.assert_not_called()

    def test_one_pass_forwards_all_bounds_and_publishes_only_finite_status(self):
        from web.compensation_scheduler import read_scheduler_status, run_scheduler

        stop = threading.Event()
        ledger = object()
        secret = "provider-payload-must-not-persist"
        run_job = Mock(return_value={
            "claimed": 4,
            "cancelled": 3,
            "completed": 1,
            "failed": 0,
            "provider_payload": secret,
        })

        run_scheduler(
            self.root,
            scheduler_environment(
                COMPENSATION_SCHEDULER_LIMIT="4",
                COMPENSATION_SCHEDULER_MAX_ATTEMPTS="5",
                COMPENSATION_SCHEDULER_BACKOFF_SECONDS="17",
                COMPENSATION_SCHEDULER_TIME_BUDGET_SECONDS="19",
            ),
            cancel_event=stop,
            run_job=run_job,
            ledger_factory=lambda _path: ledger,
            once=True,
        )

        run_job.assert_called_once_with(
            ledger=ledger,
            limit=4,
            max_attempts=5,
            backoff_seconds=17,
            source="periodic-scheduler",
            time_budget_seconds=19,
            cancel_event=stop,
        )
        status = read_scheduler_status(self.root, enabled=True)
        self.assertEqual(status["state"], "stopped")
        self.assertEqual(
            {key: status[key] for key in ("claimed", "cancelled", "completed", "failed")},
            {"claimed": 4, "cancelled": 3, "completed": 1, "failed": 0},
        )
        self.assertNotIn(secret, json.dumps(status))
        self.assertNotIn("provider_payload", status)

    def test_status_write_is_atomic_private_and_allow_listed(self):
        from web.compensation_scheduler import read_scheduler_status, write_scheduler_status

        secret = "otp=718293 provider-token=hidden"
        write_scheduler_status(self.root, {
            "enabled": True,
            "state": "running",
            "claimed": 2,
            "cancelled": 1,
            "completed": 0,
            "failed": 1,
            "error_code": "provider_timeout",
            "provider_payload": secret,
            "order_id": "remote-order-secret",
        })

        path = self.root / "data" / "web" / "compensation-scheduler-status.json"
        raw = path.read_text(encoding="utf-8")
        parsed = json.loads(raw)
        self.assertNotIn(secret, raw)
        self.assertNotIn("provider_payload", parsed)
        self.assertNotIn("order_id", parsed)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(list(path.parent.glob(".compensation-status-*")), [])
        self.assertEqual(read_scheduler_status(self.root, enabled=True)["error_code"], "provider_timeout")

    def test_status_writer_rejects_malformed_allow_list_fields(self):
        from web.compensation_scheduler import write_scheduler_status

        cases = (
            {"enabled": "true", "state": "idle"},
            {"enabled": True, "state": "arbitrary"},
            {"enabled": True, "state": "idle", "claimed": True},
            {"enabled": True, "state": "idle", "error_code": "password=secret"},
            {"enabled": True, "state": "idle", "started_at": "not-a-time"},
        )
        for status in cases:
            with self.subTest(status=status), self.assertRaises(ValueError):
                write_scheduler_status(self.root, status)

    def test_status_read_rejects_path_replaced_after_descriptor_open(self):
        from web.compensation_scheduler import read_scheduler_status, write_scheduler_status

        status_path = self.root / "data" / "web" / "compensation-scheduler-status.json"
        write_scheduler_status(self.root, {
            "enabled": True, "state": "idle", "claimed": 1,
        })
        secret = "raced-provider-payload-must-not-be-exposed"
        attacker_target = self.root / "attacker-status.json"
        attacker_target.write_text(json.dumps({
            "enabled": True,
            "state": "idle",
            "claimed": 999,
            "provider_payload": secret,
        }), encoding="utf-8")
        replacement = status_path.parent / ".raced-status-link"
        replacement.symlink_to(attacker_target)
        real_open = os.open

        def replace_after_open(path, flags, *args, **kwargs):
            descriptor = real_open(path, flags, *args, **kwargs)
            if Path(path) == status_path:
                os.replace(replacement, status_path)
            return descriptor

        with patch("web.compensation_scheduler.os.open", side_effect=replace_after_open):
            status = read_scheduler_status(self.root, enabled=True)

        self.assertEqual(status["state"], "unavailable")
        self.assertEqual(status["error_code"], "status_unavailable")
        self.assertNotIn(secret, json.dumps(status))

    @unittest.skipUnless(hasattr(os, "mkfifo"), "requires POSIX special files")
    def test_status_missing_symlink_and_nonregular_files_return_finite_safe_states(self):
        from web.compensation_scheduler import read_scheduler_status

        status_path = self.root / "data" / "web" / "compensation-scheduler-status.json"
        missing = read_scheduler_status(self.root, enabled=True)
        self.assertEqual(missing["state"], "not_started")

        secret = "status-file-secret-must-not-be-exposed"
        target = self.root / "status-target.json"
        target.write_text(secret, encoding="utf-8")
        status_path.symlink_to(target)
        linked = read_scheduler_status(self.root, enabled=True)
        self.assertEqual(linked["state"], "unavailable")
        self.assertNotIn(secret, json.dumps(linked))

        status_path.unlink()
        status_path.mkdir()
        directory = read_scheduler_status(self.root, enabled=True)
        self.assertEqual(directory["state"], "unavailable")
        self.assertNotIn(secret, json.dumps(directory))

        status_path.rmdir()
        os.mkfifo(status_path)
        fifo = read_scheduler_status(self.root, enabled=True)
        self.assertEqual(fifo["state"], "unavailable")
        self.assertNotIn(secret, json.dumps(fifo))

    def test_enabled_live_status_becomes_finite_stale_state(self):
        from web.compensation_scheduler import read_scheduler_status, write_scheduler_status

        written = write_scheduler_status(self.root, {
            "enabled": True, "state": "running",
        })
        updated = datetime.fromisoformat(written["updated_at"].replace("Z", "+00:00"))

        status = read_scheduler_status(
            self.root,
            enabled=True,
            stale_after_seconds=10,
            now=updated + timedelta(seconds=11),
        )

        self.assertEqual(status["state"], "stale")
        self.assertEqual(status["error_code"], "scheduler_stale")

    def test_interval_wait_is_interruptible_and_shutdown_is_bounded(self):
        from web.compensation_scheduler import run_scheduler

        class StopDuringWait:
            def __init__(self):
                self.waits = []

            def is_set(self):
                return False

            def wait(self, timeout):
                self.waits.append(timeout)
                return True

        stop = StopDuringWait()
        run_job = Mock(return_value={
            "claimed": 0, "cancelled": 0, "completed": 0, "failed": 0,
        })

        run_scheduler(
            self.root,
            scheduler_environment(COMPENSATION_SCHEDULER_INTERVAL_SECONDS="13"),
            cancel_event=stop,
            run_job=run_job,
            ledger_factory=lambda _path: object(),
        )

        run_job.assert_called_once()
        self.assertEqual(stop.waits, [13])

    def test_status_initialization_failure_still_releases_runtime_lock(self):
        from web.compensation_scheduler import run_scheduler

        lock = Mock()
        with patch(
            "web.compensation_scheduler.acquire_scheduler_lock", return_value=lock,
        ), patch(
            "web.compensation_scheduler.write_scheduler_status",
            side_effect=OSError("synthetic status failure"),
        ), self.assertRaises(OSError):
            run_scheduler(
                self.root,
                scheduler_environment(),
                run_job=Mock(),
                ledger_factory=Mock(),
                once=True,
            )

        lock.close.assert_called_once_with()


class SchedulerSupervisorTests(SchedulerFixture):
    def test_disabled_supervisor_never_starts_a_process(self):
        from web.server import CompensationSchedulerSupervisor

        configuration = Configuration(self.root, environment={}, code_root=ROOT)
        with patch("web.server.subprocess.Popen") as popen:
            supervisor = CompensationSchedulerSupervisor(self.root, configuration)
            self.assertFalse(supervisor.start())
            self.assertTrue(supervisor.stop())
        popen.assert_not_called()

    def test_enabled_supervisor_uses_isolated_environment_without_web_secrets(self):
        from web.server import CompensationSchedulerSupervisor

        environment = scheduler_environment(
            WEB_ADMIN_PASSWORD="admin-secret-must-not-reach-scheduler",
            WEB_SECRET_KEY="session-secret-must-not-reach-scheduler",
            FIVESIM_API_KEY="provider-key-required-by-scheduler",
        )
        configuration = Configuration(self.root, environment=environment, code_root=ROOT)
        process = Mock(pid=4321)
        process.poll.return_value = None
        process.wait.return_value = 0
        with patch("web.server.subprocess.Popen", return_value=process) as popen:
            supervisor = CompensationSchedulerSupervisor(self.root, configuration)
            self.assertTrue(supervisor.start())
            self.assertTrue(supervisor.stop())

        args, kwargs = popen.call_args
        self.assertEqual(args[0], [
            sys.executable, "-m", "web.compensation_scheduler", "--root", str(self.root),
        ])
        self.assertEqual(kwargs["cwd"], ROOT)
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(kwargs["stdout"], subprocess.DEVNULL)
        self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
        self.assertEqual(kwargs["start_new_session"], os.name != "nt")
        child_environment = kwargs["env"]
        self.assertEqual(child_environment["FIVESIM_API_KEY"], "provider-key-required-by-scheduler")
        self.assertEqual(child_environment["GMAIL_CONFIG_FROM_ENV"], "1")
        self.assertNotIn("WEB_ADMIN_PASSWORD", child_environment)
        self.assertNotIn("WEB_SECRET_KEY", child_environment)
        process.terminate.assert_called_once()
        process.wait.assert_called_once()

    def test_supervisor_escalates_only_after_timeout_and_requires_exit_observation(self):
        from web.server import CompensationSchedulerSupervisor

        configuration = Configuration(
            self.root, environment=scheduler_environment(), code_root=ROOT,
        )
        process = Mock(pid=4322)
        process.poll.return_value = None
        process.wait.side_effect = [
            subprocess.TimeoutExpired("scheduler", 10),
            subprocess.TimeoutExpired("scheduler", 2),
        ]
        with patch("web.server.subprocess.Popen", return_value=process):
            supervisor = CompensationSchedulerSupervisor(self.root, configuration)
            self.assertTrue(supervisor.start())
            self.assertFalse(supervisor.stop())

        process.terminate.assert_called_once()
        process.kill.assert_called_once()
        self.assertIs(supervisor.process, process)

    def test_supervisor_normally_terminates_real_ready_helper_and_observes_exit(self):
        from web.server import CompensationSchedulerSupervisor

        process = self.start_helper_process(
            "import sys, threading\n"
            "sys.stdout.write('ready\\n'); sys.stdout.flush()\n"
            "threading.Event().wait()\n"
        )
        supervisor = CompensationSchedulerSupervisor(
            self.root, Configuration(self.root, environment={}, code_root=ROOT),
        )
        supervisor.process = process

        self.assertTrue(supervisor.stop(timeout=2, kill_timeout=2))
        self.assertIsNotNone(process.poll())
        self.assertIsNone(supervisor.process)

    def test_supervisor_cleans_up_real_already_exited_helper(self):
        from web.server import CompensationSchedulerSupervisor

        process = self.start_helper_process(
            "import sys\n"
            "sys.stdout.write('ready\\n'); sys.stdout.flush()\n"
        )
        process.wait(timeout=5)
        supervisor = CompensationSchedulerSupervisor(
            self.root, Configuration(self.root, environment={}, code_root=ROOT),
        )
        supervisor.process = process

        self.assertTrue(supervisor.stop(timeout=0.1, kill_timeout=0.1))
        self.assertIsNotNone(process.poll())
        self.assertIsNone(supervisor.process)

    @unittest.skipIf(os.name == "nt" or not hasattr(signal, "SIGTERM"),
                     "requires POSIX SIGTERM handling")
    def test_supervisor_kills_real_helper_after_sigterm_timeout_and_observes_exit(self):
        from web.server import CompensationSchedulerSupervisor

        process = self.start_helper_process(
            "import signal, sys, threading\n"
            "signal.signal(signal.SIGTERM, lambda *_args: None)\n"
            "sys.stdout.write('ready\\n'); sys.stdout.flush()\n"
            "threading.Event().wait()\n"
        )
        supervisor = CompensationSchedulerSupervisor(
            self.root, Configuration(self.root, environment={}, code_root=ROOT),
        )
        supervisor.process = process

        self.assertTrue(supervisor.stop(timeout=0.1, kill_timeout=2))
        self.assertIsNotNone(process.poll())
        self.assertIsNone(supervisor.process)


class SchedulerWebStatusTests(SchedulerFixture):
    def make_app(self, environment=None):
        return create_app(
            root=self.root,
            code_root=ROOT,
            password=ADMIN_PASSWORD,
            secret_key=SESSION_KEY,
            environment={} if environment is None else environment,
        )

    @staticmethod
    def login(client):
        client.get("/login")
        with client.session_transaction() as current:
            csrf = current["csrf"]
        response = client.post(
            "/login", data={"password": ADMIN_PASSWORD, "csrf_token": csrf},
        )
        if response.status_code != 302:
            raise AssertionError("test login failed")
        with client.session_transaction() as current:
            return current["csrf"]

    def test_app_factory_never_starts_scheduler_and_status_api_is_authenticated_read_only(self):
        environment = scheduler_environment()
        with patch("subprocess.Popen", side_effect=AssertionError("app factory started a process")):
            app = self.make_app(environment)
        self.addCleanup(app.extensions["web_tasks"].close)
        app.config["TESTING"] = True
        client = app.test_client()

        self.assertEqual(client.get("/api/compensation-scheduler").status_code, 401)
        csrf = self.login(client)
        response = client.get("/api/compensation-scheduler")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["status"]["state"], "not_started")
        self.assertEqual(
            client.post(
                "/api/compensation-scheduler", headers={"X-CSRF-Token": csrf},
            ).status_code,
            405,
        )

    def test_malformed_status_returns_finite_safe_projection(self):
        environment = scheduler_environment()
        app = self.make_app(environment)
        self.addCleanup(app.extensions["web_tasks"].close)
        app.config["TESTING"] = True
        client = app.test_client()
        self.login(client)
        secret = "provider-response-secret-491"
        path = self.root / "data" / "web" / "compensation-scheduler-status.json"
        path.write_text(json.dumps({"state": "anything", "exception": secret}), encoding="utf-8")

        response = client.get("/api/compensation-scheduler")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()["status"]
        self.assertEqual(payload["state"], "unavailable")
        self.assertEqual(payload["error_code"], "status_unavailable")
        self.assertNotIn(secret, response.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
