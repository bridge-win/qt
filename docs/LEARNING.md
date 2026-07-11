# Learn Quant — A Sound Path from Zero to Systematic Trading

> A structured, citation-backed curriculum for building **quantitative and
> financial knowledge** from the ground up, using this repository as a live
> laboratory. The dashboard renders a summarized version of this document at
> `http://127.0.0.1:8765/learn`; this file is the detailed companion.

The goal of this guide is not to hand you signals — it is to build the
*knowledge architecture* underneath them, so that every indicator in
[`docs/indicators.md`](indicators.md) and every threshold in
[`docs/strategy.md`](strategy.md) reads as an obvious consequence of theory
rather than a magic number.

---

## How to read this guide

Quant finance is a **stack of dependent layers**. You cannot reason about a
funding-rate carry trade without probability; you cannot judge a backtest
without understanding overfitting; you cannot size a position without the
Kelly criterion and its failure modes. Learn bottom-up, but *touch the top
early and often* — build a toy strategy in week one so the theory has
something to attach to.

Three rules that separate people who learn quant from people who read about it:

1. **Learn by building, then reading, then rebuilding.** Reproduce a result
   from a paper before you trust it. This repo exists so you can. Every module
   below points to the code that demonstrates it.
2. **Keep a research journal.** Every hypothesis, every backtest, every reason
   you rejected an idea. Marcos López de Prado calls the absence of this the
   single biggest cause of backtest overfitting [1].
3. **Assume you are fooling yourself.** The base rate for "discovered" alphas
   surviving out-of-sample is brutal. Harvey, Liu & Zhu [11] show most
   published factors fail a proper multiple-testing bar. Internalize this
   before you risk a dollar.

---

## The knowledge architecture (9 layers)

```
        ┌─────────────────────────────────────────────┐
  L9    │  Behavioral finance & practitioner wisdom     │
        ├─────────────────────────────────────────────┤
  L8    │  Execution & market microstructure            │
        ├─────────────────────────────────────────────┤
  L7    │  Backtesting & validation (anti-overfitting)  │
        ├─────────────────────────────────────────────┤
  L6    │  Portfolio construction & risk management     │
        ├─────────────────────────────────────────────┤
  L5    │  Signal / factor / alpha construction         │
        ├─────────────────────────────────────────────┤
  L4    │  Time-series analysis & econometrics          │
        ├─────────────────────────────────────────────┤
  L3    │  Financial markets & instruments              │
        ├─────────────────────────────────────────────┤
  L2    │  Programming & data engineering               │
        ├─────────────────────────────────────────────┤
  L1    │  Mathematics & statistics foundation          │
        └─────────────────────────────────────────────┘
  L0    Mindset & meta-learning  (wraps all of the above)
```

Each layer below lists: **what it is**, **why it matters**, the **core
concepts** to master, **where to learn** (with citations), **how to learn**
(deliberate practice), and the **code in this repo** that makes it concrete.

---

### L0 — Mindset & meta-learning

**Why:** More aspiring quants fail on process than on math. The market is an
adversarial, non-stationary, low-signal environment; the habits that work in
a classroom (memorize, get the "right" answer) actively harm you here.

**Core concepts**
- Deliberate practice: work at the edge of your ability with fast feedback.
- Falsification over confirmation: try to *kill* your idea, not prove it.
- Process vs. outcome: a good decision can lose; a bad one can win. Judge the
  process (Annie Duke, *Thinking in Bets* [15]).
- Position sizing beats prediction: survival first, edge second.

**Where to learn**
- Nassim Taleb, *Fooled by Randomness* and *The Black Swan* [13] — why most
  track records are noise, and why tails dominate in crypto.
- Daniel Kahneman, *Thinking, Fast and Slow* [14] — the cognitive biases you
  will personally exhibit while trading.
- James Clear, *Atomic Habits* / Cal Newport, *Deep Work* — the study habits.

**How to learn:** Start a dated research journal today (a plain markdown file).
Every idea gets a hypothesis, a test, and a verdict. Re-read it monthly.

**In this repo:** [`docs/RESEARCH-EARNING.md`](RESEARCH-EARNING.md) is a worked
example of this mindset — it documents *what actually earns at personal scale
and what doesn't*, with the losing cases kept in.

---

### L1 — Mathematics & statistics foundation

