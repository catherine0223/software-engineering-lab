"""Select, preview, and atomically save settlement batches."""

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from uuid import uuid4

from .entries import entry_snapshot, parse_date
from .storage import RuleError, access, encode, now, record_event


@dataclass(frozen=True)
class Preview:
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
    if row["kind"] == "reversal":
        return datetime.fromisoformat(row["approved_at"]).astimezone().date().isoformat()
    return row["occurred_on"]


def eligible_entries(connection, ledger):
    return tuple(connection.execute(
        """SELECT e.*, original.batch_id AS original_batch_id
           FROM entries e LEFT JOIN entries original ON original.id = e.original_entry_id
           WHERE e.ledger_id = ? AND e.status = 'approved' AND e.batch_id IS NULL
           ORDER BY e.id""",
        (ledger,),
    ))


def individually_selectable(row):
    return row["kind"] != "reversal" or row["original_batch_id"] is not None


def valid_range(start, end):
    if parse_date(start) > parse_date(end):
        raise RuleError("开始日期不能晚于结束日期")


def calculate_balances(items):
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
    def __init__(self, database, accounts):
        self.database = database
        self.accounts = accounts

    def history_candidates(self, token, ledger, start, end):
        actor = self.accounts.actor(token)
        valid_range(start, end)
        with self.database.transaction() as connection:
            access(connection, actor, ledger, {"admin"}, write=True)
            return [
                {
                    "id": row["id"], "title": row["title"],
                    "date": eligibility_date(row), "amount_cent": row["amount_cent"],
                }
                for row in eligible_entries(connection, ledger)
                if individually_selectable(row) and eligibility_date(row) < start
            ]

    def preview(self, token, ledger, start, end, history_ids=()):
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

    def confirm(self, token, preview):
        actor = self.accounts.actor(token)
        ledger = preview.ledger_id
        valid_range(preview.start_date, preview.end_date)
        with self.database.transaction(write=True) as connection:
            access(connection, actor, ledger, {"admin"})
            existing = connection.execute(
                "SELECT * FROM batches WHERE ledger_id = ? AND request_id = ?",
                (ledger, preview.request_id),
            ).fetchone()
            if existing is not None:
                if existing["operator_id"] != actor:
                    raise RuleError("此请求已由其他管理员处理")
                return existing["id"]
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

    def history(self, token, ledger):
        actor = self.accounts.actor(token)
        with self.database.transaction() as connection:
            access(connection, actor, ledger)
            return [
                dict(row) for row in connection.execute(
                    """SELECT id, start_date, end_date, operator_id, created_at
                       FROM batches WHERE ledger_id = ? ORDER BY id DESC""",
                    (ledger,),
                )
            ]

    def detail(self, token, ledger, batch):
        actor = self.accounts.actor(token)
        with self.database.transaction() as connection:
            access(connection, actor, ledger)
            row = connection.execute(
                "SELECT * FROM batches WHERE id = ? AND ledger_id = ?", (batch, ledger)
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
