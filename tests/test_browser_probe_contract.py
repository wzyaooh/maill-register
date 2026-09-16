import asyncio
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from core.profile_runtime import (
    BrowserProfileKernel,
    ProfileBusyError,
    ProfileRuntime,
)


class _AsyncPage:
    def __init__(self, text="Inbox Compose Search mail", observed=None):
        self.url = "https://mail.google.com/mail/u/0/#inbox"
        self.text = text
        self.observed = observed
        self.visited = []

    async def goto(self, url, **_kwargs):
        self.visited.append(url)
        return types.SimpleNamespace(status=200)

    async def wait_for_timeout(self, *_args):
        return None

    async def content(self):
        return self.text

    async def evaluate(self, script):
        if "querySelectorAll('[data-email" in script:
            return self.observed
        if "querySelector('[role=\"main\"]" in script:
            return True
        return None


class _AsyncContext:
    def __init__(self, cookies=None):
        self._cookies = cookies or [{
            "name": "SID", "value": "live-session", "domain": ".google.com",
            "secure": True, "expires": 4102444800,
        }]
        self.closed = False

    async def cookies(self):
        return self._cookies

    async def close(self):
        self.closed = True


class _PlaywrightManager:
    def __init__(self, page):
        self.page = page
        self.context = _AsyncContext()
        self.closed = False

    async def initialize(self, **_kwargs):
        return True

    async def close(self):
        self.closed = True
        await self.context.close()

    def get_runtime_info(self):
        return {"channel": "chrome", "major_version": ""}


