"""Interactive setup wizard — 5 questions to a running QT.

    python scripts/init_wizard.py

Writes ``.env`` (venue, budget, alerts) and tunes the per-strategy YAMLs
in ``config/strategies/`` from a chosen risk preset. Everything it writes
is something you could edit by hand; the wizard just makes the first run
painless. Re-running is safe — it shows current values as defaults.

Bilingual prompts (English / 中文).
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"
ENV_EXAMPLE = ROOT / ".env.example"
STRAT_DIR = ROOT / "config" / "strategies"


# Risk presets map a single choice to concrete, pre-tested parameters.
# conservative = smaller size, higher alert bar; aggressive = the opposite.
RISK_PRESETS: dict[str, dict[str, object]] = {
    "conservative": {
        "label": "Conservative / 保守 — smaller buys, only strongest signals",
        "dca_base_buy_quote": 50.0,
        "dca_multiplier_k": 1.5,
        "carry_enter_apr": 0.20,
        "min_alert_severity": "critical",
        "max_order_quote": 50.0,
        "max_daily_spend_quote": 200.0,
        "max_total_exposure_quote": 500.0,
    },
    "balanced": {
        "label": "Balanced / 均衡 — moderate size, sensible defaults",
        "dca_base_buy_quote": 100.0,
        "dca_multiplier_k": 2.0,
        "carry_enter_apr": 0.15,
        "min_alert_severity": "warning",
        "max_order_quote": 100.0,
        "max_daily_spend_quote": 500.0,
        "max_total_exposure_quote": 1000.0,
    },
    "aggressive": {
        "label": "Aggressive / 激进 — larger buys, more signals (higher risk)",
        "dca_base_buy_quote": 200.0,
        "dca_multiplier_k": 2.5,
        "carry_enter_apr": 0.10,
        "min_alert_severity": "info",
        "max_order_quote": 250.0,
        "max_daily_spend_quote": 1500.0,
        "max_total_exposure_quote": 3000.0,
    },
}


def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        val = input(f"{prompt}{suffix}: ").strip()
    except EOFError:
        return default
    return val or default


def _ask_choice(prompt: str, choices: list[str], default: str) -> str:
    print(prompt)
    for i, c in enumerate(choices, 1):
        mark = " (default)" if c == default else ""
        print(f"  {i}. {c}{mark}")
    raw = _ask(f"Choose 1-{len(choices)}", str(choices.index(default) + 1))
    try:
        idx = int(raw) - 1
        if 0 <= idx < len(choices):
            return choices[idx]
    except ValueError:
        if raw in choices:
            return raw
    return default


def _read_env() -> dict[str, str]:
    env: dict[str, str] = {}
    src = ENV_PATH if ENV_PATH.exists() else ENV_EXAMPLE
    if src.exists():
        for line in src.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip()
    return env


def _write_env(env: dict[str, str]) -> None:
    # Preserve the example's comments/order where possible, overlay values.
    template = ENV_EXAMPLE.read_text().splitlines() if ENV_EXAMPLE.exists() else []
    seen: set[str] = set()
    out: list[str] = []
    for line in template:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            k = stripped.split("=", 1)[0].strip()
            if k in env:
                out.append(f"{k}={env[k]}")
                seen.add(k)
                continue
        out.append(line)
    for k, v in env.items():
        if k not in seen:
            out.append(f"{k}={v}")
    ENV_PATH.write_text("\n".join(out) + "\n")


def _apply_preset(preset: dict[str, object]) -> None:
    """Tune the strategy YAMLs in place from the preset."""
    files = {
        "dca": STRAT_DIR / "dca.yaml",
        "carry": STRAT_DIR / "carry.yaml",
        "trend": STRAT_DIR / "trend.yaml",
        "capitulation": STRAT_DIR / "capitulation.yaml",
    }
    for name, path in files.items():
        if not path.exists():
            continue
        data = yaml.safe_load(path.read_text()) or {}
        data.setdefault("params", {})
        data["min_alert_severity"] = preset["min_alert_severity"]
        if name == "dca":
            data["params"]["base_buy_quote"] = preset["dca_base_buy_quote"]
            data["params"]["multiplier_k"] = preset["dca_multiplier_k"]
        if name == "carry":
            data["params"]["enter_apr"] = preset["carry_enter_apr"]
        path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))


def main() -> int:
    print("=" * 60)
    print("  QT setup wizard / 配置向导")
    print("  Answer 5 questions. Enter accepts the [default].")
    print("=" * 60)

    env = _read_env()

    # 1. Venue
    venue = _ask_choice(
        "\n1) Which exchange? / 用哪个交易所？",
        ["binance", "okx"],
        env.get("QT_EXECUTION__VENUE", "binance"),
    )
    env["QT_EXECUTION__VENUE"] = venue

    # 2. Monthly budget → informs caps (we don't force it, just record + scale)
    budget = _ask(
        "\n2) Monthly budget in USDT? / 每月预算（USDT）",
        env.get("QT_MONTHLY_BUDGET_USDT", "300"),
    )
    env["QT_MONTHLY_BUDGET_USDT"] = budget

    # 3. Risk level → preset
    risk = _ask_choice(
        "\n3) Risk level? / 风险偏好？",
        list(RISK_PRESETS.keys()),
        "balanced",
    )
    preset = RISK_PRESETS[risk]
    print(f"   → {preset['label']}")

    # 4. Email alerts
    email = _ask(
        "\n4) Email for buy alerts (blank = skip) / 接收提醒的邮箱（留空跳过）",
        env.get("QT_SMTP_TO", ""),
    )
    if email:
        env["QT_SMTP_TO"] = email
        env.setdefault("QT_SMTP_HOST", "")
        print("   (fill QT_SMTP_HOST/USER/PASS in .env to actually send)")

    # 5. Telegram
    tg = _ask(
        "\n5) Telegram chat id (blank = skip) / Telegram chat id（留空跳过）",
        env.get("QT_TELEGRAM_CHAT_ID", ""),
    )
    if tg:
        env["QT_TELEGRAM_CHAT_ID"] = tg
        print("   (fill QT_TELEGRAM_BOT_TOKEN in .env to actually send)")

    # Apply live-guardrail caps from the preset (kept safe: still paper by default)
    env["QT_EXECUTION__MAX_ORDER_QUOTE"] = str(preset["max_order_quote"])
    env["QT_EXECUTION__MAX_DAILY_SPEND_QUOTE"] = str(preset["max_daily_spend_quote"])
    env["QT_EXECUTION__MAX_TOTAL_EXPOSURE_QUOTE"] = str(preset["max_total_exposure_quote"])
    env.setdefault("QT_EXECUTION__MODE", "paper")
    env.setdefault("QT_EXECUTION__LIVE_ENABLED", "false")

    _write_env(env)
    _apply_preset(preset)

    print("\n" + "=" * 60)
    print("  Done. / 完成。")
    print(f"  Wrote {ENV_PATH}")
    print(f"  Tuned strategy YAMLs in {STRAT_DIR} to '{risk}' preset")
    print("  Start everything (paper mode) with:  ./start.sh")
    print("  Live trading stays OFF until you follow docs/live-checklist.md")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
