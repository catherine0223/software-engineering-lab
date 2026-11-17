"""Entry lifecycle, participant validation, and amount allocation."""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import re

from .storage import MAX_CENTS, WRITERS, RuleError, access, clean_text, record_event


EDITABLE = {"draft", "pending", "rejected", "withdrawn"}
TYPES = {"expense", "income"}
STATUSES = EDITABLE | {"approved", "voided"}


def cents(text):
    text = text.strip()
    if not re.fullmatch(r"[0-9]{1,10}(?:\.[0-9]{1,2})?", text):
        raise RuleError("金额须为非负数字，最多保留两位小数")
    try:
        value = int(Decimal(text) * 100)
    except InvalidOperation as error:
        raise RuleError("金额格式不正确") from error
    if value > MAX_CENTS:
        raise RuleError("金额超出允许范围")
    return value


def money(amount):
    if amount is None:
        return ""
    sign = "-" if amount < 0 else ""
    whole, fraction = divmod(abs(amount), 100)
    return f"{sign}{whole}.{fraction:02d}"


def parse_date(text):
    try:
        value = date.fromisoformat(text)
    except (TypeError, ValueError) as error:
        raise RuleError("日期格式应为 YYYY-MM-DD") from error
    if value.isoformat() != text:
        raise RuleError("日期格式应为 YYYY-MM-DD")
    return value


def allocate_evenly(amount, member_ids):
    member_ids = tuple(member_ids)
    if not member_ids or len(set(member_ids)) != len(member_ids):
        raise RuleError("请选至少一位成员，且不得重复")
    if type(amount) is not int or not 0 < amount <= MAX_CENTS:
        raise RuleError("请输入有效金额")
    base, remainder = divmod(amount, len(member_ids))
    return tuple(
        (member, base + (remainder if index == 0 else 0))
        for index, member in enumerate(member_ids)
    )


@dataclass(frozen=True)
class EntryInput:
    title: str = ""
    category: str = ""
    occurred_on: str | None = None
    amount_cent: int | None = None
    entry_type: str = "expense"
    fund_holder_id: int | None = None
    allocations: tuple = ()
    notes: str = ""

    def normalized(self, complete):
        if self.entry_type not in TYPES:
            raise RuleError("请选择收入或支出")
        title = clean_text(self.title, "账目名称", 80, required=complete)
        category = clean_text(self.category, "分类", 40, required=complete)
        notes = clean_text(self.notes, "备注", 400)
        occurred_on = self.occurred_on or None
        if occurred_on is not None:
            parse_date(occurred_on)
        amount = self.amount_cent
        if amount is not None and (type(amount) is not int or not 0 < amount <= MAX_CENTS):
            raise RuleError("总金额应为允许范围内的正数")
        fund_holder = self.fund_holder_id
        if fund_holder is not None and (type(fund_holder) is not int or fund_holder <= 0):
            raise RuleError("垫付人或代收人编号无效")
        seen = set()
        for member, share in self.allocations:
            if type(member) is not int or member <= 0 or member in seen:
                raise RuleError("分配成员编号无效或重复")
            if type(share) is not int or not 0 <= share <= MAX_CENTS:
                raise RuleError("分配金额应为允许范围内的非负数")
            seen.add(member)
        if complete:
            if None in (occurred_on, amount, fund_holder) or not seen:
                raise RuleError("提交时须填写日期、总金额、资金角色和分配明细")
            if sum(share for _, share in self.allocations) != amount:
                raise RuleError("分配明细合计必须等于总金额")
        return EntryInput(
            title, category, occurred_on, amount, self.entry_type,
            fund_holder, tuple(self.allocations), notes,
        )


def entry_snapshot(connection, row):
    result = dict(row)
    result["allocations"] = [
        dict(allocation) for allocation in connection.execute(
            """SELECT member_user_id, amount_cent, selection_order
               FROM allocations WHERE entry_id = ? ORDER BY selection_order""",
            (row["id"],),
        )
    ]
    return result


