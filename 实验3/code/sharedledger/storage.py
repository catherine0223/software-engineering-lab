"""SQLite schema, transactions, and shared access rules."""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3


class RuleError(ValueError):
    """A business rule prevented the operation."""


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    phone TEXT NOT NULL UNIQUE,
    username TEXT NOT NULL DEFAULT '',
    salt BLOB NOT NULL,
    password_hash BLOB NOT NULL,
    failures INTEGER NOT NULL DEFAULT 0,
    locked_until INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS ledgers (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    dissolved INTEGER NOT NULL DEFAULT 0,
    dissolved_by INTEGER REFERENCES users
);
CREATE TABLE IF NOT EXISTS memberships (
    id INTEGER PRIMARY KEY,
    ledger_id INTEGER NOT NULL REFERENCES ledgers,
    user_id INTEGER NOT NULL REFERENCES users,
    role TEXT NOT NULL CHECK(role IN ('admin', 'writer', 'reader')),
    joined_at TEXT NOT NULL,
    left_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS one_current_membership
ON memberships(ledger_id, user_id) WHERE left_at IS NULL;
CREATE TABLE IF NOT EXISTS invitations (
    token TEXT PRIMARY KEY,
    ledger_id INTEGER NOT NULL REFERENCES ledgers,
    role TEXT NOT NULL CHECK(role IN ('admin', 'writer', 'reader')),
    expires_at TEXT NOT NULL,
    used_at TEXT
);
CREATE TABLE IF NOT EXISTS batches (
    id INTEGER PRIMARY KEY,
    ledger_id INTEGER NOT NULL REFERENCES ledgers,
    request_id TEXT NOT NULL,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    operator_id INTEGER NOT NULL REFERENCES users,
    created_at TEXT NOT NULL,
    balances TEXT NOT NULL,
    UNIQUE(ledger_id, request_id)
);
CREATE TABLE IF NOT EXISTS settlement_requests (
    sequence INTEGER PRIMARY KEY,
    ledger_id INTEGER NOT NULL REFERENCES ledgers,
    request_id TEXT NOT NULL,
    operator_id INTEGER NOT NULL REFERENCES users,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    history_ids TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    created_at TEXT NOT NULL,
    error TEXT NOT NULL DEFAULT '',
    UNIQUE(ledger_id, request_id)
);
CREATE TABLE IF NOT EXISTS entries (
    id INTEGER PRIMARY KEY,
    ledger_id INTEGER NOT NULL REFERENCES ledgers,
    creator_id INTEGER NOT NULL REFERENCES users,
    submitter_id INTEGER REFERENCES users,
    fund_holder_id INTEGER REFERENCES users,
    title TEXT NOT NULL,
    category TEXT NOT NULL,
    occurred_on TEXT,
    occurred_time TEXT,
    amount_cent INTEGER CHECK(amount_cent > 0),
    entry_type TEXT NOT NULL CHECK(entry_type IN ('expense', 'income')),
    kind TEXT NOT NULL CHECK(kind IN ('ordinary', 'reversal', 'correction')),
    status TEXT NOT NULL CHECK(status IN
        ('draft', 'pending', 'approved', 'rejected', 'withdrawn', 'voided')),
    version INTEGER NOT NULL DEFAULT 1,
    ever_submitted INTEGER NOT NULL DEFAULT 0,
    approved_at TEXT,
    notes TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    original_entry_id INTEGER REFERENCES entries,
    corrected_reversal_id INTEGER REFERENCES entries,
    batch_id INTEGER REFERENCES batches
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_reversal
ON entries(original_entry_id)
WHERE kind = 'reversal' AND status IN ('pending', 'approved');
CREATE UNIQUE INDEX IF NOT EXISTS one_correction
ON entries(corrected_reversal_id) WHERE kind = 'correction';
CREATE INDEX IF NOT EXISTS entry_ledger_status
ON entries(ledger_id, status, occurred_on);
CREATE TABLE IF NOT EXISTS entry_submission_requests (
    ledger_id INTEGER NOT NULL REFERENCES ledgers,
    actor_id INTEGER NOT NULL REFERENCES users,
    request_id TEXT NOT NULL,
    entry_id INTEGER NOT NULL REFERENCES entries,
    PRIMARY KEY(ledger_id, actor_id, request_id)
);
CREATE INDEX IF NOT EXISTS entry_ledger_kind ON entries(ledger_id, kind);
CREATE TABLE IF NOT EXISTS allocations (
    entry_id INTEGER NOT NULL REFERENCES entries,
    member_user_id INTEGER NOT NULL REFERENCES users,
    amount_cent INTEGER NOT NULL CHECK(amount_cent >= 0),
    selection_order INTEGER NOT NULL,
    PRIMARY KEY(entry_id, member_user_id),
    UNIQUE(entry_id, selection_order)
);
CREATE TABLE IF NOT EXISTS budgets (
    ledger_id INTEGER NOT NULL REFERENCES ledgers,
    month TEXT NOT NULL,
    amount_cent INTEGER NOT NULL CHECK(amount_cent > 0),
    PRIMARY KEY(ledger_id, month)
);
CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY,
    ledger_id INTEGER NOT NULL REFERENCES ledgers,
    operator_id INTEGER NOT NULL REFERENCES users,
    object_id INTEGER NOT NULL,
    occurred_at TEXT NOT NULL,
    action TEXT NOT NULL,
    reason TEXT NOT NULL,
    before_content TEXT,
    after_content TEXT,
    private_owner_id INTEGER REFERENCES users
);
CREATE TRIGGER IF NOT EXISTS no_audit_update BEFORE UPDATE ON audit_events
BEGIN SELECT RAISE(ABORT, 'audit events are immutable'); END;
CREATE TRIGGER IF NOT EXISTS no_audit_delete BEFORE DELETE ON audit_events
BEGIN SELECT RAISE(ABORT, 'audit events are immutable'); END;
CREATE TABLE IF NOT EXISTS security_events (
    id INTEGER PRIMARY KEY,
    occurred_at TEXT NOT NULL,
    action TEXT NOT NULL,
    phone TEXT NOT NULL DEFAULT '',
    user_id INTEGER REFERENCES users,
    detail TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS security_event_time ON security_events(occurred_at);
CREATE TRIGGER IF NOT EXISTS no_security_update BEFORE UPDATE ON security_events
BEGIN SELECT RAISE(ABORT, 'security events are immutable'); END;
CREATE TRIGGER IF NOT EXISTS no_security_delete BEFORE DELETE ON security_events
BEGIN SELECT RAISE(ABORT, 'security events are immutable'); END;
CREATE TABLE IF NOT EXISTS settlement_items (
    batch_id INTEGER NOT NULL REFERENCES batches,
    source_entry_id INTEGER NOT NULL UNIQUE REFERENCES entries,
    source_version INTEGER NOT NULL,
    snapshot TEXT NOT NULL,
    paired_entry_id INTEGER REFERENCES entries,
    inclusion_reason TEXT NOT NULL,
    PRIMARY KEY(batch_id, source_entry_id)
);
CREATE TABLE IF NOT EXISTS transfer_plans (
    batch_id INTEGER NOT NULL REFERENCES batches,
    payer_user_id INTEGER NOT NULL REFERENCES users,
    payee_user_id INTEGER NOT NULL REFERENCES users,
    amount_cent INTEGER NOT NULL CHECK(amount_cent > 0),
    PRIMARY KEY(batch_id, payer_user_id, payee_user_id)
);
"""

ROLES = {"admin", "writer", "reader"}
WRITERS = {"admin", "writer"}
MAX_CENTS = 99_999_999


BEIJING = timezone(timedelta(hours=8))


def beijing_today():
    """Return the current calendar date in Beijing."""
    return datetime.now(BEIJING).date()


def beijing_datetime(timestamp):
    """Convert a timestamp to Beijing time."""
    return datetime.fromisoformat(timestamp).astimezone(BEIJING)


def now():
    """Return an ISO-formatted UTC timestamp for persistence."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def encode(value):
    """Encode database payloads as stable JSON."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class Database:
    """Open an isolated connection per operation."""

    def __init__(self, path):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = self.connect()
        try:
            connection.executescript(SCHEMA)
            connection.execute("BEGIN IMMEDIATE")
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(users)")}
            if "username" not in columns:
                connection.execute(
                    "ALTER TABLE users ADD COLUMN username TEXT NOT NULL DEFAULT ''"
                )
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(entries)")}
            if "occurred_time" not in columns:
                connection.execute("ALTER TABLE entries ADD COLUMN occurred_time TEXT")
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(ledgers)")}
            if "dissolved_by" not in columns:
                connection.execute(
                    "ALTER TABLE ledgers ADD COLUMN dissolved_by INTEGER REFERENCES users"
                )
                connection.execute(
                    """UPDATE ledgers SET dissolved_by = (
                           SELECT operator_id FROM audit_events
                           WHERE ledger_id = ledgers.id AND action = 'dissolve'
                           ORDER BY id DESC LIMIT 1
                       ) WHERE dissolved = 1"""
                )
            if connection.execute(
                "SELECT 1 FROM security_events WHERE user_id IS NULL AND phone <> '' LIMIT 1"
            ).fetchone():
                connection.execute("DROP TRIGGER no_security_update")
                connection.execute("UPDATE security_events SET phone = '' WHERE user_id IS NULL")
                connection.execute(
                    """CREATE TRIGGER no_security_update BEFORE UPDATE ON security_events
                       BEGIN SELECT RAISE(ABORT, 'security events are immutable'); END"""
                )
            connection.commit()
        finally:
            connection.close()

    def connect(self):
        """Open SQLite with foreign keys enabled."""
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @contextmanager
    def transaction(self, write=False):
        """Commit on success, roll back on failure, and close the connection."""
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()


def access(connection, actor, ledger, roles=None, write=False):
    """Check active membership, role, and archive access for a ledger."""
    row = connection.execute(
        """SELECT m.role, l.dissolved, l.dissolved_by
           FROM memberships m JOIN ledgers l ON l.id = m.ledger_id
           WHERE m.ledger_id = ? AND m.user_id = ? AND m.left_at IS NULL""",
        (ledger, actor),
    ).fetchone()
    if row is None or (roles is not None and row["role"] not in roles):
        raise RuleError("没有此账本的操作权限")
    if row["dissolved"]:
        if write:
            raise RuleError("账本已解散")
        if row["dissolved_by"] != actor:
            raise RuleError("没有此账本的操作权限")
    return row["role"]


def record_event(
    connection, ledger, actor, action, object_id, *, reason="",
    before=None, after=None, private_owner=None,
):
    """Append a business event with its snapshots and visibility."""
    connection.execute(
        """INSERT INTO audit_events
           (ledger_id, operator_id, object_id, occurred_at, action, reason,
            before_content, after_content, private_owner_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            ledger, actor, object_id, now(), action, reason,
            None if before is None else encode(before),
            None if after is None else encode(after), private_owner,
        ),
    )


def clean_text(value, label, limit, *, required=False):
    """Trim text and enforce its required flag and length limit."""
    if not isinstance(value, str):
        raise RuleError(f"{label}必须为文本")
    value = value.strip()
    if len(value) > limit or (required and not value):
        raise RuleError(f"{label}长度不符合要求（最多 {limit} 字）")
    return value
