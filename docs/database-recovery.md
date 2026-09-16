# Database Recovery

SQLite snapshots are confidential recovery artifacts: account databases may
contain credentials and other sensitive records. Do not attach snapshots to
issues, upload them as CI artifacts, commit them, or log their contents. Keep
them in a trusted, owner-only directory with access-controlled storage and
encryption when transferring or retaining them off-machine. On POSIX systems
the snapshot is created with mode `0600`; on other systems configure an
equivalent owner-only filesystem ACL. Use a new destination for every snapshot.

## Before Upgrade or Migration

1. Record the currently deployed code version, dependency environment, and
   configuration version without copying secrets into logs.
2. Quiesce registration, workers, and other writers for a coordinated release
   boundary. Before starting the new version or any schema migration, explicitly
   create a snapshot using the old deployment's interpreter:

   ```sh
   python -m core.database_backup /trusted/source.sqlite /private/recovery/pre-upgrade.sqlite
   ```

3. Confirm the command succeeds and retain the snapshot with its compatible
   application version. Rehearse restoring a copy into an isolated environment
   with provider work disabled before relying on it.
4. Only then deploy the new code and run migrations. Do not assume that an old
   application can read a migrated database or that migrations are reversible.

`backup_database(source: Path, destination: Path) -> Path` uses SQLite's backup
API against a read-only source, so committed WAL records are included without
copying live SQLite files by hand. Uncommitted transactions are not included.
It checks snapshot integrity, makes the result a standalone database, refuses
any existing destination (including dangling symlinks), and removes its partial
destination on failure. A missing source is never created. The CLI reports only
generic success/failure, not paths, exception payloads, or database records.
Choose a trusted destination directory that cannot be concurrently modified by
another user or process. This is an explicit tool, not an automatic startup or
scheduled backup; no snapshot is taken just by importing or starting the app.

## Rollback

Stop the application and supervised workers and reconcile any in-flight
external operations before replacing a database. Restoring an earlier local
ledger cannot undo provider-side purchases, acknowledgements, or account
changes, and must not be treated as proof that those operations never happened.
Preserve the post-upgrade database for controlled investigation if necessary.

Restore the verified pre-upgrade snapshot into a new deployment location, then
point the matching old code and environment at that copy. Verify integrity and
expected records offline before enabling writes or provider work. Never replace
an open database or leave old `-wal`/`-shm` sidecars next to a replacement: they
belong to the stopped database's state. Keep snapshot files immutable during
restore rehearsal. A newer schema requires a tested backward-compatibility
contract or explicit reverse migration; an integrity check alone does not
establish compatibility. Rolling back loses all local changes made after the
snapshot, so reconcile those changes deliberately rather than rerunning jobs.

## Browser Profiles

A database snapshot is not a complete profile backup. It does not include
browser storage, cookies, profile manifests, or filesystem leases. Browser
engine/profile identity remains authoritative and cannot be reconstructed from
an account database row or guessed `profile_path` alone.

Profile backups are offline operations: shut down owned browser processes,
release leases only after verified shutdown, stop concurrent profile users,
then copy the entire profile and manifest together into equally confidential
storage. Do not copy an active profile and call it a consistent backup. Restore
profiles only with the matching engine, identity, binding, and verified runtime
compatibility. A database-only restore may leave accounts without usable
profiles; keep creation and health operations disabled until reconciliation.
