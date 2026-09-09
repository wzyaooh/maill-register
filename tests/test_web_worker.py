import importlib.util
import sys
import tempfile
import types
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import Mock, patch

from web.worker import execute


ROOT = Path(__file__).resolve().parents[1]


class WorkerDispatchTests(unittest.TestCase):
    def setUp(self):
        self.config = types.SimpleNamespace(
            ENGINE_MODE="playwright", FIVESIM_API_KEY="", SMS_ACTIVATE_API_KEY="",
            ONLINESIM_API_KEY="", GETSMS_API_KEY="",
        )
        self.modules = patch.dict(sys.modules, {
            "config.settings": types.SimpleNamespace(Config=self.config),
        })
        self.modules.start()
        self.addCleanup(self.modules.stop)

    def test_create_delegates_parameters_and_progress(self):
        flow = Mock(return_value={"total": 2, "successes": 1, "failures": 1})
        manager = Mock()
        manager.has_saved_session.return_value = False
        params = dict(engine="playwright", num_accounts=2, warmup_minutes=4,
                      flow_mode="workspace", use_sms_api=False, parallel=False)
        with patch.dict(sys.modules, {
            "core.creation_flow": types.SimpleNamespace(run_creation_flow=flow),
            "core.session_resume": types.SimpleNamespace(session_manager=manager),
            "core.retry_engine": types.SimpleNamespace(retry_engine=Mock()),
        }):
            result = execute("create", params, Mock())
        self.assertEqual(result["successes"], 1)
        self.assertEqual(flow.call_args.args, (2,))
        self.assertEqual(flow.call_args.kwargs["flow_mode"], "workspace")
        self.assertEqual(flow.call_args.kwargs["warmup_minutes"], 4)

    def test_resume_retains_original_state_and_engine(self):
        state = {"batch_config": {"num_accounts": 5, "warmup_minutes": 3,
                                 "flow_mode": "standard", "use_sms_api": False, "engine": "selenium"},
                 "completed_indices": [0, 1], "results": {"successes": 1, "failures": 1}}
        manager = Mock()
        manager.load_state.return_value = state
        manager.get_remaining.return_value = [2, 3, 4]
        flow = Mock(return_value={"total": 5})
        with patch.dict(sys.modules, {
            "core.creation_flow": types.SimpleNamespace(run_creation_flow=flow),
            "core.session_resume": types.SimpleNamespace(session_manager=manager),
            "core.retry_engine": types.SimpleNamespace(retry_engine=Mock()),
        }):
            execute("resume", {}, Mock())
        self.assertIs(flow.call_args.kwargs["resume_state"], state)
        self.assertEqual(self.config.ENGINE_MODE, "selenium")

    def test_new_task_does_not_overwrite_saved_session(self):
        manager = Mock()
        manager.has_saved_session.return_value = True
        flow = Mock()
        with patch.dict(sys.modules, {
            "core.creation_flow": types.SimpleNamespace(run_creation_flow=flow),
            "core.session_resume": types.SimpleNamespace(session_manager=manager),
        }):
            with self.assertRaisesRegex(ValueError, "unfinished session"):
                execute("create", {}, Mock())
        flow.assert_not_called()

    def test_health_updates_selected_accounts_only(self):
        manager = Mock()
        manager.get_all.return_value = [
            {"id": 1, "email": "one@example.test", "password": "one"},
            {"id": 2, "email": "two@example.test", "password": "two"},
        ]
        checker = Mock()
        checker.check_single.return_value = {"status": "locked", "message": "web login"}
        with patch.dict(sys.modules, {
            "core.account_manager": types.SimpleNamespace(account_manager=manager),
            "core.health_checker": types.SimpleNamespace(AccountHealthChecker=checker),
        }):
            execute("health", {"account_ids": [2]}, Mock())
        checker.check_single.assert_called_once_with("two@example.test", "two")
        manager.db.update_account_status.assert_called_once_with("two@example.test", "locked", "web login")

    def test_telegram_send_failure_is_not_reported_as_success(self):
        notifier = Mock()
        notifier.test_connection.return_value = (True, "Connected")
        notifier.send.return_value = False
        with patch.dict(sys.modules, {"core.telegram_notifier": types.SimpleNamespace(notifier=notifier)}):
            with self.assertRaisesRegex(RuntimeError, "sending"):
                execute("telegram_test", {}, Mock())

    def test_empty_balance_configuration_is_visible(self):
        async def balance():
            return {}
        with patch.dict(sys.modules, {"services.sms_manager": types.SimpleNamespace(check_balance=balance)}):
            with self.assertRaisesRegex(ValueError, "Balance checks"):
                execute("sms_balance", {}, Mock())


