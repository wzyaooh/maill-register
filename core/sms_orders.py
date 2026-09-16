"""Durable SMS order lifecycle and compensation state.

The provider adapters deal with network protocols; this module owns the
recoverable local record for an order.  It deliberately stores a masked phone
label and normalized status codes only.  OTP text, API keys, and provider
payloads never belong in this table.
"""

import json
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from core.secret_safety import SAFE_ERROR_CODES, normalize_error_code, sanitize_operation_value


ORDER_STATE_ALLOCATING = "allocating"
ORDER_STATE_ACTIVE = "active"
ORDER_STATE_AWAITING_CODE = "awaiting_code"
ORDER_STATE_CODE_RECEIVED = "code_received"
ORDER_STATE_COMPLETED = "completed"
ORDER_STATE_CANCEL_PENDING = "cancel_pending"
ORDER_STATE_FINISH_PENDING = "finish_pending"
ORDER_STATE_CANCELLED = "cancelled"
ORDER_STATE_EXPIRED = "expired"
ORDER_STATE_COMPENSATION_FAILED = "compensation_failed"

ORDER_STATES = frozenset({
    ORDER_STATE_ALLOCATING,
    ORDER_STATE_ACTIVE,
    ORDER_STATE_AWAITING_CODE,
    ORDER_STATE_CODE_RECEIVED,
    ORDER_STATE_COMPLETED,
    ORDER_STATE_CANCEL_PENDING,
    ORDER_STATE_FINISH_PENDING,
    ORDER_STATE_CANCELLED,
    ORDER_STATE_EXPIRED,
    ORDER_STATE_COMPENSATION_FAILED,
})

