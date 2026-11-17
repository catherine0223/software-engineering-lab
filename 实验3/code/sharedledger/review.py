"""Review decisions and whole-entry reversals."""

from datetime import datetime

from .entries import (
    active_reversal,
    add_allocations,
    entry_snapshot,
    find_entry,
    require_version,
    save_entry_event,
)
from .storage import WRITERS, RuleError, access, clean_text, now


class Review:
    def __init__(self, database, accounts):
        self.database = database
        self.accounts = accounts

    def decide(self, token, ledger, entry_id, version, *, approve, reason=""):
        actor = self.accounts.actor(token)
        reason = "" if approve else clean_text(reason, "退回原因", 400, required=True)
        with self.database.transaction(write=True) as connection:
            access(connection, actor, ledger, {"admin"}, write=True)
            row = find_entry(connection, ledger, entry_id, actor)
            if row["submitter_id"] == actor:
                raise RuleError("不能审核本人提交的记录")
            if row["status"] != "pending":
                raise RuleError("记录已撤回或已处理，请刷新")
            require_version(row, version)
            before = entry_snapshot(connection, row)
            status = "approved" if approve else "rejected"
            entry_reason = row["reason"] if approve and row["kind"] == "reversal" else reason
            result = connection.execute(
                """UPDATE entries SET status = ?, approved_at = ?, reason = ?,
                   version = version + 1 WHERE id = ? AND version = ? AND status = 'pending'""",
                (status, now() if approve else None, entry_reason, entry_id, version),
            )
            if result.rowcount != 1:
                raise RuleError("记录已变化，请刷新")
            save_entry_event(connection, ledger, actor, status, entry_id, before, reason)

    def reverse(self, token, ledger, entry_id, version, reason):
        actor = self.accounts.actor(token)
        reason = clean_text(reason, "冲销原因", 400, required=True)
        with self.database.transaction(write=True) as connection:
            access(connection, actor, ledger, WRITERS, write=True)
            row = find_entry(connection, ledger, entry_id, actor)
            require_version(row, version)
            if row["kind"] == "reversal" or row["status"] != "approved":
                raise RuleError("只能对已审核通过的原账申请冲销")
            if active_reversal(connection, entry_id):
                raise RuleError("这笔账已有待审或已生效的冲销")
            reversal_id = connection.execute(
                """INSERT INTO entries
                   (ledger_id, creator_id, submitter_id, fund_holder_id, title,
                    category, occurred_on, amount_cent, entry_type, kind,
                    status, ever_submitted, notes, reason, original_entry_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'reversal', 'pending', 1, '', ?, ?)""",
                (
                    ledger, actor, actor, row["fund_holder_id"], "冲销 " + row["title"],
                    row["category"], datetime.now().date().isoformat(),
                    row["amount_cent"], row["entry_type"], reason, entry_id,
                ),
            ).lastrowid
            original = entry_snapshot(connection, row)
            add_allocations(
                connection, reversal_id,
                (
                    (item["member_user_id"], item["amount_cent"])
                    for item in original["allocations"]
                ),
            )
            save_entry_event(
                connection, ledger, actor, "request_reversal", reversal_id, reason=reason
            )
            return reversal_id
