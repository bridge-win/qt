# Migrated verbatim from btcqt; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
"""Configuration loading: config.yaml (parameters) + environment (.env secrets)."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator


class AccountCfg(BaseModel):
    initial_equity: float = 5000
    maker_fee: float = 0.0002
    taker_fee: float = 0.0005
    stop_slippage_bps: float = 5


class IndicatorCfg(BaseModel):
    sigma_span_1m: int = 240
    ema_anchor_min: int = 60
    atr_period_1m: int = 60
    vb_window: int = 120
    liq_window_sec: int = 60
    liq_pctl_lookback_min: int = 1440
    funding_pctl_days: int = 30
    rv_window_min: int = 1440
    rv_pctl_lookback_days: int = 365
    trend_tf_min: int = 240
    adx_period: int = 14
    adx_strong: float = 25


class EventCfg(BaseModel):
    """Extreme-move trigger and rejection/acceptance classifier thresholds."""
    z_trigger: float = 3.0
    liq_oi_pctl_trigger: float = 99.0
    doi_pctl_trigger: float = 1.0
    pre_window_min: int = 60
    max_age_min: int = 120
    reclaim_min: float = 0.30
    reclaim_min_degraded: float = 0.40
    xvenue_dev_big: float = 1.0
    accept_hold_min: int = 10

    @model_validator(mode="after")
    def validate_thresholds(self):
        if self.z_trigger <= 0 or not 0 <= self.doi_pctl_trigger <= 100:
            raise ValueError("event z/percentile thresholds are invalid")
        if not (0 < self.reclaim_min <= self.reclaim_min_degraded <= 1.5):
            raise ValueError("degraded reclaim must be at least as strict as full-data reclaim")
        if self.pre_window_min < 30 or self.max_age_min < self.accept_hold_min:
            raise ValueError("event windows are internally inconsistent")
        return self


class CompositeCfg(BaseModel):
    cascade_weights: dict[str, float] = Field(default={"vel": 0.30, "liq": 0.30, "vb": 0.20, "doi": 0.20})
    cascade_threshold: float = 70
    post_cascade_min: int = 120
    crowding_weights: dict[str, float] = Field(default={"funding": 0.50, "oi": 0.20, "lsr": 0.15, "basis": 0.15})
    crowding_extreme: float = 60
    eatfear_weights: dict[str, float] = Field(default={"fear": 0.20, "neg_funding": 0.25, "cascade_down": 0.35, "liq24h": 0.20})


class S0Cfg(BaseModel):
    """Strategy A: volatility-adjusted trend following (plan v2.1, largest risk sleeve)."""
    enabled: bool = True
    risk_per_trade: float = 0.0035        # 0.35% (review: 0.30-0.40%)
    min_abs_t: float = 0.30               # minimum |trend score|
    min_agree: int = 2                    # >=2 of 72h/7d/14d horizons must agree
    stop_atr1h_mult: float = 2.5          # initial stop (review: 2-3 ATR, plateau-tested)
    cooldown_min: int = 120
    high_vol_rv_pctl: float = 90          # above this, halve size (vol targeting)
    high_vol_size_mult: float = 0.5


class S1Cfg(BaseModel):
    enabled: bool = True
    # entry_mode "confirm": trade only after a REJECT verdict (reclaim confirmed) —
    # live default. "ladder": legacy passive rungs inside the wick — backtest A/B variant.
    entry_mode: Literal["confirm", "ladder"] = "confirm"
    risk_per_trade: float = 0.002         # confirm-mode risk (review: 0.15-0.25%)
    stop_buffer_atr: float = 0.3          # hard stop beyond event extreme + buffer
    time_stop_min: int = 60               # review: 30-90 min
    cooldown_min: int = 30
    entry_slippage_bps: float = 20.0     # IOC limit cap; unfilled remainder is abandoned
    # ---- ladder-mode parameters (legacy variant) ----
    rung_devs: list[float] = Field(default=[3.0, 3.75, 4.5])
    risk_per_rung: float = 0.002
    max_rungs_filled: int = 3
    tp_revert_frac: float = 0.40
    stop_atr_mult: float = 0.8
    refresh_move_sigma: float = 0.3
    freeze_liq_pctl: float = 99
    freeze_doi_5m: float = -0.015


class S2Cfg(BaseModel):
    enabled: bool = False                # research/shadow only until crowding filter is validated
    funding_pctl_hi: float = 97.5
    funding_pctl_lo: float = 2.5
    min_persist_intervals: int = 2
    oi_pctl_min: float = 80
    risk_per_trade: float = 0.003         # tightened (was 0.75%)
    stop_atr4h_mult: float = 1.5
    time_stop_hours: int = 72
    funding_exit_pctl: float = 70


class RiskCfg(BaseModel):
    # v2.1 tightened defaults (review §5 adopted)
    daily_soft_loss_halt: float = 0.007   # stop new entries; keep protected winners
    daily_loss_halt: float = 0.012        # hard halt + flatten
    weekly_loss_halt: float = 0.025       # was 0.06
    system_drawdown_halt: float = 0.06    # NEW: equity -6% from peak -> full stop, manual review
    max_gross_leverage: float = 1.0       # was 2.0
    max_position_notional_frac: float = 1.0
    price_sanity_band: float = 0.05
    data_staleness_sec: int = 10
    spread_guard_pctl: float = 99         # NEW: no new entries when spread above this percentile
    max_order_notional: float = 50_000    # conservative cap until L2 capacity sizing is live
    min_stop_distance_bps: float = 5
    strategy_notional_caps: dict[str, float] = Field(
        default={"s0": 0.75, "s1": 0.25, "s2": 0.0})

    @model_validator(mode="after")
    def validate_limits(self):
        rates = (self.daily_soft_loss_halt, self.daily_loss_halt,
                 self.weekly_loss_halt, self.system_drawdown_halt)
        if any(x <= 0 or x >= 0.5 for x in rates):
            raise ValueError("risk loss limits must be between 0 and 50%")
        if self.daily_soft_loss_halt >= self.daily_loss_halt:
            raise ValueError("daily soft halt must precede the hard daily halt")
        if not 0 < self.max_gross_leverage <= 2:
            raise ValueError("max gross leverage must be in (0, 2]")
        if self.max_order_notional <= 0 or self.max_position_notional_frac <= 0:
            raise ValueError("notional limits must be positive")
        if any(v < 0 or v > 1 for v in self.strategy_notional_caps.values()):
            raise ValueError("strategy notional caps must be fractions in [0, 1]")
        if not 0 <= self.spread_guard_pctl <= 100:
            raise ValueError("spread guard percentile must be in [0, 100]")
        return self


class DataCfg(BaseModel):
    """Multi-venue collection (v2.1): Bybit's all-liquidation stream is UNTHROTTLED,
    so it is the primary liquidation source; Binance's snapshot feed is fallback."""
    bybit_enabled: bool = True
    okx_enabled: bool = True
    liq_primary: str = "bybit"            # bybit | binance
    liq_fallback_stale_sec: int = 30
    bybit_symbol: str = "BTCUSDT"
    okx_inst: str = "BTC-USDT-SWAP"