def find_entry(connection, ledger, entry_id, actor):
    row = connection.execute(
        "SELECT * FROM entries WHERE ledger_id = ? AND id = ?", (ledger, entry_id)
    ).fetchone()
    if row is None or (not row["ever_submitted"] and row["creator_id"] != actor):
        raise RuleError("记录不存在或无权查看")
    return row


def require_version(row, expected_version):
    if row["version"] != expected_version:
        raise RuleError("记录版本已变化，请刷新后重试")


def add_allocations(connection, entry_id, allocations):
    connection.executemany(
        """INSERT INTO allocations
           (entry_id, member_user_id, amount_cent, selection_order)
           VALUES (?, ?, ?, ?)""",
        (
            (entry_id, member, share, position)
            for position, (member, share) in enumerate(allocations, start=1)
        ),
    )


def save_entry_event(connection, ledger, actor, action, entry_id, before=None, reason=""):
    row = find_entry(connection, ledger, entry_id, actor)
    record_event(
        connection, ledger, actor, action, entry_id, reason=reason,
        before=before, after=entry_snapshot(connection, row),
        private_owner=None if row["ever_submitted"] else row["creator_id"],
    )


def validate_participants(connection, ledger, fields):
    rows = connection.execute(
        "SELECT user_id, joined_at, left_at FROM memberships WHERE ledger_id = ?",
        (ledger,),
    ).fetchall()
    member_ids = {member for member, _ in fields.allocations}
    if fields.fund_holder_id is not None:
        member_ids.add(fields.fund_holder_id)

    def on_date(timestamp):
        return datetime.fromisoformat(timestamp).astimezone().date().isoformat()

    if fields.occurred_on is None:
        eligible = {row["user_id"] for row in rows if row["left_at"] is None}
    else:
        eligible = {
            row["user_id"] for row in rows
            if on_date(row["joined_at"]) <= fields.occurred_on
            and (row["left_at"] is None or fields.occurred_on <= on_date(row["left_at"]))
        }
    if not member_ids <= eligible:
        raise RuleError("垫付人、代收人和分配成员必须在发生日期具有成员资格")


