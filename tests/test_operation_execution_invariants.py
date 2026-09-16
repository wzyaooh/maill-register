import tempfile
import unittest
from pathlib import Path

from core.job_ledger import JobLedger
from web.worker import _operation_result_outcome, _run_retryable_operation


class OperationExecutionInvariantTests(unittest.TestCase):
    def test_boolean_only_browser_operations_cannot_claim_success(self):
        for action in ("health", "warm"):
            for value in (True, False):
                with self.subTest(action=action, value=value):
                    self.assertEqual(
                        _operation_result_outcome(action, value),
                        (False, "invalid_reconciliation_result"),
                    )

    def test_health_success_marker_must_be_a_boolean(self):
        for value in ("false", "true", 1, 0, None, [], {}):
            with self.subTest(value=value):
                self.assertEqual(_operation_result_outcome("health", {
                    "browser_status": "authenticated", "mailbox_status": "active",
                    "status": "active", "success": value,
                }), (False, "invalid_reconciliation_result"))

    def test_malformed_warm_result_is_durably_failed_without_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = JobLedger(str(Path(directory) / "database.db"))
            job = ledger.create_job("warm")
            calls = []

            def operation():
                calls.append("executed")
                return True

            result = _run_retryable_operation(
                "warm", operation, ledger=ledger, job_id=job["job_id"], subject="fixture",
            )
            self.assertIs(result["success"], False)
            self.assertEqual(result["error_code"], "invalid_reconciliation_result")
            self.assertEqual(calls, ["executed"])
            attempt = ledger.list_attempts(job["job_id"])[0]
            self.assertEqual(attempt["state"], "failed")
            self.assertEqual(attempt["retry_decision"], "stop")

    def test_complete_degraded_health_observation_remains_a_successful_probe(self):
        self.assertEqual(_operation_result_outcome("health", {
            "browser_status": "profile_busy", "mailbox_status": "active",
            "status": "degraded", "last_error_code": "profile_busy",
        }), (True, "profile_busy"))