**Why:** Every model is applied probability. If you cannot derive why a
Z-score of −2 is a ~2.3% left-tail event, you cannot read the funding-rate
signal in [`src/qt/indicators/derivatives.py`](../src/qt/indicators/derivatives.py).

**Core concepts**
- Probability: distributions, conditional probability, Bayes, expectation,
  variance, covariance, the CLT and *why it fails for fat tails*.
- Statistics: estimators, bias/variance, confidence intervals, hypothesis
  testing, p-values and their abuse, multiple-testing correction.
- Linear algebra: vectors, matrices, eigenvectors (→ PCA, covariance),
  projections (→ regression, factor models).
- Calculus & optimization: gradients, Lagrange multipliers (→ mean-variance),
  convexity.
- Stochastic processes: random walks, Brownian motion, Itô's lemma (needed the
  moment you touch options — [`src/qt/indicators/options.py`](../src/qt/indicators/options.py)).

**Where to learn**
- Larry Wasserman, *All of Statistics* [2] — the fastest rigorous on-ramp.
- Sheldon Ross, *A First Course in Probability* [3] — the standard.
- MIT OCW **18.05 Introduction to Probability and Statistics** (free) [16].
- 3Blue1Brown, *Essence of Linear Algebra* & *Essence of Calculus* (YouTube) —
  build intuition before formalism.
- Josh Starmer, *StatQuest* (YouTube) — stats/ML concepts, unusually clear.
- Steven Shreve, *Stochastic Calculus for Finance I & II* [4] — when you reach
  options/vol.

**How to learn:** Do problems, not just readings. Re-derive the Z-score, the
Sharpe ratio, and OLS from scratch on paper. Then compute each in numpy and
check they match.

**In this repo:** the Z-score machinery in
[`src/qt/indicators/`](../src/qt/indicators/) and the Sharpe/drawdown math in
[`src/qt/backtest/metrics.py`](../src/qt/backtest/metrics.py).

---

### L2 — Programming & data engineering

**Why:** Alpha decays; infrastructure compounds. Most of a quant's time is
data plumbing — cleaning, aligning timestamps, avoiding look-ahead bias.

**Core concepts**
- Python fluency: `numpy`, `pandas`, vectorization, and *why loops lie about
  performance*.
- Data hygiene: survivorship bias, look-ahead bias, point-in-time data,
  timezone/timestamp alignment, corporate actions (splits) / for crypto,
  exchange outages and bad prints.
- Reproducibility: deterministic pipelines, seeds, storing raw data, replayable
  backtests (this repo uses a `ParquetStore` for exactly this).
- Engineering: version control, testing, typing, logging.

**Where to learn**
- MIT OCW **6.0001 Introduction to Computer Science and Programming in
  Python** (free) [17].
- Wes McKinney, *Python for Data Analysis* [5] — by the author of pandas.
- Robert Martin, *Clean Code* — for turning research scripts into systems.
- The QuantConnect / Lean docs and Boot Camp [19] for an end-to-end engine.

**How to learn:** Rebuild one indicator from
[`docs/indicators.md`](indicators.md) yourself in a notebook, then diff your
output against this repo's implementation. Bugs you find are the lesson.

**In this repo:** [`src/qt/data/`](../src/qt/data/) (ingestion adapters +
`ParquetStore` for replay-deterministic backtests) and the typed, tested,
logged structure under [`src/qt/core/`](../src/qt/core/).

---

### L3 — Financial markets & instruments

**Why:** You must know *what you are trading* before *how*. A funding rate is
meaningless until you understand perpetual swaps; a basis trade is meaningless
until you understand futures.

**Core concepts**
- Market structure: exchanges, order books, makers/takers, fees, spot vs.
  derivatives.
- Instruments: spot, futures, perpetual swaps (funding mechanism!), options
  (the Greeks), the difference between them.
- Crypto-specific: on-chain data as a new fundamental (MVRV, SOPR, NUPL),
  liquidations and cascades, stablecoin pegs, 24/7 markets, custody risk.
- Return math: log vs. simple returns, compounding, annualization.

**Where to learn**
- John Hull, *Options, Futures, and Other Derivatives* [6] — the derivatives
  reference; read the chapters on futures, forwards, and options.
- Columbia's *Financial Engineering and Risk Management* on Coursera
  (Haugh & Iyengar) [18].
