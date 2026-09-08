"""Provider catalog backed by the preserved external-data implementation.

The workbench intentionally does not duplicate provider declarations. Every
row is projected from retained btc-quant evolution provider objects, so its
credential policy, coverage, and implementation flag retain source semantics.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import asdict

from qt.legacy.btc_quant_evolution.external_data.providers import all_providers
from qt.legacy.btc_quant_evolution.external_data.registry import select_providers
from qt.legacy.btc_quant_evolution.external_data.schemas import SourcePolicy


def provider_catalog(
    env: Mapping[str, str] | None = None,
    *,
    policy: SourcePolicy = "auto",
) -> list[dict[str, object]]:
    """Return actual source-provider capabilities without credential values."""

    runtime_env = os.environ if env is None else env
    providers = all_providers()
    selection = select_providers(runtime_env, policy, providers=providers)
    selected = {item.provider: item for item in selection.items}
    rows: list[dict[str, object]] = []
    for provider in providers:
        item = selected[provider.name]
        capabilities = [asdict(capability) for capability in provider.capabilities()]
        rows.append(
            {
                "name": provider.name,
                "required_credentials": list(provider.required_credentials),
                "credential_status": item.credential_status,
                "selected": item.selected,
                "selection_reason": item.reason,
                "tier": item.tier,
                "domains": list(item.domains),
                "capabilities": capabilities,
                "entry_point": f"{type(provider).__module__}:{type(provider).__name__}",
                "source_policy": policy,
            }
        )
    return rows
