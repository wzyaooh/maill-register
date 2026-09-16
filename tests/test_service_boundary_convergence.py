import sqlite3
import os
import tempfile
import unittest
import io
import sys
from pathlib import Path
from unittest.mock import Mock, patch

from core.database import DatabaseManager
from core.health_checker import AccountHealthChecker
from web.worker import execute
from core.job_ledger import JobLedger


class LegacyProfileServiceBoundaryTests(unittest.TestCase):
    def test_worker_does_not_dispatch_path_only_accounts_to_health_checker(self):
        manager = Mock()
        manager.get_all.return_value = [{
            "id": 1,
            "email": "legacy@example.test",
            "password": "legacy-password",
            "profile_path": "/old/profile",
        }]
        checker = Mock()
        checker.check_single.return_value = {
            "email": "legacy@example.test",
            "browser_status": "authenticated",
            "mailbox_status": "active",
            "status": "active",
        }
        with patch.dict("sys.modules", {
            "core.account_manager": type("Module", (), {"account_manager": manager}),
            "core.health_checker": type("Module", (), {"AccountHealthChecker": checker}),
        }):
            result = execute("health", {"account_ids": []}, Mock())

        checker.check_single.assert_called_once_with(
            "legacy@example.test", "legacy-password"
        )
        item = result["results"][0]
        self.assertEqual(item["browser_status"], "profile_unavailable")
        self.assertEqual(item["last_error_code"], "legacy_unbound")

    def test_worker_passes_only_profile_id_for_bound_health_accounts(self):
        manager = Mock()
        manager.get_all.return_value = [{
            "id": 1,
            "email": "bound@example.test",
            "password": "account-password",
            "profile_id": "profile-one",
            "profile_path": "/runtime/data/profiles/profile-one",
            "engine": "selenium",
            "proxy": "proxy.example.test:8443",
        }]
        checker = Mock()
        checker.check_single.return_value = {
            "email": "bound@example.test",
            "browser_status": "authenticated",
            "mailbox_status": "active",
            "status": "active",
        }
        with patch.dict("sys.modules", {
            "core.account_manager": type("Module", (), {"account_manager": manager}),
            "core.health_checker": type("Module", (), {"AccountHealthChecker": checker}),
        }):
            execute("health", {"account_ids": []}, Mock())

        self.assertEqual(checker.check_single.call_count, 1)
        args, kwargs = checker.check_single.call_args
        self.assertEqual(args, ("bound@example.test", "account-password"))
        self.assertEqual(kwargs, {
            "profile_id": "profile-one",
            "engine": "selenium",
            "proxy": "proxy.example.test:8443",
        })

    def test_health_checker_public_api_does_not_accept_path_only_identity(self):
        checker = AccountHealthChecker()
        with patch.object(checker, "_check_mailbox", return_value=("active", "IMAP ok", "")), \
             patch.object(checker.runtime, "resolve", side_effect=AssertionError("path was resolved")):
            with self.assertRaises(TypeError):
                checker.check_single(
                    "legacy@example.test", "account-password", profile_path="/old/profile"
                )


