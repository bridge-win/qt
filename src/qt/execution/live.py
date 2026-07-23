"""Live broker (ccxt). DISABLED by default — many independent safety gates.

Design principle: **make the safe path the default and the dangerous path
loud and hard**. Every gate below must be *explicitly* cleared before a
single real order can leave this process:

1. ``live_enabled`` must be True (env ``QT_EXECUTION__LIVE_ENABLED`` / config).
2. ``dry_run`` must be False (defaults True — logs the order, never sends).
3. No ``KILL`` file may exist on disk (a one-line ``touch`` global stop).
4. The API key must be **trade-only, withdrawal-disabled** — verified at
   construction via the venue's permission endpoint (``require_trade_only_key``).
5. Per-order notional ≤ ``max_order_quote``.
6. Cumulative BUY notional for the UTC day ≤ ``max_daily_spend_quote``.
7. Total open position value ≤ ``max_total_exposure_quote``.
8. Only ``symbol`` (default BTC/USDT), only spot, only market orders.

If any gate fails the order is refused with a specific exception; the
runner treats that as "no trade" and keeps going, never crashing.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

from qt.core.config import ExecutionConfig
from qt.core.logging import get_logger
from qt.core.types import OrderSide, OrderType, Trade
from qt.execution.base import Broker, Order

log = get_logger(__name__)


class LiveTradingDisabledError(RuntimeError):
    """Raised when a live order would be submitted but a safety gate blocks it."""


class GuardrailBlockedError(RuntimeError):
    """Raised when a specific guardrail (spend cap, exposure, kill file) refuses an order."""


class UnsafeApiKeyError(RuntimeError):
    """Raised when the API key has withdrawal permission (or perms can't be verified)."""


class LiveOrderRejectedError(RuntimeError):
    """Raised when the exchange accepts no usable live fill for an order."""


@dataclass
class _DailySpend:
    """Tracks cumulative BUY notional for the current UTC day."""

    day: str = ""
    spent_quote: float = 0.0

    def add(self, quote: float, now: datetime) -> None:
        today = now.strftime("%Y-%m-%d")
        if today != self.day:
            self.day = today
            self.spent_quote = 0.0
        self.spent_quote += quote

    def would_exceed(self, quote: float, cap: float, now: datetime) -> bool:
        today = now.strftime("%Y-%m-%d")
        base = self.spent_quote if today == self.day else 0.0
        return (base + quote) > cap


@dataclass
class LiveBroker(Broker):
    """ccxt-backed live broker with layered guardrails.

    Construct via :meth:`from_settings` in production so every gate is wired
    from config. The raw constructor is kept simple for tests.
    """

    cfg: ExecutionConfig
    api_key: str = ""
    api_secret: str = ""
    passphrase: str = ""
    _client: object | None = None            # ccxt.Exchange (injected or lazily built)
    _daily: _DailySpend = field(default_factory=_DailySpend)

    def __post_init__(self) -> None:
        if not self.cfg.live_enabled:
            log.warning(
                "live_broker_disabled",
                hint="set execution.live_enabled=true (and dry_run=false) to trade",
            )
            return
        # Verify key safety up front — refuse to even build a live client with
        # a withdrawal-capable key.
        if self.cfg.require_trade_only_key:
            self._verify_key_is_trade_only()

    # ---- construction ----------------------------------------------------

    @classmethod
    def from_settings(cls, settings: object) -> LiveBroker:
        """Build from a Settings object, pulling the venue's key pair."""
        cfg = settings.execution  # type: ignore[attr-defined]
        venue = cfg.venue
        if venue == "binance":
            key, secret, passph = settings.binance_api_key, settings.binance_api_secret, ""  # type: ignore[attr-defined]
        elif venue == "okx":
            key = settings.okx_api_key  # type: ignore[attr-defined]
            secret = settings.okx_api_secret  # type: ignore[attr-defined]
            passph = settings.okx_passphrase  # type: ignore[attr-defined]
        else:
            key = secret = passph = ""
        return cls(cfg=cfg, api_key=key, api_secret=secret, passphrase=passph)

    def _ccxt(self) -> object:
        """Lazily build (or return injected) ccxt client."""
        if self._client is not None:
            return self._client
        import ccxt  # local import; heavy dependency

        klass = getattr(ccxt, self.cfg.venue)
        params: dict[str, object] = {
            "apiKey": self.api_key,
            "secret": self.api_secret,
            "enableRateLimit": True,
            "options": {"defaultType": "spot"},
        }
        if self.passphrase:
            params["password"] = self.passphrase
        self._client = klass(params)
        return self._client

    def _market_limits(self, symbol: str) -> dict[str, float | None]:
        client = self._ccxt()
        load_markets = getattr(client, "load_markets", None)
        if not callable(load_markets):
            return {"amount_min": None, "cost_min": None}
        markets = load_markets()
        if not isinstance(markets, dict):
            return {"amount_min": None, "cost_min": None}
        raw_market = markets.get(symbol)
        if not isinstance(raw_market, dict):
            raise GuardrailBlockedError(f"symbol {symbol} is not listed on venue {self.cfg.venue}")
        if raw_market.get("spot") is False:
            raise GuardrailBlockedError(f"symbol {symbol} is not a spot market")
        limits = raw_market.get("limits")
        if not isinstance(limits, dict):
            return {"amount_min": None, "cost_min": None}
        amount_limits = limits.get("amount")
        cost_limits = limits.get("cost")
        return {
            "amount_min": _optional_positive_float(amount_limits.get("min"))
            if isinstance(amount_limits, dict)
            else None,
            "cost_min": _optional_positive_float(cost_limits.get("min"))
            if isinstance(cost_limits, dict)
            else None,
        }

    # ---- safety gates ----------------------------------------------------

    def _verify_key_is_trade_only(self) -> None:
        """Refuse keys that can withdraw. Best-effort across venues.

        Uses ccxt's unified ``fetch_permissions`` where available; otherwise
        falls back to venue-specific account info. If we cannot positively
        confirm withdrawal is disabled, we refuse (fail-closed).
        """
        try:
            client = self._ccxt()
        except Exception as exc:  # ccxt missing or bad config
            raise UnsafeApiKeyError(
                f"cannot build venue client to verify key safety: {exc}"
            ) from exc

        can_withdraw = self._probe_withdraw_permission(client)
        if can_withdraw is None:
            raise UnsafeApiKeyError(
                "could not verify the API key is withdrawal-disabled; refusing to "
                "trade live. Create a trade-only key (no withdrawal scope) and, "
                "ideally, an IP allowlist on the exchange side."
            )
        if can_withdraw:
            raise UnsafeApiKeyError(
                "API key has WITHDRAWAL permission — refusing to trade live. "
                "Use a trade-only key with withdrawals disabled."
            )
        log.info("live_key_verified_trade_only", venue=self.cfg.venue)

    @staticmethod
    def _probe_withdraw_permission(client: object) -> bool | None:
        """Return True if key can withdraw, False if not, None if unknown."""
        # Binance: sapi GET /sapi/v1/account/apiRestrictions -> enableWithdrawals
        fetch = getattr(client, "sapiGetAccountApirestrictions", None)
        if callable(fetch):
            try:
                info = fetch()
                if isinstance(info, dict) and "enableWithdrawals" in info:
                    return bool(info["enableWithdrawals"])
            except Exception:
                return None
        # ccxt unified (some venues)
        fetch_perms = getattr(client, "fetch_permissions", None)
        if callable(fetch_perms):
            try:
                perms = fetch_perms()
                if isinstance(perms, dict):
                    w = perms.get("withdraw")
                    if w is not None:
                        return bool(w)
            except Exception:
                return None
        return None

    def _check_gates(self, order: Order, mark_price: float, now: datetime) -> None:
        """Run every pre-trade guardrail; raise on the first failure."""
        if not self.cfg.live_enabled:
            raise LiveTradingDisabledError(
                "live trading is disabled (execution.live_enabled=false)"
            )
        if not math.isfinite(order.qty) or order.qty <= 0:
            raise GuardrailBlockedError("order must have a positive finite quantity")
        if not math.isfinite(mark_price) or mark_price <= 0:
            raise GuardrailBlockedError("order must have a positive finite mark_price")
        # Kill switch file
        if self.cfg.kill_file and Path(self.cfg.kill_file).exists():
            raise GuardrailBlockedError(
                f"KILL file present at {self.cfg.kill_file}; all orders blocked"
            )
        # Symbol allowlist
        if order.symbol != self.cfg.symbol:
            raise GuardrailBlockedError(
                f"symbol {order.symbol} not allowed; only {self.cfg.symbol} is enabled"
            )
        if order.type != OrderType.MARKET:
            raise GuardrailBlockedError("only MARKET orders are allowed in live mode")

        notional = abs(order.qty) * mark_price
        if notional > self.cfg.max_order_quote:
            raise GuardrailBlockedError(
                f"order notional {notional:.2f} exceeds max_order_quote "
                    f"{self.cfg.max_order_quote:.2f}"
                )
        if not self.cfg.dry_run:
            limits = self._market_limits(order.symbol)
            amount_min = limits["amount_min"]
            cost_min = limits["cost_min"]
            if amount_min is not None and abs(order.qty) < amount_min:
                raise GuardrailBlockedError(
                    f"order quantity {abs(order.qty):.12g} is below exchange minimum "
                    f"{amount_min:.12g}"
                )
            if cost_min is not None and notional < cost_min:
                raise GuardrailBlockedError(
                    f"order notional {notional:.2f} is below exchange minimum notional "
                    f"{cost_min:.2f}"
                )
        if order.side == OrderSide.BUY:
            if not self.cfg.dry_run:
                available_cash = self.cash()
                estimated_fee = notional * (self.cfg.fee_bps / 10_000)
                required_cash = notional + estimated_fee
                if available_cash + 1e-9 < required_cash:
                    raise GuardrailBlockedError(
                        f"available USDT {available_cash:.2f} is below required "
                        f"{required_cash:.2f}"
                    )
            if self._daily.would_exceed(notional, self.cfg.max_daily_spend_quote, now):
                raise GuardrailBlockedError(
                    f"buy would exceed daily spend cap {self.cfg.max_daily_spend_quote:.2f} "
                    f"(already spent {self._daily.spent_quote:.2f} today)"
                )
            # Total exposure cap
            try:
                current = self.position_qty(order.symbol) * mark_price
            except Exception:
                current = 0.0
            if current + notional > self.cfg.max_total_exposure_quote:
                raise GuardrailBlockedError(
                    f"buy would exceed total exposure cap "
                    f"{self.cfg.max_total_exposure_quote:.2f} (current {current:.2f})"
                )
        elif not self.cfg.dry_run:
            available_qty = self.position_qty(order.symbol)
            if available_qty + 1e-12 < abs(order.qty):
                base = order.symbol.split("/")[0]
                raise GuardrailBlockedError(
                    f"available {base} {available_qty:.12g} is below sell quantity "
                    f"{abs(order.qty):.12g}"
                )

    # ---- broker surface --------------------------------------------------

    def submit(self, order: Order, mark_price: float) -> Trade:
        now = datetime.now(tz=timezone.utc)
        self._check_gates(order, mark_price, now)

        notional = abs(order.qty) * mark_price

        if self.cfg.dry_run:
            log.warning(
                "live_order_dry_run",
                symbol=order.symbol, side=order.side.value, qty=order.qty,
                mark_price=mark_price, notional=notional,
                note="DRY RUN — order NOT sent. Set execution.dry_run=false to go live.",
            )
            return Trade(
                ts=now, symbol=order.symbol, side=order.side, qty=order.qty,
                price=mark_price, fee=0.0, venue="paper",
                note=f"DRY_RUN {order.note}",
            )

        # --- real submission -------------------------------------------------
        client = self._ccxt()
        client_oid = order.client_id or f"qt-{int(now.timestamp() * 1000)}"
        try:
            resp = client.create_order(  # type: ignore[attr-defined]
                symbol=order.symbol,
                type="market",
                side=order.side.value,
                amount=abs(order.qty),
                params=_client_order_id_params(self.cfg.venue, client_oid),
            )
        except Exception as exc:
            log.error("live_order_failed", error=str(exc), symbol=order.symbol)
            raise

        if not isinstance(resp, dict):
            raise LiveOrderRejectedError("exchange returned a non-object order response")
        status = str(resp.get("status") or "").lower()
        if status in {"canceled", "cancelled", "rejected", "expired"}:
            raise LiveOrderRejectedError(f"exchange order {status} with no fill")

        filled = _positive_float(resp.get("filled"))
        if filled is None:
            raise LiveOrderRejectedError("exchange response contains no fill quantity")
        fill_px = _positive_float(resp.get("average") or resp.get("price"))
        if fill_px is None:
            cost = _positive_float(resp.get("cost"))
            if cost is not None:
                fill_px = cost / filled
        if fill_px is None:
            raise LiveOrderRejectedError("exchange response contains no fill price")
        fee_cost = 0.0
        fee = resp.get("fee") or {}
        if isinstance(fee, dict) and fee.get("cost") is not None:
            fee_cost = float(fee["cost"])

        if order.side == OrderSide.BUY:
            self._daily.add(filled * fill_px, now)

        log.info(
            "live_order_filled",
            symbol=order.symbol, side=order.side.value, qty=filled,
            price=fill_px, fee=fee_cost, client_oid=client_oid,
        )
        return Trade(
            ts=now, symbol=order.symbol, side=order.side, qty=filled,
            price=fill_px, fee=fee_cost, venue=self.cfg.venue,  # type: ignore[arg-type]
            note=order.note,
        )

    def cash(self) -> float:
        if self.cfg.dry_run or not self.cfg.live_enabled:
            raise LiveTradingDisabledError("cash() requires a live (non-dry-run) client")
        client = self._ccxt()
        bal = client.fetch_balance()  # type: ignore[attr-defined]
        free = bal.get("free", {}) if isinstance(bal, dict) else {}
        return float(free.get("USDT", 0.0))

    def position_qty(self, symbol: str) -> float:
        if self.cfg.dry_run or not self.cfg.live_enabled:
            return 0.0
        client = self._ccxt()
        bal = client.fetch_balance()  # type: ignore[attr-defined]
        base = symbol.split("/")[0]
        free = bal.get("total", {}) if isinstance(bal, dict) else {}
        return float(free.get(base, 0.0))

    def reconcile(self, symbol: str | None = None) -> dict[str, float]:
        """Read venue balances so the ledger can be rebuilt from truth.

        Never trust in-memory state on startup — always reconcile against the
        exchange. Returns {"cash": ..., "position_qty": ...}.
        """
        if self.cfg.dry_run or not self.cfg.live_enabled:
            return {"cash": 0.0, "position_qty": 0.0}
        sym = symbol or self.cfg.symbol
        return {"cash": self.cash(), "position_qty": self.position_qty(sym)}


def _optional_positive_float(value: object) -> float | None:
    parsed = _positive_float(value)
    return parsed if parsed is not None and parsed > 0 else None


def _positive_float(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(cast(Any, value))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed) or parsed <= 0:
        return None
    return parsed


def _client_order_id_params(venue: str, client_order_id: str) -> dict[str, str]:
    if venue == "binance":
        return {"newClientOrderId": client_order_id}
    if venue == "okx":
        return {"clOrdId": client_order_id}
    if venue == "bybit":
        return {"orderLinkId": client_order_id}
    if venue == "coinbase":
        return {"client_order_id": client_order_id}
    return {"clientOrderId": client_order_id}
