import asyncio
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

from core import phone_bypass
from tests.provider_transport_fakes import GMAIL_URL, install_selenium_provider


class _Input:
    def __init__(self):
        self.values = []

    async def click(self):
        return None

    async def fill(self, value):
        self.values.append(value)


class _Page:
    url = "https://accounts.google.com/signup"

    def __init__(self, contents=()):
        self._contents = list(contents)

    async def content(self):
        if self._contents:
            return self._contents.pop(0)
        return ""

    async def wait_for_timeout(self, _timeout):
        return None


class _SeleniumElement:
    def __init__(self, *, text="", displayed=True, enabled=True):
        self.text = text
        self.displayed = displayed
        self.enabled = enabled
        self.values = []
        self.clicks = 0

    def is_displayed(self):
        return self.displayed

    def is_enabled(self):
        return self.enabled

    def click(self):
        self.clicks += 1

    def clear(self):
        self.values.append("")

    def send_keys(self, value):
        self.values.append(value)


class _SeleniumDriver:
    current_url = "https://accounts.google.com/signup"

    def __init__(self, phone=None, code=None, buttons=None, contents=()):
        self.phone = phone
        self.code = code
        self.buttons = list(buttons or [])
        self.contents = list(contents)

    @property
    def page_source(self):
        if self.contents:
            return self.contents.pop(0)
        return "enter the code"

    def find_elements(self, by, selector):
        if by == "css selector":
            if "phone" in selector or "tel" in selector:
                return [self.phone] if self.phone is not None else []
            if "code" in selector or "one-time-code" in selector or "otp" in selector:
                return [self.code] if self.code is not None else []
        if by == "xpath" and "button" in selector:
            return self.buttons
        return []

    def get(self, url):
        self.current_url = url

    def execute_script(self, _script, *args):
        if args and hasattr(args[0], "click"):
            args[0].click()


class _Wait:
    def __init__(self, driver):
        self.driver = driver

    def until(self, condition):
        return condition(self.driver)


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


class _PlaywrightPage:
    def __init__(self):
        self.url = "https://accounts.google.com/signup"
        self.context = types.SimpleNamespace(clear_cookies=AsyncMock())
        self.element = _PlaywrightElement()

    async def goto(self, url, **_kwargs):
        self.url = url
        return types.SimpleNamespace(status=200)

    async def wait_for_timeout(self, _timeout):
        return None

    async def content(self):
        return "<main>Google signup</main>"

    async def wait_for_selector(self, _selector, **_kwargs):
        return self.element

    async def query_selector(self, _selector):
        return self.element

    async def query_selector_all(self, _selector):
        return []

    async def evaluate(self, _script, *_args):
        return None


