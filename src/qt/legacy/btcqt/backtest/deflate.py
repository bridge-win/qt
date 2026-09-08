# Migrated verbatim from btcqt; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
"""Multiple-testing honesty: experiment registry + Deflated Sharpe Ratio + PBO.

Every backtest run is appended to state/experiments.jsonl. The more
configurations you try, the higher the best Sharpe you'd find by pure luck —
DSR (Bailey & López de Prado) deflates the observed Sharpe against that
luck benchmark; PBO here is a CSCV-style approximation over the registry:
how often the in-sample-best configuration falls below median out-of-sample.

Honest caveats, in writing:
- DSR needs the number of INDEPENDENT trials; correlated configs overstate N,
  which makes DSR *conservative* (fine — conservative is the safe direction).
- The PBO implemented here is an approximation over registry entries sharing
  the same data span with walk-forward windows, not full CSCV over all
  submatrices. Treat borderline values as failures.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path


def config_fingerprint(cfg_dict: dict) -> str:
    return hashlib.sha256(json.dumps(cfg_dict, sort_keys=True, default=str)
                          .encode()).hexdigest()[:16]


def record_experiment(state_dir: Path, entry: dict) -> int:
    """Append one run to the registry; returns total registered trials."""
    f = state_dir / "experiments.jsonl"
    f.parent.mkdir(parents=True, exist_ok=True)
    with f.open("a") as fh:
        fh.write(json.dumps(entry, default=str) + "\n")
    return sum(1 for _ in f.open())


def load_experiments(state_dir: Path) -> list[dict]:
    f = state_dir / "experiments.jsonl"
    if not f.exists():
        return []
    out = []
    for line in f.open():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


# ---------------------------------------------------------------- normal CDF

def _phi(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def expected_max_sharpe(n_trials: int, sharpe_var: float) -> float:
    """E[max SR] among n_trials zero-skill strategies (Bailey/LdP approximation)."""
    if n_trials <= 1 or sharpe_var <= 0:
        return 0.0
    emc = 0.5772156649015329
    n = float(n_trials)
    z = (1 - emc) * _inv_phi(1 - 1 / n) + emc * _inv_phi(1 - 1 / (n * math.e))
    return math.sqrt(sharpe_var) * z


def _inv_phi(p: float) -> float:
    """Inverse normal CDF (Acklam approximation, adequate here)."""
    if not 0 < p < 1:
        return 0.0
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
               ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if p > phigh:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
               ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    q = p - 0.5
    r = q * q
    return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / \
           (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)


def deflated_sharpe(observed_sr_ann: float, n_obs_days: int, n_trials: int,
                    skew: float = 0.0, kurt: float = 3.0,
                    trial_sharpes: list[float] | None = None) -> dict:
    """Probability that the observed Sharpe beats the best-of-N luck benchmark.
    observed_sr_ann is annualized from daily returns; converted to daily internally."""
    if n_obs_days < 30:
        return {"dsr": None, "note": "too few observations"}
    sr = observed_sr_ann / math.sqrt(365)                       # daily SR
    if trial_sharpes and len(trial_sharpes) > 1:
        daily = [s / math.sqrt(365) for s in trial_sharpes]
        mu = sum(daily) / len(daily)
        var = sum((x - mu) ** 2 for x in daily) / (len(daily) - 1)
    else:
        var = 0.01 / 365                                        # conservative default
    sr0 = expected_max_sharpe(max(n_trials, 1), var)
    denom = math.sqrt(max(1e-12,
                          (1 - skew * sr + (kurt - 1) / 4.0 * sr * sr) / (n_obs_days - 1)))
    z = (sr - sr0) / denom
    return {"dsr": round(_phi(z), 4), "sr0_daily": round(sr0, 5),
            "n_trials": n_trials, "z": round(z, 3)}


def pbo_estimate(experiments: list[dict], span_key: str = "span") -> dict:
    """CSCV-style approximation: among registry entries with walk-forward results
    on the same data span, how often does the config with the best average
    in-sample window rank below median in the paired out-of-sample windows?"""
    groups: dict[str, list[dict]] = {}
    for e in experiments:
        wf = e.get("walkforward_sharpes")
        if wf and len(wf) >= 4:
            groups.setdefault(str(e.get(span_key)), []).append(e)
    for span, entries in groups.items():
        if len(entries) < 8:
            continue
        below = 0
        trials = 0
        n_win = min(len(e["walkforward_sharpes"]) for e in entries)
        half = n_win // 2
        if half < 2:
            continue
        import itertools
        for is_idx in itertools.combinations(range(n_win), half):
            oos_idx = [i for i in range(n_win) if i not in is_idx]
            scores_is = [(sum(e["walkforward_sharpes"][i] or 0 for i in is_idx), k)
                         for k, e in enumerate(entries)]
            best_k = max(scores_is)[1]
            oos = sorted(sum(e["walkforward_sharpes"][i] or 0 for i in oos_idx)
                         for e in entries)
            best_oos = sum(entries[best_k]["walkforward_sharpes"][i] or 0 for i in oos_idx)
            median = oos[len(oos) // 2]
            trials += 1
            if best_oos < median:
                below += 1
            if trials >= 200:                                   # cap combinatorics
                break
        if trials:
            return {"pbo": round(below / trials, 3), "n_configs": len(entries),
                    "n_splits": trials, "span": span}
    return {"pbo": None, "note": "need >=8 registered configs with walk-forward "
                                 "results on the same data span"}


