# Migrated with Python 3.10 UTC compatibility from btc_quant_evolution; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Literal, Protocol

CredentialStatus = Literal["enabled", "missing_credentials", "rate_limited", "unavailable", "failed"]
SourcePolicy = Literal["auto", "free-only", "paid-required"]
ProviderTier = Literal["public", "paid"]
FeatureDomain = Literal["liquidity", "derivatives", "onchain", "sentiment"]

_DATASET_DOMAINS: dict[str, FeatureDomain] = {
    "ohlcv": "liquidity",
    "funding_rate": "derivatives",
    "open_interest": "derivatives",
    "long_short_ratio": "derivatives",
    "taker_buy_sell": "derivatives",
    "fear_greed": "sentiment",
    "macro_news": "sentiment",
    "onchain": "onchain",
}


@dataclass(frozen=True)
class ProviderCapability:
    provider: str
    dataset: str
    free: bool
    required_credentials: tuple[str, ...]
    min_timeframe: str
    domain: FeatureDomain | None = None
    tier: ProviderTier | None = None
    implemented: bool = True
    required_fields: tuple[str, ...] = ()
    max_missing_fraction: float = 0.0
    max_consecutive_missing_intervals: int = 0

    def __post_init__(self) -> None:
        domain = self.domain or _DATASET_DOMAINS.get(self.dataset)
        if domain is None:
            raise ValueError(f"domain is required for dataset: {self.dataset}")
        tier = self.tier or ("public" if self.free else "paid")
        if (tier == "public") != self.free:
            raise ValueError("free must match provider tier")
        if len(set(self.required_fields)) != len(self.required_fields) or any(
            not field for field in self.required_fields
        ):
            raise ValueError("required_fields must contain unique non-empty names")
        if not 0 <= self.max_missing_fraction <= 1:
            raise ValueError("max_missing_fraction must be between 0 and 1")
        if self.max_consecutive_missing_intervals < 0:
            raise ValueError("max_consecutive_missing_intervals must be non-negative")
        object.__setattr__(self, "domain", domain)
        object.__setattr__(self, "tier", tier)


class ProviderHttpError(RuntimeError):
    def __init__(
        self,
        provider: str,
        status_code: int,
        *,
        retry_after_seconds: float | None = None,
    ) -> None:
        self.provider = provider
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds
        retry_text = (
            f"; Retry-After={retry_after_seconds:g} seconds"
            if retry_after_seconds is not None
            else ""
        )
        super().__init__(f"{provider} HTTP request failed with status {status_code}{retry_text}")


class ProviderRateLimitError(ProviderHttpError):
    def __init__(self, provider: str, *, retry_after_seconds: float | None = None) -> None:
        super().__init__(
            provider,
            429,
            retry_after_seconds=retry_after_seconds,
        )


@dataclass(frozen=True)
class ProviderSyncRequest:
    symbol: str
    timeframe: str
    start: date
    end: date
    source_policy: SourcePolicy = "auto"

    def __post_init__(self) -> None:
        if not self.symbol:
            raise ValueError("symbol is required")
        if not self.timeframe:
            raise ValueError("timeframe is required")
        if self.start >= self.end:
            raise ValueError("start must be before end")
        if self.source_policy not in ("auto", "free-only", "paid-required"):
            raise ValueError(f"unsupported source policy: {self.source_policy}")

    def start_as_datetime(self) -> datetime:
        return datetime.combine(self.start, datetime.min.time(), tzinfo=timezone.utc)

    def end_as_datetime(self) -> datetime:
        return datetime.combine(self.end, datetime.min.time(), tzinfo=timezone.utc)


@dataclass(frozen=True)
class ProviderPlanItem:
    provider: str
    dataset: str
    symbol: str
    start: datetime
    end: datetime
    params: dict[str, str]


@dataclass(frozen=True)
class ProviderSelectionItem:
    provider: str
    tier: ProviderTier
    domains: tuple[FeatureDomain, ...]
    credential_status: CredentialStatus
    selected: bool
    reason: str


@dataclass(frozen=True)
class ProviderSelection:
    policy: SourcePolicy
    items: tuple[ProviderSelectionItem, ...]
    errors: tuple[str, ...] = ()

    @property
    def selected_provider_names(self) -> tuple[str, ...]:
        return tuple(item.provider for item in self.items if item.selected)


@dataclass(frozen=True)
class NormalizedRow:
    timestamp: datetime
    available_at: datetime
    source: str
    dataset: str
    symbol: str
    values: dict[str, float | str]


class ExternalDataProvider(Protocol):
    name: str
    required_credentials: tuple[str, ...]

    def capabilities(self) -> Sequence[ProviderCapability]:
        raise NotImplementedError

    def credential_status(self, env: Mapping[str, str]) -> CredentialStatus:
        raise NotImplementedError

    def plan_sync(self, request: ProviderSyncRequest) -> list[ProviderPlanItem]:
        raise NotImplementedError

    def fetch(self, item: ProviderPlanItem) -> list[dict[str, object]]:
        raise NotImplementedError

    def normalize(self, item: ProviderPlanItem, records: list[dict[str, object]]) -> list[NormalizedRow]:
        raise NotImplementedError

