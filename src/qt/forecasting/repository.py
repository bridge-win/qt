"""Central SQL forecast authority with privileged, append-only resolutions.

Source hashes identify evidence; verification is the responsibility of the
authorized source ingestor. Owner-provided outcomes are not silently promoted
to authoritative outcomes, and proper prediction scores are not trading P&L.
"""

from __future__ import annotations

import math
import random
import re
import time
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Column,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Table,
    UniqueConstraint,
    select,
)

from qt.agent_hub.research import ResolvedForecast, evaluate_forecasts
from qt.contracts import ForecastRecord, content_hash, decimal_string
from qt.persistence import Conflict, Repository
from qt.persistence.repository import insert_once
from qt.persistence.schema import metadata

forecast_records = Table(
    "forecast_records", metadata,
    Column("tenant_id", String(80), primary_key=True),
    Column("forecast_id", String(160), primary_key=True),
    Column("event_cluster_id", String(160), nullable=False),
    Column("decision_ns", BigInteger, nullable=False),
    Column("payload", JSON, nullable=False),
    Column("sha256", String(64), nullable=False),
    Column("created_ns", BigInteger, nullable=False),
    Index("ix_v4_forecast_cluster", "tenant_id", "event_cluster_id", "decision_ns"),
)
forecast_resolution_events = Table(
    "forecast_resolution_events", metadata,
    Column("tenant_id", String(80), primary_key=True),
    Column("resolution_id", String(160), primary_key=True),
    Column("forecast_id", String(160), nullable=False),
    Column("revision", Integer, nullable=False),
    Column("actor_id", String(160), nullable=False),
    Column("payload", JSON, nullable=False),
    Column("sha256", String(64), nullable=False),
    Column("created_ns", BigInteger, nullable=False),
    ForeignKeyConstraint(
        ["tenant_id", "forecast_id"],
        ["qt_v4.forecast_records.tenant_id", "qt_v4.forecast_records.forecast_id"],
    ),
    UniqueConstraint("tenant_id", "forecast_id", "revision", name="uq_v4_forecast_resolution_revision"),
    Index("ix_v4_forecast_resolution", "tenant_id", "forecast_id", "revision"),
)
_RESOLUTION_KEYS = {
    "resolution_id", "status", "outcome", "payout", "resolved_at_ns", "observed_at_ns",
    "source", "source_hash", "rules_hash", "verified", "redeemed_at_ns", "note",
}


def _identity(value: Any, *, maximum: int = 160) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError("invalid_forecast_identity")
    return value


def _hash(value: Any) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[a-f0-9]{64}", value) is None:
        raise ValueError("resolution_requires_sha256_evidence")
    return value


def _timestamp(value: Any, *, optional: bool = False) -> int | None:
    if value is None and optional:
        return None
    if type(value) is not int or value < 0 or value > time.time_ns():
        raise ValueError("resolution_timestamp_must_be_observed_integer_nanoseconds")
    return value


def _payout(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, (str, Decimal)):
        raise ValueError("payout_requires_decimal_string")
    if isinstance(value, str) and len(value) > 128:
        raise ValueError("payout_exceeds_exact_precision")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("invalid_payout_decimal") from exc
    if not parsed.is_finite() or not Decimal(0) <= parsed <= Decimal(1):
        raise ValueError("payout_must_be_between_zero_and_one")
    if abs(parsed.as_tuple().exponent) > 1000 or parsed != parsed.quantize(Decimal("1e-18")):
        raise ValueError("payout_exceeds_exact_precision")
    return decimal_string(parsed)


def _cohort(forecast: ForecastRecord) -> str:
    return content_hash([
        forecast.event_id, forecast.outcome_id,
        forecast.horizon_end_ns, forecast.prediction_mode,
    ])