_TERMINAL_STATES = frozenset({
    ORDER_STATE_COMPLETED,
    ORDER_STATE_CANCELLED,
    ORDER_STATE_COMPENSATION_FAILED,
})
_EXPIRABLE_STATES = frozenset({
    ORDER_STATE_ALLOCATING,
    ORDER_STATE_ACTIVE,
    ORDER_STATE_AWAITING_CODE,
})
_ORDER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_PROVIDERS = frozenset({"5sim", "sms_activate", "onlinesim", "getsms"})
# Keep a named local alias for older embedders that imported this private
# symbol, but source the vocabulary from the shared safety module.
_SAFE_ERROR_CODES = SAFE_ERROR_CODES
_SAFE_PROVIDER_STATUSES = frozenset({
    "", "ok", "error", "unsupported", "not_supported", "cancelled",
    "completed", "finished", "timeout",
})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_time(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        text = str(value).strip()
        # ``datetime.fromisoformat`` on the oldest supported Python versions
        # does not accept the RFC-3339 ``Z`` suffix.
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _required_time(value: Optional[Any], field: str = "timestamp"):
    """Return a canonical UTC timestamp or reject malformed input.

    Timestamps are used as SQLite ordering/fencing keys.  Persisting an
    arbitrary string here can make a lease appear active forever, so public
    mutation methods fail closed instead of silently storing it.
    """
    if value is None:
        parsed = datetime.now(timezone.utc)
    elif isinstance(value, datetime):
        parsed = value
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        parsed = parsed.astimezone(timezone.utc)
    elif isinstance(value, str):
        parsed = _parse_time(value)
        if parsed is None:
            raise ValueError(f"invalid {field}")
    else:
        raise ValueError(f"invalid {field}")
    return parsed, parsed.isoformat()


def _strict_int(value: Any, field: str, *, minimum: int,
                maximum: Optional[int] = None) -> int:
    """Validate scheduler parameters without implicit numeric coercion."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("%s must be an integer" % field)
    if value < minimum:
        raise ValueError("%s is out of range" % field)
    if maximum is not None and value > maximum:
        raise ValueError("%s is out of range" % field)
    return value


def _bounded_identity(value: Any, field: str, *, maximum: int,
                      allow_empty: bool = True) -> str:
    """Keep provider/job identifiers printable and bounded at the DB edge."""
    if value is None:
        value = ""
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        raise ValueError("%s must be text" % field)
    text = str(value).strip()
    if not text and not allow_empty:
        raise ValueError("%s is required" % field)
    if len(text) > maximum:
        raise ValueError("%s is too long" % field)
    if any(char.isspace() or ord(char) < 0x20 or ord(char) == 0x7f
           for char in text):
        raise ValueError("%s contains unsafe characters" % field)
    return text


def _counter_value(value: Any) -> int:
    """Parse a persisted retry counter, failing closed for malformed data."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, str) and re.fullmatch(r"[+-]?\d+", value.strip()):
        try:
            return max(0, int(value.strip()))
        except (TypeError, ValueError, OverflowError):
            return 0
    return 0


def _safe_json(value: Any) -> str:
    safe = sanitize_operation_value(value if value is not None else {})
    return json.dumps(safe, ensure_ascii=True, separators=(",", ":"), default=str)


def _load_json(value: Any) -> Any:
    if not value:
        return {}
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return decoded if isinstance(decoded, (dict, list)) else {}


def _mask_phone(phone: Any) -> str:
    """Keep only a display label with the final four digits."""
    value = re.sub(r"\D", "", str(phone or ""))
    if not value:
        return ""
    return "***" + value[-4:]


def _safe_error(value: Any, fallback: str = "") -> str:
    normalized = normalize_error_code(value, default=fallback, allow_empty=not bool(fallback))
    # Keep provider-specific SMS codes that are intentionally outside the
    # general account vocabulary, while still rejecting arbitrary payloads.
    raw = str(value or "").strip().lower()
    if raw in _SAFE_ERROR_CODES:
        return raw[:128]
    return normalized if normalized in _SAFE_ERROR_CODES else fallback


def _safe_provider_status(value: Any, fallback: str = "") -> str:
    value = str(value or "").strip().lower()
    return value if value in _SAFE_PROVIDER_STATUSES else fallback


def _order_id(value: Optional[str]) -> str:
    value = str(value or "").strip()
    if not value:
        value = "sms-" + uuid.uuid4().hex
    if not _ORDER_ID.fullmatch(value):
        raise ValueError("invalid SMS order id")
    return value


class SmsOrderStore:
    """SQLite-backed SMS order state machine.

    Each mutation uses an immediate transaction.  This makes expiration scans
    safe when more than one worker starts reconciliation at the same time.
    """

    def __init__(self, db_path: str = "data/database.db"):
        self.db_path = str(Path(db_path).expanduser())
        # Compatibility callbacks from pre-claim callers are only accepted
        # when this exact store instance acquired the current claim.  A new
        # process/store therefore cannot omit the fence token.
        self._local_claim_tokens = {}
        # A narrow compatibility path remains for older direct-store callers
        # that do not know about claim tokens.  Remember only intents created
        # by this store instance; another process cannot forge that memory-only
        # marker and must use an explicit durable claim token.
        self._legacy_intents = {}
        parent = Path(self.db_path).parent
        if str(parent) not in ("", "."):
            parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def connect(self):
        conn = sqlite3.connect(self.db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=15000")
        return conn

    def _ensure_schema(self):
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sms_orders (
                    order_id TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    provider_order_id TEXT NOT NULL,
                    phone_label TEXT NOT NULL DEFAULT '',
                    job_id TEXT NOT NULL DEFAULT '',
                    attempt_id TEXT NOT NULL DEFAULT '',
                    state TEXT NOT NULL,
                    pending_action TEXT NOT NULL DEFAULT '',
                    lease_expires_at TEXT,
                    allocated_at TEXT NOT NULL,
                    activated_at TEXT,
                    last_polled_at TEXT,
                    poll_count INTEGER NOT NULL DEFAULT 0,
                    code_received_at TEXT,
                    completed_at TEXT,
                    cancelled_at TEXT,
                    next_action_at TEXT,
                    compensation_attempts INTEGER NOT NULL DEFAULT 0,
                    compensation_claim_token TEXT NOT NULL DEFAULT '',
                    compensation_claimed_until TEXT,
                    last_error_code TEXT NOT NULL DEFAULT '',
                    last_provider_status TEXT NOT NULL DEFAULT '',
                    metadata TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(provider, provider_order_id)
                );
                """
            )
            # Existing installations predate several lifecycle fields.
            # ``CREATE INDEX`` must wait until every column exists; otherwise a
            # minimal legacy table makes the process fail during startup.
            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(sms_orders)")
            }
            column_definitions = {
                "order_id": "TEXT",
                "provider": "TEXT NOT NULL DEFAULT ''",
                "provider_order_id": "TEXT NOT NULL DEFAULT ''",
                "phone_label": "TEXT NOT NULL DEFAULT ''",
                "job_id": "TEXT NOT NULL DEFAULT ''",
                "attempt_id": "TEXT NOT NULL DEFAULT ''",
                "state": "TEXT NOT NULL DEFAULT 'allocating'",
                "pending_action": "TEXT NOT NULL DEFAULT 'cancel'",
                "lease_expires_at": "TEXT",
                "allocated_at": "TEXT NOT NULL DEFAULT ''",
                "activated_at": "TEXT",
                "last_polled_at": "TEXT",
                "poll_count": "INTEGER NOT NULL DEFAULT 0",
                "code_received_at": "TEXT",
                "completed_at": "TEXT",
                "cancelled_at": "TEXT",
                "next_action_at": "TEXT",
                "compensation_attempts": "INTEGER NOT NULL DEFAULT 0",
                "compensation_claim_token": "TEXT NOT NULL DEFAULT ''",
                "compensation_claimed_until": "TEXT",
                "last_error_code": "TEXT NOT NULL DEFAULT ''",
                "last_provider_status": "TEXT NOT NULL DEFAULT ''",
                "metadata": "TEXT NOT NULL DEFAULT '{}'",
                "created_at": "TEXT NOT NULL DEFAULT ''",
                "updated_at": "TEXT NOT NULL DEFAULT ''",
            }
            for name, definition in column_definitions.items():
                if name not in columns:
                    conn.execute(
                        "ALTER TABLE sms_orders ADD COLUMN %s %s" %
                        (name, definition)
                    )
            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(sms_orders)")
            }
            stamp = _now()
            # Backfill required identity/timestamp values before constraints or
            # indexes inspect them.  Legacy rows are retained rather than
            # silently discarded.
            table_sql_row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' "
                "AND name='sms_orders'"
            ).fetchone()
            without_rowid = bool(
                table_sql_row and "WITHOUT ROWID" in str(table_sql_row[0] or "").upper()
            )
            if without_rowid:
                # WITHOUT ROWID tables have no hidden locator.  The legacy
                # schema necessarily has ``order_id`` as its primary key, so
                # use that key for the additive backfill below.
                rows = conn.execute(
                    "SELECT order_id, provider, provider_order_id, state, "
                    "pending_action, allocated_at, created_at, updated_at, "
                    "poll_count, compensation_attempts FROM sms_orders ORDER BY order_id"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT rowid, order_id, provider, provider_order_id, state, "
                    "pending_action, allocated_at, created_at, updated_at, "
                    "poll_count, compensation_attempts FROM sms_orders ORDER BY rowid"
                ).fetchall()
            seen_ids = set()
            for row in rows:
                updates = {}
                order_id = str(row["order_id"] or "").strip()
                if not _ORDER_ID.fullmatch(order_id) or order_id in seen_ids:
                    order_id = "sms-legacy-" + uuid.uuid4().hex
                    updates["order_id"] = order_id
                seen_ids.add(order_id)
                if not str(row["provider"] or "").strip():
                    updates["provider"] = "unknown"
                provider_order_id = str(row["provider_order_id"] or "").strip()
                if not provider_order_id:
                    updates["provider_order_id"] = "legacy-" + order_id
                state = str(row["state"] or "").strip().lower()
                if state not in ORDER_STATES:
                    updates["state"] = ORDER_STATE_ALLOCATING
                pending = str(row["pending_action"] or "").strip().lower()
                if pending not in ("cancel", "finish"):
                    updates["pending_action"] = "cancel"
                if not _parse_time(row["allocated_at"]):
                    updates["allocated_at"] = stamp
                if not _parse_time(row["created_at"]):
                    updates["created_at"] = stamp
                if not _parse_time(row["updated_at"]):
                    updates["updated_at"] = stamp
                try:
                    poll_count = _counter_value(row["poll_count"])
                except (TypeError, ValueError, OverflowError):
                    poll_count = 0
                if row["poll_count"] != poll_count:
                    updates["poll_count"] = 0
                    if poll_count:
                        updates["poll_count"] = poll_count
                compensation_attempts = _counter_value(row["compensation_attempts"])
                if row["compensation_attempts"] != compensation_attempts:
                    updates["compensation_attempts"] = 0
                    if compensation_attempts:
                        updates["compensation_attempts"] = compensation_attempts
                if updates:
                    assignments = ", ".join(key + "=?" for key in updates)
                    locator = "order_id=?" if without_rowid else "rowid=?"
                    locator_value = row["order_id"] if without_rowid else row["rowid"]
                    conn.execute(
                        "UPDATE sms_orders SET " + assignments + " WHERE " + locator,
                        (*updates.values(), locator_value),
                    )
            # Add indexes only after additive migration/backfill is complete.
            conn.execute(
                "CREATE INDEX IF NOT EXISTS sms_orders_due_idx "
                "ON sms_orders(state, next_action_at, lease_expires_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS sms_orders_owner_idx "
                "ON sms_orders(job_id, attempt_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS sms_orders_compensation_claim_idx "
                "ON sms_orders(state, next_action_at, compensation_claimed_until)"
            )
            try:
                conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS sms_orders_provider_order_uidx "
                    "ON sms_orders(provider, provider_order_id)"
                )
            except sqlite3.IntegrityError:
                # Keep a recoverable legacy database online even when it
                # contains duplicate provider identifiers.  New writes still
                # use the immediate transaction and return the first match.
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS sms_orders_provider_order_idx "
                    "ON sms_orders(provider, provider_order_id)"
                )
            # Older installations allowed provider strings and exception text
            # to land in these columns.  Normalize them while the schema lock
            # is held so a restart cannot re-expose an unbounded payload.
            legacy_rows = conn.execute(
                "SELECT order_id, last_provider_status, last_error_code "
                "FROM sms_orders"
            ).fetchall()
            for row in legacy_rows:
                provider_status = _safe_provider_status(
                    row["last_provider_status"], ""
                )
                error_code = _safe_error(row["last_error_code"], "")
                if (provider_status != (row["last_provider_status"] or "") or
                        error_code != (row["last_error_code"] or "")):
                    conn.execute(
                        "UPDATE sms_orders SET last_provider_status=?, "
                        "last_error_code=? WHERE order_id=?",
                        (provider_status, error_code, row["order_id"]),
                    )
            # Rewrite legacy JSON as well as projecting it safely on reads.
            # This keeps secrets out of the raw SQLite file after the first
            # restart, including rows written by older workers.
            metadata_rows = conn.execute(
                "SELECT order_id, metadata FROM sms_orders"
            ).fetchall()
            for row in metadata_rows:
                safe_metadata = _safe_json(_load_json(row["metadata"]))
                if safe_metadata != (row["metadata"] or ""):
                    conn.execute(
                        "UPDATE sms_orders SET metadata=? WHERE order_id=?",
                        (safe_metadata, row["order_id"]),
                    )
            # Normalize malformed legacy lease/queue values.  A bad expiry
            # must not make an order invisible to compensation forever.
            timestamp_rows = conn.execute(
                "SELECT order_id, lease_expires_at, next_action_at, "
                "compensation_claim_token, compensation_claimed_until "
                "FROM sms_orders"
            ).fetchall()
            migration_now = _now()
            for row in timestamp_rows:
                updates = {}
                if row["lease_expires_at"] and _parse_time(row["lease_expires_at"]) is None:
                    updates["lease_expires_at"] = migration_now
                if row["next_action_at"] and _parse_time(row["next_action_at"]) is None:
                    updates["next_action_at"] = migration_now
                token = str(row["compensation_claim_token"] or "")
                claimed_until = row["compensation_claimed_until"]
                if (token and _parse_time(claimed_until) is None) or (
                        not token and claimed_until):
                    updates["compensation_claim_token"] = ""
                    updates["compensation_claimed_until"] = None
                if updates:
                    assignments = ", ".join(key + "=?" for key in updates)
                    conn.execute(
                        "UPDATE sms_orders SET " + assignments + " WHERE order_id=?",
                        (*updates.values(), row["order_id"]),
                    )

    @staticmethod
    def _row(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
        if row is None:
            return None
        result = dict(row)
        result["metadata"] = sanitize_operation_value(
            _load_json(result.get("metadata"))
        )
        result["poll_count"] = _counter_value(result.get("poll_count"))
        result["compensation_attempts"] = _counter_value(
            result.get("compensation_attempts")
        )
        return result

    def get_order(self, order_id: str) -> Optional[Dict[str, Any]]:
        with self.connect() as conn:
            return self._row(conn.execute(
                "SELECT * FROM sms_orders WHERE order_id=?", (str(order_id),)
            ).fetchone())

    def get_by_provider_order(self, provider: str, provider_order_id: str) -> Optional[Dict[str, Any]]:
        with self.connect() as conn:
            return self._row(conn.execute(
                """SELECT * FROM sms_orders
                   WHERE provider=? AND provider_order_id=?""",
                (str(provider or "").strip().lower(), str(provider_order_id or "").strip()),
            ).fetchone())

    def list_orders(self, *, states: Optional[Iterable[str]] = None,
                    limit: int = 100) -> List[Dict[str, Any]]:
        limit = min(_strict_int(limit, "limit", minimum=1), 1000)
        with self.connect() as conn:
            if states is None:
                rows = conn.execute(
                    "SELECT * FROM sms_orders ORDER BY created_at DESC LIMIT ?", (limit,)
                ).fetchall()
            else:
                values = [str(item) for item in states]
                if not values or any(item not in ORDER_STATES for item in values):
                    raise ValueError("invalid SMS order state filter")
                placeholders = ",".join("?" for _ in values)
                rows = conn.execute(
                    "SELECT * FROM sms_orders WHERE state IN (" + placeholders + ") "
                    "ORDER BY created_at DESC LIMIT ?", (*values, limit)
                ).fetchall()
            return [self._row(row) for row in rows]

    def create_order(self, provider: str, provider_order_id: str, phone: Any = "",
                     *, job_id: str = "", attempt_id: str = "",
                     lease_seconds: int = 180,
                     metadata: Optional[Dict[str, Any]] = None,
                     order_id: Optional[str] = None) -> Dict[str, Any]:
        provider = str(provider or "").strip().lower()
        if provider not in _PROVIDERS:
            raise ValueError("unsupported SMS provider")
        provider_order_id = _bounded_identity(
            provider_order_id, "provider order id", maximum=256,
            allow_empty=False,
        )
        job_id = _bounded_identity(job_id, "job id", maximum=128)
        attempt_id = _bounded_identity(attempt_id, "attempt id", maximum=128)
        lease_seconds = _strict_int(lease_seconds, "lease_seconds", minimum=1)
        order_id = _order_id(order_id)
        parsed_now, now = _required_time(None, "now")
        deadline = (parsed_now + timedelta(seconds=lease_seconds)).isoformat()
        values = (
            order_id, provider, provider_order_id, _mask_phone(phone),
            job_id, attempt_id,
            ORDER_STATE_ALLOCATING, "cancel", deadline, now,
            _safe_json(metadata or {}), now, now,
        )
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM sms_orders WHERE provider=? AND provider_order_id=?",
                (provider, provider_order_id),
            ).fetchone()
            if existing is not None:
                conn.commit()
                return self._row(existing)
            try:
                conn.execute(
                    """INSERT INTO sms_orders
                       (order_id, provider, provider_order_id, phone_label, job_id,
                        attempt_id, state, pending_action, lease_expires_at,
                        allocated_at, metadata, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    values,
                )
            except sqlite3.IntegrityError as exc:
                # A concurrent allocator won the unique provider/id race.
                row = conn.execute(
                    "SELECT * FROM sms_orders WHERE provider=? AND provider_order_id=?",
                    (provider, provider_order_id),
                ).fetchone()
                if row is None:
                    raise ValueError("SMS order already exists") from exc
                conn.commit()
                return self._row(row)
            row = conn.execute(
                "SELECT * FROM sms_orders WHERE order_id=?", (order_id,)
            ).fetchone()
            conn.commit()
            return self._row(row)

    def _set_state(self, order_id: str, allowed: Iterable[str], state: str,
                   *, pending_action: Optional[str] = None,
                   error_code: Optional[str] = None,
                   provider_status: Optional[str] = None,
                   now: Optional[str] = None,
                   extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if state not in ORDER_STATES:
            raise ValueError("invalid SMS order state")
        _, now = _required_time(now, "now")
        allowed = tuple(allowed)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM sms_orders WHERE order_id=?", (str(order_id),)
            ).fetchone()
            if row is None:
                raise KeyError(order_id)
            current = row["state"]
            if current == state or current in _TERMINAL_STATES:
                conn.commit()
                return self._row(row)
            if current not in allowed:
                raise ValueError("invalid SMS order transition")
            fields = {
                "state": state,
                "updated_at": now,
            }
            if pending_action is not None:
                fields["pending_action"] = str(pending_action)[:32]
            if error_code is not None:
                fields["last_error_code"] = _safe_error(error_code, "provider_error")
            if provider_status is not None:
                # Provider payloads are untrusted.  Persist only the finite
                # status vocabulary used by reconciliation and exports.
                fields["last_provider_status"] = _safe_provider_status(
                    provider_status, ""
                )
            fields.update(extra or {})
            assignments = ", ".join(key + "=?" for key in fields)
            conn.execute(
                "UPDATE sms_orders SET " + assignments + " WHERE order_id=?",
                (*fields.values(), str(order_id)),
            )
            updated = conn.execute(
                "SELECT * FROM sms_orders WHERE order_id=?", (str(order_id),)
            ).fetchone()
            conn.commit()
            return self._row(updated)

    def activate(self, order_id: str) -> Dict[str, Any]:
        return self._set_state(
            order_id, (ORDER_STATE_ALLOCATING,), ORDER_STATE_ACTIVE,
            pending_action="cancel", extra={"activated_at": _now()},
        )

    def mark_awaiting_code(self, order_id: str) -> Dict[str, Any]:
        return self._set_state(
            order_id, (ORDER_STATE_ACTIVE, ORDER_STATE_ALLOCATING),
            ORDER_STATE_AWAITING_CODE, pending_action="cancel",
        )

    def mark_polled(self, order_id: str) -> Dict[str, Any]:
        now = _now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM sms_orders WHERE order_id=?", (str(order_id),)).fetchone()
            if row is None:
                raise KeyError(order_id)
            if row["state"] in _TERMINAL_STATES:
                conn.commit()
                return self._row(row)
            conn.execute(
                "UPDATE sms_orders SET last_polled_at=?, poll_count=poll_count+1, updated_at=? WHERE order_id=?",
                (now, now, str(order_id)),
            )
            updated = conn.execute("SELECT * FROM sms_orders WHERE order_id=?", (str(order_id),)).fetchone()
            conn.commit()
            return self._row(updated)

    def mark_code_received(self, order_id: str) -> Dict[str, Any]:
        updated = self._set_state(
            order_id, (ORDER_STATE_ACTIVE, ORDER_STATE_AWAITING_CODE),
            ORDER_STATE_CODE_RECEIVED, pending_action="finish",
            extra={
                "code_received_at": _now(),
                "next_action_at": _now(),
                "compensation_claim_token": "",
                "compensation_claimed_until": None,
            },
        )
        self._legacy_intents[str(order_id)] = "finish"
        return updated

    def request_finish(self, order_id: str) -> Dict[str, Any]:
        """Persist the finish intent before contacting the SMS provider."""
        updated = self._set_state(
            order_id, (ORDER_STATE_CODE_RECEIVED,), ORDER_STATE_FINISH_PENDING,
            pending_action="finish",
            extra={
                "next_action_at": _now(),
                "compensation_claim_token": "",
                "compensation_claimed_until": None,
            },
        )
        self._legacy_intents[str(order_id)] = "finish"
        return updated

    def complete(self, order_id: str, *, provider_status: str = "") -> Dict[str, Any]:
        return self._set_state(
            order_id,
            (ORDER_STATE_ACTIVE, ORDER_STATE_AWAITING_CODE, ORDER_STATE_CODE_RECEIVED),
            ORDER_STATE_COMPLETED, pending_action="", provider_status=provider_status,
            extra={
                "completed_at": _now(), "next_action_at": None,
                "compensation_claim_token": "",
                "compensation_claimed_until": None,
            },
        )

    def expire_due(self, *, now: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        _, now = _required_time(now, "now")
        limit = min(_strict_int(limit, "limit", minimum=1), 1000)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """SELECT order_id FROM sms_orders
                   WHERE state IN ('allocating','active','awaiting_code')
                     AND lease_expires_at IS NOT NULL AND lease_expires_at <= ?
                   ORDER BY lease_expires_at LIMIT ?""", (now, limit)
            ).fetchall()
            ids = [row[0] for row in rows]
            for order_id in ids:
                conn.execute(
                    """UPDATE sms_orders SET state='expired', pending_action='cancel',
                       last_error_code='order_expired', next_action_at=?, updated_at=?
                       WHERE order_id=? AND state IN ('allocating','active','awaiting_code')""",
                    (now, now, order_id),
                )
            result = [self._row(conn.execute(
                "SELECT * FROM sms_orders WHERE order_id=?", (order_id,)
            ).fetchone()) for order_id in ids]
            conn.commit()
            return result

    def request_cancel(self, order_id: str, *, error_code: str = "",
                       reason: str = "") -> Dict[str, Any]:
        now = _now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM sms_orders WHERE order_id=?", (str(order_id),)).fetchone()
            if row is None:
                raise KeyError(order_id)
            if row["state"] in _TERMINAL_STATES:
                conn.commit()
                return self._row(row)
            # Expired is retained as a historical state until this request;
            # the provider compensation queue uses cancel_pending uniformly.
            safe_code = _safe_error(error_code, "")
            conn.execute(
                """UPDATE sms_orders SET state='cancel_pending', pending_action='cancel',
                   next_action_at=?, last_error_code=CASE WHEN ? <> '' THEN ? ELSE last_error_code END,
                   compensation_claim_token='', compensation_claimed_until=NULL,
                   updated_at=? WHERE order_id=?""",
                (now, safe_code, safe_code, now, str(order_id)),
            )
            updated = conn.execute("SELECT * FROM sms_orders WHERE order_id=?", (str(order_id),)).fetchone()
            conn.commit()
            result = self._row(updated)
            self._legacy_intents[str(order_id)] = "cancel"
            return result

    def list_due_compensation(self, *, now: Optional[str] = None,
                              limit: int = 100) -> List[Dict[str, Any]]:
        _, now = _required_time(now, "now")
        limit = min(_strict_int(limit, "limit", minimum=1), 1000)
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT * FROM sms_orders
                   WHERE state IN ('cancel_pending','finish_pending','code_received')
                     AND (state <> 'code_received' OR pending_action='finish')
                   AND (next_action_at IS NULL OR next_action_at <= ?)
                     AND (compensation_claimed_until IS NULL
                          OR compensation_claimed_until='' OR compensation_claimed_until <= ?)
                   ORDER BY COALESCE(next_action_at, created_at) LIMIT ?""",
                (now, now, limit),
            ).fetchall()
            return [self._row(row) for row in rows]

    def pending_compensation_error(self) -> str:
        """Report unresolved provider failures, not healthy or terminal backlog."""
        with self.connect() as conn:
            row = conn.execute(
                """SELECT last_error_code FROM sms_orders
                   WHERE (state IN ('cancel_pending','finish_pending')
                          OR (state='code_received' AND pending_action='finish'))
                     AND last_error_code IN
                         ('provider_error','provider_timeout','provider_rejected')
                   ORDER BY CASE last_error_code
                       WHEN 'provider_rejected' THEN 0
                       WHEN 'provider_timeout' THEN 1 ELSE 2 END
                   LIMIT 1"""
            ).fetchone()
            return normalize_error_code(
                row["last_error_code"], default="provider_error", allow_empty=False
            ) if row else ""

    def claim_due_compensation(self, *, now: Optional[str] = None,
                               limit: int = 100, lease_seconds: int = 60,
                               order_id: Optional[str] = None,
                               action: Optional[str] = None,
                               exclude_order_ids=()) -> List[Dict[str, Any]]:
        """Atomically claim due provider compensation rows.

        The claim token is required when recording the provider result.  This
        prevents a worker whose lease expired from overwriting a newer worker's
        outcome, and the immediate transaction makes concurrent reconcilers
        pick disjoint rows.
        """
        _, now = _required_time(now, "now")
        limit = min(_strict_int(limit, "limit", minimum=1), 1000)
        lease_seconds = _strict_int(lease_seconds, "lease_seconds", minimum=1)
        if action is not None and str(action).strip().lower() not in ("cancel", "finish"):
            raise ValueError("unsupported SMS compensation action")
        action = str(action).strip().lower() if action is not None else None
        parsed_now = _parse_time(now)
        if parsed_now is None:  # defensive; _required_time already checked
            raise ValueError("invalid now")
        claimed_until = (parsed_now + timedelta(seconds=lease_seconds)).isoformat()
        where = [
            "((state IN ('cancel_pending','finish_pending')) OR "
            "(state='code_received' AND pending_action='finish'))",
            "(next_action_at IS NULL OR next_action_at <= ?)",
            "(compensation_claimed_until IS NULL OR compensation_claimed_until='' "
            "OR compensation_claimed_until <= ?)",
        ]
        params = [now, now]
        if not isinstance(exclude_order_ids, (tuple, list, set, frozenset)) or len(exclude_order_ids) > 1000:
            raise ValueError("invalid excluded SMS orders")
        excluded = [_order_id(value) for value in exclude_order_ids]
        if excluded:
            where.append("order_id NOT IN (" + ",".join("?" for _ in excluded) + ")")
            params.extend(excluded)
        if order_id is not None:
            where.append("order_id=?")
            params.append(str(order_id))
        if action is not None:
            where.append("pending_action=?")
            params.append(action)
        params.append(limit)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            # A partially written claim (for example after an old-version
            # crash) cannot be compared safely by SQLite's text ordering.
            # Clear it before selecting due rows; this fences any old token.
            claim_rows = conn.execute(
                "SELECT order_id, compensation_claim_token, "
                "compensation_claimed_until FROM sms_orders "
                "WHERE compensation_claim_token<>'' OR compensation_claimed_until IS NOT NULL"
            ).fetchall()
            for claim_row in claim_rows:
                token = str(claim_row["compensation_claim_token"] or "")
                expiry = _parse_time(claim_row["compensation_claimed_until"])
                if (token and expiry is None) or (not token and claim_row["compensation_claimed_until"]):
                    conn.execute(
                        "UPDATE sms_orders SET compensation_claim_token='', "
                        "compensation_claimed_until=NULL, updated_at=? WHERE order_id=?",
                        (now, claim_row["order_id"]),
                    )
            # The same rule applies to a malformed queue deadline: make it
            # immediately eligible instead of silently dropping the order.
            queue_rows = conn.execute(
                "SELECT order_id, next_action_at FROM sms_orders "
                "WHERE state IN ('cancel_pending','finish_pending','code_received') "
                "AND next_action_at IS NOT NULL"
            ).fetchall()
            for queue_row in queue_rows:
                if _parse_time(queue_row["next_action_at"]) is None:
                    conn.execute(
                        "UPDATE sms_orders SET next_action_at=?, updated_at=? WHERE order_id=?",
                        (now, now, queue_row["order_id"]),
                    )
            rows = conn.execute(
                "SELECT order_id FROM sms_orders WHERE " + " AND ".join(where) +
                " ORDER BY COALESCE(next_action_at, created_at) LIMIT ?", params,
            ).fetchall()
            claimed = []
            for row in rows:
                token = uuid.uuid4().hex
                current = conn.execute(
                    "SELECT state, pending_action FROM sms_orders WHERE order_id=?",
                    (row[0],),
                ).fetchone()
                if current is None:
                    continue
                state = current["state"]
                pending = current["pending_action"] or "cancel"
                if state == ORDER_STATE_CODE_RECEIVED and pending == "finish":
                    state = ORDER_STATE_FINISH_PENDING
                updated = conn.execute(
                    """UPDATE sms_orders SET state=?, pending_action=?,
                       compensation_claim_token=?, compensation_claimed_until=?, updated_at=?
                       WHERE order_id=? AND
                         (compensation_claimed_until IS NULL OR compensation_claimed_until=''
                          OR compensation_claimed_until <= ?)""",
                    (state, pending, token, claimed_until, now, row[0], now),
                )
                if updated.rowcount:
                    self._local_claim_tokens[row[0]] = token
                    claimed.append(self._row(conn.execute(
                        "SELECT * FROM sms_orders WHERE order_id=?", (row[0],)
                    ).fetchone()))
            conn.commit()
            return claimed

    def assert_compensation_claim(self, order_id: str, *, claim_token: str,
                                  action: str, now: Optional[str] = None) -> bool:
        current_time, _ = _required_time(now, "now")
        with self.connect() as conn:
            row = conn.execute(
                "SELECT state, pending_action, compensation_claim_token, compensation_claimed_until "
                "FROM sms_orders WHERE order_id=?", (_order_id(order_id),),
            ).fetchone()
        expiry = _parse_time(row["compensation_claimed_until"]) if row is not None else None
        if (
            row is None or row["state"] not in (ORDER_STATE_CANCEL_PENDING, "finish_pending")
            or not claim_token or row["compensation_claim_token"] != claim_token
            or row["pending_action"] != action or expiry is None or expiry <= current_time
        ):
            raise ValueError("compensation claim lost")
        return True

    def claim_expired(self, *, now: Optional[str] = None,
                      limit: int = 100) -> List[Dict[str, Any]]:
        """Expire due leases and atomically move them to compensation queue."""
        _, now = _required_time(now, "now")
        limit = min(_strict_int(limit, "limit", minimum=1), 1000)
        self.expire_due(now=now, limit=limit)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """SELECT order_id FROM sms_orders
                   WHERE state='expired' AND (next_action_at IS NULL OR next_action_at <= ?)
                   ORDER BY COALESCE(next_action_at, created_at) LIMIT ?""",
                (now, limit),
            ).fetchall()
            ids = [row[0] for row in rows]
            for order_id in ids:
                conn.execute(
                    """UPDATE sms_orders SET state='cancel_pending', pending_action='cancel',
                       next_action_at=?, compensation_claim_token='',
                       compensation_claimed_until=NULL, updated_at=?
                       WHERE order_id=? AND state='expired'""",
                    (now, now, order_id),
                )
            result = [self._row(conn.execute(
                "SELECT * FROM sms_orders WHERE order_id=?", (order_id,)
            ).fetchone()) for order_id in ids]
            conn.commit()
            for item in result:
                self._legacy_intents[str(item["order_id"])] = "cancel"
            return result

    def record_compensation(self, order_id: str, *, action: str = "cancel",
                            success: bool, error_code: str = "",
                            provider_status: str = "", max_attempts: int = 3,
                            backoff_seconds: int = 30,
                            claim_token: Optional[str] = None,
                            now: Optional[str] = None) -> Dict[str, Any]:
        action = str(action or "cancel").strip().lower()
        if action not in ("cancel", "finish"):
            raise ValueError("unsupported SMS compensation action")
        max_attempts = _strict_int(max_attempts, "max_attempts", minimum=1)
        backoff_seconds = _strict_int(backoff_seconds, "backoff_seconds", minimum=0)
        if not isinstance(success, bool):
            raise ValueError("success must be boolean")
        _, now = _required_time(now, "now")
        safe_provider_status = _safe_provider_status(provider_status, "")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM sms_orders WHERE order_id=?", (str(order_id),)).fetchone()
            if row is None:
                raise KeyError(order_id)
            if row["state"] in _TERMINAL_STATES:
                conn.commit()
                return self._row(row)
            # The pending action is part of the claim fence.  A stale or
            # confused worker must not be able to turn a finish compensation
            # into a cancellation (or vice versa) merely by presenting a
            # valid token for the order.  Keep the token-less compatibility
            # path for older direct store callers, but enforce the action
            # contract whenever the order already declares one.
            pending_action = str(row["pending_action"] or "").strip().lower()
            if pending_action and pending_action != action:
                conn.rollback()
                raise ValueError("compensation action does not match pending action")
            stored_token = str(row["compensation_claim_token"] or "")
            if claim_token is not None:
                claimed_until = _parse_time(row["compensation_claimed_until"])
                current_time = _parse_time(now)
                if (not stored_token or stored_token != str(claim_token) or
                        claimed_until is None or claimed_until <= current_time):
                    conn.rollback()
                    raise ValueError("compensation claim lost")
            elif stored_token:
                # A path-only/token-less callback is retained solely for old
                # direct-store integrations.  It must originate from this
                # instance's current claim and be an explicit provider
                # cancellation acknowledgement; all other cases fail closed.
                local_token = self._local_claim_tokens.get(str(order_id))
                claimed_until = _parse_time(row["compensation_claimed_until"])
                current_time = _parse_time(now)
                if (local_token != stored_token or claimed_until is None or
                        claimed_until <= current_time or not success or
                        action != "cancel" or safe_provider_status != "cancelled"):
                    conn.rollback()
                    raise ValueError("compensation claim token is required")
            elif self._legacy_intents.get(str(order_id)) != action:
                # Without a durable claim, only the same in-process caller
                # that recorded the intent may use the legacy callback path.
                # This preserves old integrations while preventing a fresh
                # process from mutating an order by guessing its id.
                conn.rollback()
                raise ValueError("compensation claim token is required")
            if success:
                if action == "finish":
                    state = ORDER_STATE_COMPLETED
                    extra = {
                        "pending_action": "", "completed_at": now,
                        "next_action_at": None,
                    }
                else:
                    state = ORDER_STATE_CANCELLED
                    extra = {
                        "pending_action": "", "cancelled_at": now,
                        "next_action_at": None,
                    }
                values = {
                    "state": state,
                    "last_provider_status": safe_provider_status or "ok",
                    "compensation_claim_token": "",
                    "compensation_claimed_until": None,
                    "updated_at": now,
                    **extra,
                }
            else:
                attempts = _counter_value(row["compensation_attempts"]) + 1
                exhausted = attempts >= max_attempts
                state = ORDER_STATE_COMPENSATION_FAILED if exhausted else (
                    ORDER_STATE_CANCEL_PENDING if action == "cancel" else ORDER_STATE_FINISH_PENDING
                )
                try:
                    next_action = datetime.fromisoformat(now)
                    if next_action.tzinfo is None:
                        next_action = next_action.replace(tzinfo=timezone.utc)
                    next_action = (next_action + timedelta(seconds=backoff_seconds)).isoformat()
                except ValueError:
                    next_action = now
                values = {
                    "state": state,
                    "pending_action": action,
                    "compensation_attempts": attempts,
                    "last_error_code": _safe_error(error_code, "provider_error"),
                    "last_provider_status": _safe_provider_status(provider_status, "error"),
                    "compensation_claim_token": "",
                    "compensation_claimed_until": None,
                    "next_action_at": None if exhausted else next_action,
                    "updated_at": now,
                }
            assignments = ", ".join(key + "=?" for key in values)
            conn.execute(
                "UPDATE sms_orders SET " + assignments + " WHERE order_id=?",
                (*values.values(), str(order_id)),
            )
            updated = conn.execute("SELECT * FROM sms_orders WHERE order_id=?", (str(order_id),)).fetchone()
            conn.commit()
            self._local_claim_tokens.pop(str(order_id), None)
            if success or exhausted:
                self._legacy_intents.pop(str(order_id), None)
            return self._row(updated)


__all__ = [
    "SmsOrderStore", "ORDER_STATES", "ORDER_STATE_ALLOCATING",
    "ORDER_STATE_ACTIVE", "ORDER_STATE_AWAITING_CODE", "ORDER_STATE_CODE_RECEIVED",
    "ORDER_STATE_COMPLETED", "ORDER_STATE_CANCEL_PENDING", "ORDER_STATE_FINISH_PENDING",
    "ORDER_STATE_CANCELLED", "ORDER_STATE_EXPIRED", "ORDER_STATE_COMPENSATION_FAILED",
]
