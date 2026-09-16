import os
import tempfile
import types
import unittest
from contextlib import nullcontext
from unittest.mock import Mock, patch

from core.job_ledger import JobLedger


class AppiumContractTests(unittest.TestCase):
    def test_direct_appium_manager_is_fail_closed_without_lifecycle_contract(self):
        from core import android_creator

        manager = android_creator.AppiumManager()
        with patch.object(android_creator.webdriver, "Remote") as remote:
            self.assertFalse(manager.initialize())
        remote.assert_not_called()

    def test_serial_appium_is_explicitly_unsupported_without_starting_device_or_saving_account(self):
        from core import creation_flow

        with tempfile.TemporaryDirectory() as directory:
            ledger = JobLedger(os.path.join(directory, "database.db"))
            job = ledger.create_job("create", requested_count=1, requested_engine="appium")
            config = types.SimpleNamespace(
                ENGINE_MODE="appium", YOUR_PASSWORD="fixed-password",
                YOUR_BIRTHDAY="1 1 1990", YOUR_GENDER="1",
                DELAY_BETWEEN_ACCOUNTS=0,
            )
            progress = Mock()
            with patch.object(creation_flow, "Config", config), \
                 patch.object(creation_flow, "_generate_username", return_value=("appuser", ["App", "User"])), \
                 patch("core.runners.AppiumManager", side_effect=AssertionError("device must not start")), \
                 patch.object(creation_flow, "get_progress_context", return_value=nullcontext(progress)), \
                 patch.object(creation_flow.RetryEngine, "MAX_RETRIES", 1):
                result = creation_flow.run_creation_flow(
                    1, warmup_minutes=0, flow_mode="standard",
                    use_sms_api=False, ledger=ledger, job_id=job["job_id"],
                )

            self.assertEqual(result["failures"], 1)
            attempts = ledger.list_attempts(job["job_id"])
            self.assertEqual(len(attempts), 1)
            self.assertEqual(attempts[0]["error_code"], "unsupported")
            self.assertEqual(ledger.get_job(job["job_id"])["state"], "failed")
            self.assertEqual(ledger.get_job(job["job_id"])["summary"]["successes"], 0)

    def test_parallel_appium_attempt_is_recorded_as_unsupported(self):
        from core.batch_runner import _create_single_account

        with tempfile.TemporaryDirectory() as directory:
            ledger = JobLedger(os.path.join(directory, "database.db"))
            job = ledger.create_job("create", requested_count=1, requested_engine="appium")
            config = types.SimpleNamespace(
                YOUR_BIRTHDAY="1 1 1990", YOUR_GENDER="1",
            )
            with patch("core.batch_runner.Config", config), \
                 patch("core.runners.run_appium_flow", side_effect=AssertionError("device must not start")), \
                 patch.object(__import__("core.batch_runner", fromlist=["RetryEngine"]).RetryEngine, "MAX_RETRIES", 1):
                result = _create_single_account(
                    0, 1, "appium", "fixed-password", 0, "standard", False,
                    ledger=ledger, job_id=job["job_id"], ordinal=1,
                )

            self.assertFalse(result["success"])
            attempt = ledger.list_attempts(job["job_id"])[0]
            self.assertEqual(attempt["error_code"], "unsupported")
            self.assertEqual(attempt["retry_decision"], "stop")


if __name__ == "__main__":
    unittest.main()
