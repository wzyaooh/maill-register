"""Reconciliation and deliberate legacy-adoption contract tests."""
import json
import tempfile
import unittest
from pathlib import Path

from core.profile_runtime import (
    ProfileConflictError,
    ProfileRuntime,
    ProfileUnavailableError,
)


class ProfileReconciliationTests(unittest.TestCase):
    def test_legacy_directory_without_manifest_is_reported_explicitly(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = ProfileRuntime(directory)
            legacy = runtime.profiles_root / "legacy-profile"
            legacy.mkdir()
            (legacy / "Cookies").write_text("placeholder", encoding="utf-8")

            records = runtime.reconcile()

            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["profile_id"], "legacy-profile")
            self.assertEqual(records[0]["state"], "legacy_unbound")
            self.assertEqual(records[0]["error_code"], "manifest_missing")

    def test_bound_profile_without_database_binding_is_marked_orphaned(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = ProfileRuntime(directory)
            handle = runtime.provision("bound@example.test", "playwright")
            runtime.bind(handle, "bound@example.test")
            runtime.mark_ready(handle)

            records = runtime.reconcile(accounts=[])

            self.assertEqual(records[0]["state"], "orphaned")
            self.assertEqual(records[0]["error_code"], "database_binding_missing")
            self.assertEqual(runtime.load(handle)["state"], "orphaned")

    def test_database_row_without_manifest_is_reported_as_profile_conflict(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = ProfileRuntime(directory)
            records = runtime.reconcile(accounts=[{
                "email": "missing@example.test",
                "profile_id": "missing-profile",
                "engine": "playwright",
            }])

            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["profile_id"], "missing-profile")
            self.assertEqual(records[0]["state"], "corrupt")
            self.assertEqual(records[0]["error_code"], "profile_missing")

    def test_database_row_with_a_different_profile_path_is_a_binding_conflict(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = ProfileRuntime(directory)
            handle = runtime.provision("bound@example.test", "playwright")
            runtime.bind(handle, "bound@example.test")
            runtime.mark_ready(handle)

            records = runtime.reconcile(accounts=[{
                "email": "bound@example.test",
                "profile_id": handle.profile_id,
                "engine": "playwright",
                "profile_path": str(runtime.profiles_root / "another-profile"),
            }])

            self.assertEqual(records[0]["state"], "corrupt")
            self.assertEqual(records[0]["error_code"], "binding_mismatch")

    def test_database_path_only_row_is_reported_as_legacy_unbound(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = ProfileRuntime(directory)
            legacy = runtime.profiles_root / "legacy-row"
            legacy.mkdir()
            records = runtime.reconcile(accounts=[{
                "email": "legacy@example.test",
                "profile_id": "",
                "profile_path": str(legacy),
            }])

            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["state"], "legacy_unbound")
            self.assertEqual(records[0]["error_code"], "database_legacy_path")

    def test_database_row_with_invalid_profile_id_is_explicitly_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = ProfileRuntime(directory)
            records = runtime.reconcile(accounts=[{
                "email": "invalid@example.test",
                "profile_id": "../escape",
                "engine": "playwright",
            }])
            self.assertEqual(records[0]["state"], "corrupt")
            self.assertEqual(records[0]["error_code"], "invalid_profile_id")

    def test_manifest_and_database_binding_mismatch_is_not_silently_repaired(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = ProfileRuntime(directory)
            handle = runtime.provision("bound@example.test", "playwright")
            runtime.bind(handle, "bound@example.test")
            runtime.mark_ready(handle)

            records = runtime.reconcile(accounts=[{
                "email": "different@example.test",
                "profile_id": handle.profile_id,
                "engine": "playwright",
            }])

            self.assertEqual(records[0]["state"], "corrupt")
            self.assertEqual(records[0]["error_code"], "binding_mismatch")
            self.assertEqual(runtime.load(handle)["state"], "ready")

    def test_adopted_profile_requires_fresh_identity_verification_before_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = ProfileRuntime(directory)
            legacy = runtime.profiles_root / "legacy-adopted"
            legacy.mkdir()
            (legacy / "Cookies").write_text("placeholder", encoding="utf-8")

            handle = runtime.adopt_legacy(
                str(legacy), "adopted@example.test", "selenium"
            )
            manifest = runtime.load(handle)
            self.assertEqual(manifest["identity_state"], "identity_reconstructed")
            self.assertFalse(manifest.get("identity_verified", False))

            with self.assertRaises(ProfileConflictError):
                runtime.mark_ready(handle)
            with self.assertRaises(ProfileConflictError):
                runtime.mark_identity_verified(handle, "other@example.test")

            runtime.mark_identity_verified(handle, "adopted@example.test")
            runtime.mark_ready(handle)
            manifest = runtime.load(handle)
            self.assertEqual(manifest["state"], "ready")
            self.assertTrue(manifest["identity_verified"])

    def test_adoption_can_persist_a_single_canonical_database_binding(self):
        from core.database import DatabaseManager

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = ProfileRuntime(directory)
            legacy = runtime.profiles_root / "legacy-with-db"
            legacy.mkdir()
            database = DatabaseManager(str(root / "accounts.db"))

            handle = runtime.adopt_legacy(
                str(legacy), "adopted@example.test", "playwright",
                database=database, password="secret",
            )

            row = database.get_account_by_profile_id(handle.profile_id)
            self.assertIsNotNone(row)
            self.assertEqual(row["email"], "adopted@example.test")
            self.assertEqual(row["engine"], "playwright")
            self.assertEqual(row["profile_state"], "bound")
            self.assertEqual(row["identity_state"], "identity_reconstructed")
            self.assertEqual(row["profile_path"], str(handle.path))

            records = runtime.reconcile(database=database)
            self.assertEqual(records[0]["state"], "bound")

            runtime.mark_identity_verified(
                handle, "adopted@example.test", database=database
            )
            runtime.mark_ready(handle, database=database)
            row = database.get_account_by_profile_id(handle.profile_id)
            self.assertEqual(row["profile_state"], "ready")
            self.assertEqual(row["identity_state"], "identity_reconstructed")

    def test_adoption_persists_the_database_proxy_binding_in_manifest(self):
        from core.database import DatabaseManager
        from core.profile_runtime import proxy_binding

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = ProfileRuntime(directory)
            legacy = runtime.profiles_root / "legacy-proxy"
            legacy.mkdir()
            database = DatabaseManager(str(root / "accounts.db"))
            proxy = "proxy.example.test:8443:user:secret"

            handle = runtime.adopt_legacy(
                str(legacy), "proxy@example.test", "playwright",
                database=database, password="secret", proxy=proxy,
            )

            manifest = runtime.load(handle)
            self.assertEqual(manifest["network"], proxy_binding(proxy))
            row = database.get_account_by_profile_id(handle.profile_id)
            self.assertEqual(row["proxy"], proxy)
            runtime.validate_proxy(manifest, proxy)

    def test_adoption_database_failure_marks_manifest_orphaned(self):
        class FailingDatabase:
            def get_account_by_profile_id(self, _profile_id):
                return None

            def save_profile_binding(self, **_kwargs):
                raise RuntimeError("database is unavailable")

        with tempfile.TemporaryDirectory() as directory:
            runtime = ProfileRuntime(directory)
            legacy = runtime.profiles_root / "legacy-failure"
            legacy.mkdir()
            with self.assertRaises(ProfileConflictError):
                runtime.adopt_legacy(
                    str(legacy), "failure@example.test", "selenium",
                    database=FailingDatabase(), password="secret",
                )

            handle = runtime._handle("legacy-failure")
            self.assertEqual(runtime.load(handle)["state"], "orphaned")

    def test_reconcile_accepts_database_as_legacy_positional_argument(self):
        from core.database import DatabaseManager

        with tempfile.TemporaryDirectory() as directory:
            runtime = ProfileRuntime(directory)
            database = DatabaseManager(str(Path(directory) / "accounts.db"))
            records = runtime.reconcile(database)
            self.assertEqual(records, [])

    def test_reconciliation_diagnostics_do_not_include_credentials_or_cookies(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = ProfileRuntime(directory)
            handle = runtime.provision("safe@example.test", "playwright")
            manifest = runtime.load(handle)
            manifest["password"] = "do-not-leak"
            manifest["cookies"] = [{"name": "SID", "value": "secret-cookie"}]
            runtime._atomic_json(handle.manifest_path, manifest)

            records = runtime.reconcile()
            serialized = json.dumps(records, sort_keys=True)
            self.assertNotIn("do-not-leak", serialized)
            self.assertNotIn("secret-cookie", serialized)

    def test_symlink_profile_is_reported_instead_of_silently_skipped(self):
        with tempfile.TemporaryDirectory() as directory, \
             tempfile.TemporaryDirectory() as outside:
            runtime = ProfileRuntime(directory)
            link = runtime.profiles_root / "unsafe-profile"
            link.symlink_to(Path(outside), target_is_directory=True)

            records = runtime.reconcile()

            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["profile_id"], "unsafe-profile")
            self.assertEqual(records[0]["state"], "corrupt")
            self.assertEqual(records[0]["error_code"], "unsafe_profile_path")


if __name__ == "__main__":
    unittest.main()
