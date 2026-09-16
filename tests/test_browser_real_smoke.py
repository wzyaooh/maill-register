"""Opt-in local smoke tests for the two supported browser adapters.

These tests deliberately never contact Gmail or a provider.  They require an
explicit environment opt-in because launching a real browser is machine-level
work, and they skip with a clear reason when the local runtime is unavailable.
"""

import asyncio
import http.server
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from core.profile_runtime import ProfileRuntime


def _version_major(output):
    """Extract a browser/driver major from a version command output."""
    text = str(output or "")
    match = re.search(r"(?<!\d)(\d+)(?:\.\d+){1,3}(?!\d)", text)
    return match.group(1) if match else ""


def _binary_major(path, label):
    """Read one local binary's major without starting a browser session."""
    try:
        result = subprocess.run(
            [path, "--version"], capture_output=True, text=True,
            timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise unittest.SkipTest(
            "%s version could not be inspected (%s)" % (label, type(exc).__name__)
        )
    output = "\n".join((result.stdout or "", result.stderr or ""))
    major = _version_major(output)
    if not major:
        raise unittest.SkipTest("%s version output is unavailable" % label)
    return major


class RealBrowserSmokeSupportTests(unittest.TestCase):
    def test_version_parser_accepts_browser_and_driver_outputs(self):
        self.assertEqual(_version_major("Google Chrome 140.0.7339.12"), "140")
        self.assertEqual(_version_major("ChromeDriver 140.0.7339.24 (abc)"), "140")

    def test_version_parser_rejects_unverifiable_output(self):
        self.assertEqual(_version_major("Google Chrome for Testing"), "")

    def test_binary_major_reads_stdout_and_stderr(self):
        with patch(
            "tests.test_browser_real_smoke.subprocess.run",
            return_value=types.SimpleNamespace(
                stdout="", stderr="ChromeDriver 140.0.7339.24", returncode=0,
            ),
        ):
            self.assertEqual(_binary_major("/tmp/chromedriver", "ChromeDriver"), "140")


class _SmokeHandler(http.server.BaseHTTPRequestHandler):
    requests = []

    def do_GET(self):  # noqa: N802 - stdlib handler API
        type(self).requests.append({
            "path": self.path,
            "accept_language": self.headers.get("Accept-Language", ""),
            "user_agent": self.headers.get("User-Agent", ""),
        })
        body = """<!doctype html>
<meta charset="utf-8"><title>local browser smoke</title>
<main data-smoke="fixture">local fixture</main>
<script>window.localStorage.setItem('smoke-seed', 'persisted-v1');</script>
"""
        encoded = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *_args):
        return


class _SmokeServer:
    def __init__(self):
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _SmokeHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return "http://127.0.0.1:%s/" % self.server.server_port

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


