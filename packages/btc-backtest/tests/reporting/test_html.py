from __future__ import annotations

from importlib import resources
from pathlib import Path

from btc_backtest.reporting.artifacts import ArtifactWriter
from btc_backtest.reporting.html import render_html

from .test_artifacts import complete_result, metrics, validation_result


def test_html_report_is_standalone_and_escapes_user_content(tmp_path: Path) -> None:
    result = complete_result(strategy_id="<script>alert(1)</script>")
    bundle = ArtifactWriter().write(
        result,
        metrics(),
        validation_result(),
        tmp_path,
    )

    html = render_html(bundle)

    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "https://" not in html
    assert "http://" not in html
    assert "signal-1" in html


def test_report_template_is_packaged() -> None:
    template = resources.files("btc_backtest.reporting.templates").joinpath(
        "report.html.j2"
    )

    assert template.is_file()
