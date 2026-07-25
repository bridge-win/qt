"""Standalone HTML rendering for immutable artifact bundles."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pandas as pd
from jinja2 import Environment, PackageLoader, StrictUndefined

if TYPE_CHECKING:
    from collections.abc import Mapping

    from btc_backtest.reporting.artifacts import ArtifactBundle


def render_html(bundle: ArtifactBundle) -> str:
    """Render a self-contained HTML report for a completed artifact bundle."""

    template = _environment().get_template("report.html.j2")
    rendered = template.render(
        manifest=bundle.manifest.model_dump(mode="json"),
        metrics=_read_json_object(bundle.run_dir / "metrics.json"),
        validation=_read_json_object(bundle.run_dir / "validation.json"),
        signals=_read_signal_ids(bundle.run_dir / "signals.parquet"),
    )
    return rendered


def _environment() -> Environment:
    return Environment(
        loader=PackageLoader("btc_backtest.reporting", "templates"),
        autoescape=True,
        undefined=StrictUndefined,
    )


def _read_json_object(path: Path) -> Mapping[str, object]:
    if not path.exists():
        return {}
    decoded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(decoded, dict):
        return {}
    return cast("Mapping[str, object]", decoded)


def _read_signal_ids(path: Path) -> tuple[str, ...]:
    if not path.exists():
        return ()
    frame = pd.read_parquet(path)
    if "signal_id" not in frame.columns:
        return ()
    values = frame["signal_id"].dropna().tolist()
    return tuple(str(value) for value in values)