@unittest.skipUnless(
    os.environ.get("RUN_REAL_BROWSER_SMOKE") == "1",
    "set RUN_REAL_BROWSER_SMOKE=1 to run local real-browser smoke tests",
)
class RealBrowserSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = _SmokeServer()

    @classmethod
    def tearDownClass(cls):
        cls.server.close()

    def setUp(self):
        _SmokeHandler.requests.clear()

    def _assert_runtime_matches_manifest(self, runtime, handle, runtime_info):
        channel = str((runtime_info or {}).get("channel") or "").strip().lower()
        major = str((runtime_info or {}).get("major_version") or "").strip()
        if not channel or not major:
            raise unittest.SkipTest("browser runtime did not report channel and major version")
        runtime.record_runtime(handle, {"channel": channel, "major_version": major})
        recorded = (runtime.load(handle).get("browser") or {})
        self.assertEqual(recorded.get("channel"), channel)
        self.assertEqual(recorded.get("major_version"), major)

    def _assert_accept_language(self, locale):
        expected = str(locale or "").strip().lower()
        requests = [item for item in _SmokeHandler.requests if item.get("path") == "/"]
        self.assertTrue(requests, "local fixture did not receive the browser request")
        actual = str(requests[-1].get("accept_language") or "").strip().lower()
        self.assertTrue(
            actual.startswith(expected),
            "Accept-Language %r does not preserve locale %r" % (actual, expected),
        )

    def _runtime(self, engine):
        directory = tempfile.TemporaryDirectory(prefix=".real-browser-smoke-")
        self.addCleanup(directory.cleanup)
        runtime = ProfileRuntime(directory.name)
        handle = runtime.provision("smoke@example.test", engine)
        # The smoke uses an installed/bundled runtime explicitly.  Recording
        # it while provisioning mirrors registration's first runtime binding;
        # later opens are strict about the same channel.
        runtime.record_runtime(
            handle, {"channel": "chromium" if engine == "playwright" else "chrome", "major_version": ""}
        )
        runtime.bind(handle, "smoke@example.test")
        runtime.mark_ready(handle)
        return runtime, handle

    @staticmethod
    def _assert_playwright_available():
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise unittest.SkipTest("Playwright package is not installed") from exc

        async def inspect_runtime():
            controller = await async_playwright().start()
            try:
                executable = Path(controller.chromium.executable_path)
                return executable if executable.is_file() else None
            finally:
                await controller.stop()

        executable = asyncio.run(inspect_runtime())
        if executable is None:
            raise unittest.SkipTest("Playwright Chromium executable is not installed")

    @staticmethod
    def _selenium_paths():
        driver = os.environ.get("CHROMEDRIVER_PATH") or shutil.which("chromedriver")
        browser = os.environ.get("CHROME_BINARY")
        if not browser:
            candidates = (
                shutil.which("google-chrome"),
                shutil.which("google-chrome-stable"),
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                "/Applications/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
            )
            browser = next((item for item in candidates if item and os.path.isfile(item)), None)
        if not driver or not os.path.isfile(driver):
            raise unittest.SkipTest(
                "Selenium smoke requires an explicit CHROMEDRIVER_PATH or chromedriver on PATH"
            )
        if not browser or not os.path.isfile(browser):
            raise unittest.SkipTest(
                "Selenium smoke requires CHROME_BINARY or an installed Chrome binary"
            )
        driver_major = _binary_major(driver, "ChromeDriver")
        browser_major = _binary_major(browser, "Chrome")
        if driver_major != browser_major:
            raise unittest.SkipTest(
                "Chrome/ChromeDriver major versions differ (%s vs %s)"
                % (browser_major, driver_major)
            )
        return driver, browser

    def test_playwright_manifest_identity_storage_and_process_lifecycle(self):
        self._assert_playwright_available()
        from config.settings import Config
        from core.stealth_browser import PlaywrightStealthManager

        runtime, handle = self._runtime("playwright")
        expected = runtime.load(handle)["identity"]
        url = self.server.url

        async def exercise():
            manager = PlaywrightStealthManager()
            with runtime.lease(handle, "real-smoke") as lease:
                try:
                    manifest = runtime.load(handle)
                    initialized = await manager.initialize(
                        profile_path=str(handle.path), profile_manifest=manifest,
                        lease_owned=True, profile_lease=lease, purpose="health",
                    )
                    self.assertTrue(initialized)
                    self._assert_runtime_matches_manifest(
                        runtime, handle, manager.get_runtime_info()
                    )
                    await manager.page.goto(url, wait_until="domcontentloaded")
                    observed = await manager.page.evaluate("""async () => {
                        const supported = Boolean(navigator.geolocation);
                        const position = await new Promise((resolve) => {
                          if (!supported) return resolve(null);
                          const timer = setTimeout(() => resolve(null), 1500);
                          navigator.geolocation.getCurrentPosition(
                            p => { clearTimeout(timer); resolve({latitude: p.coords.latitude, longitude: p.coords.longitude}); },
                            () => { clearTimeout(timer); resolve(null); }, {timeout: 1000}
                          );
                        });
                        return {
                          user_agent: navigator.userAgent,
                          viewport: {width: innerWidth, height: innerHeight},
                          locale: navigator.language,
                          intl_locale: Intl.DateTimeFormat().resolvedOptions().locale,
                          timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
                          geolocation_supported: supported,
                          geolocation: position,
                          storage: localStorage.getItem('smoke-seed'),
                        };
                    }""")
                    self.assertEqual(observed["user_agent"], expected["user_agent"])
                    self.assertEqual(observed["viewport"], expected["viewport"])
                    self.assertTrue(
                        observed["locale"].lower().startswith(expected["locale"].lower()),
                        "navigator.language %r does not preserve manifest locale %r"
                        % (observed["locale"], expected["locale"]),
                    )
                    self.assertTrue(
                        observed["intl_locale"].lower().startswith(expected["locale"].lower()),
                        "Intl locale %r does not preserve manifest locale %r"
                        % (observed["intl_locale"], expected["locale"]),
                    )
                    self.assertEqual(observed["timezone"], expected["timezone_id"])
                    self.assertTrue(observed["geolocation_supported"])
                    if observed["geolocation"] is not None:
                        self.assertAlmostEqual(observed["geolocation"]["latitude"], expected["geolocation"]["latitude"], places=2)
                        self.assertAlmostEqual(observed["geolocation"]["longitude"], expected["geolocation"]["longitude"], places=2)
                    self.assertEqual(observed["storage"], "persisted-v1")
                    self._assert_accept_language(expected["locale"])
                finally:
                    close_result = await manager.close()
                    self.assertTrue(close_result["success"])
                    self.assertTrue(close_result["browser_process_stopped"])

            # A second lease proves the lock was released only after shutdown.
            manager = PlaywrightStealthManager()
            with runtime.lease(handle, "real-smoke-reopen") as lease:
                try:
                    await manager.initialize(
                        profile_path=str(handle.path), profile_manifest=runtime.load(handle),
                        lease_owned=True, profile_lease=lease, purpose="health",
                    )
                    self._assert_runtime_matches_manifest(
                        runtime, handle, manager.get_runtime_info()
                    )
                    await manager.page.goto(url, wait_until="domcontentloaded")
                    self.assertEqual(await manager.page.evaluate("localStorage.getItem('smoke-seed')"), "persisted-v1")
                finally:
                    close_result = await manager.close()
                    self.assertTrue(close_result["browser_process_stopped"])

        with patch.object(Config, "HEADLESS_MODE", True):
            try:
                asyncio.run(exercise())
            except Exception as exc:
                message = str(exc).lower()
                if any(token in message for token in ("executable doesn't exist", "browser was not found", "could not find")):
                    self.skipTest("Playwright browser executable is unavailable: " + type(exc).__name__)
                raise

    def test_selenium_manifest_identity_storage_and_process_lifecycle(self):
        driver_path, browser_path = self._selenium_paths()
        from config.settings import Config
        from core import selenium_runner
        from selenium.webdriver.chrome.service import Service as ChromeService

        runtime, handle = self._runtime("selenium")
        expected = runtime.load(handle)["identity"]
        url = self.server.url

        def exercise():
            driver = None
            with runtime.lease(handle, "real-smoke") as lease:
                try:
                    driver = selenium_runner.create_driver(
                        profile_path=str(handle.path), profile_manifest=runtime.load(handle),
                        lease_owned=True, profile_lease=lease, purpose="health",
                    )
                    if driver is None:
                        self.skipTest("Selenium driver could not be created")
                    self._assert_runtime_matches_manifest(
                        runtime, handle, selenium_runner.get_runtime_info(driver)
                    )
                    try:
                        driver.execute_cdp_cmd("Browser.grantPermissions", {
                            "origin": url.rstrip("/"), "permissions": ["geolocation"],
                        })
                    except Exception:
                        pass
                    driver.get(url)
                    observed = driver.execute_async_script("""const done = arguments[0];
                        const supported = Boolean(navigator.geolocation);
                        if (!supported) return done({
                          user_agent: navigator.userAgent,
                          viewport: {width: innerWidth, height: innerHeight},
                          locale: navigator.language,
                          timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
                          geolocation_supported: false,
                          geolocation: null,
                          storage: localStorage.getItem('smoke-seed')
                        });
                        const timer = setTimeout(() => done({
                          user_agent: navigator.userAgent,
                          viewport: {width: innerWidth, height: innerHeight},
                          locale: navigator.language,
                          timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
                          geolocation_supported: true,
                          geolocation: null,
                          storage: localStorage.getItem('smoke-seed')
                        }), 1500);
                        navigator.geolocation.getCurrentPosition((p) => {
                          clearTimeout(timer);
                          done({
                            user_agent: navigator.userAgent,
                            viewport: {width: innerWidth, height: innerHeight},
                            locale: navigator.language,
                            timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
                            geolocation_supported: true,
                            geolocation: {latitude: p.coords.latitude, longitude: p.coords.longitude},
                            storage: localStorage.getItem('smoke-seed')
                          });
                        }, () => {
                          clearTimeout(timer);
                          done({
                            user_agent: navigator.userAgent,
                            viewport: {width: innerWidth, height: innerHeight},
                            locale: navigator.language,
                            timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
                            geolocation_supported: true,
                            geolocation: null,
                            storage: localStorage.getItem('smoke-seed')
                          });
                        }, {timeout: 1000});""")
                    observed["intl_locale"] = driver.execute_script(
                        "return Intl.DateTimeFormat().resolvedOptions().locale"
                    )
                    self.assertEqual(observed["user_agent"], expected["user_agent"])
                    self.assertEqual(observed["viewport"], expected["viewport"])
                    self.assertTrue(
                        observed["locale"].lower().startswith(expected["locale"].lower()),
                        "navigator.language %r does not preserve manifest locale %r"
                        % (observed["locale"], expected["locale"]),
                    )
                    self.assertTrue(
                        observed["intl_locale"].lower().startswith(expected["locale"].lower()),
                        "Intl locale %r does not preserve manifest locale %r"
                        % (observed["intl_locale"], expected["locale"]),
                    )
                    self.assertEqual(observed["timezone"], expected["timezone_id"])
                    self.assertTrue(observed["geolocation_supported"])
                    if observed["geolocation"] is not None:
                        self.assertAlmostEqual(observed["geolocation"]["latitude"], expected["geolocation"]["latitude"], places=2)
                        self.assertAlmostEqual(observed["geolocation"]["longitude"], expected["geolocation"]["longitude"], places=2)
                    self.assertEqual(observed["storage"], "persisted-v1")
                    self._assert_accept_language(expected["locale"])
                    process = getattr(getattr(driver, "service", None), "process", None)
                    self.assertIsNotNone(process, "Selenium service process must be observable")
                finally:
                    process = getattr(getattr(driver, "service", None), "process", None) if driver else None
                    if driver is not None:
                        driver.quit()
                    if process is not None:
                        deadline = time.time() + 5
                        while process.poll() is None and time.time() < deadline:
                            time.sleep(0.05)
                        self.assertIsNotNone(process.poll(), "owned ChromeDriver process did not stop")

            # Reacquiring the same lease is the lifecycle proof for the second
            # adapter as well; storage must survive the close/reopen boundary.
            with runtime.lease(handle, "real-smoke-reopen") as lease:
                reopened = None
                try:
                    reopened = selenium_runner.create_driver(
                        profile_path=str(handle.path), profile_manifest=runtime.load(handle),
                        lease_owned=True, profile_lease=lease, purpose="health",
                    )
                    self.assertIsNotNone(reopened)
                    self._assert_runtime_matches_manifest(
                        runtime, handle, selenium_runner.get_runtime_info(reopened)
                    )
                    reopened.get(url)
                    self.assertEqual(reopened.execute_script("return localStorage.getItem('smoke-seed')"), "persisted-v1")
                finally:
                    process = getattr(getattr(reopened, "service", None), "process", None) if reopened else None
                    if reopened is not None:
                        reopened.quit()
                    if process is not None:
                        deadline = time.time() + 5
                        while process.poll() is None and time.time() < deadline:
                            time.sleep(0.05)
                        self.assertIsNotNone(process.poll())

        original_options = selenium_runner.ChromeOptions

        def configured_options():
            options = original_options()
            options.binary_location = browser_path
            return options

        with patch.object(Config, "HEADLESS_MODE", True), \
             patch.object(
                 selenium_runner, "_get_chrome_service",
                 return_value=ChromeService(driver_path),
             ), \
             patch.object(selenium_runner, "ChromeOptions", side_effect=configured_options):
            try:
                result = exercise()
            except Exception as exc:
                message = str(exc).lower()
                if any(token in message for token in ("cannot find", "not found", "session not created")):
                    self.skipTest("Selenium browser/driver is unavailable: " + type(exc).__name__)
                raise


if __name__ == "__main__":
    unittest.main()
