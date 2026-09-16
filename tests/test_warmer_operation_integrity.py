import asyncio
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from core.profile_runtime import ProfileRuntime


def _valid_auth_cookie():
    return {
        "name": "SID",
        "value": "live-session",
        "domain": ".google.com",
        "secure": True,
        "expires": 4102444800,
    }


class WarmerOperationIntegrityTests(unittest.TestCase):
    class Clock:
        def __init__(self):
            self.values = iter((0, 0, 61, 61))

        def monotonic(self):
            return next(self.values)

        def sleep(self, _seconds):
            return None

    @staticmethod
    def _ready_profile(directory, engine):
        runtime = ProfileRuntime(directory)
        handle = runtime.provision("bound@example.test", engine)
        runtime.bind(handle, "bound@example.test")
        runtime.mark_ready(handle)
        return runtime, handle

    @staticmethod
    def _selenium_modules(driver):
        by = types.SimpleNamespace(CSS_SELECTOR="css", XPATH="xpath")
        return {
            "core.selenium_runner": types.SimpleNamespace(
                create_driver=lambda **_kwargs: driver,
                get_runtime_info=lambda _driver: {},
            ),
            "selenium": types.ModuleType("selenium"),
            "selenium.webdriver": types.ModuleType("selenium.webdriver"),
            "selenium.webdriver.common": types.ModuleType("selenium.webdriver.common"),
            "selenium.webdriver.common.by": types.SimpleNamespace(By=by),
            "selenium.webdriver.support": types.ModuleType("selenium.webdriver.support"),
            "selenium.webdriver.support.wait": types.SimpleNamespace(WebDriverWait=object),
            "selenium.webdriver.support.expected_conditions": types.SimpleNamespace(),
        }

    def test_playwright_all_activity_failures_are_not_reported_as_success(self):
        from core.account_warmer import warm_account_playwright

        class Page:
            def __init__(self):
                self.navigation_count = 0
                self.url = "https://mail.google.com/"

            async def goto(self, _url, **_kwargs):
                self.navigation_count += 1
                if self.navigation_count > 1:
                    raise RuntimeError("activity navigation failed")

            async def content(self):
                return "Inbox Compose Search mail"

            async def evaluate(self, script):
                if "querySelectorAll('[data-email" in script:
                    return "bound@example.test"
                if "[role=\"main\"]" in script:
                    return True
                return None

            async def wait_for_timeout(self, _milliseconds):
                return None

        class Manager:
            def __init__(self):
                self.page = Page()
                self.context = types.SimpleNamespace(cookies=lambda: [_valid_auth_cookie()])
                self.closed = False

            async def initialize(self, **_kwargs):
                return True

            async def close(self):
                self.closed = True
                return None

        with tempfile.TemporaryDirectory() as directory:
            runtime, handle = self._ready_profile(directory, "playwright")
            manager = Manager()
            with patch("core.account_warmer.ProfileRuntime.from_environment", return_value=runtime), \
                 patch("core.account_warmer.time", self.Clock()), \
                 patch.dict(sys.modules, {"core.stealth_browser": types.SimpleNamespace(
                     PlaywrightStealthManager=lambda: manager
                 )}):
                result = asyncio.run(warm_account_playwright(
                    "bound@example.test", "secret-password", 1,
                    profile_id=handle.profile_id,
                ))
            profile_state = runtime.load(handle)["state"]

            with runtime.lease(handle, "post-test"):
                pass

        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "activity_failed")
        self.assertEqual(result["activity_attempts"], 1)
        self.assertEqual(result["activity_successes"], 0)
        self.assertIn("navigation failed", result["last_activity_error"])
        self.assertNotIn("secret-password", str(result))
        self.assertEqual(result["cleanup_status"], "completed")
        self.assertTrue(result["browser_process_stopped"])
        self.assertTrue(result["lease_released"])

    def test_selenium_all_activity_failures_are_not_reported_as_success(self):
        from core.account_warmer import warm_account_selenium

        class Driver:
            page_source = "Inbox Compose Search mail"
            current_url = "https://mail.google.com/"

            def __init__(self):
                self.navigation_count = 0
                self.closed = False

            def get(self, _url):
                self.navigation_count += 1
                if self.navigation_count > 1:
                    raise RuntimeError("activity navigation failed")

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
                return None

        with tempfile.TemporaryDirectory() as directory:
            runtime, handle = self._ready_profile(directory, "selenium")
            driver = Driver()
            with patch("core.account_warmer.ProfileRuntime.from_environment", return_value=runtime), \
                 patch("core.account_warmer.time", self.Clock()), \
                 patch.dict(sys.modules, self._selenium_modules(driver)):
                result = warm_account_selenium(
                    "bound@example.test", "secret-password", 1,
                    profile_id=handle.profile_id,
                )

            with runtime.lease(handle, "post-test"):
                pass

        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "activity_failed")
        self.assertEqual(result["activity_attempts"], 1)
        self.assertEqual(result["activity_successes"], 0)
        self.assertIn("navigation failed", result["last_activity_error"])
        self.assertNotIn("secret-password", str(result))
        self.assertEqual(result["cleanup_status"], "completed")
        self.assertTrue(result["browser_process_stopped"])
        self.assertTrue(result["lease_released"])

    def test_playwright_close_failure_overrides_success(self):
        from core.account_warmer import warm_account_playwright

        class Page:
            url = "https://mail.google.com/"

            async def goto(self, _url, **_kwargs):
                return None

            async def content(self):
                return "Inbox Compose Search mail"

            async def evaluate(self, script):
                if "querySelectorAll('[data-email" in script:
                    return "bound@example.test"
                if "[role=\"main\"]" in script:
                    return True
                return None

            async def wait_for_timeout(self, _milliseconds):
                return None

        class Manager:
            page = Page()
            context = types.SimpleNamespace(cookies=lambda: [_valid_auth_cookie()])

            async def initialize(self, **_kwargs):
                return True

            async def close(self):
                raise RuntimeError("controller shutdown failed")

        with tempfile.TemporaryDirectory() as directory:
            runtime, handle = self._ready_profile(directory, "playwright")
            with patch("core.account_warmer.ProfileRuntime.from_environment", return_value=runtime), \
                 patch("core.account_warmer.time", self.Clock()), \
                 patch.dict(sys.modules, {"core.stealth_browser": types.SimpleNamespace(
                     PlaywrightStealthManager=Manager
                 )}):
                result = asyncio.run(warm_account_playwright(
                    "bound@example.test", "secret-password", 1,
                    profile_id=handle.profile_id,
                ))
            profile_state = runtime.load(handle)["state"]

        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "cleanup_failed")
        self.assertEqual(result["cleanup_status"], "failed")
        self.assertFalse(result["browser_process_stopped"])
        self.assertFalse(result["lease_released"])
        self.assertEqual(profile_state, "cleanup_failed")
        self.assertNotIn("secret-password", str(result))

    def test_playwright_initialization_failure_still_requires_cleanup_proof(self):
        """An adapter that fails during initialize may still own browser state."""
        from core.account_warmer import warm_account_playwright

        class Page:
            url = "https://mail.google.com/"

        class Manager:
            page = Page()
            context = types.SimpleNamespace(cookies=lambda: [_valid_auth_cookie()])

            async def initialize(self, **_kwargs):
                raise RuntimeError("adapter initialization failed")

            async def close(self):
                return {"success": False, "browser_process_stopped": False}

        with tempfile.TemporaryDirectory() as directory:
            runtime, handle = self._ready_profile(directory, "playwright")
            with patch("core.account_warmer.ProfileRuntime.from_environment", return_value=runtime), \
                 patch.dict(sys.modules, {"core.stealth_browser": types.SimpleNamespace(
                     PlaywrightStealthManager=Manager
                 )}):
                result = asyncio.run(warm_account_playwright(
                    "bound@example.test", "secret-password", 0,
                    profile_id=handle.profile_id,
                ))
            profile_state = runtime.load(handle)["state"]

        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "cleanup_failed")
        self.assertEqual(result["operation_error_code"], "runtime_unavailable")
        self.assertEqual(result["cleanup_status"], "failed")
        self.assertFalse(result["browser_process_stopped"])
        self.assertFalse(result["lease_released"])
        self.assertEqual(profile_state, "cleanup_failed")

    def test_selenium_driver_initialization_failure_still_requires_cleanup_proof(self):
        """A driver factory exception is not proof that no browser was spawned."""
        from core.account_warmer import warm_account_selenium

        class DriverFactory:
            @staticmethod
            def create_driver(**_kwargs):
                raise RuntimeError("driver initialization failed")

        with tempfile.TemporaryDirectory() as directory:
            runtime, handle = self._ready_profile(directory, "selenium")
            modules = self._selenium_modules(None)
            modules["core.selenium_runner"].create_driver = DriverFactory.create_driver
            with patch("core.account_warmer.ProfileRuntime.from_environment", return_value=runtime), \
                 patch.dict(sys.modules, modules):
                result = warm_account_selenium(
                    "bound@example.test", "secret-password", 0,
                    profile_id=handle.profile_id,
                )
            profile_state = runtime.load(handle)["state"]

        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "cleanup_failed")
        self.assertEqual(result["operation_error_code"], "runtime_unavailable")
        self.assertEqual(result["cleanup_status"], "failed")
        self.assertFalse(result["browser_process_stopped"])
        self.assertFalse(result["lease_released"])
        self.assertEqual(profile_state, "cleanup_failed")

    def test_playwright_unknown_process_state_overrides_success(self):
        from core.account_warmer import warm_account_playwright

        class Page:
            url = "https://mail.google.com/"

            async def goto(self, _url, **_kwargs):
                return None

            async def content(self):
                return "Inbox Compose Search mail"

            async def evaluate(self, script):
                if "querySelectorAll('[data-email" in script:
                    return "bound@example.test"
                if "[role=\"main\"]" in script:
                    return True
                return None

            async def wait_for_timeout(self, _milliseconds):
                return None

        class Manager:
            page = Page()
            context = types.SimpleNamespace(cookies=lambda: [_valid_auth_cookie()])

            async def initialize(self, **_kwargs):
                return True

            async def close(self):
                return {"success": True, "browser_process_stopped": False}

        with tempfile.TemporaryDirectory() as directory:
            runtime, handle = self._ready_profile(directory, "playwright")
            with patch("core.account_warmer.ProfileRuntime.from_environment", return_value=runtime), \
                 patch.dict(sys.modules, {"core.stealth_browser": types.SimpleNamespace(
                     PlaywrightStealthManager=Manager
                 )}):
                result = asyncio.run(warm_account_playwright(
                    "bound@example.test", "secret-password", 0,
                    profile_id=handle.profile_id,
                ))

        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "cleanup_failed")
        self.assertEqual(result["cleanup_status"], "failed")
        self.assertFalse(result["browser_process_stopped"])
        self.assertFalse(result["lease_released"])

    def test_playwright_adapter_cancellation_is_reported_as_cleanup_failure(self):
        from core.account_warmer import warm_account_playwright

        class Page:
            url = "https://mail.google.com/"

            async def goto(self, _url, **_kwargs):
                return None

            async def content(self):
                return "Inbox Compose Search mail"

            async def evaluate(self, script):
                if "querySelectorAll('[data-email" in script:
                    return "bound@example.test"
                if "[role=\"main\"]" in script:
                    return True
                return None

            async def wait_for_timeout(self, _milliseconds):
                return None

        class Manager:
            page = Page()
            context = types.SimpleNamespace(cookies=lambda: [_valid_auth_cookie()])

            async def initialize(self, **_kwargs):
                return True

            async def close(self):
                raise asyncio.CancelledError()

        with tempfile.TemporaryDirectory() as directory:
            runtime, handle = self._ready_profile(directory, "playwright")
            with patch("core.account_warmer.ProfileRuntime.from_environment", return_value=runtime), \
                 patch("core.account_warmer.time", self.Clock()), \
                 patch.dict(sys.modules, {"core.stealth_browser": types.SimpleNamespace(
                     PlaywrightStealthManager=Manager
                 )}):
                result = asyncio.run(warm_account_playwright(
                    "bound@example.test", "secret-password", 1,
                    profile_id=handle.profile_id,
                ))
            profile_state = runtime.load(handle)["state"]

        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "cleanup_failed")
        self.assertEqual(result["cleanup_status"], "failed")
        self.assertFalse(result["lease_released"])
        self.assertEqual(profile_state, "cleanup_failed")

    def test_selenium_quit_failure_overrides_success(self):
        from core.account_warmer import warm_account_selenium

        class Driver:
            page_source = "Inbox Compose Search mail"
            current_url = "https://mail.google.com/"

            def get(self, _url):
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
                raise RuntimeError("driver shutdown failed")

        with tempfile.TemporaryDirectory() as directory:
            runtime, handle = self._ready_profile(directory, "selenium")
            driver = Driver()
            with patch("core.account_warmer.ProfileRuntime.from_environment", return_value=runtime), \
                 patch("core.account_warmer.time", self.Clock()), \
                 patch.dict(sys.modules, self._selenium_modules(driver)):
                result = warm_account_selenium(
                    "bound@example.test", "secret-password", 1,
                    profile_id=handle.profile_id,
                )
            profile_state = runtime.load(handle)["state"]

        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "cleanup_failed")
        self.assertEqual(result["cleanup_status"], "failed")
        self.assertFalse(result["browser_process_stopped"])
        self.assertFalse(result["lease_released"])
        self.assertEqual(profile_state, "cleanup_failed")
        self.assertNotIn("secret-password", str(result))

    def test_unverified_lease_release_overrides_success(self):
        from core.account_warmer import warm_account_playwright

        class Page:
            url = "https://mail.google.com/"

            async def goto(self, _url, **_kwargs):
                return None

            async def content(self):
                return "Inbox Compose Search mail"

            async def evaluate(self, script):
                if "querySelectorAll('[data-email" in script:
                    return "bound@example.test"
                if "[role=\"main\"]" in script:
                    return True
                return None

            async def wait_for_timeout(self, _milliseconds):
                return None

        class Manager:
            page = Page()
            context = types.SimpleNamespace(cookies=lambda: [_valid_auth_cookie()])

            async def initialize(self, **_kwargs):
                return True

            async def close(self):
                return {"success": True, "browser_process_stopped": True}

        with tempfile.TemporaryDirectory() as directory:
            runtime, handle = self._ready_profile(directory, "playwright")
            real_lease = runtime.lease

            class StickyLease:
                _held = True

                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    # Simulate an OS lease whose close cannot be verified.
                    self._held = True

                def assert_stable(self):
                    return self

            runtime.lease = lambda *_args, **_kwargs: StickyLease()
            with patch("core.account_warmer.ProfileRuntime.from_environment", return_value=runtime), \
                 patch("core.account_warmer.time", self.Clock()), \
                 patch.dict(sys.modules, {"core.stealth_browser": types.SimpleNamespace(
                     PlaywrightStealthManager=Manager
                 )}):
                result = asyncio.run(warm_account_playwright(
                    "bound@example.test", "secret-password", 1,
                    profile_id=handle.profile_id,
                ))
            profile_state = runtime.load(handle)["state"]

        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "cleanup_failed")
        self.assertFalse(result["lease_released"])
        self.assertEqual(profile_state, "cleanup_failed")

    def test_playwright_cancellation_completes_cleanup_before_releasing_lease(self):
        from core.account_warmer import warm_account_playwright

        class Page:
            url = "https://mail.google.com/"

            def __init__(self):
                self.started = None

            async def goto(self, _url, **_kwargs):
                self.started.set()
                await asyncio.Event().wait()

            async def content(self):
                return "Inbox Compose Search mail"

            async def evaluate(self, script):
                if "querySelectorAll('[data-email" in script:
                    return "bound@example.test"
                if "[role=\"main\"]" in script:
                    return True
                return None

            async def wait_for_timeout(self, _milliseconds):
                return None

        class Manager:
            def __init__(self):
                self.page = Page()
                self.context = types.SimpleNamespace(cookies=lambda: [_valid_auth_cookie()])
                self.close_started = None
                self.close_release = None
                self.closed = False

            async def initialize(self, **_kwargs):
                return True

            async def close(self):
                self.close_started.set()
                await self.close_release.wait()
                self.closed = True

        async def exercise(runtime, handle, manager):
            manager.page.started = asyncio.Event()
            manager.close_started = asyncio.Event()
            manager.close_release = asyncio.Event()
            task = asyncio.create_task(warm_account_playwright(
                "bound@example.test", "secret-password", 1,
                profile_id=handle.profile_id,
            ))
            await manager.page.started.wait()
            task.cancel()
            await manager.close_started.wait()
            # A second cancellation arriving during adapter shutdown must not
            # interrupt the cleanup itself.
            task.cancel()
            manager.close_release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task

        with tempfile.TemporaryDirectory() as directory:
            runtime, handle = self._ready_profile(directory, "playwright")
            manager = Manager()
            with patch("core.account_warmer.ProfileRuntime.from_environment", return_value=runtime), \
                 patch.dict(sys.modules, {"core.stealth_browser": types.SimpleNamespace(
                     PlaywrightStealthManager=lambda: manager
                 )}):
                asyncio.run(exercise(runtime, handle, manager))
            self.assertTrue(manager.closed)
            with runtime.lease(handle, "post-cancel"):
                pass

    def test_playwright_sync_cancelled_error_from_close_is_a_cleanup_failure(self):
        """A synchronous CancelledError must not bypass the cleanup result."""
        from core.account_warmer import warm_account_playwright

        class Page:
            url = "https://mail.google.com/"

            async def goto(self, _url, **_kwargs):
                return None

            async def content(self):
                return "Inbox Compose Search mail"

            async def evaluate(self, script):
                if "querySelectorAll('[data-email" in script:
                    return "bound@example.test"
                if "[role=\"main\"]" in script:
                    return True
                return None

            async def wait_for_timeout(self, _milliseconds):
                return None

        class Manager:
            page = Page()
            context = types.SimpleNamespace(cookies=lambda: [_valid_auth_cookie()])

            async def initialize(self, **_kwargs):
                return True

            def close(self):
                # This is a synchronous throw, so no awaitable exists for the
                # normal cancellation shield to observe.
                raise asyncio.CancelledError()

        with tempfile.TemporaryDirectory() as directory:
            runtime, handle = self._ready_profile(directory, "playwright")
            with patch("core.account_warmer.ProfileRuntime.from_environment", return_value=runtime), \
                 patch.dict(sys.modules, {"core.stealth_browser": types.SimpleNamespace(
                     PlaywrightStealthManager=Manager
                 )}):
                result = asyncio.run(warm_account_playwright(
                    "bound@example.test", "secret-password", 0,
                    profile_id=handle.profile_id,
                ))

        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "cleanup_failed")
        self.assertFalse(result["browser_process_stopped"])
        self.assertFalse(result["lease_released"])

    def test_playwright_close_success_cannot_hide_a_live_process(self):
        """An explicit process handle is authoritative over a close boolean."""
        from core.account_warmer import warm_account_playwright

        class Process:
            def poll(self):
                return None

        class Page:
            url = "https://mail.google.com/"

            async def goto(self, _url, **_kwargs):
                return None

            async def content(self):
                return "Inbox Compose Search mail"

            async def evaluate(self, script):
                if "querySelectorAll('[data-email" in script:
                    return "bound@example.test"
                if "[role=\"main\"]" in script:
                    return True
                return None

            async def wait_for_timeout(self, _milliseconds):
                return None

        class Manager:
            page = Page()
            context = types.SimpleNamespace(cookies=lambda: [_valid_auth_cookie()])
            process = Process()

            async def initialize(self, **_kwargs):
                return True

            async def close(self):
                return {"success": True, "browser_process_stopped": True}

        with tempfile.TemporaryDirectory() as directory:
            runtime, handle = self._ready_profile(directory, "playwright")
            with patch("core.account_warmer.ProfileRuntime.from_environment", return_value=runtime), \
                 patch.dict(sys.modules, {"core.stealth_browser": types.SimpleNamespace(
                     PlaywrightStealthManager=Manager
                 )}):
                result = asyncio.run(warm_account_playwright(
                    "bound@example.test", "secret-password", 0,
                    profile_id=handle.profile_id,
                ))

        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "cleanup_failed")
        self.assertFalse(result["browser_process_stopped"])
        self.assertFalse(result["lease_released"])

    def test_unknown_lease_state_is_not_treated_as_released(self):
        from core.account_warmer import _lease_is_released
        from core.profile_runtime import _lease_release_verified

        class OpaqueLease:
            pass

        self.assertFalse(_lease_is_released(OpaqueLease()))
        self.assertFalse(_lease_release_verified(OpaqueLease()))

    def test_process_inspection_checks_live_nested_process_after_stopped_one(self):
        from core.profile_runtime import inspect_process_stopped

        class StoppedProcess:
            def poll(self):
                return 0

        class LiveProcess:
            def poll(self):
                return None

        class Adapter:
            process = StoppedProcess()
            browser_process = LiveProcess()

        self.assertFalse(inspect_process_stopped(Adapter()))

    def test_process_inspection_does_not_skip_live_child_when_adapter_is_closed(self):
        from core.profile_runtime import inspect_process_stopped

        class StoppedProcess:
            def poll(self):
                return 0

        class LiveProcess:
            def poll(self):
                return None

        class ClosedAdapter:
            closed = True
            process = StoppedProcess()
            browser_process = LiveProcess()

        self.assertFalse(inspect_process_stopped(ClosedAdapter()))

    def test_selenium_unknown_process_state_is_cleanup_failure(self):
        """A driver with no inspectable process fact must fail closed."""
        from core.account_warmer import _close_selenium_driver

        class Driver:
            def quit(self):
                return None

        cleanup = _close_selenium_driver(Driver())
        self.assertIs(cleanup["success"], False)
        self.assertIs(cleanup["browser_process_stopped"], False)

    def test_non_boolean_warmer_cleanup_fields_are_not_coerced_to_success(self):
        from core.account_warmer import _apply_cleanup_result

        class Runtime:
            def __init__(self):
                self.reasons = []

            def mark_cleanup_failed(self, _handle, reason):
                self.reasons.append(reason)

        result = {
            "success": True,
            "error_code": "",
            "cleanup_status": "not_started",
            "browser_process_stopped": True,
            "lease_released": False,
        }
        runtime = Runtime()
        _apply_cleanup_result(
            result, runtime, object(),
            {"success": "false", "browser_process_stopped": "true"},
        )
        self.assertIs(result["success"], False)
        self.assertEqual(result["error_code"], "cleanup_failed")
        self.assertEqual(result["cleanup_status"], "failed")
        self.assertIs(result["browser_process_stopped"], False)
        self.assertEqual(runtime.reasons, ["browser_cleanup_failed"])

    def test_non_boolean_warmer_process_fact_cannot_release_lease(self):
        from core.account_warmer import _apply_lease_result

        class Runtime:
            def __init__(self):
                self.reasons = []

            def mark_cleanup_failed(self, _handle, reason):
                self.reasons.append(reason)

        class ReleasedLease:
            release_verified = True
            held = False

        result = {
            "success": True,
            "error_code": "",
            "cleanup_status": "completed",
            "browser_process_stopped": "true",
            "lease_released": False,
        }
        runtime = Runtime()
        _apply_lease_result(result, runtime, object(), ReleasedLease())
        self.assertIs(result["success"], False)
        self.assertEqual(result["error_code"], "cleanup_failed")
        self.assertEqual(result["cleanup_status"], "failed")
        self.assertIs(result["lease_released"], False)
        self.assertEqual(runtime.reasons, ["browser_process_running"])

    def test_health_cleanup_rejects_non_boolean_adapter_facts(self):
        from core.profile_runtime import _apply_probe_cleanup

        class Runtime:
            def __init__(self):
                self.reasons = []

            def mark_cleanup_failed(self, _handle, reason):
                self.reasons.append(reason)

        observation = {"authenticated": True, "code": ""}
        runtime = Runtime()
        result = _apply_probe_cleanup(
            observation, runtime, object(), "selenium",
            {"success": "true", "browser_process_stopped": "true"},
        )
        self.assertIs(result["authenticated"], False)
        self.assertEqual(result["code"], "cleanup_failed")
        self.assertEqual(result["cleanup_status"], "failed")
        self.assertIs(result["browser_process_stopped"], False)
        self.assertEqual(runtime.reasons, ["browser_cleanup_failed"])

    def test_health_lease_failure_does_not_coerce_non_boolean_process_fact(self):
        from core.profile_runtime import _apply_probe_lease_release

        class Runtime:
            def __init__(self):
                self.reasons = []

            def mark_cleanup_failed(self, _handle, reason):
                self.reasons.append(reason)

        class OpaqueLease:
            pass

        observation = {
            "authenticated": True,
            "code": "authenticated",
            "cleanup_status": "completed",
            "browser_process_stopped": "false",
            "lease_released": False,
        }
        runtime = Runtime()
        _apply_probe_lease_release(observation, runtime, object(), OpaqueLease())

        self.assertIs(observation["authenticated"], False)
        self.assertEqual(observation["code"], "cleanup_failed")
        self.assertIs(observation["browser_process_stopped"], False)
        self.assertIs(observation["lease_released"], False)
        self.assertEqual(runtime.reasons, ["lease_release_failed"])

    def test_boolean_poll_result_is_not_an_exit_code(self):
        from core.profile_runtime import inspect_process_stopped

        class Adapter:
            def poll(self):
                return True

        self.assertIsNone(inspect_process_stopped(Adapter()))

    def test_playwright_lease_base_exception_still_closes_and_records_failure(self):
        from core.account_warmer import warm_account_playwright

        class Page:
            url = "https://mail.google.com/"

            async def goto(self, _url, **_kwargs):
                return None

            async def content(self):
                return "Inbox Compose Search mail"

            async def evaluate(self, script):
                if "querySelectorAll('[data-email" in script:
                    return "bound@example.test"
                if "[role=\"main\"]" in script:
                    return True
                return None

            async def wait_for_timeout(self, _milliseconds):
                return None

        class Manager:
            def __init__(self):
                self.page = Page()
                self.context = types.SimpleNamespace(cookies=lambda: [_valid_auth_cookie()])
                self.closed = False

            async def initialize(self, **_kwargs):
                return True

            async def close(self):
                self.closed = True
                return {"success": True, "browser_process_stopped": True}

        class Lease:
            def __init__(self):
                self.calls = 0
                self._held = True
                self._release_verified = False

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self._held = False
                self._release_verified = True

            def assert_stable(self):
                self.calls += 1
                if self.calls == 2:
                    raise KeyboardInterrupt()
                return self

        with tempfile.TemporaryDirectory() as directory:
            runtime, handle = self._ready_profile(directory, "playwright")
            lease = Lease()
            manager = Manager()
            runtime.lease = lambda *_args, **_kwargs: lease
            with patch("core.account_warmer.ProfileRuntime.from_environment", return_value=runtime), \
                 patch.dict(sys.modules, {"core.stealth_browser": types.SimpleNamespace(
                     PlaywrightStealthManager=lambda: manager
                 )}):
                result = asyncio.run(warm_account_playwright(
                    "bound@example.test", "secret-password", 0,
                    profile_id=handle.profile_id,
                ))
            profile_state = runtime.load(handle)["state"]

        self.assertTrue(manager.closed)
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "cleanup_failed")
        self.assertEqual(profile_state, "cleanup_failed")

    def test_playwright_lease_exit_base_exception_preserves_cleanup_failure(self):
        from core.account_warmer import warm_account_playwright

        class Page:
            url = "https://mail.google.com/"

            async def goto(self, _url, **_kwargs):
                return None

            async def content(self):
                return "Inbox Compose Search mail"

            async def evaluate(self, script):
                if "querySelectorAll('[data-email" in script:
                    return "bound@example.test"
                if "[role=\"main\"]" in script:
                    return True
                return None

            async def wait_for_timeout(self, _milliseconds):
                return None

        class Manager:
            def __init__(self):
                self.page = Page()
                self.context = types.SimpleNamespace(cookies=lambda: [_valid_auth_cookie()])
                self.closed = False

            async def initialize(self, **_kwargs):
                return True

            async def close(self):
                self.closed = True
                return {"success": True, "browser_process_stopped": True}

        class ExplodingExitLease:
            _held = True
            _release_verified = False

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self._held = False
                self._release_verified = True
                raise KeyboardInterrupt()

            def assert_stable(self):
                return self

        with tempfile.TemporaryDirectory() as directory:
            runtime, handle = self._ready_profile(directory, "playwright")
            manager = Manager()
            runtime.lease = lambda *_args, **_kwargs: ExplodingExitLease()
            with patch("core.account_warmer.ProfileRuntime.from_environment", return_value=runtime), \
                 patch.dict(sys.modules, {"core.stealth_browser": types.SimpleNamespace(
                     PlaywrightStealthManager=lambda: manager
                 )}):
                result = asyncio.run(warm_account_playwright(
                    "bound@example.test", "secret-password", 0,
                    profile_id=handle.profile_id,
                ))
            profile_state = runtime.load(handle)["state"]

        self.assertTrue(manager.closed)
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "cleanup_failed")
        self.assertEqual(profile_state, "cleanup_failed")


if __name__ == "__main__":
    unittest.main()
