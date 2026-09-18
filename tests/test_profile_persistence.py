"""Focused, dependency-free regression tests for browser profile persistence."""
import json
import asyncio
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from core.database import DatabaseManager
from core.profile_runtime import ProfileRuntime


def _valid_auth_cookie():
    return {
        "name": "SID",
        "value": "live-session",
        "domain": ".google.com",
        "secure": True,
        "expires": 4102444800,
    }


class ProfilePersistenceTests(unittest.TestCase):
    def test_profile_path_round_trips_through_account_database(self):
        with tempfile.TemporaryDirectory() as directory:
            database = DatabaseManager(str(Path(directory) / "accounts.db"))

            self.assertTrue(database.save_account(
                "profile@example.test", "secret", profile_path="/profiles/profile"
            ))

            account = database.get_all_accounts()[0]
            self.assertEqual(account["profile_path"], "/profiles/profile")

    def test_json_migration_preserves_profile_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "accounts.json"
            source.write_text(json.dumps([{
                "email": "legacy@example.test",
                "password": "secret",
                "profile_path": "/profiles/legacy",
            }]), encoding="utf-8")
            database = DatabaseManager(str(root / "accounts.db"))

            self.assertEqual(database.run_migration(str(source), str(root / "missing.txt")), 1)
            account = database.get_all_accounts()[0]
            self.assertEqual(account["profile_path"], "/profiles/legacy")

    def test_profile_id_without_path_derives_runtime_profile_path(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime_root = Path(directory) / "runtime"
            with patch.dict("os.environ", {"PROFILE_RUNTIME_ROOT": str(runtime_root)}):
                database = DatabaseManager(str(Path(directory) / "accounts.db"))
                self.assertTrue(database.save_account(
                    "derived@example.test", "secret",
                    profile_id="profile-derived", engine="playwright",
                ))

            account = database.get_all_accounts()[0]
            self.assertEqual(
                account["profile_path"],
                str((runtime_root / "data" / "profiles" / "profile-derived").resolve()),
            )

    def test_profile_id_without_verified_binding_fields_defaults_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            database = DatabaseManager(str(Path(directory) / "accounts.db"))
            self.assertTrue(database.save_account(
                "unverified@example.test", "secret",
                first_name="Imported", profile_id="profile-unverified",
            ))
            account = database.get_all_accounts()[0]
            self.assertEqual(account["profile_state"], "legacy_unbound")
            self.assertEqual(account["identity_state"], "identity_reconstructed")
            self.assertEqual(account["browser_status"], "not_configured")
            self.assertEqual(account["overall_status"], "unknown")

    def test_profile_id_derives_from_database_environment_root_when_env_is_unset(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime_root = Path(directory) / "runtime"
            database = DatabaseManager(str(runtime_root / "data" / "database.db"))
            with patch.dict("os.environ", {}, clear=True):
                self.assertTrue(database.save_account(
                    "db-root@example.test", "secret",
                    profile_id="profile-db-root", engine="playwright",
                ))
            account = database.get_all_accounts()[0]
            self.assertEqual(
                account["profile_path"],
                str((runtime_root / "data" / "profiles" / "profile-db-root").resolve()),
            )

    def test_isolated_database_root_wins_over_stale_runtime_environment(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as stale:
            runtime_root = Path(directory) / "runtime"
            database = DatabaseManager(str(runtime_root / "data" / "database.db"))
            with patch.dict("os.environ", {"PROFILE_RUNTIME_ROOT": str(Path(stale) / "other")}, clear=False):
                self.assertTrue(database.save_account(
                    "isolated@example.test", "secret",
                    profile_id="profile-isolated", engine="playwright",
                ))
            account = database.get_all_accounts()[0]
            self.assertEqual(
                account["profile_path"],
                str((runtime_root / "data" / "profiles" / "profile-isolated").resolve()),
            )

    def test_isolated_database_root_rejects_profile_path_from_another_runtime(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as other:
            runtime_root = Path(directory) / "runtime"
            database = DatabaseManager(str(runtime_root / "data" / "database.db"))
            with patch.dict("os.environ", {}, clear=True), self.assertRaises(ValueError):
                database.save_account(
                    "db-conflict@example.test", "secret",
                    profile_id="profile-db-conflict", engine="playwright",
                    profile_path=str(Path(other) / "data" / "profiles" / "profile-db-conflict"),
                )

    def test_profile_id_rejects_a_path_outside_its_derived_profiles_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            database = DatabaseManager(str(Path(directory) / "accounts.db"))
            with self.assertRaises(ValueError):
                database.save_account(
                    "conflict@example.test", "secret",
                    profile_id="profile-derived", engine="playwright",
                    profile_path=str(Path(directory) / "somewhere-else" / "profile-derived"),
                )

    def test_profile_id_rejects_invalid_identifier_shape(self):
        with tempfile.TemporaryDirectory() as directory:
            database = DatabaseManager(str(Path(directory) / "accounts.db"))
            for profile_id in ("../escape", "has space", "x" * 129, "   ", 123):
                with self.subTest(profile_id=profile_id), self.assertRaises(ValueError):
                    database.save_account(
                        "invalid@example.test", "secret",
                        profile_id=profile_id, engine="playwright",
                    )

    def test_database_rejects_broken_symlink_profile_components(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            database = DatabaseManager(str(Path(directory) / "accounts.db"))
            broken = Path(directory).resolve() / "data" / "profiles" / "broken"
            broken.parent.mkdir(parents=True)
            broken.symlink_to(Path(outside) / "removed", target_is_directory=True)

            with self.assertRaises(ValueError):
                database._reject_symlink_components(broken)

    @unittest.skipIf(os.name == "nt", "POSIX symlink semantics")
    def test_save_account_rejects_live_derived_profile_symlink_before_manifest_read(self):
        with tempfile.TemporaryDirectory() as directory, \
             tempfile.TemporaryDirectory() as outside:
            runtime_root = Path(directory)
            database = DatabaseManager(str(runtime_root / "data" / "accounts.db"))
            derived = runtime_root / "data" / "profiles" / "live-derived-link"
            derived.parent.mkdir(parents=True)
            outside_profile = Path(outside) / "profile"
            outside_profile.mkdir()
            (outside_profile / "profile_manifest.json").write_text(json.dumps({
                "profile_id": "live-derived-link",
                "email": "linked@example.test",
                "engine": "playwright",
                "state": "ready",
            }), encoding="utf-8")
            derived.symlink_to(outside_profile, target_is_directory=True)

            reads = []
            original_load = json.load

            def tracking_load(stream):
                reads.append(Path(stream.name))
                return original_load(stream)

            with patch("core.database.json.load", side_effect=tracking_load), \
                 self.assertRaisesRegex(ValueError, "symlink"):
                database.save_account(
                    "linked@example.test", "secret",
                    profile_id="live-derived-link", engine="playwright",
                )

            self.assertEqual(reads, [])
            self.assertEqual(database.get_account_count(), 0)

    @unittest.skipIf(os.name == "nt", "POSIX symlink semantics")
    def test_save_account_rejects_dangling_derived_profile_symlink(self):
        with tempfile.TemporaryDirectory() as directory, \
             tempfile.TemporaryDirectory() as outside:
            runtime_root = Path(directory)
            database = DatabaseManager(str(runtime_root / "data" / "accounts.db"))
            derived = runtime_root / "data" / "profiles" / "dangling-derived-link"
            derived.parent.mkdir(parents=True)
            derived.symlink_to(
                Path(outside) / "missing-profile", target_is_directory=True
            )

            with self.assertRaisesRegex(ValueError, "symlink"):
                database.save_account(
                    "dangling@example.test", "secret",
                    profile_id="dangling-derived-link", engine="playwright",
                )

            self.assertEqual(database.get_account_count(), 0)

    def test_existing_manifest_binding_conflicts_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = ProfileRuntime(directory)
            handle = runtime.provision("bound@example.test", "playwright")
            runtime.bind(handle, "bound@example.test")
            runtime.mark_ready(handle)
            database = DatabaseManager(str(Path(directory) / "accounts.db"))

            with self.assertRaises(ValueError):
                database.save_account(
                    "other@example.test", "secret",
                    profile_id=handle.profile_id, engine="playwright",
                    profile_path=str(handle.path),
                )

            with self.assertRaises(ValueError):
                database.save_account(
                    "bound@example.test", "secret",
                    profile_id=handle.profile_id, engine="selenium",
                    profile_path=str(handle.path),
                )

    def test_account_manager_json_migration_preserves_profile_binding_fields(self):
        from core.account_manager import AccountManager

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "data"
            data.mkdir()
            source = data / "accounts.json"
            source.write_text(json.dumps([{
                "email": "migrated@example.test",
                "password": "secret",
                "profile_id": "profile-migrated",
                "engine": "selenium",
                "profile_state": "ready",
                "identity_state": "native",
                "browser_status": "authenticated",
                "overall_status": "active",
            }]), encoding="utf-8")
            manager = AccountManager(str(root / "accounts.db"))
            previous = os.getcwd()
            try:
                os.chdir(root)
                migrated = manager.migrate_old_data()
            finally:
                os.chdir(previous)
            self.assertEqual(migrated, 1)
            account = manager.get_all()[0]
            self.assertEqual(account["profile_id"], "profile-migrated")
            self.assertEqual(account["engine"], "selenium")
            self.assertEqual(account["profile_state"], "ready")

    def test_account_manager_profile_id_only_migration_uses_fail_closed_projection(self):
        from core.account_manager import AccountManager

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "data"
            data.mkdir()
            (data / "accounts.json").write_text(json.dumps([{
                "email": "legacy-manager@example.test",
                "password": "secret",
                "last_name": "Imported",
                "profile_id": "legacy-manager-profile",
            }]), encoding="utf-8")
            manager = AccountManager(str(root / "accounts.db"))
            previous = os.getcwd()
            try:
                os.chdir(root)
                migrated = manager.migrate_old_data()
            finally:
                os.chdir(previous)

            self.assertEqual(migrated, 1)
            account = manager.get_all()[0]
            self.assertEqual(account["profile_state"], "legacy_unbound")
            self.assertEqual(account["identity_state"], "identity_reconstructed")
            self.assertEqual(account["browser_status"], "not_configured")
            self.assertEqual(account["overall_status"], "unknown")

    def test_json_migration_rejects_non_string_profile_id_without_creating_account(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "accounts.json"
            source.write_text(json.dumps([{
                "email": "malformed@example.test",
                "password": "secret",
                "profile_id": 123,
            }]), encoding="utf-8")
            database = DatabaseManager(str(root / "accounts.db"))

            self.assertEqual(database.run_migration(str(source), str(root / "missing.txt")), 0)
            self.assertEqual(database.get_account_count(), 0)

    def test_json_migration_isolates_malformed_profile_id_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "accounts.json"
            source.write_text(json.dumps([
                {
                    "email": "malformed@example.test",
                    "password": "secret",
                    "profile_id": True,
                },
                {
                    "email": "valid@example.test",
                    "password": "secret",
                },
            ]), encoding="utf-8")
            database = DatabaseManager(str(root / "accounts.db"))

            self.assertEqual(database.run_migration(str(source), str(root / "missing.txt")), 1)
            accounts = database.get_all_accounts()
            self.assertEqual([item["email"] for item in accounts], ["valid@example.test"])

    def test_playwright_warmer_uses_existing_authenticated_profile(self):
        provider_body = b")]}\'\n" + json.dumps({
            "accounts": [{
                "slot": 0,
                "email": "profile@example.test",
                "valid_session": True,
            }],
        }).encode()

        class Response:
            status = 200
            url = "https://accounts.google.com/ListAccounts?gpsia=1&source=ChromiumBrowser&json=standard"

            async def body(self):
                return provider_body

        class Page:
            def __init__(self, *, provider=False):
                self.url = "about:blank" if provider else ""
                self.provider = provider
                self.visited = []
                self.closed = False

            async def goto(self, url, **kwargs):
                self.visited.append(url)
                if "ListAccounts" in url:
                    self.url = Response.url
                    return Response()
                self.url = "https://mail.google.com/mail/u/0/#inbox"
                return types.SimpleNamespace(status=200)

            async def close(self):
                self.closed = True

            async def content(self):
                return "Inbox Compose Search mail"

            async def wait_for_timeout(self, *_args, **_kwargs):
                return None

            async def query_selector(self, *_args, **_kwargs):
                raise AssertionError("existing profile should not query login fields")

            async def evaluate(self, script, **_kwargs):
                if "[role=\"main\"]" in script:
                    return True
                return None

        class Context:
            def __init__(self):
                self.page = Page()
                self.provider = Page(provider=True)
                self.closed = False

            async def new_page(self):
                return self.provider

            async def close(self):
                self.closed = True

            async def cookies(self):
                return [_valid_auth_cookie()]

        class Manager:
            def __init__(self):
                self.context = Context()
                self.page = self.context.page
                self.profile_path = None
                self.initialized_kwargs = None

            async def initialize(self, **kwargs):
                self.initialized_kwargs = kwargs
                self.profile_path = kwargs["profile_path"]
                return True

            async def close(self):
                await self.context.close()
                return {"success": True, "browser_process_stopped": True}

        with tempfile.TemporaryDirectory() as directory:
            runtime = ProfileRuntime(directory)
            handle = runtime.provision("profile@example.test", "playwright")
            runtime.bind(handle, "profile@example.test")
            runtime.mark_ready(handle)
            manager = Manager()
            fake_modules = {
                "core.stealth_browser": types.SimpleNamespace(
                    PlaywrightStealthManager=lambda: manager
                ),
            }
            with patch.object(ProfileRuntime, "from_environment", return_value=runtime), \
                 patch.dict(sys.modules, fake_modules):
                from core.account_warmer import warm_account_playwright
                result = asyncio.run(warm_account_playwright(
                    "profile@example.test", "secret", 0, profile_id=handle.profile_id
                ))
            self.assertTrue(result["success"])
            self.assertEqual(manager.profile_path, str(handle.path))
            self.assertEqual(manager.context.page.visited, ["https://mail.google.com/"])
            self.assertTrue(manager.context.closed)
            self.assertEqual(manager.initialized_kwargs["profile_manifest"]["profile_id"], handle.profile_id)

    def test_selenium_warmer_uses_existing_authenticated_profile(self):
        provider_body = b")]}\'\n" + json.dumps({
            "accounts": [{
                "slot": 0,
                "email": "profile@example.test",
                "valid_session": True,
            }],
        }).encode()

        class Driver:
            def __init__(self):
                self.current_url = ""
                self.visited = []
                self.closed = False
                self.current_window_handle = "business"
                self._handles = ["business"]

                class SwitchTo:
                    def __init__(inner, driver):
                        inner.driver = driver

                    def new_window(inner, _kind):
                        inner.driver._handles.append("provider")
                        inner.driver.current_window_handle = "provider"

                    def window(inner, handle):
                        if handle not in inner.driver._handles:
                            raise RuntimeError("unknown window")
                        inner.driver.current_window_handle = handle

                self.switch_to = SwitchTo(self)

            @property
            def window_handles(self):
                return list(self._handles)

            @property
            def page_source(self):
                if self.current_window_handle == "provider":
                    return provider_body.decode()
                return "Inbox Compose Search mail"

            def get(self, url):
                self.visited.append(url)
                if "ListAccounts" in url:
                    self.current_url = "https://accounts.google.com/ListAccounts?gpsia=1&source=ChromiumBrowser&json=standard"
                    return
                self.current_url = "https://mail.google.com/mail/u/0/#inbox"

            def get_cookies(self):
                return [_valid_auth_cookie()]

            def execute_script(self, script, **_kwargs):
                if "document.body" in script:
                    return provider_body.decode()
                if "[role=\"main\"]" in script:
                    return self.current_window_handle == "business"
                return None

            def close(self):
                if self.current_window_handle != "business":
                    self._handles.remove(self.current_window_handle)
                    self.current_window_handle = "business"

            def quit(self):
                self.closed = True

        driver = Driver()

        class By:
            CSS_SELECTOR = "css selector"
            XPATH = "xpath"

        class WebDriverWait:
            def __init__(self, *_args, **_kwargs):
                pass

            def until(self, *_args, **_kwargs):
                raise AssertionError("existing profile should not wait for login fields")

        captured = {}

        def create_driver(**kwargs):
            captured.update(kwargs)
            return driver

        fake_selenium_runner = types.SimpleNamespace(create_driver=create_driver)
        fake_modules = {
            "core.selenium_runner": fake_selenium_runner,
            "selenium": types.ModuleType("selenium"),
            "selenium.webdriver": types.ModuleType("selenium.webdriver"),
            "selenium.webdriver.common": types.ModuleType("selenium.webdriver.common"),
            "selenium.webdriver.common.by": types.SimpleNamespace(By=By),
            "selenium.webdriver.support": types.ModuleType("selenium.webdriver.support"),
            "selenium.webdriver.support.wait": types.SimpleNamespace(WebDriverWait=WebDriverWait),
            "selenium.webdriver.support.expected_conditions": types.ModuleType(
                "selenium.webdriver.support.expected_conditions"
            ),
        }
        with patch.dict(sys.modules, fake_modules):
            from core.account_warmer import warm_account_selenium

            with tempfile.TemporaryDirectory() as directory:
                runtime = ProfileRuntime(directory)
                handle = runtime.provision("profile@example.test", "selenium")
                runtime.bind(handle, "profile@example.test")
                runtime.mark_ready(handle)
                with patch.object(ProfileRuntime, "from_environment", return_value=runtime):
                    result = warm_account_selenium(
                        "profile@example.test", "secret", 0, profile_id=handle.profile_id
                    )
                self.assertTrue(result["success"])
                self.assertEqual(driver.visited, [
                    "https://mail.google.com/",
                    "https://accounts.google.com/ListAccounts?gpsia=1&source=ChromiumBrowser&json=standard",
                ])
                self.assertEqual(captured["profile_path"], str(handle.path))
                self.assertEqual(captured["profile_manifest"]["profile_id"], handle.profile_id)
                self.assertTrue(driver.closed)


if __name__ == "__main__":
    unittest.main()
