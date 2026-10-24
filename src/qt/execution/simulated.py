"""Depth-bounded paper execution and an explicitly unsent shadow adapter.

Only immediate taker fills against supplied L2 are simulated. Resting maker
orders do not fill without a queue model. Paper results are durable when a
Repository is supplied; paper execution is not evidence of real venue fills.
"""

from __future__ import annotations

import time
from decimal import Decimal, localcontext
from typing import Any

from sqlalchemy import func, insert, select

from qt.contracts import (
    CancelResult,
    ExecutableMarketSnapshot,
    FeeSchedule,
    InstrumentSpec,
    OrderIdentity,
    OrderIntent,
    OrderSnapshot,
    ReconcileBatch,
    ReconcileCursor,
    SubmitResult,
    content_hash,
    decimal_string,
)
from qt.execution.oms import _ceil
from qt.persistence import Conflict, Repository
from qt.persistence.repository import insert_once
from qt.persistence.schema import artifacts


class PaperAdapter:
    execution_mode = "paper"

    def __init__(self, repo: Repository | None = None, *, tenant_id: str = "owner") -> None:
        self.repo, self.tenant_id = repo, tenant_id
        self._book: dict[str, Any] | None = None
        self._results: dict[str, dict] = {}
        self._canceled: set[str] = set()

    @staticmethod
    def _levels(levels, reverse: bool) -> list[tuple[Decimal, Decimal]]:
        parsed = []
        for price, quantity in levels:
            if not isinstance(price, (str, Decimal)) or not isinstance(quantity, (str, Decimal)):
                raise ValueError("paper_depth_requires_exact_decimals")
            price, quantity = Decimal(price), Decimal(quantity)
            if not price.is_finite() or not quantity.is_finite() or price <= 0 or quantity <= 0:
                raise ValueError("paper_depth_must_be_positive_and_finite")
            parsed.append((price, quantity))
        if not parsed or len({price for price, _ in parsed}) != len(parsed):
            raise ValueError("paper_depth_must_be_nonempty_and_unique")
        return sorted(parsed, reverse=reverse)

    def set_book(self, market: ExecutableMarketSnapshot, instrument: InstrumentSpec,
                 fees: FeeSchedule, levels: dict) -> None:
        bids, asks = self._levels(levels["bids"], True), self._levels(levels["asks"], False)
        if (not market.book_valid or bids[0][0] != market.bid or asks[0][0] != market.ask
                or bids[0][0] >= asks[0][0]
                or market.venue != instrument.venue or market.instrument != instrument.instrument
                or market.instrument_spec_hash != instrument.content_hash()
                or market.fee_schedule_hash != fees.content_hash()):
            raise ValueError("paper_book_quote_or_version_mismatch")
        content = {"market": market.model_dump(mode="json"),
                   "instrument": instrument.model_dump(mode="json"),
                   "fees": fees.model_dump(mode="json"),
                   "bids": [[str(p), str(q)] for p, q in bids],
                   "asks": [[str(p), str(q)] for p, q in asks]}
        self._book = {**content, "book_hash": content_hash(content)}

    def update_book(self, book, market: ExecutableMarketSnapshot,
                    instrument: InstrumentSpec, fees: FeeSchedule) -> None:
        self.set_book(market, instrument, fees, {
            "bids": [(level.price, level.quantity) for level in book.bids],
            "asks": [(level.price, level.quantity) for level in book.asks],
        })

    def _records(self, connection=None, *, book_hash: str | None = None) -> list[dict]:
        if self.repo is None:
            return [row for row in self._results.values() if book_hash is None or row["book_hash"] == book_hash]
        if connection is None:
            with self.repo.transaction() as connection:
                return self._records(connection, book_hash=book_hash)
        query = select(artifacts).where(
            artifacts.c.tenant_id == self.tenant_id, artifacts.c.kind == "paper_order_result",
        )
        if book_hash is not None:
            query = query.where(artifacts.c.payload["book_hash"].as_string() == book_hash)
        return [self._verified_payload(row) for row in connection.execute(
            query.order_by(artifacts.c.created_ns, artifacts.c.entity_id),
        ).mappings()]

    @staticmethod
    def _verified_payload(row) -> dict:
        if content_hash(row["payload"]) != row["sha256"]:
            raise Conflict("paper_result_integrity_failure")
        return dict(row["payload"])

    def _lookup(self, identity: str, connection=None) -> dict | None:
        if self.repo is None:
            return self._results.get(identity)
        if connection is None:
            with self.repo.transaction() as connection:
                return self._lookup(identity, connection)
        row = connection.execute(select(artifacts).where(
            artifacts.c.tenant_id == self.tenant_id, artifacts.c.kind == "paper_order_result",
            artifacts.c.entity_id == identity,
        )).mappings().first()
        if row is None:
            return None
        return self._verified_payload(row)

    def _match(self, intent: OrderIntent, previous: list[dict]) -> dict:
        if self._book is None:
            raise ValueError("paper_requires_supplied_depth")
        book = self._book
        market, instrument, fees = book["market"], book["instrument"], book["fees"]
        now = time.time_ns()
        if not 0 <= now - market["asof_ns"] <= 1_000_000_000:
            raise ValueError("paper_book_stale")
        if (intent.venue != market["venue"] or intent.instrument != market["instrument"]
                or intent.fee_schedule_hash != content_hash(fees)
                or intent.instrument_spec_hash != content_hash(instrument)):
            raise ValueError("paper_intent_version_mismatch")
        if fees["fee_basis"] != "notional" or instrument["instrument_type"] != "spot":
            raise ValueError("paper_fee_or_instrument_model_unavailable")
        identity = OrderIdentity(venue=intent.venue, account_id=intent.account_id,
                                 intent_id=intent.intent_id, venue_order_id="paper-" + content_hash(intent)[:32])
        used: dict[Decimal, Decimal] = {}
        side = "asks" if intent.side == "buy" else "bids"
        with localcontext() as ctx:
            ctx.prec = 80
            for order in previous:
                if order["book_hash"] != book["book_hash"] or order["intent"]["side"] != intent.side:
                    continue
                for fill in order["fills"]:
                    price = Decimal(fill["price"])
                    used[price] = used.get(price, Decimal(0)) + Decimal(fill["quantity"])
            available = []
            increment = Decimal(instrument["quantity_increment"])
            for price, quantity in book[side]:
                price, quantity = Decimal(price), Decimal(quantity)
                if (intent.side == "buy" and price > intent.limit_price) or (intent.side == "sell" and price < intent.limit_price):
                    continue
                remaining = (max(Decimal(0), quantity - used.get(price, Decimal(0))) // increment) * increment
                if remaining:
                    available.append((price, remaining))
            enough = sum((quantity for _, quantity in available), Decimal(0)) >= intent.quantity
            rejected = now >= intent.valid_until_ns or (intent.time_in_force == "FOK" and not enough)
            remaining, matched = intent.quantity, []
            if not rejected:
                for index, (price, quantity) in enumerate(available):
                    executed = min(remaining, quantity)
                    # Never claim sub-increment quantities as exchange fills.
                    executed = (executed // increment) * increment
                    if not executed:
                        continue
                    base_fee = fees["fee_asset"] == instrument["base_asset"]
                    if fees["fee_asset"] not in {instrument["quote_asset"], instrument["base_asset"]}:
                        raise ValueError("paper_fee_asset_unavailable")
                    fee = _ceil(executed * (1 if base_fee else price) * max(Decimal(0), Decimal(fees["taker_rate"])))
                    matched.append({
                        "intent_id": intent.intent_id, "venue_trade_id": f"paper:{intent.intent_id}:{index}",
                        "quantity": decimal_string(executed), "price": decimal_string(price),
                        "fee": decimal_string(fee), "fee_asset": fees["fee_asset"],
                        "base_asset": instrument["base_asset"], "quote_asset": instrument["quote_asset"],
                    })
                    remaining -= executed
                    if remaining == 0:
                        break
        status = "REJECTED" if rejected else "FILLED" if remaining == 0 else (
            "CANCELED" if intent.time_in_force == "IOC" else "PARTIALLY_FILLED" if matched else "ACKNOWLEDGED"
        )
        return {
            "intent": intent.model_dump(mode="json"), "intent_hash": intent.content_hash(),
            "book_hash": book["book_hash"], "fills": matched,
            "snapshot": OrderSnapshot(identity=identity, status=status, quantity=intent.quantity,
                                      filled_quantity=intent.quantity - remaining, asof_ns=now).model_dump(mode="json"),
            "submit": SubmitResult(status="rejected" if rejected else "accepted", identity=identity,
                                   received_at_ns=now, reason="paper_depth_only").model_dump(mode="json"),
            "limitations": ["No market impact or maker queue model; immediate taker fills only"],
        }

    async def submit(self, intent: OrderIntent) -> SubmitResult:
        if self.repo is None:
            existing = self._lookup(intent.intent_id)
            if existing is None:
                existing = self._match(intent, self._records())
                self._results[intent.intent_id] = existing
        else:
            with self.repo.transaction() as connection:
                existing = self._lookup(intent.intent_id, connection)
                if existing is not None:
                    if existing["intent_hash"] != intent.content_hash():
                        raise Conflict("paper_intent_identity_conflict")
                    return SubmitResult.model_validate(existing["submit"])
                if self._book is None:
                    raise ValueError("paper_requires_supplied_depth")
                # The immutable book row provides a cross-process PG row lock;
                # result creation and local depth consumption commit together.
                insert_once(connection, artifacts, {
                    "tenant_id": self.tenant_id, "kind": "paper_account", "entity_id": intent.account_id,
                    "payload": {"account_id": intent.account_id},
                    "sha256": content_hash({"account_id": intent.account_id}), "created_ns": time.time_ns(),
                })
                connection.execute(select(artifacts.c.entity_id).where(
                    artifacts.c.tenant_id == self.tenant_id, artifacts.c.kind == "paper_account",
                    artifacts.c.entity_id == intent.account_id,
                ).with_for_update()).first()
                insert_once(connection, artifacts, {
                    "tenant_id": self.tenant_id, "kind": "paper_book", "entity_id": self._book["book_hash"],
                    "payload": self._book, "sha256": content_hash(self._book), "created_ns": time.time_ns(),
                })
                connection.execute(select(artifacts.c.entity_id).where(
                    artifacts.c.tenant_id == self.tenant_id, artifacts.c.kind == "paper_book",
                    artifacts.c.entity_id == self._book["book_hash"],
                ).with_for_update()).first()
                existing = self._lookup(intent.intent_id, connection)
                if existing is None:
                    previous = self._records(connection, book_hash=self._book["book_hash"])
                    existing = self._match(intent, previous)
                    sequence = connection.execute(select(func.max(
                        artifacts.c.payload["account_sequence"].as_integer(),
                    )).where(
                        artifacts.c.tenant_id == self.tenant_id, artifacts.c.kind == "paper_order_result",
                        artifacts.c.payload["intent"]["account_id"].as_string() == intent.account_id,
                    )).scalar_one()
                    existing["account_sequence"] = 1 + (sequence or 0)
                    connection.execute(insert(artifacts).values(
                        tenant_id=self.tenant_id, kind="paper_order_result", entity_id=intent.intent_id,
                        payload=existing, sha256=content_hash(existing), created_ns=time.time_ns(),
                    ))
        if existing["intent_hash"] != intent.content_hash():
            raise Conflict("paper_intent_identity_conflict")
        return SubmitResult.model_validate(existing["submit"])

    def _is_canceled(self, identity: str) -> bool:
        if self.repo is None:
            return identity in self._canceled
        with self.repo.transaction() as connection:
            row = connection.execute(select(artifacts).where(
                artifacts.c.tenant_id == self.tenant_id, artifacts.c.kind == "paper_cancel",
                artifacts.c.entity_id == identity,
            )).mappings().first()
            if row is not None:
                self._verified_payload(row)
            return row is not None

    @staticmethod
    def _verify_identity(record: dict, identity: OrderIdentity) -> None:
        stored = record["snapshot"]["identity"]
        if any(stored[key] != getattr(identity, key) for key in ("venue", "account_id", "intent_id")):
            raise ValueError("paper_order_identity_mismatch")
        if identity.venue_order_id is not None and identity.venue_order_id != stored["venue_order_id"]:
            raise ValueError("paper_venue_order_identity_mismatch")

    async def query(self, identity: OrderIdentity) -> OrderSnapshot:
        record = self._lookup(identity.intent_id)
        if record is None:
            raise KeyError("paper_order_not_found")
        self._verify_identity(record, identity)
        snapshot = OrderSnapshot.model_validate(record["snapshot"])
        status = snapshot.status
        if status not in {"FILLED", "REJECTED", "CANCELED", "EXPIRED"}:
            if self._is_canceled(identity.intent_id):
                status = "CANCELED"
            elif record["intent"]["time_in_force"] == "GTD" and time.time_ns() >= record["intent"]["valid_until_ns"]:
                status = "EXPIRED"
        return snapshot.model_copy(update={"status": status, "asof_ns": time.time_ns()})

    async def cancel(self, identity: OrderIdentity) -> CancelResult:
        snapshot = await self.query(identity)
        if snapshot.status == "FILLED":
            return CancelResult(status="rejected", identity=snapshot.identity,
                                received_at_ns=time.time_ns(), reason="already_filled")
        if self.repo is None:
            self._canceled.add(identity.intent_id)
        else:
            self.repo.put(self.tenant_id, "paper_cancel", {"intent_id": identity.intent_id}, identity.intent_id)
        return CancelResult(status="confirmed", identity=snapshot.identity, received_at_ns=time.time_ns())

    async def reconcile(self, cursor: ReconcileCursor) -> ReconcileBatch:
        try:
            offset = int(cursor.cursor or "0")
        except ValueError as exc:
            raise ValueError("invalid_paper_reconcile_cursor") from exc
        if offset < 0:
            raise ValueError("invalid_paper_reconcile_cursor")
        if self.repo is None:
            records = [record for record in self._records() if (
                record["intent"]["account_id"] == cursor.account_id and record["intent"]["venue"] == cursor.venue
            )]
            if offset > len(records):
                raise ValueError("invalid_paper_reconcile_cursor")
            page = records[offset:offset + 201]
        else:
            # Bound payload materialization to one page even after years of
            # immutable paper history. Appending new records preserves offsets.
            with self.repo.transaction() as connection:
                rows = connection.execute(select(artifacts).where(
                    artifacts.c.tenant_id == self.tenant_id, artifacts.c.kind == "paper_order_result",
                    artifacts.c.payload["intent"]["account_id"].as_string() == cursor.account_id,
                    artifacts.c.payload["intent"]["venue"].as_string() == cursor.venue,
                ).order_by(artifacts.c.payload["account_sequence"].as_integer(), artifacts.c.entity_id)
                    .offset(offset).limit(201)).mappings().all()
                page = [self._verified_payload(row) for row in rows]
        complete = len(page) <= 200
        page = page[:200]
        snapshots = [await self.query(OrderIdentity.model_validate(record["snapshot"]["identity"])) for record in page]
        return ReconcileBatch(
            next_cursor=ReconcileCursor(venue=cursor.venue, account_id=cursor.account_id,
                                        cursor=str(offset + len(page))),
            orders=tuple(snapshots), fills=tuple(fill for record in page for fill in record["fills"]),
            complete=complete,
        )


class ShadowAdapter:
    execution_mode = "shadow"

    async def submit(self, intent: OrderIntent) -> SubmitResult:
        return SubmitResult(status="unknown", identity=OrderIdentity(
            venue=intent.venue, account_id=intent.account_id, intent_id=intent.intent_id,
        ), received_at_ns=time.time_ns(), reason="shadow_unsent")

    async def query(self, identity: OrderIdentity) -> OrderSnapshot:
        raise KeyError("shadow_has_no_venue_order")

    async def cancel(self, identity: OrderIdentity) -> CancelResult:
        return CancelResult(status="rejected", identity=identity, received_at_ns=time.time_ns(), reason="shadow_unsent")

    async def reconcile(self, cursor: ReconcileCursor) -> ReconcileBatch:
        return ReconcileBatch(next_cursor=cursor, orders=(), fills=(), complete=True)
