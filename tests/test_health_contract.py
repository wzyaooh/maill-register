import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.health_checker import AccountHealthChecker


class HealthContractTests(unittest.TestCase):
    def test_returns_independent_browser_and_mailbox_facts(self):
        checker = AccountHealthChecker()
        with patch.object(checker, "_probe_browser", return_value=("profile_busy", "lease busy", "profile_busy")), \
             patch.object(checker, "_check_mailbox", return_value=("active", "IMAP ok", "")):
            result = checker.check_single("user@example.test", "secret", profile_id="p1", engine="playwright")
        self.assertEqual(result["browser_status"], "profile_busy")
        self.assertEqual(result["mailbox_status"], "active")
        self.assertEqual(result["status"], "degraded")
        self.assertEqual(result["last_error_code"], "profile_busy")
        self.assertIn("browser_checked_at", result)
        self.assertIn("mailbox_checked_at", result)

    def test_mailbox_password_error_is_not_overwritten_by_browser(self):
        checker = AccountHealthChecker()
        with patch.object(checker, "_probe_browser", return_value=("authenticated", "browser ok", "")), \
             patch.object(checker, "_check_mailbox", return_value=("password_changed", "bad password", "password_changed")):
            result = checker.check_single("user@example.test", "secret", profile_id="p1", engine="selenium")
        self.assertEqual(result["browser_status"], "authenticated")
        self.assertEqual(result["mailbox_status"], "password_changed")
        self.assertEqual(result["status"], "password_changed")

    def test_profileless_account_still_checks_imap(self):
        checker = AccountHealthChecker()
        with patch.object(checker, "_check_mailbox", return_value=("active", "IMAP ok", "")) as mailbox:
            result = checker.check_single("legacy@example.test", "secret")
        mailbox.assert_called_once()
        self.assertEqual(result["browser_status"], "not_configured")
        self.assertEqual(result["mailbox_status"], "active")
        self.assertEqual(result["status"], "active")

    def test_summary_does_not_count_degraded_as_active(self):
        results = [
            {"status": "active"}, {"status": "degraded"},
            {"status": "locked"}, {"status": "network_error"},
        ]
        summary = AccountHealthChecker.get_summary(results)
        self.assertEqual(summary["active"], 1)
        self.assertEqual(summary["degraded"], 1)
        self.assertEqual(summary["total"], 4)
        self.assertEqual(summary["health_rate"], 25.0)

    def test_profileless_proxy_is_not_silently_ignored(self):
        checker = AccountHealthChecker()
        with patch.object(checker, "_check_mailbox", return_value=("active", "IMAP ok", "")):
            result = checker.check_single(
                "legacy@example.test", "secret", proxy="proxy.example.test:8443"
            )
        self.assertEqual(result["browser_status"], "profile_conflict")
        self.assertEqual(result["status"], "degraded")
        self.assertEqual(result["last_error_code"], "profile_conflict")


if __name__ == "__main__":
    unittest.main()