class SmsRegistrationIntegrationTests(unittest.TestCase):
    def test_verification_forwards_attempt_ownership_to_phone_handler(self):
        page = types.SimpleNamespace(url="https://accounts.google.com/signup")
        store = object()
        handler = AsyncMock(return_value=(True, "sms_5sim"))
        with patch.object(phone_bypass, "detect_page_type", return_value="phone"), \
             patch.object(phone_bypass, "handle_phone_page", handler), \
             patch.object(phone_bypass, "Config", types.SimpleNamespace(
                 FIVESIM_API_KEY="key", SMS_ACTIVATE_API_KEY="",
                 ONLINESIM_API_KEY="", GETSMS_API_KEY="",
             )):
            result = asyncio.run(phone_bypass.handle_verification(
                page, use_sms_api=True, job_id="job-1", attempt_id="attempt-1",
                order_store=store,
            ))

        self.assertEqual(result, (True, "sms_5sim", False))
        self.assertEqual(handler.call_args.kwargs["job_id"], "job-1")
        self.assertEqual(handler.call_args.kwargs["attempt_id"], "attempt-1")
        self.assertIs(handler.call_args.kwargs["order_store"], store)

    def test_playwright_entrypoint_forwards_job_and_attempt_to_async_flow(self):
        async_flow = AsyncMock(return_value=(True, "success"))
        manager_marker = object()
        with patch("core.runners.PlaywrightStealthManager", object()), \
             patch("core.runners.async_playwright_flow", async_flow), \
             patch("core.runners.Config", types.SimpleNamespace(
                 YOUR_BIRTHDAY="2 4 1990", YOUR_GENDER="1",
             )):
            from core.runners import run_playwright_flow

            result = run_playwright_flow(
                0, 1, "user", "User", "Test", "password", None, None, None,
                use_sms_api=True, flow_mode="standard", job_id="job-1",
                attempt_id="attempt-1", order_store=manager_marker,
            )

        self.assertTrue(result)
        self.assertEqual(async_flow.call_args.args[-2:], (True, "standard"))
        self.assertEqual(async_flow.call_args.kwargs["job_id"], "job-1")
        self.assertEqual(async_flow.call_args.kwargs["attempt_id"], "attempt-1")
        self.assertIs(async_flow.call_args.kwargs["order_store"], manager_marker)

    def test_playwright_sms_entrypoint_fails_closed_without_durable_context(self):
        async_flow = AsyncMock(side_effect=AssertionError("browser flow must not start"))
        with patch("core.runners.PlaywrightStealthManager", object()), \
             patch("core.runners.async_playwright_flow", async_flow), \
             patch("core.runners.retry_engine.record_attempt") as record:
            from core.runners import run_playwright_flow

            result = run_playwright_flow(
                0, 1, "user", "User", "Test", "password", None, None, None,
                use_sms_api=True, flow_mode="standard", return_result=True,
            )

        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "sms_missing_attempt_context")
        async_flow.assert_not_called()
        record.assert_called_once_with(
            "standard", False, "sms_missing_attempt_context"
        )

    def test_playwright_registration_preserves_sms_failure_code(self):
        from core.runners import async_playwright_flow

        page = _PlaywrightPage()
        manager = types.SimpleNamespace(
            page=page,
            context=page.context,
            is_mobile=False,
            initialize=AsyncMock(return_value=True),
            get_runtime_info=Mock(return_value={}),
            natural_type=AsyncMock(return_value=True),
            fill_birthday_gender=AsyncMock(),
            close=AsyncMock(return_value={
                "success": True, "browser_process_stopped": True,
            }),
        )
        handle = types.SimpleNamespace(path="/tmp/test-profile", profile_id="profile-1")
        lease = Mock()
        lease.assert_stable = Mock()
        lease.release.return_value = True
        runtime = Mock()
        runtime.provision.return_value = handle
        runtime.load.return_value = {"engine": "playwright", "identity_state": "native"}
        runtime.lease.return_value = lease

        with patch("core.runners.PlaywrightStealthManager", return_value=manager), \
             patch("core.runners.ProfileRuntime.from_environment", return_value=runtime), \
             patch("core.runners.handle_verification", new=AsyncMock(
                 return_value=(False, "sms_finish_failed", False)
             )), \
             patch("core.runners._try_click", new=AsyncMock(return_value=True)):
            result = asyncio.run(async_playwright_flow(
                0, 1, "user", "User", "Test", "password", None, None, None,
                "2", "4", "1990", "1", True, "standard",
                job_id="job-1", attempt_id="attempt-1", order_store=object(),
            ))

        self.assertEqual(result, (False, "sms_finish_failed"))

    def test_batch_attempt_context_reaches_playwright_registration(self):
        from core.batch_runner import _create_single_account
        from core.job_ledger import JobLedger
        import core.runners as runners_module

        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            ledger = JobLedger(directory + "/database.db")
            job = ledger.create_job("create", requested_count=1)
            with patch("core.batch_runner._generate_username", return_value=("user1234", "User", "Test")), \
                 patch("core.batch_runner.proxy_manager.get_best", return_value=None), \
                 patch("core.batch_runner.proxy_manager.get_next", return_value=None), \
                 patch.object(runners_module, "run_playwright_flow", return_value=True) as runner:
                result = _create_single_account(
                    0, 1, "playwright", "pw", 0, "standard", True,
                    ledger=ledger, job_id=job["job_id"], ordinal=1,
                )

            self.assertTrue(result["success"])
            self.assertEqual(runner.call_args.kwargs["job_id"], job["job_id"])
            self.assertTrue(runner.call_args.kwargs["attempt_id"])
            self.assertIsNotNone(runner.call_args.kwargs["order_store"])

    def test_batch_attempt_context_reaches_selenium_registration(self):
        from core.batch_runner import _create_single_account
        from core.job_ledger import JobLedger
        import core.selenium_runner as selenium_module

        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            ledger = JobLedger(directory + "/database.db")
            job = ledger.create_job("create", requested_count=1)
            with patch("core.batch_runner._generate_username", return_value=("user1234", "User", "Test")), \
                 patch("core.batch_runner.proxy_manager.get_best", return_value=None), \
                 patch("core.batch_runner.proxy_manager.get_next", return_value=None), \
                 patch.object(selenium_module, "run_selenium_flow", return_value=True) as runner:
                result = _create_single_account(
                    0, 1, "selenium", "pw", 0, "standard", True,
                    ledger=ledger, job_id=job["job_id"], ordinal=1,
                )

            self.assertTrue(result["success"])
            self.assertTrue(runner.call_args.kwargs["use_sms_api"])
            self.assertEqual(runner.call_args.kwargs["job_id"], job["job_id"])
            self.assertTrue(runner.call_args.kwargs["attempt_id"])
            self.assertIs(runner.call_args.kwargs["order_store"], ledger.sms_orders)

    def test_selenium_entrypoint_forwards_sms_attempt_context_to_registration(self):
        from core.selenium_runner import run_selenium_flow

        store = object()
        handle = types.SimpleNamespace(path="/tmp/test-profile", profile_id="profile-1")
        lease = Mock()
        lease.assert_stable = Mock()
        runtime = Mock()
        runtime.provision.return_value = handle
        runtime.load.return_value = {
            "engine": "selenium",
            "identity_state": "native",
        }
        runtime.lease.return_value = lease
        driver = Mock()
        registration = Mock(return_value=(False, "sms_no_phone_input"))

        with patch("core.selenium_runner.ProfileRuntime.from_environment", return_value=runtime), \
             patch("core.selenium_runner.create_driver", return_value=driver), \
             patch("core.selenium_runner.get_runtime_info", return_value={}), \
             patch("core.selenium_runner.create_account_selenium", registration), \
             patch("core.selenium_runner._close_registration_driver", return_value={
                 "cleanup_status": "completed",
                 "browser_process_stopped": True,
             }), \
             patch("core.selenium_runner.release_registration_lease", return_value=True), \
             patch("core.selenium_runner.Config", types.SimpleNamespace(
                 BROWSER_TIMEOUT=1,
                 ENABLE_COOKIE_REAPER=False,
                 ENABLE_FINGERPRINT_MASKING=False,
                 ENABLE_SESSION_WARMING=False,
                 YOUR_BIRTHDAY="2 4 1990",
                 YOUR_GENDER="1",
             )):
            result = run_selenium_flow(
                0, 1, "user", "password", warmup_minutes=0,
                stealth_mode=False, use_sms_api=True,
                job_id="job-1", attempt_id="attempt-1", order_store=store,
                return_result=True,
            )

        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "sms_no_phone_input")
        self.assertTrue(registration.call_args.kwargs["use_sms_api"])
        self.assertEqual(registration.call_args.kwargs["job_id"], "job-1")
        self.assertEqual(registration.call_args.kwargs["attempt_id"], "attempt-1")
        self.assertIs(registration.call_args.kwargs["order_store"], store)

    def test_selenium_sms_entrypoint_fails_closed_before_profile_launch(self):
        from core.selenium_runner import run_selenium_flow

        runtime = Mock()
        with patch("core.selenium_runner.ProfileRuntime.from_environment", return_value=runtime), \
             patch("core.selenium_runner.create_driver", side_effect=AssertionError("browser must not start")):
            result = run_selenium_flow(
                0, 1, "user", "password", warmup_minutes=0,
                stealth_mode=False, use_sms_api=True,
                return_result=True,
            )

        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "sms_missing_attempt_context")
        runtime.provision.assert_not_called()

    def test_selenium_success_persists_sms_verification_method(self):
        from core.selenium_runner import run_selenium_flow

        handle = types.SimpleNamespace(path="/tmp/test-profile", profile_id="profile-1")
        lease = Mock()
        lease.assert_stable = Mock()
        runtime = Mock()
        runtime.provision.return_value = handle
        runtime.load.return_value = {
            "engine": "selenium",
            "identity_state": "native",
        }
        runtime.lease.return_value = lease
        driver = Mock()
        driver.current_url = "https://accounts.google.com/signup"
        driver.page_source = "Inbox Compose Search mail"

        def navigate(url):
            driver.current_url = GMAIL_URL if url == "https://mail.google.com/" else url

        def execute(script, *_args):
            if "querySelectorAll('[data-email" in script:
                return "user@gmail.com"
            if "[role=\"main\"]" in script:
                return True
            return None

        driver.get.side_effect = navigate
        driver.execute_script.side_effect = execute
        install_selenium_provider(driver, "user@gmail.com")
        driver.get_cookies.return_value = [{
            "name": "SID", "value": "live-registration-session",
            "domain": ".google.com", "secure": True,
            "expires": 4102444800,
        }]
        registration = Mock(return_value=(True, "sms_5sim"))
        accounts = Mock()
        accounts.save.return_value = True
        accounts.db.update_profile_state.return_value = True
        store = object()

        with patch("core.selenium_runner.ProfileRuntime.from_environment", return_value=runtime), \
             patch("core.selenium_runner.create_driver", return_value=driver), \
             patch("core.selenium_runner.get_runtime_info", return_value={}), \
             patch("core.selenium_runner.create_account_selenium", registration), \
             patch("core.selenium_runner.account_manager", accounts), \
             patch("core.selenium_runner._close_registration_driver", return_value={
                 "success": True,
                 "cleanup_status": "completed",
                 "browser_process_stopped": True,
             }), \
             patch("core.selenium_runner.release_registration_lease", return_value=True), \
             patch("core.telegram_notifier.notifier", Mock()), \
             patch("core.selenium_runner.Config", types.SimpleNamespace(
                 BROWSER_TIMEOUT=1,
                 ENABLE_COOKIE_REAPER=False,
                 ENABLE_FINGERPRINT_MASKING=False,
                 ENABLE_SESSION_WARMING=False,
                 YOUR_BIRTHDAY="2 4 1990",
                 YOUR_GENDER="1",
             )):
            result = run_selenium_flow(
                0, 1, "user", "password", warmup_minutes=0,
                stealth_mode=False, use_sms_api=True,
                job_id="job-1", attempt_id="attempt-1", order_store=store,
                return_result=True,
            )

        self.assertTrue(result["success"], result)
        kwargs = accounts.save.call_args.kwargs
        self.assertEqual(kwargs["sms_service"], "5sim")
        self.assertEqual(
            kwargs["registration_result"]["verification_method"], "sms_5sim"
        )
        self.assertEqual(
            result["registration_result"]["verification_method"], "sms_5sim"
        )

    def test_serial_attempt_context_reaches_selenium_registration(self):
        from contextlib import nullcontext
        import tempfile

        from core import creation_flow
        from core.job_ledger import JobLedger

        with tempfile.TemporaryDirectory() as directory:
            ledger = JobLedger(directory + "/database.db")
            job = ledger.create_job(
                "create", requested_count=1, requested_engine="selenium",
            )
            progress = Mock()
            runner = Mock(return_value=True)
            session = Mock()
            with patch.object(creation_flow.Config, "ENGINE_MODE", "selenium"), \
                 patch.object(creation_flow.Config, "YOUR_PASSWORD", "pw"), \
                 patch.object(creation_flow.Config, "DELAY_BETWEEN_ACCOUNTS", 0), \
                 patch.object(creation_flow, "_generate_username", return_value=(
                     "user1234", ["User", "Test"],
                 )), \
                 patch.object(creation_flow, "get_progress_context", return_value=nullcontext(progress)), \
                 patch.object(creation_flow, "show_session_summary"), \
                 patch.object(creation_flow, "print_success"), \
                 patch.object(creation_flow, "print_error"), \
                 patch.object(creation_flow.proxy_manager, "get_best", return_value=None), \
                 patch.object(creation_flow.proxy_manager, "get_next", return_value=None), \
                 patch.object(creation_flow.DatabaseManager, "save_session_stats"), \
                 patch("core.selenium_runner.run_selenium_flow", runner), \
                 patch("core.session_resume.session_manager", session), \
                 patch("core.telegram_notifier.notifier", Mock()):
                result = creation_flow.run_creation_flow(
                    1, warmup_minutes=0, flow_mode="standard", use_sms_api=True,
                    ledger=ledger, job_id=job["job_id"],
                )

            self.assertEqual(result["successes"], 1)
            self.assertTrue(runner.call_args.kwargs["use_sms_api"])
            self.assertEqual(runner.call_args.kwargs["job_id"], job["job_id"])
            self.assertTrue(runner.call_args.kwargs["attempt_id"])
            self.assertIs(runner.call_args.kwargs["order_store"], ledger.sms_orders)

    def test_selenium_sms_bridge_uses_durable_core_and_finishes_once(self):
        from core.selenium_phone_bypass import run_selenium_sms_verification

        phone_input = _SeleniumElement()
        code_input = _SeleniumElement()
        next_button = _SeleniumElement(text="Next")
        driver = _SeleniumDriver(
            phone=phone_input, code=code_input, buttons=[next_button],
            contents=["", ""],
        )
        store = object()
        phone = {
            "phone": "+14155551234", "id": "provider-1", "service": "5sim",
            "local_order_id": "local-1",
        }
        cancel = AsyncMock()
        finish = AsyncMock(return_value={"ok": True, "state": "completed"})
        get_code = AsyncMock(return_value="246810")

        with patch("services.sms_manager.check_balance", new=AsyncMock(return_value={"5sim": 1})), \
             patch("services.sms_manager.get_phone_from_any_service", new=AsyncMock(return_value=phone)) as get_phone, \
             patch("services.sms_manager.get_code_from_service", new=get_code), \
             patch("services.sms_manager.cancel_order", new=cancel), \
             patch("services.sms_manager.finish_order", new=finish), \
             patch.object(phone_bypass.asyncio, "sleep", new=AsyncMock()):
            result = run_selenium_sms_verification(
                driver, _Wait(driver), use_sms_api=True,
                job_id="job-1", attempt_id="attempt-1", order_store=store,
            )

        self.assertEqual(result, (True, "sms_5sim"))
        self.assertEqual(phone_input.values[-1], "+14155551234")
        self.assertEqual(code_input.values[-1], "246810")
        self.assertGreater(next_button.clicks, 0)
        self.assertEqual(get_phone.call_args.kwargs["job_id"], "job-1")
        self.assertEqual(get_phone.call_args.kwargs["attempt_id"], "attempt-1")
        self.assertIs(get_phone.call_args.kwargs["order_store"], store)
        self.assertEqual(get_code.call_args.kwargs["local_order_id"], "local-1")
        finish.assert_awaited_once()
        cancel.assert_not_awaited()

    def test_selenium_registration_uses_sms_bridge_for_phone_page(self):
        import core.selenium_phone_bypass as selenium_sms
        from core.selenium_runner import create_account_selenium

        driver = _SeleniumDriver(contents=[
            "verify your phone number",
        ])
        wait = Mock()
        field = _SeleniumElement()
        driver.find_element = Mock(return_value=field)
        wait.until.return_value = field
        store = object()
        with patch("core.selenium_runner.time.sleep"), \
             patch("core.selenium_runner.generate_name", return_value="User Test"), \
             patch("core.selenium_runner._fill_field", return_value=True), \
             patch("core.selenium_runner._click_next", return_value=True), \
             patch("core.selenium_runner._select_create_own", return_value=True), \
             patch("core.selenium_runner._find_username_field", return_value=field), \
             patch.object(selenium_sms, "run_selenium_sms_verification",
                          return_value=(True, "sms_5sim")) as bridge:
            result = create_account_selenium(
                driver, wait, "user1234", "password", "2 4 1990", "1",
                use_sms_api=True, job_id="job-1", attempt_id="attempt-1",
                order_store=store,
            )

        self.assertEqual(result, (True, "sms_5sim"))
        bridge.assert_called_once_with(
            driver, wait, use_sms_api=True, job_id="job-1",
            attempt_id="attempt-1", order_store=store,
        )

    def test_selenium_registration_without_sms_keeps_phone_required_contract(self):
        from core.selenium_runner import create_account_selenium

        driver = _SeleniumDriver(contents=["verify your phone number"])
        wait = Mock()
        field = _SeleniumElement()
        driver.find_element = Mock(return_value=field)
        wait.until.return_value = field
        with patch("core.selenium_runner.time.sleep"), \
             patch("core.selenium_runner.generate_name", return_value="User Test"), \
             patch("core.selenium_runner._fill_field", return_value=True), \
             patch("core.selenium_runner._click_next", return_value=True), \
             patch("core.selenium_runner._select_create_own", return_value=True), \
             patch("core.selenium_runner._find_username_field", return_value=field):
            result = create_account_selenium(
                driver, wait, "user1234", "password", "2 4 1990", "1",
                use_sms_api=False,
            )

        self.assertEqual(result, (False, "PHONE_REQUIRED"))

    def test_selenium_sms_bridge_rejects_disabled_order_store_sentinel(self):
        from core.selenium_phone_bypass import run_selenium_sms_verification

        result = run_selenium_sms_verification(
            _SeleniumDriver(), _Wait(_SeleniumDriver()), use_sms_api=True,
            job_id="job-1", attempt_id="attempt-1", order_store=False,
        )

        self.assertEqual(result, (False, "sms_missing_attempt_context"))

    def _run_sms_flow(self, *, inputs, contents=(), code="246810",
                      finish=None, cancel=None, phone=None):
        page = _Page(contents)
        phone = phone or {"phone": "+14155551234", "id": "provider-1",
                          "service": "5sim", "local_order_id": "local-1"}
        cancel = cancel or AsyncMock()
        finish = finish or AsyncMock()
        get_code = AsyncMock(return_value=code)
        with patch.object(phone_bypass, "_find_input", new=AsyncMock(side_effect=inputs)), \
             patch.object(phone_bypass, "_try_click", new=AsyncMock(return_value=True)), \
             patch("services.sms_manager.check_balance", new=AsyncMock(return_value={"5sim": 1})), \
             patch("services.sms_manager.get_phone_from_any_service", new=AsyncMock(return_value=phone)), \
             patch("services.sms_manager.get_code_from_service", new=get_code), \
             patch("services.sms_manager.cancel_order", new=cancel), \
             patch("services.sms_manager.finish_order", new=finish), \
             patch.object(phone_bypass.asyncio, "sleep", new=AsyncMock()):
            result = asyncio.run(phone_bypass._sms_api_verification(
                page, job_id="job-test", attempt_id="attempt-test",
                order_store=object(),
            ))
        return result, cancel, finish, get_code

    def test_shared_sms_core_rejects_missing_attempt_context_before_provider_calls(self):
        balance = AsyncMock(side_effect=AssertionError("provider must not be called"))
        with patch("services.sms_manager.check_balance", new=balance):
            result = asyncio.run(phone_bypass._sms_api_verification(_Page()))

        self.assertEqual(result, (False, "sms_missing_attempt_context"))
        balance.assert_not_awaited()

    def test_missing_phone_input_preserves_reason_and_requests_cancel(self):
        result, cancel, _finish, _get_code = self._run_sms_flow(inputs=[None])
        self.assertEqual(result, (False, "sms_no_phone_input"))
        cancel.assert_awaited_once()

    def test_rejected_phone_preserves_reason_and_requests_cancel(self):
        result, cancel, _finish, _get_code = self._run_sms_flow(
            inputs=[_Input()], contents=["this phone number cannot be used"]
        )
        self.assertEqual(result, (False, "sms_phone_rejected"))
        cancel.assert_awaited_once()

    def test_missing_code_page_preserves_reason_and_requests_cancel(self):
        result, cancel, _finish, _get_code = self._run_sms_flow(
            inputs=[_Input()] + [None] * 10,
        )
        self.assertEqual(result, (False, "sms_no_code_page"))
        cancel.assert_awaited_once()

    def test_code_timeout_preserves_reason_and_requests_cancel(self):
        result, cancel, _finish, get_code = self._run_sms_flow(
            inputs=[_Input(), _Input()], code=None,
        )
        self.assertEqual(result, (False, "sms_timeout"))
        get_code.assert_awaited_once()
        cancel.assert_awaited_once()

    def test_missing_code_input_preserves_reason_and_requests_cancel(self):
        result, cancel, _finish, _get_code = self._run_sms_flow(
            inputs=[_Input(), _Input()] + [None] * 6,
        )
        self.assertEqual(result, (False, "sms_no_code_input"))
        cancel.assert_awaited_once()

    def test_wrong_code_preserves_reason_and_requests_cancel(self):
        result, cancel, _finish, _get_code = self._run_sms_flow(
            inputs=[_Input(), _Input(), _Input()], contents=["", "wrong code"]
        )
        self.assertEqual(result, (False, "sms_code_rejected"))
        cancel.assert_awaited_once()

    def test_finish_provider_failure_preserves_machine_reason_and_does_not_finish_twice(self):
        from services.sms_manager import SmsProviderError

        finish = AsyncMock(side_effect=SmsProviderError("5sim", "finish"))
        result, cancel, finish, _get_code = self._run_sms_flow(
            inputs=[_Input(), _Input(), _Input()], contents=["", ""], finish=finish,
        )
        self.assertEqual(result, (False, "sms_finish_failed"))
        finish.assert_awaited_once()
        cancel.assert_not_awaited()

    def test_unexpected_exception_after_allocation_requests_cancel_and_returns_generic_code(self):
        page = _Page()
        cancel = AsyncMock()
        with patch("services.sms_manager.check_balance", new=AsyncMock(return_value={"5sim": 1})), \
             patch("services.sms_manager.get_phone_from_any_service", new=AsyncMock(
                 return_value={"phone": "+14155551234", "id": "provider-1", "service": "5sim"}
             )), \
             patch("services.sms_manager.cancel_order", new=cancel), \
             patch("services.sms_manager.format_phone_for_google", side_effect=RuntimeError("boom")):
            result = asyncio.run(phone_bypass._sms_api_verification(
                page, job_id="job-test", attempt_id="attempt-test",
                order_store=object(),
            ))
        self.assertEqual(result, (False, "sms_error"))
        cancel.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
