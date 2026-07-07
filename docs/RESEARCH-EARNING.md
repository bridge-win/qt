# Deep Research: Earning Paths for QT

## 中文执行摘要

QT 应该优先押注三类机会：第一是资金费率/基差套利，因为它有最明确的市场结构和学术证据，但必须把交易所风险、保证金挤兑、资金费率反转算进去；第二是崩盘插针捕捉，因为加密市场存在杠杆清算和流动性枯竭造成的短期过度反应，但只能小仓位、限价、先纸盘；第三是稳定币脱锚和跨交易所价差发现，因为机会真实存在但很偶发，零售用户最大风险是转账/提款延迟和手续费吃掉利润。不要把 HFT 延迟套利、主动做市、三角套利当成个人规模的主收益来源。

结论：先把 `carry`、`dca`、`intel` 跑稳，再让 `wick` 纸盘至少 4 周。任何策略如果真实数据 walk-forward 后输给 DCA，就砍掉。

## What Is Verified To Work At Personal Scale?

| Method | Evidence | Realistic Personal-Scale Return | Capacity | Main Failure Modes | QT Action |
| --- | --- | ---: | ---: | --- | --- |
| Funding-rate carry | Perpetual funding exists to pull perp price back toward spot; academic carry work documents very high but volatile crypto carry, sometimes >40% annualized, and a 2025 funding-arb study reports large returns in selected scenarios. Sources: [Crypto Carry](https://pubsonline.informs.org/doi/10.1287/mnsc.2024.05069), [CMU working paper](https://www.andrew.cmu.edu/user/azj/files/CarryTrade.v1.0.pdf), [funding-rate arbitrage study](https://www.sciencedirect.com/science/article/pii/S2096720925000818), [perpetual futures mechanics](https://arxiv.org/html/2212.06888v5). | 8-30% APR in favorable regimes; can be 0 or negative for long periods. | Medium: limited by margin, venue limits, and ability to keep spot/perp balanced. | Funding flips, exchange insolvency, ADL/liquidation, borrow constraints, margin calls during wicks. | Keep `carry`; add `FundingScanner` and cross-venue differential alerts. |
| Cash-and-carry basis | Futures/spot basis can become fat when leverage demand is high; carry is a real premium but volatile and not risk-free. Sources: [Crypto Carry](https://pubsonline.informs.org/doi/10.1287/mnsc.2024.05069), [perpetual futures mechanics](https://arxiv.org/html/2212.06888v5). | 5-20% APR when basis is wide; episodic. | Medium if dated futures are available and collateral is managed. | Basis compression before entry, expiry/settlement mismatch, forced deleveraging, haircut/collateral risk. | Add `BasisScanner`; paper before capital. |
| Crash/wick capture | Bitcoin crash mechanics can be endogenous liquidity spirals; crypto price overreaction papers find post-overreaction patterns; EVT work confirms crypto tail risk is structurally large. Sources: [Donier & Bouchaud](https://arxiv.org/abs/1503.06704), [Caporale & Plastun](https://ideas.repec.org/p/diw/diwwpp/dp1718.html), [Gkillas & Katsiampa](https://www.sciencedirect.com/science/article/abs/pii/S0165176518300284). | Highly variable; target small bounces of 2-6% with low hit frequency. | Low to medium: deep limit orders fill only in stress. | Catching falling knives, exchange downtime, fake isolated wicks, no queue priority, repeated cascades. | Add `wick` paper strategy; size each rung small and cap open rungs. |
| Cross-exchange spot spreads | Historical crypto arbitrage was large, but later research finds exploitable spreads decreased sharply after 2018. Cross-country/fiat-segmented spreads can persist, but moving capital is the bottleneck. Sources: [Arbitrage in Cryptocurrency Markets](https://www.sciencedirect.com/science/article/pii/S1386418123000150), [CFA summary of trading and arbitrage](https://rpc.cfainstitute.org/research/cfa-digest/2020/10/dig-v50-n10-4). | Usually single-digit to tens of bps after fees; spikes during volatility. | Low unless capital is pre-positioned on both venues. | Withdrawal delays, fees, KYC/limits, stale quotes, latency, venue outage. | Use as alert/discovery first via `SpreadScanner`; do not assume executable profit. |
| Stablecoin depegs | USDC/DAI March 2023 showed real dislocations: public analyses report USDC near $0.87 and DAI near $0.85 in the SVB episode. Sources: [Federal Reserve note](https://www.federalreserve.gov/econres/notes/feds-notes/in-the-shadow-of-bank-run-lessons-from-the-silicon-valley-bank-failure-and-its-impact-on-stablecoins-20251217.html), [S&P stablecoin depeg report](https://www.spglobal.com/content/dam/spglobal/corporate/en/images/general/special-editorial/stablecoinsadeepdiveintovaluationanddepegging.pdf). | Rare but can be 30 bps to 10%+ in crisis. | Low to medium; depends on venue balances and redemption access. | Depeg is real solvency risk, not a free discount; redemption windows close; chain congestion. | Add `DepegScanner`; treat as warning plus manual decision. |
| DEX/CEX and triangular arb | DEX/CEX no-arbitrage deviations exist, but gas fees and small-trade fixed costs hurt small accounts. Sources: [CEX vs DEX market quality](https://arxiv.org/html/2112.07386v7), [CCXT unified exchange API](https://docs.ccxt.com/). | Often negative after gas/fees for small size; episodic positive in stress. | Low for personal scale. | MEV, gas spikes, bridge risk, stale pools, failed tx. | Track later; not Phase 5 core. |

## What Does Not Work For Retail

- HFT latency arbitrage is not a realistic QT target. Research on crypto arbitrage shows opportunities have compressed, and execution depends on speed, fee tier, and pre-positioned balances ([ScienceDirect arbitrage study](https://www.sciencedirect.com/science/article/pii/S1386418123000150)).
- Market making against professional flow is not a retail edge. Queue position, adverse selection, fee tiers, and inventory risk dominate.
- "Risk-free" carry is not risk-free. Perpetual funding is a transfer mechanism that can reverse, and the hedge can break under margin stress ([perpetual futures mechanics](https://arxiv.org/html/2212.06888v5)).
- Stablecoin depeg buying is not equivalent to buying $1 for $0.90. The discount prices solvency, banking, redemption, and liquidity risk ([Federal Reserve note](https://www.federalreserve.gov/econres/notes/feds-notes/in-the-shadow-of-bank-run-lessons-from-the-silicon-valley-bank-failure-and-its-impact-on-stablecoins-20251217.html)).

## System Robustness Requirements

1. Walk-forward before capital. Use anchored and rolling splits; never tune on OOS. Bailey et al. show that repeated backtests create false discoveries, so QT should rank configs by OOS stability, not best headline Sharpe ([Probability of Backtest Overfitting](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2326253)).
2. Fees and slippage must be pessimistic. Spread and basis scanners should report net-of-fees edge; wick backtests should assume taker-like cost even for limit fills until real fill logs prove maker execution.
3. Capital must be pre-positioned only after paper validation. Cross-exchange "arb" is mostly inventory management; transfer latency can erase the trade before funds arrive ([CFA summary](https://rpc.cfainstitute.org/research/cfa-digest/2020/10/dig-v50-n10-4)).
4. Kill switches need to remain boring: max drawdown, stale heartbeat, missing scanner data, exchange API errors, and no-data reconnect should all block new risk.
5. API keys must be least-privilege. Disable withdrawals, use IP restrictions where available, rotate exposed keys, and keep secrets out of git; see [OWASP Secrets Management](https://cheatsheetseries.owasp.org/cheatsheets/Secrets_Management_Cheat_Sheet.html) and [Binance API key security](https://www.binance.com/en/academy/articles/what-are-api-keys-and-security-types).

## Build Implications For QT

- Phase 5 should ship `qt.intel` and `wick` in paper mode only.
- Phase 6 should fetch real history locally, run all strategy backtests, then walk-forward tune `carry`, `capitulation`, and `wick`; any strategy that loses to DCA should be disabled.
- Phase 7 should graduate only the highest-evidence flows: carry and DCA first, wick only after 4 clean paper weeks with real fills and no exchange downtime surprises.
