import asyncio
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, Mock, call, patch

from core.profile_runtime import (
    ProfileConflictError,
    ProfileRuntimeMismatchError,
    ProxyUnavailableError,
    build_identity,
    proxy_binding,
)


class BrowserAdapterIdentityTests(unittest.TestCase):
    def test_playwright_warm_requires_manifest_and_persistent_profile_path(self):
        from core import stealth_browser

        manager = stealth_browser.PlaywrightStealthManager()
        with patch.object(stealth_browser, "async_playwright",
                          side_effect=AssertionError("warm must not start a temporary browser")):
            with self.assertRaises(ProfileConflictError):
                asyncio.run(manager.initialize(
                    purpose="warm", lease_owned=True,
                ))

    def test_selenium_health_requires_manifest_and_persistent_profile_path(self):
        from core import selenium_runner

        with patch.object(selenium_runner, "webdriver",
                          side_effect=AssertionError("health must not start a temporary browser")):
            with self.assertRaises(ProfileConflictError):
                selenium_runner.create_driver(purpose="health", lease_owned=True)

    def test_playwright_warm_rejects_incomplete_manifest_identity(self):
        from core import stealth_browser

        manifest = {
            "profile_id": "adapter-profile", "engine": "playwright",
            "identity": {},
            "browser": {"channel": "chrome", "major_version": "123"},
            "network": {"bound": False, "endpoint_hash": ""},
        }
        manager = stealth_browser.PlaywrightStealthManager()
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(stealth_browser, "async_playwright",
                          side_effect=AssertionError("invalid identity must fail before launch")):
            with self.assertRaises(ProfileConflictError):
                asyncio.run(manager.initialize(
                    profile_path=directory, profile_manifest=manifest,
                    purpose="warm", lease_owned=True,
                ))

    def test_selenium_warm_rejects_incomplete_manifest_identity(self):
        from core import selenium_runner

        manifest = {
            "profile_id": "adapter-profile", "engine": "selenium",
            "identity": {},
            "browser": {"channel": "chrome", "major_version": "123"},
            "network": {"bound": False, "endpoint_hash": ""},
        }
        webdriver = Mock()
        with patch.object(selenium_runner, "webdriver", webdriver), \
             tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ProfileConflictError):
                selenium_runner.create_driver(
                    profile_path=directory, profile_manifest=manifest,
                    purpose="warm", lease_owned=True,
                )
        webdriver.Chrome.assert_not_called()

    def test_playwright_manifest_identity_is_forwarded_without_random_defaults(self):
        identity = build_identity("adapter-profile", "playwright")
        manifest = {"profile_id": "adapter-profile", "engine": "playwright",
                    "identity": identity, "browser": {"channel": "chrome", "major_version": "123"},
                    "network": {"bound": False, "endpoint_hash": ""}}
        calls = {}

        class Context:
            async def new_page(self):
                return types.SimpleNamespace(add_init_script=AsyncMock())
            async def close(self):
                pass

        class Chromium:
            async def launch_persistent_context(self, path, **kwargs):
                calls.update(path=path, kwargs=kwargs)
                return Context()

        class PW:
            chromium = Chromium()
            async def start(self):
                return self
            async def stop(self):
                pass

        class Manager:
            pass

        from core import stealth_browser
        fake = lambda: types.SimpleNamespace(start=AsyncMock(return_value=PW()))
        manager = stealth_browser.PlaywrightStealthManager()
        # Avoid optional warmup/stealth integrations in this unit contract and
        # patch the already-imported adapter dependency instead of replacing
        # playwright.async_api globally (which can poison playwright_stealth).
        with patch.object(stealth_browser, "async_playwright", fake), \
             patch.object(stealth_browser, "Config", types.SimpleNamespace(
                 HEADLESS_MODE=True, ENABLE_SESSION_WARMING=False,
                 ENABLE_POLTERGEIST=False, ENABLE_GHOST_TYPER=False,
                 ENABLE_COOKIE_REAPER=False,
             )), tempfile.TemporaryDirectory() as directory:
            self.assertTrue(asyncio.run(manager.initialize(
                profile_path=directory, profile_manifest=manifest,
                purpose="health", lease_owned=True,
            )))
        self.assertEqual(calls["kwargs"]["user_agent"], identity["user_agent"])
        self.assertEqual(calls["kwargs"]["viewport"], identity["viewport"])
        self.assertEqual(calls["kwargs"]["timezone_id"], identity["timezone_id"])

    def test_selenium_manifest_identity_is_forwarded(self):
        identity = build_identity("adapter-profile", "selenium")
        manifest = {"profile_id": "adapter-profile", "engine": "selenium",
                    "identity": identity, "browser": {"channel": "chrome", "major_version": "123"},
                    "network": {"bound": False, "endpoint_hash": ""}}
        options = Mock()
        options.add_argument = Mock()
        options.add_experimental_option = Mock()
        from core import selenium_runner
        webdriver = Mock()
        webdriver.Chrome.return_value = types.SimpleNamespace(
            execute_cdp_cmd=Mock(), set_page_load_timeout=Mock(), get=Mock(), quit=Mock(),
            capabilities={"browserVersion": "123.0.0.0"},
        )
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(selenium_runner, "ChromeOptions", return_value=options), \
             patch.object(selenium_runner, "webdriver", webdriver), \
             patch.object(selenium_runner, "_get_chrome_service", return_value=Mock()), \
             patch.object(selenium_runner.time, "sleep"):
            selenium_runner.create_driver(profile_path=directory,
                                           profile_manifest=manifest, purpose="health",
                                           lease_owned=True)
        args = [call.args[0] for call in options.add_argument.call_args_list]
        self.assertIn("user-agent=" + identity["user_agent"], args)
        self.assertIn("--window-size=%s,%s" % (identity["viewport"]["width"], identity["viewport"]["height"]), args)

    def test_selenium_applies_manifest_viewport_before_first_navigation(self):
        """The manifest describes the content viewport, not Chrome's outer window."""
        from core import selenium_runner

        identity = build_identity("adapter-profile", "selenium")
        identity["viewport"] = {"width": 1600, "height": 720}
        identity["is_mobile"] = False
        manifest = {
            "profile_id": "adapter-profile",
            "engine": "selenium",
            "identity": identity,
            "browser": {"channel": "chrome", "major_version": "123"},
            "network": {"bound": False, "endpoint_hash": ""},
        }
        options = Mock()
        driver = Mock()
        driver.capabilities = {"browserVersion": "123.0.0.0"}
        webdriver = Mock()
        webdriver.Chrome.return_value = driver

        with tempfile.TemporaryDirectory() as directory, \
             patch.object(selenium_runner, "ChromeOptions", return_value=options), \
             patch.object(selenium_runner, "webdriver", webdriver), \
             patch.object(selenium_runner, "_get_chrome_service", return_value=Mock()), \
             patch.object(selenium_runner.time, "sleep"):
            self.assertIs(
                selenium_runner.create_driver(
                    profile_path=directory,
                    profile_manifest=manifest,
                    purpose="registration",
                    lease_owned=True,
                ),
                driver,
            )

        metrics_call = call.execute_cdp_cmd(
            "Emulation.setDeviceMetricsOverride",
            {
                "width": 1600,
                "height": 720,
                "deviceScaleFactor": 1,
                "mobile": False,
            },
        )
        self.assertIn(metrics_call, driver.method_calls)
        self.assertLess(
            driver.method_calls.index(metrics_call),
            driver.method_calls.index(call.get("https://www.google.com")),
        )

    def test_selenium_applies_manifest_locale_before_first_navigation(self):
        """Chrome UI language alone must not leak the host locale into JS or HTTP."""
        from core import selenium_runner

        identity = build_identity("adapter-profile", "selenium")
        identity["locale"] = "en-CA"
        manifest = {
            "profile_id": "adapter-profile",
            "engine": "selenium",
            "identity": identity,
            "browser": {"channel": "chrome", "major_version": "123"},
            "network": {"bound": False, "endpoint_hash": ""},
        }
        options = Mock()
        driver = Mock()
        driver.capabilities = {"browserVersion": "123.0.0.0"}
        webdriver = Mock()
        webdriver.Chrome.return_value = driver

        with tempfile.TemporaryDirectory() as directory, \
             patch.object(selenium_runner, "ChromeOptions", return_value=options), \
             patch.object(selenium_runner, "webdriver", webdriver), \
             patch.object(selenium_runner, "_get_chrome_service", return_value=Mock()), \
             patch.object(selenium_runner.time, "sleep"):
            self.assertIs(
                selenium_runner.create_driver(
                    profile_path=directory,
                    profile_manifest=manifest,
                    purpose="registration",
                    lease_owned=True,
                ),
                driver,
            )

        options.add_experimental_option.assert_any_call(
            "prefs", {"intl.accept_languages": "en-CA,en"}
        )
        locale_call = call.execute_cdp_cmd(
            "Emulation.setLocaleOverride", {"locale": "en-CA"}
        )
        self.assertIn(locale_call, driver.method_calls)
        self.assertLess(
            driver.method_calls.index(locale_call),
            driver.method_calls.index(call.get("https://www.google.com")),
        )

    def test_selenium_rejects_manifest_bound_to_playwright(self):
        """An adapter must not be able to open a profile owned by another engine."""
        from core import selenium_runner

        manifest = {
            "profile_id": "adapter-profile",
            "engine": "playwright",
            "identity": build_identity("adapter-profile", "playwright"),
            "browser": {"channel": "chrome", "major_version": "123"},
            "network": {"bound": False, "endpoint_hash": ""},
        }
        webdriver = Mock()
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(selenium_runner, "webdriver", webdriver):
            with self.assertRaises(ProfileConflictError):
                selenium_runner.create_driver(
                    profile_path=directory,
                    profile_manifest=manifest,
                    purpose="warm",
                    lease_owned=True,
                )
        webdriver.Chrome.assert_not_called()

    def test_playwright_manifest_locale_is_forwarded_to_native_options(self):
        """A persisted locale must not be replaced by the registration default."""
        from core import stealth_browser

        identity = build_identity("adapter-profile", "playwright")
        identity["locale"] = "en-GB"
        manifest = {
            "profile_id": "adapter-profile",
            "engine": "playwright",
            "identity": identity,
            "browser": {"channel": "chrome", "major_version": "123"},
            "network": {"bound": False, "endpoint_hash": ""},
        }
        calls = {}

        class Page:
            async def add_init_script(self, *_args, **_kwargs):
                pass

        class Context:
            async def new_page(self):
                return Page()

            async def close(self):
                pass

        class Chromium:
            async def launch_persistent_context(self, path, **kwargs):
                calls.update(path=path, kwargs=kwargs)
                return Context()

        class Playwright:
            chromium = Chromium()

            async def stop(self):
                pass

        class AsyncPlaywright:
            async def start(self):
                return Playwright()

        with patch.object(stealth_browser, "async_playwright",
                          return_value=AsyncPlaywright()), \
             patch.object(stealth_browser, "stealth_async", None), \
             patch.object(stealth_browser, "Config", types.SimpleNamespace(
                 HEADLESS_MODE=True, ENABLE_SESSION_WARMING=False,
                 ENABLE_POLTERGEIST=False, ENABLE_GHOST_TYPER=False,
                 ENABLE_COOKIE_REAPER=False,
             )), tempfile.TemporaryDirectory() as directory:
            manager = stealth_browser.PlaywrightStealthManager()
            self.assertTrue(asyncio.run(manager.initialize(
                profile_path=directory,
                profile_manifest=manifest,
                purpose="health",
                lease_owned=True,
            )))
            asyncio.run(manager.close())

        self.assertIn("--lang=en-GB", calls["kwargs"]["args"])
        self.assertEqual(
            calls["kwargs"]["extra_http_headers"]["Accept-Language"],
            "en-GB,en;q=0.9",
        )

    def test_selenium_final_driver_attempt_is_closed_on_setup_failure(self):
        """A driver created on the final retry must not outlive the lease."""
        from core import selenium_runner

        identity = build_identity("adapter-profile", "selenium")
        manifest = {"profile_id": "adapter-profile", "engine": "selenium",
                    "identity": identity, "browser": {"channel": "chrome", "major_version": "123"},
                    "network": {"bound": False, "endpoint_hash": ""}}
        options = Mock()
        options.add_argument = Mock()
        options.add_experimental_option = Mock()
        drivers = []

        class Driver:
            capabilities = {"browserVersion": "123.0.0.0"}

            def __init__(self):
                self.quit_calls = 0

            def execute_cdp_cmd(self, *_args, **_kwargs):
                return None

            def set_page_load_timeout(self, *_args, **_kwargs):
                raise RuntimeError("driver setup failed")

            def quit(self):
                self.quit_calls += 1

        webdriver = Mock()
        def make_driver(*_args, **_kwargs):
            driver = Driver()
            drivers.append(driver)
            return driver
        webdriver.Chrome.side_effect = make_driver

        with tempfile.TemporaryDirectory() as directory, \
             patch.object(selenium_runner, "ChromeOptions", return_value=options), \
             patch.object(selenium_runner, "webdriver", webdriver), \
             patch.object(selenium_runner, "_get_chrome_service", return_value=Mock()), \
             patch.object(selenium_runner.time, "sleep"):
            result = selenium_runner.create_driver(
                profile_path=directory, profile_manifest=manifest,
                purpose="warm", lease_owned=True,
            )

        self.assertIsNone(result)
        self.assertEqual(len(drivers), 3)
        self.assertTrue(all(driver.quit_calls == 1 for driver in drivers))

    def test_manifest_identity_path_does_not_call_random_fallbacks(self):
        from core import stealth_browser

        identity = build_identity("adapter-profile", "playwright")
        with patch.object(stealth_browser.random, "choice",
                          side_effect=AssertionError("manifest launch must not randomize identity")):
            profile = stealth_browser._build_context_profile(False, identity)

        self.assertEqual(profile["context_options"]["user_agent"], identity["user_agent"])
        self.assertEqual(profile["context_options"]["viewport"], identity["viewport"])
        self.assertEqual(profile["context_options"]["timezone_id"], identity["timezone_id"])

    def test_selenium_never_starts_direct_when_bound_proxy_needs_auth(self):
        from core import selenium_runner

        proxy = "proxy.example.test:8443:user:secret"
        manifest = {
            "profile_id": "adapter-profile",
            "engine": "selenium",
            "identity": build_identity("adapter-profile", "selenium"),
            "browser": {"channel": "chrome", "major_version": ""},
            "network": proxy_binding(proxy),
        }
        options = Mock()
        options.add_argument = Mock()
        options.add_experimental_option = Mock()
        webdriver = Mock()
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(selenium_runner, "ChromeOptions", return_value=options), \
             patch.object(selenium_runner, "webdriver", webdriver), \
             patch.object(selenium_runner, "_get_chrome_service", return_value=Mock()):
            with self.assertRaises(ProxyUnavailableError):
                selenium_runner.create_driver(
                    proxy=proxy, profile_path=directory,
                    profile_manifest=manifest, purpose="warm", lease_owned=True,
                )

        webdriver.Chrome.assert_not_called()

    def test_selenium_probe_mode_does_not_navigate_before_runtime_validation(self):
        from core import selenium_runner

        identity = build_identity("adapter-profile", "selenium")
        manifest = {
            "profile_id": "adapter-profile", "engine": "selenium",
            "identity": identity,
            "browser": {"channel": "chrome", "major_version": "123"},
            "network": {"bound": False, "endpoint_hash": ""},
        }
        options = Mock()
        options.add_argument = Mock()
        options.add_experimental_option = Mock()
        driver = types.SimpleNamespace(
            execute_cdp_cmd=Mock(), set_page_load_timeout=Mock(),
            get=Mock(), quit=Mock(), capabilities={"browserVersion": "123.0.0.0"},
        )
        webdriver = Mock()
        webdriver.Chrome.return_value = driver
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(selenium_runner, "ChromeOptions", return_value=options), \
             patch.object(selenium_runner, "webdriver", webdriver), \
             patch.object(selenium_runner, "_get_chrome_service", return_value=Mock()), \
             patch.object(selenium_runner.time, "sleep"):
            self.assertIs(selenium_runner.create_driver(
                profile_path=directory, profile_manifest=manifest,
                purpose="health", lease_owned=True,
            ), driver)
        driver.get.assert_not_called()

    def test_selenium_rejects_runtime_major_mismatch_before_returning_driver(self):
        from core import selenium_runner

        identity = build_identity("adapter-profile", "selenium")
        manifest = {
            "profile_id": "adapter-profile", "engine": "selenium",
            "identity": identity,
            "browser": {"channel": "chrome", "major_version": "123"},
            "network": {"bound": False, "endpoint_hash": ""},
        }
        options = Mock()
        driver = Mock()
        driver.capabilities = {"browserVersion": "124.0.0.0"}
        webdriver = Mock()
        webdriver.Chrome.return_value = driver
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(selenium_runner, "ChromeOptions", return_value=options), \
             patch.object(selenium_runner, "webdriver", webdriver), \
             patch.object(selenium_runner, "_get_chrome_service", return_value=Mock()), \
             patch.object(selenium_runner.time, "sleep"):
            with self.assertRaises(ProfileRuntimeMismatchError) as raised:
                selenium_runner.create_driver(
                    profile_path=directory, profile_manifest=manifest,
                    purpose="health", lease_owned=True,
                )

        self.assertEqual(raised.exception.code, "runtime_mismatch")
        driver.quit.assert_called_once()

    def test_playwright_rejects_runtime_major_mismatch_before_returning_manager(self):
        from core import stealth_browser

        identity = build_identity("adapter-profile", "playwright")
        manifest = {
            "profile_id": "adapter-profile", "engine": "playwright",
            "identity": identity,
            "browser": {"channel": "chrome", "major_version": "123"},
            "network": {"bound": False, "endpoint_hash": ""},
        }

        class Page:
            async def add_init_script(self, *_args, **_kwargs):
                pass

        class Context:
            browser = types.SimpleNamespace(version="124.0.0")

            async def new_page(self):
                return Page()

            async def close(self):
                self.closed = True

        class Chromium:
            async def launch_persistent_context(self, _path, **_kwargs):
                return Context()

        class Playwright:
            chromium = Chromium()

            async def stop(self):
                self.stopped = True

        class AsyncPlaywright:
            async def start(self):
                return Playwright()

        with patch.object(stealth_browser, "async_playwright", return_value=AsyncPlaywright()), \
             patch.object(stealth_browser, "stealth_async", None), \
             patch.object(stealth_browser, "Config", types.SimpleNamespace(
                 HEADLESS_MODE=True, ENABLE_SESSION_WARMING=False,
                 ENABLE_POLTERGEIST=False, ENABLE_GHOST_TYPER=False,
                 ENABLE_COOKIE_REAPER=False,
             )), tempfile.TemporaryDirectory() as directory:
            manager = stealth_browser.PlaywrightStealthManager()
            with self.assertRaises(ProfileRuntimeMismatchError) as raised:
                asyncio.run(manager.initialize(
                    profile_path=directory, profile_manifest=manifest,
                    purpose="health", lease_owned=True,
                ))

        self.assertEqual(raised.exception.code, "runtime_mismatch")

    def test_playwright_warm_does_not_fallback_when_recorded_channel_fails(self):
        """A bound Chrome profile must not be reopened with bundled Chromium."""
        from core import stealth_browser

        identity = build_identity("adapter-profile", "playwright")
        manifest = {
            "profile_id": "adapter-profile", "engine": "playwright",
            "identity": identity,
            "browser": {"channel": "chrome", "major_version": "120"},
            "network": {"bound": False, "endpoint_hash": ""},
        }
        calls = []

        class Chromium:
            async def launch_persistent_context(self, path, **kwargs):
                calls.append((path, kwargs))
                raise RuntimeError("recorded Chrome is unavailable")

        class Playwright:
            chromium = Chromium()

            async def stop(self):
                pass

        class AsyncPlaywright:
            async def start(self):
                return Playwright()

        with patch.object(stealth_browser, "async_playwright", return_value=AsyncPlaywright()), \
             patch.object(stealth_browser, "Config", types.SimpleNamespace(
                 HEADLESS_MODE=True, ENABLE_SESSION_WARMING=False,
                 ENABLE_POLTERGEIST=False, ENABLE_GHOST_TYPER=False,
                 ENABLE_COOKIE_REAPER=False,
             )), tempfile.TemporaryDirectory() as directory:
            manager = stealth_browser.PlaywrightStealthManager()
            with self.assertRaises(ProfileRuntimeMismatchError) as raised:
                asyncio.run(manager.initialize(
                    profile_path=directory, profile_manifest=manifest,
                    purpose="warm", lease_owned=True,
                ))

        self.assertEqual(raised.exception.code, "runtime_mismatch")
        self.assertEqual(len(calls), 1)
        self.assertIn("channel", calls[0][1])
        self.assertEqual(calls[0][1]["channel"], "chrome")

    def test_adopted_unknown_channel_may_establish_bundled_runtime(self):
        from core import stealth_browser

        identity = build_identity("adapter-profile", "playwright")
        manifest = {
            "profile_id": "adapter-profile", "engine": "playwright",
            "identity": identity,
            "browser": {"channel": "", "major_version": ""},
            "network": {"bound": False, "endpoint_hash": ""},
        }
        calls = []

        class Page:
            async def add_init_script(self, *_args, **_kwargs):
                pass

        class Context:
            async def new_page(self):
                return Page()

        class Chromium:
            async def launch_persistent_context(self, path, **kwargs):
                calls.append((path, kwargs))
                if len(calls) == 1:
                    raise RuntimeError("system Chrome is unavailable")
                return Context()

        class Playwright:
            chromium = Chromium()

            async def stop(self):
                pass

        class AsyncPlaywright:
            async def start(self):
                return Playwright()

        with patch.object(stealth_browser, "async_playwright", return_value=AsyncPlaywright()), \
             patch.object(stealth_browser, "stealth_async", None), \
             patch.object(stealth_browser, "Config", types.SimpleNamespace(
                 HEADLESS_MODE=True, ENABLE_SESSION_WARMING=False,
                 ENABLE_POLTERGEIST=False, ENABLE_GHOST_TYPER=False,
                 ENABLE_COOKIE_REAPER=False,
             )), tempfile.TemporaryDirectory() as directory:
            manager = stealth_browser.PlaywrightStealthManager()
            self.assertTrue(asyncio.run(manager.initialize(
                profile_path=directory, profile_manifest=manifest,
                purpose="warm", lease_owned=True,
            )))
            runtime_info = manager.get_runtime_info()
            asyncio.run(manager.close())

        self.assertEqual(len(calls), 2)
        self.assertEqual(runtime_info["channel"], "chromium")

    def test_registration_fallback_records_bundled_chromium_channel(self):
        """Only provisioning may fallback, and it records the actual channel."""
        from core import stealth_browser

        identity = build_identity("adapter-profile", "playwright")
        manifest = {
            "profile_id": "adapter-profile", "engine": "playwright",
            "identity": identity,
            "browser": {"channel": "chrome", "major_version": ""},
            "network": {"bound": False, "endpoint_hash": ""},
        }
        calls = []

        class Page:
            async def add_init_script(self, *_args, **_kwargs):
                pass

        class Context:
            async def new_page(self):
                return Page()

        class Chromium:
            async def launch_persistent_context(self, path, **kwargs):
                calls.append((path, kwargs))
                if len(calls) == 1:
                    raise RuntimeError("recorded Chrome is unavailable")
                return Context()

        class Playwright:
            chromium = Chromium()

            async def stop(self):
                pass

        class AsyncPlaywright:
            async def start(self):
                return Playwright()

        with patch.object(stealth_browser, "async_playwright", return_value=AsyncPlaywright()), \
             patch.object(stealth_browser, "stealth_async", None), \
             patch.object(stealth_browser, "Config", types.SimpleNamespace(
                 HEADLESS_MODE=True, ENABLE_SESSION_WARMING=False,
                 ENABLE_POLTERGEIST=False, ENABLE_GHOST_TYPER=False,
                 ENABLE_COOKIE_REAPER=False,
             )), tempfile.TemporaryDirectory() as directory:
            manager = stealth_browser.PlaywrightStealthManager()
            self.assertTrue(asyncio.run(manager.initialize(
                profile_path=directory, profile_manifest=manifest,
                purpose="registration", lease_owned=True,
            )))
            runtime_info = manager.get_runtime_info()
            asyncio.run(manager.close())

        self.assertEqual(len(calls), 2)
        self.assertNotIn("channel", calls[1][1])
        self.assertEqual(runtime_info["channel"], "chromium")


if __name__ == "__main__":
    unittest.main()