class BrowserProbeContractTests(unittest.TestCase):
    def _runtime(self, engine="playwright", identity_state="native"):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        runtime = ProfileRuntime(directory.name)
        handle = runtime.provision("user@example.test", engine)
        runtime.bind(handle, "user@example.test")
        if identity_state != "native":
            manifest = runtime.load(handle)
            manifest["identity_state"] = identity_state
            manifest["identity_verified"] = False
            manifest.pop("identity_verified_email", None)
            manifest["state"] = "ready"
            runtime._atomic_json(handle.manifest_path, manifest)
        else:
            runtime.mark_ready(handle)
        return runtime, handle

    def test_playwright_probe_reports_authenticated_facts_and_closes_under_lease(self):
        runtime, handle = self._runtime()
        page = _AsyncPage(observed="user@example.test")
        manager = _PlaywrightManager(page)
        events = []
        real_lease = runtime.lease

        class Lease:
            def __init__(self, inner):
                self.inner = inner

            def __enter__(self):
                self.inner.__enter__()
                events.append("lease-enter")
                return self

            def __exit__(self, *args):
                events.append("lease-exit")
                return self.inner.__exit__(*args)

        runtime.lease = lambda h, operation, timeout=0: Lease(
            real_lease(h, operation, timeout)
        )
        fake = types.SimpleNamespace(PlaywrightStealthManager=lambda: manager)
        with patch.dict(sys.modules, {"core.stealth_browser": fake}):
            observation = asyncio.run(
                BrowserProfileKernel(runtime).probe_playwright(
                    handle, expected_email="user@example.test"
                )
            )

        self.assertEqual(observation.status, "authenticated")
        self.assertTrue(observation["authenticated"])
        self.assertEqual(observation["observed_email"], "user@example.test")
        self.assertEqual(observation["origin"], "https://mail.google.com")
        self.assertTrue(manager.closed)
        self.assertLess(events.index("lease-enter"), events.index("lease-exit"))
        self.assertTrue(events)

    def test_probe_status_uses_the_same_bound_identity_as_mapping(self):
        runtime, handle = self._runtime()
        manager = _PlaywrightManager(_AsyncPage(observed="user@example.test"))
        with patch.dict(sys.modules, {
            "core.stealth_browser": types.SimpleNamespace(
                PlaywrightStealthManager=lambda: manager
            )
        }):
            observation = asyncio.run(
                BrowserProfileKernel(runtime).probe_playwright(
                    handle, expected_email="user@example.test"
                )
            )

        self.assertTrue(observation["authenticated"])
        self.assertEqual(observation.status, "authenticated")

    def test_playwright_probe_rejects_observed_account_mismatch(self):
        runtime, handle = self._runtime()
        manager = _PlaywrightManager(_AsyncPage(observed="other@example.test"))
        with patch.dict(sys.modules, {
            "core.stealth_browser": types.SimpleNamespace(
                PlaywrightStealthManager=lambda: manager
            )
        }):
            observation = asyncio.run(
                BrowserProfileKernel(runtime).probe_playwright(
                    handle, expected_email="user@example.test"
                )
            )
        self.assertEqual(observation.status, "account_mismatch")
        self.assertFalse(observation["authenticated"])
        self.assertTrue(manager.closed)

    def test_playwright_probe_normalizes_email_embedded_in_account_label(self):
        runtime, handle = self._runtime()
        page = _AsyncPage(observed="Google Account: user@example.test")
        manager = _PlaywrightManager(page)
        with patch.dict(sys.modules, {
            "core.stealth_browser": types.SimpleNamespace(
                PlaywrightStealthManager=lambda: manager
            )
        }):
            observation = asyncio.run(
                BrowserProfileKernel(runtime).probe_playwright(
                    handle, expected_email="user@example.test"
                )
            )
        self.assertEqual(observation.status, "authenticated")
        self.assertEqual(observation["observed_email"], "user@example.test")

    def test_playwright_login_page_with_stale_auth_cookie_is_not_authenticated(self):
        runtime, handle = self._runtime()
        page = _AsyncPage(text="Sign in to Gmail", observed=None)
        manager = _PlaywrightManager(page)
        with patch.dict(sys.modules, {
            "core.stealth_browser": types.SimpleNamespace(
                PlaywrightStealthManager=lambda: manager
            )
        }):
            observation = asyncio.run(
                BrowserProfileKernel(runtime).probe_playwright(
                    handle, expected_email="user@example.test"
                )
            )
        self.assertEqual(observation.status, "login_required")
        self.assertFalse(observation["authenticated"])

    def test_playwright_probe_requires_identity_for_reconstructed_profile(self):
        runtime, handle = self._runtime(identity_state="identity_reconstructed")
        manager = _PlaywrightManager(_AsyncPage(observed=None))
        with patch.dict(sys.modules, {
            "core.stealth_browser": types.SimpleNamespace(
                PlaywrightStealthManager=lambda: manager
            )
        }):
            observation = asyncio.run(
                BrowserProfileKernel(runtime).probe_playwright(
                    handle, expected_email="user@example.test"
                )
            )
        self.assertEqual(observation.status, "account_mismatch")
        self.assertFalse(observation["authenticated"])

    def test_verified_reconstructed_profile_still_requires_a_fresh_identity_label(self):
        runtime, handle = self._runtime(identity_state="identity_reconstructed")
        manifest = runtime.load(handle)
        manifest["identity_verified"] = True
        manifest["identity_verified_email"] = "user@example.test"
        runtime._atomic_json(handle.manifest_path, manifest)
        manager = _PlaywrightManager(_AsyncPage(observed=None))
        with patch.dict(sys.modules, {
            "core.stealth_browser": types.SimpleNamespace(
                PlaywrightStealthManager=lambda: manager
            )
        }):
            observation = asyncio.run(
                BrowserProfileKernel(runtime).probe_playwright(
                    handle, expected_email="user@example.test"
                )
            )
        self.assertEqual(observation.status, "account_mismatch")
        self.assertFalse(observation["authenticated"])

    def test_probe_does_not_start_wrong_engine(self):
        runtime, handle = self._runtime(engine="selenium")
        with patch.dict(sys.modules, {
            "core.stealth_browser": types.SimpleNamespace(
                PlaywrightStealthManager=lambda: self.fail("wrong adapter started")
            )
        }):
            observation = asyncio.run(
                BrowserProfileKernel(runtime).probe_playwright(handle)
            )
        self.assertEqual(observation.status, "profile_conflict")

    def test_probe_busy_lease_is_structured(self):
        runtime, handle = self._runtime()
        original = runtime.lease

        def busy(*_args, **_kwargs):
            raise ProfileBusyError("already held")

        runtime.lease = busy
        observation = asyncio.run(BrowserProfileKernel(runtime).probe_playwright(handle))
        self.assertEqual(observation.status, "profile_busy")
        runtime.lease = original

    def test_playwright_cleanup_failure_overrides_authenticated_observation(self):
        runtime, handle = self._runtime()

        class FailingManager(_PlaywrightManager):
            async def close(self):
                raise RuntimeError("browser process survived")

        manager = FailingManager(_AsyncPage(observed="user@example.test"))
        with patch.dict(sys.modules, {
            "core.stealth_browser": types.SimpleNamespace(
                PlaywrightStealthManager=lambda: manager
            )
        }):
            observation = asyncio.run(
                BrowserProfileKernel(runtime).probe_playwright(
                    handle, expected_email="user@example.test"
                )
            )

        self.assertEqual(observation.status, "cleanup_failed")
        self.assertFalse(observation["authenticated"])
        self.assertEqual(observation["cleanup_status"], "failed")
        self.assertFalse(observation["browser_process_stopped"])
        self.assertFalse(observation["lease_released"])
        self.assertEqual(runtime.load(handle)["state"], "cleanup_failed")

    def test_playwright_unverified_lease_release_overrides_authenticated_observation(self):
        runtime, handle = self._runtime()
        manager = _PlaywrightManager(_AsyncPage(observed="user@example.test"))

        class StickyLease:
            _held = True

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                # Simulate an OS lock that could not be verified as released.
                self._held = True

            def assert_stable(self):
                return self

        runtime.lease = lambda *_args, **_kwargs: StickyLease()
        with patch.dict(sys.modules, {
            "core.stealth_browser": types.SimpleNamespace(
                PlaywrightStealthManager=lambda: manager
            )
        }):
            observation = asyncio.run(
                BrowserProfileKernel(runtime).probe_playwright(
                    handle, expected_email="user@example.test"
                )
            )

        self.assertEqual(observation.status, "cleanup_failed")
        self.assertFalse(observation["authenticated"])
        self.assertFalse(observation["lease_released"])
        self.assertEqual(runtime.load(handle)["state"], "cleanup_failed")

    def test_playwright_lease_exit_base_exception_preserves_probe_failure(self):
        runtime, handle = self._runtime()

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

        manager = _PlaywrightManager(_AsyncPage(observed="user@example.test"))
        runtime.lease = lambda *_args, **_kwargs: ExplodingExitLease()
        with patch.dict(sys.modules, {
            "core.stealth_browser": types.SimpleNamespace(
                PlaywrightStealthManager=lambda: manager
            )
        }):
            observation = asyncio.run(
                BrowserProfileKernel(runtime).probe_playwright(
                    handle, expected_email="user@example.test"
                )
            )

        self.assertTrue(manager.closed)
        self.assertEqual(observation.status, "cleanup_failed")
        self.assertFalse(observation["authenticated"])
        self.assertEqual(runtime.load(handle)["state"], "cleanup_failed")

    def test_selenium_cleanup_failure_overrides_authenticated_observation(self):
        runtime, handle = self._runtime(engine="selenium")

        class FailingDriver:
            current_url = "https://mail.google.com/"
            page_source = "Inbox Compose Search mail"

            def execute_script(self, script):
                if "querySelectorAll('[data-email" in script:
                    return "user@example.test"
                if "querySelector('[role=\"main\"]" in script:
                    return True
                return None

            def get_cookies(self):
                return [{
                    "name": "SID", "value": "live-session", "domain": ".google.com",
                    "secure": True, "expires": 4102444800,
                }]

            def get(self, _url):
                return None

            def quit(self):
                raise RuntimeError("driver process survived")

        driver = FailingDriver()
        fake_runner = types.SimpleNamespace(
            create_driver=lambda **_kwargs: driver,
            get_runtime_info=lambda _driver: {},
        )
        with patch.dict(sys.modules, {"core.selenium_runner": fake_runner}):
            observation = BrowserProfileKernel(runtime).probe_selenium(
                handle, expected_email="user@example.test"
            )

        self.assertEqual(observation.status, "cleanup_failed")
        self.assertFalse(observation["authenticated"])
        self.assertEqual(observation["cleanup_status"], "failed")
        self.assertFalse(observation["browser_process_stopped"])
        self.assertFalse(observation["lease_released"])
        self.assertEqual(runtime.load(handle)["state"], "cleanup_failed")

    def test_selenium_cancelled_quit_is_reported_as_cleanup_failure(self):
        runtime, handle = self._runtime(engine="selenium")

        class CancelledDriver:
            current_url = "https://mail.google.com/"
            page_source = "Inbox Compose Search mail"

            def execute_script(self, script):
                if "querySelectorAll('[data-email" in script:
                    return "user@example.test"
                if "querySelector('[role=\"main\"]" in script:
                    return True
                return None

            def get_cookies(self):
                return [{
                    "name": "SID", "value": "live-session", "domain": ".google.com",
                    "secure": True, "expires": 4102444800,
                }]

            def get(self, _url):
                return None

            def quit(self):
                raise asyncio.CancelledError()

        driver = CancelledDriver()
        fake_runner = types.SimpleNamespace(
            create_driver=lambda **_kwargs: driver,
            get_runtime_info=lambda _driver: {},
        )
        with patch.dict(sys.modules, {"core.selenium_runner": fake_runner}):
            observation = BrowserProfileKernel(runtime).probe_selenium(
                handle, expected_email="user@example.test"
            )

        self.assertEqual(observation.status, "cleanup_failed")
        self.assertFalse(observation["authenticated"])
        self.assertFalse(observation["lease_released"])
        self.assertEqual(runtime.load(handle)["state"], "cleanup_failed")

    def test_playwright_runtime_mismatch_is_reported_before_navigation(self):
        runtime, handle = self._runtime()
        manifest = runtime.load(handle)
        manifest["browser"] = {"channel": "chrome", "major_version": "120"}
        runtime._atomic_json(handle.manifest_path, manifest)
        page = _AsyncPage(observed="user@example.test")
        manager = _PlaywrightManager(page)
        manager.get_runtime_info = lambda: {
            "channel": "chrome", "major_version": "121"
        }
        with patch.dict(sys.modules, {
            "core.stealth_browser": types.SimpleNamespace(
                PlaywrightStealthManager=lambda: manager
            )
        }):
            observation = asyncio.run(
                BrowserProfileKernel(runtime).probe_playwright(
                    handle, expected_email="user@example.test"
                )
            )
        self.assertEqual(observation.status, "runtime_mismatch")
        self.assertEqual(page.visited, [])
        self.assertTrue(manager.closed)

    def test_playwright_constructor_failure_after_launch_is_cleanup_failure(self):
        runtime, handle = self._runtime()

        def constructor():
            raise RuntimeError("browser process may have started")

        with patch.dict(sys.modules, {
            "core.stealth_browser": types.SimpleNamespace(
                PlaywrightStealthManager=constructor
            )
        }):
            observation = asyncio.run(
                BrowserProfileKernel(runtime).probe_playwright(
                    handle, expected_email="user@example.test"
                )
            )

        self.assertEqual(observation.status, "cleanup_failed")
        self.assertFalse(observation["authenticated"])
        self.assertFalse(observation["browser_process_stopped"])
        self.assertFalse(observation["lease_released"])
        self.assertEqual(runtime.load(handle)["state"], "cleanup_failed")

    def test_selenium_constructor_failure_after_launch_is_cleanup_failure(self):
        runtime, handle = self._runtime(engine="selenium")
        fake_runner = types.SimpleNamespace(
            create_driver=lambda **_kwargs: (_ for _ in ()).throw(
                RuntimeError("driver process may have started")
            ),
            get_runtime_info=lambda _driver: {},
        )
        with patch.dict(sys.modules, {"core.selenium_runner": fake_runner}):
            observation = BrowserProfileKernel(runtime).probe_selenium(
                handle, expected_email="user@example.test"
            )

        self.assertEqual(observation.status, "cleanup_failed")
        self.assertFalse(observation["authenticated"])
        self.assertFalse(observation["browser_process_stopped"])
        self.assertFalse(observation["lease_released"])
        self.assertEqual(runtime.load(handle)["state"], "cleanup_failed")

    def test_playwright_probe_cancellation_finishes_close_before_lease_release(self):
        runtime, handle = self._runtime()

        class CancellablePage(_AsyncPage):
            def __init__(self):
                super().__init__(observed="user@example.test")
                self.started = None

            async def goto(self, _url, **_kwargs):
                self.started.set()
                await asyncio.Event().wait()

        class CancellableManager(_PlaywrightManager):
            def __init__(self, page):
                super().__init__(page)
                self.close_started = None
                self.close_release = None

            async def close(self):
                self.close_started.set()
                await self.close_release.wait()
                self.closed = True

        page = CancellablePage()
        manager = CancellableManager(page)

        async def exercise():
            page.started = asyncio.Event()
            manager.close_started = asyncio.Event()
            manager.close_release = asyncio.Event()
            task = asyncio.create_task(
                BrowserProfileKernel(runtime).probe_playwright(
                    handle, expected_email="user@example.test"
                )
            )
            await page.started.wait()
            task.cancel()
            await manager.close_started.wait()
            # A second cancellation must not cut off adapter shutdown.
            task.cancel()
            manager.close_release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task

        with patch.dict(sys.modules, {
            "core.stealth_browser": types.SimpleNamespace(
                PlaywrightStealthManager=lambda: manager
            )
        }):
            asyncio.run(exercise())

        self.assertTrue(manager.closed)
        with runtime.lease(handle, "probe-after-cancel"):
            pass

    def test_selenium_runtime_mismatch_is_reported_before_navigation(self):
        runtime, handle = self._runtime(engine="selenium")
        manifest = runtime.load(handle)
        manifest["browser"] = {"channel": "chrome", "major_version": "120"}
        runtime._atomic_json(handle.manifest_path, manifest)

        class Driver:
            current_url = "https://mail.google.com/"
            page_source = "Inbox Compose Search mail"

            def __init__(self):
                self.visited = []
                self.closed = False

            def get(self, url):
                self.visited.append(url)

            def execute_script(self, _script):
                return "user@example.test"

            def get_cookies(self):
                return [{"name": "SID"}]

            def quit(self):
                self.closed = True

        driver = Driver()
        fake_runner = types.SimpleNamespace(
            create_driver=lambda **_kwargs: driver,
            get_runtime_info=lambda _driver: {
                "channel": "chrome", "major_version": "121"
            },
        )
        with patch.dict(sys.modules, {
            "core.selenium_runner": fake_runner,
        }):
            observation = BrowserProfileKernel(runtime).probe_selenium(
                handle, expected_email="user@example.test"
            )
        self.assertEqual(observation.status, "runtime_mismatch")
        self.assertEqual(driver.visited, [])
        self.assertTrue(driver.closed)


if __name__ == "__main__":
    unittest.main()
