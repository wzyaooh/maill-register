import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import types
import unittest
from urllib.parse import quote
from pathlib import Path
from unittest.mock import Mock, patch

from core.database import DatabaseManager
from core.captcha_solver import CaptchaSolver
from core.telegram_notifier import TelegramNotifier
from web.tasks import _voice_token_is_configured
from core.secret_safety import (
    normalize_flow_mode,
    normalize_sms_service,
    redact_text,
    safe_account_metadata,
)


ROOT = Path(__file__).resolve().parents[1]


class DatabaseNotesBoundaryTests(unittest.TestCase):
    def test_strategy_and_sms_service_are_finite_on_write_and_read_projection(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "accounts.db"
            db = DatabaseManager(str(path))
            self.assertTrue(db.save_account(
                "metadata@example.test", "account-pass-100",
                strategy="password=metadata-secret",
                sms_service="token=sms-secret",
            ))
            row = db.get_all_accounts()[0]
            self.assertEqual(row["strategy"], normalize_flow_mode("password=metadata-secret"))
            self.assertEqual(row["sms_service"], normalize_sms_service("token=sms-secret"))

            # A legacy row can be edited by an older process after initial
            # creation.  Re-opening the database must scrub those values too.
            with sqlite3.connect(path) as conn:
                conn.execute(
                    "UPDATE accounts SET strategy=?, sms_service=? WHERE email=?",
                    ("authorization=legacy-secret", "password=legacy-sms-secret",
                     "metadata@example.test"),
                )
            reopened = DatabaseManager(str(path))
            row = reopened.get_all_accounts()[0]
            self.assertEqual(row["strategy"], "standard")
            self.assertEqual(row["sms_service"], "")
            projected = safe_account_metadata({
                "strategy": "authorization=projection-secret",
                "sms_service": "token=projection-secret",
            })
            self.assertEqual(projected["strategy"], "standard")
            self.assertEqual(projected["sms_service"], "")

    def test_legacy_offline_labels_remain_bounded_compatibility_metadata(self):
        self.assertEqual(normalize_flow_mode("offline"), "offline")
        self.assertEqual(normalize_sms_service("offline"), "offline")

    def test_unknown_flow_labels_collapse_to_standard(self):
        self.assertEqual(normalize_flow_mode("custom-safe-label"), "standard")
        self.assertEqual(normalize_flow_mode("Internal Migration Label"), "standard")

    def test_unknown_sms_labels_collapse_to_empty(self):
        self.assertEqual(normalize_sms_service("custom-safe-label"), "")
        self.assertEqual(normalize_sms_service("Internal Provider Label"), "")

    def test_redact_text_masks_unbound_auth_headers_and_token_shapes(self):
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.signature-value-123"
        text = (
            "Authorization: Bearer bearer-secret-123 "
            "Basic basic-secret-456 Cookie: SID=cookie-secret-789; HSID=other-secret "
            "access_token=access-secret refresh_token=refresh-secret "
            "storage_state=state-secret client_secret=client-secret oauth_token=oauth-secret "
            "AKIAIOSFODNN7EXAMPLE " + jwt
        )

        redacted = redact_text(text)

        for secret in (
            "bearer-secret-123", "basic-secret-456", "cookie-secret-789",
            "other-secret", "access-secret", "refresh-secret", "state-secret",
            "client-secret", "oauth-secret", "AKIAIOSFODNN7EXAMPLE", jwt,
        ):
            self.assertNotIn(secret, redacted)
        self.assertIn("Authorization:", redacted)
        self.assertIn("Cookie:", redacted)

    def test_health_snapshot_does_not_persist_secret_bearing_message(self):
        with tempfile.TemporaryDirectory() as directory:
            db = DatabaseManager(str(Path(directory) / "accounts.db"))
            self.assertTrue(db.save_account("health@example.test", "account-pass-123"))

            snapshot = {
                "browser_status": "authenticated",
                "mailbox_status": "active",
                "status": "degraded",
                "message": "proxy password=hidden-secret-123; token=otp-987654",
                "last_error_code": "password=hidden-secret-123",
            }
            self.assertTrue(db.update_health_snapshot("health@example.test", snapshot))

            row = db.get_all_accounts()[0]
            self.assertNotIn("hidden-secret-123", row["notes"])
            self.assertNotIn("otp-987654", row["notes"])
            self.assertLessEqual(len(row["notes"]), 512)
            self.assertEqual(row["last_error_code"], "")

    def test_generic_database_logs_and_session_stats_redact_free_form_values(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "accounts.db"
            db = DatabaseManager(str(path))
            secret = "unbound-log-secret-739"
            db.log_event("ERROR", "Authorization: Bearer " + secret)
            db.save_session_stats(
                total_attempts=1, successes=0, failures=1,
                strategies_used={"standard": 1},
                errors={"RuntimeError": "password=" + secret},
                duration_seconds=1,
            )
            with sqlite3.connect(path) as conn:
                rows = conn.execute(
                    "SELECT message FROM execution_logs UNION ALL SELECT errors FROM session_stats"
                ).fetchall()
            serialized = json.dumps(rows)
            self.assertNotIn(secret, serialized)

    def test_account_status_does_not_persist_unbounded_secret_bearing_notes(self):
        with tempfile.TemporaryDirectory() as directory:
            db = DatabaseManager(str(Path(directory) / "accounts.db"))
            self.assertTrue(db.save_account("status@example.test", "account-pass-456"))

            note = "password=legacy-db-secret token=legacy-token-456 " + ("x" * 900)
            self.assertTrue(db.update_account_status("status@example.test", "locked", note))

            row = db.get_all_accounts()[0]
            self.assertNotIn("legacy-db-secret", row["notes"])
            self.assertNotIn("legacy-token-456", row["notes"])
            self.assertLessEqual(len(row["notes"]), 512)

            self.assertTrue(db.update_account_status(
                "status@example.test", "locked",
                "otp=112233 verification_code=445566 sms_code=778899 "
                "verificationCode=990011",
            ))
            row = db.get_all_accounts()[0]
            for code in ("112233", "445566", "778899", "990011"):
                self.assertNotIn(code, row["notes"])

    def test_health_snapshot_reduces_unknown_statuses_to_protocol_values(self):
        with tempfile.TemporaryDirectory() as directory:
            db = DatabaseManager(str(Path(directory) / "accounts.db"))
            self.assertTrue(db.save_account("status-code@example.test", "account-pass-789"))

            self.assertTrue(db.update_health_snapshot("status-code@example.test", {
                "browser_status": "password=browser-secret",
                "mailbox_status": "token=mailbox-secret",
                "status": "https://user:pass@example.test/health",
                "message": "safe diagnostic",
            }))

            row = db.get_all_accounts()[0]
            self.assertEqual(row["browser_status"], "error")
            self.assertEqual(row["mailbox_status"], "error")
            self.assertEqual(row["overall_status"], "error")

    def test_account_status_protocol_does_not_store_free_form_secret_text(self):
        with tempfile.TemporaryDirectory() as directory:
            db = DatabaseManager(str(Path(directory) / "accounts.db"))
            self.assertTrue(db.save_account(
                "status-protocol@example.test", "account-pass-000",
                status="token=creation-secret",
            ))
            self.assertTrue(db.update_account_status(
                "status-protocol@example.test", "password=runtime-secret",
            ))

            row = db.get_all_accounts()[0]
            self.assertEqual(row["status"], "error")

    def test_account_status_rejects_markup_even_when_it_has_no_credential_label(self):
        with tempfile.TemporaryDirectory() as directory:
            db = DatabaseManager(str(Path(directory) / "accounts.db"))
            self.assertTrue(db.save_account("markup-status@example.test", "account-pass-002"))
            self.assertTrue(db.update_account_status(
                "markup-status@example.test", "<script>alert(1)</script>",
            ))
            self.assertEqual(db.get_all_accounts()[0]["status"], "error")

    def test_account_creation_health_fields_use_the_same_protocol_allowlists(self):
        with tempfile.TemporaryDirectory() as directory:
            db = DatabaseManager(str(Path(directory) / "accounts.db"))
            self.assertTrue(db.save_account(
                "creation-health@example.test", "account-pass-003",
                browser_status="token=browser-secret",
                mailbox_status="password=mailbox-secret",
                overall_status="<b>active</b>",
            ))
            row = db.get_all_accounts()[0]
            self.assertEqual(row["browser_status"], "error")
            self.assertEqual(row["mailbox_status"], "error")
            self.assertEqual(row["overall_status"], "active")

    def test_health_snapshot_rejects_secret_bearing_timestamp_values(self):
        with tempfile.TemporaryDirectory() as directory:
            db = DatabaseManager(str(Path(directory) / "accounts.db"))
            self.assertTrue(db.save_account("timestamp@example.test", "account-pass-001"))
            self.assertTrue(db.update_health_snapshot("timestamp@example.test", {
                "browser_status": "authenticated",
                "mailbox_status": "active",
                "status": "active",
                "checked_at": "password=timestamp-secret",
                "browser_checked_at": "token=timestamp-token",
                "mailbox_checked_at": "https://user:pass@example.test",
            }))

            row = db.get_all_accounts()[0]
            serialized = " ".join(str(row.get(field, "")) for field in (
                "browser_checked_at", "mailbox_checked_at",
            ))
            self.assertNotIn("timestamp-secret", serialized)
            self.assertNotIn("timestamp-token", serialized)
            self.assertNotIn("user:pass", serialized)


class VoiceBoundaryTests(unittest.TestCase):
    @staticmethod
    def _load_voice(config):
        module_name = "tested_voice_boundary_%s" % id(config)
        spec = importlib.util.spec_from_file_location(module_name, ROOT / "services/voice.py")
        module = importlib.util.module_from_spec(spec)
        speech = types.ModuleType("speech_recognition")
        pydub = types.SimpleNamespace(AudioSegment=Mock())
        with patch.dict(sys.modules, {
            "config.settings": types.SimpleNamespace(Config=config),
            "speech_recognition": speech,
            "pydub": pydub,
        }), patch("os.makedirs"):
            spec.loader.exec_module(module)
        return module

    def test_missing_or_default_token_rejects_both_routes(self):
        for configured in ("", "changeme"):
            with self.subTest(configured=repr(configured)):
                module = self._load_voice(types.SimpleNamespace(VOICE_SERVER_TOKEN=configured))
                client = module.app.test_client()
                self.assertEqual(client.get("/otp").status_code, 401)
                self.assertEqual(client.post("/voice").status_code, 401)

    def test_non_ascii_configured_token_is_compared_without_server_error(self):
        configured = "令牌-secret-安全"
        module = self._load_voice(types.SimpleNamespace(VOICE_SERVER_TOKEN=configured))
        client = module.app.test_client()
        response = client.get("/otp?token=" + quote(configured, safe=""))
        self.assertEqual(response.status_code, 200)

    def test_task_launch_predicate_rejects_blank_and_case_variant_defaults(self):
        for value in ("", "   ", "changeme", " CHANGEme ", None):
            with self.subTest(value=repr(value)):
                self.assertFalse(_voice_token_is_configured(value))
        self.assertTrue(_voice_token_is_configured("voice-secret"))

    def test_server_defaults_to_loopback(self):
        module = self._load_voice(types.SimpleNamespace(VOICE_SERVER_TOKEN="voice-secret"))
        waitress = types.SimpleNamespace(serve=Mock())
        with patch.dict(sys.modules, {"waitress": waitress}), \
             patch.dict(os.environ, {"VOICE_SERVER_HOST": ""}, clear=False):
            module.run_server()
        self.assertEqual(waitress.serve.call_args.kwargs["host"], "127.0.0.1")


class TelegramBoundaryTests(unittest.TestCase):
    def test_failure_notification_redacts_fields_and_escapes_markup(self):
        notifier = TelegramNotifier("bot-token", "chat-id")
        with patch("core.telegram_notifier.requests.post") as post:
            post.return_value.status_code = 200
            self.assertTrue(notifier.notify_account_failed(
                "victim@example.test",
                "password=hidden-pass-123",
                "<i>evil</i><a href=\"https://attacker.test/?token=raw-token\">x</a>",
            ))

        payload = post.call_args.kwargs["json"]
        message = payload["text"]
        self.assertNotIn("hidden-pass-123", message)
        self.assertNotIn("raw-token", message)
        self.assertIn("&lt;i&gt;evil&lt;/i&gt;", message)
        self.assertIn("&lt;a href=", message)
        self.assertIn("<b>Account Failed</b>", message)
        self.assertNotIn("[redacted]]", message)


class RuntimeLoggingBoundaryTests(unittest.TestCase):
    def test_provider_exception_payload_is_not_written_to_logs(self):
        secret = "password=provider-secret-123 token=provider-token-456"
        with patch(
            "core.captcha_solver.requests.post",
            side_effect=RuntimeError(secret),
        ), self.assertLogs("gmail_creator_captcha", level="ERROR") as captured:
            self.assertIsNone(
                CaptchaSolver._solve_anticaptcha("site-key", "https://local.test")
            )

        rendered = "\n".join(captured.output)
        self.assertNotIn(secret, rendered)
        self.assertNotIn("provider-secret-123", rendered)
        self.assertNotIn("provider-token-456", rendered)
        self.assertIn("RuntimeError", rendered)


class TelegramBoundaryContinuationTests(unittest.TestCase):
    def test_generic_send_redacts_secrets_and_does_not_forward_arbitrary_html(self):
        notifier = TelegramNotifier("bot-token", "chat-id")
        with patch("core.telegram_notifier.requests.post") as post:
            post.return_value.status_code = 200
            self.assertTrue(notifier.send(
                "<a href=\"https://attacker.test/?token=raw-token\">"
                "password=hidden-pass-456</a>"
            ))

        message = post.call_args.kwargs["json"]["text"]
        self.assertNotIn("hidden-pass-456", message)
        self.assertNotIn("raw-token", message)
        self.assertIn("&lt;a href=", message)


if __name__ == "__main__":
    unittest.main()
