import unittest

from core.browser_capabilities import (
    CAPABILITY_KEYS,
    CAPABILITY_STATUSES,
    get_capability_matrix,
    get_engine_capabilities,
    capability_status,
    capability_report,
)


class BrowserCapabilityMatrixTests(unittest.TestCase):
    def test_matrix_covers_both_engines_and_every_identity_dimension(self):
        matrix = get_capability_matrix()
        self.assertEqual(set(matrix), {"playwright", "selenium"})
        self.assertEqual(set(CAPABILITY_KEYS), {
            "user_agent", "viewport", "locale", "accept_language",
            "timezone", "geolocation", "browser_channel_version",
            "persistent_storage", "proxy_endpoint", "proxy_credentials",
        })
        for engine in matrix:
            self.assertEqual(set(matrix[engine]), set(CAPABILITY_KEYS))
            for status in matrix[engine].values():
                self.assertIn(status, CAPABILITY_STATUSES)

    def test_matrix_records_native_and_explicitly_unsupported_contracts(self):
        self.assertEqual(capability_status("playwright", "user_agent"), "native")
        self.assertEqual(capability_status("playwright", "proxy_credentials"), "native")
        self.assertEqual(capability_status("selenium", "persistent_storage"), "native")
        self.assertEqual(capability_status("selenium", "proxy_credentials"), "unsupported")
        self.assertEqual(capability_status("selenium", "timezone"), "best_effort")

    def test_unknown_engine_or_capability_fails_closed(self):
        with self.assertRaises(ValueError):
            get_engine_capabilities("appium")
        with self.assertRaises(ValueError):
            capability_status("playwright", "cookies")

    def test_report_marks_unverified_observations_without_claiming_support(self):
        report = capability_report(
            "playwright", observed={"user_agent": True, "timezone": False}
        )
        self.assertEqual(report["user_agent"]["status"], "native")
        self.assertTrue(report["user_agent"]["verified"])
        self.assertEqual(report["timezone"]["status"], "native")
        self.assertFalse(report["timezone"]["verified"])
        self.assertEqual(report["viewport"]["status"], "native")
        self.assertFalse(report["viewport"]["verified"])

    def test_report_includes_reason_and_never_verifies_unsupported_capabilities(self):
        report = capability_report(
            "selenium", observed={"proxy_credentials": True, "timezone": True}
        )
        self.assertFalse(report["proxy_credentials"]["verified"])
        self.assertTrue(report["proxy_credentials"]["reason"])
        self.assertTrue(report["timezone"]["verified"])
        self.assertTrue(report["timezone"]["reason"])

    def test_report_rejects_non_boolean_observation_evidence(self):
        with self.assertRaises(ValueError):
            capability_report("playwright", observed={"timezone": "false"})

    def test_returned_matrix_is_a_copy(self):
        matrix = get_capability_matrix()
        matrix["playwright"]["user_agent"] = "unsupported"
        self.assertEqual(capability_status("playwright", "user_agent"), "native")


if __name__ == "__main__":
    unittest.main()
