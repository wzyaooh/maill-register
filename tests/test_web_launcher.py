import ast
import io
import runpy
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from web.server import main


ROOT = Path(__file__).resolve().parents[1]


class WebLauncherTests(unittest.TestCase):
    def test_default_and_legacy_flag_both_start_web_without_input(self):
        for arguments in ([], ["--web"], ["--host", "127.0.0.1", "--port", "8088"]):
            with self.subTest(arguments=arguments):
                manager = Mock()
                application = SimpleNamespace(extensions={"web_tasks": manager})
                lock = Mock()
                with patch.object(sys, "argv", ["auto_gmail_creator.py", *arguments]), \
                        patch("web.server.os.chdir"), \
                        patch("web.server.lock_server", return_value=lock), \
                        patch("web.app.create_app", return_value=application), \
                        patch("waitress.serve") as serve, \
                        patch("web.server.atexit.register"), \
                        patch("builtins.input", side_effect=AssertionError("Interactive input is forbidden")), \
                        redirect_stdout(io.StringIO()):
                    main()
                serve.assert_called_once_with(application, host="127.0.0.1",
                                              port=8088 if "--port" in arguments else 8080, threads=8)
                manager.close.assert_called_once()
                lock.close.assert_called_once()

    def test_primary_script_and_module_delegate_to_web_launcher(self):
        with patch("web.server.main") as launch:
            runpy.run_path(str(ROOT / "auto_gmail_creator.py"), run_name="__main__")
            launch.assert_called_once_with()
        with patch("web.server.main") as launch:
            runpy.run_module("web", run_name="__main__")
            launch.assert_called_once_with()

    def test_removed_menu_has_no_remaining_runtime_dependencies(self):
        self.assertFalse((ROOT / "core/ui.py").exists())
        for relative in ("auto_gmail_creator.py", "core/creation_flow.py",
                         "core/progress.py", "core/runners.py", "web/worker.py"):
            tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
            with self.subTest(path=relative):
                for node in ast.walk(tree):
                    if isinstance(node, ast.ImportFrom):
                        self.assertNotIn(node.module, ("core.ui", "rich.prompt"))
                    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                        self.assertNotEqual(node.func.id, "input")


if __name__ == "__main__":
    unittest.main()
