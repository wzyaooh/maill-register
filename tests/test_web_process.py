import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from web.configuration import Configuration
from web.tasks import ACTIVE, TaskManager, TaskStore


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
            except ProcessLookupError:
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
            time.sleep(0.1)
        else:
            self.fail("Orphan browser process group survived its cleanup watchdog")
        self.manager.store.update(task["id"], status="interrupted")

    def test_active_service_remains_visible_after_history_limit(self):
        voice = self.manager.store.create("voice", {})
        for _ in range(101):
            task = self.manager.store.create("validate", {})
            self.manager.store.update(task["id"], status="completed")
        self.assertIn(voice["id"], [task["id"] for task in self.manager.store.list()])
        self.assertEqual([voice["id"]], [task["id"] for task in self.manager.store.active()])
        self.manager.store.update(voice["id"], status="cancelled")


if __name__ == "__main__":
    unittest.main()
