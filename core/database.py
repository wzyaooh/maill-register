"""
Database Manager - SQLite storage for accounts, logs, and session data
"""
import sqlite3
import os
import logging
from datetime import datetime
import json
import re
from pathlib import Path

from core.secret_safety import (
    SAFE_ERROR_CODES,
    normalize_flow_mode,
    normalize_error_code,
    normalize_sms_service,
    redact_text,
    safe_registration_result_summary,
    safe_warm_result_summary,
    sanitize_operation_value,
)


_PROFILE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_PROFILE_ENGINES = ("playwright", "selenium", "appium")
_VERIFIED_PROFILE_ENGINES = ("playwright", "selenium")
_MAX_ACCOUNT_NOTE_LENGTH = 512

# Health snapshots are a typed protocol.  Values outside these sets are
# treated as an unavailable observation instead of becoming durable free text.
_BROWSER_STATUS_VALUES = frozenset({
    "authenticated", "not_configured", "login_required", "challenge",
    "account_mismatch", "profile_busy", "runtime_mismatch",
    "proxy_mismatch", "proxy_unavailable", "runtime_unavailable",
    "profile_conflict", "profile_unavailable", "network_error", "error",
    "identity_unavailable", "cleanup_failed",
})
_MAILBOX_STATUS_VALUES = frozenset({
    "active", "not_configured", "password_changed", "locked", "suspended",
    "network_error", "error", "unknown",
})
_OVERALL_STATUS_VALUES = frozenset({
    "active", "degraded", "password_changed", "suspended", "locked",
    "network_error", "error", "unknown",
})
_ACCOUNT_STATUS_VALUES = _OVERALL_STATUS_VALUES | frozenset({
    "disabled", "failed", "pending", "created", "authenticated",
    "not_configured", "login_required", "challenge", "profile_busy",
    "profile_conflict", "profile_unavailable", "cleanup_failed",
    "orphaned", "retired",
})
_SAFE_LEGACY_STATUS = re.compile(r"^[A-Za-z0-9@=+._-]{1,64}$")
_LEGACY_SAFE_STATUS_VALUES = frozenset({"+1", "-1"})
_LEGACY_SECRET_MARKER = re.compile(
    r"(?i)(password|passwd|pwd|token|secret|api[_ -]?key|authorization|bearer|"
    r"cookie|session|otp|verification|sms[_ -]?code|jwt|akia[0-9a-z]{8,})"
)

logger = logging.getLogger('gmail_creator_db')


def _normalise_error_code(value):
    """Keep only machine-readable health/profile codes in account metadata."""
    raw = "" if value is None else str(value).strip()
    if not raw:
        return ""
    # Account rows are consumed by the health/API projection, whose contract
    # is the protocol vocabulary in ``SAFE_ERROR_CODES``.  Do not turn a
    # malformed legacy payload (or an exception class name) into the generic
    # ``error`` marker: dropping it keeps the row semantically unknown and
    # prevents free-form diagnostics from becoming durable account state.
    for code in SAFE_ERROR_CODES:
        if code and raw.lower() == code.lower():
            return code
    return ""


def _normalise_protocol_status(value, allowed, default="error"):
    """Keep health status columns within their machine-readable vocabulary."""
    return value if isinstance(value, str) and value in allowed else default


def _normalise_account_status(value, default="active", secrets=()):
    """Keep account status safe while preserving legacy short labels.

    Older imports accepted display labels such as ``+1``.  Known protocol
    values are retained; unknown values are allowed only after credential
    redaction and bounded control-character filtering, so compatibility does
    not reopen a secret-bearing status channel.
    """
    if not isinstance(value, str):
        return default
    candidate = value.strip()
    if not candidate:
        return default
    if candidate in _ACCOUNT_STATUS_VALUES:
        return candidate
    # Status is a protocol field, not an arbitrary note channel.  Retain the
    # two short labels used by the pre-profile importer, but reject all other
    # unknown text even when it happens to look identifier-shaped.
    if candidate in _LEGACY_SAFE_STATUS_VALUES:
        return candidate
    redacted = redact_text(candidate, secrets=secrets)
    if redacted != candidate or not _SAFE_LEGACY_STATUS.fullmatch(redacted):
        return default
    if _LEGACY_SECRET_MARKER.search(candidate):
        return default
    if candidate not in _ACCOUNT_STATUS_VALUES:
        return default
    if any(ord(character) < 32 and character not in "\r\n\t" for character in redacted):
        return default
    return redacted


