# Migrated verbatim from btc_quant_evolution; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict

from .providers import all_providers
from .schemas import (
    CredentialStatus,
    ExternalDataProvider,
    FeatureDomain,
    ProviderCapability,
    ProviderSelection,
    ProviderSelectionItem,
    ProviderTier,
    SourcePolicy,
)


def _available_providers(env: Mapping[str, str]) -> list[ExternalDataProvider]:
    return registered_providers(env)


def registered_providers(env: Mapping[str, str]) -> list[ExternalDataProvider]:
    providers = all_providers()
    selection = select_providers(env, "auto", providers=providers)
    selected_names = set(selection.selected_provider_names)
    return [provider for provider in providers if provider.name in selected_names]


def provider_statuses(env: Mapping[str, str]) -> dict[str, CredentialStatus]:
    return {provider.name: provider.credential_status(env) for provider in all_providers()}


def provider_selection_fingerprint(env: Mapping[str, str], policy: SourcePolicy) -> str:
    """Fingerprint provider availability without including credential values."""
    selection = select_providers(env, policy)
    payload = {
        "errors": selection.errors,
        "items": [asdict(item) for item in selection.items],
        "policy": selection.policy,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def select_providers(
    env: Mapping[str, str],
    policy: SourcePolicy,
    *,
    providers: Sequence[ExternalDataProvider] | None = None,
) -> ProviderSelection:
    candidates = tuple(providers) if providers is not None else all_providers()
    items = tuple(_selection_item(provider, env, policy) for provider in candidates)
    has_selected_paid = any(item.selected and item.tier == "paid" for item in items)
    errors = ()
    if policy == "paid-required" and not has_selected_paid:
        errors = ("paid-required requires at least one credentialed paid provider",)
    return ProviderSelection(policy=policy, items=items, errors=errors)


def _selection_item(
    provider: ExternalDataProvider,
    env: Mapping[str, str],
    policy: SourcePolicy,
) -> ProviderSelectionItem:
    capabilities = tuple(provider.capabilities())
    if not capabilities:
        return ProviderSelectionItem(
            provider=provider.name,
            tier="public",
            domains=(),
            credential_status=provider.credential_status(env),
            selected=False,
            reason="skipped: provider has no capabilities",
        )

    tier = capabilities[0].tier
    if tier is None or any(capability.tier != tier for capability in capabilities):
        raise ValueError(f"provider must declare one tier: {provider.name}")
    domains = _unique_domains(capabilities)
    credential_status = provider.credential_status(env)
    implemented = all(capability.implemented for capability in capabilities)
    if not implemented:
        return ProviderSelectionItem(provider.name, tier, domains, credential_status, False, "skipped: provider is not implemented")
    if tier == "paid" and policy == "free-only":
        return ProviderSelectionItem(provider.name, tier, domains, credential_status, False, "skipped: free-only policy")
    if credential_status != "enabled":
        return ProviderSelectionItem(provider.name, tier, domains, credential_status, False, _skip_reason(credential_status))
    if tier == "paid":
        return ProviderSelectionItem(provider.name, tier, domains, credential_status, True, "selected: credentialed paid provider")
    return ProviderSelectionItem(provider.name, tier, domains, credential_status, True, "selected: implemented public provider")


def _unique_domains(capabilities: Sequence[ProviderCapability]) -> tuple[FeatureDomain, ...]:
    domains: list[FeatureDomain] = []
    for capability in capabilities:
        domain = capability.domain
        if domain is not None and domain not in domains:
            domains.append(domain)
    return tuple(domains)


def _skip_reason(status: CredentialStatus) -> str:
    if status == "missing_credentials":
        return "skipped: missing credentials"
    return f"skipped: provider status is {status}"

