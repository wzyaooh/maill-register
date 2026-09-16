"""Fail closed unless both real localhost adapter smoke tests succeed."""

import os
import sys
import unittest


REQUIRED_TEST_IDS = frozenset({
    "tests.test_browser_real_smoke.RealBrowserSmokeTests.test_playwright_manifest_identity_storage_and_process_lifecycle",
    "tests.test_browser_real_smoke.RealBrowserSmokeTests.test_selenium_manifest_identity_storage_and_process_lifecycle",
})


class _ExecutionResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.successful_test_ids = set()

    def addSuccess(self, test):  # noqa: N802 - unittest result API
        super().addSuccess(test)
        self.successful_test_ids.add(test.id())


def run_suite(suite, required_test_ids=REQUIRED_TEST_IDS, stream=None):
    """Execute tests and return a process status, not a source-text heuristic."""
    stream = sys.stderr if stream is None else stream
    result = unittest.TextTestRunner(
        verbosity=2, stream=stream, resultclass=_ExecutionResult,
    ).run(suite)
    missing = set(required_test_ids) - result.successful_test_ids
    if not result.wasSuccessful() or result.skipped or missing:
        print("Required browser smoke gate failed: both adapters must execute successfully without skips.", file=stream)
        return 1
    return 0


def main():
    # Set the opt-in before loading the module's unittest decorators.
    os.environ["RUN_REAL_BROWSER_SMOKE"] = "1"
    suite = unittest.defaultTestLoader.loadTestsFromName("tests.test_browser_real_smoke")
    return run_suite(suite)


if __name__ == "__main__":
    sys.exit(main())