class ForecastRepository:
    def __init__(self, repo: Repository) -> None:
        self.repo = repo

    @staticmethod
    def _verified_resolution(row) -> None:
        if content_hash([row["forecast_id"], row["actor_id"], row["payload"]]) != row["sha256"]:
            raise Conflict("resolution_integrity_failure")

    def record(self, tenant: str, forecast: ForecastRecord) -> dict:
        _identity(tenant, maximum=80)
        _identity(forecast.forecast_id)
        _identity(forecast.event_cluster_id)
        if forecast.decision_at_ns > time.time_ns():
            raise ValueError("forecast_decision_is_in_the_future")
        payload = forecast.model_dump(mode="json")
        with self.repo.transaction() as connection:
            insert_once(connection, forecast_records, {
                "tenant_id": tenant, "forecast_id": forecast.forecast_id,
                "event_cluster_id": forecast.event_cluster_id, "decision_ns": forecast.decision_at_ns,
                "payload": payload, "sha256": forecast.content_hash(), "created_ns": time.time_ns(),
            })
            row = connection.execute(select(forecast_records).where(
                forecast_records.c.tenant_id == tenant,
                forecast_records.c.forecast_id == forecast.forecast_id,
            )).mappings().one()
            if row["sha256"] != forecast.content_hash():
                raise Conflict("forecast_identity_conflict")
        return dict(row)

    def get(self, tenant: str, forecast_id: str) -> dict:
        with self.repo.transaction() as connection:
            row = self._get(connection, tenant, forecast_id)
        return row

    @staticmethod
    def _get(connection, tenant: str, forecast_id: str) -> dict:
        row = connection.execute(select(forecast_records).where(
            forecast_records.c.tenant_id == tenant, forecast_records.c.forecast_id == forecast_id,
        ).with_for_update()).mappings().first()
        if row is None:
            raise KeyError("forecast_not_found")
        if content_hash(row["payload"]) != row["sha256"]:
            raise Conflict("forecast_integrity_failure")
        return dict(row)

    @staticmethod
    def _resolution(forecast: ForecastRecord, request: dict, previous: dict | None) -> dict:
        if set(request) - _RESOLUTION_KEYS:
            raise ValueError("unknown_resolution_fields")
        if request.get("verified") is not True:
            raise ValueError("resolution_requires_verified_source_ingestor")
        if _hash(request.get("rules_hash")) != forecast.rules_hash:
            raise ValueError("resolution_rules_version_mismatch")
        status = request.get("status", "resolved")
        if status not in {"pending", "resolved", "canceled", "disputed"}:
            raise ValueError("invalid_resolution_status")
        outcome = request.get("outcome")
        if outcome is not None and (type(outcome) is not int or outcome not in (0, 1)):
            raise ValueError("outcome_must_be_binary_or_null")
        payout = _payout(request.get("payout"))
        if outcome is not None and payout is not None and Decimal(payout) != outcome:
            raise ValueError("special_payout_requires_null_binary_outcome")
        if status == "resolved" and outcome is None and payout is None:
            raise ValueError("resolved_outcome_or_payout_required")
        resolved = _timestamp(request.get("resolved_at_ns"), optional=status != "resolved")
        observed = _timestamp(request.get("observed_at_ns", previous["observed_at_ns"] if previous else time.time_ns()))
        if observed < forecast.decision_at_ns or (
            resolved is not None and not forecast.decision_at_ns < resolved <= observed
        ):
            raise ValueError("resolution_time_precedes_decision_or_observation")
        redeemed = _timestamp(request.get("redeemed_at_ns"), optional=True)
        if redeemed is not None and (resolved is None or not resolved <= redeemed <= observed):
            raise ValueError("redemption_must_follow_resolution")
        note = request.get("note")
        if note is not None and (not isinstance(note, str) or len(note) > 4096):
            raise ValueError("invalid_resolution_note")
        return {
            "status": status, "outcome": outcome, "payout": payout,
            "resolved_at_ns": resolved, "observed_at_ns": observed,
            "source": _identity(request.get("source"), maximum=2048),
            "source_hash": _hash(request.get("source_hash")), "rules_hash": forecast.rules_hash,
            "verified": True, "redeemed_at_ns": redeemed, "note": note,
        }

    def resolve(self, tenant: str, forecast_id: str, resolution: dict, *,
                actor_id: str, permissions) -> dict:
        if permissions is None or isinstance(permissions, str) or "forecast:resolve" not in permissions:
            raise PermissionError("forecast_source_resolution_permission_required")
        _identity(actor_id)
        with self.repo.transaction() as connection:
            row = self._get(connection, tenant, forecast_id)
            forecast = ForecastRecord.model_validate(row["payload"])
            identity = resolution.get("resolution_id")
            existing = None
            if identity is not None:
                _identity(identity)
                existing = connection.execute(select(forecast_resolution_events).where(
                    forecast_resolution_events.c.tenant_id == tenant,
                    forecast_resolution_events.c.resolution_id == identity,
                )).mappings().first()
            body = self._resolution(forecast, resolution, existing["payload"] if existing else None)
            identity = identity or content_hash([forecast_id, body])
            digest = content_hash([forecast_id, actor_id, body])
            if existing is not None:
                self._verified_resolution(existing)
                if existing["sha256"] != digest or existing["forecast_id"] != forecast_id:
                    raise Conflict("resolution_identity_conflict")
                return dict(existing)
            latest = connection.execute(select(forecast_resolution_events.c.revision).where(
                forecast_resolution_events.c.tenant_id == tenant,
                forecast_resolution_events.c.forecast_id == forecast_id,
            ).order_by(forecast_resolution_events.c.revision.desc()).limit(1)).scalar_one_or_none()
            result = {
                "tenant_id": tenant, "forecast_id": forecast_id, "resolution_id": identity,
                "revision": (latest or 0) + 1, "actor_id": actor_id, "payload": body,
                "sha256": digest, "created_ns": time.time_ns(),
            }
            # The forecast row lock serializes revisions and duplicate IDs.
            if connection.execute(select(forecast_resolution_events.c.resolution_id).where(
                forecast_resolution_events.c.tenant_id == tenant,
                forecast_resolution_events.c.resolution_id == identity,
            )).first():
                old = connection.execute(select(forecast_resolution_events).where(
                    forecast_resolution_events.c.tenant_id == tenant,
                    forecast_resolution_events.c.resolution_id == identity,
                )).mappings().one()
                self._verified_resolution(old)
                if old["sha256"] != digest:
                    raise Conflict("resolution_identity_conflict")
                return dict(old)
            insert_once(connection, forecast_resolution_events, result)
            actual = connection.execute(select(forecast_resolution_events).where(
                forecast_resolution_events.c.tenant_id == tenant,
                forecast_resolution_events.c.resolution_id == identity,
            )).mappings().one()
            if actual["sha256"] != digest:
                raise Conflict("resolution_identity_conflict")
            self.repo.event(connection, tenant, forecast_id, "forecast_resolution_appended", {
                "resolution_id": identity, "revision": result["revision"],
                "actor_id": actor_id, "source_hash": body["source_hash"],
            })
        return dict(actual)

    def history(self, tenant: str, forecast_id: str) -> list[dict]:
        with self.repo.transaction() as connection:
            self._get(connection, tenant, forecast_id)
            rows = connection.execute(select(forecast_resolution_events).where(
                forecast_resolution_events.c.tenant_id == tenant,
                forecast_resolution_events.c.forecast_id == forecast_id,
            ).order_by(forecast_resolution_events.c.revision)).mappings().all()
        for row in rows:
            self._verified_resolution(row)
        return [dict(row) for row in rows]

    def score(self, tenant: str) -> dict:
        with self.repo.transaction() as connection:
            records = connection.execute(select(forecast_records).where(
                forecast_records.c.tenant_id == tenant,
            ).order_by(forecast_records.c.decision_ns, forecast_records.c.forecast_id)).mappings().all()
            resolutions = connection.execute(select(forecast_resolution_events).where(
                forecast_resolution_events.c.tenant_id == tenant,
            ).order_by(forecast_resolution_events.c.revision)).mappings().all()
        for row in resolutions:
            self._verified_resolution(row)
        latest = {row["forecast_id"]: row for row in resolutions}
        counts = {"total_forecasts": len(records), "unresolved_forecasts": 0, "canceled_forecasts": 0,
                  "disputed_forecasts": 0, "non_binary_resolutions": 0,
                  "duplicate_cohort_forecasts": 0, "invalid_observations": 0}
        cohorts, cluster_ids = {}, {}
        for row in records:
            if content_hash(row["payload"]) != row["sha256"]:
                raise Conflict("forecast_integrity_failure")
            forecast = ForecastRecord.model_validate(row["payload"])
            resolution = latest.get(forecast.forecast_id)
            body = resolution["payload"] if resolution else None
            if body is None or body["status"] == "pending":
                counts["unresolved_forecasts"] += 1
            elif body["status"] == "canceled":
                counts["canceled_forecasts"] += 1
            elif body["status"] == "disputed":
                counts["disputed_forecasts"] += 1
            elif body["outcome"] not in (0, 1) or (
                body["payout"] is not None and Decimal(body["payout"]) != body["outcome"]
            ):
                counts["non_binary_resolutions"] += 1
            key = _cohort(forecast)
            if key in cohorts:
                counts["duplicate_cohort_forecasts"] += 1
                continue
            cohorts[key] = (forecast, body)
            cluster_ids[key] = forecast.event_cluster_id
        rows, clusters = [], []
        for key, (forecast, body) in cohorts.items():
            if body is None or body["status"] != "resolved" or body["outcome"] not in (0, 1):
                continue
            if body["payout"] is not None and Decimal(body["payout"]) != body["outcome"]:
                continue
            if not body["verified"] or body["rules_hash"] != forecast.rules_hash:
                counts["invalid_observations"] += 1
                continue
            try:
                # Integer-nanosecond chronology was validated on ingestion.
                # Float seconds can collapse distinct nanoseconds into a tie;
                # do not rerun the legacy seconds-based chronology validator.
                rows.append(ResolvedForecast.model_construct(
                    market_id=key, prediction=float(forecast.probability),
                    market_probability=float(forecast.market_mid) if forecast.market_mid is not None else None,
                    outcome=body["outcome"], predicted_at=forecast.decision_at_ns / 1e9,
                    resolved_at=body["resolved_at_ns"] / 1e9,
                ))
                clusters.append(cluster_ids[key])
            except ValueError:
                counts["invalid_observations"] += 1
        report = _score_rows(rows)
        report.update(
            schema_version="forecast-scorecard.v2", counts=counts,
            scored_observations=len(rows), independent_event_clusters=len(set(clusters)),
            effective_sample_size=None, selected_cohorts=len(cohorts),
            selection_policy="first_forecast_per_event_outcome_horizon_mode",
            evidence_sha256=content_hash({
                "forecasts": [row["sha256"] for row in records],
                "resolutions": [row["sha256"] for row in resolutions],
            }),
            live_ready=False,
        )
        report.update(_cluster_comparison(rows, clusters))
        report["limitations"].extend([
            "Independent event clusters are source-provided grouping labels; common shocks may remain correlated",
            "Earliest cohort forecast is selected before resolution and market-baseline availability",
            "Permissioned source ingestor must actually verify the evidence identified by source/rules hashes",
            "Unresolved, canceled, disputed and nonbinary payouts are excluded from binary scoring",
        ])
        return report


