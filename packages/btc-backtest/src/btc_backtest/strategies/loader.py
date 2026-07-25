"""Explicit file and opt-in entry-point loading for custom strategies."""

from __future__ import annotations

import hashlib
import importlib.util
import inspect
from importlib.metadata import entry_points
from pathlib import Path
from types import ModuleType

from btc_backtest.errors import StrategyLoadError
from btc_backtest.strategies.base import Strategy, StrategyMetadata

ENTRY_POINT_GROUP = "btc_backtest.strategies"


def load_strategy(reference: str) -> Strategy:
    """Load and validate one explicitly named ``file.py:ClassName`` strategy."""

    path_text, separator, class_name = reference.rpartition(":")
    if (
        not separator
        or not path_text
        or not class_name.isidentifier()
        or not path_text.endswith(".py")
    ):
        raise StrategyLoadError(
            "strategy reference must use exact file.py:ClassName syntax"
        )
    try:
        path = Path(path_text).expanduser().resolve(strict=True)
    except OSError as error:
        raise StrategyLoadError(f"strategy file does not exist: {path_text}") from error
    if not path.is_file() or path.suffix != ".py":
        raise StrategyLoadError("strategy reference must point to one Python file")

    module = _load_module(path)
    candidate = getattr(module, class_name, None)
    if candidate is None:
        raise StrategyLoadError(
            f"strategy class {class_name} was not found in {path}"
        )
    return _construct_and_validate(candidate, f"{path}:{class_name}")


def discover_entry_point_strategies() -> dict[str, Strategy]:
    """Discover custom strategies only when explicitly requested by the caller."""

    discovered: dict[str, Strategy] = {}
    for item in entry_points(group=ENTRY_POINT_GROUP):
        try:
            candidate = item.load()
        except Exception as error:
            raise StrategyLoadError(
                f"failed to load strategy entry point {item.name}"
            ) from error
        strategy = _construct_and_validate(
            candidate,
            f"entry point {item.name}",
        )
        strategy_id = strategy.metadata.id
        if strategy_id in discovered:
            raise StrategyLoadError(
                f"duplicate strategy id discovered: {strategy_id}"
            )
        discovered[strategy_id] = strategy
    return discovered


def _load_module(path: Path) -> ModuleType:
    digest = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:16]
    module_name = f"_btc_backtest_custom_{digest}"
    specification = importlib.util.spec_from_file_location(module_name, path)
    if specification is None or specification.loader is None:
        raise StrategyLoadError(f"cannot create import specification for {path}")
    module = importlib.util.module_from_spec(specification)
    try:
        specification.loader.exec_module(module)
    except Exception as error:
        raise StrategyLoadError(f"failed to import strategy file {path}") from error
    return module


def _construct_and_validate(candidate: object, source: str) -> Strategy:
    try:
        instance = candidate() if inspect.isclass(candidate) else candidate
    except Exception as error:
        raise StrategyLoadError(f"failed to construct strategy from {source}") from error
    if not isinstance(instance, Strategy):
        raise StrategyLoadError(
            f"strategy from {source} does not satisfy the Strategy protocol"
        )
    metadata = getattr(instance, "metadata", None)
    if not isinstance(metadata, StrategyMetadata):
        raise StrategyLoadError(
            f"strategy from {source} has invalid StrategyMetadata"
        )
    if metadata.api_version != "1":
        raise StrategyLoadError(
            f"strategy {metadata.id} uses unsupported API version "
            f"{metadata.api_version}"
        )
    return instance
