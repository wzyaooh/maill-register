import ast
import asyncio
import pathlib
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

from core.database import DatabaseManager


ROOT = pathlib.Path(__file__).resolve().parents[1]


def _auth_scenario(name):
    scenario = {
        "final_url": "https://mail.google.com/mail/u/0/#inbox",
        "text": "Inbox Compose Search mail",
        "observed_email": "user@gmail.com",
        "application_shell": True,
        "cookies": [{
            "name": "SID", "value": "live-registration-session",
            "domain": ".google.com", "secure": True,
            "expires": 4102444800,
        }],
    }
    if name == "login":
        scenario.update(
            final_url="https://accounts.google.com/signin/v2/identifier",
            text="Inbox Compose Sign in to Google Email or phone",
        )
    elif name == "challenge":
        scenario["text"] += " Verify it's you security challenge"
    elif name == "weak_url_body":
        scenario.update(
            final_url="https://www.youtube.com/",
            text="Inbox Compose Welcome to Google",
        )
    elif name == "missing_cookie":
        scenario["cookies"] = []
    elif name == "missing_shell":
        scenario["application_shell"] = False
    elif name == "missing_identity":
        scenario["observed_email"] = None
    elif name == "mismatched_identity":
        scenario["observed_email"] = "other@gmail.com"
    return scenario


class _PlaywrightElement:
    async def is_visible(self):
        return True

    async def click(self, **_kwargs):
        return None

    async def fill(self, _value):
        return None

    async def dispatch_event(self, _name):
        return None

    async def scroll_into_view_if_needed(self):
        return None


class _PlaywrightContext:
    def __init__(self, scenario):
        self.scenario = scenario

    async def clear_cookies(self):
        return None

    async def cookies(self):
        return list(self.scenario["cookies"])


class _PlaywrightPage:
    def __init__(self, scenario):
        self.scenario = scenario
        self.url = "https://accounts.google.com/signup"
        self.visited = []
        self.context = _PlaywrightContext(scenario)
        self.element = _PlaywrightElement()

    async def goto(self, url, **_kwargs):
        self.visited.append(url)
        if url == "https://mail.google.com/":
            self.url = self.scenario["final_url"]
        else:
            self.url = url
        return types.SimpleNamespace(status=200)

    async def wait_for_timeout(self, _timeout):
        return None

    async def content(self):
        return self.scenario["text"]

    async def wait_for_selector(self, _selector, **_kwargs):
        return self.element

    async def query_selector(self, selector):
        if "recaptcha" in selector.lower():
            return None
        return self.element

    async def query_selector_all(self, _selector):
        return []

    async def evaluate(self, script, *_args):
        if "querySelectorAll('[data-email" in script:
            return self.scenario["observed_email"]
        if "[role=\"main\"]" in script:
            return self.scenario["application_shell"]
        return None


class _PlaywrightManager:
    def __init__(self, scenario, close_mode):
        self.page = _PlaywrightPage(scenario)
        self.context = self.page.context
        self.is_mobile = False
        self.close_mode = close_mode

    async def initialize(self, **_kwargs):
        return True

    def get_runtime_info(self):
        return {}

    async def natural_type(self, *_args, **_kwargs):
        return True

    async def fill_birthday_gender(self, *_args, **_kwargs):
        return None

    async def close(self):
        if self.close_mode == "close_failure":
            raise RuntimeError("close failed")
        if self.close_mode == "live_process":
            return {"success": True, "browser_process_stopped": False}
        return {"success": True, "browser_process_stopped": True}


class _Process:
    def __init__(self):
        self.exit_code = None

    def poll(self):
        return self.exit_code


class _SeleniumDriver:
    def __init__(self, scenario, close_mode):
        self.scenario = scenario
        self.close_mode = close_mode
        self.current_url = "https://accounts.google.com/signup"
        self.visited = []
        self.service = types.SimpleNamespace(process=_Process())

    @property
    def page_source(self):
        return self.scenario["text"]

    def get(self, url):
        self.visited.append(url)
        if url == "https://mail.google.com/":
            self.current_url = self.scenario["final_url"]
        else:
            self.current_url = url

    def execute_script(self, script, *_args):
        if "querySelectorAll('[data-email" in script:
            return self.scenario["observed_email"]
        if "[role=\"main\"]" in script:
            return self.scenario["application_shell"]
        return None

    def get_cookies(self):
        return list(self.scenario["cookies"])

    def quit(self):
        if self.close_mode == "close_failure":
            raise RuntimeError("quit failed")
        if self.close_mode != "live_process":
            self.service.process.exit_code = 0
        return None