- Glassnode Academy & CryptoQuant's guides — for the on-chain metrics this
  strategy actually uses.
- MIT OCW **15.401 Finance Theory I** (Andrew Lo) [16] for the fundamentals.

**How to learn:** Open a testnet/paper account. Place (fake) maker and taker
orders. Watch a funding payment settle. Read a liquidation cascade on a chart
next to the order book. Abstractions become concrete fast.

**In this repo:** the instrument-specific scanners in
[`src/qt/intel/scanners.py`](../src/qt/intel/scanners.py) (funding, spread,
basis, depeg) and their rationale in
[`docs/RESEARCH-EARNING.md`](RESEARCH-EARNING.md).

---

### L4 — Time-series analysis & econometrics

**Why:** Prices are time series with fat tails, volatility clustering, and
regime changes. Standard i.i.d. statistics quietly break; you need the tools
built for dependence.

**Core concepts**
- Stationarity, autocorrelation, unit roots, cointegration (→ pairs / basis).
- ARIMA and, more importantly for crypto, **GARCH** volatility modeling and
  regime-switching (this strategy's volatility group rests on it).
- Realized vs. implied volatility; the volatility-of-volatility.
- Extreme Value Theory (EVT) — the correct lens for capitulation tails.
- The dangers: spurious regression, data-mined cointegration, non-stationarity.

**Where to learn**
- Ruey Tsay, *Analysis of Financial Time Series* [7] — the standard graduate
  text; strong on GARCH.
- Rob Hyndman & George Athanasopoulos, *Forecasting: Principles and Practice*
  (free online) [20].
- The papers this strategy cites: Ardia et al. (2019) on Markov-switching
  GARCH for BTC, and Gkillas & Katsiampa (2018) on EVT for crypto tails — see
  [`docs/strategy.md`](strategy.md).

**How to learn:** Fit a GARCH(1,1) to BTC returns; plot the conditional
volatility against realized 30-day vol. Then look at how this repo's
short/long realized-vol *ratio* approximates the same regime signal cheaply.

**In this repo:**
[`src/qt/indicators/volatility.py`](../src/qt/indicators/volatility.py) and the
regime logic in [`src/qt/indicators/regime.py`](../src/qt/indicators/regime.py)
and [`src/qt/signal/multiframe.py`](../src/qt/signal/multiframe.py).

---

### L5 — Signal / factor / alpha construction

**Why:** This is where an *edge* is supposed to live. It is also where people
fool themselves most. The discipline is turning a noisy economic idea into a
falsifiable, testable factor — and combining weak factors intelligently.

**Core concepts**
- Factor investing: value, momentum, carry, low-vol — and their crypto
  analogues (Fama–French [10] is the intellectual template).
- Signal combination: weighted Z-scores vs. **N-of-K boolean voting** (this
  repo deliberately chooses the latter — read *why* in
  [`docs/strategy.md`](strategy.md)).
- The Fundamental Law of Active Management: `IR ≈ IC · √breadth`
  (Grinold & Kahn [8]) — why breadth and independence of bets matter as much
  as accuracy.
- Feature engineering without leakage; meta-labeling (López de Prado [1]).

**Where to learn**
- Marcos López de Prado, *Advances in Financial Machine Learning* [1] — the
  modern bible for ML-driven signals *and* the traps.
- Grinold & Kahn, *Active Portfolio Management* [8] — the theory of turning
  forecasts into portfolios.
- Rishi Narang, *Inside the Black Box* [9] — a plain-language tour of how real
  quant shops are structured.
- The **Quantopian Lecture Series** (archived on GitHub) — free, notebook-based,
  and excellent on factor research and pitfalls.

**How to learn:** Take a single factor (e.g. funding-rate Z-score). Define it,
test its predictive power (IC) honestly with no peeking, and only then think
about combining it. Resist adding a second factor until the first is
understood.

**In this repo:** the composite scorer in
[`src/qt/indicators/composite.py`](../src/qt/indicators/composite.py) and the
`SignalEngine` in [`src/qt/signal/engine.py`](../src/qt/signal/engine.py),
which turn factor votes into sparse signals.

---

### L6 — Portfolio construction & risk management

**Why:** You survive on risk management, not on being right. Position sizing
and drawdown control are the difference between a strategy that compounds and
one that blows up on the first fat tail.

**Core concepts**
- Modern Portfolio Theory & the efficient frontier (Markowitz [12]); CAPM
  (Sharpe) and its limits.
- The **Kelly criterion** [21] and why practitioners use *fractional* Kelly —
  full Kelly is too aggressive under parameter uncertainty.
- Volatility targeting, risk parity, correlation and diversification (and how
  correlations → 1 in a crisis).
- Drawdown, Value-at-Risk / Expected Shortfall, and their weaknesses; the
  kill-switch as a hard floor.

**Where to learn**
- Grinold & Kahn [8] (portfolio side); Hull [6] (VaR chapters).
- Ed Thorp's writing and Kelly's original paper [21] — sizing done right.
- Antti Ilmanen, *Expected Returns* — a superb survey of what actually drives
  asset returns across the board.

**How to learn:** Simulate the *same* edge at full Kelly, half Kelly, and
quarter Kelly across many random return paths; watch the median vs. the ruin
probability. The lesson is visceral.

**In this repo:** [`src/qt/risk/sizing.py`](../src/qt/risk/sizing.py)
(fractional Kelly + vol targeting), [`src/qt/risk/stops.py`](../src/qt/risk/stops.py)
(ATR stops), and the 20% drawdown kill-switch in
[`src/qt/risk/engine.py`](../src/qt/risk/engine.py).

---

### L7 — Backtesting & validation (the anti-overfitting layer)

**Why:** A backtest is a hypothesis test that is trivially easy to rig — usually
by accident. This layer is what separates a research toy from something you'd
risk money on. Most published strategies are overfit noise.

**Core concepts**
- Look-ahead bias, survivorship bias, in-sample overfitting, p-hacking by
  parameter search.
- **Walk-forward analysis** and out-of-sample discipline.
- The **Deflated Sharpe Ratio** and the effect of the *number of trials* on
  the significance of a backtest (Bailey & López de Prado [22]).
- Multiple-testing correction: with enough tries, everything looks
  significant (Harvey, Liu & Zhu [11] — demand t-stats near 3, not 2).
- Monte Carlo / bootstrap of the equity curve to see the distribution of
  outcomes, not one lucky path.
- Transaction costs, slippage, and capacity — the alpha that dies at the fee
  line.

**Where to learn**
- López de Prado [1], chapters on backtesting and the "Seven Sins".
- Bailey, Borwein, López de Prado & Zhu, *Pseudo-Mathematics and Financial
  Charlatanism* (Notices of the AMS, 2014) [23] — read this before you trust
  any backtest, including your own.
- Ernest Chan, *Quantitative Trading* [24] — pragmatic backtesting for the
  independent trader.

**How to learn:** Deliberately overfit a strategy (search 1,000 parameter
combos, pick the best). Then watch it die out-of-sample. Do it *once* on
purpose so you recognize it forever.

**In this repo:** [`src/qt/backtest/`](../src/qt/backtest/) —
`walkforward.py`, `montecarlo.py`, `metrics.py`, and the event-driven engine.
The roadmap in [`docs/ROADMAP.md`](ROADMAP.md) makes passing walk-forward and
Monte Carlo a *precondition* for live capital.

---

### L8 — Execution & market microstructure

**Why:** The gap between backtest and reality is mostly execution: spread,
slippage, fees, latency, and market impact. A strategy can be right about
direction and still lose to costs.

**Core concepts**
- Order book mechanics, market vs. limit orders, maker/taker fee tiers.
- Slippage and market impact; the Almgren–Chriss optimal-execution frame [25].
- Adverse selection and the informational content of order flow (Kyle [26]).
- Why the same signal earns at small size and vanishes at large size
  (capacity) — the intel scanner ranks by *net edge after fees* for this
  reason.

**Where to learn**
- Maureen O'Hara, *Market Microstructure Theory* — the academic foundation.
- Almgren & Chriss (2000) [25] and Kyle (1985) [26] — the two canonical papers.
- Larry Harris, *Trading and Exchanges* — how markets actually work,
  practitioner-level.

**How to learn:** Model your own fees and slippage explicitly and re-run a
"winning" backtest with realistic costs. Watch the Sharpe fall. That delta is
the microstructure lesson.

**In this repo:** the `FillModel` in
[`src/qt/backtest/fills.py`](../src/qt/backtest/fills.py), the fee-aware
ranking in [`src/qt/intel/ranker.py`](../src/qt/intel/ranker.py), and the
deep-limit-ladder logic in
[`src/qt/strategies/wick_catcher.py`](../src/qt/strategies/wick_catcher.py).

---

### L9 — Behavioral finance & practitioner wisdom

**Why:** Edges often exist *because* other participants behave predictably
badly (panic-selling into capitulation is this whole strategy's thesis). And
your own psychology is the last, hardest risk to manage.

**Core concepts**
- Behavioral biases: overconfidence, recency, loss aversion, herding,
  disposition effect — both in the market and in yourself.
- Why capitulation / forced-liquidation events create the mean-reversion this
  strategy targets (the crowd is the counterparty).
- Discipline, sizing under uncertainty, and knowing when *not* to trade.
- Reflexivity and non-stationarity: edges decay as they are discovered.

**Where to learn**
- Kahneman [14], *Thinking, Fast and Slow*; Taleb [13].
- Robert Shiller, *Irrational Exuberance* — bubbles and sentiment.
- Annie Duke, *Thinking in Bets* [15] — decision-making under uncertainty.

**How to learn:** Journal your emotional state next to each (paper) trade.
Correlate your worst decisions with your emotional peaks. The pattern is the
lesson — and it is why systematic, rules-based trading exists.

**In this repo:** the entire long-only, capitulation-only thesis in
[`docs/strategy.md`](strategy.md) is a bet on *other people's* behavioral
biases; the rules-based design removes *yours* from the loop.

---

## A concrete 6-month study plan

You don't climb the layers strictly in order — you spiral through them, going
deeper each pass. A realistic part-time (~10 hrs/week) plan:

| Month | Focus | Deliverable |
|---|---|---|
| 1 | L0–L1: mindset + probability/stats; Python setup | Reproduce the Sharpe & Z-score math in numpy; start a research journal |
| 2 | L2–L3: pandas + markets/instruments | Ingest BTC OHLCV; rebuild one indicator from `docs/indicators.md` |
| 3 | L4: time series + volatility | Fit GARCH; replicate the vol-ratio regime signal |
| 4 | L5: single-factor research | Test one factor's IC honestly, out-of-sample |
| 5 | L6–L7: sizing + backtesting/validation | Run this repo's walk-forward + Monte Carlo; overfit-on-purpose once |
| 6 | L8–L9: execution + review | Add realistic costs; write an honest post-mortem of your strategy |

Then **repeat the spiral** at greater depth. Competence in quant is measured in
years and reproduced results, not weeks and read books.

---

## Curated resources at a glance

| Area | Best starting point | Type |
|---|---|---|
| Probability/Stats | Wasserman, *All of Statistics* [2]; MIT OCW 18.05 [16] | Book / free course |
| Linear algebra intuition | 3Blue1Brown, *Essence of Linear Algebra* | Free video |
| Python for data | McKinney, *Python for Data Analysis* [5]; MIT OCW 6.0001 [17] | Book / free course |
| Derivatives & markets | Hull, *Options, Futures & Other Derivatives* [6] | Book |
| Financial engineering | Columbia FE&RM on Coursera [18] | Free course (audit) |
| Time series | Tsay [7]; Hyndman *FPP* [20] | Book / free book |
| ML for finance | López de Prado, *Advances in Financial ML* [1] | Book |
| Portfolio & alpha | Grinold & Kahn [8]; Narang [9] | Book |
| Backtesting reality | Bailey et al., *Pseudo-Mathematics* [23]; Chan [24] | Paper / book |
| Hands-on engine | QuantConnect Boot Camp [19]; Quantopian Lectures | Free |
| Mindset | Taleb [13]; Kahneman [14]; Duke [15] | Book |
| Community | Quantitative Finance Stack Exchange; arXiv q-fin; SSRN | Free |

> A caution on communities: forums like r/algotrading contain both gems and
> confident nonsense. Trust papers with reproducible methods and your own
> out-of-sample tests over anyone's screenshot of an equity curve.

---

## References

Full citations. Repo-internal cross-references appear inline above.

1. López de Prado, M. (2018). *Advances in Financial Machine Learning*. Wiley.
2. Wasserman, L. (2004). *All of Statistics: A Concise Course in Statistical
   Inference*. Springer.
3. Ross, S. (2018). *A First Course in Probability* (10th ed.). Pearson.
4. Shreve, S. (2004). *Stochastic Calculus for Finance I & II*. Springer.
5. McKinney, W. (2022). *Python for Data Analysis* (3rd ed.). O'Reilly.
6. Hull, J. (2021). *Options, Futures, and Other Derivatives* (11th ed.).
   Pearson.
7. Tsay, R. (2010). *Analysis of Financial Time Series* (3rd ed.). Wiley.
8. Grinold, R. & Kahn, R. (1999). *Active Portfolio Management* (2nd ed.).
   McGraw-Hill. (Fundamental Law of Active Management: IR ≈ IC·√breadth.)
9. Narang, R. (2013). *Inside the Black Box: A Simple Guide to Quantitative
   and High-Frequency Trading* (2nd ed.). Wiley.
10. Fama, E. & French, K. (1993). "Common Risk Factors in the Returns on Stocks
    and Bonds." *Journal of Financial Economics*, 33(1). (And the 2015
    five-factor extension.)
11. Harvey, C., Liu, Y. & Zhu, H. (2016). "…and the Cross-Section of Expected
    Returns." *Review of Financial Studies*, 29(1). (Multiple-testing bar for
    factors.)
