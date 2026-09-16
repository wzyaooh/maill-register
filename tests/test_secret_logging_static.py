import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class SecretLoggingStaticTests(unittest.TestCase):
    """Keep exception payloads and tracebacks out of operational log sinks."""

    MODULES = (
        "core/android_creator.py",
        "core/behavior.py",
        "core/captcha_solver.py",
        "core/cookie_reaper.py",
        "core/fingerprint.py",
        "core/proxy_fetcher.py",
        "core/trust_builder.py",
        "core/warmup.py",
        "core/account_warmer.py",
        "core/batch_runner.py",
        "core/creation_flow.py",
        "core/phone_bypass.py",
        "core/profile_runtime.py",
        "core/runners.py",
        "core/selenium_runner.py",
        "web/server.py",
    )

    def test_logger_calls_do_not_interpolate_exception_payloads_or_tracebacks(self):
        raw_exception_fstring = re.compile(
            r"logger\.(?:debug|info|warning|error|exception)\([^\n]*"
            r"f[\"'][^\n]*\{(?:e|exc|error|exception)\b"
        )
        for relative in self.MODULES:
            source = (ROOT / relative).read_text(encoding="utf-8")
            with self.subTest(module=relative):
                self.assertNotIn("exc_info=True", source)
                self.assertIsNone(raw_exception_fstring.search(source))
