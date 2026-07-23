# Live-trading enablement checklist

Live trading is **off by default** and gated behind many independent
safety layers. Do not skip steps. The whole point of QT is to automate
discipline — that includes the discipline of turning real money on slowly.

## Prerequisites (must all be true)

- [ ] The strategy has paper-traded cleanly for **≥ 4 weeks**
      (`/portfolio` shows a real ledger, no errors, P&L reconciles).
- [ ] You have run it through `qt.backtest.walkforward` and
      `qt.backtest.montecarlo` and understood the drawdown distribution.
- [ ] The money at stake is money you can afford to lose entirely.
      Suggested first live budget: **100–500 USDT total**.

## Exchange-side key security (do this on the exchange website)

- [ ] Create an API key that is **trade-only**. Enable *Spot Trading*.
      **Disable withdrawals.** QT refuses to trade with a withdrawal-capable
      key (`require_trade_only_key=true` — verified at startup).
- [ ] Add an **IP allowlist** on the key: only the server's IP.
- [ ] Never put keys in YAML or git. They live only in `.env`
      (`QT_BINANCE_API_KEY`, `QT_BINANCE_API_SECRET`).

## The safety gates (all enforced in `LiveBroker`)

| Gate | Config field | Default |
| --- | --- | --- |
| Master switch | `execution.live_enabled` | `false` |
| Dry-run (log, don't send) | `execution.dry_run` | `true` |
| Global stop file | `execution.kill_file` | `data/runtime/KILL` |
| Trade-only key required | `execution.require_trade_only_key` | `true` |
| Per-order cap | `execution.max_order_quote` | `100` USDT |
| Daily spend cap | `execution.max_daily_spend_quote` | `500` USDT |
| Total exposure cap | `execution.max_total_exposure_quote` | `1000` USDT |
| Symbol allowlist | `execution.symbol` | `BTC/USDT` |
| Market orders only | (hard-coded) | — |

The **KILL switch**: `touch data/runtime/KILL` blocks every order
immediately, no restart needed. `rm data/runtime/KILL` re-enables.

## Rollout ladder (do not jump steps)

1. **Dry-run, 1 week.** `live_enabled=true`, `dry_run=true`. QT logs the
   exact order it *would* send every time an opportunity fires. Confirm the
   orders match what paper mode did.
2. **Live, tiny, DCA only.** `dry_run=false`, `max_order_quote=50`,
   only the `dca` strategy enabled. Watch for 1 week. The ledger must equal
   the exchange balance at all times (`LiveBroker.reconcile`).
3. **Add carry**, then **capitulation / trend**, one at a time, each gated
   on the previous behaving identically to paper.
4. Only after 30 days of clean live operation, consider raising the caps.

## Turning it on

```bash
# .env
QT_EXECUTION__LIVE_ENABLED=true
QT_EXECUTION__DRY_RUN=true          # keep true for the first week
QT_EXECUTION__MAX_ORDER_QUOTE=50
QT_BINANCE_API_KEY=...              # trade-only, no withdrawal, IP-locked
QT_BINANCE_API_SECRET=...
```

Then restart. Watch the logs for `live_key_verified_trade_only` (good) or
`live_order_dry_run` (dry-run working). Any `UnsafeApiKeyError` means the
key still has withdrawal permission — fix it on the exchange.

Before starting the long-running service, run the live preflight. It validates
the configured BTC symbol, live switches, trade-only key verification, and caps
without placing an order:

```bash
qt --config config/default.yaml live preflight
```

For real-data backtest rehearsal, fail closed instead of falling back to
synthetic data:

```bash
qt --config config/default.yaml strategy run dca --real-only
qt --config config/default.yaml strategy run carry --real-only
```

Each backtest summary includes `data_fingerprint`, a stable SHA-256 digest of
the validated OHLCV snapshot, so repeated runs can prove they used the same
market data.
