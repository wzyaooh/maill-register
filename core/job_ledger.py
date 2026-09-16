"""Durable job/attempt ledger shared by orchestrators and retry decisions.

The ledger deliberately stores execution identity and normalized outcomes, not
credentials or provider payloads.  Each public mutation opens its own SQLite
connection and uses an immediate transaction so separate workers cannot claim
the same attempt ordinal concurrently.
"""

import json
import os
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from core.secret_safety import (
    normalize_error_code,
    safe_proxy_label,
    sanitize_operation_value,
)


_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_JOB_STATES = frozenset({
    "pending", "running", "succeeded", "failed", "cancelled",
    "interrupted", "blocked",
})
_ATTEMPT_STATES = frozenset({
    "running", "succeeded", "failed", "cancelled", "interrupted", "skipped",
})
_TERMINAL_JOB_STATES = frozenset({
    "succeeded", "failed", "cancelled", "blocked",
})

_TEXT_LIMITS = {
    "kind": 64,
    "requested_engine": 64,
    "subject": 256,
    "strategy": 256,
    "engine": 256,
    "profile_id": 256,
    "worker_id": 256,
    "proxy_label": 256,
}

_SAFE_LEDGER_STRATEGIES = frozenset({
    "", "standard", "youtube", "workspace", "mobile_ua", "offline",
    "warm", "health", "compensation", "playwright", "selenium",
})


_WORKER_ID_LOCK = threading.Lock()
_WORKER_ID_PID = None
_WORKER_ID = None


def _reset_worker_identity_after_fork() -> None:
    """Discard parent process ownership state in a forked child.

    A child has one surviving thread, so it must not retain a lock held by a
    vanished parent thread. ``register_at_fork`` runs this before the child
    can call the public identity function.
    """
    global _WORKER_ID_LOCK, _WORKER_ID_PID, _WORKER_ID
    _WORKER_ID_LOCK = threading.Lock()
    _WORKER_ID_PID = None
    _WORKER_ID = None


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_worker_identity_after_fork)


def current_worker_id() -> str:
    """Return this execution's durable owner token.

    A PID alone can be reused by the operating system and is copied into a
    forked child. Retain one random token per process and regenerate it when
    the observed PID changes, which keeps normal callbacks stable while a fork
    cannot inherit its parent's authority.
    """
    global _WORKER_ID, _WORKER_ID_PID
    pid = os.getpid()
    with _WORKER_ID_LOCK:
        if _WORKER_ID is None or _WORKER_ID_PID != pid:
            _WORKER_ID_PID = pid
            _WORKER_ID = "worker-%s-%s" % (pid, uuid.uuid4().hex)
        return _WORKER_ID


def _bounded_text(value: Any, field: str, *, allow_empty: bool = True) -> str:
    """Validate ledger identity text before it reaches SQLite or logs."""
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ValueError("%s must be text" % field)
    if any(ord(char) < 0x20 or ord(char) == 0x7f for char in value):
        raise ValueError("%s contains control characters" % field)
    normalized = value.strip()
    if not allow_empty and not normalized:
        raise ValueError("%s is required" % field)
    limit = _TEXT_LIMITS.get(field, 256)
    if len(normalized) > limit:
        raise ValueError("%s is too long" % field)
    return normalized


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_time(value: Any) -> Optional[datetime]:
    """Parse a durable timestamp and normalize it to UTC."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except (TypeError, ValueError):
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _required_time(value: Any, field: str = "timestamp"):
    parsed = _parse_time(value) if value is not None else datetime.now(timezone.utc)
    if parsed is None:
        raise ValueError("invalid %s" % field)
    return parsed, parsed.isoformat()


def _strict_int(value: Any, field: str, *, minimum: int = 0,
                maximum: Optional[int] = None) -> int:
    """Reject implicit float/string coercion at the ledger API boundary."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("%s must be an integer" % field)
    if value < minimum or (maximum is not None and value > maximum):
        raise ValueError("%s is out of range" % field)
    return value


