"""Ledger filtering, monthly budgets, and visible audit history."""

from collections import defaultdict
from dataclasses import dataclass
import re

from .accounts import user_operation
from .entries import STATUSES, TYPES, entry_snapshot, find_entry, parse_date
from .storage import MAX_CENTS, RuleError, access, beijing_datetime, record_event


APPROVED_REVERSAL = """EXISTS (
    SELECT 1 FROM entries r WHERE r.ledger_id = e.ledger_id
    AND r.original_entry_id = e.id AND r.kind = 'reversal' AND r.status = 'approved'
)"""


@dataclass(frozen=True)
class EntryFilter:
    """Optional date, amount, member, and status criteria for entry queries."""
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
    start_time: str = ""
    end_time: str = ""


TIME_PATTERN = r"(?:[01][0-9]|2[0-3]):[0-5][0-9]"


def parse_boundary(value, default_time, label):
    """Parse a query boundary at Beijing minute precision."""
    text = (value or "").strip()
    if not text:
        return None
    day, separator, minute = text.partition("T")
    if not separator:
        day, _, minute = text.partition(" ")
    parse_date(day)
    minute = minute.strip()
    if minute and not re.fullmatch(TIME_PATTERN, minute):
        raise RuleError(f"{label}时间格式应为 HH:MM")
    return day, minute or default_time


def parse_day(value):
    """Extract a calendar date from a statistics boundary."""
    text = (value or "").strip()
    day = text.split("T", 1)[0].split(" ", 1)[0]
    return parse_date(day).isoformat()


def boundary_value(date_value, time_value, default_time, label):
    """Combine the chosen date with an optional time of day."""
    text = (date_value or "").strip()
    extra = (time_value or "").strip()
    if extra:
        if not text:
            raise RuleError(f"请先填写{label}日期")
        day = text.split("T", 1)[0].split(" ", 1)[0]
        parse_date(day)
        if not re.fullmatch(TIME_PATTERN, extra):
            raise RuleError(f"{label}时间格式应为 HH:MM")
        return day, extra
    return parse_boundary(text, default_time, label)


def validate_month(month):
    """Validate and return the canonical month key."""
    parse_date(f"{month}-01")
    return month


