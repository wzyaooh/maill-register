import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from core.secret_safety import safe_account_metadata, sanitize_operation_value
from core.session_resume import SessionManager
from web.app import _mask_proxy_resource


class SecretBoundaryHardeningTests(unittest.TestCase):
    def test_retry_operation_exception_payload_never_reaches_result_or_attempt(self):
        from core.job_ledger import JobLedger
        from core.retry_engine import RetryEngine
        from web.worker import _run_retryable_operation

        payloads = (
            "opaque-provider-material-543",
            '{"diagnostic":"opaque-provider-material-543"}',
            "opaque-provider-material-543\nsecond-line",
        )
        for action in ("health", "warm", "compensation"):
            for payload in payloads:
                with self.subTest(action=action, payload=payload), tempfile.TemporaryDirectory() as directory:
                    ledger = JobLedger(str(Path(directory) / "database.db"))
                    job = ledger.create_job(action)
                    with patch.object(RetryEngine, "MAX_RETRIES", 1):
                        result = _run_retryable_operation(
                            action, Mock(side_effect=RuntimeError(payload)),
                            ledger=ledger, job_id=job["job_id"], subject="local-operation",
                        )

                    self.assertFalse(result["success"])
                    self.assertNotIn("message", result)
                    attempts = ledger.list_attempts(job["job_id"])
                    self.assertEqual([item["state"] for item in attempts], ["failed"])
                    self.assertNotIn("opaque-provider-material-543",
                                     json.dumps(result) + json.dumps(attempts))

    def test_redact_text_masks_unlabelled_auth_and_key_material_shapes(self):
        text = (
            "cookie=cookie-secret-739 session=session-secret-740 "
            "session_id=session-id-secret-741 bearer=bearer-secret-742 "
            "authorization_header=header-secret-743 "
            "x_api_key=api-secret-744 oauth2_token=oauth-secret-745 "
            "private_key=private-secret-746"
        )

        redacted = __import__("core.secret_safety", fromlist=["redact_text"]).redact_text(text)

        for secret in (
            "cookie-secret-739", "session-secret-740", "session-id-secret-741",
            "bearer-secret-742", "header-secret-743", "api-secret-744",
            "oauth-secret-745", "private-secret-746",
        ):
            self.assertNotIn(secret, redacted)

    def test_structured_unlabelled_auth_keys_are_removed(self):
        value = {
            "cookie": "cookie-secret-739",
            "session": "session-secret-740",
            "session_id": "session-id-secret-741",
            "bearer": "bearer-secret-742",
            "authorization_header": "header-secret-743",
            "x_api_key": "api-secret-744",
            "oauth2_token": "oauth-secret-745",
            "private_key": "private-secret-746",
            "safe_status": "active",
        }

        safe = sanitize_operation_value(value)

        for key in (
            "cookie", "session", "session_id", "bearer", "authorization_header",
            "x_api_key", "oauth2_token", "private_key",
        ):
            self.assertNotIn(key, safe)
        self.assertEqual(safe["safe_status"], "active")
    def test_camel_case_cookie_session_and_integer_otp_fields_are_removed(self):
        value = {
            "authToken": "token-secret",
            "verificationCode": 246810,
            "otpCode": 135790,
            "cookieValue": "cookie-secret",
            "sessionState": {"SID": "cookie-secret"},
            "sessionId": "session-secret",
            "code": 246810,
            "statusCode": 200,
        }

        safe = sanitize_operation_value(value)

        for key in (
            "authToken", "verificationCode", "otpCode", "cookieValue",
            "sessionState", "sessionId", "code",
        ):
            self.assertNotIn(key, safe)
        self.assertEqual(safe["statusCode"], 200)

    def test_safe_account_metadata_enumizes_unknown_error_codes(self):
        unknown = safe_account_metadata({
            "id": 1,
            "email": "account@example.test",
            "last_error_code": "password=account-secret-739",
        })
        known = safe_account_metadata({
            "id": 2,
            "email": "known@example.test",
            "last_error_code": "profile_busy",
        })

        self.assertEqual(unknown["last_error_code"], "")
        self.assertEqual(known["last_error_code"], "profile_busy")
        self.assertNotIn("account-secret-739", json.dumps(unknown))

    def test_proxy_comment_lines_are_masked_before_resource_response(self):
        content = (
            "# proxy.example.test:8080:comment-user:comment-secret-739\n"
            "# https://comment-user:comment-secret-739@example.test:8080\n"
            "# password=comment-secret-739\n"
        )

        masked = _mask_proxy_resource(content)

        self.assertNotIn("comment-user", masked)
        self.assertNotIn("comment-secret-739", masked)
        self.assertEqual(masked.count("\n"), 3)

    def test_session_state_is_sanitized_on_write_and_legacy_read(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.json"
            manager = SessionManager(str(path))
            manager.save_state(
                batch_config={
                    "num_accounts": 1,
                    "password": "account-secret-739",
                    "authToken": "token-secret-739",
                },
                completed_indices=[0],
                results={
                    "successes": 1,
                    "failures": 0,
                    "cookieValue": "cookie-secret-739",
                },
            )

            raw = path.read_text(encoding="utf-8")
            self.assertNotIn("account-secret-739", raw)
            self.assertNotIn("token-secret-739", raw)
            self.assertNotIn("cookie-secret-739", raw)
            loaded = manager.load_state()
            self.assertNotIn("password", loaded["batch_config"])
            self.assertNotIn("authToken", loaded["batch_config"])
            self.assertNotIn("cookieValue", loaded["results"])

            # A pre-existing session file must be scrubbed before callers use
            # it, too; otherwise the persistence boundary is only half closed.
            path.write_text(json.dumps({
                "saved_at": "now",
                "batch_config": {
                    "num_accounts": 1,
                    "proxy": "proxy.example.test:8080:user:legacy-secret-739",
                },
                "completed_indices": [],
                "results": {"successes": 0, "failures": 0},
            }), encoding="utf-8")
            loaded = manager.load_state()
            self.assertNotIn("proxy", loaded["batch_config"])
            self.assertNotIn("legacy-secret-739", json.dumps(loaded))


if __name__ == "__main__":
    unittest.main()
