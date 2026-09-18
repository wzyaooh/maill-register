import importlib.util
import os
import sys
import tempfile
import types
import unittest
from contextlib import nullcontext
from unittest.mock import Mock, patch

from core.job_ledger import JobLedger
from core.retry_engine import RetryEngine
from core.secret_safety import SAFE_ERROR_CODES, normalize_error_code


class ErrorProtocolTests(unittest.TestCase):
    def test_migration_does_not_coerce_non_string_profile_id(self):
        from core.database import DatabaseManager

        projected = DatabaseManager.migration_profile_projection({"profile_id": 123})
        self.assertEqual(projected["profile_id"], "")

    SMS_CODES = (
        "sms_finish_failed", "sms_poll_failed", "sms_cancel_failed",
        "sms_error", "sms_no_balance", "sms_no_number",
        "sms_no_phone_input", "sms_phone_rejected", "sms_no_code_page",
        "sms_no_code_input", "sms_code_rejected", "sms_import_error",
        "sms_missing_attempt_context",
        "send_sms_blocked", "send_sms_escaped", "all_failed",
    )

    def test_sms_codes_are_part_of_the_global_finite_vocabulary(self):
        for code in self.SMS_CODES:
            with self.subTest(code=code):
                self.assertIn(code, SAFE_ERROR_CODES)
                self.assertEqual(normalize_error_code(code.upper()), code)

    def test_sms_error_code_survives_ledger_round_trip_without_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = JobLedger(os.path.join(directory, "database.db"))
            job = ledger.create_job("create")
            attempt = ledger.start_attempt(job["job_id"], ordinal=1)
            finished = ledger.finish_attempt(
                attempt["attempt_id"], outcome="failed",
                error_code="sms_code_rejected",
            )
            self.assertEqual(finished["error_code"], "sms_code_rejected")
            self.assertEqual(
                ledger.list_attempts(job["job_id"])[0]["error_code"],
                "sms_code_rejected",
            )

    def test_retry_policy_separates_sms_recovery_from_finish_compensation(self):
        engine = RetryEngine()
        self.assertTrue(engine.should_retry("sms_poll_failed", 1))
        self.assertTrue(engine.should_retry("sms_code_rejected", 1))
        self.assertTrue(engine.should_retry("sms_timeout", 1))
        self.assertFalse(engine.should_retry("sms_finish_failed", 1))
        self.assertFalse(engine.should_retry("sms_cancel_failed", 1))
        self.assertFalse(engine.should_retry("sms_no_balance", 1))
        self.assertFalse(engine.should_retry("sms_missing_attempt_context", 1))
        self.assertTrue(engine.should_retry("provider_timeout", 1, operation="health"))
        self.assertTrue(engine.should_retry("provider_timeout", 1, operation="warm"))
        self.assertIn("identity_unavailable", SAFE_ERROR_CODES)
        self.assertFalse(engine.should_retry("provider_rejected", 1, operation="compensation"))

    def test_creation_runner_preserves_stable_tuple_failure_code(self):
        """A runner's (False, code) result must reach the durable attempt."""
        spec = importlib.util.spec_from_file_location(
            "tested_creation_error_protocol",
            os.path.join(os.path.dirname(__file__), "..", "core", "creation_flow.py"),
        )
        module = importlib.util.module_from_spec(spec)
        with tempfile.TemporaryDirectory() as directory:
            ledger = JobLedger(os.path.join(directory, "database.db"))
            job = ledger.create_job("create", requested_count=1,
                                    requested_engine="playwright")
            config = types.SimpleNamespace(
                ENGINE_MODE="playwright", YOUR_PASSWORD="pw",
                DELAY_BETWEEN_ACCOUNTS=0,
            )
            proxy = Mock(get_best=Mock(return_value=None),
                         get_next=Mock(return_value=None))
            runner = Mock(return_value=(False, "sms_code_rejected"))
            fake_modules = {
                "config.settings": types.SimpleNamespace(Config=config),
                "core.proxy_manager": types.SimpleNamespace(proxy_manager=proxy),
                "core.database": types.SimpleNamespace(DatabaseManager=Mock()),
                "core.session_resume": types.SimpleNamespace(
                    session_manager=Mock(save_state=Mock(), clear_state=Mock()),
                ),
                "core.runners": types.SimpleNamespace(run_playwright_flow=runner),
                "core.telegram_notifier": types.SimpleNamespace(notifier=Mock()),
            }
            with patch.dict(sys.modules, fake_modules):
                spec.loader.exec_module(module)
                module._generate_username = Mock(
                    return_value=("smsuser", ["Sms", "User"])
                )
                module.get_progress_context = lambda: nullcontext(Mock())
                module.show_session_summary = Mock()
                module.print_success = Mock()
                module.print_error = Mock()
                module.print_warning = Mock()
                module.time.sleep = Mock()
                with patch.object(module.RetryEngine, "MAX_RETRIES", 1):
                    result = module.run_creation_flow(
                        1, warmup_minutes=0, flow_mode="standard",
                        use_sms_api=True, ledger=ledger, job_id=job["job_id"],
                    )

            self.assertEqual(result["failures"], 1)
            attempt = ledger.list_attempts(job["job_id"])[0]
            self.assertEqual(attempt["error_code"], "sms_code_rejected")


if __name__ == "__main__":
    unittest.main()
