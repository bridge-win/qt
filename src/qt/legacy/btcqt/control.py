# Migrated verbatim from btcqt; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
"""Authenticated runtime controls and validated, persistent hot configuration."""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from .config import Config

log = logging.getLogger(__name__)


HOT_PATHS = {
    "event.z_trigger", "event.liq_oi_pctl_trigger", "event.doi_pctl_trigger",
    "event.max_age_min", "event.reclaim_min", "event.reclaim_min_degraded",
    "event.xvenue_dev_big", "event.accept_hold_min",
    "composite.cascade_threshold", "composite.post_cascade_min",
    "composite.crowding_extreme",
    "s0.risk_per_trade", "s0.min_abs_t", "s0.min_agree", "s0.stop_atr1h_mult",
    "s0.cooldown_min", "s0.high_vol_rv_pctl", "s0.high_vol_size_mult",
    "s1.risk_per_trade", "s1.stop_buffer_atr",
    "s1.time_stop_min", "s1.cooldown_min", "s1.entry_slippage_bps",
    "s1.rung_devs", "s1.risk_per_rung", "s1.max_rungs_filled",
    "s1.tp_revert_frac", "s1.stop_atr_mult", "s1.refresh_move_sigma",
    "s1.freeze_liq_pctl", "s1.freeze_doi_5m",
    "s2.funding_pctl_hi", "s2.funding_pctl_lo", "s2.min_persist_intervals",
    "s2.oi_pctl_min", "s2.risk_per_trade", "s2.stop_atr4h_mult",
    "s2.time_stop_hours", "s2.funding_exit_pctl",
    "risk.daily_soft_loss_halt", "risk.daily_loss_halt", "risk.weekly_loss_halt",
    "risk.system_drawdown_halt", "risk.max_gross_leverage",
    "risk.max_position_notional_frac", "risk.max_order_notional",
    "risk.price_sanity_band", "risk.spread_guard_pctl",
}

ControlCallback = Callable[[], Awaitable[str]]


def _get_path(root: Any, path: str) -> Any:
    value = root
    for part in path.split("."):
        value = getattr(value, part)
    return value


def _set_dict_path(root: dict, path: str, value: Any) -> None:
    parts = path.split(".")
    cur = root
    for part in parts[:-1]:
        cur = cur[part]
    cur[parts[-1]] = value


class RuntimeController:
    def __init__(self, cfg: Config, state_dir: Path,
                 callbacks: dict[str, ControlCallback],
                 apply_hook: Callable[[], None] | None = None):
        self.cfg = cfg
        self.callbacks = callbacks
        self.apply_hook = apply_hook
        self.path = state_dir / "runtime-config.json"
        self.audit_path = state_dir / "control-audit.jsonl"

    def values(self) -> dict[str, Any]:
        return {path: _get_path(self.cfg, path) for path in sorted(HOT_PATHS)}

    def apply_saved(self) -> None:
        if not self.path.exists():
            return
        changes = json.loads(self.path.read_text(encoding="utf-8"))
        self.patch(changes, persist=False)

    def patch(self, changes: dict[str, Any], *, persist: bool = True) -> dict[str, Any]:
        if not changes or any(path not in HOT_PATHS for path in changes):
            unknown = sorted(set(changes) - HOT_PATHS)
            raise ValueError(f"unsupported parameter(s): {unknown or 'empty patch'}")
        raw = self.cfg.model_dump()
        for path, value in changes.items():
            _set_dict_path(raw, path, value)
        validated = Config.model_validate(raw)
        if persist:
            self._audit("config_patch_requested", {"changes": changes})
            persisted = {path: _get_path(validated, path) for path in sorted(HOT_PATHS)}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            with tmp.open("w", encoding="utf-8") as handle:
                handle.write(json.dumps(persisted, indent=2))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
        for path in changes:
            target, leaf = path.rsplit(".", 1)
            setattr(_get_path(self.cfg, target), leaf, _get_path(validated, path))
        if self.apply_hook:
            self.apply_hook()
        return self.values()

    async def command(self, name: str) -> str:
        callback = self.callbacks.get(name)
        if callback is None:
            raise ValueError(f"unknown command: {name}")
        self._audit("command_requested", {"name": name})
        detail = await callback()
        try:
            self._audit("command_completed", {"name": name, "detail": detail})
        except OSError:
            log.exception("control command completed but completion audit failed")
        return detail

    def _audit(self, event: str, data: dict[str, Any]) -> None:
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        row = {"ts": int(time.time() * 1000), "event": event, **data}
        with self.audit_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, default=str) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