def _score_rows(rows: list[ResolvedForecast]) -> dict:
    if not rows:
        return {"status": "empty", "samples": 0, "brier_score": None, "log_loss": None,
                "market_baseline_brier": None, "paired_model_brier": None, "brier_skill": None,
                "paired_comparison_samples": 0, "market_baseline_coverage": 0.0,
                "comparison_status": "missing_market_baseline", "calibration_bins": [], "limitations": []}
    if len(rows) <= 10_000:
        return evaluate_forecasts(rows)
    # Avoid the legacy evaluator's request-size bound without silently truncating
    # a durable production history. These are descriptive proper scores only.
    paired = [row for row in rows if row.market_probability is not None]
    model = sum((row.prediction - row.outcome) ** 2 for row in paired) / len(paired) if paired else None
    market = sum((row.market_probability - row.outcome) ** 2 for row in paired) / len(paired) if paired else None
    return {"status": "evaluated", "samples": len(rows),
            "brier_score": sum((row.prediction - row.outcome) ** 2 for row in rows) / len(rows),
            "log_loss": sum(-math.log(max(1e-12, row.prediction if row.outcome else 1 - row.prediction)) for row in rows) / len(rows),
            "market_baseline_brier": market, "paired_model_brier": model,
            "brier_skill": 1 - model / market if market else None,
            "paired_comparison_samples": len(paired), "market_baseline_coverage": len(paired) / len(rows),
            "comparison_status": "comparable" if paired else "missing_market_baseline",
            "calibration_bins": [], "data_sha256": content_hash([row.model_dump() for row in rows]),
            "limitations": ["Large-history proper scores use all rows; legacy fixed calibration bins omitted"]}


