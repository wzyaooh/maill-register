"""Explicit, confidential SQLite snapshots; never run automatically.

The public snapshot operation creates and validates a private temporary file in
the destination directory, then publishes it with a platform no-clobber
primitive.  The final destination is never opened or removed by a failure
path, so a competing process can safely create it while a snapshot is being
built.
"""

import argparse
import errno
import os
import sqlite3
import stat
import sys
import tempfile
from contextlib import closing
from pathlib import Path


class SnapshotError(RuntimeError):
    """Base class for finite, non-sensitive snapshot errors."""

    code = "snapshot_failed"


class SnapshotPublishUnsupported(SnapshotError):
    """The current filesystem cannot provide a no-clobber publication."""

    code = "snapshot_publish_unsupported"


class SnapshotDurabilityUnconfirmed(SnapshotError):
    """The file was published but its directory durability is unknown."""

    code = "snapshot_durability_unconfirmed"


def _reject_symlink(path: Path, *, missing_ok: bool = True) -> None:
    """Reject a symlink at one security boundary without following it."""
    if path.is_symlink():
        raise FileExistsError("snapshot path cannot be a symlink")
    if not missing_ok and not path.exists():
        raise FileNotFoundError(str(path))


def _validate_source(source: Path) -> Path:
    _reject_symlink(source)
    source_info = os.lstat(str(source))
    if not stat.S_ISREG(source_info.st_mode):
        raise ValueError("snapshot source must be a regular file")
    return source.resolve(strict=True)


def _validate_destination(destination: Path) -> Path:
    parent = destination.parent
    _reject_symlink(parent, missing_ok=False)
    parent_info = os.lstat(str(parent))
    if not stat.S_ISDIR(parent_info.st_mode):
        raise NotADirectoryError(str(parent))

    # lexists deliberately includes dangling symlinks.  No final path or
    # SQLite sidecar may be claimed by this operation.
    for candidate in (
        destination,
        Path(str(destination) + "-wal"),
        Path(str(destination) + "-shm"),
        Path(str(destination) + "-journal"),
    ):
        if os.path.lexists(str(candidate)):
            raise FileExistsError("snapshot destination already exists")
    return destination


def _create_private_temp(destination: Path):
    """Create a private same-directory temporary file and return fd/path."""
    descriptor, temporary = tempfile.mkstemp(
        prefix=".snapshot-", suffix=".tmp", dir=str(destination.parent)
    )
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)
        else:  # pragma: no cover - Python platforms without fchmod.
            os.chmod(temporary, 0o600)
    except BaseException:
        try:
            os.close(descriptor)
        finally:
            try:
                os.unlink(temporary)
            except OSError:
                pass
        raise
    return descriptor, Path(temporary)


def _sync_file(path: Path) -> None:
    """Flush one snapshot file before it can be published."""
    if not hasattr(os, "fsync"):
        raise SnapshotPublishUnsupported()
    descriptor = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sync_directory(directory: Path) -> None:
    """Flush a POSIX directory entry after successful publication."""
    if os.name == "nt":  # Windows rename is durable within its own contract.
        return
    if not hasattr(os, "fsync") or not hasattr(os, "O_DIRECTORY"):
        raise SnapshotPublishUnsupported()
    descriptor = os.open(
        str(directory), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _publish_noclobber(temporary: Path, destination: Path) -> None:
    """Atomically publish a temporary file without replacing a destination."""
    if os.name == "posix":
        try:
            # Hard-linking two names in one directory is atomic and never
            # replaces an existing destination.  The temporary name is
            # removed only after the link has succeeded.
            os.link(str(temporary), str(destination))
        except OSError as exc:
            if getattr(exc, "errno", None) in (errno.EOPNOTSUPP, errno.ENOTSUP):
                raise SnapshotPublishUnsupported() from exc
            raise
        return

    if os.name == "nt":  # pragma: no cover - exercised by Windows CI.
        # Python maps os.rename to the Windows no-replace rename primitive;
        # unlike POSIX rename, an existing destination raises FileExistsError.
        try:
            os.rename(str(temporary), str(destination))
        except OSError as exc:
            if getattr(exc, "errno", None) in (errno.EOPNOTSUPP, errno.ENOTSUP):
                raise SnapshotPublishUnsupported() from exc
            raise
        return

    raise SnapshotPublishUnsupported()


def _remove_temporary(path: Path) -> None:
    """Best-effort cleanup restricted to the generated temporary pathname."""
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        # The primary snapshot error is more useful than a cleanup payload;
        # callers must never be tempted to delete the final destination.
        pass


def backup_database(source: Path, destination: Path) -> Path:
    """Copy committed records into a new private SQLite snapshot.

    ``destination`` must not exist, including as a symlink or SQLite sidecar.
    All work happens in a random same-directory temporary file.  A failure
    removes that temporary file only; it never unlinks the final destination.
    """
    source = _validate_source(Path(source))
    destination = _validate_destination(Path(destination))
    descriptor = None
    temporary = None
    published = False
    try:
        descriptor, temporary = _create_private_temp(destination)
        # Closing the creation descriptor lets SQLite open the file on Windows
        # while retaining the 0600 mode established above.
        os.close(descriptor)
        descriptor = None

        with closing(
            sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
        ) as source_connection:
            with closing(sqlite3.connect(str(temporary))) as destination_connection:
                source_connection.backup(destination_connection)
                destination_connection.execute("PRAGMA journal_mode=DELETE")
                if destination_connection.execute(
                    "PRAGMA integrity_check"
                ).fetchall() != [("ok",)]:
                    raise sqlite3.DatabaseError("Snapshot integrity check failed")

        _sync_file(temporary)
        _publish_noclobber(temporary, destination)
        published = True
        try:
            _sync_directory(destination.parent)
        except KeyboardInterrupt:
            # Cancellation after publication cannot safely remove the final
            # file.  Preserve the cancellation signal and the artifact.
            raise
        except BaseException as exc:
            # The final path is already visible; report that its directory
            # entry durability is unknown and leave it intact.
            raise SnapshotDurabilityUnconfirmed() from exc
        return destination
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if temporary is not None:
            # The hard link/rename consumed the temporary name on success;
            # otherwise this removes only our private partial snapshot.
            _remove_temporary(temporary)


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(
            2,
            "Database snapshot arguments are invalid; supply source and new destination.\n",
        )


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
