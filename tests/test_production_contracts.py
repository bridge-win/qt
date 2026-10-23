"""Public contracts reject lossy values and freeze signed content recursively."""

from __future__ import annotations

import json
from decimal import Decimal, localcontext

import pytest
from pydantic import ValidationError

from qt.contracts import (
    CostModel,
    ForecastRecord,
    FrozenDict,
    MarketEvent,
    MetricResult,
    OrderIdentity,
    OrderIntent,
    OrderSnapshot,
    RiskBudget,
    SubmitResult,
    canonical_json,
    content_hash,
)

HASH = "a" * 64


def order(**changes: object) -> OrderIntent:
    fields: dict[str, object] = {
        "intent_id": "i1", "account_id": "a1", "deployment_id": "d1", "signal_id": "s1",
        "venue": "binance", "instrument": "BTCUSDT", "side": "buy", "quantity": "0.0100",
        "limit_price": "50000.00", "time_in_force": "IOC", "strategy_hash": HASH,
        "policy_hash": HASH, "fee_schedule_hash": HASH, "instrument_spec_hash": HASH,
        "risk_reservation_id": "r1", "created_at_ns": 100, "valid_until_ns": 200,
    }
    return OrderIntent(**{**fields, **changes})


def forecast(**changes: object) -> ForecastRecord:
    fields: dict[str, object] = {
        "forecast_id": "f1", "event_id": "e1", "event_cluster_id": "cluster1",
        "market_id": "m1", "outcome_id": "yes", "probability": "0", "decision_at_ns": 100,
        "horizon_end_ns": 200, "model_provider": "fixture", "model_id": "frozen-1",
        "prompt_hash": HASH, "rules_hash": HASH, "prediction_mode": "blind",
        "market_probability_missing_reason": "no_quote_at_decision_time",
    }
    return ForecastRecord(**{**fields, **changes})


@pytest.mark.parametrize("value", [0.01, 1, True, "NaN", "Infinity", " 0.01", "1e999999"])
def test_financial_amounts_reject_lossy_or_nonfinite_values(value: object) -> None:
    with pytest.raises(ValidationError):
        order(quantity=value)


@pytest.mark.parametrize("value", ["0", "-0.1", "-0"])
def test_order_quantity_must_be_positive(value: str) -> None:
    with pytest.raises(ValidationError):
        order(quantity=value)


def test_decimal_transport_and_content_hash_ignore_redundant_zeros() -> None:
    original = order()
    assert original.quantity == Decimal("0.01")
    assert original.model_dump(mode="json")["quantity"] == "0.01"
    assert original.content_hash() == order(quantity="1e-2", limit_price="5e4").content_hash()
    assert OrderIntent.model_validate_json(original.model_dump_json()) == original
    with pytest.raises(ValidationError):
        original.quantity = Decimal("2")


def test_canonical_decimal_does_not_depend_on_decimal_context() -> None:
    text = "12345678901234567890.123456789012345678"
    with localcontext() as context:
        context.prec = 6
        item = order(quantity=text)
        assert json.loads(item.canonical_json())["quantity"] == text


@pytest.mark.parametrize("value", ["123456789012345678901", "0.1234567890123456789"])
def test_amount_must_fit_database_precision_without_rounding(value: str) -> None:
    with pytest.raises(ValidationError, match="NUMERIC"):
        order(quantity=value)


def test_unknown_fields_and_noninteger_timestamps_are_forbidden() -> None:
    for changes in ({"surprise": 1}, {"created_at_ns": 100.0}, {"created_at_ns": True},
                    {"valid_until_ns": 100}, {"schema_version": "1.0"}):
        with pytest.raises(ValidationError):
            order(**changes)


def test_payload_is_deeply_immutable_and_copied_from_caller() -> None:
    source = {"nested": {"levels": [["1", "2"]]}}
    event = MarketEvent(venue="fixture", instrument="BTCUSDT", event_type="trade",
                        exchange_ts_ns=1, received_ts_ns=2, payload=source)
    original_hash = event.content_hash()
    source["nested"]["levels"][0][0] = "99"
    assert event.content_hash() == original_hash
    assert isinstance(event.payload, FrozenDict)
    assert isinstance(event.payload["nested"], FrozenDict)
    with pytest.raises(TypeError):
        event.payload["nested"]["levels"][0][0] = "3"
    with pytest.raises(TypeError):
        event.payload._values = {}


def test_json_arrays_work_in_fastapi_style_decoded_objects() -> None:
    event = MarketEvent.model_validate({
        "venue": "fixture", "instrument": "BTCUSDT", "event_type": "trade",
        "exchange_ts_ns": 1, "received_ts_ns": 2, "quality_flags": ["test"],
        "payload": {"levels": [["1", "2"]]},
    })
    assert event.quality_flags == ("test",)
    assert json.loads(event.model_dump_json())["quality_flags"] == ["test"]


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_payload_and_metrics_rejected(value: float) -> None:
    with pytest.raises(ValidationError):
        MarketEvent(venue="fixture", instrument="BTCUSDT", event_type="trade",
                    exchange_ts_ns=1, received_ts_ns=2, payload={"value": value})
    with pytest.raises(ValidationError):
        MetricResult(value=value, sample_count=100, calculation_version="1", start_ns=1, end_ns=2)


def test_unavailable_metric_requires_reason_and_paired_interval() -> None:
    with pytest.raises(ValidationError):
        MetricResult(value=None, sample_count=0, calculation_version="1", start_ns=1, end_ns=2)
    with pytest.raises(ValidationError):
        MetricResult(value=1.0, confidence_lower=0.5, sample_count=10,
                     calculation_version="1", start_ns=1, end_ns=2)


def test_missing_market_probability_stays_missing_and_zero_is_valid() -> None:
    item = forecast()
    assert item.probability == 0
    assert item.market_mid is None
    assert item.event_cluster_id == "cluster1"
    with pytest.raises(ValidationError):
        forecast(market_probability_missing_reason=None)
    assert forecast(market_mid="0", market_probability_missing_reason=None).market_mid == 0


def test_cost_model_supports_nonzero_slippage() -> None:
    costs = CostModel(version="v2", fee_schedule_hashes=(HASH,), slippage_bps="12.5")
    assert costs.slippage_bps == Decimal("12.5")


def test_risk_defaults_are_explicit_and_order_unknown_is_not_rejected() -> None:
    budget = RiskBudget(capital="10000", currency="USDT")
    assert budget.maximum_daily_loss_fraction == Decimal("0.005")
    identity = OrderIdentity(venue="fixture", account_id="a1", intent_id="i1")
    result = SubmitResult(status="unknown", identity=identity, received_at_ns=1, reason="timeout")
    assert result.status == "unknown"
    with pytest.raises(ValidationError):
        OrderSnapshot(identity=identity, status="FILLED", quantity="1", filled_quantity="2", asof_ns=1)


def test_canonical_json_orders_keys_and_rejects_nonfinite_raw_json() -> None:
    assert canonical_json({"z": [1], "a": "中文"}) == '{"a":"中文","z":[1]}'
    assert content_hash({"a": 1, "b": 2}) == content_hash({"b": 2, "a": 1})
    with pytest.raises(ValueError):
        canonical_json({"bad": float("nan")})


def test_contracts_produce_openapi_compatible_json_schemas() -> None:
    assert MarketEvent.model_json_schema()["properties"]["payload"]["type"] == "object"
    schema = OrderIntent.model_json_schema()
    assert schema["additionalProperties"] is False
