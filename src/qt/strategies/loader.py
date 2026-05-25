"""Load per-strategy YAML configs from a directory.

Each file ``<dir>/<name>.yaml`` is loaded into a ``StrategyConfig``. The
filename stem becomes the strategy name and must match a key in
``qt.strategies.registry.REGISTRY``.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from qt.strategies.base import Strategy, StrategyConfig
from qt.strategies.registry import strategy_class


def load_strategy_configs(directory: str | Path) -> list[StrategyConfig]:
    """Read every ``*.yaml`` under ``directory`` into a StrategyConfig."""

    d = Path(directory)
    if not d.exists():
        return []
    out: list[StrategyConfig] = []
    for path in sorted(d.glob("*.yaml")):
        with path.open(encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
        raw.setdefault("name", path.stem)
        cfg = StrategyConfig.model_validate(raw)
        if cfg.name != path.stem:
            raise ValueError(
                f"strategy file {path} declares name {cfg.name!r} "
                f"but expected {path.stem!r}"
            )
        out.append(cfg)
    return out


def build_strategies(configs: list[StrategyConfig]) -> list[Strategy]:
    """Instantiate Strategy objects for the given configs (enabled only)."""

    out: list[Strategy] = []
    for cfg in configs:
        if not cfg.enabled:
            continue
        cls = strategy_class(cfg.name)
        out.append(cls(cfg))
    return out


__all__ = ["build_strategies", "load_strategy_configs"]
