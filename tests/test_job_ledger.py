import json
import importlib.util
import os
import sqlite3
import sys
import tempfile
import threading
import time
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from core.job_ledger import JobLedger
from core.retry_engine import CreationError, RetryEngine, RetryScheduler

ROOT = Path(__file__).resolve().parents[1]


class JobLedgerTests(unittest.TestCase):
    def test_retry_cooldown_persists_a_next_attempt_at_and_due_query(self):
        job = self.ledger.create_job("warm")
        attempt = self.ledger.start_attempt(job["job_id"], ordinal=1)
        finished = self.ledger.finish_attempt(
            attempt["attempt_id"], outcome="failed", error_code="timeout",
            retry_decision="retry", cooldown_seconds=30,
        )

        self.assertIsNotNone(finished["next_attempt_at"])
        due = self.ledger.list_due_attempts(
            now=(datetime.now(timezone.utc) + timedelta(seconds=31)).isoformat()
        )
        self.assertEqual([item["attempt_id"] for item in due], [attempt["attempt_id"]])
        not_due = self.ledger.list_due_attempts(now=datetime.now(timezone.utc).isoformat())
        self.assertEqual(not_due, [])

    def test_terminal_attempts_do_not_carry_a_retry_schedule(self):
        job = self.ledger.create_job("warm")
        attempt = self.ledger.start_attempt(job["job_id"], ordinal=1)
        finished = self.ledger.finish_attempt(
            attempt["attempt_id"], outcome="failed", error_code="locked",
            retry_decision="stop", cooldown_seconds=30,
        )
        self.assertIsNone(finished["next_attempt_at"])

    def test_due_retry_claim_is_atomic_and_idempotently_fenced(self):
        job = self.ledger.create_job("warm")
        attempt = self.ledger.start_attempt(job["job_id"], ordinal=1)
        self.ledger.finish_attempt(
            attempt["attempt_id"], outcome="failed", error_code="timeout",
            retry_decision="retry", cooldown_seconds=0,
        )
        claimed = self.ledger.claim_due_retry(
            attempt["attempt_id"], worker_id="retry-worker",
        )
        self.assertEqual(claimed["retry_decision"], "pending")
        self.assertEqual(claimed["worker_id"], "retry-worker")
        with self.assertRaisesRegex(ValueError, "not due|claimed"):
            self.ledger.claim_due_retry(attempt["attempt_id"])

    def test_retry_scheduler_reloads_persisted_schedule_after_ledger_restart(self):
        job = self.ledger.create_job("warm")
        attempt = self.ledger.start_attempt(job["job_id"], ordinal=1)
        finished = self.ledger.finish_attempt(
            attempt["attempt_id"], outcome="failed", error_code="timeout",
            retry_decision="retry", cooldown_seconds=0,
        )

        reopened = JobLedger(self.db_path)
        scheduler = RetryScheduler(reopened)
        due = scheduler.due()
        self.assertEqual([item["attempt_id"] for item in due], [finished["attempt_id"]])
        claimed = scheduler.claim(finished["attempt_id"])
        self.assertEqual(claimed["retry_decision"], "pending")

    def test_reopening_ledger_preserves_pending_retry_recovery_timestamp(self):
        job = self.ledger.create_job("warm")
        attempt = self.ledger.start_attempt(job["job_id"], worker_id="retry-owner")
        self.ledger.finish_attempt(
            attempt["attempt_id"], outcome="failed", error_code="timeout",
            retry_decision="retry", cooldown_seconds=0, worker_id="retry-owner",
        )
        claimed = self.ledger.claim_due_retry(
            attempt["attempt_id"], worker_id="retry-owner",
        )
        self.assertEqual(claimed["retry_decision"], "pending")
        self.assertIsNotNone(claimed["next_attempt_at"])

        reopened = JobLedger(self.db_path).get_attempt(attempt["attempt_id"])

        self.assertEqual(reopened["retry_decision"], "pending")
        self.assertEqual(reopened["next_attempt_at"], claimed["next_attempt_at"])

    def test_retry_scheduler_wait_can_be_cancelled_without_claiming_retry(self):
        job = self.ledger.create_job("warm")
        attempt = self.ledger.start_attempt(job["job_id"], ordinal=1)
        self.ledger.finish_attempt(
            attempt["attempt_id"], outcome="failed", error_code="timeout",
            retry_decision="retry", cooldown_seconds=30,
        )
        cancelled = threading.Event()
        cancelled.set()
        scheduler = RetryScheduler(self.ledger)

        self.assertFalse(
            scheduler.wait_until_due(
                attempt["attempt_id"], cancel_event=cancelled,
            )
        )
        self.assertEqual(
            self.ledger.get_attempt(attempt["attempt_id"])["retry_decision"],
            "retry",
        )

    def test_retry_scheduler_fails_closed_for_malformed_persisted_timestamp(self):
        job = self.ledger.create_job("warm")
        attempt = self.ledger.start_attempt(job["job_id"], ordinal=1)
        self.ledger.finish_attempt(
            attempt["attempt_id"], outcome="failed", error_code="timeout",
            retry_decision="retry", cooldown_seconds=0,
        )
        with self.ledger.connect() as conn:
            conn.execute(
                "UPDATE attempts SET next_attempt_at=? WHERE attempt_id=?",
                ("not-a-timestamp", attempt["attempt_id"]),
            )

        scheduler = RetryScheduler(self.ledger)
        self.assertEqual(scheduler.due(), [])
        self.assertFalse(scheduler.ledger.retry_is_due(attempt["attempt_id"]))
        with self.assertRaisesRegex(ValueError, "not due|invalid"):
            scheduler.claim(attempt["attempt_id"])
        self.assertFalse(
            scheduler.wait_until_due(attempt["attempt_id"], max_wait=0)
        )

    def test_retry_scheduler_claim_is_atomic_across_workers(self):
        job = self.ledger.create_job("warm")
        attempt = self.ledger.start_attempt(job["job_id"], ordinal=1)
        self.ledger.finish_attempt(
            attempt["attempt_id"], outcome="failed", error_code="timeout",
            retry_decision="retry", cooldown_seconds=0,
        )
        barrier = threading.Barrier(2)
        outcomes = []

        def claim():
            scheduler = RetryScheduler(JobLedger(self.db_path))
            barrier.wait()
            try:
                outcomes.append(scheduler.claim(attempt["attempt_id"])["retry_decision"])
            except ValueError:
                outcomes.append("rejected")

        workers = [threading.Thread(target=claim) for _ in range(2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=5)
        self.assertEqual(sorted(outcomes), ["pending", "rejected"])

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.db_path = self.directory.name + "/database.db"
        self.ledger = JobLedger(self.db_path)
        self.addCleanup(self.directory.cleanup)

    def test_job_and_attempt_lifecycle_survives_new_ledger_instance(self):
        job = self.ledger.create_job(
            "create", requested_count=2, requested_engine="playwright",
            metadata={"email": "user@example.test", "password": "do-not-store"},
        )
        attempt = self.ledger.start_attempt(
            job["job_id"], ordinal=1, account_id=7,
            subject="user@example.test", strategy="standard",
            engine="playwright", profile_id="profile-7",
            proxy="https://user:proxy-secret@example.test:8443",
            metadata={"otp": "246810", "safe": "value"},
        )
        self.assertEqual(attempt["state"], "running")
        self.ledger.finish_attempt(
            attempt["attempt_id"], outcome="failed", error_code="timeout",
            retry_decision="retry", cooldown_seconds=15,
            result={"message": "timeout", "token": "secret"},
        )

        reopened = JobLedger(self.db_path)
        loaded_job = reopened.get_job(job["job_id"])
        loaded_attempts = reopened.list_attempts(job["job_id"])
        self.assertEqual(loaded_job["requested_count"], 2)
        self.assertEqual(len(loaded_attempts), 1)
        self.assertEqual(loaded_attempts[0]["error_code"], "timeout")
        self.assertEqual(loaded_attempts[0]["retry_decision"], "retry")
        raw = sqlite3.connect(self.db_path).execute(
            "SELECT metadata, result_metadata FROM attempts"
        ).fetchone()
        self.assertNotIn("proxy-secret", json.dumps(raw))
        self.assertNotIn("246810", json.dumps(raw))
        self.assertNotIn("secret", json.dumps(raw))

    def test_claim_is_atomic_and_ordinal_is_unique(self):
        job = self.ledger.create_job("warm", requested_count=1)
        first = self.ledger.start_attempt(job["job_id"], ordinal=1)
        with self.assertRaises(ValueError):
            self.ledger.start_attempt(job["job_id"], ordinal=1)
        self.assertEqual(self.ledger.list_attempts(job["job_id"])[0]["attempt_id"], first["attempt_id"])

    def test_finish_job_records_terminal_summary(self):
        job = self.ledger.create_job("health")
        self.ledger.start_attempt(job["job_id"], ordinal=1)
        finished = self.ledger.finish_job(
            job["job_id"], state="failed", terminal_error_code="runtime_unavailable",
            summary={"failed": 1, "password": "must-not-store"},
        )
        self.assertEqual(finished["state"], "failed")
        self.assertEqual(finished["terminal_error_code"], "runtime_unavailable")
        self.assertNotIn("must-not-store", json.dumps(finished))

    def test_recover_stale_attempt_marks_job_interrupted(self):
        job = self.ledger.create_job("create")
        attempt = self.ledger.start_attempt(job["job_id"], ordinal=1)
        old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        with self.ledger.connect() as conn:
            conn.execute("UPDATE attempts SET started_at=?, heartbeat_at=? WHERE attempt_id=?",
                         (old, old, attempt["attempt_id"]))
            conn.execute("UPDATE jobs SET updated_at=? WHERE job_id=?", (old, job["job_id"]))
        recovered = self.ledger.recover_stale(max_age_seconds=60)
        self.assertEqual(recovered, 1)
        self.assertEqual(self.ledger.list_attempts(job["job_id"])[0]["state"], "interrupted")
        self.assertEqual(self.ledger.get_job(job["job_id"])["state"], "interrupted")

    def test_recover_stale_releases_a_pending_retry_claim_after_worker_loss(self):
        job = self.ledger.create_job("warm")
        attempt = self.ledger.start_attempt(job["job_id"], ordinal=1)
        finished = self.ledger.finish_attempt(
            attempt["attempt_id"], outcome="failed", error_code="timeout",
            retry_decision="retry", cooldown_seconds=0,
        )
        self.ledger.claim_due_retry(finished["attempt_id"])
        old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        with self.ledger.connect() as conn:
            conn.execute(
                "UPDATE attempts SET heartbeat_at=? WHERE attempt_id=?",
                (old, attempt["attempt_id"]),
            )

        recovered = self.ledger.recover_stale(max_age_seconds=60)
        self.assertEqual(recovered, 1)
        restored = self.ledger.get_attempt(attempt["attempt_id"])
        self.assertEqual(restored["retry_decision"], "retry")
        self.assertTrue(self.ledger.retry_is_due(attempt["attempt_id"]))

    def test_recover_stale_uses_supplied_clock_and_rejects_invalid_clock(self):
        job = self.ledger.create_job("warm")
        attempt = self.ledger.start_attempt(job["job_id"], ordinal=1)
        heartbeat = "2999-01-01T00:00:00+00:00"
        with self.ledger.connect() as conn:
            conn.execute(
                "UPDATE attempts SET heartbeat_at=?, started_at=? WHERE attempt_id=?",
                (heartbeat, heartbeat, attempt["attempt_id"]),
            )

        recovered = self.ledger.recover_stale(
            max_age_seconds=60, now="2999-01-01T00:02:00+00:00"
        )
        self.assertEqual(recovered, 1)
        self.assertEqual(
            self.ledger.get_attempt(attempt["attempt_id"])["finished_at"],
            "2999-01-01T00:02:00+00:00",
        )

        second_job = self.ledger.create_job("warm")
        second = self.ledger.start_attempt(second_job["job_id"], ordinal=1)
        with self.assertRaises(ValueError):
            self.ledger.recover_stale(now="not-a-timestamp")
        self.assertEqual(
            self.ledger.get_attempt(second["attempt_id"])["state"], "running"
        )

    def test_unknown_job_and_invalid_transition_are_rejected(self):
        with self.assertRaises(KeyError):
            self.ledger.start_attempt("missing")
        job = self.ledger.create_job("warm")
        attempt = self.ledger.start_attempt(job["job_id"])
        self.ledger.finish_attempt(attempt["attempt_id"], outcome="succeeded")
        with self.assertRaises(ValueError):
            self.ledger.finish_attempt(attempt["attempt_id"], outcome="failed")

    def test_attempt_finalize_is_fenced_by_explicit_worker_owner(self):
        job = self.ledger.create_job("warm")
        attempt = self.ledger.start_attempt(
            job["job_id"], worker_id="worker-a", strategy="warm"
        )
        with self.assertRaisesRegex(ValueError, "ownership"):
            self.ledger.finish_attempt(
                attempt["attempt_id"], outcome="succeeded", worker_id="worker-b"
            )
        self.assertEqual(
            self.ledger.get_attempt(attempt["attempt_id"])["state"], "running"
        )
        finished = self.ledger.finish_attempt(
            attempt["attempt_id"], outcome="succeeded", worker_id="worker-a"
        )
        self.assertEqual(finished["state"], "succeeded")

    def test_attempt_error_code_is_finite_and_never_contains_exception_payload(self):
        job = self.ledger.create_job("warm")
        attempt = self.ledger.start_attempt(job["job_id"])
        finished = self.ledger.finish_attempt(
            attempt["attempt_id"], outcome="failed",
            error_code="password=ledger-secret-739; provider=raw-payload",
        )

        self.assertEqual(finished["error_code"], "error")
        self.assertNotIn("ledger-secret-739", json.dumps(finished))
        raw = sqlite3.connect(self.db_path).execute(
            "SELECT error_code FROM attempts"
        ).fetchone()[0]
        self.assertEqual(raw, "error")

    def test_terminal_job_error_code_is_finite_and_legacy_rows_are_scrubbed(self):
        job = self.ledger.create_job("health")
        self.ledger.finalize_job(
            job["job_id"], state="failed",
            error_code="token=terminal-secret-740",
            summary={"message": "password=summary-secret-741"},
        )

        reopened = JobLedger(self.db_path)
        finished = reopened.get_job(job["job_id"])
        self.assertEqual(finished["terminal_error_code"], "error")
        self.assertNotIn("terminal-secret-740", json.dumps(finished))
        self.assertNotIn("summary-secret-741", json.dumps(finished))

        # Simulate a pre-existing ledger row written before the finite-code
        # boundary existed; constructing a new ledger must scrub it.
        legacy_job = reopened.create_job("warm", job_id="job-legacy-error")
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE jobs SET terminal_error_code=? WHERE job_id=?",
                ("password=legacy-secret-742", legacy_job["job_id"]),
            )
        migrated = JobLedger(self.db_path).get_job(legacy_job["job_id"])
        self.assertEqual(migrated["terminal_error_code"], "error")
        self.assertNotIn("legacy-secret-742", json.dumps(migrated))

    def test_finalizing_cancelled_job_closes_running_attempts_atomically(self):
        job = self.ledger.create_job("warm", requested_count=2)
        first = self.ledger.start_attempt(job["job_id"], ordinal=1)
        second = self.ledger.start_attempt(job["job_id"], ordinal=2)
        finalized = self.ledger.finalize_job(
            job["job_id"], state="cancelled", error_code="cancelled",
        )
        self.assertEqual(finalized["state"], "cancelled")
        attempts = self.ledger.list_attempts(job["job_id"])
        self.assertEqual([item["state"] for item in attempts], ["cancelled", "cancelled"])
        self.assertTrue(all(item["retry_decision"] == "stop" for item in attempts))
        repeated = self.ledger.finalize_job(
            job["job_id"], state="cancelled", error_code="cancelled",
        )
        self.assertEqual(repeated["state"], "cancelled")

    def test_finalizing_terminal_job_clears_scheduled_retries(self):
        job = self.ledger.create_job("warm")
        attempt = self.ledger.start_attempt(job["job_id"], ordinal=1)
        self.ledger.finish_attempt(
            attempt["attempt_id"], outcome="failed", error_code="timeout",
            retry_decision="retry", cooldown_seconds=30,
        )

        self.ledger.finalize_job(job["job_id"], state="cancelled")
        persisted = self.ledger.get_attempt(attempt["attempt_id"])
        self.assertEqual(persisted["retry_decision"], "stop")
        self.assertIsNone(persisted["next_attempt_at"])
        self.assertEqual(self.ledger.list_due_attempts(), [])

    def test_interrupted_job_closes_running_attempt_but_remains_resumable(self):
        job = self.ledger.create_job("create", requested_count=1)
        self.ledger.start_attempt(job["job_id"], ordinal=1, account_id=0)
        interrupted = self.ledger.finalize_job(
            job["job_id"], state="interrupted", error_code="worker_lost",
        )
        self.assertEqual(interrupted["state"], "interrupted")
        self.assertEqual(self.ledger.list_attempts(job["job_id"])[0]["state"], "interrupted")
        retry = self.ledger.start_attempt(job["job_id"], ordinal=2, account_id=0)
        self.assertEqual(retry["state"], "running")

    def test_legacy_jobs_and_attempts_tables_are_migrated_additively(self):
        path = self.directory.name + "/legacy.db"
        with sqlite3.connect(path) as conn:
            conn.execute(
                "CREATE TABLE jobs (job_id TEXT PRIMARY KEY, kind TEXT NOT NULL, "
                "state TEXT NOT NULL, metadata TEXT, summary TEXT, "
                "created_at TEXT, updated_at TEXT)"
            )
            conn.execute(
                "CREATE TABLE attempts (attempt_id TEXT PRIMARY KEY, job_id TEXT, "
                "ordinal INTEGER, state TEXT, started_at TEXT, heartbeat_at TEXT, "
                "metadata TEXT, result_metadata TEXT, error_code TEXT)"
            )
            conn.execute(
                "INSERT INTO jobs(job_id, kind, state, created_at, updated_at) "
                "VALUES ('job-old', 'warm', 'pending', 'old', 'old')"
            )
            conn.execute(
                "UPDATE jobs SET metadata=?, summary=? WHERE job_id='job-old'",
                ('{"password":"old-secret","safe":"kept"}',
                 '{"token":"old-token","count":1}'),
            )
            conn.commit()

        migrated = JobLedger(path)
        job = migrated.get_job("job-old")
        self.assertEqual(job["requested_count"], 0)
        self.assertEqual(job["retry_policy"], {})
        attempt = migrated.start_attempt("job-old", ordinal=1, subject="old@example.test")
        self.assertEqual(attempt["state"], "running")

        columns = {
            row[1] for row in sqlite3.connect(path).execute("PRAGMA table_info(jobs)")
        }
        self.assertIn("summary", columns)
        self.assertIn("finished_at", columns)
        raw = sqlite3.connect(path).execute(
            "SELECT metadata, summary FROM jobs WHERE job_id='job-old'"
        ).fetchone()
        self.assertNotIn("old-secret", json.dumps(raw))
        self.assertNotIn("old-token", json.dumps(raw))

    def test_legacy_non_numeric_ordinal_is_rewritten_during_migration(self):
        path = self.directory.name + "/legacy-ordinal.db"
        with sqlite3.connect(path) as conn:
            conn.execute(
                "CREATE TABLE jobs (job_id TEXT PRIMARY KEY, kind TEXT, state TEXT, "
                "created_at TEXT, updated_at TEXT)"
            )
            conn.execute(
                "CREATE TABLE attempts (attempt_id TEXT PRIMARY KEY, job_id TEXT, "
                "ordinal TEXT, state TEXT, started_at TEXT, heartbeat_at TEXT)"
            )
            conn.execute(
                "INSERT INTO jobs(job_id, kind, state) VALUES ('job-ordinal', 'warm', 'pending')"
            )
            conn.execute(
                "INSERT INTO attempts(attempt_id, job_id, ordinal, state) "
                "VALUES ('attempt-ordinal', 'job-ordinal', 'not-a-number', 'running')"
            )

        migrated = JobLedger(path)
        self.assertEqual(migrated.get_attempt("attempt-ordinal")["ordinal"], 1)
        raw = sqlite3.connect(path).execute(
            "SELECT ordinal FROM attempts WHERE attempt_id='attempt-ordinal'"
        ).fetchone()[0]
        self.assertEqual(raw, 1)

    def test_without_rowid_legacy_tables_are_migrated_without_startup_failure(self):
        path = self.directory.name + "/without-rowid.db"
        with sqlite3.connect(path) as conn:
            conn.execute(
                "CREATE TABLE jobs (job_id TEXT PRIMARY KEY, kind TEXT) WITHOUT ROWID"
            )
            conn.execute(
                "CREATE TABLE attempts (attempt_id TEXT PRIMARY KEY, job_id TEXT, ordinal INTEGER) WITHOUT ROWID"
            )
            conn.execute("INSERT INTO jobs(job_id, kind) VALUES ('job-legacy', 'warm')")
            conn.execute(
                "INSERT INTO attempts(attempt_id, job_id, ordinal) "
                "VALUES ('attempt-legacy', 'job-legacy', 1)"
            )

        ledger = JobLedger(path)
        self.assertEqual(ledger.get_job("job-legacy")["kind"], "warm")
        self.assertEqual(ledger.get_attempt("attempt-legacy")["ordinal"], 1)

    def test_start_attempt_rejects_control_and_overlong_identity_fields(self):
        job = self.ledger.create_job("warm")
        for field, value in (
            ("subject", "x" * 257),
            ("strategy", "standard\nforged"),
            ("engine", "x" * 257),
            ("profile_id", "x" * 257),
            ("worker_id", "x" * 257),
        ):
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.ledger.start_attempt(job["job_id"], **{field: value})

    def test_finish_attempt_rejects_state_outcome_conflicts_and_defaults_failed_code(self):
        job = self.ledger.create_job("warm")
        attempt = self.ledger.start_attempt(job["job_id"])
        with self.assertRaises(ValueError):
            self.ledger.finish_attempt(
                attempt["attempt_id"], outcome="succeeded", state="failed"
            )
        finished = self.ledger.finish_attempt(
            attempt["attempt_id"], outcome="failed"
        )
        self.assertEqual(finished["error_code"], "error")

    def test_ledger_numeric_parameters_reject_implicit_coercion(self):
        with self.assertRaises(ValueError):
            self.ledger.create_job("warm", requested_count=1.5)
        with self.assertRaises(ValueError):
            self.ledger.list_jobs(limit="1")
        job = self.ledger.create_job("warm")
        with self.assertRaises(ValueError):
            self.ledger.start_attempt(job["job_id"], ordinal=1.5)
        attempt = self.ledger.start_attempt(job["job_id"], ordinal=1)
        with self.assertRaises(ValueError):
            self.ledger.finish_attempt(attempt["attempt_id"], outcome="failed", cooldown_seconds="1")
        with self.assertRaises(ValueError):
            self.ledger.recover_stale(max_age_seconds=1.5)

    def test_repeating_the_same_attempt_finalize_is_idempotent(self):
        job = self.ledger.create_job("warm")
        attempt = self.ledger.start_attempt(job["job_id"])
        first = self.ledger.finish_attempt(
            attempt["attempt_id"], outcome="failed", error_code="timeout",
            retry_decision="retry", cooldown_seconds=1,
        )
        repeated = self.ledger.finish_attempt(
            attempt["attempt_id"], outcome="failed", error_code="timeout",
            retry_decision="retry", cooldown_seconds=1,
        )
        self.assertEqual(repeated["attempt_id"], first["attempt_id"])
        self.assertEqual(repeated["finished_at"], first["finished_at"])

    def test_conflicting_repeated_job_finalize_is_rejected(self):
        job = self.ledger.create_job("warm")
        first = self.ledger.finalize_job(
            job["job_id"], state="failed", error_code="timeout",
            summary={"failed": 1},
        )
        self.assertEqual(first["state"], "failed")
        with self.assertRaisesRegex(ValueError, "already finalized"):
            self.ledger.finalize_job(
                job["job_id"], state="failed", error_code="provider_error",
                summary={"failed": 2},
            )

    def test_lookup_and_mutation_apis_reject_non_string_identifiers(self):
        job = self.ledger.create_job("warm")
        attempt = self.ledger.start_attempt(job["job_id"])
        for operation, call in (
            ("get_job", lambda: self.ledger.get_job(1)),
            ("get_attempt", lambda: self.ledger.get_attempt(1)),
            ("list_attempts", lambda: self.ledger.list_attempts(1)),
            ("count_attempts", lambda: self.ledger.count_attempts(1)),
            ("heartbeat_attempt", lambda: self.ledger.heartbeat_attempt(1)),
            ("finalize_job", lambda: self.ledger.finalize_job(1, state="failed")),
            ("finish_attempt", lambda: self.ledger.finish_attempt(1, outcome="failed")),
            ("stats", lambda: self.ledger.stats(1)),
        ):
            with self.subTest(operation=operation), self.assertRaises(ValueError):
                call()
        self.assertEqual(self.ledger.get_attempt(attempt["attempt_id"])["state"], "running")

    def test_secret_strategy_is_reduced_before_ledger_persistence(self):
        job = self.ledger.create_job("warm")
        attempt = self.ledger.start_attempt(
            job["job_id"], strategy="password=ledger-strategy-secret",
        )
        self.assertNotIn("ledger-strategy-secret", json.dumps(attempt))
        raw = sqlite3.connect(self.db_path).execute(
            "SELECT strategy FROM attempts WHERE attempt_id=?", (attempt["attempt_id"],)
        ).fetchone()[0]
        self.assertNotIn("ledger-strategy-secret", raw)

    def test_same_job_id_with_conflicting_configuration_is_rejected(self):
        from unittest.mock import patch
        from web.worker import _new_ledger

        path = self.directory.name + "/worker.db"
        with patch.dict(os.environ, {"LEDGER_DB_PATH": path}):
            ledger = JobLedger(path)
            ledger.create_job(
                "warm", requested_count=1, requested_engine="playwright",
                job_id="job-config-conflict",
            )
            with self.assertRaisesRegex(ValueError, "configuration"):
                _new_ledger(
                    "warm",
                    {"num_accounts": 2, "engine": "selenium"},
                    "job-config-conflict",
                )

    def test_worker_uses_retry_engine_policy_for_each_operation(self):
        from web.worker import _retryable_operation_errors

        for operation, expected in RetryEngine.OPERATION_RETRYABLE_ERRORS.items():
            self.assertEqual(
                _retryable_operation_errors(operation), frozenset(expected)
            )

    def test_system_exit_closes_a_running_operation_attempt(self):
        from web.worker import _run_retryable_operation

        with tempfile.TemporaryDirectory() as directory:
            ledger = JobLedger(directory + "/database.db")
            job = ledger.create_job("warm")

            def operation():
                raise SystemExit("provider payload must not persist")

            with self.assertRaises(SystemExit):
                _run_retryable_operation(
                    "warm", operation, ledger=ledger, job_id=job["job_id"],
                    subject="exit@example.test",
                )
            attempts = ledger.list_attempts(job["job_id"])
            self.assertEqual(len(attempts), 1)
            self.assertNotEqual(attempts[0]["state"], "running")


