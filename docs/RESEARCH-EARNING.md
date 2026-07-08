# How a Personal Crypto Quant System Actually Earns — Deep Research Report

_Researched 2026-07. Companion to [`ROADMAP.md`](ROADMAP.md) (which carries the
retail-loss statistics) and [`solution2.md`](../solution2.md). Every major claim
links its source. English body; 中文摘要在最后。_

---

## 0. Executive summary

Four earning modes survive scrutiny at **personal scale** (one operator, retail
capital, no colocation). In order of evidence strength:

| # | Method | Realistic net return | Risk that kills it | Verdict for QT |
| --- | --- | --- | --- | --- |
| 1 | **Funding-rate carry** (spot long + perp short) | 5–20% APR average; episodic spikes to 70%+ APR | funding flips negative; venue failure; margin call on the short leg during pumps | **Build** — core "quasi-risk-free" engine |
| 2 | **Crash/capitulation buying** (multi-factor, infrequent) | a few high-quality entries per cycle; historically strong forward returns from capitulation lows | catching a falling knife in a true regime break | **Built** — keep tuning |
| 3 | **Wick catching** (deep resting limit-buy ladder) | small, steady; pays only in flash events (10–30% intraminute dips happen multiple times/year) | position fills then market keeps falling; no execution guarantee in gaps | **Build** — small sizing, mechanical exits |
| 4 | **Episodic dislocations** (stablecoin depegs, cross-venue spreads in panic) | rare but large (USDC 2023: buy at $0.88, redeem at $1.00) | the depeg is real (UST); capital locked mid-crisis | **Scan & alert** — human confirms, system spots |
| ✗ | Cross-exchange latency arb, triangular arb, HFT market making | — | professionals with colocation take ~all of it; windows < 4 s | **Do not build execution** — scan only |

The honest core finding: **for a personal system, the durable edges are patience
edges, not speed edges.** Carry monetizes other people's leverage demand;
crash/wick buying monetizes other people's forced liquidations; depeg buying
monetizes other people's panic. All three pay the patient side of the trade.
Speed-based arbitrage is a professional's game now — but a personal system can
still *detect* dislocations and alert, because during volatility spikes windows
widen from milliseconds to minutes.

---

## 1. "Risk-free" money: funding carry and basis — the strongest evidence

**Mechanism.** Perpetual futures charge a funding payment (usually every 8 h)
from the crowded side to the other side. Holding spot long + perp short is
price-neutral and *collects* funding when it's positive — which it is most of
the time, because crypto's structural demand is for leveraged longs.

