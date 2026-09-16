import os
import select
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from core.job_ledger import JobLedger, current_worker_id


class LedgerOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.ledger = JobLedger(self.directory.name + "/ledger.db")

    def tearDown(self):
        self.directory.cleanup()

    def test_foreign_owner_cannot_finalize_a_live_running_attempt(self):
        job = self.ledger.create_job("warm")
        attempt = self.ledger.start_attempt(job["job_id"], worker_id="worker-a")

        with self.assertRaisesRegex(ValueError, "ownership"):
            self.ledger.finalize_job(job["job_id"], state="failed", worker_id="worker-b")

        self.assertEqual(self.ledger.get_job(job["job_id"])["state"], "running")
        self.assertEqual(self.ledger.get_attempt(attempt["attempt_id"])["state"], "running")

    def test_foreign_owner_cannot_cancel_a_pending_retry(self):
        job = self.ledger.create_job("warm")
        attempt = self.ledger.start_attempt(job["job_id"], worker_id="worker-a")
        self.ledger.finish_attempt(
            attempt["attempt_id"], outcome="failed", error_code="timeout",
            retry_decision="retry", cooldown_seconds=0, worker_id="worker-a",
        )
        self.ledger.claim_due_retry(attempt["attempt_id"], worker_id="worker-a")

        with self.assertRaisesRegex(ValueError, "ownership"):
            self.ledger.finish_job(job["job_id"], state="cancelled", worker_id="worker-b")

        persisted = self.ledger.get_attempt(attempt["attempt_id"])
        self.assertEqual(persisted["retry_decision"], "pending")
        self.assertEqual(persisted["worker_id"], "worker-a")

    def test_matching_owner_can_finalize_and_stop_its_pending_retry(self):
        job = self.ledger.create_job("warm")
        attempt = self.ledger.start_attempt(job["job_id"], worker_id="worker-a")
        self.ledger.finish_attempt(
            attempt["attempt_id"], outcome="failed", error_code="timeout",
            retry_decision="retry", cooldown_seconds=0, worker_id="worker-a",
        )
        self.ledger.claim_due_retry(attempt["attempt_id"], worker_id="worker-a")

        finalized = self.ledger.finalize_job(
            job["job_id"], state="cancelled", worker_id="worker-a",
        )

        self.assertEqual(finalized["state"], "cancelled")
        self.assertEqual(
            self.ledger.get_attempt(attempt["attempt_id"])["retry_decision"], "stop"
        )

    def test_supervisor_requires_real_boolean_cleanup_observation(self):
        job = self.ledger.create_job("warm")
        self.ledger.start_attempt(job["job_id"], worker_id="abandoned-worker")

        for value in (None, 0, 1, "true"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "boolean"):
                self.ledger.supervisor_finalize_job(
                    job["job_id"], state="cancelled", cleanup_verified=value,
                )

    def test_unverified_supervisor_cannot_record_cancellation(self):
        job = self.ledger.create_job("warm")
        self.ledger.start_attempt(job["job_id"], worker_id="abandoned-worker")

        with self.assertRaisesRegex(ValueError, "unverified"):
            self.ledger.supervisor_finalize_job(
                job["job_id"], state="cancelled", cleanup_verified=False,
            )

        self.assertEqual(self.ledger.get_job(job["job_id"])["state"], "running")

    def test_verified_supervisor_can_cancel_an_abandoned_attempt(self):
        job = self.ledger.create_job("warm")
        attempt = self.ledger.start_attempt(job["job_id"], worker_id="abandoned-worker")

        finalized = self.ledger.supervisor_finalize_job(
            job["job_id"], state="cancelled", cleanup_verified=True,
        )

        self.assertEqual(finalized["state"], "cancelled")
        self.assertEqual(self.ledger.get_attempt(attempt["attempt_id"])["state"], "cancelled")

    def test_task_manager_uses_verified_supervisor_finalization(self):
        from pathlib import Path
        from web.tasks import TaskManager

        root = Path(self.directory.name)
        task_ledger = JobLedger(str(root / "data" / "database.db"))
        job = task_ledger.create_job("warm", job_id="task-supervisor")
        attempt = task_ledger.start_attempt(job["job_id"], worker_id="lost-worker")
        manager = object.__new__(TaskManager)
        manager.root = root

        manager._finalize_ledger(
            job["job_id"], "cancelled", "cancelled", cleanup_verified=True,
        )

        self.assertEqual(task_ledger.get_job(job["job_id"])["state"], "cancelled")
        self.assertEqual(task_ledger.get_attempt(attempt["attempt_id"])["state"], "cancelled")

    def test_finalizing_failed_job_does_not_rewrite_completed_subject_facts(self):
        job = self.ledger.create_job("create", requested_count=2)
        complete = self.ledger.start_attempt(
            job["job_id"], subject="complete@example.test", worker_id="worker-a",
        )
        self.ledger.finish_attempt(
            complete["attempt_id"], outcome="succeeded", worker_id="worker-a",
        )
        live = self.ledger.start_attempt(
            job["job_id"], subject="unfinished@example.test", worker_id="worker-a",
        )

        self.ledger.finalize_job(job["job_id"], state="failed", worker_id="worker-a")

        self.assertEqual(self.ledger.get_attempt(complete["attempt_id"])["state"], "succeeded")
        self.assertEqual(self.ledger.get_attempt(live["attempt_id"])["state"], "failed")

    def test_current_worker_id_is_stable_distinct_and_fork_aware(self):
        first = current_worker_id()
        self.assertEqual(first, current_worker_id())

        import core.job_ledger as job_ledger
        original_pid = os.getpid()
        with patch.object(job_ledger.os, "getpid", return_value=original_pid + 100000):
            fork_child = current_worker_id()
            self.assertEqual(fork_child, current_worker_id())

        self.assertNotEqual(first, fork_child)

    @unittest.skipUnless(hasattr(os, "fork"), "os.fork is unavailable")
    def test_current_worker_id_does_not_inherit_a_locked_identity_lock(self):
        """A forked child must not wait on a parent thread that no longer exists."""
        import core.job_ledger as job_ledger

        parent_id = current_worker_id()
        locked = threading.Event()
        release = threading.Event()

        def hold_identity_lock():
            with job_ledger._WORKER_ID_LOCK:
                locked.set()
                release.wait(5)

        holder = threading.Thread(target=hold_identity_lock, daemon=True)
        holder.start()
        self.assertTrue(locked.wait(1), "parent did not hold identity lock")
        read_fd, write_fd = os.pipe()
        child_pid = os.fork()
        if child_pid == 0:
            try:
                os.close(read_fd)
                os.write(write_fd, current_worker_id().encode("ascii"))
            finally:
                os._exit(0)

        os.close(write_fd)
        child_id = b""
        deadline = time.monotonic() + 2
        try:
            while time.monotonic() < deadline:
                readable, _, _ = select.select([read_fd], [], [], 0.05)
                if readable:
                    child_id = os.read(read_fd, 512)
                    break
            if not child_id:
                try:
                    os.kill(child_pid, 9)
                except ProcessLookupError:
                    pass
                self.fail("forked child blocked on inherited identity lock")
            os.waitpid(child_pid, 0)
        finally:
            release.set()
            holder.join(timeout=1)
            os.close(read_fd)

        self.assertNotEqual(parent_id, child_id.decode("ascii"))


if __name__ == "__main__":
    unittest.main()
