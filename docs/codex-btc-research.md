# Codex BTC monitor: evidence and research protocol

Research checked on 2026-10-12, implementation validated on 2026-10-13. This is a testable system design, not a
profitability claim. No reviewed source establishes that Codex can reliably
identify Bitcoin's absolute tops or bottoms. An extreme signal identifies an
unusual condition whose future return distribution must be measured.

## What the evidence supports

| Approach | Primary evidence | Consequence for this system |
| --- | --- | --- |
| Trend and market regime | Liu and Tsyvinski find cryptocurrency time-series momentum and an association between investor attention and future returns in their historical sample. [Paper](https://www.nber.org/papers/w24877) | Include a trend benchmark and recovery confirmation. An oversold reading alone is not evidence that a decline is over. |
| On-chain valuation extremes | Glassnode defines MVRV as market value relative to realized value; readings below 1 describe aggregate unrealized losses. Its analysis studies historical cycle extremes. [Research](https://research.glassnode.com/mastering-the-mvrv-ratio/) | MVRV can describe valuation context. It does not identify an exchange fill price, every holder's true cost basis, or a guaranteed reversal. |
| Leverage and crowded positioning | BIS research finds large, variable futures carry, connects elevated carry with subsequent crashes, and identifies margin and liquidation risks for arbitrageurs. [Paper](https://www.bis.org/publications/working-paper-1087-crypto-carry) | Funding, basis, and open-interest stress are useful candidate context. Cash-and-carry is a separate two-leg strategy with counterparty and margin risks, outside the first spot-only system. |
| LLM agents | FinMem uses memory, profiling, and decision modules and reports historical stock-trading experiments. [Paper](https://arxiv.org/abs/2311.13743) | Use structured, timestamped context and retain decisions for audit. Results on selected stocks do not establish a BTC trading edge. |
| LLM robustness | FINSABER evaluates longer periods and broader stock universes and finds previously reported LLM advantages deteriorate. [Paper](https://arxiv.org/abs/2505.07078) | Compare a fixed deterministic baseline with the same baseline plus Codex review. Greater agent complexity is not a substitute for out-of-sample evidence. |

The reviewed literature motivates hypotheses; it does not validate the exact
thresholds below. The hit-rate percentages, perfect-cycle claims, return
forecasts, and descriptions of near risk-free carry in older
[`strategy.md`](strategy.md) and [`RESEARCH-EARNING.md`](RESEARCH-EARNING.md)
are not accepted performance evidence for this service. They were not
reproduced with point-in-time data, costs, and a locked evaluation period in
this research task.

## A conservative candidate to test

Use daily, completed UTC candles for opportunity identification, with more
frequent operational checks. Continuous monitoring does not require continuous
trading. The initial experiment is long-only BTC spot: a sell reduces an
existing position, and does not open a leveraged short.

The initial service uses the following hypothesis. These thresholds are
conservative engineering choices, not calibrated or validated estimates from
the papers above. Research runs should record a new experiment identifier
after a rule change; the service does not currently enforce experiment versioning.

1. Require at least 210 consecutive completed daily candles with finite,
   consistent OHLCV values. Reject duplicated, missing, future, or stale data.
2. Compute drawdown from the trailing 90-day highest close, Wilder RSI(14),
   and close divided by SMA(200), using only information available at the
   decision timestamp.
3. Mark a possible capitulation zone when RSI is at most 25 and drawdown is
   at most -30%. Require coincident stress on that bar: volume at least twice
   the preceding 30-day mean, or a daily return at most -8%. Look for this
   combined event in the latest three completed bars, and require the latest
   close to exceed the previous close before considering an entry. These
   measures overlap and are correlated; they are not independent confirmations.
4. Enter at most one bounded position after the candle closes and after Codex
   approves the existing candidate. Require seven days between recorded buys,
   including after a position closes. Initial virtual-order and daily-buy
   caps are 25 USDT with a fee reserve; the position-value cap checked on entry
   is 100 USDT, with no adding to a position. Market appreciation can increase
   the position's value afterward. These are sandbox limits, not a sizing
   recommendation for a real-money account.
5. Check a 10% loss exit against the recorded fee-adjusted entry cost, a 15%
   trailing exit activated after a 20% gain, and a 90-day maximum holding time
   at each poll using the current bid. An overheating exit requires completed
   daily RSI at least 80 and close/SMA(200) at least 1.5. Sells also have the
   per-order cap, so a position may require multiple polls to close. Actual
   losses can exceed the planned threshold. Compare the combined exit policy
   against simpler alternatives without selecting on the final evaluation set.
6. Protective exits bypass Codex and daily-candle retrieval. They still require
   valid quotes, balances, resolved prior orders, and a working process and
   venue. The manual kill file halts every new submission, including exits;
   it does not liquidate a position. These are software checks, not standing
   venue stop orders.

These rules deliberately may produce no trades for long periods. A one-day
bounce can fail, and extended bear markets can remain statistically cheap.
Monitor counts, holding periods, and time invested rather than promising a
fixed number of trades per year. A cooldown limits repeated entries but does
not by itself establish ultra-low frequency.

Optional future confirmation can include point-in-time MVRV below 1, sustained
negative funding, or a large open-interest contraction. These require an
identified provider, documented units, release times, revision policy, and
access rights. Keep price-only results separate from enriched results. Do not
silently lower a required confirmation count when a provider is unavailable.
News can explain an event or veto an entry; headlines alone should not produce
an executable order.

## Codex's bounded role

The service sends Codex only the signal fields: action, reason, bar timestamp,
price, ATR, and calculated factors. It does not send account balances, position
state, execution limits, news, funding, or on-chain data. The current reviewer
is therefore a consistency check over supplied numerical context, not an
independent market-research agent. Its strict response schema has only
`approve` and `reason`; there is no separate risks field. The service retains
authority over sizing, risk checks, and the order lifecycle.

Codex must not invent prices, bypass a rule, change limits, choose a production
endpoint, or receive broker credentials. A timeout, invalid JSON, unavailable
model, or schema-invalid response blocks a new entry. The reviewer is prompted
to veto inconsistent evidence; this is a model judgment, not a separately
validated guarantee. A durable per-bar latch permits only one review attempt
for that candidate bar, including after a veto or review failure. The reviewer
has no enabled browsing or execution tools and no exchange credentials.

Current persistence includes the latest review's bar and review timestamps,
decision, signal input and its SHA-256 hash, and prompt version. Position
state, order intents and fills, a heartbeat, and up to 10,000 recent cycle
events are also saved. The `reviews` table retains structured inputs and
responses, including sanitized failures; the latest-review record is replaced
on each review for status display. The resolved model identity is not recorded.
Adding model metadata is a research recommendation before comparing versions. A saved
explanation alone does not establish predictive accuracy.

For later news ingestion, save source URLs and publication and retrieval times.
Treat all fetched text as untrusted evidence, never as agent instructions.
Preserve the original dataset needed to reproduce the information available
at the decision time. Disable tool access that would let a retrieved headline
instruct the reviewer to place orders or reveal credentials.

## Reuse maintained components

| Component | Decision |
| --- | --- |
| [CCXT](https://github.com/ccxt/ccxt/wiki/manual) | The service reuses the installed dependency to sign allowlisted Binance Spot Testnet requests, with sandbox mode set before other calls and separate testnet credentials. Daily OHLCV is read through HTTP from Binance's public production market-data endpoint. |
| [Freqtrade/FreqAI](https://www.freqtrade.io/en/stable/freqai/) | A maintained reference for scheduled model retraining, historical simulation, dry runs, and Docker deployment. Its example strategy is explicitly for demonstration. Consider it for a later supervised-model experiment; adopting a second trading engine now would duplicate QT's existing execution and persistence concerns. |
| [Freqtrade lookahead analysis](https://www.freqtrade.io/en/stable/lookahead-analysis/) | Reuse the validation principle: compare decisions computed on a full history with decisions computed on truncated prefixes. A clean result covers only exercised signals and does not prove profitability. |
| Existing QT components | Keep this feature isolated from historical strategies, reuse installed numerical and validation libraries, and minimize custom exchange protocol code. |

## Evaluation that can falsify the hypothesis

The implemented replay is a deterministic daily baseline without Codex. It
requires 210 warmup bars and at least one subsequent execution bar, assumes
next-day-open fills, charges 10 basis points of fees and 10 basis points of
adverse slippage per side, and marks any final holding to market. It compares
100 USDT starting cash against buy-and-hold and 25-USDT monthly purchases
funded from the same cash, without deposits. Different exposure levels limit
direct return comparisons.

The replay does not replicate the running service: exits observe daily closes,
not each poll's bid; a full position sells in one synthetic fill rather than
potentially several capped orders; there is no quote-age, spread, partial-fill,
or exchange-outage simulation. The replay shares the service's seven-day
between-buy cooldown and fee-adjusted risk cost basis, but its fixed fee model
and entry sizing still differ from actual venue commissions and the service's
fee reserve. These differences must be considered before claiming parity.
Paper and sandbox execution test the software path; neither adds independent
market factors or validates the model.

The following is the broader research protocol, not a claim that all these
experiments have already been run:

- Freeze a small set of variants and record every attempted parameter set.
  Use chronological training/validation/test partitions across bull, bear,
  and crash periods. Do not randomly split overlapping time-series labels.
  Purge overlapping holding periods at boundaries and fit transformations only
  on training data. Repeatedly examining a holdout can itself overfit;
  [Bailey et al.](https://escholarship.org/uc/item/4w1110bb) develop a framework
  for quantifying this selection risk.
- Calculate features at the close and execute no earlier than the next
  available price. Charge both entry and exit fees and stress spreads,
  slippage, gaps, partial fills, and outage delays. If a daily candle touches
  both stop and target, use finer data or a conservative ordering assumption.
- Benchmark cash, buy-and-hold, periodic equal purchases, a simple trend rule,
  the deterministic candidate, and candidate plus Codex. Report net return,
  maximum drawdown, exposure, turnover, costs, trade count, duration, and
  uncertainty. A high win rate can coexist with losses from a few large tails.
- For LLM historical tests, prevent future articles and retrospective summaries
  from entering the prompt. A model's training data may already contain the
  historical outcome; historical replay alone cannot eliminate that leakage.
  Use forward paper observation for the decisive Codex comparison.
- Several cycle trades are not a large independent sample. Report per-regime
  outcomes and sensitivity to nearby parameters; do not infer a stable edge
  from a few spectacular reversals. Synthetic fixtures validate program
  behavior only. Even a successful forward observation period guarantees
  neither future profit nor a bounded maximum loss.

## What each test network proves

| Environment | Useful acceptance evidence | What it does not prove |
| --- | --- | --- |
| Historical replay / local paper account | Deterministic signals, risk behavior, persistence, repeatability, restart recovery | Real venue order acceptance or real profits |
| Exchange BTC/USDT sandbox | Authenticated order submission, fills/cancel, balances, duplicate protection and reconciliation using virtual exchange balances | Bitcoin blockchain settlement, realistic liquidity, or production returns |
| Bitcoin Core regtest | Controlled local blockchain, wallet funding, transaction broadcast and confirmation | Connection to a public Bitcoin test network or exchange matching |
| Public Bitcoin testnet4 / signet | Actual test-network peers, blocks and, if funded, a transaction ID and confirmations | A BTC/USD market, fiat proceeds, or profitable timing |

[Binance's official Spot Test Network documentation](https://github.com/binance/binance-spot-api-docs/blob/master/testnet/general-info.md)
states that its balances are virtual and cannot be moved in or out. Periodic
resets remove pending and executed orders, so account state must be reconciled
instead of assuming a local fill ledger remains authoritative. An endpoint
that validates an order without matching it is not evidence of a completed
trade.

[Bitcoin Core's testing guide](https://developer.bitcoin.org/examples/testing.html)
describes regtest's controlled block generation. For public network semantics,
use the current [testnet4 specification](https://github.com/bitcoin/bips/blob/master/bip-0094.mediawiki)
or [signet specification](https://github.com/bitcoin/bips/blob/master/bip-0325.mediawiki).
Never report a successful public read-only block query as a wallet transaction,
or a local regtest transaction as a public-testnet transaction. Record the
network, observed block or transaction identifiers, timestamps, and the exact
completed or blocked stage in the test report.