**Evidence.**
- Academic full-sample estimate: the crypto carry trade returns ~8%/yr with very
  low volatility, driven almost entirely by the funding leg ([CMU, *The Crypto
  Carry Trade*](https://www.andrew.cmu.edu/user/azj/files/CarryTrade.v1.0.pdf)).
- A 2025 peer-reviewed study of funding-rate arbitrage on CEX and DEX found
  returns up to 115.9% over six months with max losses under 2% in the studied
  window ([ScienceDirect](https://www.sciencedirect.com/science/article/pii/S2096720925000818)).
- Practitioner estimates: 15–20%+ APR typical; USDT-margined carry gross yield
  ranges 5–70% APR depending on regime ([1Token fund
  primer](https://blog.1token.tech/crypto-fund-101-funding-fee-arbitrage-strategy/));
  practitioner-reported Sharpe/Calmar of funding arb strategies are high (5–10)
  in normal regimes.
- Cross-venue variant: funding differs across Binance/OKX/Bybit (different
  intervals too — 8 h vs 4 h vs 1 h), so long-perp on the negative-funding venue
  + short-perp on the positive-funding venue collects both legs
  ([MDPI, two-tiered funding market structure](https://www.mdpi.com/2227-7390/14/2/346),
  [ArbiSight comparison](https://arbisight.com/blog/okx-bitget-bybit-funding-arbitrage-comparison)).

**What kills it (quantified).**
- Only ~40% of top cross-venue funding spreads (≥20 bps) survive transaction
  costs and spread reversal ([MDPI](https://www.mdpi.com/2227-7390/14/2/346)) —
  **net-of-fees math must gate every entry.**
- Funding flips negative in bear regimes → the trade bleeds; needs an exit rule
  (QT's carry strategy already has `exit_apr` + negative-streak exit).
- The short-perp leg can be margin-called during violent pumps → keep leverage
  ≤ 2x and hold reserve margin; perps track spot much tighter than dated futures
  in crises (perp basis deviates ~3% vs 8–10% for quarterlies —
  [Funding Payments Crisis-Proofed Bitcoin's Perpetuals](https://www.researchgate.net/publication/388019725_Funding_Payments_Crisis-Proofed_Bitcoin's_Perpetual_Futures)).
- Venue/counterparty risk is the tail risk (FTX). Split across venues; never
  100% of capital on one exchange.

**QT implication.** Carry is the closest thing to "risk-free" that's real. The
Intelligence system should compute **net APR after fees per venue** continuously
and alert when it clears a threshold; the existing `BasisCarry` strategy
executes it.

---

## 2. Earning on crashes and wicks

### 2.1 Why crashes over-shoot: liquidation cascades

Crypto flash crashes are mostly **forced-seller events**: leveraged longs get
liquidated, each liquidation pushes price into the next cluster of liquidation
levels, and price drops 10–30% in minutes before recovering as forced selling
exhausts itself ([Bit.com, cascade
mechanics](https://www.bit.com/insights/knowledge-hub/cascade-liquidation)).
Real examples: Oct-2025, −12% in 8 h with ~$19 B liquidated, **87% of it
longs** ([CCN post-mortem](https://www.ccn.com/education/crypto/oct-10-crypto-flash-crash-exchanges-whales-traders-who-lost/));
Feb-2026, −20% cascade to $60k and a V-shaped recovery within hours
([WazirX analysis](https://wazirx.com/blog/bitcoin-liquidation-cascade-june-2026/), [Arkham research](https://info.arkm.com/research/bitcoin-crash-why-they-happen-and-the-effect-they-have)).

The economic logic of buying these: **the seller is not selling on
information — they're selling because a margin engine made them.** Buying from
forced sellers is the classic liquidity-provision premium. Caveat from the same
sources: the first bounce is often short-covering, not new demand, so exits
should be quick (mean-reversion target), not "hold forever."

QT's existing `capitulation` strategy (5-factor N-of-K voting + macro veto) is
the *slow* version of this. The *fast* version is:

### 2.2 Wick catching with a resting limit-buy ladder

Place deep limit buys (e.g. −5%, −8%, −12% below spot, refreshed periodically);
when a cascade wicks through a rung, you get filled at panic prices; take
profit on the reversion. This is how patient market participants monetize
wicks — the fills only happen *because* someone was forced through your price.

**Honest constraints from the evidence:**
- No execution guarantee: in a gapping market, price can blow through a level
  with no liquidity and your order may not fill, or fills only partially
  ([B2Prime flash-crash order behavior](https://b2prime.com/news/flash-crash-trading-guide),
  [Kraken on limit orders](https://www.kraken.com/learn/what-is-a-stop-limit-order)).
- Avoid round numbers — they're order-cluster magnets and produce worse fills
  ([same sources](https://b2prime.com/news/flash-crash-trading-guide)).
- The real risk is not missing the fill — it's **filling and the market keeps
  going down** (a real regime break, not a wick). So: small per-rung size, a
  hard stop below the ladder, and the macro-veto from the capitulation engine
  as a filter.
- Related evidence class: grid bots monetize the same volatility-harvesting
  premium in ranges, and the documented failure mode is identical — a trending
  breakout leaves you holding the bag; fees eat tight grids
  ([Bitsgap grid mechanics & risks](https://bitsgap.com/blog/grid-trading-strategy-explained-how-to-profit-in-any-market-in-2026),
  [Zignaly guide](https://zignaly.com/crypto-trading/algorithmic-strategies/grid-trading)).

**QT implication.** Build `wick_catcher` as a strategy: ladder config in YAML,
per-rung sizing via RiskEngine, take-profit + time-stop + ladder-bottom stop,
and gate the whole ladder on the macro veto. Backtest on synthetic (which
injects crash cycles) and on real 1m data once fetched.

---

## 3. Arbitrage: what a personal system should and shouldn't attempt

### 3.1 The professional-dominated part (don't compete on speed)

- Cross-exchange spot arb windows on majors last **under ~4 seconds**; retail
  execution costs are systematically underestimated; meaningful capital
  (~$100k+) is needed for it to matter after fees; cross-exchange price variance
  is down ~78% since 2020 ([CoinAPI 2025 overview](https://www.coinapi.io/blog/crypto-arbitrage-explained-coinapi-profit-opportunities-2025), [dCentraLab guide](https://www.dcentralab.com/blog/what-is-crypto-arbitrage-trading-a-traders-guide-for-2025)).
- CEX↔DEX arbitrage: 19 months of on-chain data shows $233.8M extracted across
  7.2M arbs — but **3 searchers captured ~75% of it**, and profitability is
  tied to privileged integration with block builders (MEV infrastructure), not
  trading skill ([arXiv, *The Darkest of the MEV Dark
  Forest*](https://arxiv.org/html/2507.13023v1)).
- Single-exchange triangular loops average 0.05–0.15% per cycle before
  costs and are bot-dominated ([TradeAlgo](https://www.tradealgo.com/trading-guides/crypto/crypto-arbitrage-how-to-profit-from-price-differences-across-exchanges); academic case studies show episodic
  monthly returns in volatile windows but flag execution risk and liquidity
  limits — [KSE thesis](https://kse.ua/wp-content/uploads/2025/09/Vadym_Pakholchuk_ARBITRAGE-OPPORTUNITIES.pdf)).

**Conclusion: QT should not build speed-race execution.** The capital,
colocation, and MEV plumbing aren't available to a personal operator.

### 3.2 The part that IS accessible: episodic dislocations + detection

During panics, spreads that are normally milliseconds wide stay open for
**minutes to days** because everyone's risk systems pull back at once:

- **USDC depeg, March 2023**: traded to $0.87–0.88 while remaining fully
  redeemable at $1.00 once banking resumed; one wallet documented +$16.5M; the
  peg restored within ~2 days after the Fed backstop
  ([CoinDesk on-chain analysis](https://www.coindesk.com/business/2023/03/17/on-chain-data-reveals-how-trading-firms-worked-the-usdc-stablecoin-repeg),
  [Cybrid post-mortem](https://cybrid.xyz/blog/2023-usdc-depeg-explained)).
  The catch: you had to distinguish USDC (backed, temporary bank-access issue)
  from UST (structurally broken, went to zero) — that's an *information* edge a
  human + checklist can have, not a speed edge.
- **Chainalysis 2025**: ≥0.5% cross-exchange discrepancies still occur thousands
  of times daily — mostly un-capturable at speed, but the *fat tail* of them
  (during cascades) persists long enough for a scanner + human confirmation
  ([via CoinAPI](https://www.coinapi.io/blog/crypto-arbitrage-explained-coinapi-profit-opportunities-2025)).
- Funding-rate differentials across venues (§1) are the slowest-moving
  "arbitrage" of all — the position is held for hours/days, so retail speed is
  sufficient ([1Token](https://blog.1token.tech/crypto-fund-101-funding-fee-arbitrage-strategy/)).

**Conclusion: build an Intelligence Discovery system that scans, ranks
net-of-fees, and alerts — with execution left to the slow strategies (carry)
or to the human (depegs).** This is exactly the asymmetry a personal system
can win: 24/7 machine vigilance + human judgment on rare fat opportunities.

---

## 4. Robustness: making the system trustworthy

- **Backtest overfitting is the #1 silent killer.** Three strategy variations
  are enough to produce a fake-significant backtest; out-of-sample returns
  average ~26% below in-sample ([Bailey & López de Prado, *Deflated Sharpe
  Ratio*](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2460551),
  [McLean & Pontiff via portfolio-optimization text](https://portfoliooptimizationbook.com/book/8.3-dangers-backtesting.html)).
  QT rules: few parameters, evidence-based defaults, walk-forward re-validation
  (already in `qt.backtest.walkforward`), and the DCA benchmark gate — a
  strategy that can't beat buying gets turned off.
- **Walk-forward is necessary but not sufficient** — combinatorial purged CV
  catches false discoveries better ([ScienceDirect ML-era comparison](https://www.sciencedirect.com/science/article/abs/pii/S0950705124011110)); for QT's low-frequency, low-parameter
  strategies, walk-forward + DSR-style skepticism is proportionate.
- **Fee/slippage realism**: QT sims already charge 12.5 bps/side; funding-arb
  research shows most raw "opportunities" die at the fee line — the intel
  ranker must subtract *both* legs' taker fees + a slippage buffer before
  calling anything an opportunity.
- **Ops**: single-operator safety = the existing stack (durable heartbeats,
  watchdog, KILL file, trade-only keys, caps). Nothing new needed; keep it.

---

## 5. The build plan this report implies

1. **`qt.intel` — Intelligence Discovery** (scan + rank + alert, no execution):
   FundingScanner (per-venue APR + cross-venue differential), SpreadScanner
   (cross-venue spot spread net of 2× taker fees), BasisScanner (spot vs dated
   futures APR), DepegScanner (USDT/USDC/DAI vs $1), WickScanner (live wick
   regime from `qt.indicators.events`). Everything ranked by **net edge in bps
   after fees** with a plain-language "why", persisted for the dashboard
   (`/intel`), alerted through the existing channels.
2. **`wick_catcher` strategy**: ladder of deep limit buys, macro-veto gated,
   per-rung RiskEngine sizing, reversion take-profit, ladder-bottom stop.
   Paper-first like everything else.
3. **Data realism pass**: on a machine with exchange access, `./start.sh fetch`,
   re-run every backtest + `qt report benchmark` on real history; walk-forward
   tune carry thresholds and wick ladder depths; kill what loses to DCA.
4. **Live ladder** (already documented in `docs/live-checklist.md`): carry and
   DCA first (strongest evidence), wick-catcher only after 4 clean paper weeks,
   depeg trades stay human-confirmed.

---

## 中文摘要

**个人量化系统真正能赚钱的四种方式（按证据强度排序）：**

1. **资金费率套利（现货多 + 永续空）** — 最接近"无风险"：学术全样本约 8%/年，
   从业者常态 15–20% APR，行情火热时可达 70% APR。真正的风险：费率转负要止损、
   交易所倒闭要分仓、暴涨时空头腿爆仓要低杠杆。跨所费率差（Binance/OKX/Bybit
   结算周期不同）是零售速度就够用的"慢套利"。
2. **暴跌抄底（已有 capitulation 策略）** — 清算瀑布是强制卖盘，不是信息卖盘；
   从强制卖家手里接货赚的是流动性溢价。注意第一波反弹常是空头回补，出场要快。
3. **插针捕捉（新 wick_catcher 策略）** — 在 -5%/-8%/-12% 挂深度限价买单等瀑布
   插针成交。诚实的约束：极端行情可能不成交或部分成交；真正的风险是成交后继续
   跌（趋势破位而非插针），所以每档小仓位 + 阶梯底部硬止损 + 宏观否决过滤。
4. **事件性错位（稳定币脱锚等）** — 2023 年 3 月 USDC 跌到 $0.88 而赎回价值仍是
   $1，有钱包赚了 1650 万美元，两天回锚。这类机会靠**信息判断**（USDC≠UST）而
   不是速度，适合"系统扫描 + 人工确认"。

**明确不做的**：跨所高频价差、三角套利、CEX-DEX MEV——窗口 <4 秒、75% 利润被
3 家专业机构拿走、需要区块构建者关系，个人玩家没有牌桌。

**稳健性铁律**：三次参数尝试就能造出假阳性回测；样本外收益平均比回测低 26%。
所以：参数要少、walk-forward 复验、打不赢纯定投的策略直接关。

**接下来构建**：`qt.intel` 情报发现系统（扫描+净费率排名+推送，不自动执行）、
`wick_catcher` 插针策略、真实数据回测校准、然后按 live-checklist 阶梯上实盘。

---

## Source index

Liquidation cascades & crashes: [Bit.com](https://www.bit.com/insights/knowledge-hub/cascade-liquidation) · [CCN Oct-2025 post-mortem](https://www.ccn.com/education/crypto/oct-10-crypto-flash-crash-exchanges-whales-traders-who-lost/) · [WazirX Feb-2026](https://wazirx.com/blog/bitcoin-liquidation-cascade-june-2026/) · [Arkham](https://info.arkm.com/research/bitcoin-crash-why-they-happen-and-the-effect-they-have) · [CryptoSlate](https://cryptoslate.com/bitcoin-sees-another-flash-crash-leading-to-1-52-billion-cascade-in-crypto-liquidations/)
Carry & funding: [CMU Crypto Carry Trade](https://www.andrew.cmu.edu/user/azj/files/CarryTrade.v1.0.pdf) · [ScienceDirect 2025 funding arb](https://www.sciencedirect.com/science/article/pii/S2096720925000818) · [MDPI two-tier funding](https://www.mdpi.com/2227-7390/14/2/346) · [1Token](https://blog.1token.tech/crypto-fund-101-funding-fee-arbitrage-strategy/) · [ArbiSight](https://arbisight.com/blog/okx-bitget-bybit-funding-arbitrage-comparison) · [Perp crisis behavior](https://www.researchgate.net/publication/388019725_Funding_Payments_Crisis-Proofed_Bitcoin's_Perpetual_Futures)
Arbitrage landscape: [CoinAPI 2025](https://www.coinapi.io/blog/crypto-arbitrage-explained-coinapi-profit-opportunities-2025) · [dCentraLab](https://www.dcentralab.com/blog/what-is-crypto-arbitrage-trading-a-traders-guide-for-2025) · [MEV Dark Forest (arXiv)](https://arxiv.org/html/2507.13023v1) · [KSE DEX arb thesis](https://kse.ua/wp-content/uploads/2025/09/Vadym_Pakholchuk_ARBITRAGE-OPPORTUNITIES.pdf) · [TradeAlgo](https://www.tradealgo.com/trading-guides/crypto/crypto-arbitrage-how-to-profit-from-price-differences-across-exchanges)
Depegs: [CoinDesk USDC repeg](https://www.coindesk.com/business/2023/03/17/on-chain-data-reveals-how-trading-firms-worked-the-usdc-stablecoin-repeg) · [Cybrid](https://cybrid.xyz/blog/2023-usdc-depeg-explained) · [Fed note on SVB & stablecoins](https://www.federalreserve.gov/econres/notes/feds-notes/in-the-shadow-of-bank-run-lessons-from-the-silicon-valley-bank-failure-and-its-impact-on-stablecoins-20251217.html)
Wick/limit-order execution: [B2Prime](https://b2prime.com/news/flash-crash-trading-guide) · [Kraken](https://www.kraken.com/learn/what-is-a-stop-limit-order)
Grid/volatility harvesting: [Bitsgap](https://bitsgap.com/blog/grid-trading-strategy-explained-how-to-profit-in-any-market-in-2026) · [Zignaly](https://zignaly.com/crypto-trading/algorithmic-strategies/grid-trading)
Robustness: [Deflated Sharpe Ratio (SSRN)](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2460551) · [ML-era OOS testing (ScienceDirect)](https://www.sciencedirect.com/science/article/abs/pii/S0950705124011110) · [Dangers of backtesting](https://portfoliooptimizationbook.com/book/8.3-dangers-backtesting.html)
