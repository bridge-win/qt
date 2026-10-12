"""Persistent paper execution with explicit fees, separate from testnet records."""

import json
import math
from collections.abc import Callable
from dataclasses import asdict
from typing import Literal

from qt.btc_agent.broker import Fill, Quote
from qt.btc_agent.store import Store


class PaperBroker:
    def __init__(self, store: Store, quote_source: Callable[[], Quote]):
        self.store = store
        self.quote = quote_source

    def balances(self) -> dict[str, float]:
        btc, cash = 0.0, 1000.0
        for row in self.store.db.execute("SELECT fill FROM paper_fills"):
            fill = Fill(**json.loads(row[0]))
            sign = 1 if fill.side == "buy" else -1
            btc += sign * fill.filled_qty - fill.fee_btc
            cash -= sign * fill.quote_qty + fill.fee_quote
        return {"BTC": btc, "USDT": cash}

    def prepare_price(self, price: float, side: Literal["buy", "sell"]) -> float:
        rounding = math.floor if side == "buy" else math.ceil
        return rounding(price * 100) / 100

    def prepare_quantity(self, qty: float, price: float) -> float:
        qty = math.floor(qty * 100_000) / 100_000
        if qty <= 0 or qty * price < 5:
            raise ValueError("paper order below exchange-like minimum")
        return qty

    def is_dust(self, qty: float, price: float) -> bool:
        if not all(math.isfinite(v) and v > 0 for v in (qty, price)):
            raise ValueError("invalid dust assessment")
        rounded = math.floor(qty * 100_000) / 100_000
        return rounded <= 0 or rounded * price < 5

    def lookup(self, client_id: str) -> Fill | None:
        row = self.store.db.execute(
            "SELECT fill FROM paper_fills WHERE client_id=?", (client_id,)
        ).fetchone()
        return Fill(**json.loads(row[0])) if row else None

    def submit(
        self, client_id: str, side: Literal["buy", "sell"], qty: float, limit_price: float
    ) -> Fill:
        if existing := self.lookup(client_id):
            return existing
        book = self.quote()
        price = book.ask if side == "buy" else book.bid
        crossed = price <= limit_price if side == "buy" else price >= limit_price
        amount = qty if crossed else 0
        quote_qty = amount * price
        fee = quote_qty * 0.001
        balances = self.balances()
        if (side == "buy" and quote_qty + fee > balances["USDT"]) or (
            side == "sell" and amount > balances["BTC"] + 1e-10
        ):
            raise ValueError("insufficient paper funds")
        fill = Fill(
            client_id,
            side,
            "FILLED" if crossed else "EXPIRED",
            amount,
            quote_qty,
            fee_quote=fee,
            exchange_id=f"paper-{client_id}",
        )
        with self.store.db:
            self.store.db.execute(
                "INSERT INTO paper_fills VALUES (?,?)", (client_id, json.dumps(asdict(fill)))
            )
        return fill
