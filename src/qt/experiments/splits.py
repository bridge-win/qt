"""Chronological splits and genuine combinatorial purged cross validation.

CPCV deliberately trains on both sides of a test group. It is a robustness
analysis on the development set, not a chronological live-trading simulation.
Label information intervals are closed: touching an endpoint is overlap.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from itertools import combinations, pairwise
from math import comb


@dataclass(frozen=True)
class LabelInterval:
    start: int
    end: int

    def __post_init__(self) -> None:
        if self.start < 0 or self.end < self.start:
            raise ValueError("invalid_label_interval")


@dataclass(frozen=True)
class Split:
    split_id: int
    train_indices: tuple[int, ...]
    test_indices: tuple[int, ...]
    test_groups: tuple[int, ...] = ()


@dataclass(frozen=True)
class PathSegment:
    group: int
    split_id: int
    test_indices: tuple[int, ...]


@dataclass(frozen=True)
class CPCVPlan:
    groups: int
    test_groups: int
    splits: tuple[Split, ...]
    paths: tuple[tuple[PathSegment, ...], ...]
    independent_paths: bool = False


def _validate_intervals(intervals: Sequence[LabelInterval]) -> None:
    if not intervals:
        raise ValueError("empty_intervals")
    if any(a.start > b.start for a, b in pairwise(intervals)):
        raise ValueError("intervals_not_chronological")


def purge_training(
    intervals: Sequence[LabelInterval],
    train_indices: Sequence[int],
    test_indices: Sequence[int],
    *,
    embargo: int = 0,
) -> tuple[int, ...]:
    """Remove training labels overlapping test information or post-test embargo.

    ``embargo`` uses the same clock units as the intervals, not a bar count or
    feature warmup. Test label ends, including their horizon, anchor the embargo.
    """
    _validate_intervals(intervals)
    if embargo < 0 or not test_indices:
        raise ValueError("invalid_embargo_or_empty_test")
    indices = (*train_indices, *test_indices)
    if any(i < 0 or i >= len(intervals) for i in indices):
        raise ValueError("index_out_of_range")
    if len(set(train_indices)) != len(train_indices) or len(set(test_indices)) != len(test_indices):
        raise ValueError("duplicate_indices")
    tests = [intervals[i] for i in test_indices]
    return tuple(
        i
        for i in train_indices
        if i not in test_indices
        and all(
            intervals[i].end < test.start or intervals[i].start > test.end + embargo
            for test in tests
        )
    )


def walk_forward_splits(
    intervals: Sequence[LabelInterval],
    *,
    train_size: int,
    test_size: int,
    step: int | None = None,
    expanding: bool = False,
    embargo: int = 0,
) -> tuple[Split, ...]:
    _validate_intervals(intervals)
    stride = test_size if step is None else step
    if train_size < 1 or test_size < 1 or stride < test_size:
        raise ValueError("invalid_walk_forward_sizes_or_overlapping_tests")
    splits = []
    for start in range(train_size, len(intervals) - test_size + 1, stride):
        tests = tuple(range(start, start + test_size))
        train = range(0 if expanding else start - train_size, start)
        # Training labels must be fully observed before the first decision.
        causal = tuple(i for i in train if intervals[i].end < intervals[start].start)
        purged = purge_training(intervals, causal, tests, embargo=embargo)
        if not purged:
            raise ValueError("empty_training_after_purge")
        splits.append(Split(len(splits), purged, tests))
    return tuple(splits)


def cpcv_splits(
    intervals: Sequence[LabelInterval], *, groups: int, test_groups: int, embargo: int = 0
) -> CPCVPlan:
    _validate_intervals(intervals)
    if not 1 <= test_groups < groups <= len(intervals):
        raise ValueError("invalid_cpcv_groups")
    # Balanced contiguous groups; all observations are used exactly once per path.
    base, extra = divmod(len(intervals), groups)
    blocks = []
    offset = 0
    for group in range(groups):
        size = base + (group < extra)
        blocks.append(tuple(range(offset, offset + size)))
        offset += size
    splits = []
    for selected in combinations(range(groups), test_groups):
        tests = tuple(i for group in selected for i in blocks[group])
        train = tuple(i for group in range(groups) if group not in selected for i in blocks[group])
        purged = purge_training(intervals, train, tests, embargo=embargo)
        if not purged:
            raise ValueError("empty_training_after_purge")
        splits.append(Split(len(splits), purged, tests, selected))
    path_count = comb(groups - 1, test_groups - 1)
    paths: list[list[PathSegment]] = [[] for _ in range(path_count)]
    for group in range(groups):
        appearances = [split for split in splits if group in split.test_groups]
        for path_index, split in enumerate(appearances):
            paths[path_index].append(PathSegment(group, split.split_id, blocks[group]))
    return CPCVPlan(groups, test_groups, tuple(splits), tuple(tuple(path) for path in paths))