def _cluster_comparison(rows: list[ResolvedForecast], clusters: list[str]) -> dict:
    grouped: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for row, cluster in zip(rows, clusters, strict=True):
        if row.market_probability is not None:
            grouped[cluster].append(((row.prediction - row.outcome) ** 2,
                                    (row.market_probability - row.outcome) ** 2))
    values = [(sum(model for model, _ in group) / len(group),
               sum(market for _, market in group) / len(group)) for _, group in sorted(grouped.items())]
    comparison = {
        "paired_event_clusters": len(values),
        "paired_event_cluster_coverage": len(values) / len(set(clusters)) if clusters else 0.0,
        "cluster_brier_skill": None, "cluster_brier_skill_95pct": None,
        "cluster_inference_status": "insufficient_event_clusters",
    }
    if not values:
        return comparison
    model = sum(value[0] for value in values) / len(values)
    market = sum(value[1] for value in values) / len(values)
    comparison["cluster_brier_skill"] = 1 - model / market if market else None
    if len(values) < 30 or market == 0:
        return comparison
    rng = random.Random(0)
    samples = []
    for _ in range(10_000):
        selected = rng.choices(values, k=len(values))
        baseline = sum(item[1] for item in selected)
        if baseline:
            samples.append(1 - sum(item[0] for item in selected) / baseline)
    if len(samples) == 10_000:
        samples.sort()
        comparison["cluster_brier_skill_95pct"] = [samples[249], samples[9749]]
        comparison["cluster_inference_status"] = "event_cluster_bootstrap_assuming_cluster_independence"
    return comparison