class _Lease:
    def __init__(self, release_ok=True):
        self.release_ok = release_ok

    def acquire(self):
        return self

    def assert_stable(self):
        return self

    def release(self):
        return self.release_ok


def _registration_runtime(engine, release_ok=True):
    handle = types.SimpleNamespace(
        path=pathlib.Path("/tmp") / ("test-" + engine + "-profile"),
        profile_id="profile-" + engine,
    )
    lease = _Lease(release_ok=release_ok)
    runtime = Mock()
    runtime.provision.return_value = handle
    runtime.load.return_value = {"engine": engine, "identity_state": "native"}
    runtime.lease.return_value = lease
    return runtime, handle


def _account_store():
    store = Mock()
    store.save.return_value = True
    store.db.update_profile_state.return_value = True
    store.db.update_operation_results.return_value = True
    return store


class RegistrationProfileLifecycleContractTests(unittest.TestCase):
    def _source(self, relative):
        return (ROOT / relative).read_text(encoding="utf-8")

    def _run_playwright_registration(self, *, auth_case="valid", warming=False,
                                     close_mode="success", release_ok=True):
        from core.runners import async_playwright_flow

        scenario = _auth_scenario(auth_case)
        manager = _PlaywrightManager(scenario, close_mode)
        runtime, _handle = _registration_runtime("playwright", release_ok)
        accounts = _account_store()
        with patch("core.runners.PlaywrightStealthManager", return_value=manager), \
             patch("core.runners.ProfileRuntime.from_environment", return_value=runtime), \
             patch("core.runners.handle_verification", new=AsyncMock(
                 return_value=(True, "sms_fake", False)
             )), \
             patch("core.runners._try_click", new=AsyncMock(return_value=True)), \
             patch("core.runners.Config", types.SimpleNamespace(
                 ENABLE_SESSION_WARMING=warming,
             )), \
             patch("core.account_manager.account_manager", accounts), \
             patch("core.progress.print_success"), \
             patch("core.telegram_notifier.notifier", Mock()):
            result = asyncio.run(async_playwright_flow(
                0, 1, "user", "User", "Test", "password",
                None, None, None, "2", "4", "1990", "1",
                True, "standard", job_id="job-1", attempt_id="attempt-1",
                order_store=object(),
            ))
        return result, accounts, manager, runtime

    def _run_selenium_registration(self, *, auth_case="valid", warming=False,
                                   close_mode="success", release_ok=True):
        from core.selenium_runner import run_selenium_flow

        scenario = _auth_scenario(auth_case)
        driver = _SeleniumDriver(scenario, close_mode)
        runtime, _handle = _registration_runtime("selenium", release_ok)
        accounts = _account_store()
        with patch("core.selenium_runner.ProfileRuntime.from_environment", return_value=runtime), \
             patch("core.selenium_runner.create_driver", return_value=driver), \
             patch("core.selenium_runner.get_runtime_info", return_value={}), \
             patch("core.selenium_runner.create_account_selenium", return_value=(True, "sms_fake")), \
             patch("core.selenium_runner.account_manager", accounts), \
             patch("core.selenium_runner.warm_up_session"), \
             patch("core.selenium_runner.Config", types.SimpleNamespace(
                 BROWSER_TIMEOUT=1,
                 ENABLE_COOKIE_REAPER=False,
                 ENABLE_FINGERPRINT_MASKING=False,
                 ENABLE_SESSION_WARMING=warming,
                 YOUR_BIRTHDAY="2 4 1990",
                 YOUR_GENDER="1",
             )), \
             patch("core.telegram_notifier.notifier", Mock()):
            result = run_selenium_flow(
                0, 1, "user", "password", warmup_minutes=0,
                stealth_mode=False, use_sms_api=True,
                job_id="job-1", attempt_id="attempt-1",
                order_store=object(), return_result=True,
            )
        return result, accounts, driver, runtime

    def test_registration_flows_do_not_derive_profiles_from_usernames(self):
        for relative in ("core/runners.py", "core/selenium_runner.py"):
            with self.subTest(relative=relative):
                source = self._source(relative)
                self.assertNotIn("_account_profile_path", source)
                self.assertNotIn('os.path.join("data", "profiles"', source)
                self.assertNotIn("os.path.join('data', 'profiles'", source)

    def test_registration_launches_consume_manifest_and_lease_owned_flag(self):
        playwright = self._source("core/runners.py")
        selenium = self._source("core/selenium_runner.py")
        self.assertIn("ProfileRuntime.from_environment", playwright)
        self.assertIn("profile_manifest=profile_manifest", playwright)
        self.assertIn("lease_owned=True", playwright)
        self.assertIn("ProfileRuntime.from_environment", selenium)
        self.assertIn("profile_manifest=profile_manifest", selenium)
        self.assertIn("lease_owned=True", selenium)

    def test_registration_warm_calls_use_profile_id_not_profile_path(self):
        for relative in ("core/runners.py", "core/selenium_runner.py"):
            with self.subTest(relative=relative):
                source = self._source(relative)
                self.assertNotIn("profile_path=profile_path", source)
                self.assertRegex(source, r"profile_id=profile_handle\.profile_id")

    def test_registration_cleanup_requires_explicit_browser_and_lease_proof(self):
        from core import runners, selenium_runner

        cleanup_ok = {"success": True, "browser_process_stopped": True}
        cleanup_missing = {"success": True}
        cleanup_false = {"success": False, "browser_process_stopped": True}
        for module in (runners, selenium_runner):
            with self.subTest(module=module.__name__):
                verifier = module.registration_cleanup_verified
                self.assertTrue(verifier(cleanup_ok))
                self.assertFalse(verifier(cleanup_missing))
                self.assertFalse(verifier(cleanup_false))

                class Lease:
                    def __init__(self, value):
                        self.value = value

                    def release(self):
                        return self.value

                self.assertTrue(module.release_registration_lease(Lease(True)))
                self.assertFalse(module.release_registration_lease(Lease(1)))
                self.assertFalse(module.release_registration_lease(Lease(False)))

                class RaisingLease:
                    def release(self):
                        raise RuntimeError("release failed")

                self.assertFalse(module.release_registration_lease(RaisingLease()))

    def test_selenium_driver_close_uses_service_process_as_stop_proof(self):
        from core.selenium_runner import _close_registration_driver

        class Process:
            def __init__(self, exit_code=None):
                self.exit_code = exit_code

            def poll(self):
                return self.exit_code

        class Service:
            def __init__(self, process):
                self.process = process

        class Driver:
            def __init__(self, process, quit_error=None):
                self.service = Service(process)
                self.quit_error = quit_error

            def quit(self):
                if self.quit_error is not None:
                    raise self.quit_error
                self.service.process.exit_code = 0
                return None

        stopped = _close_registration_driver(Driver(Process()))
        self.assertTrue(stopped["success"])
        self.assertTrue(stopped["browser_process_stopped"])

        still_running = _close_registration_driver(
            Driver(Process(), quit_error=RuntimeError("quit failed"))
        )
        self.assertFalse(still_running["success"])
        self.assertFalse(still_running["browser_process_stopped"])

        class LiveDriver(Driver):
            def quit(self):
                return None

        live = _close_registration_driver(LiveDriver(Process()))
        self.assertFalse(live["success"])
        self.assertFalse(live["browser_process_stopped"])

    def test_successful_registration_is_downgraded_when_required_cleanup_fails(self):
        cases = (
            ("close_failure", True),
            ("live_process", True),
            ("success", False),
        )
        runners = (
            ("playwright", self._run_playwright_registration),
            ("selenium", self._run_selenium_registration),
        )
        for engine, run in runners:
            for warming in (False, True):
                for close_mode, release_ok in cases:
                    with self.subTest(
                        engine=engine, warming=warming,
                        close_mode=close_mode, release_ok=release_ok,
                    ):
                        result, accounts, _adapter, runtime = run(
                            warming=warming, close_mode=close_mode,
                            release_ok=release_ok,
                        )
                        self.assertIsInstance(result, dict)
                        self.assertFalse(result["success"])
                        self.assertEqual(result["error_code"], "cleanup_failed")
                        self.assertTrue(result["registration_result"]["success"])
                        self.assertEqual(
                            result["registration_result"]["status"], "created"
                        )
                        durable = accounts.save.call_args.kwargs["registration_result"]
                        self.assertTrue(durable["success"])
                        self.assertEqual(durable["status"], "created")
                        runtime.mark_cleanup_failed.assert_called()

    def test_registration_identity_proof_fails_closed_for_both_engines(self):
        rejected = (
            "login", "challenge", "weak_url_body", "missing_cookie",
            "missing_shell", "missing_identity", "mismatched_identity",
        )
        runners = (
            ("playwright", self._run_playwright_registration),
            ("selenium", self._run_selenium_registration),
        )
        for engine, run in runners:
            for auth_case in rejected:
                with self.subTest(engine=engine, auth_case=auth_case):
                    result, accounts, _adapter, runtime = run(auth_case=auth_case)
                    success = result.get("success") if isinstance(result, dict) else result[0]
                    self.assertFalse(success)
                    self.assertFalse(accounts.save.called)
                    runtime.bind.assert_not_called()
                    runtime.mark_ready.assert_not_called()

    def test_registration_identity_proof_accepts_only_matching_mail_session(self):
        runners = (
            ("playwright", self._run_playwright_registration),
            ("selenium", self._run_selenium_registration),
        )
        for engine, run in runners:
            with self.subTest(engine=engine):
                result, accounts, adapter, runtime = run(auth_case="valid")
                self.assertTrue(result["success"])
                if engine == "playwright":
                    self.assertEqual(
                        adapter.page.visited[-1:], ["https://mail.google.com/"]
                    )
                else:
                    self.assertEqual(adapter.visited[-1:], ["https://mail.google.com/"])
                self.assertTrue(accounts.save.called)
                runtime.bind.assert_called_once()
                runtime.mark_ready.assert_called_once()

    def test_modules_remain_parseable(self):
        for relative in ("core/runners.py", "core/selenium_runner.py"):
            ast.parse(self._source(relative), filename=relative)

    def test_account_persists_registration_and_warm_results_separately(self):
        with tempfile.TemporaryDirectory() as directory:
            db = DatabaseManager(str(pathlib.Path(directory) / "database.db"))
            self.assertTrue(db.save_account(
                email="created@example.test",
                password="private-password",
                registration_result={
                    "success": True, "status": "created", "error_code": "",
                },
                warm_result={
                    "success": False, "error_code": "activity_failed",
                    "cleanup_status": "completed",
                    "browser_process_stopped": True, "lease_released": True,
                },
            ))
            row = db.get_all_accounts()[0]

        self.assertEqual(row["registration_result"]["success"], True)
        self.assertEqual(row["registration_result"]["status"], "created")
        self.assertFalse(row["warm_result"]["success"])
        self.assertEqual(row["warm_result"]["error_code"], "activity_failed")
        self.assertNotIn("private-password", str(row["registration_result"]))
        self.assertNotIn("private-password", str(row["warm_result"]))

    def test_playwright_runner_return_result_keeps_warm_failure_projection(self):
        async_flow = AsyncMock(return_value={
            "success": True,
            "error_code": "",
            "registration_result": {"success": True, "status": "created"},
            "warm_result": {
                "success": False, "error_code": "activity_failed",
                "cleanup_status": "completed",
                "browser_process_stopped": True, "lease_released": True,
            },
        })
        with patch("core.runners.PlaywrightStealthManager", object()), \
             patch("core.runners.async_playwright_flow", async_flow), \
             patch("core.runners.Config", type("Config", (), {
                 "YOUR_BIRTHDAY": "2 4 1990", "YOUR_GENDER": "1",
             })):
            from core.runners import run_playwright_flow
            result = run_playwright_flow(
                0, 1, "user", "User", "Test", "password", None, None, None,
                use_sms_api=False, flow_mode="standard", return_result=True,
            )

        self.assertIsInstance(result, dict)
        self.assertTrue(result["success"])
        self.assertFalse(result["warm_result"]["success"])
        self.assertEqual(result["warm_result"]["error_code"], "activity_failed")

    def test_runner_result_metadata_keeps_only_safe_nested_outcomes(self):
        from core.operation_result import safe_creation_result_metadata

        metadata = safe_creation_result_metadata({
            "success": True,
            "registration_result": {
                "success": True, "status": "created",
                "password": "private-password",
            },
            "warm_result": {
                "success": False, "error_code": "activity_failed",
                "provider_payload": "token=private-token",
                "browser_process_stopped": True,
                "lease_released": True,
            },
        })

        self.assertEqual(metadata["registration_result"]["status"], "created")
        self.assertEqual(metadata["warm_result"]["error_code"], "activity_failed")
        self.assertNotIn("private-password", str(metadata))
        self.assertNotIn("private-token", str(metadata))


if __name__ == "__main__":
    unittest.main()
