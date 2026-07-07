# ROADMAP — from signal generator to a system anyone can run (and that can realistically make money)

_Last reviewed: 2026-07. Companion to [`solution2.md`](../solution2.md) (strategy research)
and [`architecture.md`](architecture.md) (live-trading enablement checklist)._

---

## Part 1 — The truth about making money, verified against evidence

Before planning features, be clear-eyed about what the data says. These are the
facts the entire roadmap is built on:

1. **Most retail traders lose.** Across 8M traders / 295M trades (1998–2025),
   74–89 % of retail investors lose money; a 2025 survey of 1,005 crypto
   retail traders found 84 % lost money in year one, and day trading was the
   #1 cause. ~97 % of day traders are unprofitable within a year.
   → **QT must never become a day-trading tool.** Low frequency is a feature.

2. **Win rate is not the problem — sizing is.** A 25k-trader study found 65 %
   had win rates above 50 %, yet 82 % of them still lost money: average win
   +1.2 % vs average loss −2.8 %.
   → **The risk engine (asymmetric stops, position caps, kill-switch) matters
   more than the signal.** QT already encodes this; never bypass it.

3. **What has real evidence, in order of robustness:**
   | Approach | Evidence | Realistic expectation |
   | --- | --- | --- |
   | **DCA / accumulate** | Lump-sum wins ~66 % of time on paper, but DCA wins behaviorally — it's the strategy people actually stick with through −70 % drawdowns | market return (BTC beta), smoother path |
   | **Funding-rate carry** | Academic full-sample mean ≈ 8 %/yr at very low vol; episodic spikes much higher; risk = funding flips negative | 5–15 % APR, near-market-neutral |
   | **Trend following** | Strong 2011–2020 (up to 255 % walk-forward annualized in early years), **decaying as the market matures** — recent studies show momentum losing explanatory power | lower CAGR than early backtests; main value is cutting drawdowns |
   | **Capitulation buying** | Practitioner-consensus (N-of-K factor voting); rare events, small sample | a few great entries per cycle; must be sized humbly |

4. **There is no guaranteed method.** Anyone claiming one is selling something.
   The honest edge for a retail user is: **low costs + low frequency +
   mechanical discipline + surviving drawdowns**. QT's job is to automate that
   discipline, not to promise alpha.