class SerialResumeTests(unittest.TestCase):
    def test_repeated_resume_preserves_prior_counts_and_clears_completed_state(self):
        from core.session_resume import SessionManager
        with tempfile.TemporaryDirectory() as directory:
            session_manager = SessionManager(str(Path(directory) / "session.json"))
            config = types.SimpleNamespace(ENGINE_MODE="playwright", YOUR_PASSWORD="test-password",
                                           DELAY_BETWEEN_ACCOUNTS=0)
            proxy = Mock()
            proxy.get_best.return_value = None
            proxy.get_next.return_value = None
            proxy.count = 0
            flow = Mock(return_value=True)
            fake_modules = {
                "config.settings": types.SimpleNamespace(Config=config),
                "core.account_manager": types.SimpleNamespace(account_manager=Mock()),
                "core.proxy_manager": types.SimpleNamespace(proxy_manager=proxy),
                "core.retry_engine": types.SimpleNamespace(retry_engine=Mock()),
                "core.database": types.SimpleNamespace(DatabaseManager=Mock()),
                "core.session_resume": types.SimpleNamespace(session_manager=session_manager),
                "core.telegram_notifier": types.SimpleNamespace(notifier=Mock()),
                "core.runners": types.SimpleNamespace(run_playwright_flow=flow),
            }
            spec = importlib.util.spec_from_file_location("tested_creator", ROOT / "core/creation_flow.py")
            module = importlib.util.module_from_spec(spec)
            with patch.dict(sys.modules, fake_modules):
                spec.loader.exec_module(module)
                module._generate_username = Mock(return_value=("testuser", ["Test", "User"]))
                module.get_progress_context = lambda: nullcontext(Mock())
                module.show_session_summary = Mock()
                module.print_success = Mock()
                saved = {"batch_config": {"num_accounts": 4}, "completed_indices": [0, 1],
                         "results": {"successes": 1, "failures": 1}}
                progress = Mock()
                flow.side_effect = [True, KeyboardInterrupt]
                with self.assertRaises(KeyboardInterrupt):
                    module.run_creation_flow(2, resume_state=saved, on_progress=progress)
                saved = session_manager.load_state()
                self.assertEqual(saved["completed_indices"], [0, 1, 2])
                self.assertEqual(saved["results"], {"successes": 2, "failures": 1})
                flow.side_effect = None
                result = module.run_creation_flow(1, resume_state=saved, on_progress=progress)
            self.assertEqual(flow.call_count, 3)
            self.assertEqual([call.args[0] for call in flow.call_args_list], [2, 3, 3])
            self.assertEqual(result["total"], 4)
            self.assertEqual(result["successes"], 3)
            self.assertEqual(result["failures"], 1)
            self.assertEqual(progress.call_args.args[:2], (4, 4))
            self.assertFalse(session_manager.has_saved_session())


class VoiceAuthenticationTests(unittest.TestCase):
    def test_both_voice_routes_require_configured_token(self):
        config = types.SimpleNamespace(VOICE_SERVER_TOKEN="local-voice-test-token")
        spec = importlib.util.spec_from_file_location("tested_voice", ROOT / "services/voice.py")
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {
            "config.settings": types.SimpleNamespace(Config=config),
            "speech_recognition": types.ModuleType("speech_recognition"),
            "pydub": types.SimpleNamespace(AudioSegment=Mock()),
        }), patch("os.makedirs"):
            spec.loader.exec_module(module)
        client = module.app.test_client()
        self.assertEqual(client.get("/otp").status_code, 401)
        self.assertEqual(client.post("/voice").status_code, 401)
        self.assertEqual(client.get("/otp?token=incorrect").status_code, 401)
        response = client.get("/otp", headers={"X-Voice-Token": config.VOICE_SERVER_TOKEN})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json, {"code": None})
        response = client.post("/voice?token=" + config.VOICE_SERVER_TOKEN)
        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
