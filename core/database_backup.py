"""Explicit, confidential SQLite snapshots; never run automatically."""

import argparse
import os
import sqlite3
import sys
from contextlib import closing
from pathlib import Path


def backup_database(source: Path, destination: Path) -> Path:
    """Copy committed records (including WAL) into a new private database.

    The destination directory must be trusted and not concurrently modified.
    Existing files and symlinks are refused by exclusive creation, not a
    check-then-create sequence. A failed snapshot is not a recovery artifact.
    """
    source = Path(source)
    destination = Path(destination)
    for suffix in ("-wal", "-shm", "-journal"):
        if os.path.lexists(str(destination) + suffix):
            raise FileExistsError("SQLite destination sidecar already exists")
    descriptor = os.open(str(destination), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    created_stat = os.fstat(descriptor)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)
        os.close(descriptor)
        descriptor = None
        with closing(sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True)) as source_connection:
            with closing(sqlite3.connect(str(destination))) as destination_connection:
                source_connection.backup(destination_connection)
                # A snapshot is a standalone file, not a second live WAL set.
                destination_connection.execute("PRAGMA journal_mode=DELETE")
                if destination_connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                    raise sqlite3.DatabaseError("Snapshot integrity check failed")
        return destination
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        try:
            current_stat = os.stat(destination)
        except FileNotFoundError:
            current_stat = None
        if current_stat is not None and (
            current_stat.st_dev == created_stat.st_dev
            and current_stat.st_ino == created_stat.st_ino
        ):
            destination.unlink()
        raise


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, "Database snapshot arguments are invalid; supply source and new destination.\n")


def main(argv=None):
    parser = _SafeArgumentParser(
        prog="database-backup", description="Create a new confidential SQLite snapshot",
    )
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    arguments = parser.parse_args(argv)
    try:
        backup_database(arguments.source, arguments.destination)
    except Exception:
        print("Database snapshot failed.", file=sys.stderr)
        return 1
    print("Database snapshot created and integrity verified.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
