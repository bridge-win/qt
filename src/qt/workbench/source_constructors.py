"""Version-pinned constructors for source-native Lab builtin identities.

These constructors are intentionally narrower than the strategy catalog. A
source family is admitted only when its preserved port and native order/fill
adapter can express the original semantics. Qt5 gallery interfaces stay in
``legacy_workflows`` because they produce evaluations or historical replays,
not generic execution strategies.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal

from btc_backtest.strategies.base import Strategy

from qt.strategy_ports.btcqt import (
    BTCQT_PORTS,
    BtcqtCausalBundle,
    BtcqtNativeStrategy,
    create_btcqt_port,
)
from qt.strategy_ports.freqtrade import (
    FREQTRADE_PORTS,
    FreqtradeNativeStrategy,
    create_freqtrade_port,
)
from qt.workbench.catalog import profile


def build_source_strategy(
    version: Mapping[str, object],
    parameters: Mapping[str, object],
    *,
    dataset_version: str,
    causal_bundle: BtcqtCausalBundle | Mapping[str, object] | None = None,
    leverage: Decimal = Decimal("1"),
) -> Strategy:
    """Construct one source-native strategy or fail before execution starts."""

    content = _mapping(version.get("content"), "source strategy content")
    identity = _text(content.get("builtin_identity"), "builtin_identity")
    expected_version = _text(content.get("builtin_version"), "builtin_version")
    if identity.startswith("catalog:"):
        profile_id = identity.removeprefix("catalog:")
        source_profile = profile(profile_id)
        actual_version = str(source_profile.get("version", 1))
        if expected_version != actual_version:
            raise ValueError(
                f"catalog source version mismatch for {identity}: "
                f"expected {expected_version}, got {actual_version}"
            )
        return FreqtradeNativeStrategy(
            create_freqtrade_port(
                "btc_quant_catalog",
                profile_id=profile_id,
                parameters=parameters,
            ),
            dataset_version=dataset_version,
        )
    if identity.startswith("freqtrade:"):
        strategy_id = identity.removeprefix("freqtrade:")
        metadata = FREQTRADE_PORTS.get(strategy_id)
        if metadata is None:
            raise ValueError(f"unknown preserved Freqtrade source identity: {identity}")
        if expected_version != metadata.source.commit:
            raise ValueError(
                f"Freqtrade source version mismatch for {identity}: "
                f"expected {expected_version}, got {metadata.source.commit}"
            )
        return FreqtradeNativeStrategy(
            create_freqtrade_port(strategy_id, parameters=parameters),
            dataset_version=dataset_version,
        )
    if identity.startswith("btcqt:"):
        strategy_id = identity.removeprefix("btcqt:")
        metadata = BTCQT_PORTS.get(strategy_id)
        if metadata is None:
            raise ValueError(f"unknown preserved btc-qt source identity: {identity}")
        if expected_version != metadata.source.commit:
            raise ValueError(
                f"btc-qt source version mismatch for {identity}: "
                f"expected {expected_version}, got {metadata.source.commit}",
            )
        parsed_bundle = (
            causal_bundle
            if isinstance(causal_bundle, BtcqtCausalBundle)
            else BtcqtCausalBundle.from_payload(causal_bundle)
            if causal_bundle is not None
            else None
        )
        return BtcqtNativeStrategy(
            create_btcqt_port(strategy_id, parameters=parameters),
            dataset_version=dataset_version,
            causal_bundle=parsed_bundle,
            leverage=leverage,
        )
    if identity.startswith(("qt5live:", "qt5sim:")):
        raise ValueError(
            f"{identity} is a Qt5 evaluation/replay workflow, not a native order strategy; "
            "use qt.workbench.legacy_workflows"
        )
    raise ValueError(f"unsupported source-native builtin identity: {identity}")


def _mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be an object")
    return value


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required")
    return value.strip()


__all__ = ["build_source_strategy"]
