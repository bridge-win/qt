"""Seeded block bootstrap validation utilities."""

from __future__ import annotations

import math
import random

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, field_validator


class BootstrapResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    paths: tuple[tuple[float, ...], ...] = Field(min_length=1)
    simulations: int = Field(gt=0)
    block_size: int = Field(gt=0)
    seed: int

    @field_validator("paths")
    @classmethod
    def require_finite_paths(
        cls,
        paths: tuple[tuple[float, ...], ...],
    ) -> tuple[tuple[float, ...], ...]:
        if any(not path for path in paths):
            raise ValueError("bootstrap paths must be non-empty")
        if any(
            not math.isfinite(value)
            for path in paths
            for value in path
        ):
            raise ValueError("bootstrap paths must be finite")
        return paths


class BlockBootstrap:
    @staticmethod
    def run(
        returns: pd.Series,
        *,
        simulations: int,
        block_size: int,
        seed: int,
    ) -> BootstrapResult:
        values = tuple(float(value) for value in returns.dropna().tolist())
        if not values:
            raise ValueError("returns must be non-empty")
        if any(not math.isfinite(value) for value in values):
            raise ValueError("returns must be finite")
        if simulations <= 0:
            raise ValueError("simulations must be positive")
        if block_size <= 0:
            raise ValueError("block_size must be positive")
        rng = random.Random(seed)
        paths = tuple(
            _sample_path(values, block_size=block_size, rng=rng)
            for _ in range(simulations)
        )
        return BootstrapResult(
            paths=paths,
            simulations=simulations,
            block_size=block_size,
            seed=seed,
        )


def _sample_path(
    values: tuple[float, ...],
    *,
    block_size: int,
    rng: random.Random,
) -> tuple[float, ...]:
    sampled: list[float] = []
    while len(sampled) < len(values):
        start = rng.randrange(len(values))
        for offset in range(block_size):
            sampled.append(values[(start + offset) % len(values)])
            if len(sampled) == len(values):
                break
    return tuple(sampled)