class Entries:
    def __init__(self, database, accounts):
        self.database = database
        self.accounts = accounts

    def get(self, token, ledger, entry_id):
        actor = self.accounts.actor(token)
        with self.database.transaction() as connection:
            access(connection, actor, ledger)
            return entry_snapshot(connection, find_entry(connection, ledger, entry_id, actor))

    def create(self, token, ledger, fields, *, submit=False, correction_for=None):
        actor = self.accounts.actor(token)
        fields = fields.normalized(submit)
        with self.database.transaction(write=True) as connection:
            access(connection, actor, ledger, WRITERS, write=True)
            validate_participants(connection, ledger, fields)
            kind = "ordinary"
            if correction_for is not None:
                reversal = find_entry(connection, ledger, correction_for, actor)
                if reversal["kind"] != "reversal" or reversal["status"] != "approved":
                    raise RuleError("补记须关联一条已通过的冲销")
                if connection.execute(
                    "SELECT 1 FROM entries WHERE corrected_reversal_id = ?",
                    (correction_for,),
                ).fetchone():
                    raise RuleError("此冲销已有关联补记")
                kind = "correction"
            entry_id = connection.execute(
                """INSERT INTO entries
                   (ledger_id, creator_id, submitter_id, fund_holder_id, title,
                    category, occurred_on, amount_cent, entry_type, kind,
                    status, ever_submitted, notes, corrected_reversal_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    ledger, actor, actor if submit else None, fields.fund_holder_id,
                    fields.title, fields.category, fields.occurred_on,
                    fields.amount_cent, fields.entry_type, kind,
                    "pending" if submit else "draft", int(submit), fields.notes,
                    correction_for,
                ),
            ).lastrowid
            add_allocations(connection, entry_id, fields.allocations)
            save_entry_event(
                connection, ledger, actor, "submit" if submit else "draft", entry_id
            )
            return entry_id

    def edit(self, token, ledger, entry_id, version, fields, *, submit=False):
        actor = self.accounts.actor(token)
        with self.database.transaction(write=True) as connection:
            access(connection, actor, ledger, WRITERS, write=True)
            row = find_entry(connection, ledger, entry_id, actor)
            require_version(row, version)
            if (
                row["creator_id"] != actor or row["kind"] == "reversal"
                or row["status"] not in EDITABLE
            ):
                raise RuleError("此记录不能编辑")
            if submit and row["status"] == "pending":
                raise RuleError("此记录已在待审核队列，可直接保存修改")
            fields = fields.normalized(submit or row["status"] != "draft")
            validate_participants(connection, ledger, fields)
            before = entry_snapshot(connection, row)
            connection.execute(
                """UPDATE entries SET title = ?, category = ?, occurred_on = ?,
                   amount_cent = ?, entry_type = ?, fund_holder_id = ?, notes = ?,
                   status = ?, submitter_id = ?, ever_submitted = ?, reason = ?,
                   version = version + 1 WHERE id = ?""",
                (
                    fields.title, fields.category, fields.occurred_on,
                    fields.amount_cent, fields.entry_type, fields.fund_holder_id,
                    fields.notes, "pending" if submit else row["status"],
                    actor if submit else row["submitter_id"],
                    1 if submit else row["ever_submitted"],
                    "" if submit else row["reason"], entry_id,
                ),
            )
            connection.execute("DELETE FROM allocations WHERE entry_id = ?", (entry_id,))
            add_allocations(connection, entry_id, fields.allocations)
            # A public submission event must not reveal earlier private draft content.
            public_before = before if row["ever_submitted"] else None
            save_entry_event(
                connection, ledger, actor, "submit" if submit else "edit",
                entry_id, public_before if submit else before,
            )

    def withdraw(self, token, ledger, entry_id, version):
        actor = self.accounts.actor(token)
        with self.database.transaction(write=True) as connection:
            access(connection, actor, ledger, WRITERS, write=True)
            row = find_entry(connection, ledger, entry_id, actor)
            require_version(row, version)
            if row["submitter_id"] != actor or row["status"] != "pending":
                raise RuleError("只有提交人可以撤回待审核记录")
            before = entry_snapshot(connection, row)
            connection.execute(
                """UPDATE entries SET status = 'withdrawn', version = version + 1
                   WHERE id = ?""",
                (entry_id,),
            )
            save_entry_event(connection, ledger, actor, "withdraw", entry_id, before)

    def void(self, token, ledger, entry_id, version, reason):
        actor = self.accounts.actor(token)
        reason = clean_text(reason, "作废原因", 400, required=True)
        with self.database.transaction(write=True) as connection:
            role = access(connection, actor, ledger, write=True)
            row = find_entry(connection, ledger, entry_id, actor)
            require_version(row, version)
            if row["status"] == "draft":
                allowed = row["creator_id"] == actor
            elif row["status"] == "approved":
                allowed = (
                    role == "admin" and row["batch_id"] is None
                    and row["kind"] != "reversal"
                    and not active_reversal(connection, entry_id)
                )
            else:
                allowed = role == "admin" and row["status"] in EDITABLE
            if not allowed:
                raise RuleError("此记录不能作废")
            before = entry_snapshot(connection, row)
            connection.execute(
                """UPDATE entries SET status = 'voided', version = version + 1,
                   reason = ? WHERE id = ?""",
                (reason, entry_id),
            )
            save_entry_event(connection, ledger, actor, "void", entry_id, before, reason)


def active_reversal(connection, entry_id):
    return connection.execute(
        """SELECT id, status FROM entries WHERE original_entry_id = ?
           AND kind = 'reversal' AND status IN ('pending', 'approved')""",
        (entry_id,),
    ).fetchone()