class ApiCfg(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8080
    cors_origins: list[str] = Field(default_factory=list)
    history_max_rows: int = 20000
    dashboard_enabled: bool = True
    control_token_required: bool = True
    backtest_max_days: int = 365

    @model_validator(mode="after")
    def validate_api(self):
        if not 1 <= self.port <= 65535 or self.history_max_rows < 100:
            raise ValueError("invalid API port/history limit")
        if not 1 <= self.backtest_max_days <= 730:
            raise ValueError("backtest_max_days must be in [1, 730]")
        return self


class TelegramCfg(BaseModel):
    daily_report_hour_utc: int = 0


class WatchdogCfg(BaseModel):
    check_interval_sec: int = 5


class Secrets(BaseModel):
    binance_api_key: str = ""
    binance_api_secret: str = ""
    live_trading: bool = False
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    metrics_api_token: str = ""
    control_api_token: str = ""
    coinglass_api_key: str = ""


class Config(BaseModel):
    symbol: str = "BTCUSDT"
    data_dir: str = "data"
    state_dir: str = "state"
    account: AccountCfg = AccountCfg()
    indicators: IndicatorCfg = IndicatorCfg()
    event: EventCfg = EventCfg()
    composite: CompositeCfg = CompositeCfg()
    s0: S0Cfg = S0Cfg()
    s1: S1Cfg = S1Cfg()
    s2: S2Cfg = S2Cfg()
    risk: RiskCfg = RiskCfg()
    data: DataCfg = DataCfg()
    api: ApiCfg = ApiCfg()
    telegram: TelegramCfg = TelegramCfg()
    watchdog: WatchdogCfg = WatchdogCfg()
    secrets: Secrets = Secrets()

    @property
    def data_path(self) -> Path:
        p = Path(self.data_dir)
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def state_path(self) -> Path:
        p = Path(self.state_dir)
        p.mkdir(parents=True, exist_ok=True)
        return p


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader (no extra dependency). Does not override real env vars."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip().strip('"').strip("'")
        os.environ.setdefault(k, v)


def load_config(path: str | Path = "config/config.yaml") -> Config:
    path = Path(path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    _load_dotenv(Path(".env"))
    secrets = Secrets(
        binance_api_key=os.environ.get("BINANCE_API_KEY", ""),
        binance_api_secret=os.environ.get("BINANCE_API_SECRET", ""),
        live_trading=os.environ.get("LIVE_TRADING", "no").lower() == "yes",
        telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID", ""),
        metrics_api_token=os.environ.get("METRICS_API_TOKEN", ""),
        control_api_token=os.environ.get("CONTROL_API_TOKEN", ""),
        coinglass_api_key=os.environ.get("COINGLASS_API_KEY", ""),
    )
    cfg = Config(**(raw or {}))
    cfg.secrets = secrets
    return cfg

