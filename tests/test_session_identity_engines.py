"""Symmetric Playwright/Selenium transport contracts for session identity."""

import asyncio
import json
import tempfile
import types
import unittest
from unittest.mock import patch

from core.profile_runtime import BrowserProfileKernel, ProfileRuntime
from core.session_identity import IDENTITY_ENDPOINT, IdentityObservation


def _provider_body(*records):
    payload = {
        "accounts": [
            {"slot": slot, "email": email, "valid_session": valid}
            for slot, email, valid in records
        ]
    }
    return b")]}\'\n" + json.dumps(payload, separators=(",", ":")).encode()


class _ProviderResponse:
    status = 200

    def __init__(self, body):
        self._body = body

    async def body(self):
        return self._body


class _PlaywrightPage:
    def __init__(self, url, *, body=b"", shell=True, response_body=False):
        self.url = url
        self._body = body
        self.shell = shell
        self.response_body = response_body
        self.visited = []
        self.closed = False

    async def goto(self, url, **kwargs):
        self.visited.append((url, kwargs))
        if "ListAccounts" in url:
            self.url = IDENTITY_ENDPOINT
            return _ProviderResponse(self._body) if self.response_body else None
        self.url = "https://mail.google.com/mail/u/1/#inbox"
        return types.SimpleNamespace(status=200)

    async def content(self):
        return "Inbox Compose a@example.test"

    async def evaluate(self, script):
        if "querySelector('[role=\"main\"]" in script:
            return self.shell
        return None

    async def close(self):
        self.closed = True


class _PlaywrightContext:
    def __init__(self, business, provider):
        self.business = business
        self.provider = provider
        self.pages = [business]

    async def new_page(self):
        self.pages.append(self.provider)
        return self.provider

    async def cookies(self):
        return [{
            "name": "SID", "value": "live-session", "domain": ".google.com",
            "secure": True, "expires": 4102444800,
        }]


class _PlaywrightManager:
    def __init__(self, body):
        self.business = _PlaywrightPage("https://mail.google.com/mail/u/1/#inbox")
        self.provider = _PlaywrightPage(
            "about:blank", body=body, response_body=True,
        )
        self.context = _PlaywrightContext(self.business, self.provider)
        self.page = self.business
        self.closed = False

    async def initialize(self, **_kwargs):
        return True

    async def close(self):
        self.closed = True
        return {"success": True, "browser_process_stopped": True}

    def get_runtime_info(self):
        return {"channel": "chrome", "major_version": ""}


class _SwitchTo:
    def __init__(self, driver):
        self.driver = driver

    def new_window(self, _kind):
        handle = "identity-tab"
        self.driver.handles.append(handle)
        self.driver.current_window_handle = handle

    def window(self, handle):
        if handle not in self.driver.handles:
            raise RuntimeError("missing window")
        self.driver.current_window_handle = handle


class _SeleniumDriver:
    def __init__(self, body):
        self.body = body
        self.handles = ["business"]
        self.current_window_handle = "business"
        self.switch_to = _SwitchTo(self)
        self.current_url = "https://mail.google.com/mail/u/1/#inbox"
        self.visited = []
        self.closed = False

    @property
    def window_handles(self):
        return list(self.handles)

    @property
    def page_source(self):
        if self.current_window_handle == "identity-tab":
            return self.body.decode("utf-8")
        return "Inbox Compose a@example.test"

    def get(self, url):
        self.visited.append(url)
        if "ListAccounts" in url:
            self.current_url = IDENTITY_ENDPOINT
        else:
            self.current_url = "https://mail.google.com/mail/u/1/#inbox"

    def execute_script(self, script):
        if "document.body" in script:
            return self.body.decode("utf-8")
        if "querySelector('[role=\"main\"]" in script:
            return self.current_window_handle == "business"
        return None

    def get_cookies(self):
        return [{
            "name": "SID", "value": "live-session", "domain": ".google.com",
            "secure": True, "expires": 4102444800,
        }]

    def close(self):
        if self.current_window_handle != "business":
            self.handles.remove(self.current_window_handle)
            self.current_window_handle = "business"

    def quit(self):
        self.closed = True
        return {"success": True, "browser_process_stopped": True}


class SessionIdentityEngineTests(unittest.TestCase):
    def test_missing_slot_cannot_authenticate_from_native_manifest(self):
        facts = BrowserProfileKernel._identity_auth_facts(
            IdentityObservation("identity_unavailable", None, "identity_unavailable"),
            expected_email="a@example.test",
            manifest={"identity_state": "native", "email": "a@example.test"},
            text="Inbox Compose", origin="https://mail.google.com/",
            cookies=[{"name": "SID", "value": "live", "domain": ".google.com",
                      "secure": True, "expires": 4102444800}],
            application_shell=True,
        )
        self.assertFalse(facts["authenticated"])
        self.assertEqual(facts["status"], "identity_unavailable")
        self.assertIsNone(facts["_evidence_token"])

    def _runtime(self, engine):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        runtime = ProfileRuntime(directory.name)
        handle = runtime.provision("a@example.test", engine)
        runtime.bind(handle, "a@example.test")
        runtime.mark_ready(handle)
        return runtime, handle

    def test_playwright_provider_overrides_dom_email_and_stays_in_lease(self):
        runtime, handle = self._runtime("playwright")
        manager = _PlaywrightManager(_provider_body((1, "b@example.test", True)))
        events = []
        real_lease = runtime.lease

        class Lease:
            def __enter__(self):
                events.append("lease-enter")
                return real.__enter__()

            def __exit__(self, *args):
                events.append("lease-exit")
                return real.__exit__(*args)

        real = None

        def lease(*args, **kwargs):
            nonlocal real
            real = real_lease(*args, **kwargs)
            return Lease()

        runtime.lease = lease
        with patch.dict(
            __import__("sys").modules,
            {"core.stealth_browser": types.SimpleNamespace(
                PlaywrightStealthManager=lambda: manager
            )},
        ):
            observation = asyncio.run(
                BrowserProfileKernel(runtime).probe_playwright(
                    handle, expected_email="a@example.test"
                )
            )
        self.assertEqual(observation.status, "account_mismatch")
        self.assertFalse(observation["authenticated"])
        self.assertNotEqual(observation.get("observed_email"), "a@example.test")
        self.assertTrue(manager.provider.closed)
        self.assertEqual(manager.provider.visited[0][0], IDENTITY_ENDPOINT)
        self.assertEqual(manager.provider.visited[0][1]["timeout"], 15000)
        self.assertLess(events.index("lease-enter"), events.index("lease-exit"))

    def test_selenium_provider_overrides_dom_email_and_restores_window_set(self):
        runtime, handle = self._runtime("selenium")
        driver = _SeleniumDriver(_provider_body((1, "b@example.test", True)))
        fake_runner = types.SimpleNamespace(
            create_driver=lambda **_kwargs: driver,
            get_runtime_info=lambda _driver: {},
        )
        with patch.dict(__import__("sys").modules, {"core.selenium_runner": fake_runner}):
            observation = BrowserProfileKernel(runtime).probe_selenium(
                handle, expected_email="a@example.test"
            )
        self.assertEqual(observation.status, "account_mismatch")
        self.assertFalse(observation["authenticated"])
        self.assertEqual(driver.window_handles, ["business"])
        self.assertEqual(driver.current_window_handle, "business")
        self.assertIn(IDENTITY_ENDPOINT, driver.visited)
        self.assertTrue(driver.closed)


if __name__ == "__main__":
    unittest.main()
