from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from btc_backtest.signals.calibration import (
    CalibrationWindow,
    ProviderOutcome,
    ReliabilityCalibrator,
    ReliabilitySnapshot,
)

UTC = timezone.utc


def utc(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=UTC)


def outcome(
    suffix: str,
    *,
    provider: str = "a",
    correct: bool,
    horizon_closes_at: str = "2024-01-31T00:00:00",
    payload_sha256: str | None = None,
) -> ProviderOutcome:
    return ProviderOutcome(
        provider=provider,
        observation_id=f"{provider}:{suffix}",
        horizon_closes_at=utc(horizon_closes_at),
        correct=correct,
        payload_sha256=payload_sha256 or ("a" * 64),
    )


def completed_window(
    month: str,
    *,
    wins: int,
    losses: int,
    provider: str = "a",
) -> CalibrationWindow:
    outcomes = tuple(
        outcome(f"w{index}", provider=provider, correct=True)
        for index in range(wins)
    ) + tuple(
        outcome(f"l{index}", provider=provider, correct=False)
        for index in range(losses)
    )
    return CalibrationWindow(
        start=utc(f"{month}-01T00:00:00"),
        end=utc("2024-02-01T00:00:00"),
        outcomes=outcomes,
        source_fingerprint="0" * 64,
    )


def snapshot(
    through: str,
    *,
    provider: str = "a",
    alpha: str = "2",
    beta: str = "2",
) -> ReliabilitySnapshot:
    return ReliabilitySnapshot(
        provider=provider,
        alpha=Decimal(alpha),
        beta=Decimal(beta),
        through=utc(f"{through}T00:00:00"),
        sample_count=0,
        source_fingerprint="1" * 64,
    )


def test_completed_window_updates_beta_prior() -> None:
    previous = snapshot("2023-12-31", alpha="2", beta="2")
    updated = ReliabilityCalibrator().update(
        previous,
        completed_window("2024-01", wins=3, losses=1),
    )

    assert updated.alpha == Decimal("5")
    assert updated.beta == Decimal("3")
    assert updated.reliability == Decimal("0.625")
    assert updated.sample_count == 4
    assert updated.through == utc("2024-02-01T00:00:00")
    assert updated.source_fingerprint != previous.source_fingerprint


def test_window_cannot_calibrate_itself() -> None:
    with pytest.raises(ValueError, match="completed before"):
        ReliabilityCalibrator().weights_for(
            snapshots=(snapshot("2024-01-31"),),
            window_start=utc("2024-01-01T00:00:00"),
        )


def test_outcome_scores_only_after_horizon_closes() -> None:
    previous = snapshot("2023-12-31")
    open_horizon = CalibrationWindow(
        start=utc("2024-01-01T00:00:00"),
        end=utc("2024-02-01T00:00:00"),
        outcomes=(
            outcome(
                "x",
                correct=True,
                horizon_closes_at="2024-02-01T00:00:01",
            ),
        ),
        source_fingerprint="2" * 64,
    )

    with pytest.raises(ValueError, match="horizon closes"):
        ReliabilityCalibrator().update(previous, open_horizon)


def test_update_requires_prior_snapshot_before_window() -> None:
    with pytest.raises(ValueError, match="completed before"):
        ReliabilityCalibrator().update(
            snapshot("2024-01-01"),
            completed_window("2024-01", wins=1, losses=0),
        )


def test_update_is_immutable_and_filters_provider_outcomes() -> None:
    previous = snapshot("2023-12-31", provider="a")
    mixed_window = completed_window("2024-01", wins=1, losses=1)
    mixed_window = mixed_window.model_copy(
        update={
            "outcomes": (
                *mixed_window.outcomes,
                outcome(
                    "b0",
                    provider="b",
                    correct=False,
                    payload_sha256="b" * 64,
                ),
            )
        }
    )

    updated = ReliabilityCalibrator().update(previous, mixed_window)

    assert previous.alpha == Decimal("2")
    assert previous.beta == Decimal("2")
    assert updated.alpha == Decimal("3")
    assert updated.beta == Decimal("3")
    assert updated.sample_count == 2


def test_weights_for_uses_snapshot_or_clamped_fallback_prior() -> None:
    weights = ReliabilityCalibrator(fallback_prior=Decimal("1.5")).weights_for(
        snapshots=(snapshot("2023-12-31", provider="a", alpha="4", beta="1"),),
        window_start=utc("2024-01-01T00:00:00"),
        providers=("a", "b"),
    )

    assert weights == {
        "a": Decimal("0.8"),
        "b": Decimal("0.9"),
    }
