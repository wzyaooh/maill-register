import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from web.configuration import Configuration
from web.tasks import ACTIVE, TaskManager, TaskStore
from core.profile_runtime import ProfileRuntime


ROOT = Path(__file__).resolve().parents[1]


class ProcessLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "config").mkdir()
        shutil.copyfile(ROOT / "config/settings.py", self.root / "config/settings.py")
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(ROOT)
        config = Configuration(self.root, environment)
        self.manager = TaskManager(self.root, config)
        self.addCleanup(self.manager.close)

    def wait_for_finish(self, task_id):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            task = self.manager.store.get(task_id)
            if task["status"] not in ACTIVE:
                return task
            time.sleep(0.05)
        self.fail("Worker did not terminate")

    def test_real_validation_worker_returns_persistent_result_and_logs(self):
        task = self.manager.start("validate", {})
        result = self.wait_for_finish(task["id"])
        self.assertEqual(result["status"], "completed", self.manager.store.logs(task["id"]))
        self.assertIn("errors", result["result"])
        self.assertIn("warnings", result["result"])
        self.assertEqual(result["progress"]["completed"], 1)
        reopened = TaskStore(self.manager.store.directory)
        self.assertEqual(reopened.get(task["id"])["result"], result["result"])
        self.assertIn("completed", reopened.logs(task["id"]))

    def test_worker_pipe_sanitizes_os_write_before_log_reaches_disk(self):
        package = self.root / "web"
        package.mkdir()
        (package / "__init__.py").write_text("", encoding="utf-8")
        secret = "pipe-password-secret-938475"
        (package / "worker.py").write_text(
            "import os\n"
            "os.write(1, b'password=" + secret + "\\n')\n",
            encoding="utf-8",
        )
        task = self.manager.start("validate", {})
        result = self.wait_for_finish(task["id"])
        path = self.manager.store.directory / (task["id"] + ".log")
        raw = path.read_text(encoding="utf-8")
        self.assertNotIn(secret, raw)
        self.assertIn("[redacted]", raw)
        self.assertEqual(result["status"], "failed")

    def test_cancel_terminates_real_process_and_records_cancellation(self):
        # A local idle fixture exercises supervision without touching a service.
        package = self.root / "web"
        package.mkdir()
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "worker.py").write_text(
            "import time\nprint('fixture ready', flush=True)\ntime.sleep(60)\n", encoding="utf-8")
        task = self.manager.start("validate", {})
        deadline = time.monotonic() + 5
        while "fixture ready" not in self.manager.store.logs(task["id"]):
            if time.monotonic() > deadline:
                self.fail("Fixture did not start")
            time.sleep(0.05)
        self.manager.cancel(task["id"])
        result = self.wait_for_finish(task["id"])
        self.assertEqual(result["status"], "cancelled")

    @unittest.skipIf(os.name == "nt", "POSIX process group behavior")
    def test_cancelled_worker_releases_profile_lease(self):
        runtime = ProfileRuntime(self.root)
        handle = runtime.provision("lease@example.test", "playwright")
        runtime.bind(handle, "lease@example.test")
        runtime.mark_ready(handle)

        package = self.root / "web"
        package.mkdir()
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "worker.py").write_text(
            "import os,time\n"
            "from core.profile_runtime import ProfileRuntime\n"
            "runtime=ProfileRuntime(os.environ['PROFILE_RUNTIME_ROOT'])\n"
            "handle=runtime.resolve(profile_id=%r)\n"
            "lease=runtime.lease(handle,'fixture')\n"
            "lease.acquire()\n"
            "print('lease ready', flush=True)\n"
            "time.sleep(60)\n" % handle.profile_id,
            encoding="utf-8",
        )

        task = self.manager.start("validate", {})
        deadline = time.monotonic() + 5
        while "lease ready" not in self.manager.store.logs(task["id"]):
            if time.monotonic() > deadline:
                self.fail("Lease fixture did not start")
            time.sleep(0.05)

        self.manager.cancel(task["id"])
        result = self.wait_for_finish(task["id"])
        self.assertEqual(result["status"], "cancelled")
        with runtime.lease(handle, "after-cancel") as lease:
            self.assertTrue(lease.assert_stable())

    @unittest.skipIf(os.name == "nt", "POSIX process group behavior")
    def test_cancel_cleans_resistant_descendant_after_worker_exits(self):
        package = self.root / "web"
        package.mkdir()
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "worker.py").write_text(
            "import subprocess,sys,time\n"
            "subprocess.Popen([sys.executable, '-c', "
            "\"import os,signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); "
            "print('child ready '+str(os.getpid()),flush=True); time.sleep(60)\"])\n"
            "time.sleep(60)\n", encoding="utf-8")
        task = self.manager.start("validate", {})
        deadline = time.monotonic() + 5
        while "child ready" not in self.manager.store.logs(task["id"]):
            if time.monotonic() > deadline:
                self.fail("Descendant fixture did not start")
            time.sleep(0.05)
        self.manager.cancel(task["id"])
        result = self.wait_for_finish(task["id"])
        self.assertEqual(result["status"], "cancelled")
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                os.killpg(task["pid"], 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            os.killpg(task["pid"], signal.SIGKILL)
            self.fail("Descendant process group survived cancellation")

    def test_recovered_worker_exit_releases_task_slot(self):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        task = self.manager.store.create("validate", {})
        self.manager.store.update(task["id"], pid=proc.pid)
        recovered = TaskManager(self.root, self.manager.configuration)
        self.addCleanup(recovered.close)
        proc.terminate()
        proc.wait(timeout=5)
        result = self.wait_for_finish(task["id"])
        self.assertEqual(result["status"], "interrupted")

    def test_task_manager_startup_recovers_durable_ledger_attempts(self):
        from core.job_ledger import JobLedger

        ledger_path = self.root / "data" / "database.db"
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        JobLedger(str(ledger_path)).create_job("warm")
        with patch("web.worker.recover_stale_attempts") as recover:
            manager = TaskManager(self.root, self.manager.configuration)
            self.addCleanup(manager.close)

        recover.assert_called_once()
        recovered_ledger = recover.call_args.kwargs["ledger"]
        self.assertEqual(recovered_ledger.db_path, str(ledger_path))

    def test_stop_task_does_not_coerce_non_boolean_cleanup_fact(self):
        task = self.manager.store.create("validate", {})
        process = type(
            "Process",
            (),
            {"pid": 12345, "wait": lambda self, timeout=None: None},
        )()
        self.manager.store.update(task["id"], status="stopping", pid=process.pid)
        self.manager.processes[task["id"]] = process
        with patch.object(self.manager, "_kill_later", return_value="false"), \
             patch.object(self.manager, "_wait_for_process_group_gone", return_value=True):
            self.manager._stop_task(task["id"], process)
        result = self.manager.store.get(task["id"])
        self.assertEqual(result["status"], "cleanup_failed")

    def test_stop_task_records_cleanup_failure_when_supervisor_raises(self):
        task = self.manager.store.create("validate", {})
        process = type(
            "Process",
            (),
            {"pid": 12346, "wait": lambda self, timeout=None: None},
        )()
        self.manager.store.update(task["id"], status="stopping", pid=process.pid)
        self.manager.processes[task["id"]] = process
        with patch.object(self.manager, "_kill_later", side_effect=RuntimeError("probe")):
            self.manager._stop_task(task["id"], process)
        result = self.manager.store.get(task["id"])
        self.assertEqual(result["status"], "cleanup_failed")

    def test_unverified_stop_overrides_worker_cancelled_ledger_job(self):
        """Cleanup uncertainty wins over a worker's premature cancellation."""
        from core.job_ledger import JobLedger, current_worker_id

        task = self.manager.store.create("warm", {})
        ledger = JobLedger(str(self.root / "data" / "database.db"))
        job = ledger.create_job("warm", job_id=task["id"])
        owner = current_worker_id()
        succeeded = ledger.start_attempt(
            job["job_id"], subject="complete@example.test", worker_id=owner,
        )
        ledger.finish_attempt(
            succeeded["attempt_id"], outcome="succeeded", worker_id=owner,
        )
        live = ledger.start_attempt(
            job["job_id"], subject="unfinished@example.test", worker_id=owner,
        )
        ledger.finalize_job(job["job_id"], state="cancelled", worker_id=owner)

        process = type(
            "Process", (), {"pid": 12347, "wait": lambda self, timeout=None: None},
        )()
        self.manager.store.update(task["id"], status="stopping", pid=process.pid)
        self.manager.processes[task["id"]] = process
        with patch.object(self.manager, "_kill_later", return_value=False):
            self.manager._stop_task(task["id"], process)

        self.assertEqual(self.manager.store.get(task["id"])["status"], "cleanup_failed")
        reconciled = ledger.get_job(job["job_id"])
        self.assertEqual(reconciled["state"], "failed")
        self.assertEqual(reconciled["terminal_error_code"], "cleanup_failed")
        self.assertEqual(ledger.get_attempt(succeeded["attempt_id"])["state"], "succeeded")
        self.assertEqual(ledger.get_attempt(live["attempt_id"])["state"], "cancelled")

    def test_worker_wait_exception_is_cleanup_failure_not_plain_worker_exit(self):
        """An untrusted wait failure cannot be reported as a normal exit."""
        task = self.manager.store.create("validate", {})
        process = type(
            "Process",
            (),
            {
                "pid": 12348,
                "wait": lambda self: (_ for _ in ()).throw(RuntimeError("wait failed")),
            },
        )()
        self.manager.processes[task["id"]] = process
        with patch.object(self.manager, "_finish_log_capture"):
            self.manager._watch(task["id"], process)
        result = self.manager.store.get(task["id"])
        self.assertEqual(result["status"], "cleanup_failed")
        self.assertIn("cleanup_failed", result["error"])

    def test_worker_completion_cannot_overwrite_recovered_terminal_state(self):
        """A late worker result must not replace an interrupted task state."""
        import io
        import sys
        import web.worker as worker

        task = self.manager.store.create("validate", {})
        old_argv = sys.argv
        old_stdout, old_stderr = sys.stdout, sys.stderr
        try:
            sys.argv = ["worker", task["id"]]
            sys.stdout = io.StringIO()
            sys.stderr = io.StringIO()

            def late_result(_action, _params, _report, job_id=None):
                self.manager.store.update(
                    job_id, status="interrupted", finished_at="recovered"
                )
                return {"success": True}

            with patch.object(worker, "TaskStore", return_value=self.manager.store), \
                 patch.object(worker, "_invoke_execute", side_effect=late_result), \
                 patch.dict(os.environ, {
                     "WEB_TASK_DIRECTORY": str(self.manager.store.directory),
                     "WEB_PARENT_PID": str(os.getpid()),
                 }, clear=False):
                worker.main()
        finally:
            sys.argv = old_argv
            sys.stdout, sys.stderr = old_stdout, old_stderr

        result = self.manager.store.get(task["id"])
        self.assertEqual(result["status"], "interrupted")
        self.assertEqual(result["finished_at"], "recovered")
        self.assertIsNone(result["result"])

    @unittest.skipIf(os.name == "nt", "POSIX process group behavior")
    def test_worker_exit_with_live_process_group_is_cleanup_failure(self):
        """A returned worker PID does not prove browser descendants are gone."""
        task = self.manager.store.create("validate", {})
        process = type(
            "Process",
            (),
            {
                "pid": 12350,
                "wait": lambda self: 0,
            },
        )()
        self.manager.processes[task["id"]] = process
        with patch.object(self.manager, "_finish_log_capture"), \
             patch.object(self.manager, "_process_group_gone", return_value=False), \
             patch.object(self.manager, "_wait_for_process_group_gone", return_value=False):
            self.manager._watch(task["id"], process)
        result = self.manager.store.get(task["id"])
        self.assertEqual(result["status"], "cleanup_failed")
        self.assertTrue(result["error"].startswith("cleanup_failed:"))

    @unittest.skipIf(os.name == "nt", "POSIX process group behavior")
    def test_completed_worker_with_live_descendant_is_downgraded_to_cleanup_failure(self):
        """A completed result is not trustworthy while its process group lives."""
        task = self.manager.store.create("warm", {})
        self.manager.store.update(
            task["id"], status="completed", finished_at="winner",
            result={"success": True},
        )
        process = type(
            "Process",
            (),
            {"pid": 12355, "wait": lambda self: 0},
        )()
        self.manager.processes[task["id"]] = process
        with patch.object(self.manager, "_finish_log_capture"), \
             patch.object(self.manager, "_process_group_gone", return_value=False), \
             patch.object(self.manager, "_wait_for_process_group_gone", return_value=False), \
             patch.object(self.manager, "_signal", return_value=True):
            self.manager._watch(task["id"], process)
        result = self.manager.store.get(task["id"])
        self.assertEqual(result["status"], "cleanup_failed")
        self.assertTrue(result["error"].startswith("cleanup_failed:"))

    @staticmethod
    def _run_lost_worker_cleanup(task_id, pid, directory, ledger_path):
        from web import worker_cleanup

        with patch.object(worker_cleanup, "_process_group_gone", return_value=True):
            return worker_cleanup.cleanup_lost_worker(
                task_id, pid, directory, ledger_path,
            )

    def test_lost_parent_helper_reconciles_running_task_as_interrupted(self):
        """The detached helper must handle a row still marked ``running``."""
        from web.worker_cleanup import cleanup_lost_worker

        task = self.manager.store.create("validate", {})
        result = None
        with patch("web.worker_cleanup._process_group_gone", return_value=True):
            result = cleanup_lost_worker(
                task["id"], 12349, self.manager.store.directory,
            )
        self.assertTrue(result)
        persisted = self.manager.store.get(task["id"])
        self.assertEqual(persisted["status"], "interrupted")

    def test_recovered_stopping_task_waits_for_process_group_before_terminal_state(self):
        recovered = TaskManager(self.root, self.manager.configuration)
        self.addCleanup(recovered.close)
        task = recovered.store.create("validate", {})
        pid = 12347
        recovered.store.update(task["id"], status="stopping", pid=pid)
        with patch.object(recovered, "_pid_alive", return_value=False), \
             patch.object(recovered, "_process_group_gone", return_value=False), \
             patch.object(recovered, "_wait_for_process_group_gone", return_value=False), \
             patch.object(recovered, "_signal", return_value=False):
            # The recovery watcher normally runs in a daemon thread; invoke a
            # single reconciliation pass directly to make the contract
            # deterministic.
            recovered._reconcile_recovered_task(task["id"], pid)
        result = recovered.store.get(task["id"])
        self.assertEqual(result["status"], "cleanup_failed")

    @unittest.skipIf(os.name == "nt", "POSIX process group behavior")
    def test_recovered_cleanup_does_not_overwrite_concurrent_terminal_state(self):
        """A recovery pass must lose a compare-and-set race cleanly."""
        recovered = TaskManager(self.root, self.manager.configuration)
        self.addCleanup(recovered.close)
        task = recovered.store.create("validate", {})
        pid = 12351

        def lose_terminal_claim(task_id, expected, **values):
            recovered.store.update(task_id, status="completed", finished_at="winner")
            return False

        with patch.object(recovered, "_process_group_gone", return_value=True), \
             patch.object(recovered.store, "update_if_status", side_effect=lose_terminal_claim):
            recovered._reconcile_recovered_task(task["id"], pid)
        result = recovered.store.get(task["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["finished_at"], "winner")

    def test_repeated_cancel_after_supervisor_removed_is_idempotent(self):
        task = self.manager.store.create("validate", {})
        self.manager.store.update(task["id"], status="stopping")
        # A stop supervisor may have already removed its process entry while a
        # client retries the cancel request.  The durable stopping state is
        # still authoritative and should be returned unchanged.
        result = self.manager.cancel(task["id"])
        self.assertEqual(result["status"], "stopping")

    def test_stop_supervisor_does_not_overwrite_concurrent_terminal_state(self):
        """The cancellation worker must lose a late terminal-state race."""
        task = self.manager.store.create("validate", {})
        process = type(
            "Process",
            (),
            {"pid": 12352, "wait": lambda self, timeout=None: None},
        )()
        self.manager.store.update(task["id"], status="stopping", pid=process.pid)

        def lose_terminal_claim(task_id, expected, **values):
            self.manager.store.update(task_id, status="completed", finished_at="winner")
            return False

        with patch.object(self.manager, "_kill_later", return_value=True), \
             patch.object(self.manager, "_wait_for_process_group_gone", return_value=True), \
             patch.object(self.manager.store, "update_if_status", side_effect=lose_terminal_claim):
            self.manager._stop_task(task["id"], process)
        result = self.manager.store.get(task["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["finished_at"], "winner")

    @unittest.skipUnless(os.name != "nt", "Windows contract is simulated")
    def test_stop_supervisor_requires_windows_process_exit_fact(self):
        """A Windows wait return is insufficient while the handle is alive."""
        task = self.manager.store.create("validate", {})
        process = type(
            "Process",
            (),
            {
                "pid": 12354,
                "wait": lambda self, timeout=None: None,
                "poll": lambda self: None,
            },
        )()
        self.manager.store.update(task["id"], status="stopping", pid=process.pid)
        with patch("web.tasks.os.name", "nt"), \
             patch.object(self.manager, "_kill_later", return_value=True), \
             patch.object(self.manager, "_finalize_ledger"):
            self.manager._stop_task(task["id"], process)
        result = self.manager.store.get(task["id"])
        self.assertEqual(result["status"], "cleanup_failed")

    def test_cancel_does_not_signal_after_running_state_compare_and_set_loses(self):
        """A raced completion must not receive a stale cancellation signal."""
        task = self.manager.store.create("validate", {})
        process = type("Process", (), {"pid": 12353})()
        self.manager.processes[task["id"]] = process

        def lose_running_claim(task_id, expected, **values):
            self.manager.store.update(task_id, status="completed", finished_at="winner")
            return False

        with patch.object(self.manager.store, "update_if_status", side_effect=lose_running_claim), \
             patch.object(self.manager, "_signal") as signal_worker:
            result = self.manager.cancel(task["id"])
        self.assertEqual(result["status"], "completed")
        signal_worker.assert_not_called()

    @unittest.skipIf(os.name == "nt", "POSIX orphan process group behavior")
    def test_lost_parent_does_not_abandon_resistant_browser_descendant(self):
        task = self.manager.store.create("validate", {})
        fixture = self.root / "orphan_worker.py"
        fixture.write_text(
            "import subprocess,sys,time\n"
            "from web import worker\n"
            "def execute(action,params,report):\n"
            "    subprocess.Popen([sys.executable,'-c',"
            "\"import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN);"
            "print('child ready',flush=True);time.sleep(60)\"])\n"
            "    time.sleep(60)\n"
            "worker.execute=execute\n"
            "worker.main()\n", encoding="utf-8")
        log = self.root / "orphan.log"
        environment = dict(self.manager.configuration.environment)
        environment["WEB_TASK_DIRECTORY"] = str(self.manager.store.directory)
        parent = subprocess.Popen([
            sys.executable, "-c",
            "import os,subprocess,sys,time\n"
            "os.environ['WEB_PARENT_PID']=str(os.getpid())\n"
            "with open(sys.argv[3],'wb') as log:\n"
            "    p=subprocess.Popen([sys.executable,sys.argv[1],sys.argv[2]],"
            "stdout=log,stderr=log,start_new_session=True)\n"
            "print(p.pid,flush=True)\n"
            "time.sleep(60)\n",
            str(fixture), task["id"], str(log),
        ], env=environment, stdout=subprocess.PIPE, text=True)
        worker_pid = int(parent.stdout.readline())

        def cleanup():
            if parent.poll() is None:
                parent.kill()
            parent.wait(timeout=5)
            parent.stdout.close()
            try:
                os.killpg(worker_pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

        self.addCleanup(cleanup)
        self.manager.store.update(task["id"], pid=worker_pid)
        deadline = time.monotonic() + 5
        while not log.exists() or "child ready" not in log.read_text(encoding="utf-8"):
            if time.monotonic() > deadline:
                self.fail("Orphan fixture did not start")
            time.sleep(0.05)
        parent.kill()
        parent.wait(timeout=5)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                os.killpg(worker_pid, 0)
            except ProcessLookupError:
                break
            except PermissionError:
                self.skipTest("macOS sandbox does not permit process-group inspection")
            time.sleep(0.1)
        else:
            self.fail("Orphan browser process group survived its cleanup watchdog")
        # The lost-parent watchdog owns the cancellation lifecycle even after
        # it has to terminate its own worker process group.  It must leave a
        # durable terminal state instead of stranded ``stopping`` metadata.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            state = self.manager.store.get(task["id"])
            if state and state["status"] not in ("running", "stopping"):
                break
            time.sleep(0.05)
        else:
            self.fail("Lost-parent cleanup left the task in stopping")
        self.assertEqual(state["status"], "interrupted")

    def test_active_service_remains_visible_after_history_limit(self):
        voice = self.manager.store.create("voice", {})
        for _ in range(101):
            task = self.manager.store.create("validate", {})
            self.manager.store.update(task["id"], status="completed")
        self.assertIn(voice["id"], [task["id"] for task in self.manager.store.list()])
        self.assertEqual([voice["id"]], [task["id"] for task in self.manager.store.active()])
        self.manager.store.update(voice["id"], status="cancelled")

    @unittest.skipIf(os.name == "nt", "POSIX process group behavior")
    def test_signal_permission_error_is_reported_without_raising(self):
        process = type("Process", (), {"pid": 12345, "poll": lambda self: None})()
        with patch("web.tasks.os.killpg", side_effect=PermissionError("sandbox")):
            self.assertFalse(TaskManager._signal(process, signal.SIGTERM))


if __name__ == "__main__":
    unittest.main()