class RetryEngineLedgerTests(unittest.TestCase):
    def test_retry_engine_claims_only_after_durable_schedule_is_due(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = JobLedger(directory + "/database.db")
            job = ledger.create_job("warm")
            engine = RetryEngine(ledger=ledger, job_id=job["job_id"])
            with patch.object(RetryEngine, "COOLDOWN_BASE", 0):
                recorded = engine.record_attempt(
                    "warm", False, "timeout", operation="warm",
                )
            self.assertEqual(recorded["retry_decision"], "retry")
            claimed = engine.wait_for_retry(recorded)
            self.assertEqual(claimed["retry_decision"], "pending")

    def test_retry_engine_persists_operation_cooldown_cap(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = JobLedger(directory + "/database.db")
            job = ledger.create_job("compensation")
            engine = RetryEngine(ledger=ledger, job_id=job["job_id"])
            with patch.object(RetryEngine, "COOLDOWN_BASE", 30):
                recorded = engine.record_attempt(
                    "compensation", False, "provider_timeout",
                    operation="compensation", cooldown_cap=0,
                )
            self.assertEqual(recorded["retry_decision"], "retry")
            self.assertEqual(recorded["cooldown_seconds"], 0)
            self.assertIsNotNone(recorded["next_attempt_at"])

    def test_retry_engine_reclaims_retry_after_a_new_process_instance(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = JobLedger(directory + "/database.db")
            job = ledger.create_job("warm")
            first = RetryEngine(
                ledger=ledger, job_id=job["job_id"], worker_id="worker-a",
            )
            with patch.object(RetryEngine, "COOLDOWN_BASE", 0):
                recorded = first.record_attempt(
                    "warm", False, "timeout", operation="warm",
                    subject="restart@example.test", account_id=4,
                )

            restarted = RetryEngine(
                ledger=JobLedger(directory + "/database.db"),
                job_id=job["job_id"], worker_id="worker-b",
            )
            claimed = restarted.resume_existing_retry(
                subject="restart@example.test", account_id=4,
            )
            self.assertEqual(claimed["attempt_id"], recorded["attempt_id"])
            self.assertEqual(claimed["retry_decision"], "pending")

    def test_retry_engine_never_persists_provider_payload_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = directory + "/database.db"
            ledger = JobLedger(db_path)
            job = ledger.create_job("warm")
            engine = RetryEngine(ledger=ledger, job_id=job["job_id"])
            engine.record_attempt(
                "warm", True, operation="warm",
                metadata={
                    "status": "authenticated",
                    "provider_payload": {"opaque": "raw-provider-secret-991"},
                },
            )
            raw = sqlite3.connect(db_path).execute(
                "SELECT result_metadata FROM attempts"
            ).fetchone()[0]
            self.assertNotIn("provider_payload", raw)
            self.assertNotIn("raw-provider-secret-991", raw)

    def test_worker_operation_claims_a_persisted_retry_before_new_attempt(self):
        from web.worker import _run_retryable_operation

        with tempfile.TemporaryDirectory() as directory:
            ledger = JobLedger(directory + "/database.db")
            job = ledger.create_job("warm")
            previous = RetryEngine(
                ledger=ledger, job_id=job["job_id"], worker_id="worker-a",
            )
            with patch.object(RetryEngine, "COOLDOWN_BASE", 0):
                previous.record_attempt(
                    "warm", False, "timeout", operation="warm",
                    subject="restart-operation@example.test", account_id=4,
                )

            calls = []

            def operation():
                calls.append(True)
                return {
                    "success": True,
                    "browser_status": "authenticated",
                    "cleanup_status": "completed",
                    "browser_process_stopped": True,
                    "lease_released": True,
                }

            result = _run_retryable_operation(
                "warm", operation, ledger=JobLedger(directory + "/database.db"),
                job_id=job["job_id"], subject="restart-operation@example.test",
                account={"id": 4},
            )
            self.assertTrue(result["success"])
            self.assertEqual(calls, [True])
            attempts = ledger.list_attempts(job["job_id"])
            self.assertEqual(attempts[0]["retry_decision"], "pending")
            self.assertEqual(attempts[1]["state"], "succeeded")

    def test_worker_operation_does_not_rerun_a_completed_subject_after_restart(self):
        from web.worker import _run_retryable_operation

        with tempfile.TemporaryDirectory() as directory:
            ledger = JobLedger(directory + "/database.db")
            job = ledger.create_job("warm")
            previous = RetryEngine(
                ledger=ledger, job_id=job["job_id"], worker_id="worker-a",
            )
            attempt = previous.begin_attempt(
                "warm", subject="already-done@example.test", account_id=8,
            )
            previous.record_attempt(
                "warm", True, attempt_id=attempt["attempt_id"],
                operation="warm", subject="already-done@example.test", account_id=8,
                metadata={"status": "authenticated"},
            )
            operation = Mock(side_effect=AssertionError("completed subject rerun"))

            result = _run_retryable_operation(
                "warm", operation, ledger=JobLedger(directory + "/database.db"),
                job_id=job["job_id"], subject="already-done@example.test",
                account={"id": 8},
            )
            self.assertTrue(result["success"])
            operation.assert_not_called()
            self.assertEqual(len(ledger.list_attempts(job["job_id"])), 1)

    def test_retry_engine_heartbeat_updates_owned_attempt_and_stops(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = JobLedger(directory + "/database.db")
            job = ledger.create_job("create")
            engine = RetryEngine(
                ledger=ledger, job_id=job["job_id"], worker_id="heartbeat-worker"
            )
            attempt = engine.begin_attempt("standard")
            seen = threading.Event()
            original = ledger.heartbeat_attempt

            def heartbeat(attempt_id, *, worker_id=None):
                result = original(attempt_id, worker_id=worker_id)
                seen.set()
                return result

            with patch.object(ledger, "heartbeat_attempt", side_effect=heartbeat):
                handle = engine.start_heartbeat(attempt, interval=0.01)
                self.assertTrue(seen.wait(2))
                engine.stop_heartbeat(handle)
                calls_after_stop = seen.is_set()

            self.assertTrue(calls_after_stop)
            self.assertEqual(
                ledger.get_attempt(attempt["attempt_id"])["state"], "running"
            )

    def test_retry_budget_constant_is_the_total_attempt_limit(self):
        engine = RetryEngine()
        self.assertEqual(engine.MAX_RETRIES, 3)
        self.assertFalse(engine.should_retry(CreationError.TIMEOUT, engine.MAX_RETRIES))
        self.assertTrue(engine.should_retry(CreationError.TIMEOUT, engine.MAX_RETRIES - 1))
        with self.assertRaises(ValueError):
            engine.should_retry(CreationError.TIMEOUT, -1)

    def test_cooldown_is_bounded_even_when_policy_base_is_misconfigured(self):
        engine = RetryEngine()
        with patch.object(RetryEngine, "COOLDOWN_BASE", 10 ** 9), \
             patch("core.retry_engine.random.uniform", return_value=2.0):
            cooldown = engine.get_cooldown(3, CreationError.TIMEOUT)
        self.assertLessEqual(cooldown, RetryEngine.MAX_COOLDOWN_SECONDS)

    def test_retry_counts_reject_implicit_string_coercion(self):
        engine = RetryEngine()
        with self.assertRaises(ValueError):
            engine.should_retry(CreationError.TIMEOUT, "1")
        with self.assertRaises(ValueError):
            engine.get_cooldown("1", CreationError.TIMEOUT)

    def test_operation_attempt_accepts_non_creation_strategy_name(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = JobLedger(directory + "/database.db")
            job = ledger.create_job("warm", requested_count=1)
            engine = RetryEngine(ledger=ledger, job_id=job["job_id"])

            recorded = engine.record_attempt(
                "warm", False, "runtime_unavailable", operation="warm",
                subject="warm@example.test",
            )

            self.assertEqual(recorded["retry_decision"], "retry")
            self.assertEqual(ledger.list_attempts(job["job_id"])[0]["strategy"], "warm")

    def test_record_attempt_persists_retry_decision_and_strategy(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = JobLedger(directory + "/database.db")
            job = ledger.create_job("create", requested_count=1)
            engine = RetryEngine(ledger=ledger, job_id=job["job_id"])
            engine.record_attempt("standard", False, CreationError.TIMEOUT)
            attempts = ledger.list_attempts(job["job_id"])
            self.assertEqual(len(attempts), 1)
            self.assertEqual(attempts[0]["strategy"], "standard")
            self.assertEqual(attempts[0]["error_code"], CreationError.TIMEOUT)
            self.assertEqual(attempts[0]["retry_decision"], "retry")

    def test_successful_retry_is_recorded_as_a_second_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = JobLedger(directory + "/database.db")
            job = ledger.create_job("create", requested_count=1)
            engine = RetryEngine(ledger=ledger, job_id=job["job_id"])
            engine.record_attempt("standard", False, CreationError.CAPTCHA)
            engine.record_attempt("youtube", True)
            attempts = ledger.list_attempts(job["job_id"])
            self.assertEqual([a["ordinal"] for a in attempts], [1, 2])
            self.assertEqual(attempts[-1]["state"], "succeeded")
            self.assertEqual(attempts[-1]["retry_decision"], "stop")

    def test_retry_budget_is_counted_per_account_not_across_the_whole_job(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = JobLedger(directory + "/database.db")
            job = ledger.create_job("create", requested_count=2)
            engine = RetryEngine(ledger=ledger, job_id=job["job_id"])
            for _ in range(2):
                engine.record_attempt(
                    "standard", False, CreationError.TIMEOUT,
                    account_id=0, subject="first@example.test",
                )
            second = engine.record_attempt(
                "standard", False, CreationError.TIMEOUT,
                account_id=1, subject="second@example.test",
            )
            self.assertEqual(second["retry_decision"], "retry")

    def test_retry_engine_normalizes_unknown_error_before_memory_and_ledger(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = JobLedger(directory + "/database.db")
            job = ledger.create_job("warm")
            engine = RetryEngine(ledger=ledger, job_id=job["job_id"])
            engine.record_attempt(
                "warm", False, "authorization_header=retry-secret-743",
                operation="warm", subject="warm@example.test",
            )

            attempt = ledger.list_attempts(job["job_id"])[0]
            self.assertEqual(attempt["error_code"], "error")
            self.assertNotIn("retry-secret-743", json.dumps(engine.get_stats()))

    def test_record_attempt_requires_boolean_success(self):
        engine = RetryEngine()
        with self.assertRaises(ValueError):
            engine.record_attempt("standard", "false", CreationError.TIMEOUT)
        self.assertEqual(engine.get_stats()["total_attempts"], 0)

    def test_record_attempt_rejects_unknown_operation_without_mutating_memory_or_ledger(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = JobLedger(directory + "/database.db")
            job = ledger.create_job("warm")
            engine = RetryEngine(ledger=ledger, job_id=job["job_id"])
            with self.assertRaises(ValueError):
                engine.record_attempt(
                    "warm", False, "runtime_unavailable", operation="unknown",
                )
            self.assertEqual(engine.get_stats()["total_attempts"], 0)
            self.assertEqual(ledger.list_attempts(job["job_id"]), [])

    def test_record_attempt_missing_attempt_does_not_mutate_memory(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = JobLedger(directory + "/database.db")
            job = ledger.create_job("warm")
            engine = RetryEngine(ledger=ledger, job_id=job["job_id"])
            with self.assertRaises(KeyError):
                engine.record_attempt(
                    "warm", False, "runtime_unavailable",
                    attempt_id="missing-attempt", operation="warm",
                )
            self.assertEqual(engine.get_stats()["total_attempts"], 0)
            self.assertEqual(ledger.list_attempts(job["job_id"]), [])

    def test_record_attempt_terminal_job_does_not_mutate_memory(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = JobLedger(directory + "/database.db")
            job = ledger.create_job("warm")
            ledger.finalize_job(job["job_id"], state="cancelled")
            engine = RetryEngine(ledger=ledger, job_id=job["job_id"])
            with self.assertRaises(ValueError):
                engine.record_attempt("warm", False, "runtime_unavailable", operation="warm")
            self.assertEqual(engine.get_stats()["total_attempts"], 0)
            self.assertEqual(ledger.list_attempts(job["job_id"]), [])


class WorkerLedgerIntegrationTests(unittest.TestCase):
    def test_creation_attempt_heartbeat_is_started_and_stopped(self):
        from core.batch_runner import _create_single_account
        from core.retry_engine import RetryEngine

        with tempfile.TemporaryDirectory() as directory:
            ledger = JobLedger(directory + "/database.db")
            job = ledger.create_job("create", requested_count=1)
            heartbeat = Mock()
            runner = Mock(return_value={"success": True})
            proxy = Mock(get_best=Mock(return_value=None), get_next=Mock(return_value=None))
            config = types.SimpleNamespace(ENGINE_MODE="playwright")
            with patch("core.batch_runner._generate_username", return_value=(
                "heartbeatcreate", "Heartbeat", "Create"
            )), patch("core.batch_runner.proxy_manager", proxy), \
                 patch("core.batch_runner.Config", config), \
                 patch.dict(sys.modules, {
                     "core.runners": types.SimpleNamespace(
                         run_playwright_flow=runner,
                     ),
                 }), \
                 patch.object(RetryEngine, "start_heartbeat", return_value=heartbeat) as start, \
                 patch.object(RetryEngine, "stop_heartbeat") as stop:
                result = _create_single_account(
                    0, 1, "playwright", "pw", 0, "standard", False,
                    ledger=ledger, job_id=job["job_id"], ordinal=1,
                )

            self.assertTrue(result["success"])
            start.assert_called_once()
            stop.assert_called_once_with(heartbeat)

    def test_retryable_operation_heartbeats_until_operation_finishes(self):
        from web.worker import _run_retryable_operation

        with tempfile.TemporaryDirectory() as directory:
            ledger = JobLedger(directory + "/database.db")
            job = ledger.create_job("warm")
            heartbeat_seen = threading.Event()
            calls = []
            original_heartbeat = ledger.heartbeat_attempt

            def heartbeat(attempt_id, *, worker_id=None):
                calls.append((attempt_id, worker_id))
                heartbeat_seen.set()
                return original_heartbeat(attempt_id, worker_id=worker_id)

            with patch.object(ledger, "heartbeat_attempt", side_effect=heartbeat):
                def operation():
                    self.assertTrue(heartbeat_seen.wait(2), "operation was never heartbeated")
                    return {
                        "success": True,
                        "browser_status": "authenticated",
                        "cleanup_status": "completed",
                        "browser_process_stopped": True,
                        "lease_released": True,
                    }

                result = _run_retryable_operation(
                    "warm", operation, ledger=ledger, job_id=job["job_id"],
                    subject="heartbeat@example.test", heartbeat_interval=0.01,
                )

            self.assertTrue(result["success"])
            self.assertTrue(calls)
            attempt = ledger.list_attempts(job["job_id"])[0]
            self.assertEqual(attempt["state"], "succeeded")
            from core.job_ledger import current_worker_id
            self.assertEqual(attempt["worker_id"], current_worker_id())

    def test_heartbeat_failure_does_not_replace_operation_result(self):
        from web.worker import _run_retryable_operation

        with tempfile.TemporaryDirectory() as directory:
            ledger = JobLedger(directory + "/database.db")
            job = ledger.create_job("warm")
            heartbeat_seen = threading.Event()

            def heartbeat(_attempt_id, *, worker_id=None):
                heartbeat_seen.set()
                return False

            with patch.object(ledger, "heartbeat_attempt", side_effect=heartbeat):
                def operation():
                    self.assertTrue(heartbeat_seen.wait(2), "heartbeat failure was never observed")
                    return {
                        "success": True,
                        "browser_status": "authenticated",
                        "cleanup_status": "completed",
                        "browser_process_stopped": True,
                        "lease_released": True,
                    }

                result = _run_retryable_operation(
                    "warm", operation, ledger=ledger, job_id=job["job_id"],
                    subject="heartbeat-failure@example.test", heartbeat_interval=0.01,
                )

            self.assertTrue(result["success"])
            self.assertEqual(ledger.list_attempts(job["job_id"])[0]["state"], "succeeded")

    def test_worker_startup_recovers_stale_attempt_before_operation(self):
        from web import worker
        from web.tasks import TaskStore

        with tempfile.TemporaryDirectory() as directory:
            task_store = TaskStore(directory + "/web")
            task = task_store.create("warm", {})
            ledger_path = directory + "/database.db"
            ledger = JobLedger(ledger_path)
            job = ledger.create_job("warm", job_id=task["id"])
            attempt = ledger.start_attempt(job["job_id"], worker_id="old-worker")
            old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
            with ledger.connect() as conn:
                conn.execute(
                    "UPDATE attempts SET started_at=?, heartbeat_at=? WHERE attempt_id=?",
                    (old, old, attempt["attempt_id"]),
                )

            observed = []

            def execute_after_recovery(_action, _params, _report, job_id=None):
                observed.append(ledger.get_attempt(attempt["attempt_id"])["state"])
                return {"success": True}

            old_argv = sys.argv
            old_stdout, old_stderr = sys.stdout, sys.stderr
            try:
                sys.argv = ["worker", task["id"]]
                sys.stdout = types.SimpleNamespace(write=lambda value: len(value), flush=lambda: None)
                sys.stderr = types.SimpleNamespace(write=lambda value: len(value), flush=lambda: None)
                with patch.object(worker, "TaskStore", return_value=task_store), \
                     patch.object(worker, "_invoke_execute", side_effect=execute_after_recovery), \
                     patch.object(worker, "reconcile_sms_orders_once", return_value={
                         "claimed": 0, "cancelled": 0, "completed": 0, "failed": 0,
                     }, create=True), \
                     patch.dict(os.environ, {
                         "WEB_TASK_DIRECTORY": directory + "/web",
                         "LEDGER_DB_PATH": ledger_path,
                         "WEB_PARENT_PID": str(os.getpid()),
                     }, clear=False):
                    worker.main()
            finally:
                sys.argv = old_argv
                sys.stdout, sys.stderr = old_stdout, old_stderr

            self.assertEqual(observed, ["interrupted"])

    def test_worker_rejects_malformed_compensation_counters(self):
        from web.worker import _operation_result_outcome

        malformed_values = (True, False, -1, 1.5, "1", float("inf"), None)
        for value in malformed_values:
            with self.subTest(value=value):
                success, error_code = _operation_result_outcome(
                    "compensation", {"failed": value}
                )
                self.assertFalse(success)
                self.assertEqual(error_code, "invalid_reconciliation_result")

    def test_worker_rejects_warm_success_without_complete_reconciliation(self):
        from web.worker import _operation_result_outcome

        malformed = {
            "success": True,
            "cleanup_status": "completed",
            "browser_process_stopped": False,
            "lease_released": False,
        }
        success, error_code = _operation_result_outcome("warm", malformed)
        self.assertFalse(success)
        self.assertEqual(error_code, "cleanup_failed")

    def test_worker_does_not_count_malformed_warm_success_as_job_success(self):
        from unittest.mock import Mock, patch
        from web.worker import execute

        with tempfile.TemporaryDirectory() as directory:
            db_path = directory + "/database.db"
            manager = Mock()
            manager.get_all.return_value = [{
                "id": 21, "email": "malformed@example.test", "password": "pw",
                "profile_id": "profile-malformed", "engine": "playwright", "proxy": "",
            }]
            warmer = Mock(return_value={
                "email": "malformed@example.test", "success": True,
                "browser_status": "authenticated", "cleanup_status": "completed",
                "browser_process_stopped": False, "lease_released": False,
            })
            with patch.dict(os.environ, {"LEDGER_DB_PATH": db_path}), \
                 patch.dict(sys.modules, {
                     "core.account_manager": types.SimpleNamespace(account_manager=manager),
                     "core.account_warmer": types.SimpleNamespace(warm_account=warmer),
                 }):
                result = execute(
                    "warm", {"account_ids": [], "duration_minutes": 1},
                    Mock(), job_id="job-malformed-warm",
                )

            self.assertEqual(result["successes"], 0)
            self.assertEqual(result["failures"], 1)
            self.assertFalse(result["results"][0]["success"])
            ledger = JobLedger(db_path)
            self.assertEqual(ledger.get_job("job-malformed-warm")["state"], "failed")
            attempt = ledger.list_attempts("job-malformed-warm")[0]
            self.assertEqual(attempt["state"], "failed")
            self.assertEqual(attempt["error_code"], "cleanup_failed")

    def test_worker_closes_attempt_when_compatibility_retry_returns_bad_cooldown(self):
        from unittest.mock import Mock, patch
        from web.worker import _run_retryable_operation

        class LegacyRetryAdapter:
            MAX_RETRIES = 2
            worker_id = "legacy-worker"

            def begin_attempt(self, strategy, **kwargs):
                return ledger.start_attempt(
                    kwargs["job_id"], strategy=strategy,
                    worker_id=self.worker_id,
                )

            def record_attempt(self, *args, **kwargs):
                return {"retry_decision": "retry", "cooldown_seconds": "not-an-int"}

        with tempfile.TemporaryDirectory() as directory:
            ledger = JobLedger(directory + "/database.db")
            job = ledger.create_job("warm")
            adapter = LegacyRetryAdapter()
            with patch("web.worker._operation_retry_engine", return_value=adapter), \
                 patch("web.worker.time.sleep", new=Mock()):
                result = _run_retryable_operation(
                    "warm", lambda: {
                        "success": False,
                        "error_code": "runtime_unavailable",
                    }, ledger=ledger, job_id=job["job_id"],
                    subject="legacy@example.test",
                )

            self.assertFalse(result["success"])
            attempts = ledger.list_attempts(job["job_id"])
            self.assertEqual(len(attempts), 1)
            self.assertNotEqual(attempts[0]["state"], "running")
            self.assertEqual(attempts[0]["error_code"], "invalid_reconciliation_result")

    def test_worker_closes_attempt_when_retry_adapter_is_cancelled(self):
        import asyncio
        from unittest.mock import patch
        from web.worker import _run_retryable_operation

        class CancelledRetryAdapter:
            MAX_RETRIES = 1
            worker_id = "cancelled-retry-worker"

            def begin_attempt(self, strategy, **kwargs):
                return ledger.start_attempt(
                    kwargs["job_id"], strategy=strategy,
                    worker_id=self.worker_id,
                )

            def record_attempt(self, *args, **kwargs):
                raise asyncio.CancelledError()

        with tempfile.TemporaryDirectory() as directory:
            ledger = JobLedger(directory + "/database.db")
            job = ledger.create_job("warm")
            with patch("web.worker._operation_retry_engine", return_value=CancelledRetryAdapter()):
                with self.assertRaises(asyncio.CancelledError):
                    _run_retryable_operation(
                        "warm", lambda: {"success": False, "error_code": "timeout"},
                        ledger=ledger, job_id=job["job_id"],
                        subject="cancelled-retry@example.test",
                    )

            attempts = ledger.list_attempts(job["job_id"])
            self.assertEqual(len(attempts), 1)
            self.assertEqual(attempts[0]["state"], "cancelled")

    def test_web_warm_retries_transient_failure_through_durable_engine(self):
        from unittest.mock import Mock, patch
        from core.retry_engine import RetryEngine
        from web.worker import execute

        with tempfile.TemporaryDirectory() as directory:
            db_path = directory + "/database.db"
            account_manager = Mock()
            account_manager.get_all.return_value = [{
                "id": 4, "email": "retry@example.test", "password": "pw",
                "profile_id": "profile-retry", "engine": "playwright", "proxy": "",
            }]
            warmer = Mock(side_effect=[
                {"email": "retry@example.test", "success": False,
                 "browser_status": "runtime_unavailable", "error_code": "runtime_unavailable"},
                {"email": "retry@example.test", "success": True,
                 "browser_status": "authenticated", "error_code": "",
                 "cleanup_status": "completed", "browser_process_stopped": True,
                 "lease_released": True},
            ])
            modules = {
                "core.account_manager": types.SimpleNamespace(account_manager=account_manager),
                "core.account_warmer": types.SimpleNamespace(warm_account=warmer),
            }
            with patch.dict(os.environ, {"LEDGER_DB_PATH": db_path}), \
                 patch.dict(sys.modules, modules), \
                 patch.object(RetryEngine, "MAX_RETRIES", 2), \
                 patch.object(RetryEngine, "COOLDOWN_BASE", 0), \
                 patch("web.worker.time.sleep"):
                result = execute(
                    "warm", {"account_ids": [], "duration_minutes": 1},
                    Mock(), job_id="job-web-warm-retry",
                )

            self.assertEqual(result["successes"], 1)
            self.assertEqual(warmer.call_count, 2)
            attempts = JobLedger(db_path).list_attempts("job-web-warm-retry")
            self.assertEqual(len(attempts), 2)
            self.assertEqual(attempts[0]["retry_decision"], "stop")
            self.assertEqual(attempts[1]["retry_decision"], "stop")

    def test_web_compensation_retries_failed_pass_through_durable_engine(self):
        from unittest.mock import AsyncMock, Mock, patch
        from core.retry_engine import RetryEngine
        from services import sms_manager
        from web.worker import execute

        with tempfile.TemporaryDirectory() as directory:
            db_path = directory + "/database.db"
            reconciler = AsyncMock(side_effect=[
                {"claimed": 1, "cancelled": 0, "completed": 0, "failed": 1},
                {"claimed": 1, "cancelled": 1, "completed": 0, "failed": 0},
            ])
            with patch.dict(os.environ, {"LEDGER_DB_PATH": db_path}), \
                 patch.object(sms_manager, "reconcile_expired_orders", reconciler), \
                 patch.object(RetryEngine, "MAX_RETRIES", 2), \
                 patch.object(RetryEngine, "COOLDOWN_BASE", 0), \
                 patch("web.worker.time.sleep"):
                result = execute(
                    "compensation", {}, Mock(), job_id="job-compensation-retry",
                )

            self.assertEqual(result["cancelled"], 1)
            self.assertEqual(reconciler.await_count, 2)
            attempts = JobLedger(db_path).list_attempts("job-compensation-retry")
            self.assertEqual(len(attempts), 2)
            self.assertEqual(attempts[0]["retry_decision"], "stop")
            self.assertEqual(attempts[1]["retry_decision"], "stop")

    def test_web_warm_exception_retries_and_records_stable_error(self):
        from unittest.mock import Mock, patch
        from core.retry_engine import RetryEngine
        from web.worker import execute

        with tempfile.TemporaryDirectory() as directory:
            db_path = directory + "/database.db"
            account_manager = Mock()
            account_manager.get_all.return_value = [{
                "id": 5, "email": "exception@example.test", "password": "pw",
                "profile_id": "profile-exception", "engine": "selenium", "proxy": "",
            }]
            warmer = Mock(side_effect=[RuntimeError("provider password=secret"), {
                "email": "exception@example.test", "success": True,
                "browser_status": "authenticated", "cleanup_status": "completed",
                "browser_process_stopped": True, "lease_released": True,
            }])
            with patch.dict(os.environ, {"LEDGER_DB_PATH": db_path}), \
                 patch.dict(sys.modules, {
                     "core.account_manager": types.SimpleNamespace(account_manager=account_manager),
                     "core.account_warmer": types.SimpleNamespace(warm_account=warmer),
                 }), \
                 patch.object(RetryEngine, "MAX_RETRIES", 2), \
                 patch.object(RetryEngine, "COOLDOWN_BASE", 0):
                result = execute(
                    "warm", {"account_ids": [], "duration_minutes": 1},
                    Mock(), job_id="job-web-warm-exception",
                )

            self.assertEqual(result["successes"], 1)
            self.assertEqual(warmer.call_count, 2)
            attempts = JobLedger(db_path).list_attempts("job-web-warm-exception")
            self.assertEqual(attempts[0]["error_code"], "error")
            self.assertEqual(attempts[0]["retry_decision"], "stop")

    def test_web_health_retries_incomplete_network_snapshot(self):
        from unittest.mock import Mock, patch
        from core.retry_engine import RetryEngine
        from web.worker import execute

        with tempfile.TemporaryDirectory() as directory:
            db_path = directory + "/database.db"
            account_manager = Mock()
            account_manager.get_all.return_value = [{
                "id": 8, "email": "health-retry@example.test", "password": "pw",
                "profile_id": "profile-health-retry", "engine": "playwright", "proxy": "",
            }]
            checker = Mock()
            checker.check_single.side_effect = [
                {"status": "network_error", "message": "temporary"},
                {"status": "active", "message": "ok"},
            ]
            with patch.dict(os.environ, {"LEDGER_DB_PATH": db_path}), \
                 patch.dict(sys.modules, {
                     "core.account_manager": types.SimpleNamespace(account_manager=account_manager),
                     "core.health_checker": types.SimpleNamespace(AccountHealthChecker=checker),
                 }), \
                 patch.object(RetryEngine, "MAX_RETRIES", 2), \
                 patch.object(RetryEngine, "COOLDOWN_BASE", 0):
                result = execute(
                    "health", {"account_ids": []}, Mock(),
                    job_id="job-web-health-retry",
                )

            self.assertEqual(checker.check_single.call_count, 2)
            self.assertEqual(result["results"][-1]["status"], "active")
            attempts = JobLedger(db_path).list_attempts("job-web-health-retry")
            self.assertEqual(attempts[0]["retry_decision"], "stop")
            self.assertEqual(attempts[1]["retry_decision"], "stop")

    def test_web_warm_cancellation_closes_current_attempt_and_job(self):
        from unittest.mock import Mock, patch
        from core.retry_engine import RetryEngine
        from web.worker import execute

        with tempfile.TemporaryDirectory() as directory:
            db_path = directory + "/database.db"
            account_manager = Mock()
            account_manager.get_all.return_value = [{
                "id": 6, "email": "cancel@example.test", "password": "pw",
                "profile_id": "profile-cancel", "engine": "playwright", "proxy": "",
            }]
            warmer = Mock(side_effect=KeyboardInterrupt)
            with patch.dict(os.environ, {"LEDGER_DB_PATH": db_path}), \
                 patch.dict(sys.modules, {
                     "core.account_manager": types.SimpleNamespace(account_manager=account_manager),
                     "core.account_warmer": types.SimpleNamespace(warm_account=warmer),
                 }), \
                 patch.object(RetryEngine, "MAX_RETRIES", 2):
                with self.assertRaises(KeyboardInterrupt):
                    execute(
                        "warm", {"account_ids": [], "duration_minutes": 1},
                        Mock(), job_id="job-web-warm-cancel",
                    )

            ledger = JobLedger(db_path)
            self.assertEqual(ledger.get_job("job-web-warm-cancel")["state"], "cancelled")
            attempts = ledger.list_attempts("job-web-warm-cancel")
            self.assertEqual(len(attempts), 1)
            self.assertEqual(attempts[0]["state"], "cancelled")
            self.assertEqual(attempts[0]["retry_decision"], "stop")

    def test_web_operation_does_not_start_attempt_for_terminal_job(self):
        from unittest.mock import Mock, patch
        from web.worker import execute

        with tempfile.TemporaryDirectory() as directory:
            db_path = directory + "/database.db"
            ledger = JobLedger(db_path)
            ledger.create_job("warm", job_id="job-web-terminal")
            ledger.finalize_job("job-web-terminal", state="cancelled", error_code="cancelled")
            manager = Mock()
            manager.get_all.return_value = [{
                "id": 7, "email": "terminal@example.test", "password": "pw",
                "profile_id": "profile-terminal", "engine": "playwright", "proxy": "",
            }]
            warmer = Mock()
            with patch.dict(os.environ, {"LEDGER_DB_PATH": db_path}), \
                 patch.dict(sys.modules, {
                     "core.account_manager": types.SimpleNamespace(account_manager=manager),
                     "core.account_warmer": types.SimpleNamespace(warm_account=warmer),
                 }):
                with self.assertRaisesRegex(ValueError, "already finalized"):
                    execute(
                        "warm", {"account_ids": [], "duration_minutes": 1},
                        Mock(), job_id="job-web-terminal",
                    )
            self.assertEqual(ledger.list_attempts("job-web-terminal"), [])
            warmer.assert_not_called()

    def test_web_warm_accounts_share_one_durable_job_and_attempt_ledger(self):
        from unittest.mock import Mock, patch
        from web.worker import execute

        with tempfile.TemporaryDirectory() as directory:
            db_path = directory + "/database.db"
            account_manager = Mock()
            account_manager.get_all.return_value = [{
                "id": 4, "email": "one@example.test", "password": "pw",
                "profile_id": "profile-4", "engine": "playwright", "proxy": "",
            }]
            warmer = Mock(return_value={
                "email": "one@example.test", "success": True,
                "browser_status": "authenticated", "error_code": "",
                "cleanup_status": "completed", "browser_process_stopped": True,
                "lease_released": True,
            })
            modules = {
                "config.settings": types.SimpleNamespace(Config=types.SimpleNamespace()),
                "core.account_manager": types.SimpleNamespace(account_manager=account_manager),
                "core.account_warmer": types.SimpleNamespace(warm_account=warmer),
            }
            with patch.dict(os.environ, {"LEDGER_DB_PATH": db_path}), \
                 patch.dict(sys.modules, modules):
                result = execute(
                    "warm", {"account_ids": [], "duration_minutes": 1},
                    Mock(), job_id="job-web-warm",
                )

            self.assertEqual(result["successes"], 1)
            ledger = JobLedger(db_path)
            self.assertEqual(ledger.get_job("job-web-warm")["state"], "succeeded")
            attempts = ledger.list_attempts("job-web-warm")
            self.assertEqual(len(attempts), 1)
            self.assertEqual(attempts[0]["subject"], "one@example.test")
            self.assertEqual(attempts[0]["state"], "succeeded")

    def test_parallel_account_attempt_uses_same_ledger_job(self):
        from unittest.mock import Mock, patch
        from core.batch_runner import _create_single_account

        with tempfile.TemporaryDirectory() as directory:
            ledger = JobLedger(directory + "/database.db")
            job = ledger.create_job("create", requested_count=1)
            with patch.dict(sys.modules, {
                "core.runners": types.SimpleNamespace(
                    run_playwright_flow=lambda *_args, **_kwargs: True,
                ),
            }):
                result = _create_single_account(
                    0, 1, "playwright", "pw", 0, "standard", False,
                    ledger=ledger, job_id=job["job_id"], ordinal=1,
                )
            self.assertTrue(result["success"])
            attempts = ledger.list_attempts(job["job_id"])
            self.assertEqual(len(attempts), 1)
            self.assertEqual(attempts[0]["ordinal"], 1)
            self.assertEqual(attempts[0]["state"], "succeeded")

    def test_serial_worker_does_not_add_orchestration_attempt_on_top_of_account_attempt(self):
        from unittest.mock import Mock, patch
        from web.worker import execute

        with tempfile.TemporaryDirectory() as directory:
            db_path = directory + "/database.db"
            manager = Mock()
            manager.has_saved_session.return_value = False
            manager.clear_state = Mock()

            def flow(_count, **kwargs):
                attempt = kwargs["ledger"].start_attempt(
                    kwargs["job_id"], ordinal=1, subject="serial@example.test",
                    strategy="standard", engine="playwright",
                )
                kwargs["ledger"].finish_attempt(attempt["attempt_id"], outcome="succeeded")
                return {"total": 1, "successes": 1, "failures": 0, "duration": 0}

            config = types.SimpleNamespace(
                YOUR_PASSWORD="pw", ENGINE_MODE="playwright",
                FIVESIM_API_KEY="", SMS_ACTIVATE_API_KEY="",
                ONLINESIM_API_KEY="", GETSMS_API_KEY="",
            )
            with patch.dict(os.environ, {"LEDGER_DB_PATH": db_path}), \
                 patch.dict(sys.modules, {
                     "config.settings": types.SimpleNamespace(Config=config),
                     "core.session_resume": types.SimpleNamespace(session_manager=manager),
                     "core.creation_flow": types.SimpleNamespace(run_creation_flow=flow),
                     "core.retry_engine": types.SimpleNamespace(retry_engine=Mock()),
                 }):
                result = execute(
                    "create", {
                        "engine": "playwright", "num_accounts": 1,
                        "warmup_minutes": 0, "flow_mode": "standard",
                        "use_sms_api": False, "parallel": False,
                        "max_threads": 1,
                    }, Mock(), job_id="job-serial-ledger",
                )

            self.assertEqual(result["successes"], 1)
            attempts = JobLedger(db_path).list_attempts("job-serial-ledger")
            self.assertEqual(len(attempts), 1)
            self.assertEqual(attempts[0]["subject"], "serial@example.test")

    def test_parallel_worker_does_not_add_orchestration_attempt_on_top_of_account_attempt(self):
        from unittest.mock import Mock, patch
        from web.worker import execute

        with tempfile.TemporaryDirectory() as directory:
            db_path = directory + "/database.db"

            def batch(_count, **kwargs):
                attempt = kwargs["ledger"].start_attempt(
                    kwargs["job_id"], ordinal=1, subject="parallel@example.test",
                    strategy="standard", engine="playwright",
                )
                kwargs["ledger"].finish_attempt(attempt["attempt_id"], outcome="succeeded")
                return {"total": 1, "successes": 1, "failures": 0, "duration": 0}

            config = types.SimpleNamespace(
                YOUR_PASSWORD="pw", ENGINE_MODE="playwright",
                FIVESIM_API_KEY="", SMS_ACTIVATE_API_KEY="",
                ONLINESIM_API_KEY="", GETSMS_API_KEY="",
            )
            with patch.dict(os.environ, {"LEDGER_DB_PATH": db_path}), \
                 patch.dict(sys.modules, {
                     "config.settings": types.SimpleNamespace(Config=config),
                     "core.session_resume": types.SimpleNamespace(session_manager=Mock(has_saved_session=Mock(return_value=False))),
                     "core.creation_flow": types.SimpleNamespace(run_creation_flow=Mock()),
                     "core.batch_runner": types.SimpleNamespace(run_batch=batch),
                     "core.retry_engine": types.SimpleNamespace(retry_engine=Mock()),
                 }):
                result = execute(
                    "create", {
                        "engine": "playwright", "num_accounts": 1,
                        "warmup_minutes": 0, "flow_mode": "standard",
                        "use_sms_api": False, "parallel": True,
                        "max_threads": 1,
                    }, Mock(), job_id="job-parallel-ledger",
                )

            self.assertEqual(result["successes"], 1)
            attempts = JobLedger(db_path).list_attempts("job-parallel-ledger")
            self.assertEqual(len(attempts), 1)
            self.assertEqual(attempts[0]["subject"], "parallel@example.test")

    def test_task_manager_cancellation_finalizes_matching_ledger_job(self):
        from pathlib import Path
        from web.tasks import TaskManager

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger_path = root / "data" / "database.db"
            ledger = JobLedger(str(ledger_path))
            job = ledger.create_job("warm", requested_count=1, job_id="task-cancel-ledger")
            attempt = ledger.start_attempt(job["job_id"], ordinal=1)
            manager = object.__new__(TaskManager)
            manager.root = root
            manager._finalize_ledger(
                "task-cancel-ledger", "cancelled", "cancelled", cleanup_verified=True,
            )
            self.assertEqual(ledger.get_job(job["job_id"])["state"], "cancelled")
            self.assertEqual(ledger.get_attempt(attempt["attempt_id"])["state"], "cancelled")

    def test_serial_creation_uses_retry_engine_decision_and_next_strategy(self):
        from contextlib import nullcontext
        from unittest.mock import Mock, patch
        from core.job_ledger import JobLedger

        with tempfile.TemporaryDirectory() as directory:
            ledger = JobLedger(directory + "/database.db")
            job = ledger.create_job("create", requested_count=1, requested_engine="playwright")
            config = types.SimpleNamespace(
                ENGINE_MODE="playwright", YOUR_PASSWORD="pw",
                DELAY_BETWEEN_ACCOUNTS=0,
            )
            proxy = Mock()
            proxy.count = 2
            proxy.get_best.return_value = None
            proxy.get_next.return_value = None
            flow = Mock(side_effect=[False, True])
            fake_session = Mock()
            fake_session.save_state = Mock()
            fake_session.clear_state = Mock()
            fake_modules = {
                "config.settings": types.SimpleNamespace(Config=config),
                "core.proxy_manager": types.SimpleNamespace(proxy_manager=proxy),
                "core.database": types.SimpleNamespace(DatabaseManager=Mock()),
                "core.session_resume": types.SimpleNamespace(session_manager=fake_session),
                "core.runners": types.SimpleNamespace(run_playwright_flow=flow),
                "core.telegram_notifier": types.SimpleNamespace(notifier=Mock()),
            }
            spec = importlib.util.spec_from_file_location(
                "tested_creation_retry", ROOT / "core/creation_flow.py"
            )
            module = importlib.util.module_from_spec(spec)
            with patch.dict(sys.modules, fake_modules):
                spec.loader.exec_module(module)
                module._generate_username = Mock(return_value=("retryuser", ["Retry", "User"]))
                module.get_progress_context = lambda: nullcontext(Mock())
                module.show_session_summary = Mock()
                module.print_success = Mock()
                module.print_error = Mock()
                module.print_warning = Mock()
                module.time.sleep = Mock()
                with patch.object(module.RetryEngine, "COOLDOWN_BASE", 0):
                    result = module.run_creation_flow(
                        1, warmup_minutes=0, flow_mode="standard", use_sms_api=False,
                        ledger=ledger, job_id=job["job_id"],
                    )

            self.assertEqual(result["successes"], 1)
            self.assertEqual(flow.call_count, 2)
            self.assertNotEqual(
                flow.call_args_list[0].kwargs["flow_mode"],
                flow.call_args_list[1].kwargs["flow_mode"],
            )
            attempts = ledger.list_attempts(job["job_id"])
            self.assertEqual(len(attempts), 2)
            self.assertEqual(attempts[0]["retry_decision"], "stop")
            self.assertEqual(attempts[1]["retry_decision"], "stop")

    def test_serial_resume_skips_account_already_succeeded_in_ledger(self):
        from contextlib import nullcontext

        with tempfile.TemporaryDirectory() as directory:
            ledger = JobLedger(directory + "/database.db")
            job = ledger.create_job(
                "create", requested_count=1, requested_engine="playwright",
            )
            retry = RetryEngine(ledger=ledger, job_id=job["job_id"])
            retry.record_attempt(
                "standard", True, account_id=0,
                subject="completed@example.test",
            )
            config = types.SimpleNamespace(
                ENGINE_MODE="playwright", YOUR_PASSWORD="pw",
                DELAY_BETWEEN_ACCOUNTS=0,
            )
            runner = Mock(side_effect=AssertionError("completed account rerun"))
            session = Mock()
            fake_modules = {
                "config.settings": types.SimpleNamespace(Config=config),
                "core.proxy_manager": types.SimpleNamespace(proxy_manager=Mock()),
                "core.database": types.SimpleNamespace(DatabaseManager=Mock()),
                "core.session_resume": types.SimpleNamespace(session_manager=session),
                "core.runners": types.SimpleNamespace(run_playwright_flow=runner),
                "core.telegram_notifier": types.SimpleNamespace(notifier=Mock()),
            }
            spec = importlib.util.spec_from_file_location(
                "tested_creation_resume_ledger", ROOT / "core/creation_flow.py"
            )
            module = importlib.util.module_from_spec(spec)
            with patch.dict(sys.modules, fake_modules):
                spec.loader.exec_module(module)
                module.get_progress_context = lambda: nullcontext(Mock())
                module.show_session_summary = Mock()
                module.print_success = Mock()
                module.print_error = Mock()
                module.print_warning = Mock()
                result = module.run_creation_flow(
                    1, ledger=ledger, job_id=job["job_id"],
                    resume_state={
                        "batch_config": {
                            "num_accounts": 1, "engine": "playwright",
                            "flow_mode": "standard", "use_sms_api": False,
                            "warmup_minutes": 0, "job_id": job["job_id"],
                        },
                        "completed_indices": [],
                        "results": {"successes": 0, "failures": 0},
                    },
                )

            self.assertEqual(result["successes"], 1)
            runner.assert_not_called()
            self.assertEqual(len(ledger.list_attempts(job["job_id"])), 1)

    def test_serial_cancellation_during_retry_wait_finalizes_job(self):
        from contextlib import nullcontext

        with tempfile.TemporaryDirectory() as directory:
            ledger = JobLedger(directory + "/database.db")
            job = ledger.create_job(
                "create", requested_count=1, requested_engine="playwright",
            )
            config = types.SimpleNamespace(
                ENGINE_MODE="playwright", YOUR_PASSWORD="pw",
                DELAY_BETWEEN_ACCOUNTS=0,
            )
            session = Mock(has_saved_session=Mock(return_value=False))
            fake_modules = {
                "config.settings": types.SimpleNamespace(Config=config),
                "core.proxy_manager": types.SimpleNamespace(
                    proxy_manager=Mock(get_best=Mock(return_value=None), get_next=Mock(return_value=None))
                ),
                "core.database": types.SimpleNamespace(DatabaseManager=Mock()),
                "core.session_resume": types.SimpleNamespace(session_manager=session),
                "core.runners": types.SimpleNamespace(run_playwright_flow=Mock(return_value=False)),
                "core.telegram_notifier": types.SimpleNamespace(notifier=Mock()),
            }
            spec = importlib.util.spec_from_file_location(
                "tested_creation_retry_cancel", ROOT / "core/creation_flow.py"
            )
            module = importlib.util.module_from_spec(spec)
            with patch.dict(sys.modules, fake_modules):
                spec.loader.exec_module(module)
                module._generate_username = Mock(return_value=("cancelwait", ["Cancel", "Wait"]))
                module.get_progress_context = lambda: nullcontext(Mock())
                module.print_error = Mock()
                module.print_warning = Mock()
                with patch.object(module.RetryEngine, "wait_for_retry", side_effect=KeyboardInterrupt):
                    with self.assertRaises(KeyboardInterrupt):
                        module.run_creation_flow(
                            1, warmup_minutes=0, ledger=ledger,
                            job_id=job["job_id"],
                        )

            self.assertEqual(ledger.get_job(job["job_id"])["state"], "cancelled")
            attempt = ledger.list_attempts(job["job_id"])[0]
            self.assertEqual(attempt["retry_decision"], "stop")
            self.assertIsNone(attempt["next_attempt_at"])

    def test_parallel_account_uses_retry_engine_until_success(self):
        from unittest.mock import Mock, patch
        from core.batch_runner import _create_single_account

        with tempfile.TemporaryDirectory() as directory:
            ledger = JobLedger(directory + "/database.db")
            job = ledger.create_job("create", requested_count=1)
            runner = Mock(side_effect=[False, True])
            with patch("core.batch_runner._generate_username", return_value=("parallelretry", "Parallel", "Retry")), \
                 patch("core.batch_runner.proxy_manager.get_best", return_value=None), \
                 patch("core.batch_runner.proxy_manager.get_next", return_value=None), \
                 patch.object(RetryEngine, "COOLDOWN_BASE", 0), \
                 patch.dict(sys.modules, {
                     "core.runners": types.SimpleNamespace(run_playwright_flow=runner),
                 }):
                result = _create_single_account(
                    0, 1, "playwright", "pw", 0, "standard", False,
                    ledger=ledger, job_id=job["job_id"], ordinal=1,
                )
            self.assertTrue(result["success"])
            self.assertEqual(runner.call_count, 2)
            self.assertNotEqual(
                runner.call_args_list[0].kwargs["flow_mode"],
                runner.call_args_list[1].kwargs["flow_mode"],
            )
            attempts = ledger.list_attempts(job["job_id"])
            self.assertEqual(len(attempts), 2)
            self.assertEqual(attempts[0]["retry_decision"], "pending")
            self.assertEqual(attempts[1]["retry_decision"], "stop")

    def test_parallel_batch_cancellation_wakes_retry_waiters(self):
        from core.batch_runner import run_batch

        with tempfile.TemporaryDirectory() as directory:
            ledger = JobLedger(directory + "/database.db")
            job = ledger.create_job("create", requested_count=1)
            observed = []

            def worker(*args, **kwargs):
                event = kwargs.get("cancel_event")
                observed.append(event)
                if event is not None:
                    event.wait(1)
                else:
                    time.sleep(0.05)
                return {
                    "index": 0, "username": "cancelled", "email": "",
                    "success": False, "error_type": "cancelled",
                    "proxy_endpoint": "",
                }

            config = types.SimpleNamespace(
                ENGINE_MODE="playwright", YOUR_PASSWORD="pw",
            )
            with patch("core.batch_runner.Config", config), \
                 patch("core.batch_runner._create_single_account", side_effect=worker), \
                 patch("core.batch_runner.as_completed", side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    run_batch(
                        1, max_threads=1, ledger=ledger, job_id=job["job_id"],
                    )

            self.assertEqual(len(observed), 1)
            self.assertIsNotNone(observed[0])
            self.assertTrue(observed[0].is_set())
            self.assertEqual(ledger.get_job(job["job_id"])["state"], "cancelled")

    def test_parallel_batch_cancellation_reaches_durable_retry_wait(self):
        from core.batch_runner import run_batch

        with tempfile.TemporaryDirectory() as directory:
            ledger = JobLedger(directory + "/database.db")
            job = ledger.create_job("create", requested_count=1)
            retry_wait_started = threading.Event()
            observed = []

            def wait_for_retry(_engine, _attempt, *, cancel_event=None, max_wait=None):
                observed.append(cancel_event)
                retry_wait_started.set()
                if cancel_event is None:
                    time.sleep(0.05)
                else:
                    cancel_event.wait(2)
                return None

            def interrupt_when_retry_waits(_futures):
                self.assertTrue(retry_wait_started.wait(1))
                raise KeyboardInterrupt
                yield  # pragma: no cover - keeps this an iterator

            config = types.SimpleNamespace(
                ENGINE_MODE="playwright", YOUR_PASSWORD="pw",
            )
            with patch("core.batch_runner.Config", config), \
                 patch("core.batch_runner._generate_username", return_value=("cancelretry", "Cancel", "Retry")), \
                 patch("core.batch_runner.proxy_manager.get_best", return_value=None), \
                 patch("core.batch_runner.proxy_manager.get_next", return_value=None), \
                 patch("core.runners.run_playwright_flow", return_value=False), \
                 patch.object(RetryEngine, "wait_for_retry", new=wait_for_retry), \
                 patch("core.batch_runner.as_completed", new=interrupt_when_retry_waits):
                started = time.monotonic()
                with self.assertRaises(KeyboardInterrupt):
                    run_batch(
                        1, max_threads=1, ledger=ledger, job_id=job["job_id"],
                    )
                elapsed = time.monotonic() - started

            self.assertEqual(len(observed), 1)
            self.assertIsNotNone(observed[0])
            self.assertTrue(observed[0].is_set())
            self.assertLess(elapsed, 1)
            self.assertEqual(ledger.get_job(job["job_id"])["state"], "cancelled")

    def test_parallel_missing_password_finishes_job_instead_of_leaving_pending(self):
        from unittest.mock import patch
        from core.batch_runner import run_batch

        with tempfile.TemporaryDirectory() as directory:
            ledger = JobLedger(directory + "/database.db")
            job = ledger.create_job("create", requested_count=1)
            config = types.SimpleNamespace(
                ENGINE_MODE="playwright", YOUR_PASSWORD="",
            )
            with patch("core.batch_runner.Config", config), \
                 patch("builtins.open", side_effect=FileNotFoundError):
                result = run_batch(
                    1, max_threads=1, ledger=ledger, job_id=job["job_id"],
                )
            self.assertEqual(result["total"], 0)
            self.assertEqual(ledger.get_job(job["job_id"])["state"], "failed")

    def test_direct_serial_flow_closes_running_attempt_on_keyboard_interrupt(self):
        from contextlib import nullcontext
        from unittest.mock import Mock, patch

        with tempfile.TemporaryDirectory() as directory:
            ledger = JobLedger(directory + "/database.db")
            job = ledger.create_job("create", requested_count=1, requested_engine="playwright")
            config = types.SimpleNamespace(
                ENGINE_MODE="playwright", YOUR_PASSWORD="pw",
                DELAY_BETWEEN_ACCOUNTS=0,
            )
            flow = Mock(side_effect=KeyboardInterrupt)
            fake_modules = {
                "config.settings": types.SimpleNamespace(Config=config),
                "core.proxy_manager": types.SimpleNamespace(
                    proxy_manager=Mock(get_best=Mock(return_value=None), get_next=Mock(return_value=None))
                ),
                "core.runners": types.SimpleNamespace(run_playwright_flow=flow),
                "core.database": types.SimpleNamespace(DatabaseManager=Mock()),
                "core.session_resume": types.SimpleNamespace(session_manager=Mock()),
                "core.telegram_notifier": types.SimpleNamespace(notifier=Mock()),
            }
            spec = importlib.util.spec_from_file_location(
                "tested_direct_serial_interrupt", ROOT / "core/creation_flow.py"
            )
            module = importlib.util.module_from_spec(spec)
            with patch.dict(sys.modules, fake_modules):
                spec.loader.exec_module(module)
                module._generate_username = Mock(return_value=("interruptuser", ["Interrupt", "User"]))
                module.get_progress_context = lambda: nullcontext(Mock())
                module.show_session_summary = Mock()
                module.print_success = Mock()
                module.print_error = Mock()
                module.print_warning = Mock()
                module.time.sleep = Mock()
                with self.assertRaises(KeyboardInterrupt):
                    module.run_creation_flow(
                        1, warmup_minutes=0, flow_mode="standard",
                        use_sms_api=False, ledger=ledger, job_id=job["job_id"],
                    )

            attempts = ledger.list_attempts(job["job_id"])
            self.assertEqual(len(attempts), 1)
            self.assertEqual(attempts[0]["state"], "cancelled")
            self.assertEqual(attempts[0]["error_code"], "cancelled")
            self.assertEqual(ledger.get_job(job["job_id"])["state"], "cancelled")

    def test_direct_parallel_runner_exception_closes_running_attempt(self):
        from unittest.mock import Mock, patch
        from core.batch_runner import _create_single_account

        with tempfile.TemporaryDirectory() as directory:
            ledger = JobLedger(directory + "/database.db")
            job = ledger.create_job("create", requested_count=1, requested_engine="playwright")
            runner = Mock(side_effect=RuntimeError("runner exploded"))
            with patch.object(__import__("core.batch_runner", fromlist=["proxy_manager"]),
                              "proxy_manager", Mock(get_best=Mock(return_value=None), get_next=Mock(return_value=None))), \
                 patch.dict(sys.modules, {
                     "core.runners": types.SimpleNamespace(run_playwright_flow=runner),
                 }), \
                 patch.object(__import__("core.batch_runner", fromlist=["RetryEngine"]).RetryEngine,
                              "COOLDOWN_BASE", 0):
                result = _create_single_account(
                    0, 1, "playwright", "pw", 0, "standard", False,
                    ledger=ledger, job_id=job["job_id"], ordinal=1,
                )

            self.assertFalse(result["success"])
            attempts = ledger.list_attempts(job["job_id"])
            self.assertGreaterEqual(len(attempts), 1)
            self.assertTrue(all(item["state"] == "failed" for item in attempts))
            self.assertTrue(all(item["error_code"] == "RuntimeError" for item in attempts))
            self.assertTrue(all(item["state"] != "running" for item in attempts))

    def test_direct_parallel_flow_closes_attempts_on_keyboard_interrupt(self):
        from unittest.mock import Mock, patch
        from core.batch_runner import run_batch

        with tempfile.TemporaryDirectory() as directory:
            ledger = JobLedger(directory + "/database.db")
            job = ledger.create_job("create", requested_count=1, requested_engine="playwright")
            config = types.SimpleNamespace(ENGINE_MODE="playwright", YOUR_PASSWORD="pw")
            proxy = Mock(get_best=Mock(return_value=None), get_next=Mock(return_value=None))
            with patch("core.batch_runner.Config", config), \
                 patch("core.batch_runner.proxy_manager", proxy), \
                 patch.dict(sys.modules, {
                     "core.runners": types.SimpleNamespace(
                         run_playwright_flow=Mock(side_effect=KeyboardInterrupt)
                     ),
                 }), \
                 patch("core.batch_runner.time.sleep"):
                with self.assertRaises(KeyboardInterrupt):
                    run_batch(
                        1, max_threads=1, warmup_minutes=0,
                        ledger=ledger, job_id=job["job_id"],
                    )

            attempts = ledger.list_attempts(job["job_id"])
            self.assertTrue(attempts)
            self.assertTrue(all(item["state"] == "cancelled" for item in attempts))
            self.assertEqual(ledger.get_job(job["job_id"])["state"], "cancelled")



if __name__ == "__main__":
    unittest.main()
