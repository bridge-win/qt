"""Fail-closed stage gates. Missing evidence never becomes a passing zero."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from math import isfinite
from typing import Any

from qt.persistence.repository import checksum


@dataclass(frozen=True)
class Rule:
    name: str
    comparison: str
    threshold: float
    sample_profile: str
    failure_action: str
    method: str

    def evaluate(self, evidence: dict) -> dict:
        value = evidence.get(self.name)
        if type(value) not in (int, float, bool) or not isfinite(float(value)):
            return {**asdict(self), "status": "insufficient", "observed": None,
                    "reason": "missing_or_nonfinite_evidence"}
        probability = self.name in {"holm_p", "ece", "availability", "data_coverage", "fee_coverage"}
        flag = self.name.endswith("_verified") or self.name in {"reproducible", "stress_passed",
                                                               "power_sufficient", "capital_configured"}
        signed = self.name in {"net_expectancy_lcb", "benchmark_excess_lcb", "brier_skill_lcb",
                               "economic_profit_lcb", "sharpe", "sortino", "calmar"}
        if ((probability and not 0 <= value <= 1) or (flag and value not in (0, 1))
                or (not signed and value < 0)):
            return {**asdict(self), "status": "fail", "observed": value,
                    "reason": "evidence_outside_metric_domain"}
        good = value >= self.threshold if self.comparison == ">=" else value <= self.threshold
        return {**asdict(self), "status": "pass" if good else "fail", "observed": value,
                "reason": "threshold_satisfied" if good else "threshold_not_satisfied"}


INTEGRITY = (
    Rule("causality_verified", ">=", 1, "R/E", "Q", "future mutation and prefix checks"),
    Rule("accounting_verified", ">=", 1, "E", "H", "per-asset ledger balance"),
    Rule("fee_coverage", ">=", 1, "R/E", "B", "verified applicable costs / all applicable costs"),
    Rule("dataset_verified", ">=", 1, "D", "Q", "manifest and actual content hash"),
    Rule("reproducible", ">=", 1, "E", "Q", "same spec/image/seed output hashes"),
)
STRATEGY = (
    Rule("oos_days", ">=", 365, "R", "B", "calendar daily NAV observations"),
    Rule("independent_events", ">=", 100, "R", "B", "pre-registered risk event clusters"),
    Rule("sharpe", ">=", 1, "R", "B", "365-day annualized net return Sharpe"),
    Rule("sortino", ">=", 1, "R", "B", "target-zero downside deviation"),
    Rule("calmar", ">=", .5, "R", "B", "calendar CAGR / maximum drawdown"),
    Rule("profit_factor", ">=", 1.1, "R", "B", "net winning PnL / absolute losing PnL"),
    Rule("net_expectancy_lcb", ">=", 1e-12, "R", "B", "cluster bootstrap 95% lower bound"),
    Rule("benchmark_excess_lcb", ">=", 1e-12, "R", "B", "paired benchmark utility lower bound"),
    Rule("holm_p", "<=", .05, "R", "B", "full registered hypothesis-family correction"),
    Rule("search_accounting_verified", ">=", 1, "R", "B", "completed population audit"),
    Rule("stress_passed", ">=", 1, "R/E", "B", "2x costs and liquidity/latency scenarios"),
    Rule("power_sufficient", ">=", 1, "R", "B", "pre-registered effect size power analysis"),
)
FORWARD = (
    Rule("paper_days", ">=", 30, "E", "B", "elapsed frozen-version paper observations"),
    Rule("paper_intents", ">=", 200, "E", "B", "logical order intents"),
    Rule("shadow_days", ">=", 14, "E", "B", "elapsed frozen-version shadow observations"),
    Rule("shadow_intents", ">=", 100, "E", "B", "fully risk-checked unsent intents"),
    Rule("duplicate_effects", "<=", 0, "E", "H", "duplicate submissions or ledger applications"),
    Rule("unreconciled_orders", "<=", 0, "E/L", "H", "UNKNOWN or unexplained order states"),
    Rule("account_difference", "<=", 0, "E/L", "H", "unexplained difference in native asset units"),
    Rule("data_coverage", ">=", .999, "D", "H", "valid active intervals / expected intervals"),
    Rule("availability", ">=", .999, "O", "H", "eligible trading minutes / planned minutes"),
    Rule("restore_drills", ">=", 3, "O", "B", "successful fresh-environment reconciled restores"),
    Rule("recovery_verified", ">=", 1, "E/O", "B", "all critical crash injection cases"),
    Rule("account_eligibility_verified", ">=", 1, "L", "B", "venue and account qualification"),
    Rule("live_adapter_verified", ">=", 1, "E", "B", "exact deployed funded lifecycle adapter"),
    Rule("capital_configured", ">=", 1, "L", "B", "isolated account capital and risk policy"),
)


class GatePolicy:
    version = "qt-production-v1"

    def definition(self) -> dict:
        return {"version": self.version, "rules": [asdict(rule) for rule in (*INTEGRITY, *STRATEGY, *FORWARD)],
                "defaults": {"paper_capital": "10000", "max_order_fraction": "0.02",
                             "max_position_fraction": "0.30", "max_daily_loss_fraction": "0.005",
                             "max_drawdown_fraction": "0.03", "max_leverage": "1"}}

    def fingerprint(self) -> str:
        return checksum(self.definition())

    def evaluate(self, stage: str, evidence: dict[str, Any], *, low_frequency: bool = False,
                 prediction_market: bool = False) -> dict:
        if stage not in {"research", "paper", "shadow", "live_canary", "scale"}:
            raise ValueError("unknown_promotion_stage")
        rules = list(INTEGRITY)
        if stage in {"shadow", "live_canary", "scale"}:
            rules.extend(STRATEGY)
        if stage in {"live_canary", "scale"}:
            rules.extend(FORWARD)
            if low_frequency:
                rules = [rule for rule in rules if rule.name not in {"paper_days", "paper_intents"}]
                rules.extend((
                    Rule("paper_days", ">=", 90, "E", "B", "low-frequency elapsed paper days"),
                    Rule("paper_independent_events", ">=", 30, "E", "B", "independent event episodes"),
                ))
            if prediction_market:
                rules.extend((
                    Rule("settled_event_clusters", ">=", 300, "F", "B", "distinct settled event clusters"),
                    Rule("brier_skill_lcb", ">=", 1e-12, "F", "B", "paired event-cluster bootstrap"),
                    Rule("ece", "<=", .05, "F", "B", "adaptive bins with >=30 observations"),
                    Rule("rules_verified", ">=", 1, "F", "B", "exact settlement and fee versions"),
                ))
        if stage == "scale":
            rules.extend((
                Rule("live_days", ">=", 90, "L", "B", "frozen deployment actual funded days"),
                Rule("live_lifecycles", ">=", 100, "L", "B", "actual completed trade lifecycles"),
                Rule("economic_profit_lcb", ">=", 1e-12, "L", "B", "net profit after platform costs"),
            ))
        checks = [rule.evaluate(evidence) for rule in rules]
        return {"policy_version": self.version, "policy_hash": self.fingerprint(), "stage": stage,
                "eligible": all(check["status"] == "pass" for check in checks), "checks": checks,
                "limits": "Thresholds are admission rules, not profit or loss guarantees."}
