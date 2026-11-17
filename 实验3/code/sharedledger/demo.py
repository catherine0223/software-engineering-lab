"""Prepare sample accounts in the separate demo database."""

import secrets

from .accounts import password_hash
from .storage import RuleError, now


DEMO_ACCOUNTS = {
    "admin": ("13800009001", "演示管理员"),
    "writer": ("13800009002", "演示记账成员"),
}
DEMO_PASSWORD = "LedgerDemo2026"


def demo_login(app, role):
    """Initialize the separate demo database and sign in with its chosen role."""
    database = app.accounts.database
    with database.transaction(write=True) as connection:
        initialized = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'demo_initialized'"
        ).fetchone()
        if not initialized:
            if connection.execute("SELECT 1 FROM users LIMIT 1").fetchone():
                raise RuleError("演示数据库位置已有其他账号，请先移开该文件")
            ledger = connection.execute(
                "INSERT INTO ledgers(name) VALUES (?)", ("演示账本",)
            ).lastrowid
            timestamp = now()
            for member_role, (phone, username) in DEMO_ACCOUNTS.items():
                salt = secrets.token_bytes(16)
                user = connection.execute(
                    """INSERT INTO users(phone, username, salt, password_hash)
                       VALUES (?, ?, ?, ?)""",
                    (phone, username, salt, password_hash(DEMO_PASSWORD, salt)),
                ).lastrowid
                connection.execute(
                    """INSERT INTO memberships(ledger_id, user_id, role, joined_at)
                       VALUES (?, ?, ?, ?)""",
                    (ledger, user, member_role, timestamp),
                )
            connection.execute("CREATE TABLE demo_initialized (version INTEGER)")
            connection.execute("INSERT INTO demo_initialized VALUES (1)")
    return app.accounts.login(DEMO_ACCOUNTS[role][0], DEMO_PASSWORD)
