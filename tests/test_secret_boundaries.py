import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from core.account_manager import AccountManager
from core.secret_safety import is_sensitive_key, redact_text, sanitize_operation_value
from core.telegram_notifier import TelegramNotifier
from core.proxy_manager import ProxyManager
from web.tasks import TaskStore


class SecretBoundaryTests(unittest.TestCase):
    def test_sensitive_key_matching_keeps_non_secret_metadata(self):
        for key in ("proxy_status", "proxy_count", "tokenizer", "cookie_policy"):
            self.assertFalse(is_sensitive_key(key), key)
        for key in ("password", "pass", "auth_password", "request_token", "proxy_auth"):
            self.assertTrue(is_sensitive_key(key), key)

    def test_structured_mapping_keys_cannot_carry_proxy_credentials(self):
        value = {
            "host:80:user:proxy-secret": 50,
            "https://user:proxy-secret@example.test:80": 1,
            "raw-secret-token": "value",
            "proxy_status": "healthy",
        }
        sanitized = sanitize_operation_value(value, secrets=("proxy-secret",))
        self.assertNotIn("host:80:user:proxy-secret", sanitized)
        self.assertNotIn("https://user:proxy-secret@example.test:80", sanitized)
        self.assertNotIn("raw-secret-token", sanitized)
        self.assertEqual(sanitized["proxy_status"], "healthy")

    def test_redact_text_covers_proxy_spellings_without_replacing_short_words(self):
        text = (
            "host:80:user:proxy-secret user:proxy-secret@example.test:80 "
            "socks5://user:proxy-secret@example.test:80 password=a alpha"
        )
        redacted = redact_text(text, secrets=("a",))
        self.assertNotIn("proxy-secret", redacted)
        self.assertIn("alpha", redacted)
        self.assertIn("password=[redacted]", redacted)
        self.assertNotIn("abcd123", redact_text("raw abcd123 output", secrets=("abcd123",)))

    def test_task_store_sanitizes_legacy_params_and_custom_proxy_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config").mkdir()
            custom_proxy = root / "config" / "custom-proxies.txt"
            custom_proxy.write_text("proxy.example.test:8080:user:custom-proxy-secret\n", encoding="utf-8")
            os.environ["PROXY_FILE"] = "config/custom-proxies.txt"
            self.addCleanup(os.environ.pop, "PROXY_FILE", None)
            store = TaskStore(root / "data" / "web")
            task = store.create("validate", {"password": "legacy-password", "proxy": "custom-proxy-secret"})
            with store.connect() as conn:
                conn.execute(
                    "UPDATE tasks SET params=? WHERE id=?",
                    (json.dumps({"password": "legacy-password", "proxy": "custom-proxy-secret", "ok": True}), task["id"]),
                )
            decoded = store.get(task["id"])
            self.assertNotIn("password", decoded["params"])
            self.assertNotIn("proxy", decoded["params"])
            self.assertNotIn("custom-proxy-secret", json.dumps(decoded))

    def test_task_store_logs_redact_before_tail_cut(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data" / "web").mkdir(parents=True)
            secret = "cross-boundary-secret-123456789"
            (root / "config").mkdir()
            (root / "config" / "password.txt").write_text(secret + "\n", encoding="utf-8")
            store = TaskStore(root / "data" / "web")
            task = store.create("validate", {})
            path = store.directory / (task["id"] + ".log")
            path.write_text("x" * (128 * 1024 - 5) + secret + "\n", encoding="utf-8")
            self.assertNotIn(secret, store.logs(task["id"]))

    def test_otp_fields_are_removed_but_normal_status_code_is_kept(self):
        value = sanitize_operation_value({
            "otp": "246810",
            "verification_code": "135790",
            "code": "112233",
            "error_code": "profile_busy",
            "status_code": 200,
        })
        self.assertNotIn("otp", value)
        self.assertNotIn("verification_code", value)
        self.assertNotIn("code", value)
        self.assertEqual(value["error_code"], "profile_busy")
        self.assertEqual(value["status_code"], 200)

    def test_operation_sanitizer_removes_camel_case_numeric_otp_and_session_fields(self):
        value = sanitize_operation_value({
            "verificationCode": "135790",
            "smsCode": 246810,
            "code": 987654,
            "cookieValue": "session-cookie-secret",
            "cookie_value": "session-cookie-secret-2",
            "sessionId": "session-id-secret",
            "session_id": "session-id-secret-2",
            "statusCode": 200,
        })

        for key in (
            "verificationCode", "smsCode", "code", "cookieValue",
            "cookie_value", "sessionId", "session_id",
        ):
            self.assertNotIn(key, value)
        self.assertEqual(value["statusCode"], 200)

    def test_redact_text_masks_signed_url_query_parameters_without_known_secrets(self):
        text = (
            "recording=https://media.example.test/call.mp3?sig=sig-secret-123"
            "&signature=signature-secret-456&key=key-secret-000"
            "&X-Amz-Signature=aws-secret-789&X-Amz-Credential=aws-key-111"
        )

        redacted = redact_text(text)

        self.assertNotIn("sig-secret-123", redacted)
        self.assertNotIn("signature-secret-456", redacted)
        self.assertNotIn("key-secret-000", redacted)
        self.assertNotIn("aws-secret-789", redacted)
        self.assertNotIn("aws-key-111", redacted)

    def test_creation_notification_does_not_send_password_or_proxy(self):
        notifier = TelegramNotifier("bot-token", "chat-id")
        notifier.send = Mock(return_value=True)

        notifier.notify_account_created(
            email="created@example.test",
            password="account-password-secret",
            strategy="standard",
            proxy="proxy.test:8000:proxy-user:proxy-password-secret",
        )

        message = notifier.send.call_args.args[0]
        self.assertIn("created@example.test", message)
        self.assertIn("standard", message)
        self.assertNotIn("account-password-secret", message)
        self.assertNotIn("proxy-password-secret", message)
        self.assertNotIn("Password", message)
        self.assertNotIn("Proxy", message)

    def test_account_manager_exports_metadata_only(self):
        account = {
            "id": 1,
            "email": "export@example.test",
            "password": "account-password-secret",
            "proxy": "proxy.test:8000:proxy-user:proxy-password-secret",
            "phone_number": "+15550000101",
            "first_name": "Export",
            "last_name": "Test",
            "strategy": "standard",
            "status": "active",
            "created_at": "2026-09-12 00:00:00",
            "profile_id": "profile-one",
            "engine": "playwright",
            "profile_state": "ready",
            "identity_state": "native",
            "browser_status": "authenticated",
            "mailbox_status": "active",
            "overall_status": "active",
        }
        manager = AccountManager.__new__(AccountManager)
        manager.db = Mock(get_all_accounts=Mock(return_value=[account]))

        with tempfile.TemporaryDirectory() as directory:
            json_path = str(Path(directory) / "accounts.json")
            csv_path = str(Path(directory) / "accounts.csv")
            txt_path = str(Path(directory) / "accounts.txt")
            manager.export_json(json_path)
            manager.export_csv(csv_path)
            manager.export_txt(txt_path)

            json_content = Path(json_path).read_text(encoding="utf-8")
            csv_content = Path(csv_path).read_text(encoding="utf-8")
            txt_content = Path(txt_path).read_text(encoding="utf-8")

        for content in (json_content, csv_content, txt_content):
            self.assertNotIn("account-password-secret", content)
            self.assertNotIn("proxy-password-secret", content)
            self.assertNotIn("proxy-user", content)
        exported = json.loads(json_content)
        self.assertEqual(exported[0]["email"], "export@example.test")
        self.assertNotIn("password", exported[0])
        self.assertNotIn("proxy", exported[0])

    def test_proxy_pool_stats_use_safe_endpoint_labels(self):
        manager = ProxyManager.__new__(ProxyManager)
        manager._static_proxies = ["proxy.test:8000:proxy-user:proxy-password-secret"]
        manager._kooip_proxies = []
        manager._health = {manager._static_proxies[0]: True}
        manager._scores = {manager._static_proxies[0]: 50}
        manager._sources = {manager._static_proxies[0]: "static"}
        stats = manager.get_stats()
        serialized = json.dumps(stats)
        self.assertNotIn("proxy-password-secret", serialized)
        self.assertNotIn("proxy-user", serialized)
        self.assertEqual(list(stats["scores"]), ["http://proxy.test:8000"])


if __name__ == "__main__":
    unittest.main()