Sources: [NFTevening 2025 survey](https://nftevening.com/84-percent-of-retail-crypto-traders-lose-money-in-their-first-year/),
[hedgefundalpha 27-yr meta-analysis](https://hedgefundalpha.com/news/retail-traders-lost-volatility-event/),
[Quantified Strategies day-trading stats](https://www.quantifiedstrategies.com/day-trading-statistics/),
[CMU crypto carry trade](https://www.andrew.cmu.edu/user/azj/files/CarryTrade.v1.0.pdf),
[Funding-rate arbitrage risk/return (ScienceDirect 2025)](https://www.sciencedirect.com/science/article/pii/S2096720925000818),
[A Decade of Trend Following in Crypto (arXiv)](https://arxiv.org/pdf/2009.12155),
[Rosen & Wang, Bitcoin's evolution (SSRN)](https://papers.ssrn.com/sol3/Delivery.cfm/5732803.pdf?abstractid=5732803&mirid=1),
[Nakamoto Portfolio DCA study](https://nakamotoportfolio.com/static/docs/DCA_Lumpsum.pdf).

---

## Part 2 — Where the code stands today (review 2026-07)

**Working and verified** (84 tests green, smoke-tested end-to-end):

- 4 live signal strategies (`dca`, `capitulation`, `trend`, `carry`), each with
  its own YAML config, thread, heartbeat, and dashboard sub-route
- One-command lifecycle: `./start.sh` (setup → run → daemon → status → stop)
- Alerts: email + Telegram on every opportunity
- Research stack: composite score backtester, walk-forward, Monte Carlo,
  batch backtests (`qt.strategies.sim`), watchdog + systemd deploy

**The gaps between "signals" and "money":**

| # | Gap | Where |
| --- | --- | --- |
| G1 | `LiveBroker` is a stub — `submit/cash/position_qty` all raise `NotImplementedError`. The system cannot place any order. | `src/qt/execution/live.py` |
| G2 | Gallery strategies emit `Opportunity` alerts but nothing consumes them for execution — there is no Opportunity → RiskEngine → Broker path. | `src/qt/strategies/runner.py` |
| G3 | No live portfolio state: positions, cost basis, realized/unrealized P&L are not tracked or shown anywhere. | (missing module) |
| G4 | No paper-execution validation gate: we can't say "strategy X paper-traded N weeks with these results" before enabling live. | (missing) |
| G5 | Dashboard has no P&L / equity page; a user can't answer "am I making money?" | `src/qt/dashboard/` |
| G6 | Key security is only implied (.env), not enforced/documented: trade-only scope, no-withdrawal, IP allowlist, spend caps. | docs + config |

---

## Implementation status (2026-07)

| Phase | Status | What shipped |
| --- | --- | --- |
| **1 — Paper execution loop** | ✅ Done | `qt.portfolio` durable ledger; Opportunity → RiskEngine → PaperBroker → ledger in the runner; `/portfolio` dashboard page + P&L APIs + per-strategy P&L cards |
| **Backtest** | ✅ Done | `qt.backtest.strategy_backtest`: unified `run_strategy_backtest()` with deterministic synthetic-data fallback (runs offline), artifact export, `qt strategy run` + `./start.sh bt` |
| **2 — Live execution MVP** | ✅ Done | `LiveBroker` (ccxt) with 8 layered safety gates, trade-only key verification, `docs/live-checklist.md`, config-driven caps defaulting to tiny values |
| **3 — Anyone can use it** | ✅ Done | `./start.sh init` 5-question wizard with risk presets; plain-language bilingual (EN/中文) dashboard banner with traffic-light health |
| **4 — Trust & measurement** | ✅ Done | `qt.reporting` DCA-benchmark comparison with honest verdict; `qt report benchmark` + `scripts/monthly_report.py` (`./start.sh report`) |

The remaining work is operational, not code: run paper mode for 4+ weeks,
then walk the live rollout ladder in `docs/live-checklist.md`.

## Part 2.5 — Intelligence discovery + wick catcher (2026-07)

This tranche adds a research-to-runtime loop for finding edges before they
become live strategies.

| Phase | Status | What shipped |
| --- | --- | --- |
| **5 — Research intelligence layer** | Done | `docs/RESEARCH-EARNING.md`; `qt.intel` package with funding, spread, basis, depeg, and wick scanners; ranked JSON output under `data/runtime/intel/opportunities.json`; `/intel` and `/api/intel` dashboard views |
| **6 — Wick catcher strategy** | Done | Live `wick` signal strategy, YAML config, sim backtester, registry/export wiring, and `qt strategy run wick --synthetic` |
| **7 — Capital readiness gates** | Planned | Exchange-specific capacity checks, fee/slippage calibration from live fills, and paper-trading acceptance reports before any increase in order size |

Operating rule: the intel layer is discovery, not execution. A candidate must
graduate through a strategy-specific paper ledger, benchmark comparison, and
walk-forward/stress evidence before it can enter the live rollout ladder.

## Part 3 — The plan (phased; each phase is releasable on its own)

### Phase 1 — Paper execution loop (closes G2, G3, G4, G5)

Goal: every strategy's opportunities flow through the risk engine into the
**PaperBroker**, producing a real (simulated-fill) portfolio you can watch.

1. `qt.portfolio` module: durable JSON ledger per strategy
   (`data/runtime/portfolio/<name>.json`) — positions, cash, cost basis,
   realized/unrealized P&L, trade log (append-only CSV).
2. Extend `runner.py`: after `evaluate()`, feed `Opportunity` →
   `RiskEngine.evaluate_entry/exit` → `PaperBroker.submit` → ledger.
   Config flag per strategy: `execution: none | paper | live` (default `paper`).
3. Dashboard: `/portfolio` page + per-strategy P&L on `/strategy/<name>`
   (equity curve, open position, last 20 trades, total fees paid).
4. Nightly digest alert: one email/Telegram per day with per-strategy P&L —
   so "is it making money" arrives in your inbox.

Exit criteria: 4 strategies paper-trading simultaneously for **≥ 4 weeks**,
ledgers reconcile, dashboard shows live P&L.

### Phase 2 — Live execution MVP, smallest possible surface (closes G1, G6)

Goal: real orders, but only the narrowest safe subset: **spot BTC, long-only,
market/limit buys and sells on one venue (Binance or OKX via ccxt)**.

1. Implement `LiveBroker` with ccxt: `submit` (with retry + idempotent
   client order id), `cash`, `position_qty`, startup reconciliation
   (read venue balances/orders → rebuild ledger; never trust memory).
2. Hard guardrails, all enforced in code and configured in YAML:
   - `max_order_quote` (e.g. 100 USDT) and `max_daily_spend_quote`
   - `max_total_exposure_pct` across all strategies combined
   - global kill-switch file (`data/runtime/KILL`) checked before every order
   - dry-run mode: logs the exact order it *would* send, for 1 week minimum
3. Key security (documented in `docs/live-checklist.md` + enforced at startup):
   - API key must be **trade-only, withdrawal disabled** — startup verifies via
     ccxt permissions endpoint and refuses to run otherwise
   - IP allowlist on the exchange side; keys only in `.env` (never in YAML/git)
4. Rollout ladder (enforced by config, not discipline):
   `dry-run 1 week → live with 50–100 USDT per order, DCA strategy only →
   add carry → add capitulation/trend`, each step gated on the previous one
   behaving identically to paper.

Exit criteria: 30 days live on small size with zero order errors,
ledger == exchange balance at all times.

### Phase 3 — "Anyone can use it" (usability)

1. `./start.sh init` interactive wizard: asks 5 questions (venue, budget/month,
   risk level conservative/balanced/aggressive, email, Telegram) → writes
   `.env` + strategy YAMLs. Risk level maps to pre-tested parameter presets.
2. Dashboard home rewritten in plain language: "What the system did this week,
   in one paragraph" + traffic-light health, bilingual (EN/中文).
3. Every alert carries the *why* in one sentence (already partly done via
   `Opportunity.reason`) and the *what happens next*.
4. Docs: a single `GETTING-STARTED.md` (EN/中文) — 10 minutes from clone to
   running paper mode; no crypto jargon without a one-line explanation.

### Phase 4 — Trust & measurement (ongoing)

1. Walk-forward re-validation job (monthly cron): every strategy's params
   re-tested against the latest year; params only change via PR with the
   walk-forward report attached.
2. Monthly report artifact: P&L vs plain DCA benchmark — the honest question
   "did the extra complexity beat just buying?" is answered automatically.
3. Anomaly monitors: fee drift, slippage vs paper assumptions, funding regime
   change for carry.

---

## Part 4 — Ground rules (non-negotiable, encoded in the system)

1. **Paper first, always.** No strategy goes live without ≥ 4 weeks of clean
   paper execution. The config refuses `execution: live` if the paper ledger
   is younger than that.
2. **Start money = money you can lose.** Suggested first live budget:
   100–500 USDT total.
3. **The benchmark is DCA.** If a strategy can't beat simple weekly buying
   after fees over a full cycle, it gets turned off — no sunk-cost defense.
4. **Never day-trade, never leverage-long, never chase.** These are the three
   behaviors the evidence says destroy retail accounts; QT will not implement
   them.
