"""Select, preview, and atomically save settlement batches."""

from collections import defaultdict
from dataclasses import dataclass
import hashlib
import json
import sqlite3
from uuid import uuid4

from .accounts import user_operation
from .entries import entry_snapshot, parse_date
from .storage import RuleError, access, beijing_datetime, encode, now, record_event


@dataclass(frozen=True)
class Preview:
    """Selected entries and calculated transfers shown before confirmation."""
    ledger_id: int
    start_date: str
    end_date: str
    history_ids: tuple
    request_id: str
    fingerprint: str
    items: tuple
    balances: tuple
    transfers: tuple


def eligibility_date(row):
    """Use the approval date for reversals and occurrence date otherwise."""
    if row["kind"] == "reversal":
        return beijing_datetime(row["approved_at"]).date().isoformat()
    return row["occurred_on"]


def eligible_entries(connection, ledger):
    """Find approved entries that are not assigned to a saved batch."""
    return tuple(connection.execute(
        """SELECT e.*, original.batch_id AS original_batch_id
           FROM entries e LEFT JOIN entries original ON original.id = e.original_entry_id
           WHERE e.ledger_id = ? AND e.status = 'approved' AND e.batch_id IS NULL
           ORDER BY e.id""",
        (ledger,),
    ))


def individually_selectable(row):
    """Allow independent selection for originals and reversals of settled originals."""
    return row["kind"] != "reversal" or row["original_batch_id"] is not None


def valid_range(start, end):
    """Check that the start and end dates are valid and ordered."""
    if parse_date(start) > parse_date(end):
        raise RuleError("开始日期不能晚于结束日期")


def calculate_balances(items):
    """Compute member receivables and payables from selected entries."""
    balances = defaultdict(int)
    for item in items:
        entry = item["snapshot"]
        direction = -1 if entry["kind"] == "reversal" else 1
        if entry["entry_type"] == "income":
            direction *= -1
        balances[entry["fund_holder_id"]] += direction * entry["amount_cent"]
        for allocation in entry["allocations"]:
            balances[allocation["member_user_id"]] -= direction * allocation["amount_cent"]
    if sum(balances.values()) != 0:
        raise RuleError("结算明细金额不平衡")
    return tuple(sorted(balances.items()))


def calculate_transfers(balances):
    """Generate transfers between members with opposite net balances."""
    payers = [[member, -amount] for member, amount in balances if amount < 0]
    payees = [[member, amount] for member, amount in balances if amount > 0]
    payer_index = payee_index = 0
    transfers = []
    while payer_index < len(payers) and payee_index < len(payees):
        payer, debt = payers[payer_index]
        payee, credit = payees[payee_index]
        amount = min(debt, credit)
        transfers.append((payer, payee, amount))
        payers[payer_index][1] -= amount
        payees[payee_index][1] -= amount
        if debt == amount:
            payer_index += 1
        if credit == amount:
            payee_index += 1
    return tuple(transfers)


