import asyncio
import inspect
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from core.profile_runtime import ProfileRuntime


def _valid_auth_cookie():
    """Return the complete cookie shape required by the auth protocol."""
    return {
        "name": "SID",
        "value": "live-session",
        "domain": ".google.com",
        "secure": True,
        "expires": 4102444800,
    }


class WarmerManifestContractTests(unittest.TestCase):
    def test_callers_never_use_path_only_warmer_api(self):
        root = os.path.dirname(os.path.dirname(__file__))
        for relative in ("web/worker.py", "core/runners.py", "core/selenium_runner.py"):
            source = Path(os.path.join(root, relative)).read_text(encoding="utf-8")
            self.assertNotRegex(
                source,
                r"warm_account_(?:playwright|selenium)\([^)]*profile_path",
                msg=relative,
            )

    def test_public_warmer_apis_do_not_accept_profile_path(self):
        from core.account_warmer import (
            warm_account, warm_account_playwright, warm_account_selenium,
        )
        for function in (warm_account, warm_account_playwright, warm_account_selenium):
            with self.subTest(function=function.__name__):
                self.assertNotIn("profile_path", inspect.signature(function).parameters)
                self.assertIn("profile_id", inspect.signature(function).parameters)

    def test_missing_profile_never_imports_or_starts_adapter(self):
        from core.account_warmer import warm_account
        with tempfile.TemporaryDirectory() as directory, \
             patch.dict(os.environ, {"PROFILE_RUNTIME_ROOT": directory}), \
             patch.dict(sys.modules, {
                 "core.stealth_browser": types.SimpleNamespace(
                     PlaywrightStealthManager=lambda: self.fail("adapter started")
                 ),
             }):
            result = warm_account(
                "missing@example.test", "secret", 0,
                profile_id="missing-profile", engine="playwright",
            )
        self.assertFalse(result["success"])
        self.assertEqual(result["browser_status"], "profile_unavailable")

    def test_engine_or_proxy_conflict_never_starts_adapter(self):
        from core.account_warmer import warm_account
        with tempfile.TemporaryDirectory() as directory, \
             patch.dict(os.environ, {"PROFILE_RUNTIME_ROOT": directory}):
            runtime = ProfileRuntime(directory)
            handle = runtime.provision(
                "bound@example.test", "selenium",
                proxy="proxy.example.test:8443:user:secret",
            )
            runtime.bind(handle, "bound@example.test")
            runtime.mark_ready(handle)
            fake_selenium = types.SimpleNamespace(
                create_driver=lambda **_kwargs: self.fail("adapter started")
            )
            with patch.dict(sys.modules, {"core.selenium_runner": fake_selenium}):
                wrong_engine = warm_account(
                    "bound@example.test", "secret", 0,
                    profile_id=handle.profile_id, engine="playwright",
                    proxy="proxy.example.test:8443:other:password",
                )
                wrong_proxy = warm_account(
                    "bound@example.test", "secret", 0,
                    profile_id=handle.profile_id, engine="selenium",
                    proxy="other.example.test:8443:user:secret",
                )
        self.assertEqual(wrong_engine["browser_status"], "profile_conflict")
        self.assertEqual(wrong_proxy["browser_status"], "proxy_mismatch")

    def test_playwright_closes_browser_before_releasing_lease(self):
        from core.account_warmer import warm_account_playwright
        events = []

        class Page:
            url = "https://mail.google.com/mail/u/0/#inbox"
            async def goto(self, *_args, **_kwargs):
                events.append("probe")
                return types.SimpleNamespace(status=200)
            async def wait_for_timeout(self, *_args):
                return None
            async def content(self):
                return "<main>Inbox Compose Search mail</main>"
            async def evaluate(self, script):
                if "querySelectorAll('[data-email" in script:
                    return "bound@example.test"
                if "[role=\"main\"]" in script:
                    return True
                return None

        class Manager:
            page = Page()
            context = types.SimpleNamespace(cookies=lambda: [_valid_auth_cookie()])
            closed = False
            async def initialize(self, **kwargs):
                events.append("open")
                self.kwargs = kwargs
                return True
            async def close(self):
                events.append("close")
                self.closed = True

        with tempfile.TemporaryDirectory() as directory, \
             patch.dict(os.environ, {"PROFILE_RUNTIME_ROOT": directory}):
            runtime = ProfileRuntime(directory)
            handle = runtime.provision("bound@example.test", "playwright")
            runtime.bind(handle, "bound@example.test")
            runtime.mark_ready(handle)

            real_lease = runtime.lease
            class TrackedLease:
                def __init__(self, inner):
                    self.inner = inner
                def __enter__(self):
                    self.inner.__enter__()
                    events.append("lease-enter")
                    return self
                def __exit__(self, *args):
                    events.append("lease-exit")
                    return self.inner.__exit__(*args)
            runtime.lease = lambda h, operation, timeout=0: TrackedLease(
                real_lease(h, operation, timeout)
            )
            fake_module = types.SimpleNamespace(PlaywrightStealthManager=Manager)
            with patch("core.account_warmer.ProfileRuntime.from_environment", return_value=runtime), \
                 patch.dict(sys.modules, {"core.stealth_browser": fake_module}):
                result = asyncio.run(warm_account_playwright(
                    "bound@example.test", "secret", 0,
                    profile_id=handle.profile_id,
                ))

        self.assertTrue(result["success"])
        self.assertLess(events.index("lease-enter"), events.index("open"))
        self.assertLess(events.index("close"), events.index("lease-exit"))

    def test_playwright_constructs_adapter_only_after_lease(self):
        """A profile lock must be held before an adapter can touch its directory."""
        from core.account_warmer import warm_account_playwright

        events = []

        class Page:
            url = "https://mail.google.com/"

            async def goto(self, *_args, **_kwargs):
                events.append("probe")

            async def content(self):
                return "Inbox Compose Search mail"

            async def evaluate(self, script):
                if "querySelectorAll('[data-email" in script:
                    return "bound@example.test"
                if "[role=\"main\"]" in script:
                    return True
                return None

            async def wait_for_timeout(self, *_args):
                return None

        class Context:
            async def cookies(self):
                return [_valid_auth_cookie()]

        class Manager:
            closed = False

            def __init__(self):
                events.append("adapter-init")
                self.page = Page()
                self.context = Context()

            async def initialize(self, **_kwargs):
                events.append("open")
                return True

            async def close(self):
                events.append("close")
                self.closed = True

        with tempfile.TemporaryDirectory() as directory:
            runtime = ProfileRuntime(directory)
            handle = runtime.provision("bound@example.test", "playwright")
            runtime.bind(handle, "bound@example.test")
            runtime.mark_ready(handle)
            real_lease = runtime.lease

            class TrackedLease:
                def __init__(self, inner):
                    self.inner = inner

                def __enter__(self):
                    self.inner.__enter__()
                    events.append("lease-enter")
                    return self

                def __exit__(self, *args):
                    events.append("lease-exit")
                    return self.inner.__exit__(*args)

            runtime.lease = lambda h, operation, timeout=0: TrackedLease(
                real_lease(h, operation, timeout)
            )
            fake_module = types.SimpleNamespace(PlaywrightStealthManager=Manager)
            with patch("core.account_warmer.ProfileRuntime.from_environment", return_value=runtime), \
                 patch.dict(sys.modules, {"core.stealth_browser": fake_module}):
                result = asyncio.run(warm_account_playwright(
                    "bound@example.test", "secret", 0,
                    profile_id=handle.profile_id,
                ))

        self.assertTrue(result["success"])
        self.assertLess(events.index("lease-enter"), events.index("adapter-init"))
        self.assertLess(events.index("adapter-init"), events.index("open"))
        self.assertLess(events.index("close"), events.index("lease-exit"))

    def test_playwright_warm_rejects_profile_directory_replacement_before_adapter(self):
        """A lease/path swap must not reach the browser adapter."""
        from core.account_warmer import warm_account_playwright

        events = []

        class SwappingLease:
            def __init__(self, inner, handle, outside):
                self.inner = inner
                self.handle = handle
                self.outside = outside

            def __enter__(self):
                self.inner.__enter__()
                moved = self.handle.path.parent / "moved-profile"
                self.handle.path.rename(moved)
                self.handle.path.symlink_to(self.outside, target_is_directory=True)
                return self

            def __exit__(self, *args):
                return self.inner.__exit__(*args)

            def assert_stable(self):
                return self.inner.assert_stable()

        class Manager:
            def __init__(self):
                events.append("adapter-init")

        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            runtime = ProfileRuntime(directory)
            handle = runtime.provision("bound@example.test", "playwright")
            runtime.bind(handle, "bound@example.test")
            runtime.mark_ready(handle)
            real_lease = runtime.lease
            runtime.lease = lambda h, operation, timeout=0: SwappingLease(
                real_lease(h, operation, timeout), h, Path(outside)
            )
            with patch("core.account_warmer.ProfileRuntime.from_environment", return_value=runtime), \
                 patch.dict(sys.modules, {"core.stealth_browser": types.SimpleNamespace(
                     PlaywrightStealthManager=Manager
                 )}):
                result = asyncio.run(warm_account_playwright(
                    "bound@example.test", "secret", 0,
                    profile_id=handle.profile_id,
                ))

        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "profile_conflict")
        self.assertEqual(events, [])

    def test_playwright_account_mismatch_stops_before_login(self):
        from core.account_warmer import warm_account_playwright

        class Page:
            url = "https://mail.google.com/"

            async def goto(self, *_args, **_kwargs):
                return None

            async def content(self):
                return "Inbox Compose Search mail"

            async def evaluate(self, *_args):
                return "other@example.test"

            async def query_selector(self, *_args, **_kwargs):
                raise AssertionError("account mismatch must not enter credentials")

            async def wait_for_timeout(self, *_args):
                return None

        class Manager:
            def __init__(self):
                self.page = Page()
                self.context = types.SimpleNamespace(cookies=lambda: [])
                self.closed = False

            async def initialize(self, **_kwargs):
                return True

            async def close(self):
                self.closed = True

        with tempfile.TemporaryDirectory() as directory:
            runtime = ProfileRuntime(directory)
            handle = runtime.provision("bound@example.test", "playwright")
            runtime.bind(handle, "bound@example.test")
            runtime.mark_ready(handle)
            manager = Manager()
            with patch("core.account_warmer.ProfileRuntime.from_environment", return_value=runtime), \
                 patch.dict(sys.modules, {"core.stealth_browser": types.SimpleNamespace(
                     PlaywrightStealthManager=lambda: manager
                 )}):
                result = asyncio.run(warm_account_playwright(
                    "bound@example.test", "secret", 0,
                    profile_id=handle.profile_id,
                ))

        self.assertFalse(result["success"])
        self.assertEqual(result["browser_status"], "account_mismatch")
        self.assertEqual(result["error_code"], "account_mismatch")
        self.assertTrue(manager.closed)

    def test_selenium_account_mismatch_stops_before_login(self):
        from core.account_warmer import warm_account_selenium

        class Driver:
            page_source = "Inbox Compose Search mail"

            def __init__(self):
                self.closed = False

            def get(self, *_args):
                return None

            def execute_script(self, *_args):
                return "other@example.test"

            def get_cookies(self):
                return []

            def quit(self):
                self.closed = True

        driver = Driver()
        fake_runner = types.SimpleNamespace(create_driver=lambda **_kwargs: driver)
        by = types.SimpleNamespace(CSS_SELECTOR="css", XPATH="xpath")
        fake_modules = {
            "core.selenium_runner": fake_runner,
            "selenium": types.ModuleType("selenium"),
            "selenium.webdriver": types.ModuleType("selenium.webdriver"),
            "selenium.webdriver.common": types.ModuleType("selenium.webdriver.common"),
            "selenium.webdriver.common.by": types.SimpleNamespace(By=by),
            "selenium.webdriver.support": types.ModuleType("selenium.webdriver.support"),
            "selenium.webdriver.support.wait": types.SimpleNamespace(
                WebDriverWait=lambda *_args, **_kwargs: self.fail("login wait must not run")
            ),
            "selenium.webdriver.support.expected_conditions": types.SimpleNamespace(),
        }
        with tempfile.TemporaryDirectory() as directory:
            runtime = ProfileRuntime(directory)
            handle = runtime.provision("bound@example.test", "selenium")
            runtime.bind(handle, "bound@example.test")
            runtime.mark_ready(handle)
            with patch("core.account_warmer.ProfileRuntime.from_environment", return_value=runtime), \
                 patch.dict(sys.modules, fake_modules):
                result = warm_account_selenium(
                    "bound@example.test", "secret", 0,
                    profile_id=handle.profile_id,
                )

        self.assertFalse(result["success"])
        self.assertEqual(result["browser_status"], "account_mismatch")
        self.assertEqual(result["error_code"], "account_mismatch")
        self.assertTrue(driver.closed)

    def test_playwright_runtime_mismatch_stops_before_probe(self):
        from core.account_warmer import warm_account_playwright

        class Page:
            async def goto(self, *_args, **_kwargs):
                raise AssertionError("runtime mismatch must stop before navigation")

        class Manager:
            page = Page()
            context = types.SimpleNamespace(cookies=lambda: [])

            def __init__(self):
                self.closed = False

            async def initialize(self, **_kwargs):
                return True

            def get_runtime_info(self):
                return {"channel": "chrome", "major_version": "121"}

            async def close(self):
                self.closed = True

        with tempfile.TemporaryDirectory() as directory:
            runtime = ProfileRuntime(directory)
            handle = runtime.provision("bound@example.test", "playwright")
            runtime.bind(handle, "bound@example.test", {
                "channel": "chrome", "major_version": "120",
            })
            runtime.mark_ready(handle)
            manager = Manager()
            with patch("core.account_warmer.ProfileRuntime.from_environment", return_value=runtime), \
                 patch.dict(sys.modules, {"core.stealth_browser": types.SimpleNamespace(
                     PlaywrightStealthManager=lambda: manager
                 )}):
                result = asyncio.run(warm_account_playwright(
                    "bound@example.test", "secret", 0,
                    profile_id=handle.profile_id,
                ))

        self.assertFalse(result["success"])
        self.assertEqual(result["browser_status"], "runtime_mismatch")
        self.assertEqual(result["error_code"], "runtime_mismatch")
        self.assertTrue(manager.closed)

    def test_selenium_runtime_mismatch_stops_before_navigation(self):
        """Selenium must not navigate a profile on a different browser major."""
        from core.account_warmer import warm_account_selenium

        class Driver:
            page_source = "Inbox Compose Search mail"
            current_url = "https://mail.google.com/"
            closed = False

            def __init__(self):
                self.events = []

            def get(self, url):
                self.events.append(url)

            def execute_script(self, script):
                if "querySelectorAll('[data-email" in script:
                    return "bound@example.test"
                if "[role=\"main\"]" in script:
                    return True
                return None

            def get_cookies(self):
                return [_valid_auth_cookie()]

            def quit(self):
                self.events.append("quit")

        driver = Driver()
        fake_runner = types.SimpleNamespace(
            create_driver=lambda **_kwargs: driver,
            get_runtime_info=lambda _driver: {
                "channel": "chrome", "major_version": "121"
            },
        )
        by = types.SimpleNamespace(CSS_SELECTOR="css", XPATH="xpath")
        fake_modules = {
            "core.selenium_runner": fake_runner,
            "selenium": types.ModuleType("selenium"),
            "selenium.webdriver": types.ModuleType("selenium.webdriver"),
            "selenium.webdriver.common": types.ModuleType("selenium.webdriver.common"),
            "selenium.webdriver.common.by": types.SimpleNamespace(By=by),
            "selenium.webdriver.support": types.ModuleType("selenium.webdriver.support"),
            "selenium.webdriver.support.wait": types.SimpleNamespace(
                WebDriverWait=lambda *_args, **_kwargs: self.fail(
                    "runtime mismatch must stop before login"
                )
            ),
            "selenium.webdriver.support.expected_conditions": types.SimpleNamespace(),
        }
        with tempfile.TemporaryDirectory() as directory:
            runtime = ProfileRuntime(directory)
            handle = runtime.provision("bound@example.test", "selenium")
            runtime.bind(handle, "bound@example.test", {
                "channel": "chrome", "major_version": "120",
            })
            runtime.mark_ready(handle)
            with patch("core.account_warmer.ProfileRuntime.from_environment", return_value=runtime), \
                 patch.dict(sys.modules, fake_modules):
                result = warm_account_selenium(
                    "bound@example.test", "secret", 0,
                    profile_id=handle.profile_id,
                )

        self.assertFalse(result["success"])
        self.assertEqual(result["browser_status"], "runtime_mismatch")
        self.assertEqual(result["error_code"], "runtime_mismatch")
        self.assertEqual(driver.events, ["quit"])

    def test_selenium_closes_browser_before_releasing_lease(self):
        from core.account_warmer import warm_account_selenium
        events = []

        class Driver:
            page_source = "Inbox Compose Search mail"
            current_url = "https://mail.google.com/"

            def get(self, _url):
                events.append("probe")

            def execute_script(self, script):
                if "querySelectorAll('[data-email" in script:
                    return "bound@example.test"
                if "[role=\"main\"]" in script:
                    return True
                return None

            def get_cookies(self):
                return [_valid_auth_cookie()]

            def quit(self):
                events.append("close")
                self.closed = True

        driver = Driver()

        def create_driver(**_kwargs):
            events.append("open")
            return driver

        fake_runner = types.SimpleNamespace(
            create_driver=create_driver,
            get_runtime_info=lambda _driver: {
                "channel": "chrome", "major_version": ""
            },
        )
        by = types.SimpleNamespace(CSS_SELECTOR="css", XPATH="xpath")
        fake_modules = {
            "core.selenium_runner": fake_runner,
            "selenium": types.ModuleType("selenium"),
            "selenium.webdriver": types.ModuleType("selenium.webdriver"),
            "selenium.webdriver.common": types.ModuleType("selenium.webdriver.common"),
            "selenium.webdriver.common.by": types.SimpleNamespace(By=by),
            "selenium.webdriver.support": types.ModuleType("selenium.webdriver.support"),
            "selenium.webdriver.support.wait": types.SimpleNamespace(WebDriverWait=object),
            "selenium.webdriver.support.expected_conditions": types.SimpleNamespace(),
        }
        with tempfile.TemporaryDirectory() as directory:
            runtime = ProfileRuntime(directory)
            handle = runtime.provision("bound@example.test", "selenium")
            runtime.bind(handle, "bound@example.test")
            runtime.mark_ready(handle)
            real_lease = runtime.lease

            class TrackedLease:
                def __init__(self, inner):
                    self.inner = inner

                def __enter__(self):
                    self.inner.__enter__()
                    events.append("lease-enter")
                    return self

                def __exit__(self, *args):
                    events.append("lease-exit")
                    return self.inner.__exit__(*args)

            runtime.lease = lambda h, operation, timeout=0: TrackedLease(
                real_lease(h, operation, timeout)
            )
            with patch("core.account_warmer.ProfileRuntime.from_environment", return_value=runtime), \
                 patch.dict(sys.modules, fake_modules):
                result = warm_account_selenium(
                    "bound@example.test", "secret", 0,
                    profile_id=handle.profile_id,
                )

        self.assertTrue(result["success"])
        self.assertLess(events.index("lease-enter"), events.index("open"))
        self.assertLess(events.index("open"), events.index("probe"))
        self.assertLess(events.index("close"), events.index("lease-exit"))

    def test_reconstructed_playwright_profile_requires_observed_identity(self):
        from core.account_warmer import warm_account_playwright

        class Page:
            url = "https://mail.google.com/"

            async def goto(self, *_args, **_kwargs):
                return None

            async def content(self):
                return "Inbox Compose Search mail"

            async def evaluate(self, *_args):
                return None

            async def query_selector(self, *_args, **_kwargs):
                raise AssertionError("unverified identity must not enter login")

            async def wait_for_timeout(self, *_args):
                return None

        class Manager:
            def __init__(self):
                self.page = Page()
                self.context = types.SimpleNamespace(cookies=lambda: [_valid_auth_cookie()])
                self.closed = False
                self.closed = False

            async def initialize(self, **_kwargs):
                return True

            async def close(self):
                self.closed = True

        with tempfile.TemporaryDirectory() as directory:
            runtime = ProfileRuntime(directory)
            handle = runtime.provision("bound@example.test", "playwright")
            runtime.bind(handle, "bound@example.test")
            manifest = runtime.load(handle)
            manifest["identity_state"] = "identity_reconstructed"
            manifest["identity_verified"] = False
            manifest.pop("identity_verified_email", None)
            manifest["state"] = "ready"
            runtime._atomic_json(handle.manifest_path, manifest)
            manager = Manager()
            with patch("core.account_warmer.ProfileRuntime.from_environment", return_value=runtime), \
                 patch.dict(sys.modules, {"core.stealth_browser": types.SimpleNamespace(
                     PlaywrightStealthManager=lambda: manager
                 )}):
                result = asyncio.run(warm_account_playwright(
                    "bound@example.test", "secret", 0,
                    profile_id=handle.profile_id,
                ))

        self.assertFalse(result["success"])
        self.assertEqual(result["browser_status"], "account_mismatch")
        self.assertTrue(manager.closed)

    def test_reconstructed_playwright_profile_persists_fresh_identity_verification(self):
        from core.account_warmer import warm_account_playwright

        class Page:
            url = "https://mail.google.com/"

            async def goto(self, *_args, **_kwargs):
                return None

            async def content(self):
                return "Inbox Compose Search mail"

            async def evaluate(self, script):
                if "querySelectorAll('[data-email" in script:
                    return "bound@example.test"
                if "[role=\"main\"]" in script:
                    return True
                return None

            async def wait_for_timeout(self, *_args):
                return None

        class Manager:
            def __init__(self):
                self.page = Page()
                self.context = types.SimpleNamespace(cookies=lambda: [_valid_auth_cookie()])

            async def initialize(self, **_kwargs):
                return True

            async def close(self):
                self.closed = True

        with tempfile.TemporaryDirectory() as directory:
            runtime = ProfileRuntime(directory)
            legacy = runtime.profiles_root / "adopted-playwright"
            legacy.mkdir()
            handle = runtime.adopt_legacy(
                str(legacy), "bound@example.test", "playwright"
            )
            manager = Manager()
            with patch("core.account_warmer.ProfileRuntime.from_environment", return_value=runtime), \
                 patch.dict(sys.modules, {"core.stealth_browser": types.SimpleNamespace(
                     PlaywrightStealthManager=lambda: manager
                 )}):
                result = asyncio.run(warm_account_playwright(
                    "bound@example.test", "secret", 0,
                    profile_id=handle.profile_id,
                ))

            self.assertTrue(result["success"])
            manifest = runtime.load(handle)
            self.assertTrue(manifest["identity_verified"])
            self.assertEqual(manifest["identity_verified_email"], "bound@example.test")

    def test_reconstructed_playwright_signed_out_profile_can_login_then_verify(self):
        """A migrated profile must treat an explicit sign-in page as login_required.

        It must not be rejected as an identity mismatch before the credentials
        can establish the fresh identity observation required for adoption.
        """
        from core.account_warmer import warm_account_playwright

        events = []

        class Input:
            def __init__(self, name):
                self.name = name

            async def fill(self, value):
                events.append((self.name, value))

        class Page:
            def __init__(self):
                self.phase = "signed_out"
                self.url = "https://accounts.google.com/signin"

            async def goto(self, url, **_kwargs):
                events.append(("goto", url))
                self.url = url

            async def content(self):
                if self.phase == "signed_out":
                    return "Sign in to Gmail Email or phone"
                if self.phase == "password":
                    return "Enter your password"
                return "Inbox Compose Search mail"

            async def evaluate(self, script):
                if "querySelectorAll('[data-email" in script:
                    return (
                        None if self.phase != "signed_in"
                        else "Google Account: adopted@example.test"
                    )
                if "[role=\"main\"]" in script:
                    return self.phase == "signed_in"
                return None

            async def query_selector(self, selector):
                if self.phase == "signed_out" and 'type="email"' in selector:
                    return Input("email")
                if self.phase == "password" and 'type="password"' in selector:
                    return Input("password")
                return None

            async def click(self, _selector):
                events.append("next")
                if self.phase == "signed_out":
                    self.phase = "password"
                elif self.phase == "password":
                    self.phase = "signed_in"
                    self.url = "https://mail.google.com/mail/u/0/#inbox"

            async def wait_for_timeout(self, *_args):
                return None

        class Context:
            async def cookies(self):
                return [_valid_auth_cookie()]

        class Manager:
            def __init__(self):
                self.page = Page()
                self.context = Context()
                self.closed = False

            async def initialize(self, **_kwargs):
                return True

            async def close(self):
                self.closed = True

        with tempfile.TemporaryDirectory() as directory:
            runtime = ProfileRuntime(directory)
            legacy = runtime.profiles_root / "adopted-signed-out"
            legacy.mkdir()
            handle = runtime.adopt_legacy(
                str(legacy), "adopted@example.test", "playwright"
            )
            manager = Manager()
            class Clock:
                def __init__(self):
                    self.values = iter((0, 0, 0, 61, 61))

                def monotonic(self):
                    return next(self.values)

            clock = Clock()
            with patch("core.account_warmer.ProfileRuntime.from_environment", return_value=runtime), \
                 patch("core.account_warmer.time", clock), \
                 patch.dict(sys.modules, {"core.stealth_browser": types.SimpleNamespace(
                     PlaywrightStealthManager=lambda: manager
                 )}):
                result = asyncio.run(warm_account_playwright(
                    "adopted@example.test", "secret", 1,
                    profile_id=handle.profile_id,
                ))

            manifest = runtime.load(handle)

        self.assertTrue(result["success"])
        self.assertIn(("email", "adopted@example.test"), events)
        self.assertIn(("password", "secret"), events)
        self.assertGreaterEqual(
            len([event for event in events if isinstance(event, tuple) and event[0] == "goto"]),
            3,
        )
        self.assertTrue(manifest["identity_verified"])
        self.assertEqual(manifest["identity_verified_email"], "adopted@example.test")
        self.assertTrue(manager.closed)

    def test_reconstructed_selenium_profile_requires_observed_identity(self):
        from core.account_warmer import warm_account_selenium

        class Driver:
            page_source = "Inbox Compose Search mail"
            current_url = "https://mail.google.com/"
            closed = False

            def get(self, *_args):
                return None

            def execute_script(self, *_args):
                return None

            def get_cookies(self):
                return [{"name": "SID"}]

            def quit(self):
                pass

        driver = Driver()
        fake_runner = types.SimpleNamespace(create_driver=lambda **_kwargs: driver)
        by = types.SimpleNamespace(CSS_SELECTOR="css", XPATH="xpath")
        fake_modules = {
            "core.selenium_runner": fake_runner,
            "selenium": types.ModuleType("selenium"),
            "selenium.webdriver": types.ModuleType("selenium.webdriver"),
            "selenium.webdriver.common": types.ModuleType("selenium.webdriver.common"),
            "selenium.webdriver.common.by": types.SimpleNamespace(By=by),
            "selenium.webdriver.support": types.ModuleType("selenium.webdriver.support"),
            "selenium.webdriver.support.wait": types.SimpleNamespace(
                WebDriverWait=lambda *_args, **_kwargs: self.fail(
                    "unverified identity must not enter login"
                )
            ),
            "selenium.webdriver.support.expected_conditions": types.SimpleNamespace(),
        }
        with tempfile.TemporaryDirectory() as directory:
            runtime = ProfileRuntime(directory)
            handle = runtime.provision("bound@example.test", "selenium")
            runtime.bind(handle, "bound@example.test")
            manifest = runtime.load(handle)
            manifest["identity_state"] = "identity_reconstructed"
            manifest["identity_verified"] = False
            manifest.pop("identity_verified_email", None)
            manifest["state"] = "ready"
            runtime._atomic_json(handle.manifest_path, manifest)
            with patch("core.account_warmer.ProfileRuntime.from_environment", return_value=runtime), \
                 patch.dict(sys.modules, fake_modules):
                result = warm_account_selenium(
                    "bound@example.test", "secret", 0,
                    profile_id=handle.profile_id,
                )

        self.assertFalse(result["success"])
        self.assertEqual(result["browser_status"], "account_mismatch")

    def test_reconstructed_selenium_profile_persists_fresh_identity_verification(self):
        from core.account_warmer import warm_account_selenium

        class Driver:
            page_source = "Inbox Compose Search mail"
            current_url = "https://mail.google.com/"

            def get(self, *_args):
                return None

            def execute_script(self, script):
                if "querySelectorAll('[data-email" in script:
                    return "bound@example.test"
                if "[role=\"main\"]" in script:
                    return True
                return None

            def get_cookies(self):
                return [_valid_auth_cookie()]

            def quit(self):
                self.closed = True

        driver = Driver()
        fake_runner = types.SimpleNamespace(create_driver=lambda **_kwargs: driver)
        by = types.SimpleNamespace(CSS_SELECTOR="css", XPATH="xpath")
        fake_modules = {
            "core.selenium_runner": fake_runner,
            "selenium": types.ModuleType("selenium"),
            "selenium.webdriver": types.ModuleType("selenium.webdriver"),
            "selenium.webdriver.common": types.ModuleType("selenium.webdriver.common"),
            "selenium.webdriver.common.by": types.SimpleNamespace(By=by),
            "selenium.webdriver.support": types.ModuleType("selenium.webdriver.support"),
            "selenium.webdriver.support.wait": types.SimpleNamespace(WebDriverWait=object),
            "selenium.webdriver.support.expected_conditions": types.SimpleNamespace(),
        }
        with tempfile.TemporaryDirectory() as directory:
            runtime = ProfileRuntime(directory)
            legacy = runtime.profiles_root / "adopted-selenium"
            legacy.mkdir()
            handle = runtime.adopt_legacy(
                str(legacy), "bound@example.test", "selenium"
            )
            with patch("core.account_warmer.ProfileRuntime.from_environment", return_value=runtime), \
                 patch.dict(sys.modules, fake_modules):
                result = warm_account_selenium(
                    "bound@example.test", "secret", 0,
                    profile_id=handle.profile_id,
                )

            self.assertTrue(result["success"])
            manifest = runtime.load(handle)
            self.assertTrue(manifest["identity_verified"])
            self.assertEqual(manifest["identity_verified_email"], "bound@example.test")

    def test_reconstructed_selenium_signed_out_profile_can_login_then_verify(self):
        from core.account_warmer import warm_account_selenium

        events = []

        class Element:
            def __init__(self, name):
                self.name = name

            def send_keys(self, value):
                events.append((self.name, value))

            def click(self):
                return None

        class Driver:
            def __init__(self):
                self.phase = "signed_out"
                self.current_url = "https://accounts.google.com/signin"
                self.closed = False

            @property
            def page_source(self):
                if self.phase == "signed_out":
                    return "Sign in to Gmail Email or phone"
                if self.phase == "password":
                    return "Enter your password"
                return "Inbox Compose Search mail"

            def get(self, url):
                events.append(("goto", url))
                self.current_url = url

            def execute_script(self, script):
                if "querySelectorAll('[data-email" in script:
                    return (
                        None if self.phase != "signed_in"
                        else "Google Account: adopted@example.test"
                    )
                if "[role=\"main\"]" in script:
                    return self.phase == "signed_in"
                return None

            def get_cookies(self):
                return [_valid_auth_cookie()]

            def find_element(self, _by, _selector):
                events.append("next")
                if self.phase == "signed_out":
                    self.phase = "password"
                elif self.phase == "password":
                    self.phase = "signed_in"
                    self.current_url = "https://mail.google.com/mail/u/0/#inbox"
                return Element("next")

            def quit(self):
                self.closed = True

        driver = Driver()

        class Wait:
            def __init__(self, *_args, **_kwargs):
                pass

            def until(self, predicate):
                marker = predicate(None) if callable(predicate) else None
                selector = str(marker or "")
                if "password" in selector:
                    return Element("password")
                return Element("email")

        class EC:
            @staticmethod
            def presence_of_element_located(locator):
                return lambda _driver: locator[1]

        fake_runner = types.SimpleNamespace(
            create_driver=lambda **_kwargs: driver,
            get_runtime_info=lambda _driver: {},
        )
        by = types.SimpleNamespace(CSS_SELECTOR="css", XPATH="xpath")
        fake_modules = {
            "core.selenium_runner": fake_runner,
            "selenium": types.ModuleType("selenium"),
            "selenium.webdriver": types.ModuleType("selenium.webdriver"),
            "selenium.webdriver.common": types.ModuleType("selenium.webdriver.common"),
            "selenium.webdriver.common.by": types.SimpleNamespace(By=by),
            "selenium.webdriver.support": types.ModuleType("selenium.webdriver.support"),
            "selenium.webdriver.support.wait": types.SimpleNamespace(WebDriverWait=Wait),
            "selenium.webdriver.support.expected_conditions": types.SimpleNamespace(
                presence_of_element_located=EC.presence_of_element_located,
            ),
        }

        with tempfile.TemporaryDirectory() as directory:
            runtime = ProfileRuntime(directory)
            legacy = runtime.profiles_root / "adopted-selenium-signed-out"
            legacy.mkdir()
            handle = runtime.adopt_legacy(
                str(legacy), "adopted@example.test", "selenium"
            )
            class Clock:
                def __init__(self):
                    self.values = iter((0, 0, 0, 61, 61))

                def monotonic(self):
                    return next(self.values)

                def sleep(self, _seconds):
                    return None

            clock = Clock()
            with patch("core.account_warmer.ProfileRuntime.from_environment", return_value=runtime), \
                 patch("core.account_warmer.time", clock), \
                 patch.dict(sys.modules, fake_modules):
                result = warm_account_selenium(
                    "adopted@example.test", "secret", 1,
                    profile_id=handle.profile_id,
                )

            manifest = runtime.load(handle)

        self.assertTrue(result["success"])
        self.assertIn(("email", "adopted@example.test"), events)
        self.assertIn(("password", "secret"), events)
        self.assertGreaterEqual(
            len([event for event in events if isinstance(event, tuple) and event[0] == "goto"]),
            3,
        )
        self.assertTrue(manifest["identity_verified"])
        self.assertEqual(manifest["identity_verified_email"], "adopted@example.test")
        self.assertTrue(driver.closed)


if __name__ == "__main__":
    unittest.main()