class Queries:
    """Read ledger data and manage monthly budget amounts."""
    def __init__(self, database, accounts):
        self.database = database
        self.accounts = accounts

    @user_operation
    def entries(self, token, ledger, filters=None):
        """List visible records satisfying all supplied filter criteria."""
        actor = self.accounts.actor(token)
        filters = filters or EntryFilter()
        if filters.entry_type and filters.entry_type not in TYPES:
            raise RuleError("未知的收支类型")
        if filters.status and filters.status not in STATUSES | {"reversed"}:
            raise RuleError("未知的记录状态")
        start = boundary_value(filters.start_date, filters.start_time, "00:00", "开始")
        end = boundary_value(filters.end_date, filters.end_time, "23:59", "结束")
        if start and end:
            if f"{start[0]}T{start[1]}" > f"{end[0]}T{end[1]}":
                raise RuleError("开始时间不能晚于结束时间")
        if (
            filters.minimum is not None and filters.maximum is not None
            and filters.minimum > filters.maximum
        ):
            raise RuleError("最低金额不能超过最高金额")
        if any(
            amount is not None and amount < 0
            for amount in (filters.minimum, filters.maximum)
        ):
            raise RuleError("金额范围不能为负")
        clauses = ["e.ledger_id = ?", "(e.ever_submitted = 1 OR e.creator_id = ?)"]
        values = [ledger, actor]
        if filters.status == "reversed":
            clauses.append(APPROVED_REVERSAL)
        if start:
            clauses.append(
                "(e.occurred_on IS NOT NULL AND e.occurred_on || 'T' "
                "|| COALESCE(e.occurred_time, '00:00') >= ?)"
            )
            values.append(f"{start[0]}T{start[1]}")
        if end:
            clauses.append(
                "(e.occurred_on IS NOT NULL AND e.occurred_on || 'T' "
                "|| COALESCE(e.occurred_time, '00:00') <= ?)"
            )
            values.append(f"{end[0]}T{end[1]}")
        comparisons = (
            ("e.entry_type = ?", filters.entry_type or None),
            ("e.status = ?", None if filters.status == "reversed" else filters.status or None),
            ("e.category = ?", filters.category or None),
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
            sql = (
                "SELECT e.*, (SELECT COUNT(*) FROM allocations a WHERE a.entry_id = e.id)"
                f" AS participant_count, {APPROVED_REVERSAL} AS is_reversed"
                " FROM entries e WHERE " + " AND ".join(clauses)
            )
            return [
                dict(row) for row in connection.execute(sql + " ORDER BY e.id DESC", values)
            ]

    def pending_review_count(self, token, ledger):
        """Count pending entries this administrator can review."""
        actor = self.accounts.actor(token)
        with self.database.transaction() as connection:
            access(connection, actor, ledger, {"admin"})
            return connection.execute(
                """SELECT COUNT(*) FROM entries WHERE ledger_id = ? AND status = 'pending'
                   AND ever_submitted = 1 AND submitter_id <> ?""",
                (ledger, actor),
            ).fetchone()[0]

    @user_operation
    def set_budget(self, token, ledger, month, amount):
        """Set a positive monthly budget and record the change."""
        actor = self.accounts.actor(token)
        validate_month(month)
        if type(amount) is not int or not 0 < amount <= MAX_CENTS:
            raise RuleError("预算金额须大于 0 且不超过 999999.99 元")
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

    @user_operation
    def statistics(self, token, ledger, month=None, *, start=None, end=None):
        """Summarize approved entries by original period, category, and member."""
        actor = self.accounts.actor(token)
        range_mode = start is not None or end is not None
        if range_mode:
            if not start or not end:
                raise RuleError("请同时填写开始日期和结束日期")
            start_day = parse_day(start)
            end_day = parse_day(end)
            if start_day > end_day:
                raise RuleError("开始日期不能晚于结束日期")
        else:
            if not month:
                raise RuleError("请填写统计月份")
            validate_month(month)
        totals = {"expense": 0, "income": 0}
        categories = defaultdict(lambda: {"expense": 0, "income": 0})
        members = defaultdict(lambda: {"expense": 0, "income": 0, "paid": 0, "collected": 0})
        occurrence = (
            "CASE WHEN e.kind = 'reversal' THEN original.occurred_on "
            "ELSE e.occurred_on END"
        )
        with self.database.transaction() as connection:
            access(connection, actor, ledger)
            if range_mode:
                budget = None
                rows = connection.execute(
                    f"""SELECT e.* FROM entries e
                        LEFT JOIN entries original
                            ON original.id = e.original_entry_id
                           AND original.ledger_id = e.ledger_id
                        WHERE e.ledger_id = ? AND e.status = 'approved'
                          AND {occurrence} BETWEEN ? AND ?""",
                    (ledger, start_day, end_day),
                ).fetchall()
            else:
                budget_row = connection.execute(
                    "SELECT amount_cent FROM budgets WHERE ledger_id = ? AND month = ?",
                    (ledger, month),
                ).fetchone()
                budget = budget_row[0] if budget_row else None
                rows = connection.execute(
                    f"""SELECT e.* FROM entries e
                        LEFT JOIN entries original
                            ON original.id = e.original_entry_id
                           AND original.ledger_id = e.ledger_id
                        WHERE e.ledger_id = ? AND e.status = 'approved'
                          AND substr({occurrence}, 1, 7) = ?""",
                    (ledger, month),
                ).fetchall()
            for row in rows:
                direction = -1 if row["kind"] == "reversal" else 1
                entry_type = row["entry_type"]
                amount = direction * row["amount_cent"]
                totals[entry_type] += amount
                categories[row["category"]][entry_type] += amount
                holder_role = "paid" if entry_type == "expense" else "collected"
                members[row["fund_holder_id"]][holder_role] += amount
                for share in entry_snapshot(connection, row)["allocations"]:
                    members[share["member_user_id"]][entry_type] += (
                        direction * share["amount_cent"]
                    )
        for amounts in categories.values():
            amounts["net"] = amounts["income"] - amounts["expense"]
        for amounts in members.values():
            amounts["net"] = (
                amounts["paid"] - amounts["expense"] + amounts["income"] - amounts["collected"]
            )
        return {
            **totals, "budget": budget,
            "remaining": None if budget is None else budget - totals["expense"],
            "categories": dict(categories), "members": dict(members),
        }

    def statistics_range(self, token, ledger, start, end):
        """Return category and member totals for an inclusive date interval."""
        return self.statistics(token, ledger, start=start, end=end)

    @user_operation
    def audit(
        self, token, ledger, *, entry_id=None, operator_id=None, action="",
        object_id=None, start="", end="",
    ):
        """Query the ledger or a specific entry while enforcing privacy rules."""
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
                    "'approved', 'rejected', 'request_reversal', 'confirm_duplicate') "
                    "AND object_id = ?"
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
                day = beijing_datetime(row["occurred_at"]).date().isoformat()
                if (not start or day >= start) and (not end or day <= end):
                    result.append(dict(row))
            return result