class Settlements:
    """Preview, commit, and recover settlement requests."""
    def __init__(self, database, accounts):
        self.database = database
        self.accounts = accounts

    def history_candidates(self, token, ledger, start, end):
        """Find older eligible entries that an administrator may include."""
        actor = self.accounts.actor(token)
        valid_range(start, end)
        with self.database.transaction() as connection:
            access(connection, actor, ledger, {"admin"}, write=True)
            return [
                {
                    "id": row["id"], "title": row["title"],
                    "date": eligibility_date(row), "amount_cent": row["amount_cent"],
                    "snapshot": entry_snapshot(connection, row),
                }
                for row in eligible_entries(connection, ledger)
                if individually_selectable(row) and eligibility_date(row) < start
            ]

    @user_operation
    def preview(self, token, ledger, start, end, history_ids=()):
        """Build a settlement preview without changing the ledger."""
        actor = self.accounts.actor(token)
        valid_range(start, end)
        with self.database.transaction() as connection:
            access(connection, actor, ledger, {"admin"}, write=True)
            return self._preview(
                connection, ledger, start, end,
                tuple(sorted(set(history_ids))), uuid4().hex,
            )

    def _preview(self, connection, ledger, start, end, history_ids, request_id):
        rows = eligible_entries(connection, ledger)
        historical = {
            row["id"] for row in rows
            if individually_selectable(row) and eligibility_date(row) < start
        }
        if not set(history_ids) <= historical:
            raise RuleError("所选历史记录已变化，请刷新预览")
        selected = {
            row["id"]: "history" if row["id"] in history_ids else "period"
            for row in rows if individually_selectable(row)
            and (row["id"] in history_ids or start <= eligibility_date(row) <= end)
        }
        pairs = {}
        for row in rows:
            if row["kind"] == "reversal" and row["original_entry_id"] in selected:
                original = row["original_entry_id"]
                pairs[original] = row["id"]
                pairs[row["id"]] = original
                within = start <= eligibility_date(row) <= end
                selected[row["id"]] = "paired" if within else "paired_outside_period"
        items = tuple(
            {
                "snapshot": entry_snapshot(connection, row),
                "paired_entry_id": pairs.get(row["id"]),
                "inclusion_reason": selected[row["id"]],
            }
            for row in rows if row["id"] in selected
        )
        if not items:
            raise RuleError("没有可结算记录")
        balances = calculate_balances(items)
        transfers = calculate_transfers(balances)
        fingerprint = hashlib.sha256(encode(items).encode()).hexdigest()
        return Preview(
            ledger, start, end, history_ids, request_id,
            fingerprint, items, balances, transfers,
        )

    @user_operation
    def confirm(self, token, preview):
        """Persist the intent and save the batch only if its preview still matches."""
        actor = self.accounts.actor(token)
        ledger = preview.ledger_id
        valid_range(preview.start_date, preview.end_date)
        with self.database.transaction(write=True) as connection:
            access(connection, actor, ledger, {"admin"})
            existing = self._saved_batch(connection, preview, actor)
            if existing is not None:
                return existing
            access(connection, actor, ledger, {"admin"}, write=True)
            connection.execute(
                """INSERT INTO settlement_requests
                   (ledger_id, request_id, operator_id, start_date, end_date,
                    history_ids, fingerprint, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(ledger_id, request_id) DO NOTHING""",
                (ledger, preview.request_id, actor, preview.start_date, preview.end_date,
                 encode(preview.history_ids), preview.fingerprint, now()),
            )
            row = connection.execute(
                """SELECT operator_id, start_date, end_date, history_ids, fingerprint
                   FROM settlement_requests WHERE ledger_id = ? AND request_id = ?""",
                (ledger, preview.request_id),
            ).fetchone()
            if tuple(row) != (actor, preview.start_date, preview.end_date,
                              encode(preview.history_ids), preview.fingerprint):
                raise RuleError("结算请求与原预览不一致，请重新预览")
        try:
            return self._confirm_registered(actor, preview)
        except (RuleError, sqlite3.Error) as error:
            message = str(error) if isinstance(error, RuleError) else "保存结算事务时发生数据库错误"
            try:
                with self.database.transaction(write=True) as connection:
                    connection.execute(
                        """UPDATE settlement_requests SET error = ?
                           WHERE ledger_id = ? AND request_id = ? AND operator_id = ?""",
                        (message, ledger, preview.request_id, actor),
                    )
            except sqlite3.Error:
                # The durable request remains available even while SQLite cannot write.
                pass
            raise

    @staticmethod
    def _saved_batch(connection, preview, actor):
        row = connection.execute(
            "SELECT id, operator_id FROM batches WHERE ledger_id = ? AND request_id = ?",
            (preview.ledger_id, preview.request_id),
        ).fetchone()
        if row is None:
            return None
        if row["operator_id"] != actor:
            raise RuleError("此请求已由其他管理员处理")
        return row["id"]

    def _confirm_registered(self, actor, preview):
        ledger = preview.ledger_id
        with self.database.transaction(write=True) as connection:
            access(connection, actor, ledger, {"admin"})
            existing = self._saved_batch(connection, preview, actor)
            if existing is not None:
                return existing
            access(connection, actor, ledger, {"admin"}, write=True)
            try:
                current = self._preview(
                    connection, ledger, preview.start_date, preview.end_date,
                    preview.history_ids, preview.request_id,
                )
            except RuleError as error:
                raise RuleError("结算依据已变化，请刷新预览") from error
            if current.fingerprint != preview.fingerprint:
                raise RuleError("结算依据已变化，请刷新预览")
            return self._save(connection, actor, current)

    @user_operation
    def requests(self, token, ledger):
        """List the operator's settlement requests with their committed batch IDs."""
        actor = self.accounts.actor(token)
        # Wait for any active save transaction before reporting its commit status.
        with self.database.transaction(write=True) as connection:
            access(connection, actor, ledger, {"admin"})
            return [
                dict(row) for row in connection.execute(
                    """SELECT r.*, b.id AS batch_id
                       FROM settlement_requests r LEFT JOIN batches b
                           ON b.ledger_id = r.ledger_id AND b.request_id = r.request_id
                       WHERE r.ledger_id = ? AND r.operator_id = ?
                       ORDER BY r.sequence DESC""",
                    (ledger, actor),
                )
            ]

    def retry(self, token, ledger, request_id):
        """Retry a durable request with the original preview fingerprint."""
        actor = self.accounts.actor(token)
        with self.database.transaction() as connection:
            access(connection, actor, ledger, {"admin"})
            row = connection.execute(
                """SELECT * FROM settlement_requests
                   WHERE ledger_id = ? AND request_id = ? AND operator_id = ?""",
                (ledger, request_id, actor),
            ).fetchone()
            if row is None:
                raise RuleError("找不到可恢复的结算请求，请重新查询")
            preview = Preview(
                ledger, row["start_date"], row["end_date"], tuple(json.loads(row["history_ids"])),
                request_id, row["fingerprint"], (), (), (),
            )
        return self.confirm(token, preview)

    @staticmethod
    def _save(connection, actor, preview):
        ledger = preview.ledger_id
        batch = connection.execute(
            """INSERT INTO batches
               (ledger_id, request_id, start_date, end_date, operator_id,
                created_at, balances) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                ledger, preview.request_id, preview.start_date, preview.end_date,
                actor, now(), encode(preview.balances),
            ),
        ).lastrowid
        for item in preview.items:
            row = item["snapshot"]
            updated = connection.execute(
                """UPDATE entries SET batch_id = ?, version = version + 1
                   WHERE id = ? AND batch_id IS NULL AND version = ?""",
                (batch, row["id"], row["version"]),
            )
            if updated.rowcount != 1:
                raise RuleError("结算依据已变化，请刷新预览")
            connection.execute(
                """INSERT INTO settlement_items
                   (batch_id, source_entry_id, source_version, snapshot,
                    paired_entry_id, inclusion_reason) VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    batch, row["id"], row["version"], encode(row),
                    item["paired_entry_id"], item["inclusion_reason"],
                ),
            )
        connection.executemany(
            """INSERT INTO transfer_plans
               (batch_id, payer_user_id, payee_user_id, amount_cent) VALUES (?, ?, ?, ?)""",
            ((batch, payer, payee, amount) for payer, payee, amount in preview.transfers),
        )
        record_event(
            connection, ledger, actor, "settle", batch,
            after={"entries": [item["snapshot"]["id"] for item in preview.items]},
        )
        return batch

    @user_operation
    def history(self, token, ledger):
        """List saved batches in reverse order with ledger-local numbers."""
        actor = self.accounts.actor(token)
        with self.database.transaction() as connection:
            access(connection, actor, ledger)
            batches = connection.execute(
                """SELECT id, start_date, end_date, operator_id, created_at,
                          ROW_NUMBER() OVER (ORDER BY id) AS batch_number
                   FROM batches WHERE ledger_id = ? ORDER BY id DESC""",
                (ledger,),
            )
            return [dict(batch) for batch in batches]

    @user_operation
    def detail(self, token, ledger, batch):
        """Return the immutable items and transfers from a saved batch."""
        actor = self.accounts.actor(token)
        with self.database.transaction() as connection:
            access(connection, actor, ledger)
            row = connection.execute(
                """SELECT b.*, (SELECT COUNT(*) FROM batches earlier
                               WHERE earlier.ledger_id = b.ledger_id AND earlier.id <= b.id)
                               AS batch_number
                   FROM batches b WHERE b.id = ? AND b.ledger_id = ?""",
                (batch, ledger),
            ).fetchone()
            if row is None:
                raise RuleError("结算批次不存在")
            items = []
            for item in connection.execute(
                "SELECT * FROM settlement_items WHERE batch_id = ? ORDER BY source_entry_id",
                (batch,),
            ):
                value = dict(item)
                value["snapshot"] = json.loads(value["snapshot"])
                items.append(value)
            transfers = [
                tuple(transfer) for transfer in connection.execute(
                    """SELECT payer_user_id, payee_user_id, amount_cent
                       FROM transfer_plans WHERE batch_id = ?
                       ORDER BY payer_user_id, payee_user_id""",
                    (batch,),
                )
            ]
            return {
                **dict(row), "balances": json.loads(row["balances"]),
                "items": items, "transfers": transfers,
            }