def _normalise_timestamp(value, default=""):
    """Accept only compact timestamp-shaped metadata, never free-form text."""
    if not isinstance(value, str):
        return default
    candidate = value.strip()
    if not candidate or len(candidate) > 64:
        return default
    if not re.fullmatch(r"[0-9TtZz:+. _-]+", candidate):
        return default
    return candidate


def _normalise_note(value, secrets=()):
    """Prepare operator notes for durable storage.

    Notes are compatibility metadata rather than a credential store.  The
    database boundary therefore redacts both labelled credential forms and the
    account's known password/proxy values, strips control characters, and
    applies a bounded length before SQLite sees the value.
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    value = redact_text(value, secrets=secrets)
    value = "".join(
        character if character in "\r\n\t" or ord(character) >= 32 else " "
        for character in value
    )
    return value[:_MAX_ACCOUNT_NOTE_LENGTH]


def _legacy_note(value, secrets=()):
    """Scrub a pre-existing note before it can cross the DB boundary.

    Legacy notes were never a credential store and may contain provider keys
    without a ``key=value`` label.  Preserve ordinary short operator text, but
    clear anything that still resembles a credential after redaction.  This is
    intentionally stricter than the normal new-write path because the old
    value has no provenance we can trust.
    """
    candidate = _normalise_note(value, secrets=secrets)
    if not candidate:
        return ""
    if _LEGACY_SECRET_MARKER.search(candidate):
        return ""
    if re.search(r"(?i)\b(?:eyJ[a-z0-9_-]{8,}\.[a-z0-9_.-]{8,}|AKIA[0-9A-Z]{8,})\b", candidate):
        return ""
    # High-entropy opaque values are overwhelmingly tokens in the old notes
    # column.  Keep natural-language notes and short identifiers intact.
    compact = re.sub(r"\s+", "", candidate)
    if len(compact) >= 32 and re.fullmatch(r"[A-Za-z0-9_./+=-]+", compact):
        return ""
    return candidate


class DatabaseManager:
    def __init__(self, db_path="data/database.db"):
        self.db_path = db_path
        self._ensure_dir()
        self._init_db()
        self._migrate_schema()
        # The job/attempt ledger shares this database so account state and
        # execution ownership survive the same restart and backup boundary.
        from core.job_ledger import JobLedger
        self.ledger = JobLedger(self.db_path)
        from core.sms_orders import SmsOrderStore
        self.sms_orders = SmsOrderStore(self.db_path)

    def _ensure_dir(self):
        db_dir = os.path.dirname(self.db_path)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir, exist_ok=True)

    def _init_db(self):
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()

                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS accounts (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        email TEXT UNIQUE NOT NULL,
                        password TEXT NOT NULL,
                        first_name TEXT DEFAULT '',
                        last_name TEXT DEFAULT '',
                        birthday TEXT DEFAULT '',
                        gender TEXT DEFAULT '',
                        proxy TEXT DEFAULT '',
                        strategy TEXT DEFAULT '',
                        sms_service TEXT DEFAULT '',
                        phone_number TEXT DEFAULT '',
                        profile_path TEXT DEFAULT '',
                        profile_id TEXT DEFAULT '',
                        engine TEXT DEFAULT '',
                        profile_state TEXT DEFAULT 'legacy_unbound',
                        identity_state TEXT DEFAULT 'legacy_unbound',
                        browser_status TEXT DEFAULT 'not_configured',
                        mailbox_status TEXT DEFAULT 'not_configured',
                        overall_status TEXT DEFAULT 'active',
                        browser_checked_at TEXT DEFAULT '',
                        mailbox_checked_at TEXT DEFAULT '',
                        last_error_code TEXT DEFAULT '',
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        status TEXT DEFAULT 'active',
                        notes TEXT DEFAULT '',
                        registration_result TEXT DEFAULT '{}',
                        warm_result TEXT DEFAULT '{}'
                    )
                ''')

                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS execution_logs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        level TEXT,
                        message TEXT
                    )
                ''')

                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS session_stats (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_start TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        total_attempts INTEGER DEFAULT 0,
                        successes INTEGER DEFAULT 0,
                        failures INTEGER DEFAULT 0,
                        strategies_used TEXT DEFAULT '{}',
                        errors TEXT DEFAULT '{}',
                        duration_seconds REAL DEFAULT 0
                    )
                ''')

                conn.commit()
                logger.debug("Database initialized successfully.")
        except sqlite3.Error as e:
            logger.error("Database initialization failed: %s", type(e).__name__)

    def _migrate_schema(self):
        new_columns = [
            ("first_name", "TEXT DEFAULT ''"),
            ("last_name", "TEXT DEFAULT ''"),
            ("proxy", "TEXT DEFAULT ''"),
            # SQLite rejects non-constant defaults in ALTER TABLE.  A text
            # default is portable; empty legacy values are backfilled below.
            ("created_at", "TEXT DEFAULT ''"),
            ("status", "TEXT DEFAULT 'active'"),
            ("birthday", "TEXT DEFAULT ''"),
            ("gender", "TEXT DEFAULT ''"),
            ("strategy", "TEXT DEFAULT ''"),
            ("sms_service", "TEXT DEFAULT ''"),
            ("phone_number", "TEXT DEFAULT ''"),
            ("profile_path", "TEXT DEFAULT ''"),
            ("profile_id", "TEXT DEFAULT ''"),
            ("engine", "TEXT DEFAULT ''"),
            ("profile_state", "TEXT DEFAULT 'legacy_unbound'"),
            ("identity_state", "TEXT DEFAULT 'legacy_unbound'"),
            ("browser_status", "TEXT DEFAULT 'not_configured'"),
            ("mailbox_status", "TEXT DEFAULT 'not_configured'"),
            ("overall_status", "TEXT DEFAULT 'active'"),
            ("browser_checked_at", "TEXT DEFAULT ''"),
            ("mailbox_checked_at", "TEXT DEFAULT ''"),
            ("last_error_code", "TEXT DEFAULT ''"),
            ("notes", "TEXT DEFAULT ''"),
            ("registration_result", "TEXT DEFAULT '{}'"),
            ("warm_result", "TEXT DEFAULT '{}'"),
        ]
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("PRAGMA table_info(accounts)")
                existing = {row[1] for row in cursor.fetchall()}
                for col_name, col_def in new_columns:
                    if col_name not in existing:
                        cursor.execute(f"ALTER TABLE accounts ADD COLUMN {col_name} {col_def}")
                        logger.info(f"Migrated: added column '{col_name}' to accounts table")
                # Existing rows have no trustworthy browser identity.  Make
                # that explicit instead of implying that a path alone is a
                # valid ready profile.
                cursor.execute("""
                    UPDATE accounts
                       SET profile_state=CASE WHEN COALESCE(profile_id, '') <> '' THEN COALESCE(NULLIF(profile_state, ''), 'bound') ELSE 'legacy_unbound' END,
                           identity_state=CASE WHEN COALESCE(profile_id, '') <> '' THEN COALESCE(NULLIF(identity_state, ''), 'identity_reconstructed') ELSE 'legacy_unbound' END,
                           browser_status=COALESCE(NULLIF(browser_status, ''), 'not_configured'),
                           mailbox_status=COALESCE(NULLIF(mailbox_status, ''), 'not_configured'),
                           overall_status=COALESCE(NULLIF(overall_status, ''), 'active')
                """)
                # Backfill timestamps and scrub legacy free-form metadata in
                # the same transaction.  Password/proxy are used only as
                # in-memory redaction hints and are never copied out.
                cursor.execute(
                    "UPDATE accounts SET created_at=CURRENT_TIMESTAMP "
                    "WHERE created_at IS NULL OR TRIM(CAST(created_at AS TEXT))=''"
                )
                legacy_rows = cursor.execute(
                    "SELECT id, password, proxy, notes, status, last_error_code, "
                    "strategy, sms_service, registration_result, warm_result "
                    "FROM accounts"
                ).fetchall()
                for row in legacy_rows:
                    (account_id, password, proxy, notes, status, error_code,
                     strategy, sms_service, registration_result, warm_result) = row
                    safe_status = _normalise_account_status(
                        status, default="active", secrets=(password, proxy)
                    )
                    # ``error`` is the only safe fallback for an unknown
                    # non-empty legacy status; an empty value remains active.
                    if isinstance(status, str) and status.strip() and \
                            safe_status == "active" and status.strip() not in ("active",):
                        safe_status = "error"
                    safe_error = _normalise_error_code(error_code)
                    safe_notes = _legacy_note(notes, secrets=(password, proxy))
                    safe_strategy = normalize_flow_mode(strategy)
                    safe_sms_service = normalize_sms_service(sms_service)
                    safe_registration = self._encode_operation_result(
                        registration_result, kind="registration"
                    )
                    safe_warm = self._encode_operation_result(
                        warm_result, kind="warm"
                    )
                    cursor.execute(
                        "UPDATE accounts SET notes=?, status=?, last_error_code=?, "
                        "strategy=?, sms_service=?, registration_result=?, warm_result=? "
                        "WHERE id=?",
                        (safe_notes, safe_status, safe_error, safe_strategy,
                         safe_sms_service, safe_registration, safe_warm, account_id),
                    )
                try:
                    cursor.execute(
                        "CREATE UNIQUE INDEX IF NOT EXISTS accounts_profile_id_unique "
                        "ON accounts(profile_id) WHERE profile_id IS NOT NULL AND profile_id <> ''"
                    )
                except sqlite3.IntegrityError:
                    # Do not destroy user data during migration.  The caller
                    # still receives explicit duplicate errors on new writes.
                    logger.warning("Duplicate legacy profile_id values prevent unique index creation")
                conn.commit()
        except sqlite3.Error as e:
            logger.warning("Schema migration warning: %s", type(e).__name__)

    def _runtime_root(self):
        """Return the process-selected runtime root without creating files."""
        # A DatabaseManager created for an isolated environment normally uses
        # ``<runtime>/data/database.db``.  Infer that root when the worker has
        # exported PROFILE_RUNTIME_ROOT (and even when a stale parent-process
        # value is present).  The database location is the stronger binding:
        # it prevents one environment's profile ids from being written into
        # another environment's profile tree.
        database_path = Path(self.db_path).expanduser().resolve()
        if database_path.parent.name == "data":
            return database_path.parent.parent
        if database_path.parent.name == "web" and database_path.parent.parent.name == "data":
            return database_path.parent.parent.parent
        configured = os.getenv("PROFILE_RUNTIME_ROOT") or os.getenv("GMAIL_DATA_ROOT")
        if configured:
            return Path(configured).expanduser().resolve()
        return Path(os.getcwd()).resolve()

    def _derived_profile_path(self, profile_id):
        return self._runtime_root() / "data" / "profiles" / profile_id

    @staticmethod
    def _reject_symlink_components(path):
        cursor = Path(path)
        while True:
            # ``is_symlink`` also sees dangling links; ``exists`` alone would
            # turn a broken link into an apparently safe ordinary path.
            if cursor.is_symlink():
                raise ValueError("profile_path cannot contain a symlink")
            if cursor.parent == cursor:
                break
            cursor = cursor.parent

    def _normalise_profile_binding(self, profile_id, profile_path, engine,
                                   email, profile_state, identity_state):
        """Validate the durable id/path binding before writing an account row.

        ``profile_path`` remains a compatibility column, but a row with a
        ``profile_id`` must point to the one directory derived from that id.
        When a runtime root is explicitly configured (as web workers do), the
        configured root is authoritative.  Standalone migration/tests may
        supply an existing ``.../data/profiles/<id>`` path; its root is then
        retained after canonicalisation.
        """
        if profile_id not in (None, "") and not isinstance(profile_id, str):
            raise ValueError("Invalid profile_id")
        if isinstance(profile_id, str) and profile_id and not profile_id.strip():
            raise ValueError("Invalid profile_id")
        profile_id = str(profile_id or "").strip()
        profile_path = str(profile_path or "").strip()
        if not profile_id:
            # Rows without a durable id are intentionally legacy/unbound.  Do
            # not reinterpret an old path as a new profile binding.
            return profile_path, profile_id
        if not _PROFILE_ID.fullmatch(profile_id):
            raise ValueError("Invalid profile_id")
        if engine and engine not in _PROFILE_ENGINES:
            raise ValueError("Unsupported account engine")
        if profile_state and profile_state not in (
            "provisioning", "bound", "ready", "orphaned", "corrupt",
            "cleanup_failed",
            "retired", "legacy_unbound",
        ):
            raise ValueError("Invalid profile_state")
        if identity_state and identity_state not in (
            "native", "identity_reconstructed", "legacy_unbound",
        ):
            raise ValueError("Invalid identity_state")

        database_path = Path(self.db_path).expanduser().resolve()
        isolated_database = (
            database_path.parent.name == "data"
            or (database_path.parent.name == "web" and database_path.parent.parent.name == "data")
        )
        configured = bool(os.getenv("PROFILE_RUNTIME_ROOT") or os.getenv("GMAIL_DATA_ROOT")) \
            or isolated_database
        derived = self._derived_profile_path(profile_id)
        if profile_path:
            supplied = Path(profile_path).expanduser()
            if not supplied.is_absolute():
                supplied = self._runtime_root() / supplied
            supplied = Path(os.path.abspath(str(supplied)))
            self._reject_symlink_components(supplied)
            supplied_resolved = supplied.resolve(strict=False)
            if configured:
                if supplied_resolved != derived:
                    raise ValueError("profile_path does not match profile_id")
            else:
                # In a standalone process there may be no environment root;
                # accept an explicitly supplied canonical profile path, but
                # never an arbitrary directory with the same basename.
                parts = supplied_resolved.parts
                if len(parts) < 3 or parts[-3:] != ("data", "profiles", profile_id):
                    raise ValueError("profile_path does not match profile_id")
                derived = supplied_resolved
            canonical = supplied_resolved
        else:
            canonical = derived

        # Check the lexical path selected by either branch before resolving or
        # opening a manifest.  This also detects dangling derived-directory
        # links, which Path.exists() would otherwise hide.
        self._reject_symlink_components(canonical)

        # If a manifest is already present, the database row must agree with
        # its immutable binding.  Missing manifests remain permissible for
        # legacy/import tooling and are surfaced by runtime reconciliation.
        manifest_path = canonical / "profile_manifest.json"
        if manifest_path.exists():
            if manifest_path.is_symlink() or not manifest_path.is_file():
                raise ValueError("profile manifest is not a regular file")
            try:
                with manifest_path.open("r", encoding="utf-8") as stream:
                    manifest = json.load(stream)
            except (OSError, ValueError, TypeError) as exc:
                raise ValueError("profile manifest cannot be read") from exc
            if not isinstance(manifest, dict):
                raise ValueError("profile manifest is invalid")
            if manifest.get("profile_id") != profile_id:
                raise ValueError("profile manifest profile_id conflicts with account")
            bound_email = str(manifest.get("email") or "").strip().lower()
            if bound_email and str(email or "").strip().lower() != bound_email:
                raise ValueError("profile manifest email conflicts with account")
            bound_engine = manifest.get("engine")
            if engine and bound_engine and engine != bound_engine:
                raise ValueError("profile manifest engine conflicts with account")
            manifest_state = manifest.get("state")
            if manifest_state in ("orphaned", "corrupt", "retired") and profile_state in ("bound", "ready"):
                raise ValueError("profile manifest is not available for account binding")
        return str(canonical), profile_id

    @staticmethod
    def migration_profile_projection(account):
        """Project imported profile metadata without manufacturing trust."""
        item = account if isinstance(account, dict) else {}
        raw_profile_id = item.get("profile_id")
        # Imports must preserve the direct API's type contract.  Coercing
        # numeric/boolean ids into strings can accidentally bind an unrelated
        # directory name, even though the resulting row is fail-closed.
        profile_id = raw_profile_id.strip() if isinstance(raw_profile_id, str) else ""
        engine = str(item.get("engine") or "").strip()
        profile_state = str(item.get("profile_state") or "").strip()
        identity_state = str(item.get("identity_state") or "").strip()
        browser_status = str(item.get("browser_status") or "").strip()
        overall_status = str(item.get("overall_status") or "").strip()
        verified = bool(
            profile_id
            and engine in _VERIFIED_PROFILE_ENGINES
            and profile_state in ("bound", "ready")
            and identity_state == "native"
            and browser_status == "authenticated"
            and overall_status == "active"
        )
        if profile_id and not verified:
            profile_state = "legacy_unbound"
            identity_state = "identity_reconstructed"
            browser_status = "not_configured"
            overall_status = "unknown"
        elif not profile_id:
            profile_state = profile_state or "legacy_unbound"
            identity_state = identity_state or "legacy_unbound"
            browser_status = browser_status or "not_configured"
            overall_status = overall_status or item.get("status", "active")
        return {
            "profile_path": item.get("profile_path", ""),
            "profile_id": profile_id,
            "engine": engine,
            "profile_state": profile_state,
            "identity_state": identity_state,
            "browser_status": browser_status,
            "mailbox_status": item.get("mailbox_status", "not_configured"),
            "overall_status": overall_status,
        }

    def save_account(self, email, password, first_name="", last_name="",
                     proxy="", strategy="", sms_service="", phone_number="",
                     birthday="", gender="", status="active", notes="", profile_path="",
                     profile_id="", engine="", profile_state="", identity_state="",
                     browser_status="", mailbox_status="", overall_status="",
                     browser_checked_at="", mailbox_checked_at="", last_error_code="",
                     registration_result=None, warm_result=None):
        profile_path, profile_id = self._normalise_profile_binding(
            profile_id, profile_path, engine, email, profile_state, identity_state
        )
        if engine and engine not in _PROFILE_ENGINES:
            raise ValueError("Unsupported account engine")
        verified_claim = bool(profile_id and (
            profile_state == "ready"
            or identity_state == "native"
            or browser_status == "authenticated"
            or overall_status == "active"
        ))
        if verified_claim and not (
            engine in _VERIFIED_PROFILE_ENGINES
            and profile_state in ("bound", "ready")
            and identity_state == "native"
            and browser_status == "authenticated"
            and overall_status == "active"
        ):
            raise ValueError(
                "Verified profile bindings require an engine and explicit trust states"
            )
        if not profile_state:
            profile_state = "legacy_unbound"
        if not identity_state:
            identity_state = (
                "identity_reconstructed" if profile_id else "legacy_unbound"
            )
        browser_status = _normalise_protocol_status(
            browser_status or "not_configured",
            _BROWSER_STATUS_VALUES,
        )
        mailbox_status = (
            "not_configured"
            if not mailbox_status
            else _normalise_protocol_status(mailbox_status, _MAILBOX_STATUS_VALUES)
        )
        status = _normalise_account_status(status, secrets=(password, proxy))
        overall_status = _normalise_account_status(
            overall_status or ("unknown" if profile_id else status),
            secrets=(password, proxy),
        )
        last_error_code = _normalise_error_code(last_error_code)
        strategy = normalize_flow_mode(strategy)
        sms_service = normalize_sms_service(sms_service)
        notes = _normalise_note(notes, secrets=(password, proxy))
        browser_checked_at = _normalise_timestamp(browser_checked_at)
        mailbox_checked_at = _normalise_timestamp(mailbox_checked_at)
        registration_result = self._encode_operation_result(
            registration_result, kind="registration"
        )
        warm_result = self._encode_operation_result(warm_result, kind="warm")
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT INTO accounts
                    (email, password, first_name, last_name, birthday, gender,
                     proxy, strategy, sms_service, phone_number, status, notes,
                     profile_path, profile_id, engine, profile_state, identity_state,
                     browser_status, mailbox_status, overall_status,
                     browser_checked_at, mailbox_checked_at, last_error_code,
                     registration_result, warm_result, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (email, password, first_name, last_name, birthday, gender,
                      proxy, strategy, sms_service, phone_number, status, notes,
                      profile_path, profile_id, engine, profile_state, identity_state,
                      browser_status, mailbox_status, overall_status,
                      browser_checked_at, mailbox_checked_at, last_error_code,
                      registration_result, warm_result,
                      datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
                conn.commit()
                logger.info(f"Account saved: {email}")
                return True
        except sqlite3.IntegrityError:
            logger.warning(f"Account already exists: {email}")
            return False
        except ValueError:
            raise
        except sqlite3.Error as e:
            logger.error("Failed to save account %s: %s", email, type(e).__name__)
            return False

    def save_profile_binding(self, email, password, profile_id, engine,
                             profile_path=None, profile_state="bound",
                             identity_state="native", **fields):
        """Persist a profile-backed account through the canonical binding API.

        Callers normally pass the ``ProfileHandle.path``; the regular
        ``save_account`` validation still owns all path/manifest checks.
        This helper keeps adoption and future recovery code from rebuilding
        the compatibility columns by hand.
        """
        # Do not allow compatibility fields to override the canonical
        # binding arguments supplied above.
        for key in ("profile_id", "profile_path", "engine", "profile_state", "identity_state"):
            fields.pop(key, None)
        return self.save_account(
            email=email, password=password, profile_id=profile_id,
            profile_path=profile_path or "", engine=engine,
            profile_state=profile_state, identity_state=identity_state,
            **fields,
        )

    def get_all_accounts(self):
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute('SELECT * FROM accounts ORDER BY created_at DESC')
                return [self._decode_account_row(dict(row)) for row in cursor.fetchall()]
        except sqlite3.Error as e:
            logger.error("Failed to retrieve accounts: %s", type(e).__name__)
            return []

    def get_account_count(self):
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute('SELECT COUNT(*) FROM accounts')
                return cursor.fetchone()[0]
        except sqlite3.Error:
            return 0

    def get_account_by_profile_id(self, profile_id):
        if not profile_id:
            return None
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute("SELECT * FROM accounts WHERE profile_id=? LIMIT 1", (profile_id,)).fetchone()
                return self._decode_account_row(dict(row)) if row else None
        except sqlite3.Error as e:
            logger.error("Failed to retrieve profile %s: %s", profile_id, type(e).__name__)
            return None

    def update_health_snapshot(self, email, snapshot):
        """Persist browser/mailbox observations in one SQLite transaction."""
        if not isinstance(snapshot, dict):
            raise ValueError("health snapshot must be an object")
        browser_status = _normalise_protocol_status(
            snapshot.get("browser_status"), _BROWSER_STATUS_VALUES
        )
        mailbox_status = _normalise_protocol_status(
            snapshot.get("mailbox_status"), _MAILBOX_STATUS_VALUES
        )
        overall = _normalise_protocol_status(
            snapshot.get("status") or snapshot.get("overall_status"),
            _OVERALL_STATUS_VALUES,
        )
        checked_at = _normalise_timestamp(
            snapshot.get("checked_at"),
            datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        )
        try:
            with sqlite3.connect(self.db_path, timeout=15) as conn:
                conn.execute("BEGIN IMMEDIATE")
                account_row = conn.execute(
                    "SELECT password, proxy FROM accounts WHERE email=? LIMIT 1",
                    (email,),
                ).fetchone()
                if account_row is None:
                    conn.rollback()
                    return False
                cursor = conn.execute("""
                    UPDATE accounts
                       SET browser_status=?, mailbox_status=?, overall_status=?, status=?,
                           browser_checked_at=?, mailbox_checked_at=?, last_error_code=?, notes=?
                     WHERE email=?
                """, (
                    browser_status, mailbox_status, overall, overall,
                    _normalise_timestamp(snapshot.get("browser_checked_at"), checked_at),
                    _normalise_timestamp(snapshot.get("mailbox_checked_at"), checked_at),
                    _normalise_error_code(snapshot.get("last_error_code", "")),
                    _normalise_note(snapshot.get("message", ""), secrets=account_row), email,
                ))
                conn.commit()
                return cursor.rowcount > 0
        except sqlite3.Error as e:
            logger.error("Failed to update health snapshot for %s: %s", email, type(e).__name__)
            return False

    @staticmethod
    def _encode_operation_result(value, *, kind):
        """Return safe JSON for one registration/warm outcome column."""
        if value is None:
            return "{}"
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (TypeError, ValueError):
                value = {}
        if kind == "registration":
            projected = safe_registration_result_summary(value)
        else:
            projected = safe_warm_result_summary(value)
        return json.dumps(projected, ensure_ascii=True, separators=(",", ":"))

    @staticmethod
    def _decode_account_row(row):
        """Decode and re-project durable operation results on read."""
        for field, kind in (("registration_result", "registration"),
                            ("warm_result", "warm")):
            raw = row.get(field)
            if kind == "registration":
                try:
                    decoded = json.loads(raw) if isinstance(raw, str) else raw
                except (TypeError, ValueError):
                    decoded = {}
                row[field] = safe_registration_result_summary(decoded) if decoded else {}
            else:
                try:
                    decoded = json.loads(raw) if isinstance(raw, str) else raw
                except (TypeError, ValueError):
                    decoded = {}
                row[field] = safe_warm_result_summary(decoded) if decoded else {}
        return row

    def update_operation_results(self, email, registration_result=None,
                                 warm_result=None):
        """Atomically update safe registration/warm projections for an account.

        ``None`` means leave that projection unchanged.  The method is used
        after post-registration warming so a warm failure cannot roll back the
        already committed registration outcome.
        """
        if registration_result is None and warm_result is None:
            return False
        try:
            with sqlite3.connect(self.db_path, timeout=15) as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT registration_result, warm_result FROM accounts "
                    "WHERE email=? LIMIT 1", (email,)
                ).fetchone()
                if row is None:
                    conn.rollback()
                    return False
                registration_json = (
                    self._encode_operation_result(registration_result, kind="registration")
                    if registration_result is not None else row[0] or "{}"
                )
                warm_json = (
                    self._encode_operation_result(warm_result, kind="warm")
                    if warm_result is not None else row[1] or "{}"
                )
                cursor = conn.execute(
                    "UPDATE accounts SET registration_result=?, warm_result=? "
                    "WHERE email=?",
                    (registration_json, warm_json, email),
                )
                conn.commit()
                return cursor.rowcount > 0
        except sqlite3.Error as e:
            logger.error("Failed to update operation results for %s: %s", email, type(e).__name__)
            return False

    def update_profile_state(self, email, profile_state, identity_state=None):
        """Advance the database half of the recoverable profile binding."""
        try:
            with sqlite3.connect(self.db_path, timeout=15) as conn:
                if identity_state is None:
                    cursor = conn.execute(
                        "UPDATE accounts SET profile_state=? WHERE email=?",
                        (profile_state, email),
                    )
                else:
                    cursor = conn.execute(
                        "UPDATE accounts SET profile_state=?, identity_state=? WHERE email=?",
                        (profile_state, identity_state, email),
                    )
                conn.commit()
                return cursor.rowcount > 0
        except sqlite3.Error as e:
            logger.error("Failed to update profile state for %s: %s", email, type(e).__name__)
            return False

    def update_account_status(self, email, status, notes=""):
        try:
            with sqlite3.connect(self.db_path) as conn:
                account_row = conn.execute(
                    "SELECT password, proxy FROM accounts WHERE email=? LIMIT 1",
                    (email,),
                ).fetchone()
                if account_row is None:
                    return False
                cursor = conn.cursor()
                cursor.execute(
                    'UPDATE accounts SET status=?, notes=? WHERE email=?',
                    (_normalise_account_status(status, default="error", secrets=account_row),
                     _normalise_note(notes, secrets=account_row), email)
                )
                conn.commit()
                return cursor.rowcount > 0
        except sqlite3.Error as e:
            logger.error("Failed to update account %s: %s", email, type(e).__name__)
            return False

    def log_event(self, level, message):
        try:
            safe_level = redact_text(level)[:32]
            safe_message = redact_text(message)
            safe_message = "".join(
                character if character in "\r\n\t" or ord(character) >= 32 else " "
                for character in safe_message
            )[:2048]
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT INTO execution_logs (level, message, timestamp)
                    VALUES (?, ?, ?)
                ''', (safe_level, safe_message, datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
                conn.commit()
        except sqlite3.Error as e:
            logger.error("Failed to save log: %s", type(e).__name__)

    def save_session_stats(self, total_attempts, successes, failures,
                           strategies_used, errors, duration_seconds):
        try:
            safe_strategies = sanitize_operation_value(strategies_used)
            safe_errors = sanitize_operation_value(errors)
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT INTO session_stats
                    (total_attempts, successes, failures, strategies_used, errors, duration_seconds)
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', (total_attempts, successes, failures,
                      json.dumps(safe_strategies, ensure_ascii=False),
                      json.dumps(safe_errors, ensure_ascii=False),
                      duration_seconds))
                conn.commit()
        except sqlite3.Error as e:
            logger.error("Failed to save session stats: %s", type(e).__name__)

    def get_session_history(self, limit=10):
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute(
                    'SELECT * FROM session_stats ORDER BY session_start DESC LIMIT ?',
                    (limit,)
                )
                return [dict(row) for row in cursor.fetchall()]
        except sqlite3.Error:
            return []

    def run_migration(self, old_json_path="data/accounts.json", old_txt_path="data/accounts.txt"):
        migrated_count = 0

        if os.path.exists(old_txt_path):
            try:
                with open(old_txt_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line and ":" in line:
                            parts = line.split(":")
                            email = parts[0]
                            password = parts[1] if len(parts) > 1 else ""
                            if self.save_account(email, password):
                                migrated_count += 1
            except Exception as e:
                logger.error("TXT migration failed: %s", type(e).__name__)

        if os.path.exists(old_json_path):
            try:
                with open(old_json_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for acc in data:
                    if isinstance(acc, dict) and 'email' in acc and 'password' in acc:
                        profile = self.migration_profile_projection(acc)
                        if self.save_account(
                            email=acc.get('email'),
                            password=acc.get('password'),
                            first_name=acc.get('first_name', ''),
                            last_name=acc.get('last_name', ''),
                            status=acc.get('status', 'active'),
                            **profile,
                        ):
                            migrated_count += 1
            except Exception as e:
                logger.error("JSON migration failed: %s", type(e).__name__)

        return migrated_count
