"""Account sessions, ledger membership, and invitations."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import re
import secrets
import time

from .storage import ROLES, RuleError, access, clean_text, now, record_event


LOGIN_ATTEMPTS = 5
LOCK_SECONDS = 15 * 60
SESSION_SECONDS = 8 * 60 * 60


def password_hash(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 600_000)


@dataclass(frozen=True)
class Session:
    user_id: int
    expires_at: float


class Accounts:
    def __init__(self, database):
        self.database = database
        self.sessions = {}

    def register(self, phone, password):
        phone = phone.strip()
        if not re.fullmatch(r"1[3-9][0-9]{9}", phone):
            raise RuleError("请输入有效的大陆手机号")
        if (
            not 8 <= len(password) <= 64
            or not re.search(r"[A-Za-z]", password)
            or not re.search(r"[0-9]", password)
        ):
            raise RuleError("密码须为 8–64 位，并含英文字母和数字")
        salt = secrets.token_bytes(16)
        digest = password_hash(password, salt)
        with self.database.transaction(write=True) as connection:
            if connection.execute(
                "SELECT 1 FROM users WHERE phone = ?", (phone,)
            ).fetchone():
                raise RuleError("此手机号已注册")
            cursor = connection.execute(
                "INSERT INTO users(phone, salt, password_hash) VALUES (?, ?, ?)",
                (phone, salt, digest),
            )
            return cursor.lastrowid

    def login(self, phone, password):
        if not 1 <= len(password) <= 64:
            raise RuleError("手机号或密码错误")
        timestamp = int(time.time())
        user_id = None
        locked = False
        with self.database.transaction(write=True) as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE phone = ?", (phone.strip(),)
            ).fetchone()
            if row is not None and row["locked_until"] > timestamp:
                locked = True
            elif row is not None and hmac.compare_digest(
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
        if locked:
            raise RuleError("登录失败次数过多，请 15 分钟后重试")
        if user_id is None:
            raise RuleError("手机号或密码错误")
        token = secrets.token_urlsafe(32)
        self.sessions[token] = Session(user_id, time.monotonic() + SESSION_SECONDS)
        return token

    def logout(self, token):
        self.sessions.pop(token, None)

    def actor(self, token):
        session = self.sessions.get(token)
        if session is None or session.expires_at <= time.monotonic():
            self.logout(token)
            raise RuleError("请重新登录")
        return session.user_id


class Ledgers:
    def __init__(self, database, accounts):
        self.database = database
        self.accounts = accounts

    def create(self, token, name):
        actor = self.accounts.actor(token)
        name = clean_text(name, "账本名称", 60, required=True)
        with self.database.transaction(write=True) as connection:
            ledger = connection.execute(
                "INSERT INTO ledgers(name) VALUES (?)", (name,)
            ).lastrowid
            self._join(connection, ledger, actor, "admin")
            record_event(connection, ledger, actor, "create_ledger", ledger)
            return ledger

    def list(self, token):
        actor = self.accounts.actor(token)
        with self.database.transaction() as connection:
            return [
                dict(row) for row in connection.execute(
                    """SELECT l.*, m.role FROM ledgers l
                       JOIN memberships m ON m.ledger_id = l.id
                       WHERE m.user_id = ? AND m.left_at IS NULL ORDER BY l.id""",
                    (actor,),
                )
            ]

    def members(self, token, ledger):
        actor = self.accounts.actor(token)
        with self.database.transaction() as connection:
            access(connection, actor, ledger)
            return [
                dict(row) for row in connection.execute(
                    """SELECT m.*, u.phone FROM memberships m
                       JOIN users u ON u.id = m.user_id
                       WHERE m.ledger_id = ? ORDER BY m.id""",
                    (ledger,),
                )
            ]

    def invite(self, token, ledger, role):
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

    def accept(self, token, invitation):
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
        self._require_role(role)
        self._change_member(token, ledger, member_id, role, leaving=False)

    def remove(self, token, ledger, member_id):
        self._change_member(token, ledger, member_id, None, leaving=False)

    def leave(self, token, ledger):
        actor = self.accounts.actor(token)
        self._change_member(token, ledger, actor, None, leaving=True)

    def _change_member(self, token, ledger, member_id, role, leaving):
        actor = self.accounts.actor(token)
        with self.database.transaction(write=True) as connection:
            access(connection, actor, ledger, None if leaving else {"admin"}, write=True)
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

    def dissolve(self, token, ledger):
        actor = self.accounts.actor(token)
        with self.database.transaction(write=True) as connection:
            access(connection, actor, ledger, {"admin"}, write=True)
            open_entries = connection.execute(
                """SELECT 1 FROM entries WHERE ledger_id = ?
                   AND (status = 'pending' OR (status = 'approved' AND batch_id IS NULL))
                   LIMIT 1""",
                (ledger,),
            ).fetchone()
            if open_entries:
                raise RuleError("请先处理待审核和未结算记录")
            connection.execute("UPDATE ledgers SET dissolved = 1 WHERE id = ?", (ledger,))
            record_event(connection, ledger, actor, "dissolve", ledger)

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
