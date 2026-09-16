"""Exercise real unittest outcomes, including mandatory-test skips."""

import io
import unittest

try:
    from tools.run_browser_smoke import run_suite
except ImportError:
    run_suite = None


def fixture_case(outcome):
    class Fixture(unittest.TestCase):
        def test_playwright(self):
            if outcome == "skip":
                self.skipTest("runtime missing")
            if outcome == "failure":
                self.fail("fixture failure")
            if outcome == "error":
                raise RuntimeError("fixture error")
            if outcome == "subtest-skip":
                with self.subTest(runtime="fixture"):
                    self.skipTest("partial smoke missing")

        def test_selenium(self):
            pass

    return Fixture


class BrowserSmokeGateTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(run_suite, "Required browser execution gate is not implemented")

    def exercise(self, outcome, missing=False):
        case = fixture_case(outcome)
        required = {case("test_playwright").id(), case("test_selenium").id()}
        suite = unittest.TestSuite([case("test_selenium")]) if missing else unittest.defaultTestLoader.loadTestsFromTestCase(case)
        stream = io.StringIO()
        return run_suite(suite, required_test_ids=required, stream=stream)

    def test_successful_executed_required_tests_pass(self):
        self.assertEqual(self.exercise("success"), 0)

    def test_skipped_required_test_fails_gate(self):
        self.assertNotEqual(self.exercise("skip"), 0)

    def test_absent_required_test_fails_gate(self):
        self.assertNotEqual(self.exercise("success", missing=True), 0)

    def test_required_subtest_skip_fails_gate(self):
        self.assertNotEqual(self.exercise("subtest-skip"), 0)

    def test_failures_and_errors_fail_gate(self):
        for outcome in ("failure", "error"):
            with self.subTest(outcome=outcome):
                self.assertNotEqual(self.exercise(outcome), 0)

    def test_successful_required_tests_cannot_hide_other_failures(self):
        case = fixture_case("success")
        failing = fixture_case("failure")
        required = {case("test_selenium").id()}
        suite = unittest.TestSuite([case("test_selenium"), failing("test_playwright")])
        self.assertNotEqual(run_suite(suite, required_test_ids=required, stream=io.StringIO()), 0)


if __name__ == "__main__":
    unittest.main()
