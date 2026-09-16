import re
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]


class RunnerSecretSinkTests(unittest.TestCase):
    """Regression coverage for browser-runner output boundaries."""

    def _source(self, relative):
        return (ROOT / relative).read_text(encoding="utf-8")

    def test_target_loggers_do_not_interpolate_dynamic_urls_or_dom_payloads(self):
        forbidden = {
            "core/phone_bypass.py": (
                r"logger\.[a-z]+\([^\n]*\{(?:url|bypass_url|result|service_name)\b",
            ),
            "core/runners.py": (
                r"logger\.[a-z]+\([^\n]*\{(?:url|current_url|method)\b",
                r"logger\.[a-z]+\([^\n]*warm_result",
            ),
            "core/selenium_runner.py": (
                r"logger\.[a-z]+\([^\n]*warm_result",
            ),
            "core/stealth_browser.py": (
                r"logger\.[a-z]+\([^\n]*(?:select_info|result|option_clicked|selector)\b",
            ),
            "core/trust_builder.py": (
                r"logger\.[a-z]+\([^\n]*\{(?:ip|url)\b",
                r"logger\.[a-z]+\([^\n]*info\.get\(['\"]city",
            ),
        }
        for relative, patterns in forbidden.items():
            source = self._source(relative)
            for pattern in patterns:
                with self.subTest(module=relative, pattern=pattern):
                    self.assertIsNone(re.search(pattern, source))

    def test_runner_persistence_uses_bounded_strategy_and_sms_labels(self):
        runners = self._source("core/runners.py")
        selenium = self._source("core/selenium_runner.py")
        self.assertIn("flow_mode = normalize_flow_mode(flow_mode)", runners)
        self.assertNotIn('sms_service=method if "sms" in method else ""', runners)
        self.assertIn("normalize_sms_service(method[4:])", runners)
        self.assertIn("mode = normalize_flow_mode(mode)", selenium)
        self.assertNotIn('strategy=f"selenium_{mode}"', selenium)

    def test_warm_result_summary_is_structured_and_drops_free_form_values(self):
        from core.secret_safety import safe_warm_result_summary

        result = safe_warm_result_summary({
            "success": False,
            "error_code": "password=warm-secret-123",
            "message": "https://user:warm-secret-123@example.test/path",
            "last_activity_error": "token=warm-token-456",
            "email": "account@example.test",
            "profile_id": "profile-one",
        })

        self.assertEqual(set(result), {"success", "error_code", "cleanup_status",
                                       "browser_process_stopped", "lease_released"})
        serialized = repr(result)
        self.assertNotIn("warm-secret-123", serialized)
        self.assertNotIn("warm-token-456", serialized)
        self.assertEqual(result["error_code"], "error")

    def test_sms_service_and_verification_labels_are_finite(self):
        from core.secret_safety import normalize_sms_service, normalize_verification_method

        self.assertEqual(normalize_sms_service("5sim"), "5sim")
        self.assertEqual(normalize_sms_service("password=sms-secret-123"), "")
        self.assertEqual(normalize_verification_method("sms_5sim"), "sms_5sim")
        self.assertEqual(normalize_verification_method("token=method-secret-123"), "unknown")

    def test_network_trust_log_contains_no_provider_identity(self):
        from core import trust_builder

        ip_secret = "198.51.100.77"
        city_secret = "private-city"
        first = Mock()
        first.json.return_value = {"ip": ip_secret}
        second = Mock()
        second.json.return_value = {"city": city_secret, "country": "ZZ", "org": "Example ISP"}
        with patch.object(trust_builder.requests, "get", side_effect=[first, second]), \
             self.assertLogs("gmail_creator_trust", level="INFO") as captured:
            trust_builder.network_trust_check()

        rendered = "\n".join(captured.output)
        self.assertNotIn(ip_secret, rendered)
        self.assertNotIn(city_secret, rendered)


if __name__ == "__main__":
    unittest.main()
