# Migrated verbatim from btc_quant_main; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
from dataclasses import dataclass


@dataclass(frozen=True)
class PositionSize:
    stake_amount: float
    asset_amount: float
    risk_amount: float
    risk_fraction: float
    stop_distance_fraction: float


def calculate_atr_stop(entry_price: float, atr: float, atr_multiple: float) -> float:
    if entry_price <= 0:
        raise ValueError("entry_price must be positive")
    if atr <= 0:
        raise ValueError("atr must be positive")
    if atr_multiple <= 0:
        raise ValueError("atr_multiple must be positive")

    return max(0.0, entry_price - (atr * atr_multiple))


def calculate_position_size(
    *,
    equity: float,
    entry_price: float,
    stop_price: float,
    risk_fraction: float,
    max_position_value: float,
    min_position_value: float = 0.0,
) -> PositionSize:
    if equity <= 0:
        raise ValueError("equity must be positive")
    if entry_price <= 0:
        raise ValueError("entry_price must be positive")
    if not 0 < stop_price < entry_price:
        raise ValueError("stop_price must be between zero and entry_price")
    if not 0 < risk_fraction < 1:
        raise ValueError("risk_fraction must be between zero and one")
    if max_position_value <= 0:
        raise ValueError("max_position_value must be positive")
    if min_position_value < 0:
        raise ValueError("min_position_value cannot be negative")

    risk_amount = equity * risk_fraction
    stop_distance_fraction = (entry_price - stop_price) / entry_price
    raw_stake = risk_amount / stop_distance_fraction
    stake_amount = min(raw_stake, max_position_value)

    if stake_amount < min_position_value:
        stake_amount = 0.0

    asset_amount = stake_amount / entry_price
    effective_risk_amount = stake_amount * stop_distance_fraction

    return PositionSize(
        stake_amount=stake_amount,
        asset_amount=asset_amount,
        risk_amount=effective_risk_amount,
        risk_fraction=effective_risk_amount / equity,
        stop_distance_fraction=stop_distance_fraction,
    )


def can_open_trade(
    *,
    existing_open_risk_fraction: float,
    new_trade_risk_fraction: float,
    max_total_open_risk_fraction: float,
) -> bool:
    if existing_open_risk_fraction < 0:
        raise ValueError("existing_open_risk_fraction cannot be negative")
    if new_trade_risk_fraction < 0:
        raise ValueError("new_trade_risk_fraction cannot be negative")
    if max_total_open_risk_fraction <= 0:
        raise ValueError("max_total_open_risk_fraction must be positive")

    return existing_open_risk_fraction + new_trade_risk_fraction <= max_total_open_risk_fraction

