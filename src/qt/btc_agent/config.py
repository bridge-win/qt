"""Validated operational limits; there is deliberately no real-money mode."""

from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class AgentConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="BTC_AGENT_", allow_inf_nan=False)

    mode: Literal["paper", "testnet"] = "paper"
    test_environment: Literal["binance_spot_testnet", "okx_demo"] = "binance_spot_testnet"
    state_dir: Path = Path("data/runtime/btc-agent")
    poll_seconds: int = Field(default=300, ge=10, le=3600)
    max_order_quote: float = Field(default=25, ge=10, le=1000)
    max_daily_buy_quote: float = Field(default=25, ge=10, le=1000)
    max_position_quote: float = Field(default=100, ge=10, le=5000)
    cooldown_days: int = Field(default=7, ge=1)
    stop_loss: float = Field(default=0.10, gt=0, le=0.5)
    trailing_loss: float = Field(default=0.15, gt=0, le=0.5)
    trailing_activation: float = Field(default=0.20, gt=0, le=1)
    max_hold_days: int = Field(default=90, ge=1)
    max_spread_bps: float = Field(default=50, gt=0, le=500)
    limit_offset_bps: float = Field(default=10, ge=0, le=100)
    fee_reserve: float = Field(default=0.005, ge=0.001, le=0.05)
    max_quote_age_seconds: int = Field(default=30, ge=1, le=120)
    max_entry_deviation: float = Field(default=0.05, gt=0, le=0.25)
    reviewer_url: str = "http://reviewer:8090/review"
    reviewer_timeout_seconds: int = Field(default=120, ge=10, le=300)

    @property
    def state_namespace(self) -> str:
        # Preserve the existing Binance ledger; never mix different venues' intents.
        if self.test_environment == "okx_demo":
            return f"{self.mode}-okx-demo"
        return self.mode

    @property
    def database(self) -> Path:
        return self.state_dir / f"{self.state_namespace}.sqlite3"

    @property
    def kill_file(self) -> Path:
        return self.state_dir / "KILL"