12. Markowitz, H. (1952). "Portfolio Selection." *Journal of Finance*, 7(1).
13. Taleb, N. N. (2001/2007). *Fooled by Randomness* / *The Black Swan*.
    Random House.
14. Kahneman, D. (2011). *Thinking, Fast and Slow*. Farrar, Straus & Giroux.
15. Duke, A. (2018). *Thinking in Bets*. Portfolio.
16. MIT OpenCourseWare — 18.05 *Introduction to Probability and Statistics* and
    15.401 *Finance Theory I* (A. Lo). https://ocw.mit.edu
17. MIT OpenCourseWare — 6.0001 *Introduction to CS and Programming in Python*.
    https://ocw.mit.edu
18. Haugh, M. & Iyengar, G. — *Financial Engineering and Risk Management*,
    Columbia University (Coursera).
19. QuantConnect — Boot Camp & Lean engine documentation.
    https://www.quantconnect.com
20. Hyndman, R. & Athanasopoulos, G. (2021). *Forecasting: Principles and
    Practice* (3rd ed.). OTexts (free online). https://otexts.com/fpp3/
21. Kelly, J. L. (1956). "A New Interpretation of Information Rate." *Bell
    System Technical Journal*. (See also Thorp on fractional Kelly.)
22. Bailey, D. & López de Prado, M. (2014). "The Deflated Sharpe Ratio:
    Correcting for Selection Bias, Backtest Overfitting and Non-Normality."
    *Journal of Portfolio Management*.
