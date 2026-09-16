"""Consistent confidential SQLite snapshot contract."""

import contextlib
import io
import os
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    from core import database_backup
except ImportError:
    database_backup = None


class DatabaseBackupTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(database_backup, "SQLite snapshot API is not implemented")
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.source = self.directory / "source.sqlite"
        self.destination = self.directory / "snapshot.sqlite"

    def create_database(self):
        connection = sqlite3.connect(str(self.source))
        self.addCleanup(connection.close)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute("CREATE TABLE fixture (value TEXT)")
        connection.execute("INSERT INTO fixture VALUES ('committed')")
        connection.commit()
        return connection

    def test_snapshot_restores_committed_wal_row_without_uncommitted_write(self):
        source = self.create_database()
        source.execute("INSERT INTO fixture VALUES ('uncommitted')")
        before = self.source.read_bytes()
        self.assertEqual(database_backup.backup_database(self.source, self.destination), self.destination)
        with contextlib.closing(sqlite3.connect(str(self.destination))) as restored:
            self.assertEqual(restored.execute('SELECT value FROM fixture').fetchone(), ('committed',))
            self.assertEqual(restored.execute("SELECT COUNT(*) FROM fixture").fetchone(), (1,))
            self.assertEqual(restored.execute("PRAGMA integrity_check").fetchall(), [("ok",)])
        self.assertEqual(self.source.read_bytes(), before)

    def test_existing_destination_is_never_overwritten(self):
        self.create_database()
        self.destination.write_bytes(b"keep this existing file")
        with self.assertRaises(FileExistsError):
            database_backup.backup_database(self.source, self.destination)
        self.assertEqual(self.destination.read_bytes(), b"keep this existing file")

    def test_existing_and_dangling_symlink_destinations_are_rejected(self):
        self.create_database()
        for target in (self.source, self.directory / "missing-target"):
            with self.subTest(target_exists=target.exists()):
                self.destination.symlink_to(target)
                with self.assertRaises(FileExistsError):
                    database_backup.backup_database(self.source, self.destination)
                self.assertTrue(self.destination.is_symlink())
                self.destination.unlink()
        self.assertFalse((self.directory / "missing-target").exists())

    def test_preexisting_sidecars_are_rejected_and_preserved(self):
        self.create_database()
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = Path(str(self.destination) + suffix)
            for dangling in (False, True):
                with self.subTest(suffix=suffix, dangling=dangling):
                    if dangling:
                        sidecar.symlink_to(self.directory / ("absent-" + suffix[1:]))
                    else:
                        sidecar.write_bytes(b"sidecar sentinel")
                    try:
                        with self.assertRaises(FileExistsError):
                            database_backup.backup_database(self.source, self.destination)
                    finally:
                        if self.destination.exists() or self.destination.is_symlink():
                            self.destination.unlink()
                    self.assertTrue(os.path.lexists(str(sidecar)))
                    if not dangling:
                        self.assertEqual(sidecar.read_bytes(), b"sidecar sentinel")
                    sidecar.unlink()

    def test_failure_does_not_remove_replacement_at_destination_path(self):
        self.create_database()
        original = sqlite3.connect

        def connect(*args, **kwargs):
            if not kwargs.get("uri"):
                self.destination.unlink()
                self.destination.write_bytes(b"replacement sentinel")
                raise sqlite3.OperationalError("synthetic destination failure")
            return original(*args, **kwargs)

        with patch.object(database_backup.sqlite3, "connect", side_effect=connect):
            with self.assertRaises(sqlite3.OperationalError):
                database_backup.backup_database(self.source, self.destination)
        self.assertEqual(self.destination.read_bytes(), b"replacement sentinel")

    def test_missing_source_is_not_created_and_partial_destination_is_removed(self):
        with self.assertRaises(Exception):
            database_backup.backup_database(self.source, self.destination)
        self.assertFalse(self.source.exists())
        self.assertFalse(self.destination.exists())

    def test_invalid_database_failure_removes_partial_snapshot(self):
        self.source.write_bytes(b"not a SQLite database")
        with self.assertRaises(sqlite3.DatabaseError):
            database_backup.backup_database(self.source, self.destination)
        self.assertFalse(self.destination.exists())
        self.assertEqual(self.source.read_bytes(), b"not a SQLite database")

    def test_destination_connection_failure_removes_partial_snapshot(self):
        self.create_database()
        original = sqlite3.connect

        def connect(*args, **kwargs):
            if not kwargs.get("uri"):
                raise sqlite3.OperationalError("synthetic sensitive failure")
            return original(*args, **kwargs)

        with patch.object(database_backup.sqlite3, "connect", side_effect=connect):
            with self.assertRaises(sqlite3.OperationalError):
                database_backup.backup_database(self.source, self.destination)
        self.assertFalse(self.destination.exists())

    def test_integrity_failure_removes_partial_snapshot(self):
        self.create_database()
        original = sqlite3.connect

        class InvalidIntegrityConnection(sqlite3.Connection):
            def execute(self, sql, *args, **kwargs):
                if sql == "PRAGMA integrity_check":
                    return super().execute("SELECT 'synthetic integrity failure'")
                return super().execute(sql, *args, **kwargs)

        def connect(*args, **kwargs):
            if not kwargs.get("uri"):
                kwargs["factory"] = InvalidIntegrityConnection
            return original(*args, **kwargs)

        with patch.object(database_backup.sqlite3, "connect", side_effect=connect):
            with self.assertRaises(sqlite3.DatabaseError):
                database_backup.backup_database(self.source, self.destination)
        self.assertFalse(self.destination.exists())

    @unittest.skipUnless(os.name == "posix", "POSIX permission contract")
    def test_snapshot_is_private_even_with_permissive_umask(self):
        self.create_database()
        previous = os.umask(0)
        try:
            database_backup.backup_database(self.source, self.destination)
        finally:
            os.umask(previous)
        self.assertEqual(stat.S_IMODE(self.destination.stat().st_mode), 0o600)

    def test_cli_failure_does_not_disclose_paths_or_exception_payload(self):
        output = io.StringIO()
        with patch.object(database_backup, "backup_database", side_effect=RuntimeError("secret-credential")), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            status = database_backup.main([str(self.source), str(self.destination)])
        self.assertNotEqual(status, 0)
        self.assertNotIn("secret-credential", output.getvalue())
        self.assertNotIn(str(self.directory), output.getvalue())

    def test_cli_invalid_arguments_do_not_disclose_argument_values(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            with self.assertRaises(SystemExit) as raised:
                database_backup.main([str(self.source), str(self.destination), "secret-credential"])
        self.assertNotEqual(raised.exception.code, 0)
        self.assertNotIn("secret-credential", output.getvalue())
        self.assertNotIn(str(self.directory), output.getvalue())


if __name__ == "__main__":
    unittest.main()
