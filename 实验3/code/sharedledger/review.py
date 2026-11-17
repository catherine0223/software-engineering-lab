"""Review decisions and whole-entry reversals."""

from .accounts import user_operation
from .entries import (
    active_reversal,
    add_allocations,
    entry_snapshot,
    find_entry,
    require_version,
    save_entry_event,
    validate_occurred_on,
)
from .storage import WRITERS, RuleError, access, beijing_today, clean_text, now


class Review:
    """Apply review decisions and create or resubmit whole-entry reversals."""
    def __init__(self, database, accounts):
        self.database = database
        self.accounts = accounts

    @user_operation
    def decide(self, token, ledger, entry_id, version, *, approve, reason=""):
        """Approve or return the exact version reviewed by another administrator."""
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
            if approve:
                validate_occurred_on(row["occurred_on"])
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

    @user_operation
    def reverse(self, token, ledger, entry_id, version, reason):
        """Copy an approved entry into a pending whole-amount reversal."""
        actor = self.accounts.actor(token)
        reason = clean_text(reason, "冲销原因", 400, required=True)
        with self.database.transaction(write=True) as connection:
            role = access(connection, actor, ledger, WRITERS, write=True)
            row = find_entry(connection, ledger, entry_id, actor)
            require_version(row, version)
            if row["kind"] == "reversal" or row["status"] != "approved":
                raise RuleError("只能对已审核通过的原账申请冲销")
            if role != "admin" and row["submitter_id"] != actor:
                raise RuleError("只有原提交人或账本管理员可以申请冲销")
            if active_reversal(connection, entry_id):
                raise RuleError("这笔账已有未结束或已生效的冲销，请查看原申请")
            reversal_id = connection.execute(
                """INSERT INTO entries
                   (ledger_id, creator_id, submitter_id, fund_holder_id, title,
                    category, occurred_on, amount_cent, entry_type, kind,
                    status, ever_submitted, notes, reason, original_entry_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'reversal', 'pending', 1, '', ?, ?)""",
                (
                    ledger, actor, actor, row["fund_holder_id"], "冲销 " + row["title"],
                    row["category"], beijing_today().isoformat(),
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

    @user_operation
    def resubmit_reversal(self, token, ledger, entry_id, version, reason):
        """Resubmit the existing reversal with the applicant's revised reason."""
        actor = self.accounts.actor(token)
        reason = clean_text(reason, "冲销原因", 400, required=True)
        with self.database.transaction(write=True) as connection:
            role = access(connection, actor, ledger, WRITERS, write=True)
            row = find_entry(connection, ledger, entry_id, actor)
            require_version(row, version)
            if (row["kind"] != "reversal" or row["submitter_id"] != actor
                    or row["status"] not in {"rejected", "withdrawn"}):
                raise RuleError("只有申请人可以重新提交被退回或已撤回的冲销")
            original = find_entry(connection, ledger, row["original_entry_id"], actor)
            if role != "admin" and original["submitter_id"] != actor:
                raise RuleError("只有原提交人或账本管理员可以申请冲销")
            if original["status"] != "approved":
                raise RuleError("原账已不可冲销")
            if active_reversal(connection, original["id"], excluding=entry_id):
                raise RuleError("原账已有其他未结束或已生效的冲销，请先处理")
            before = entry_snapshot(connection, row)
            connection.execute(
                """UPDATE entries SET status = 'pending', reason = ?, version = version + 1
                   WHERE id = ?""",
                (reason, entry_id),
            )
            save_entry_event(connection, ledger, actor, "submit", entry_id, before, reason)
