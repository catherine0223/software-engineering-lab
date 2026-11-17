"""Account sessions, ledger membership, and invitations."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import wraps
import hashlib
import hmac
import re
import secrets
from threading import RLock
import time

from .storage import ROLES, RuleError, access, clean_text, now, record_event


LOGIN_ATTEMPTS = 5
LOCK_SECONDS = 15 * 60
SESSION_SECONDS = 30 * 60
passive_read = ContextVar("passive_read", default=False)


@contextmanager
def observe_only():
    """Prevent automatic reads from extending the current session."""
    marker = passive_read.set(True)
    try:
        yield
    finally:
        passive_read.reset(marker)


def user_operation(method):
    """Renew the session only after an authenticated operation succeeds."""
    @wraps(method)
    def run(service, token, *args, **kwargs):
        result = method(service, token, *args, **kwargs)
        service.accounts.renew(token)
        return result

    return run


def password_hash(password, salt):
    """Derive a salted hash for password storage or verification."""
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 600_000)


def record_security_event(connection, action, phone, user_id=None, detail=""):
    """Write an authentication event without saving credentials."""
    connection.execute(
        """INSERT INTO security_events(occurred_at, action, phone, user_id, detail)
           VALUES (?, ?, ?, ?, ?)""",
        (now(), action, phone, user_id, detail),
    )


@dataclass(frozen=True)
class Session:
    """In-memory identity and monotonic expiration deadline."""
    user_id: int
    expires_at: float


class Accounts:
    """Register users, authenticate credentials, and manage sessions."""
    def __init__(self, database):
        self.database = database
        self.sessions = {}
        self.session_lock = RLock()

    def register(self, phone, password, username=""):
        """Create a unique account with a compliant phone number and password."""
        username = clean_text(username, "用户名", 24)
        if not isinstance(phone, str) or not re.fullmatch(r"1[3-9][0-9]{9}", phone):
            raise RuleError("请输入有效的大陆手机号（11 位，不含空格）")
        if (
            not isinstance(password, str)
            or not 8 <= len(password) <= 64
            or not re.search(r"[a-z]", password)
            or not re.search(r"[A-Z]", password)
            or not re.search(r"[0-9]", password)
        ):
            raise RuleError("密码须为 8–64 位，并同时包含大小写字母和数字")
        salt = secrets.token_bytes(16)
        digest = password_hash(password, salt)
        with self.database.transaction(write=True) as connection:
            if connection.execute(
                "SELECT 1 FROM users WHERE phone = ?", (phone,)
            ).fetchone():
                raise RuleError("该手机号已注册，请登录")
            cursor = connection.execute(
                "INSERT INTO users(phone, salt, password_hash, username) VALUES (?, ?, ?, ?)",
                (phone, salt, digest, username),
            )
            return cursor.lastrowid

    def login(self, phone, password):
        """Validate credentials, apply lockout rules, and issue a session token."""
        timestamp = int(time.time())
        phone = phone if isinstance(phone, str) else ""
        password = password if isinstance(password, str) else ""
        valid_password = 1 <= len(password) <= 64
        user_id = None
        locked = False
        with self.database.transaction(write=True) as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE phone = ?", (phone,)
            ).fetchone()
            if row is not None and row["locked_until"] > timestamp:
                locked = True
                record_security_event(
                    connection, "login_blocked", phone, row["id"],
                    f"locked_until={row['locked_until']}",
                )
            elif row is not None and valid_password and hmac.compare_digest(
                password_hash(password, row["salt"]), row["password_hash"]
            ):
                user_id = row["id"]
                connection.execute(
                    "UPDATE users SET failures = 0, locked_until = 0 WHERE id = ?",
                    (user_id,),
                )
            elif row is not None:
                failures = 1 if row["locked_until"] else row["failures"] + 1
                locked = failures >= LOGIN_ATTEMPTS
                connection.execute(
                    "UPDATE users SET failures = ?, locked_until = ? WHERE id = ?",
                    (failures, timestamp + LOCK_SECONDS if locked else 0, row["id"]),
                )
                record_security_event(
                    connection, "login_failed", phone, row["id"],
                    f"failures={failures}",
                )
                if locked:
                    record_security_event(
                        connection, "login_locked", phone, row["id"],
                        f"locked_until={timestamp + LOCK_SECONDS}",
                    )
            else:
                record_security_event(
                    connection, "login_failed", "", None, "unknown account"
                )
        if locked:
            raise RuleError("登录失败次数过多，请 15 分钟后重试")
        if user_id is None:
            raise RuleError("手机号或密码错误")
        token = secrets.token_urlsafe(32)
        with self.session_lock:
            self.sessions[token] = Session(user_id, time.monotonic() + SESSION_SECONDS)
        return token

    def security_events(self, *, phone=None, action=None, limit=200):
        """Read authentication events for local maintenance."""
        clauses, values = [], []
        if phone is not None:
            clauses.append("phone = ?")
            values.append(phone)
        if action is not None:
            clauses.append("action = ?")
            values.append(action)
        sql = "SELECT * FROM security_events"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id DESC LIMIT ?"
        values.append(limit)
        with self.database.transaction() as connection:
            return [dict(row) for row in connection.execute(sql, values)]

    def logout(self, token):
        """Discard the session associated with a token."""
        with self.session_lock:
            self.sessions.pop(token, None)

    def username(self, token):
        """Return the current user's display name."""
        actor = self.actor(token)
        with self.database.transaction() as connection:
            return connection.execute(
                "SELECT username FROM users WHERE id = ?", (actor,)
            ).fetchone()[0]

    def set_username(self, token, username):
        """Change the current user's display name and renew the session."""
        actor = self.actor(token)
        username = clean_text(username, "用户名", 24, required=True)
        with self.database.transaction(write=True) as connection:
            connection.execute(
                "UPDATE users SET username = ? WHERE id = ?", (username, actor)
            )
        self.renew(token)

    def actor(self, token):
        """Resolve a valid token to a user ID, expiring idle sessions."""
        with self.session_lock:
            session = self.sessions.get(token)
            if session is None or session.expires_at <= time.monotonic():
                self.sessions.pop(token, None)
                raise RuleError("请重新登录")
            return session.user_id

    def renew(self, token):
        """Extend a live session after an effective user operation."""
        if passive_read.get():
            return
        with self.session_lock:
            session = self.sessions.get(token)
            if session is not None:
                self.sessions[token] = Session(session.user_id, time.monotonic() + SESSION_SECONDS)


