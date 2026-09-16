import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from core.database import DatabaseManager


class ProfileDatabaseContractTests(unittest.TestCase):
    def test_new_columns_and_profile_uniqueness_are_migrated(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "accounts.db"
            with sqlite3.connect(path) as conn:
                conn.execute("CREATE TABLE accounts (id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL, password TEXT NOT NULL)")
                conn.commit()
            db = DatabaseManager(str(path))
            columns = {row[1] for row in sqlite3.connect(path).execute("PRAGMA table_info(accounts)")}
            for column in ("profile_id", "engine", "profile_state", "identity_state",
                           "browser_status", "mailbox_status", "overall_status",
                           "browser_checked_at", "mailbox_checked_at", "last_error_code"):
                self.assertIn(column, columns)
            self.assertTrue(db.save_account("one@example.test", "secret", profile_id="profile-1", engine="playwright"))
            self.assertFalse(db.save_account("two@example.test", "secret", profile_id="profile-1", engine="selenium"))
            self.assertEqual(db.get_account_by_profile_id("profile-1")["email"], "one@example.test")

    def test_health_snapshot_round_trip_is_atomic_and_derived(self):
        with tempfile.TemporaryDirectory() as directory:
            db = DatabaseManager(str(Path(directory) / "accounts.db"))
            self.assertTrue(db.save_account("one@example.test", "secret", profile_id="p1", engine="playwright"))
            snapshot = {
                "email": "one@example.test", "engine": "playwright", "profile_id": "p1",
                "browser_status": "profile_busy", "mailbox_status": "active",
                "status": "degraded", "message": "busy", "browser_checked_at": "b",
                "mailbox_checked_at": "m", "checked_at": "c", "last_error_code": "profile_busy",
            }
            self.assertTrue(db.update_health_snapshot("one@example.test", snapshot))
            row = db.get_all_accounts()[0]
            self.assertEqual(row["browser_status"], "profile_busy")
            self.assertEqual(row["mailbox_status"], "active")
            self.assertEqual(row["overall_status"], "degraded")
            self.assertEqual(row["status"], "degraded")
            self.assertEqual(row["last_error_code"], "profile_busy")
            self.assertEqual(row["notes"], "busy")

    def test_health_snapshot_and_account_creation_reject_free_form_error_codes(self):
        with tempfile.TemporaryDirectory() as directory:
            db = DatabaseManager(str(Path(directory) / "accounts.db"))
            self.assertTrue(db.save_account(
                "created@example.test", "secret",
                last_error_code="otp=246810 token=database-secret-739",
            ))
            created = db.get_all_accounts()[0]
            self.assertEqual(created["last_error_code"], "")

            snapshot = {
                "browser_status": "profile_busy",
                "mailbox_status": "active",
                "status": "degraded",
                "last_error_code": "api-key=database-secret-740",
            }
            self.assertTrue(db.update_health_snapshot("created@example.test", snapshot))
            updated = db.get_all_accounts()[0]
            self.assertEqual(updated["last_error_code"], "")

    def test_json_migration_marks_profileless_rows_legacy_unbound(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "accounts.json"
            source.write_text(json.dumps([{"email": "legacy@example.test", "password": "secret"}]), encoding="utf-8")
            db = DatabaseManager(str(root / "accounts.db"))
            self.assertEqual(db.run_migration(str(source), str(root / "missing.txt")), 1)
            row = db.get_all_accounts()[0]
            self.assertEqual(row["profile_state"], "legacy_unbound")
            self.assertEqual(row["identity_state"], "legacy_unbound")
            self.assertEqual(row["browser_status"], "not_configured")

    def test_profile_id_only_json_migration_is_reconstructed_and_not_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "accounts.json"
            source.write_text(json.dumps([{
                "email": "legacy-profile@example.test",
                "password": "secret",
                "first_name": "Legacy",
                "profile_id": "legacy-profile-only",
            }]), encoding="utf-8")
            db = DatabaseManager(str(root / "accounts.db"))

            self.assertEqual(
                db.run_migration(str(source), str(root / "missing.txt")), 1
            )
            row = db.get_all_accounts()[0]
            self.assertEqual(row["profile_state"], "legacy_unbound")
            self.assertEqual(row["identity_state"], "identity_reconstructed")
            self.assertEqual(row["browser_status"], "not_configured")
            self.assertEqual(row["overall_status"], "unknown")

    def test_verified_profile_binding_requires_engine_and_explicit_states(self):
        with tempfile.TemporaryDirectory() as directory:
            db = DatabaseManager(str(Path(directory) / "accounts.db"))
            with self.assertRaises(ValueError):
                db.save_account(
                    "missing-engine@example.test", "secret",
                    profile_id="verified-without-engine",
                    profile_state="ready", identity_state="native",
                    browser_status="authenticated", overall_status="active",
                )
            with self.assertRaises(ValueError):
                db.save_account(
                    "implicit-states@example.test", "secret",
                    profile_id="verified-implicit-states", engine="playwright",
                    browser_status="authenticated",
                )
            with self.assertRaises(ValueError):
                db.save_account(
                    "unsupported-appium@example.test", "secret",
                    profile_id="verified-appium", engine="appium",
                    profile_state="ready", identity_state="native",
                    browser_status="authenticated", overall_status="active",
                )
            self.assertEqual(db.get_account_count(), 0)


if __name__ == "__main__":
    unittest.main()
