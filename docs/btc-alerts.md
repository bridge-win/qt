# BTC opportunity alerts on OrbStack

Open the local Workbench's **BTC monitor → BTC 机会提醒** card. The flow is
public data → closed-bar thresholds → frozen strategy evidence → isolated
Codex review → current quote/promotion checks → durable hourly quota →
Telegram. The service has no order API or exchange-account credentials.

## Start and configure

```bash
bash deploy/orbstack.sh up
bash deploy/orbstack.sh alerts-init
bash deploy/orbstack.sh up
bash deploy/orbstack.sh alerts-check
bash deploy/orbstack.sh alerts-mode shadow
```

All local container commands select `--context orbstack`. The six services
are web, api, research-worker, monitor, alerts and reviewer. Existing logins,
data volumes and private fields are preserved. The first `up` adds missing
internal alert settings. `alerts-init` supports hidden credential entry.

Only reviewer receives `CODEX_API_KEY`. Set a supported, explicit
`QT_BTC_ALERTS_CODEX_MODEL`. Only alerts receives Telegram credentials;
monitor no longer receives them in this bundle. A separate internal secret
protects reviewer HTTP requests. No reviewer port is published. A healthy
reviewer HTTP process does not establish model authentication.

Blank external credentials allow shadow monitoring but cannot authorize a
notification. Never place secrets in CLI arguments, chat, reports or Git.

## Verify Telegram

From your logged-in Telegram account, send the expected bot (default
`radarxdotbot`) a private message containing `QT VERIFY ` followed by
at least 16 random letters/digits. Verify the exact challenge within ten minutes:

```bash
bash deploy/orbstack.sh alerts-verify 'QT VERIFY REPLACE_WITH_RANDOM_CHALLENGE'
bash deploy/orbstack.sh alerts-check
bash deploy/orbstack.sh alerts-test
```

Verification checks the bot identity, requires no active webhook and reads
updates without changing their offset. The exact fresh challenge must come
from a private chat whose sender ID matches the configured chat ID. No webhook
or other receiver is altered. Changing token, expected bot or chat invalidates
the binding. Confirm the unique **QT 测试** ID in the actual Telegram client;
an API acknowledgement alone is insufficient.

## Rules and evidence

The rules use completed UTC 15m, 1h and 4h OHLCV, with at least 210 bars per
timeframe. Startup uses REST history; live spot eligibility also requires the
existing monitor's fresh WebSocket quote/trade channels. Grid warmup and
directional research have separate readiness requirements.

| Card | Initial condition |
| --- | --- |
| trend | Close beyond prior 20-bar range; volume ≥1.5× median; EMA20/50 align on 1h and 4h |
| recovery | RSI14 ≤25 and volume ≥2× median coincide in latest three bars; close recovers the broken range low and rises |
| wick | Shadow/body ≥2, shadow/range ≥60%, range/prior ATR14 ≥1.5; close returns inside prior range |
| overheat | Prior RSI14 ≥75, current <70, close below prior three lows and higher timeframes weak |

These are engineering hypotheses, not numerical results from the cited
literature. Cards retain source links, summary hashes, availability metadata,
invalidation conditions and counterexamples. SQLite stores cards and an FTS
index; review retrieval uses explicit strategy IDs.

Spot bearish signals mean reduction of existing BTC. Short research uses
separate OKX BTC-USDT-SWAP quotes, mark and funding evidence. Missing historical
funding coverage blocks short promotion. Projected funding is a conservative
scenario cost, not a forecast of future settlements.

Default reference invalidation/target distances are 1.5×/4× one-hour ATR14.
Costs assume 10 bps fees and 2 bps slippage per side, plus 3 bps buffer.
Required net reward/risk is at least 2. Saving changed parameters creates a
new strategy version in shadow mode. Evidence grades are not win probabilities.

## Review, promotion and delivery

Each Codex invocation has a temporary home, disabled tools/search/plugins,
read-only sandbox, strict JSON schema and a 100-second timeout. Approval must
cite strategy, market and validation evidence. Unknown citations, missing or
opposing evidence, model failure and conflicting directions block delivery.

Limits: one batch per strategy-version/bar, at most four model calls per
rolling hour and 96 per rolling day. Authentication probes share the budget.
Input/output hashes, model pin, prompt version, usage and sanitized errors are
retained. The doctor runs at most one probe per rolling hour.

Formal notifications require:

- At least 100 non-overlapping OOS events per strategy/direction, costs,
  purged boundaries and a positive lower 95% weekly-block bootstrap bound.
- At least seven elapsed days and 20 complete forward shadow outcomes.
- Those outcomes must include completed reviews by the same pinned model and
  prompt version; missing reviews and previous model identities do not count.
  The seven-day review clock starts with that model's first completed review.
- An authenticated pinned model, verified recipient and current market data.
- Candidate age <600s; quote age ≤5s; spread ≤10 bps; movement
  ≤min(0.3%, 0.25×one-hour ATR/reference price); current net reward/risk ≥2.

`alerts-research` downloads keyless official spot history and evaluates
fixed rules with a final 180-day held-out period. Snapshots, run artifacts and
hashes remain in the alert volume. Research never enables notifications and
does not claim historical AI forecasting performance.
The command uses a disposable research process/container, separate from the
running alert worker; a worker restart does not interrupt that research job.

```bash
bash deploy/orbstack.sh alerts-research
bash deploy/orbstack.sh alerts-mode notify
bash deploy/orbstack.sh alerts-mode off
bash deploy/orbstack.sh status
bash deploy/orbstack.sh down
```

Notify mode rejects missing operational, research or forward prerequisites.
Off mode pauses reviews/sends. Down keeps volumes.

The recipient's BTC quota covers buys, reductions, short research and test
messages together. A transaction claims the send and hour before network I/O.
Success and uncertain outcomes consume the slot across restarts. Uncertain
sends are never automatically retried. Explicit 429 responses permit bounded
retries, which must reacquire quota and pass expiry checks. Expired opportunities
are never backfilled after cooldown. Bot API cannot guarantee physical phone
arrival timing or exactly-once external delivery.

## Operations and acceptance

SQLite WAL, a unique worker lock and daily consistent backups preserve state.
Seven daily backups live under the alert volume's `backups/`. Stop the
worker and preserve the current ledger before restoring. Never delete the
database to bypass cooldowns.

Workbench authentication covers GET `/api/v3/btc-alerts`,
GET `/api/v3/btc-alerts/events/{id}`,
PUT `/api/v3/btc-alerts/policy`, and
POST `/api/v3/btc-alerts/test-notification`. No route accepts credentials,
recipient overrides or orders.

Keep Mac mini and OrbStack online. Distinguish process liveness, market
readiness, actual model authentication, strategy qualification, Telegram API
acceptance and client receipt. TC remains a separate research host and does
not duplicate this instance's opportunity sender. A startup or fixture test
is not a 24-hour stability acceptance.