class Ledgers:
    """Manage ledger membership, invitations, and dissolution."""
    def __init__(self, database, accounts):
        self.database = database
        self.accounts = accounts

    @user_operation
    def create(self, token, name):
        """Create a ledger and its initial administrator in one transaction."""
        actor = self.accounts.actor(token)
        name = clean_text(name, "账本名称", 60, required=True)
        with self.database.transaction(write=True) as connection:
            ledger = connection.execute(
                "INSERT INTO ledgers(name) VALUES (?)", (name,)
            ).lastrowid
            self._join(connection, ledger, actor, "admin")
            record_event(connection, ledger, actor, "create_ledger", ledger)
            return ledger

    @user_operation
    def list(self, token):
        """List the ledgers the current member is allowed to open."""
        actor = self.accounts.actor(token)
        with self.database.transaction() as connection:
            return [
                dict(row) for row in connection.execute(
                    """SELECT l.*, m.role FROM ledgers l
                       JOIN memberships m ON m.ledger_id = l.id
                       WHERE m.user_id = ? AND m.left_at IS NULL
                         AND (l.dissolved = 0 OR l.dissolved_by = m.user_id)
                       ORDER BY l.id""",
                    (actor,),
                )
            ]

    @user_operation
    def members(self, token, ledger):
        """List current and historical memberships for an accessible ledger."""
        actor = self.accounts.actor(token)
        with self.database.transaction() as connection:
            access(connection, actor, ledger)
            return [
                dict(row) for row in connection.execute(
                    """SELECT m.*, u.phone, u.username FROM memberships m
                       JOIN users u ON u.id = m.user_id
                       WHERE m.ledger_id = ? ORDER BY m.id""",
                    (ledger,),
                )
            ]

    @user_operation
    def invite(self, token, ledger, role):
        """Issue a single-use invitation with a specified role and expiry."""
        actor = self.accounts.actor(token)
        self._require_role(role)
        invitation = secrets.token_urlsafe(24)
        expires = (datetime.now(timezone.utc) + timedelta(hours=168)).isoformat(
            timespec="seconds"
        )
        with self.database.transaction(write=True) as connection:
            access(connection, actor, ledger, {"admin"}, write=True)
            connection.execute(
                """INSERT INTO invitations(token, ledger_id, role, expires_at)
                   VALUES (?, ?, ?, ?)""",
                (invitation, ledger, role, expires),
            )
            record_event(
                connection, ledger, actor, "invite", ledger, after={"role": role}
            )
        return invitation

    @user_operation
    def accept(self, token, invitation):
        """Join with the invited role and consume the invitation atomically."""
        actor = self.accounts.actor(token)
        with self.database.transaction(write=True) as connection:
            row = connection.execute(
                """SELECT i.*, l.dissolved FROM invitations i
                   JOIN ledgers l ON l.id = i.ledger_id WHERE i.token = ?""",
                (invitation.strip(),),
            ).fetchone()
            timestamp = now()
            if row is None or row["used_at"] or row["expires_at"] <= timestamp:
                raise RuleError("邀请无效、过期或已使用")
            if row["dissolved"]:
                raise RuleError("账本已解散")
            ledger = row["ledger_id"]
            active = connection.execute(
                """SELECT 1 FROM memberships WHERE ledger_id = ?
                   AND user_id = ? AND left_at IS NULL""",
                (ledger, actor),
            ).fetchone()
            if active:
                raise RuleError("你已加入此账本")
            self._join(connection, ledger, actor, row["role"])
            connection.execute(
                "UPDATE invitations SET used_at = ? WHERE token = ?",
                (timestamp, row["token"]),
            )
            record_event(connection, ledger, actor, "join", actor)
            return ledger

    def change_role(self, token, ledger, member_id, role):
        """Change an active member's role while preserving an administrator."""
        self._require_role(role)
        self._change_member(token, ledger, member_id, role, leaving=False)

    def remove(self, token, ledger, member_id):
        """End another user's active membership as a ledger administrator."""
        self._change_member(token, ledger, member_id, None, leaving=False)

    def leave(self, token, ledger):
        """End the current user's membership while preserving their history."""
        actor = self.accounts.actor(token)
        self._change_member(token, ledger, actor, None, leaving=True)

    @user_operation
    def _change_member(self, token, ledger, member_id, role, leaving):
        actor = self.accounts.actor(token)
        with self.database.transaction(write=True) as connection:
            access(connection, actor, ledger, None if leaving else {"admin"}, write=True)
            if role is None and not leaving and member_id == actor:
                raise RuleError("不能移除自己，请使用「退出账本」")
            member = connection.execute(
                """SELECT id, role FROM memberships WHERE ledger_id = ?
                   AND user_id = ? AND left_at IS NULL""",
                (ledger, member_id),
            ).fetchone()
            if member is None:
                raise RuleError("成员已退出或不存在")
            if member["role"] == role:
                return
            if member["role"] == "admin" and role != "admin":
                count = connection.execute(
                    """SELECT count(*) FROM memberships WHERE ledger_id = ?
                       AND role = 'admin' AND left_at IS NULL""",
                    (ledger,),
                ).fetchone()[0]
                if count == 1:
                    raise RuleError("请先指定另一位管理员")
            if role is None:
                connection.execute(
                    "UPDATE memberships SET left_at = ? WHERE id = ?",
                    (now(), member["id"]),
                )
            else:
                connection.execute(
                    "UPDATE memberships SET role = ? WHERE id = ?",
                    (role, member["id"]),
                )
            action = "leave" if leaving else "remove" if role is None else "role"
            record_event(
                connection, ledger, actor, action, member_id,
                before={"role": member["role"]}, after={"role": role},
            )

    @user_operation
    def dissolve_info(self, token, ledger):
        """Return the eligibility counts for the confirmation dialog."""
        actor = self.accounts.actor(token)
        with self.database.transaction() as connection:
            access(connection, actor, ledger, {"admin"})
            return self._dissolve_counts(connection, ledger)

    @staticmethod
    def _dissolve_counts(connection, ledger):
        members = connection.execute(
            "SELECT count(*) FROM memberships WHERE ledger_id = ? AND left_at IS NULL",
            (ledger,),
        ).fetchone()[0]
        pending = connection.execute(
            "SELECT count(*) FROM entries WHERE ledger_id = ? AND status = 'pending'",
            (ledger,),
        ).fetchone()[0]
        unsettled = connection.execute(
            """SELECT count(*) FROM entries WHERE ledger_id = ?
               AND status = 'approved' AND batch_id IS NULL""",
            (ledger,),
        ).fetchone()[0]
        transfers = connection.execute(
            """SELECT count(*) FROM transfer_plans t
               JOIN batches b ON b.id = t.batch_id WHERE b.ledger_id = ?""",
            (ledger,),
        ).fetchone()[0]
        return {
            "members": members, "pending": pending,
            "unsettled": unsettled, "transfers": transfers,
        }

    @user_operation
    def dissolve(self, token, ledger, offline_confirmed=False):
        """Archive an eligible ledger and record any offline confirmation."""
        actor = self.accounts.actor(token)
        with self.database.transaction(write=True) as connection:
            access(connection, actor, ledger, {"admin"}, write=True)
            counts = self._dissolve_counts(connection, ledger)
            if counts["members"] > 1:
                raise RuleError("仅剩管理员本人时才能解散账本")
            if counts["pending"] or counts["unsettled"]:
                raise RuleError("请先处理待审核和未结算记录")
            plans = [
                {
                    "batch_id": row["batch_id"], "payer_id": row["payer_user_id"],
                    "payee_id": row["payee_user_id"], "amount_cent": row["amount_cent"],
                }
                for row in connection.execute(
                    """SELECT t.batch_id, t.payer_user_id, t.payee_user_id, t.amount_cent
                       FROM transfer_plans t JOIN batches b ON b.id = t.batch_id
                       WHERE b.ledger_id = ?
                       ORDER BY t.batch_id, t.payer_user_id, t.payee_user_id""",
                    (ledger,),
                )
            ]
            if plans and not offline_confirmed:
                raise RuleError("存在涉及退出成员的结算方案，请确认已线下处理")
            if plans:
                record_event(
                    connection, ledger, actor, "confirm_offline", ledger,
                    reason="管理员确认历史结算方案已在线下处理",
                    after={"plans": plans},
                )
            connection.execute(
                "UPDATE ledgers SET dissolved = 1, dissolved_by = ? WHERE id = ?", (actor, ledger)
            )
            record_event(
                connection, ledger, actor, "dissolve", ledger,
                after={"members": counts["members"],
                       "offline_confirmed": bool(counts["transfers"])},
            )

    @staticmethod
    def _require_role(role):
        if role not in ROLES:
            raise RuleError("角色应为管理员、记账成员或只读成员")

    @staticmethod
    def _join(connection, ledger, user_id, role):
        connection.execute(
            """INSERT INTO memberships(ledger_id, user_id, role, joined_at)
               VALUES (?, ?, ?, ?)""",
            (ledger, user_id, role, now()),
        )
