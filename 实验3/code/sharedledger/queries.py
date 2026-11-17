"""Ledger filtering, monthly budgets, and visible audit history."""

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

from .entries import STATUSES, TYPES, entry_snapshot, find_entry, parse_date
from .storage import MAX_CENTS, RuleError, access, record_event


@dataclass(frozen=True)
class EntryFilter:
    entry_type: str = ""
    status: str = ""
    category: str = ""
    start_date: str = ""
    end_date: str = ""
    minimum: int | None = None
    maximum: int | None = None
    submitter: int | None = None
    fund_holder: int | None = None
    participant: int | None = None


def validate_month(month):
    parse_date(f"{month}-01")
    return month


class Queries:
    def __init__(self, database, accounts):
        self.database = database
        self.accounts = accounts

    def entries(self, token, ledger, filters=None):
        actor = self.accounts.actor(token)
        filters = filters or EntryFilter()
        if filters.entry_type and filters.entry_type not in TYPES:
            raise RuleError("未知的收支类型")
        if filters.status and filters.status not in STATUSES:
            raise RuleError("未知的记录状态")
        for boundary in (filters.start_date, filters.end_date):
            if boundary:
                parse_date(boundary)
        if filters.start_date and filters.end_date and filters.start_date > filters.end_date:
            raise RuleError("开始日期不能晚于结束日期")
        if (
            filters.minimum is not None and filters.maximum is not None
            and filters.minimum > filters.maximum
        ):
            raise RuleError("最低金额不能超过最高金额")
        clauses = ["e.ledger_id = ?", "(e.ever_submitted = 1 OR e.creator_id = ?)"]
        values = [ledger, actor]
        comparisons = (
            ("e.entry_type = ?", filters.entry_type or None),
            ("e.status = ?", filters.status or None),
            ("e.category = ?", filters.category or None),
            ("e.occurred_on >= ?", filters.start_date or None),
            ("e.occurred_on <= ?", filters.end_date or None),
            ("e.amount_cent >= ?", filters.minimum),
            ("e.amount_cent <= ?", filters.maximum),
            ("e.submitter_id = ?", filters.submitter),
            ("e.fund_holder_id = ?", filters.fund_holder),
            (
                "EXISTS (SELECT 1 FROM allocations a WHERE a.entry_id = e.id "
                "AND a.member_user_id = ?)", filters.participant,
            ),
        )
        for clause, value in comparisons:
            if value is not None:
                clauses.append(clause)
                values.append(value)
        with self.database.transaction() as connection:
            access(connection, actor, ledger)
            sql = "SELECT e.* FROM entries e WHERE " + " AND ".join(clauses)
            return [
                dict(row) for row in connection.execute(sql + " ORDER BY e.id DESC", values)
            ]

    def set_budget(self, token, ledger, month, amount):
        actor = self.accounts.actor(token)
        validate_month(month)
        if type(amount) is not int or not 0 <= amount <= MAX_CENTS:
            raise RuleError("预算金额无效")
        with self.database.transaction(write=True) as connection:
            access(connection, actor, ledger, {"admin"}, write=True)
            old = connection.execute(
                "SELECT amount_cent FROM budgets WHERE ledger_id = ? AND month = ?",
                (ledger, month),
            ).fetchone()
            connection.execute(
                """INSERT INTO budgets(ledger_id, month, amount_cent) VALUES (?, ?, ?)
                   ON CONFLICT(ledger_id, month)
                   DO UPDATE SET amount_cent = excluded.amount_cent""",
                (ledger, month, amount),
            )
            record_event(
                connection, ledger, actor, "budget", ledger,
                before=None if old is None else {"month": month, "amount": old[0]},
                after={"month": month, "amount": amount},
            )

    def statistics(self, token, ledger, month):
        actor = self.accounts.actor(token)
        validate_month(month)
        totals = {"expense": 0, "income": 0}
        categories = defaultdict(int)
        members = defaultdict(lambda: {"expense": 0, "income": 0})
        with self.database.transaction() as connection:
            access(connection, actor, ledger)
            budget = connection.execute(
                "SELECT amount_cent FROM budgets WHERE ledger_id = ? AND month = ?",
                (ledger, month),
            ).fetchone()
            rows = connection.execute(
                """SELECT e.* FROM entries e WHERE e.ledger_id = ?
                   AND e.status = 'approved'""",
                (ledger,),
            ).fetchall()
            for row in rows:
                if row["kind"] == "reversal":
                    day = datetime.fromisoformat(row["approved_at"]).astimezone().date()
                    month_of_record = day.isoformat()[:7]
                    direction = -1
                else:
                    month_of_record = row["occurred_on"][:7]
                    direction = 1
                if month_of_record != month:
                    continue
                entry_type = row["entry_type"]
                totals[entry_type] += direction * row["amount_cent"]
                if entry_type == "expense":
                    categories[row["category"]] += direction * row["amount_cent"]
                for share in entry_snapshot(connection, row)["allocations"]:
                    members[share["member_user_id"]][entry_type] += (
                        direction * share["amount_cent"]
                    )
        budget_value = budget[0] if budget else None
        return {
            **totals, "budget": budget_value,
            "remaining": None if budget_value is None else budget_value - totals["expense"],
            "categories": dict(categories), "members": dict(members),
        }

    def audit(
        self, token, ledger, *, entry_id=None, operator_id=None, action="",
        object_id=None, start="", end="",
    ):
        actor = self.accounts.actor(token)
        if start:
            parse_date(start)
        if end:
            parse_date(end)
        if start and end and start > end:
            raise RuleError("开始日期不能晚于结束日期")
        with self.database.transaction() as connection:
            role = access(connection, actor, ledger)
            if entry_id is None and role != "admin":
                raise RuleError("仅管理员可查询账本操作日志")
            clauses = ["ledger_id = ?", "(private_owner_id IS NULL OR private_owner_id = ?)"]
            values = [ledger, actor]
            if entry_id is not None:
                find_entry(connection, ledger, entry_id, actor)
                clauses.append(
                    "action IN ('draft', 'submit', 'edit', 'withdraw', 'void', "
                    "'approved', 'rejected', 'request_reversal') AND object_id = ?"
                )
                values.append(entry_id)
            for clause, value in (
                ("operator_id = ?", operator_id), ("action = ?", action or None),
                ("object_id = ?", object_id),
            ):
                if value is not None:
                    clauses.append(clause)
                    values.append(value)
            sql = "SELECT * FROM audit_events WHERE " + " AND ".join(clauses)
            events = connection.execute(sql + " ORDER BY id DESC", values)
            result = []
            for row in events:
                day = datetime.fromisoformat(row["occurred_at"]).astimezone().date().isoformat()
                if (not start or day >= start) and (not end or day <= end):
                    result.append(dict(row))
            return result
