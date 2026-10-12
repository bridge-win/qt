# Codex BTC agent acceptance — 2026-10-13

Branch: `codex/btc-extreme-agent-testnet`. Deployment target: local Docker.

## Verified

- 201 relevant tests passed: 177 new BTC-agent tests and 24 existing indicator,
  paper-broker and live-guardrail regression tests.
- Ruff passed; strict mypy passed for all 11 BTC-agent Python modules.
- Both Docker images built; the reviewer contains `codex-cli 0.162.1` and its
  non-interactive command/schema flags were inspected inside the container.
- Production public BTC daily history and Binance Spot Testnet public quotes
  were reachable. Testnet precision/notional metadata was checked.
- Docker worker runs in **paper** mode with a named persistent volume,
  restart policy and health check. It reports `watching` / `hold`, zero position
  and zero pending orders. A worker restart retained the SQLite records and
  resumed monitoring.
- Reviewer without an API key returns HTTP 503. New entries remain blocked;
  the HTTP liveness check does not claim model authentication.
- Tests cover unfinished/stale/malformed daily bars, reviewer veto and failure,
  post-review quote movement, spend/position limits, partial and zero fills,
  ambiguous submission recovery, idempotent accounting, dust, key/mode isolation,
  restart locking, and protective exits independent of model availability.

## Diagnostic replay, not strategy acceptance

The final Docker replay downloaded 1,000 daily candles, excluded the unfinished
day and used 210 warm-up bars. Evaluation covers 2024-08-14 through
2026-10-12 00:00 UTC (789 daily bars), with 100 USDT initial capital,
10 bps fees and 10 bps adverse slippage on each side.

| Rules / benchmark | Net portfolio return | Max drawdown | Completed trades |
| --- | ---: | ---: | ---: |
| Extreme-event rules | +0.30% | 7.92% | 2 |
| Buy and hold | +37.99% | 52.97% | 0, position remains open |
| Monthly 25-USDT DCA, same fixed initial cash | +32.68% | 52.97% | 0, position remains open |

These portfolios have different exposure. Two completed trades cannot establish
an edge. This is the final replay with the service's between-buy cooldown and
fee-adjusted cost basis; it replaces the preliminary diagnostic before those
rules were aligned. It excludes Codex judgment, real IOC liquidity, partial fills,
intraday monitoring and the runtime entry-deviation guard. Future thresholds
must not be chosen on this same evaluation sample and called out-of-sample.
The ignored local artifact is `data/runtime/btc-agent-verification/backtest.json`.

## Still unverified

- Authenticated Codex response: `CODEX_API_KEY` was not available.
- Authenticated Binance sandbox balances or orders: dedicated
  `BINANCE_TESTNET_API_KEY` and `BINANCE_TESTNET_API_SECRET` were not available.
- A real sandbox buy/sell round trip, a 24-hour soak, remote/cloud deployment,
  and any real-money trading or profitability validation.
- Bitcoin L1 testnet transactions: this implementation tests the exchange
  order lifecycle, not on-chain wallet transfers.

No authenticated order was sent and no secret was committed. Follow the
[operator guide](../codex-btc-agent.md) to configure keys locally and complete
the authenticated smoke; do not interpret public connectivity or unit-test
fills as exchange execution evidence.