def _legacy_int(value: Any, *, minimum: int = 0) -> Optional[int]:
    """Parse a legacy SQLite integer without accepting floats or booleans."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= minimum else None
    if isinstance(value, str) and re.fullmatch(r"[+-]?\d+", value.strip()):
        try:
            parsed = int(value.strip())
        except (TypeError, ValueError, OverflowError):
            return None
        return parsed if parsed >= minimum else None
    return None


def default_ledger_path() -> str:
    """Resolve the shared ledger path for CLI, web, and worker processes."""
    import os
    configured = os.environ.get("LEDGER_DB_PATH")
    if configured:
        return configured
    runtime_root = os.environ.get("PROFILE_RUNTIME_ROOT") or os.environ.get("GMAIL_DATA_ROOT")
    if runtime_root:
        return str(Path(runtime_root).expanduser() / "data" / "database.db")
    return "data/database.db"


def _safe_json(value: Any) -> str:
    safe = sanitize_operation_value(value if value is not None else {})
    return json.dumps(safe, ensure_ascii=True, separators=(",", ":"), default=str)


def _load_json(value: Any) -> Any:
    if not value:
        return {}
    try:
        decoded = json.loads(value)
        return decoded if isinstance(decoded, (dict, list)) else {}
    except (TypeError, ValueError):
        return {}


def _id(value: Optional[str], prefix: str) -> str:
    if value is None or value == "":
        result = prefix + uuid.uuid4().hex
    else:
        if not isinstance(value, str):
            raise ValueError("ledger identifier must be text")
        result = value.strip()
    if not _ID.fullmatch(result):
        raise ValueError("invalid ledger identifier")
    return result


def _existing_id(value: Any, field: str) -> str:
    """Validate an identifier supplied to a lookup/mutation API."""
    if not isinstance(value, str):
        raise ValueError("%s must be a text identifier" % field)
    result = value.strip()
    if not _ID.fullmatch(result):
        raise ValueError("invalid %s" % field)
    return result


def _safe_strategy(value: Any) -> str:
    """Keep attempt strategy labels finite and payload-free."""
    raw = _bounded_text(value, "strategy")
    candidate = raw.lower().replace("-", "_")
    return candidate if candidate in _SAFE_LEDGER_STRATEGIES else "unknown"


def _outcome_state(outcome: Any) -> str:
    if outcome is True:
        return "succeeded"
    if outcome is False or outcome is None:
        return "failed"
    value = str(outcome).strip().lower()
    aliases = {"success": "succeeded", "ok": "succeeded", "complete": "succeeded"}
    value = aliases.get(value, value)
    if value not in _ATTEMPT_STATES - {"running"}:
        raise ValueError("invalid attempt outcome")
    return value


class JobLedger:
    """SQLite-backed job and attempt state machine."""

    def __init__(self, db_path: str = "data/database.db"):
        self.db_path = str(Path(db_path).expanduser())
        parent = Path(self.db_path).parent
        if str(parent) not in ("", "."):
            parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()
        # SMS orders share the same durable database boundary as jobs and
        # attempts.  Import lazily to keep the two state-machine modules
        # independent during bootstrap and tests.
        from core.sms_orders import SmsOrderStore
        self.sms_orders = SmsOrderStore(self.db_path)

    def connect(self):
        conn = sqlite3.connect(self.db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=15000")
        return conn

    def _ensure_schema(self):
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    requested_engine TEXT NOT NULL DEFAULT '',
                    requested_count INTEGER NOT NULL DEFAULT 0,
                    state TEXT NOT NULL DEFAULT 'pending',
                    retry_policy TEXT NOT NULL DEFAULT '{}',
                    metadata TEXT NOT NULL DEFAULT '{}',
                    terminal_error_code TEXT NOT NULL DEFAULT '',
                    summary TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS attempts (
                    attempt_id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
                    ordinal INTEGER NOT NULL,
                    account_id INTEGER,
                    subject TEXT NOT NULL DEFAULT '',
                    strategy TEXT NOT NULL DEFAULT '',
                    engine TEXT NOT NULL DEFAULT '',
                    profile_id TEXT NOT NULL DEFAULT '',
                    proxy_label TEXT NOT NULL DEFAULT '',
                    worker_id TEXT NOT NULL DEFAULT '',
                    state TEXT NOT NULL DEFAULT 'running',
                    outcome TEXT NOT NULL DEFAULT '',
                    error_code TEXT NOT NULL DEFAULT '',
                    retry_decision TEXT NOT NULL DEFAULT '',
                    cooldown_seconds INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TEXT,
                    metadata TEXT NOT NULL DEFAULT '{}',
                    result_metadata TEXT NOT NULL DEFAULT '{}',
                    started_at TEXT NOT NULL,
                    heartbeat_at TEXT NOT NULL,
                    finished_at TEXT,
                    UNIQUE(job_id, ordinal)
                );
                """
            )

            # ``CREATE TABLE IF NOT EXISTS`` does not evolve an existing
            # database.  Older deployments may have created only a small
            # subset of these columns, so add missing fields before creating
            # indexes or reading them below.  Every change is additive and
            # therefore safe for SQLite files that already contain rows.
            job_columns = {
                "job_id": "TEXT",
                "kind": "TEXT NOT NULL DEFAULT 'unknown'",
                "requested_engine": "TEXT NOT NULL DEFAULT ''",
                "requested_count": "INTEGER NOT NULL DEFAULT 0",
                "state": "TEXT NOT NULL DEFAULT 'pending'",
                "retry_policy": "TEXT NOT NULL DEFAULT '{}'",
                "metadata": "TEXT NOT NULL DEFAULT '{}'",
                "terminal_error_code": "TEXT NOT NULL DEFAULT ''",
                "summary": "TEXT NOT NULL DEFAULT '{}'",
                "created_at": "TEXT NOT NULL DEFAULT ''",
                "started_at": "TEXT",
                "finished_at": "TEXT",
                "updated_at": "TEXT NOT NULL DEFAULT ''",
            }
            attempt_columns = {
                "attempt_id": "TEXT",
                "job_id": "TEXT NOT NULL DEFAULT ''",
                "ordinal": "INTEGER NOT NULL DEFAULT 1",
                "account_id": "INTEGER",
                "subject": "TEXT NOT NULL DEFAULT ''",
                "strategy": "TEXT NOT NULL DEFAULT ''",
                "engine": "TEXT NOT NULL DEFAULT ''",
                "profile_id": "TEXT NOT NULL DEFAULT ''",
                "proxy_label": "TEXT NOT NULL DEFAULT ''",
                "worker_id": "TEXT NOT NULL DEFAULT ''",
                "state": "TEXT NOT NULL DEFAULT 'running'",
                "outcome": "TEXT NOT NULL DEFAULT ''",
                "error_code": "TEXT NOT NULL DEFAULT ''",
                "retry_decision": "TEXT NOT NULL DEFAULT ''",
                "cooldown_seconds": "INTEGER NOT NULL DEFAULT 0",
                "next_attempt_at": "TEXT",
                "metadata": "TEXT NOT NULL DEFAULT '{}'",
                "result_metadata": "TEXT NOT NULL DEFAULT '{}'",
                "started_at": "TEXT NOT NULL DEFAULT ''",
                "heartbeat_at": "TEXT NOT NULL DEFAULT ''",
                "finished_at": "TEXT",
            }

            def add_missing(table: str, columns: Dict[str, str]) -> None:
                existing = {
                    row[1] for row in conn.execute("PRAGMA table_info(%s)" % table)
                }
                for name, definition in columns.items():
                    if name not in existing:
                        conn.execute(
                            "ALTER TABLE %s ADD COLUMN %s %s" %
                            (table, name, definition)
                        )

            add_missing("jobs", job_columns)
            add_missing("attempts", attempt_columns)

            def table_without_rowid(table: str) -> bool:
                row = conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                    (table,),
                ).fetchone()
                return bool(row and "WITHOUT ROWID" in str(row[0] or "").upper())

            # SQLite cannot change a column's declared affinity with ALTER
            # TABLE.  Some early ledgers declared ``ordinal`` as TEXT, which
            # means even an integer write is stored as text forever.  Rebuild
            # just that legacy table into the canonical schema so the durable
            # file, not only the read projection, carries an INTEGER ordinal.
            ordinal_info = next(
                (
                    row for row in conn.execute("PRAGMA table_info(attempts)")
                    if row[1] == "ordinal"
                ),
                None,
            )
            ordinal_declared = str((ordinal_info[2] if ordinal_info else "") or "")
            if "INT" not in ordinal_declared.upper():
                legacy_without_rowid = table_without_rowid("attempts")
                if legacy_without_rowid:
                    legacy_rows = conn.execute(
                        "SELECT * FROM attempts ORDER BY attempt_id"
                    ).fetchall()
                else:
                    legacy_rows = conn.execute(
                        "SELECT rowid, * FROM attempts ORDER BY rowid"
                    ).fetchall()

                # Preserve explicit user indexes where possible.  SQLite's
                # implicit primary/unique indexes are recreated by the table
                # definition and are deliberately omitted here.
                legacy_indexes = []
                for index_row in conn.execute("PRAGMA index_list(attempts)").fetchall():
                    index_name = str(index_row[1])
                    index_origin = str(index_row[3]) if len(index_row) > 3 else "c"
                    index_sql_row = conn.execute(
                        "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
                        (index_name,),
                    ).fetchone()
                    index_sql = index_sql_row[0] if index_sql_row else None
                    if index_sql and index_origin == "c":
                        legacy_indexes.append(index_sql)
                        conn.execute(
                            "DROP INDEX \"%s\"" % index_name.replace('"', '""')
                        )

                migration_stamp = _now()
                canonical_columns = (
                    "attempt_id", "job_id", "ordinal", "account_id", "subject",
                    "strategy", "engine", "profile_id", "proxy_label", "worker_id",
                    "state", "outcome", "error_code", "retry_decision",
                    "cooldown_seconds", "next_attempt_at", "metadata", "result_metadata", "started_at",
                    "heartbeat_at", "finished_at",
                )
                canonical_rows = []
                next_ordinals = {}
                seen_attempt_ids = set()
                for row in legacy_rows:
                    raw = dict(row)
                    job_key = str(raw.get("job_id") or "")
                    ordinal = _legacy_int(raw.get("ordinal"), minimum=1)
                    prior = next_ordinals.get(job_key, 0)
                    if ordinal is None or ordinal <= prior:
                        ordinal = prior + 1
                    next_ordinals[job_key] = ordinal

                    attempt_key = str(raw.get("attempt_id") or "").strip()
                    if not _ID.fullmatch(attempt_key) or attempt_key in seen_attempt_ids:
                        attempt_key = "attempt-" + uuid.uuid4().hex
                    seen_attempt_ids.add(attempt_key)

                    def text_value(name: str) -> str:
                        value = raw.get(name)
                        return "" if value is None else str(value)

                    account_id = _legacy_int(raw.get("account_id"), minimum=0)
                    cooldown = _legacy_int(raw.get("cooldown_seconds"), minimum=0)
                    canonical_rows.append(
                        (
                            attempt_key,
                            job_key,
                            ordinal,
                            account_id,
                            text_value("subject"),
                            text_value("strategy"),
                            text_value("engine"),
                            text_value("profile_id"),
                            text_value("proxy_label"),
                            text_value("worker_id"),
                            text_value("state") or "running",
                            text_value("outcome"),
                            text_value("error_code"),
                            text_value("retry_decision"),
                            cooldown if cooldown is not None else 0,
                            raw.get("next_attempt_at"),
                            raw.get("metadata") or "{}",
                            raw.get("result_metadata") or "{}",
                            raw.get("started_at") or migration_stamp,
                            raw.get("heartbeat_at") or migration_stamp,
                            raw.get("finished_at"),
                        )
                    )

                rebuilt_name = "attempts__rebuild_" + uuid.uuid4().hex
                conn.execute(
                    "ALTER TABLE attempts RENAME TO \"%s\"" % rebuilt_name
                )
                conn.execute(
                    """
                    CREATE TABLE attempts (
                        attempt_id TEXT PRIMARY KEY,
                        job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
                        ordinal INTEGER NOT NULL,
                        account_id INTEGER,
                        subject TEXT NOT NULL DEFAULT '',
                        strategy TEXT NOT NULL DEFAULT '',
                        engine TEXT NOT NULL DEFAULT '',
                        profile_id TEXT NOT NULL DEFAULT '',
                        proxy_label TEXT NOT NULL DEFAULT '',
                        worker_id TEXT NOT NULL DEFAULT '',
                        state TEXT NOT NULL DEFAULT 'running',
                        outcome TEXT NOT NULL DEFAULT '',
                        error_code TEXT NOT NULL DEFAULT '',
                        retry_decision TEXT NOT NULL DEFAULT '',
                        cooldown_seconds INTEGER NOT NULL DEFAULT 0,
                        next_attempt_at TEXT,
                        metadata TEXT NOT NULL DEFAULT '{}',
                        result_metadata TEXT NOT NULL DEFAULT '{}',
                        started_at TEXT NOT NULL,
                        heartbeat_at TEXT NOT NULL,
                        finished_at TEXT,
                        UNIQUE(job_id, ordinal)
                    )
                    """
                )
                placeholders = ", ".join("?" for _ in canonical_columns)
                conn.executemany(
                    "INSERT INTO attempts (%s) VALUES (%s)" %
                    (", ".join(canonical_columns), placeholders),
                    canonical_rows,
                )
                conn.execute("DROP TABLE \"%s\"" % rebuilt_name)
                for index_sql in legacy_indexes:
                    try:
                        conn.execute(index_sql)
                    except sqlite3.Error:
                        # The standard indexes below are authoritative; a
                        # stale custom index should not prevent startup.
                        pass

            # A few early development databases did not declare the primary
            # identifiers as columns at all.  SQLite cannot add a primary key
            # with ALTER TABLE, so backfill stable identifiers and enforce
            # uniqueness with indexes after the additive migration.
            for table, key_column, prefix in (
                ("jobs", "job_id", "job-"),
                ("attempts", "attempt_id", "attempt-"),
            ):
                seen = set()
                without_rowid = table_without_rowid(table)
                if without_rowid:
                    rows = conn.execute(
                        "SELECT %s FROM %s ORDER BY %s" %
                        (key_column, table, key_column)
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT rowid, %s FROM %s ORDER BY rowid" %
                        (key_column, table)
                    ).fetchall()
                for row in rows:
                    value = str(row[key_column] or "").strip()
                    if not _ID.fullmatch(value) or value in seen:
                        value = prefix + uuid.uuid4().hex
                        if without_rowid:
                            conn.execute(
                                "UPDATE %s SET %s=? WHERE %s=?" %
                                (table, key_column, key_column),
                                (value, row[key_column]),
                            )
                        else:
                            conn.execute(
                                "UPDATE %s SET %s=? WHERE rowid=?" %
                                (table, key_column), (value, row["rowid"])
                            )
                    seen.add(value)

            # Normalize null/empty legacy values that became required in the
            # current schema.  Use one timestamp for rows that predate the
            # durable ledger rather than leaving them impossible to recover.
            stamp = _now()
            conn.execute("UPDATE jobs SET kind='unknown' WHERE kind IS NULL OR kind='' ")
            conn.execute("UPDATE jobs SET state='pending' WHERE state IS NULL OR state='' ")
            # SQLite is dynamically typed, so legacy INTEGER columns can still
            # contain strings/floats.  Canonicalize every scalar before the
            # scheduler or retry engine reads it.
            for row in conn.execute(
                "SELECT job_id, requested_count, state, created_at, updated_at, "
                "started_at, finished_at FROM jobs"
            ).fetchall():
                updates = {}
                requested_count = _legacy_int(row["requested_count"], minimum=0)
                if requested_count is None:
                    requested_count = 0
                if row["requested_count"] != requested_count:
                    updates["requested_count"] = requested_count
                if str(row["state"] or "").strip().lower() not in _JOB_STATES:
                    updates["state"] = "pending"
                for field in ("created_at", "updated_at", "started_at", "finished_at"):
                    raw = row[field]
                    if raw and _parse_time(raw) is None:
                        updates[field] = None if field in ("started_at", "finished_at") else stamp
                if updates:
                    assignments = ", ".join(key + "=?" for key in updates)
                    conn.execute(
                        "UPDATE jobs SET " + assignments + " WHERE job_id=?",
                        (*updates.values(), row["job_id"]),
                    )
            conn.execute("UPDATE jobs SET created_at=? WHERE created_at IS NULL OR created_at='' ", (stamp,))
            conn.execute("UPDATE jobs SET updated_at=COALESCE(NULLIF(updated_at, ''), created_at, ?) ", (stamp,))
            conn.execute("UPDATE attempts SET state='running' WHERE state IS NULL OR state='' ")
            for row in conn.execute(
                "SELECT attempt_id, account_id, cooldown_seconds, next_attempt_at, state, outcome, "
                "retry_decision, started_at, heartbeat_at, finished_at FROM attempts"
            ).fetchall():
                updates = {}
                account_id = _legacy_int(row["account_id"], minimum=0)
                if row["account_id"] is not None and row["account_id"] != account_id:
                    updates["account_id"] = account_id
                cooldown = _legacy_int(row["cooldown_seconds"], minimum=0)
                if cooldown is None:
                    cooldown = 0
                if row["cooldown_seconds"] != cooldown:
                    updates["cooldown_seconds"] = cooldown
                next_attempt_at = row["next_attempt_at"]
                if next_attempt_at and _parse_time(next_attempt_at) is None:
                    updates["next_attempt_at"] = None
                state = str(row["state"] or "").strip().lower()
                if state not in _ATTEMPT_STATES:
                    updates["state"] = "running"
                outcome = str(row["outcome"] or "").strip().lower()
                if outcome and outcome not in _ATTEMPT_STATES:
                    updates["outcome"] = ""
                decision = str(row["retry_decision"] or "").strip().lower()
                if decision not in ("", "retry", "stop", "pending"):
                    updates["retry_decision"] = ""
                    decision = ""
                # A claimed retry moves from ``retry`` to ``pending`` while
                # retaining its original deadline. Stale-claim recovery uses
                # that durable timestamp after a process restart.
                if decision not in ("retry", "pending") and next_attempt_at:
                    updates["next_attempt_at"] = None
                for field in ("started_at", "heartbeat_at", "finished_at"):
                    raw = row[field]
                    if raw and _parse_time(raw) is None:
                        updates[field] = None if field == "finished_at" else stamp
                if updates:
                    assignments = ", ".join(key + "=?" for key in updates)
                    conn.execute(
                        "UPDATE attempts SET " + assignments + " WHERE attempt_id=?",
                        (*updates.values(), row["attempt_id"]),
                    )
            conn.execute("UPDATE attempts SET started_at=? WHERE started_at IS NULL OR started_at='' ", (stamp,))
            conn.execute("UPDATE attempts SET heartbeat_at=COALESCE(NULLIF(heartbeat_at, ''), started_at, ?) ", (stamp,))

            conn.execute(
                "CREATE INDEX IF NOT EXISTS jobs_state_updated_idx "
                "ON jobs(state, updated_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS attempts_job_ordinal_idx "
                "ON attempts(job_id, ordinal)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS attempts_state_heartbeat_idx "
                "ON attempts(state, heartbeat_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS attempts_retry_due_idx "
                "ON attempts(retry_decision, next_attempt_at)"
            )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS jobs_job_id_unique_idx "
                "ON jobs(job_id)"
            )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS attempts_attempt_id_unique_idx "
                "ON attempts(attempt_id)"
            )

            # Enforce the ordinal claim invariant on legacy tables that did
            # not carry the table-level UNIQUE(job_id, ordinal) constraint.
            # Duplicate historical rows are retained but assigned fresh
            # ordinals before the unique index is created.
            attempts_without_rowid = table_without_rowid("attempts")
            if attempts_without_rowid:
                rows = conn.execute(
                    "SELECT attempt_id, job_id, ordinal FROM attempts "
                    "ORDER BY job_id, ordinal, attempt_id"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT rowid, job_id, ordinal FROM attempts ORDER BY job_id, ordinal, rowid"
                ).fetchall()
            next_ordinals = {}
            for row in rows:
                job_key = str(row["job_id"] or "")
                ordinal = _legacy_int(row["ordinal"], minimum=1)
                next_value = next_ordinals.get(job_key, 0)
                if ordinal is None or ordinal <= next_value:
                    ordinal = next_value + 1
                # Always write the canonical integer.  A malformed first row
                # otherwise remains TEXT "not-a-number" because its fallback
                # value (1) does not collide with an earlier ordinal.
                if row["ordinal"] != ordinal:
                    if attempts_without_rowid:
                        conn.execute(
                            "UPDATE attempts SET ordinal=? WHERE attempt_id=?",
                            (ordinal, row["attempt_id"]),
                        )
                    else:
                        conn.execute(
                            "UPDATE attempts SET ordinal=? WHERE rowid=?",
                            (ordinal, row["rowid"]),
                        )
                next_ordinals[job_key] = ordinal
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS attempts_job_ordinal_unique_idx "
                "ON attempts(job_id, ordinal)"
            )

            # Scrub rows created by older versions that accepted arbitrary
            # exception/provider text in the error-code columns.  The read
            # projection also normalizes defensively, while this rewrite keeps
            # the raw SQLite file clean after a restart.
            for row in conn.execute(
                "SELECT job_id, terminal_error_code FROM jobs"
            ).fetchall():
                safe_code = normalize_error_code(
                    row["terminal_error_code"], default="", allow_empty=True
                )
                if safe_code != (row["terminal_error_code"] or ""):
                    conn.execute(
                        "UPDATE jobs SET terminal_error_code=? WHERE job_id=?",
                        (safe_code, row["job_id"]),
                    )
            for row in conn.execute(
                "SELECT attempt_id, error_code FROM attempts"
            ).fetchall():
                safe_code = normalize_error_code(
                    row["error_code"], default="", allow_empty=True
                )
                if safe_code != (row["error_code"] or ""):
                    conn.execute(
                        "UPDATE attempts SET error_code=? WHERE attempt_id=?",
                        (safe_code, row["attempt_id"]),
                    )

            # Older rows may also contain credentials in JSON metadata even
            # when their error-code columns look valid.  Rewrite all JSON
            # projections through the same sanitizer used for new writes.
            for table, key_column, json_columns in (
                ("jobs", "job_id", ("retry_policy", "metadata", "summary")),
                ("attempts", "attempt_id", ("metadata", "result_metadata")),
            ):
                rows = conn.execute(
                    "SELECT %s, %s FROM %s" %
                    (key_column, ", ".join(json_columns), table)
                ).fetchall()
                for row in rows:
                    updates = []
                    values = []
                    for column in json_columns:
                        safe_value = _safe_json(_load_json(row[column]))
                        if safe_value != (row[column] or ""):
                            updates.append("%s=?" % column)
                            values.append(safe_value)
                    if updates:
                        values.append(row[key_column])
                        conn.execute(
                            "UPDATE %s SET %s WHERE %s=?" %
                            (table, ", ".join(updates), key_column),
                            tuple(values),
                        )

    @staticmethod
    def _job_row(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
        if row is None:
            return None
        result = dict(row)
        result["terminal_error_code"] = normalize_error_code(
            result.get("terminal_error_code"), default="", allow_empty=True
        )
        for key in ("retry_policy", "metadata", "summary"):
            result[key] = _load_json(result.get(key))
        return result

    @staticmethod
    def _attempt_row(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
        if row is None:
            return None
        result = dict(row)
        # SQLite preserves the declared affinity of legacy tables.  Expose a
        # canonical integer even when an old database still contains a text
        # ordinal; startup migration below rewrites the physical column too.
        ordinal = _legacy_int(result.get("ordinal"), minimum=1)
        result["ordinal"] = ordinal if ordinal is not None else 1
        account_id = _legacy_int(result.get("account_id"), minimum=0)
        result["account_id"] = account_id
        cooldown = _legacy_int(result.get("cooldown_seconds"), minimum=0)
        result["cooldown_seconds"] = cooldown if cooldown is not None else 0
        next_attempt_at = result.get("next_attempt_at")
        result["next_attempt_at"] = (
            _parse_time(next_attempt_at).isoformat()
            if _parse_time(next_attempt_at) is not None else None
        )
        result["error_code"] = normalize_error_code(
            result.get("error_code"), default="", allow_empty=True
        )
        for key in ("metadata", "result_metadata"):
            result[key] = _load_json(result.get(key))
        return result

    def create_job(self, kind: str, *, requested_count: int = 0,
                   requested_engine: str = "", retry_policy: Optional[Dict[str, Any]] = None,
                   metadata: Optional[Dict[str, Any]] = None,
                   job_id: Optional[str] = None) -> Dict[str, Any]:
        kind = _bounded_text(kind, "kind", allow_empty=False)
        requested_engine = _bounded_text(
            requested_engine, "requested_engine"
        )
        requested_count = _strict_int(
            requested_count, "requested_count", minimum=0
        )
        job_id = _id(job_id, "job-")
        created = _now()
        try:
            with self.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    """INSERT INTO jobs
                       (job_id, kind, requested_engine, requested_count, state,
                        retry_policy, metadata, created_at, updated_at)
                       VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?)""",
                    (job_id, kind, requested_engine, requested_count,
                     _safe_json(retry_policy or {}), _safe_json(metadata or {}),
                     created, created),
                )
                row = conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
                conn.commit()
                return self._job_row(row)
        except sqlite3.IntegrityError as exc:
            raise ValueError("job_id already exists") from exc

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        job_id = _existing_id(job_id, "job_id")
        with self.connect() as conn:
            return self._job_row(conn.execute(
                "SELECT * FROM jobs WHERE job_id=?", (job_id,)
            ).fetchone())

    def list_jobs(self, *, state: Optional[str] = None, limit: int = 100) -> list:
        limit = min(_strict_int(limit, "limit", minimum=1), 1000)
        with self.connect() as conn:
            if state is None:
                rows = conn.execute(
                    "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
                ).fetchall()
            else:
                if state not in _JOB_STATES:
                    raise ValueError("invalid job state")
                rows = conn.execute(
                    "SELECT * FROM jobs WHERE state=? ORDER BY created_at DESC LIMIT ?",
                    (state, limit),
                ).fetchall()
            return [self._job_row(row) for row in rows]

    def start_attempt(self, job_id: str, *, ordinal: Optional[int] = None,
                      account_id: Optional[int] = None, subject: str = "",
                      strategy: str = "", engine: str = "", profile_id: str = "",
                      proxy: Any = None, proxy_label: str = "", worker_id: str = "",
                      metadata: Optional[Dict[str, Any]] = None,
                      attempt_id: Optional[str] = None) -> Dict[str, Any]:
        job_id = _existing_id(job_id, "job_id")
        attempt_id = _id(attempt_id, "attempt-")
        if account_id is not None:
            account_id = _strict_int(account_id, "account_id", minimum=0)
        if ordinal is not None:
            ordinal = _strict_int(ordinal, "ordinal", minimum=1)
        now = _now()
        if not proxy_label and proxy:
            proxy_label = safe_proxy_label(proxy)
        subject = _bounded_text(subject, "subject")
        strategy = _safe_strategy(strategy)
        engine = _bounded_text(engine, "engine")
        profile_id = _bounded_text(profile_id, "profile_id")
        worker_id = _bounded_text(worker_id, "worker_id")
        proxy_label = _bounded_text(proxy_label, "proxy_label")
        try:
            with self.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                job = conn.execute(
                    "SELECT state FROM jobs WHERE job_id=?", (job_id,)
                ).fetchone()
                if job is None:
                    raise KeyError(job_id)
                if job[0] in _TERMINAL_JOB_STATES:
                    raise ValueError("cannot start an attempt for a terminal job")
                if ordinal is None:
                    row = conn.execute(
                        "SELECT COALESCE(MAX(ordinal), 0) + 1 FROM attempts WHERE job_id=?",
                        (job_id,),
                    ).fetchone()
                    ordinal = int(row[0])
                conn.execute(
                    """INSERT INTO attempts
                       (attempt_id, job_id, ordinal, account_id, subject, strategy,
                        engine, profile_id, proxy_label, worker_id, state, metadata,
                        started_at, heartbeat_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'running', ?, ?, ?)""",
                    (attempt_id, job_id, ordinal, account_id, subject,
                     strategy, engine, profile_id, proxy_label, worker_id,
                     _safe_json(metadata or {}), now, now),
                )
                conn.execute(
                    """UPDATE jobs SET state='running', started_at=COALESCE(started_at, ?),
                       updated_at=? WHERE job_id=?""", (now, now, job_id)
                )
                row = conn.execute(
                    "SELECT * FROM attempts WHERE attempt_id=?", (attempt_id,)
                ).fetchone()
                conn.commit()
                return self._attempt_row(row)
        except sqlite3.IntegrityError as exc:
            raise ValueError("attempt ordinal or id already exists") from exc

    begin_attempt = start_attempt

    def heartbeat_attempt(self, attempt_id: str, *, worker_id: Optional[str] = None) -> bool:
        attempt_id = _existing_id(attempt_id, "attempt_id")
        if worker_id is not None:
            worker_id = _bounded_text(worker_id, "worker_id", allow_empty=False)
        now = _now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if worker_id is None:
                cursor = conn.execute(
                    "UPDATE attempts SET heartbeat_at=? WHERE attempt_id=? AND state='running'",
                    (now, attempt_id),
                )
            else:
                cursor = conn.execute(
                    """UPDATE attempts SET heartbeat_at=?
                       WHERE attempt_id=? AND state='running' AND worker_id=?""",
                    (now, attempt_id, worker_id),
                )
            conn.commit()
            return cursor.rowcount > 0

    def finish_attempt(self, attempt_id: str, *, outcome: Any,
                       error_code: str = "", retry_decision: str = "",
                       cooldown_seconds: int = 0,
                       result: Optional[Dict[str, Any]] = None,
                       state: Optional[str] = None,
                       worker_id: Optional[str] = None) -> Dict[str, Any]:
        attempt_id = _existing_id(attempt_id, "attempt_id")
        if worker_id is not None:
            worker_id = _bounded_text(worker_id, "worker_id", allow_empty=False)
        derived_state = _outcome_state(outcome)
        if state is None:
            final_state = derived_state
        else:
            final_state = _bounded_text(state, "state", allow_empty=False)
            # Creation cancellation historically supplied
            # ``outcome='cancelled', state='interrupted'`` to preserve resume
            # semantics.  Keep that one explicit compatibility transition;
            # all other state/outcome mismatches are ambiguous and rejected.
            if final_state != derived_state and not (
                derived_state == "cancelled" and final_state == "interrupted"
            ):
                raise ValueError("attempt state and outcome conflict")
        if final_state not in _ATTEMPT_STATES - {"running"}:
            raise ValueError("invalid attempt state")
        cooldown_seconds = _strict_int(
            cooldown_seconds, "cooldown_seconds", minimum=0
        )
        decision = str(retry_decision or "").strip().lower()
        if decision not in ("", "retry", "stop", "pending"):
            raise ValueError("invalid retry decision")
        if not decision:
            decision = "stop"
        if decision == "retry" and final_state not in ("failed", "interrupted"):
            raise ValueError("retry decision requires a retryable attempt state")
        normalized_error = normalize_error_code(
            error_code,
            default={
                "succeeded": "",
                "cancelled": "cancelled",
                "interrupted": "interrupted",
                "skipped": "blocked",
                "failed": "error",
            }.get(final_state, "error"),
            allow_empty=(final_state == "succeeded"),
        )
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()
        next_attempt_at = (
            (now_dt + timedelta(seconds=cooldown_seconds)).isoformat()
            if decision == "retry" else None
        )
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM attempts WHERE attempt_id=?", (attempt_id,)
            ).fetchone()
            if row is None:
                raise KeyError(attempt_id)
            # A caller that supplies an owner token must still own the running
            # attempt.  This check is intentionally performed before the
            # idempotent terminal callback path: a stale worker must not be
            # able to replay another worker's terminal transition.
            if worker_id is not None and row["worker_id"] != worker_id:
                raise ValueError("attempt ownership mismatch")
            if row["state"] != "running":
                # Repeated callbacks are harmless when they describe exactly
                # the same terminal transition.  A conflicting second write
                # remains an explicit protocol error.
                existing_result = _safe_json(_load_json(row["result_metadata"]))
                requested_result = _safe_json(result or {})
                if (
                    row["state"] == final_state
                    and row["error_code"] == normalized_error
                    and row["retry_decision"] == decision
                    and int(row["cooldown_seconds"] or 0) == cooldown_seconds
                    and existing_result == requested_result
                ):
                    conn.commit()
                    return self._attempt_row(row)
                raise ValueError("attempt is already finalized")
            conn.execute(
                """UPDATE attempts SET state=?, outcome=?, error_code=?,
                   retry_decision=?, cooldown_seconds=?, next_attempt_at=?, result_metadata=?, finished_at=?,
                   heartbeat_at=? WHERE attempt_id=?""",
                (final_state, final_state, normalized_error, decision,
                 cooldown_seconds, next_attempt_at, _safe_json(result or {}), now, now,
                 attempt_id),
            )
            conn.execute(
                "UPDATE jobs SET updated_at=? WHERE job_id=?", (now, row["job_id"])
            )
            updated = conn.execute(
                "SELECT * FROM attempts WHERE attempt_id=?", (attempt_id,)
            ).fetchone()
            conn.commit()
            return self._attempt_row(updated)

    def finish_job(self, job_id: str, *, state: str,
                   terminal_error_code: str = "",
                   summary: Optional[Dict[str, Any]] = None,
                   worker_id: Optional[str] = None) -> Dict[str, Any]:
        return self.finalize_job(
            job_id, state=state, error_code=terminal_error_code, summary=summary,
            worker_id=worker_id,
        )

    def finalize_job(self, job_id: str, *, state: str,
                     error_code: str = "", summary: Optional[Dict[str, Any]] = None,
                     worker_id: Optional[str] = None) -> Dict[str, Any]:
        """Finalize a job and close any attempt still owned by its worker.

        Cancellation and worker loss must be reflected in the same durable
        transaction as the user-visible job state.  ``interrupted`` remains
        resumable; other states are terminal and reject later attempts.
        """
        return self._finalize_job(
            job_id, state=state, error_code=error_code, summary=summary,
            worker_id=worker_id, supervisor_cleanup_verified=None,
        )

    def supervisor_finalize_job(self, job_id: str, *, state: str,
                                cleanup_verified: bool, error_code: str = "",
                                summary: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Reconcile an abandoned job after an external cleanup observation."""
        if type(cleanup_verified) is not bool:
            raise ValueError("cleanup_verified must be a boolean")
        return self._finalize_job(
            job_id, state=state, error_code=error_code, summary=summary,
            worker_id=None, supervisor_cleanup_verified=cleanup_verified,
        )

    def _finalize_job(self, job_id: str, *, state: str, error_code: str,
                      summary: Optional[Dict[str, Any]],
                      worker_id: Optional[str],
                      supervisor_cleanup_verified: Optional[bool]) -> Dict[str, Any]:
        job_id = _existing_id(job_id, "job_id")
        if not isinstance(state, str):
            raise ValueError("state must be text")
        state = state.strip().lower()
        if state not in {"succeeded", "failed", "cancelled", "blocked", "interrupted"}:
            raise ValueError("invalid terminal job state")
        if supervisor_cleanup_verified is False:
            if state != "failed":
                raise ValueError("unverified cleanup may only finalize failed jobs")
            if error_code and normalize_error_code(
                error_code, default="cleanup_failed", allow_empty=False
            ) != "cleanup_failed":
                raise ValueError("unverified cleanup requires cleanup_failed")
            error_code = "cleanup_failed"
        elif supervisor_cleanup_verified is True and state == "succeeded":
            raise ValueError("supervisor cleanup cannot mark a job succeeded")
        if supervisor_cleanup_verified is None:
            # Older rows use an empty owner. They remain compatible during the
            # additive migration, while every new native callback has a token.
            worker_id = current_worker_id() if worker_id is None else _bounded_text(
                worker_id, "worker_id", allow_empty=False
            )
        if not isinstance(error_code, str):
            raise ValueError("error_code must be text")
        if summary is not None and not isinstance(summary, dict):
            raise ValueError("summary must be an object")
        if state == "interrupted":
            default_error = "worker_lost"
        elif state == "cancelled":
            default_error = "cancelled"
        elif state == "blocked":
            default_error = "blocked"
        elif state == "succeeded":
            default_error = ""
        else:
            default_error = "job_failed"
        normalized_error = normalize_error_code(
            error_code, default="error", allow_empty=False
        ) if error_code else default_error
        unverified_cleanup_override = (
            supervisor_cleanup_verified is False
            and state == "failed"
            and normalized_error == "cleanup_failed"
        )
        requested_summary = _safe_json(summary or {})
        now = _now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(job_id)
            if supervisor_cleanup_verified is None:
                foreign_owner = conn.execute(
                    """SELECT attempt_id FROM attempts
                       WHERE job_id=? AND worker_id<>'' AND worker_id<>?
                         AND (state='running' OR retry_decision='pending')
                       LIMIT 1""",
                    (job_id, worker_id),
                ).fetchone()
                if foreign_owner is not None:
                    raise ValueError("job ownership mismatch")
            if (
                row["state"] in _TERMINAL_JOB_STATES
                and row["state"] != state
                and not unverified_cleanup_override
            ):
                raise ValueError("job is already finalized")
            if row["state"] == state and row["finished_at"] and not unverified_cleanup_override:
                existing_code = normalize_error_code(
                    row["terminal_error_code"], default="", allow_empty=True
                )
                existing_summary = _safe_json(_load_json(row["summary"]))
                if existing_code == normalized_error and existing_summary == requested_summary:
                    conn.commit()
                    return self._job_row(row)
                raise ValueError("job is already finalized")
            if state == "interrupted":
                attempt_state = "interrupted"
                retry_decision = "retry"
                default_error = "worker_lost"
            elif state == "cancelled":
                attempt_state = "cancelled"
                retry_decision = "stop"
                default_error = "cancelled"
            elif state == "blocked":
                attempt_state = "skipped"
                retry_decision = "stop"
                default_error = "blocked"
            elif state == "succeeded":
                attempt_state = "failed"
                retry_decision = "stop"
                default_error = "job_finalized"
            else:
                attempt_state = "failed"
                retry_decision = "stop"
                default_error = "job_failed"
            if state != "interrupted":
                # A terminal job cannot leave a failed/pending retry behind:
                # a later scheduler process must never resurrect work after
                # cancellation or finalization.  ``interrupted`` is the one
                # resumable state and intentionally preserves its schedule.
                conn.execute(
                    """UPDATE attempts SET retry_decision='stop', next_attempt_at=NULL
                       WHERE job_id=? AND state IN ('failed', 'interrupted')
                         AND retry_decision IN ('retry', 'pending')""",
                    (job_id,),
                )
            conn.execute(
                """UPDATE attempts SET state=?, outcome=?, error_code=?,
                   retry_decision=?, next_attempt_at=NULL, finished_at=?, heartbeat_at=?
                   WHERE job_id=? AND state='running'""",
                (attempt_state, attempt_state, normalized_error, retry_decision,
                 now, now, job_id),
            )
            conn.execute(
                """UPDATE jobs SET state=?, terminal_error_code=?, summary=?,
                   finished_at=COALESCE(finished_at, ?), updated_at=? WHERE job_id=?""",
                (state, normalized_error, requested_summary,
                 now, now, job_id),
            )
            updated = conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            conn.commit()
            return self._job_row(updated)

    def get_attempt(self, attempt_id: str) -> Optional[Dict[str, Any]]:
        attempt_id = _existing_id(attempt_id, "attempt_id")
        with self.connect() as conn:
            return self._attempt_row(conn.execute(
                "SELECT * FROM attempts WHERE attempt_id=?", (attempt_id,)
            ).fetchone())

    def list_attempts(self, job_id: str) -> list:
        job_id = _existing_id(job_id, "job_id")
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM attempts WHERE job_id=? ORDER BY ordinal", (job_id,)
            ).fetchall()
            return [self._attempt_row(row) for row in rows]

    def list_retry_attempts(self, job_id: str, *, account_id: Optional[int] = None,
                            subject: Optional[str] = None) -> list:
        """Return resumable retry rows for one logical operation subject."""
        job_id = _existing_id(job_id, "job_id")
        clauses = ["job_id=?", "retry_decision='retry'",
                   "state IN ('failed', 'interrupted')"]
        values = [job_id]
        if account_id is not None:
            clauses.append("account_id=?")
            values.append(_strict_int(account_id, "account_id", minimum=0))
        elif subject:
            clauses.append("subject=?")
            values.append(_bounded_text(subject, "subject", allow_empty=False))
        else:
            raise ValueError("account_id or subject is required")
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM attempts WHERE " + " AND ".join(clauses) +
                " ORDER BY ordinal DESC",
                tuple(values),
            ).fetchall()
            return [self._attempt_row(row) for row in rows]

    def get_latest_attempt(self, job_id: str, *, account_id: Optional[int] = None,
                           subject: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Return the latest durable fact for one logical operation subject."""
        job_id = _existing_id(job_id, "job_id")
        clauses = ["job_id=?"]
        values = [job_id]
        if account_id is not None:
            clauses.append("account_id=?")
            values.append(_strict_int(account_id, "account_id", minimum=0))
        elif subject:
            clauses.append("subject=?")
            values.append(_bounded_text(subject, "subject", allow_empty=False))
        else:
            raise ValueError("account_id or subject is required")
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM attempts WHERE " + " AND ".join(clauses) +
                " ORDER BY ordinal DESC LIMIT 1",
                tuple(values),
            ).fetchone()
            return self._attempt_row(row)

    def list_due_attempts(self, *, now: Optional[str] = None, limit: int = 100) -> list:
        """Return retryable attempts whose durable cooldown has elapsed."""
        limit = min(_strict_int(limit, "limit", minimum=1), 1000)
        parsed_now, _stamp = _required_time(now, "now")
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT * FROM attempts
                   WHERE retry_decision='retry'
                     AND state IN ('failed', 'interrupted')
                   ORDER BY next_attempt_at, started_at
                   """
            ).fetchall()
        due = []
        for row in rows:
            due_at = _parse_time(row["next_attempt_at"])
            # A malformed or missing schedule is never treated as immediately
            # due.  It stays visible to repair tooling but cannot trigger a
            # provider/browser retry by accident.
            if due_at is None or due_at > parsed_now:
                continue
            due.append(self._attempt_row(row))
            if len(due) >= limit:
                break
        return due

    def retry_is_due(self, attempt_id: str, *, now: Optional[str] = None) -> bool:
        """Check one persisted retry schedule without changing ownership."""
        attempt_id = _existing_id(attempt_id, "attempt_id")
        parsed_now, _stamp = _required_time(now, "now")
        with self.connect() as conn:
            row = conn.execute(
                """SELECT retry_decision, state, next_attempt_at FROM attempts
                   WHERE attempt_id=?""", (attempt_id,)
            ).fetchone()
        if row is None:
            raise KeyError(attempt_id)
        due_at = _parse_time(row["next_attempt_at"])
        return bool(
            row["retry_decision"] == "retry"
            and row["state"] in ("failed", "interrupted")
            and due_at is not None
            and due_at <= parsed_now
        )

    def claim_due_retry(self, attempt_id: str, *, now: Optional[str] = None,
                        worker_id: Optional[str] = None) -> Dict[str, Any]:
        """Fence a retry schedule so only one scheduler can consume it."""
        attempt_id = _existing_id(attempt_id, "attempt_id")
        if worker_id is not None:
            worker_id = _bounded_text(worker_id, "worker_id", allow_empty=False)
        parsed_now, stamp = _required_time(now, "now")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute(
                """SELECT * FROM attempts WHERE attempt_id=?""",
                (attempt_id,),
            ).fetchone()
            if current is None:
                conn.rollback()
                raise KeyError(attempt_id)
            due_at = _parse_time(current["next_attempt_at"])
            if due_at is None:
                conn.rollback()
                raise ValueError("invalid retry timestamp")
            if (
                current["retry_decision"] != "retry"
                or current["state"] not in ("failed", "interrupted")
                or due_at > parsed_now
            ):
                conn.rollback()
                raise ValueError("retry is not due or was already claimed")
            # Keep the original raw timestamp in the predicate.  The
            # immediate transaction plus this compare-and-set makes a claim
            # safe even when two schedulers opened separate connections.
            cursor = conn.execute(
                """UPDATE attempts SET retry_decision='pending', worker_id=?, heartbeat_at=?
                   WHERE attempt_id=? AND retry_decision='retry'
                     AND state IN ('failed', 'interrupted')
                     AND next_attempt_at=?""",
                (worker_id or current["worker_id"], stamp,
                 attempt_id, current["next_attempt_at"]),
            )
            if cursor.rowcount == 0:
                conn.rollback()
                raise ValueError("retry is not due or was already claimed")
            row = conn.execute(
                "SELECT * FROM attempts WHERE attempt_id=?", (attempt_id,)
            ).fetchone()
            conn.commit()
            return self._attempt_row(row)

    def count_attempts(self, job_id: str, *, account_id: Optional[int] = None,
                       subject: Optional[str] = None) -> int:
        """Count attempts for one logical subject, not the entire job."""
        job_id = _existing_id(job_id, "job_id")
        clauses = ["job_id=?"]
        values = [job_id]
        if account_id is not None:
            clauses.append("account_id=?")
            values.append(_strict_int(account_id, "account_id", minimum=0))
        elif subject:
            subject = _bounded_text(subject, "subject", allow_empty=False)
            clauses.append("subject=?")
            values.append(subject)
        else:
            return len(self.list_attempts(job_id))
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM attempts WHERE " + " AND ".join(clauses),
                tuple(values),
            ).fetchone()
            return int(row[0])

    def recover_stale(self, max_age_seconds: int = 300, *, now: Optional[str] = None) -> int:
        max_age_seconds = _strict_int(
            max_age_seconds, "max_age_seconds", minimum=1
        )
        parsed_now, stamp = _required_time(now, "now")
        cutoff = parsed_now.timestamp() - max_age_seconds
        # ISO timestamps are lexicographically sortable in this module, but a
        # numeric cutoff keeps recovery correct across offsets in old rows.
        threshold = datetime.fromtimestamp(cutoff, timezone.utc).isoformat()
        recovered = 0
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """SELECT attempt_id, job_id, state, retry_decision,
                          next_attempt_at, finished_at
                   FROM attempts
                   WHERE (state='running' AND COALESCE(heartbeat_at, started_at) < ?)
                      OR (state IN ('failed', 'interrupted')
                          AND retry_decision='pending'
                          AND COALESCE(heartbeat_at, finished_at) < ?)""",
                (threshold, threshold),
            ).fetchall()
            for row in rows:
                if row["state"] == "running":
                    cursor = conn.execute(
                        """UPDATE attempts SET state='interrupted', outcome='interrupted',
                           error_code='interrupted', retry_decision='retry', finished_at=?,
                           next_attempt_at=?, heartbeat_at=?
                           WHERE attempt_id=? AND state='running'""",
                        (stamp, stamp, stamp, row["attempt_id"]),
                    )
                else:
                    # ``pending`` is the short hand-off between a scheduler
                    # claim and the next running attempt.  If its owner died,
                    # make it claimable again; if the persisted timestamp is
                    # corrupt, fail closed instead of inventing an immediate
                    # retry.
                    due_at = _parse_time(row["next_attempt_at"])
                    if due_at is None:
                        cursor = conn.execute(
                            """UPDATE attempts SET retry_decision='stop',
                               next_attempt_at=NULL, heartbeat_at=?
                               WHERE attempt_id=? AND retry_decision='pending'""",
                            (stamp, row["attempt_id"]),
                        )
                    else:
                        cursor = conn.execute(
                            """UPDATE attempts SET retry_decision='retry', heartbeat_at=?
                               WHERE attempt_id=? AND retry_decision='pending'""",
                            (stamp, row["attempt_id"]),
                        )
                if cursor.rowcount:
                    recovered += 1
                    conn.execute(
                        """UPDATE jobs SET state=CASE WHEN state IN ('pending','running')
                           THEN 'interrupted' ELSE state END, updated_at=?
                           WHERE job_id=?""", (stamp, row["job_id"])
                    )
            conn.commit()
        return recovered

    def stats(self, job_id: str) -> Dict[str, Any]:
        job_id = _existing_id(job_id, "job_id")
        attempts = self.list_attempts(job_id)
        return {
            "job_id": job_id,
            "total_attempts": len(attempts),
            "succeeded": sum(1 for item in attempts if item["state"] == "succeeded"),
            "failed": sum(1 for item in attempts if item["state"] == "failed"),
            "interrupted": sum(1 for item in attempts if item["state"] == "interrupted"),
        }


__all__ = ["JobLedger", "default_ledger_path"]
