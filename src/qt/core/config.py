"""Configuration loading. YAML + env override via QT_* variables."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ThresholdConfig(BaseModel):
    """Numeric thresholds for the extreme-event detector.

    Defaults are intentionally conservative; they should be re-tuned per
    market regime via walk-forward analysis in `qt.backtest`.
    """

    # Price action
    rsi_oversold: float = 20.0
    bb_std: float = 2.5
    drawdown_30d_min: float = 0.15
    wick_body_ratio_min: float = 3.0

    # Volatility
    rv_ratio_min: float = 2.0
    atr_ratio_min: float = 2.0

    # Derivatives
    funding_rate_8h_max: float = -0.0005  # -0.05% per 8h or more negative
    oi_drop_24h_min: float = 0.10
    long_liq_pct_min: float = 0.95

    # On-chain
    asopr_max: float = 1.0
    mvrv_z_max: float = 0.0
    exchange_netflow_neg: bool = True

    # Sentiment
    fear_greed_max: int = 20
    social_z_max: float = -2.0

    # Macro filters (avoid buying into these regimes)
    vix_max: float = 40.0
    dxy_z_max: float = 2.5

    # Aggregation
    entry_score_min: float = 0.65
    min_factor_groups: int = 4  # how many of {price, vol, deriv, onchain, sentiment} must fire


class RiskConfig(BaseModel):
    base_currency: str = "USDT"
    max_position_pct: float = 0.20         # cap per-trade notional at 20% of equity
    max_total_exposure_pct: float = 0.50   # cap total exposure at 50% of equity
    max_drawdown_pct: float = 0.25         # kill-switch at -25% from equity peak
    cooldown_bars: int = 24                # bars to wait after a losing exit
    kelly_fraction: float = 0.25           # fractional Kelly
    vol_target_annual: float = 0.40        # 40% annualized vol target
    atr_stop_mult: float = 2.5
    atr_take_profit_mult: float = 4.0
    time_stop_bars: int = 120              # close after N bars regardless


class DataConfig(BaseModel):
    primary_symbol: str = "BTC/USDT"
    timeframes: list[str] = Field(default_factory=lambda: ["1h", "4h", "1d"])
    exchanges: list[str] = Field(default_factory=lambda: ["binance"])
    history_days: int = 365 * 3
    parquet_dir: Path = Path("data/parquet")
    cache_dir: Path = Path("data/cache")


class ExecutionConfig(BaseModel):
    mode: str = "paper"             # paper | live
    live_enabled: bool = False
    fee_bps: float = 5.0            # 5 basis points round-trip taker
    slippage_bps: float = 8.0
    max_order_value_usdt: float = 100_000.0
    venue: str = "binance"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="QT_", env_file=".env", extra="ignore")

    env: str = "research"
    log_level: str = "INFO"
    data_dir: Path = Path("./data")

    thresholds: ThresholdConfig = Field(default_factory=ThresholdConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    data: DataConfig = Field(default_factory=DataConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)

    # API keys (loaded from env, never logged)
    binance_api_key: str = ""
    binance_api_secret: str = ""
    okx_api_key: str = ""
    okx_api_secret: str = ""
    okx_passphrase: str = ""
    glassnode_api_key: str = ""
    cryptoquant_api_key: str = ""
    coinglass_api_key: str = ""
    santiment_api_key: str = ""
    lunarcrush_api_key: str = ""
    fred_api_key: str = ""
    newsapi_key: str = ""
    cryptopanic_token: str = ""

    live_trading_enabled: bool = False


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in override.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_settings(config_path: str | Path | None = None) -> Settings:
    """Load Settings from optional YAML, then overlay env (.env / process env)."""

    yaml_data: dict[str, Any] = {}
    if config_path is not None:
        p = Path(config_path)
        if p.exists():
            with p.open() as fh:
                yaml_data = yaml.safe_load(fh) or {}

    # Build Settings: env always wins, but YAML provides defaults for nested models.
    env_settings = Settings()
    if not yaml_data:
        return env_settings

    base = env_settings.model_dump()
    merged = _deep_merge(base, yaml_data)
    return Settings(**merged)