class LegacyDatabaseMigrationTests(unittest.TestCase):
    def test_migration_handles_minimal_old_schema_and_orders_accounts(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.db"
            with sqlite3.connect(path) as conn:
                conn.execute(
                    "CREATE TABLE accounts (id INTEGER PRIMARY KEY, email TEXT, password TEXT)"
                )
                conn.execute(
                    "INSERT INTO accounts(id, email, password) VALUES (1, ?, ?)",
                    ("old@example.test", "old-password"),
                )
                conn.commit()

            db = DatabaseManager(str(path))
            rows = db.get_all_accounts()

        self.assertEqual([row["email"] for row in rows], ["old@example.test"])
        self.assertIn("created_at", rows[0])

    def test_migration_scrubs_sensitive_legacy_notes_and_status(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.db"
            with sqlite3.connect(path) as conn:
                conn.execute(
                    """CREATE TABLE accounts (
                        id INTEGER PRIMARY KEY,
                        email TEXT,
                        password TEXT,
                        notes TEXT,
                        status TEXT,
                        last_error_code TEXT
                    )"""
                )
                conn.execute(
                    """INSERT INTO accounts
                       (id, email, password, notes, status, last_error_code)
                       VALUES (1, ?, ?, ?, ?, ?)""",
                    (
                        "old@example.test",
                        "old-password",
                        "password=old-password token=old-token",
                        "token=old-token",
                        "password=old-password",
                    ),
                )
                conn.commit()

            db = DatabaseManager(str(path))
            row = db.get_all_accounts()[0]

        self.assertNotIn("old-password", row["notes"])
        self.assertNotIn("old-token", row["notes"])
        self.assertEqual(row["status"], "error")
        self.assertEqual(row["last_error_code"], "")


class ExecuteLedgerBoundaryTests(unittest.TestCase):
    def _execute_with_failure(self, failure):
        with tempfile.TemporaryDirectory() as directory:
            db_path = str(Path(directory) / "database.db")
            with patch.dict(os.environ, {"LEDGER_DB_PATH": db_path}, clear=False), \
                 patch.dict("sys.modules", {
                     "core.creation_flow": type("Module", (), {
                         "run_creation_flow": Mock(side_effect=failure),
                     }),
                     "core.session_resume": type("Module", (), {
                         "session_manager": Mock(has_saved_session=Mock(return_value=False)),
                     }),
                     "config.settings": type("Module", (), {
                         "Config": type("Config", (), {
                             "FIVESIM_API_KEY": "", "SMS_ACTIVATE_API_KEY": "",
                             "ONLINESIM_API_KEY": "", "GETSMS_API_KEY": "",
                         }),
                     }),
                 }):
                with self.assertRaises(type(failure)):
                    execute(
                        "create",
                        {
                            "num_accounts": 1, "engine": "playwright", "parallel": False,
                            "warmup_minutes": 0, "flow_mode": "standard", "use_sms_api": False,
                            "max_threads": 1,
                        },
                        Mock(),
                    )
            ledger = JobLedger(db_path)
            jobs = ledger.list_jobs()
            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0]["state"], "cancelled" if isinstance(failure, KeyboardInterrupt) else "failed")
            attempts = ledger.list_attempts(jobs[0]["job_id"])
            self.assertFalse(any(item["state"] == "running" for item in attempts))

    def test_execute_exception_finalizes_job(self):
        self._execute_with_failure(RuntimeError("flow failed"))

    def test_execute_cancellation_finalizes_job(self):
        self._execute_with_failure(KeyboardInterrupt())


class WorkerExceptionBoundaryTests(unittest.TestCase):
    def test_worker_process_error_uses_stable_code_without_exception_payload(self):
        import web.worker as worker
        from web.tasks import TaskStore

        with tempfile.TemporaryDirectory() as directory:
            store = TaskStore(directory)
            task = store.create("validate", {})
            secret = "unlabelled-worker-boundary-secret-123"
            old_argv = sys.argv
            old_stdout, old_stderr = sys.stdout, sys.stderr
            try:
                sys.argv = ["worker", task["id"]]
                sys.stdout = io.StringIO()
                sys.stderr = io.StringIO()
                with patch.object(worker, "TaskStore", return_value=store), \
                     patch.object(worker, "execute", side_effect=RuntimeError(secret)), \
                     patch.dict(os.environ, {"WEB_TASK_DIRECTORY": directory}), \
                     self.assertRaises(SystemExit):
                    worker.main()
            finally:
                output = (sys.stdout.getvalue(), sys.stderr.getvalue())
                sys.argv = old_argv
                sys.stdout, sys.stderr = old_stdout, old_stderr

            persisted = store.get(task["id"])
            self.assertNotIn(secret, persisted["error"] or "")
            self.assertEqual(persisted["error"], "worker_error:RuntimeError")
            self.assertNotIn(secret, "".join(output))

    def test_worker_parent_transition_uses_conditional_task_update(self):
        """A lost-parent transition must not overwrite a terminal row."""
        import web.worker as worker
        from web.tasks import TaskStore

        with tempfile.TemporaryDirectory() as directory:
            store = TaskStore(directory)
            task = store.create("validate", {})

            def lose_claim(task_id, expected, **values):
                store.update(task_id, status="completed", finished_at="winner")
                return False

            with patch.object(store, "update_if_status", side_effect=lose_claim):
                changed = worker._update_task_if_status(
                    store, task["id"], ("running",), status="stopping"
                )
            self.assertFalse(changed)
            self.assertEqual(store.get(task["id"])["status"], "completed")


if __name__ == "__main__":
    unittest.main()
