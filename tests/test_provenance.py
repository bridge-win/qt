"""Every numeric constant must carry provenance; cycle bands must be data-derived."""

from __future__ import annotations

import numpy as np
import pandas as pd

from qt.core import provenance as prov
from qt.indicators.cycle import calibrate_bands, cycle_position, locate_cycle_extrema


def test_every_threshold_field_has_provenance():
    assert prov.audit_threshold_config() == []


def test_registry_sources_resolve_and_methods_valid():
    for p in prov.REGISTRY.values():
        assert p.method in ("observed", "computed", "literature", "fitted", "assumed")
        for s in p.sources:
            assert s in prov.SOURCES
            assert prov.SOURCES[s].url.startswith("http")
        if p.method == "fitted":
            assert p.fit, f"{p.key}: fitted params must state the estimator"


def test_annotate_carries_source_and_caveat():
    a = prov.annotate(7.0, "mvrv_z.top_literature")
    assert a["method"] == "literature"
    assert a["source_urls"] and "caveat" in a


def _synthetic(years: float = 13.7, decay: float = 0.1):
    idx = pd.date_range("2013-01-01", periods=int(years * 365.25), freq="D", tz="UTC")
    t = np.arange(len(idx)) / 365.25
    close = pd.Series(200 * np.exp(0.55 * t + 1.1 * np.sin(2 * np.pi * (t - 0.3) / 4)), index=idx)
    mvrv_z = pd.Series((6 * np.sin(2 * np.pi * (t - 0.3) / 4) + 2) * np.exp(-decay * t), index=idx)
    return close, mvrv_z


def test_locate_cycle_extrema_uses_halving_clock():
    close, _ = _synthetic()
    ext = locate_cycle_extrema(close)
    tops = [e for e in ext if e.kind == "top"]
    assert len(tops) >= 3
    for e in tops:
        assert 0 < e.days_after_halving < 730
    # tops precede bottoms within a cycle
    by_cycle = {}
    for e in ext:
        by_cycle.setdefault(e.cycle, {})[e.kind] = e.date
    for d in by_cycle.values():
        if "top" in d and "bottom" in d:
            assert d["bottom"] > d["top"]


def test_calibrate_bands_is_fitted_from_data_and_labelled():
    close, mvrv_z = _synthetic()
    bands = calibrate_bands(close, {"mvrv_z": mvrv_z})
    b = bands["mvrv_z"]
    assert b.hi_method.startswith("fitted")
    assert b.fit["estimator"].startswith("OLS")
    assert b.fit["n"] >= 3
    assert b.lo_method.startswith("observed")
    # decaying peaks -> projected top below the first observed peak
    assert b.hi < max(b.observed_tops.values())
    assert b.hi > b.lo


def test_calibrate_bands_falls_back_to_literature_on_short_history():
    close, mvrv_z = _synthetic(years=3.0)
    bands = calibrate_bands(close, {"mvrv_z": mvrv_z})
    # one observed top -> mean of observed, explicitly labelled as not fitted
    assert "observed mean of 1" in bands["mvrv_z"].hi_method
    close, mvrv_z = _synthetic(years=1.2)
    bands = calibrate_bands(close, {"mvrv_z": mvrv_z})
    assert "literature" in bands["mvrv_z"].hi_method
    assert bands["mvrv_z"].hi == 7.0


def test_cycle_position_outputs_provenance_for_every_number():
    close, mvrv_z = _synthetic()
    cp = cycle_position(close, mvrv_z=mvrv_z)
    pv = cp.provenance
    assert pv["price"]["source"].startswith("Bitstamp")
    assert set(pv["bands"]) >= {"mvrv_z", "mayer"}
    for b in pv["bands"].values():
        assert b["hi_method"] and b["lo_method"] and b["sources"]
    assert pv["weights"]["method"] == "assumed"
    assert pv["halving_top_phase"]["method"] == "fitted"
    assert pv["cycle_extrema"] and all("method" in e for e in pv["cycle_extrema"])