23. Bailey, D., Borwein, J., López de Prado, M. & Zhu, Q. (2014).
    "Pseudo-Mathematics and Financial Charlatanism: The Effects of Backtest
    Overfitting on Out-of-Sample Performance." *Notices of the AMS*, 61(5).
24. Chan, E. (2009/2013). *Quantitative Trading* / *Algorithmic Trading*. Wiley.
25. Almgren, R. & Chriss, N. (2000). "Optimal Execution of Portfolio
    Transactions." *Journal of Risk*, 3(2).
26. Kyle, A. (1985). "Continuous Auctions and Insider Trading." *Econometrica*,
    53(6). (See also O'Hara, *Market Microstructure Theory*, and Harris,
    *Trading and Exchanges*.)

---

### Related reading inside this repo

- [`docs/strategy.md`](strategy.md) — the strategy's own empirical citations
  (Caporale et al. 2018, Ardia et al. 2019, Gkillas & Katsiampa 2018, Shu et
  al. 2021) and the N-of-K voting rationale.
- [`docs/indicators.md`](indicators.md) — every indicator, defined.
- [`docs/RESEARCH-EARNING.md`](RESEARCH-EARNING.md) — what actually earns at
  personal scale, with citations and honest negatives.
- [`docs/ROADMAP.md`](ROADMAP.md) — the evidence-based path from signal to safe
  live trading.
- [`docs/architecture.md`](architecture.md) — how the system fits together.
