# Quant Learning Center Design Specification

**Status:** Proposed for implementation  
**Date:** 2026-07-11  
**Surface:** QT local dashboard at `/learn`

## 1. Purpose

Add a trustworthy learning center that teaches a beginner how to build the
knowledge required to understand, test, and operate QT. The page must be useful
without promising profitability or presenting strategy folklore as established
fact.

The learning center has three jobs:

1. give the learner a coherent map of financial and quantitative knowledge;
2. provide a staged route through that map, with exercises grounded in QT; and
3. attach every substantive claim or recommended method to a visible source.

It is educational material, not personalized investment advice. The page must
state that clearly and must not recommend leverage, live trading, or a specific
capital allocation.

## 2. Code-review findings

### What is already strong

- QT already contains evidence-rich material in `solution2.md`,
  `docs/strategy.md`, `docs/indicators.md`, and
  `docs/RESEARCH-EARNING.md`.
- The dashboard is deliberately dependency-free and works as server-rendered
  HTML from the standard library.
- Existing tests exercise real HTTP routes and rendered HTML, so `/learn` can
  be added without introducing a browser framework.
- The project already provides concrete laboratories for data quality,
  backtesting, walk-forward analysis, Monte Carlo analysis, portfolio risk,
  paper execution, and benchmark comparison.

### Gaps affecting this feature

- Research is spread across several long documents. There is no beginner entry
  point or recommended order.
- `src/qt/dashboard/server.py` contains routing, data access, HTML, and repeated
  page-level CSS. Adding a long curriculum directly to it would further blur
  responsibilities.
- Existing research mixes peer-reviewed results, vendor explanations, and
  practitioner experience. The learning surface needs visible evidence labels
  so learners understand the difference.
- The dashboard navigation is page-specific and does not expose a stable route
  to educational material.

## 3. Recommended approach

Use a dedicated, statically authored curriculum module and server-render it at
`/learn`.

Two alternatives were rejected:

- **Documentation only:** cheapest, but it preserves the current discovery and
  sequencing problem.
- **Interactive course platform:** progress accounts, quizzes, and persistence
  would add state and product complexity before the curriculum itself is
  validated.

The recommended version is intentionally read-only. It provides a high-quality
learning map now and leaves progress tracking as a later, evidence-based
enhancement.

## 4. Information architecture

The page begins with a compact “start here” summary and then presents eight
modules in prerequisite order.

### Module 1 — Financial system and instruments

**Outcome:** Explain cash flows, compounding, discounting, asset classes,
exchanges, order books, spot, futures, and perpetual swaps.

**Core knowledge:** time value of money; nominal versus real return; simple
versus log return; market, limit, and stop orders; bid-ask spread; leverage,
margin, and liquidation.

**QT exercise:** inspect `qt data sources`, identify what each dataset measures,
and describe which observations are prices, flows, positions, or derived
indicators.

### Module 2 — Probability and statistics

**Outcome:** Reason about uncertainty without treating estimates as facts.

**Core knowledge:** distributions; expectation and variance; covariance and
correlation; conditional probability; sampling error; confidence intervals;
hypothesis tests; multiple testing; fat tails.

**QT exercise:** calculate BTC return distributions, compare normal-model tail
probabilities with observed tails, and explain why a Sharpe estimate is
uncertain.

### Module 3 — Data and time series

**Outcome:** Build a dataset that does not quietly leak future information.

**Core knowledge:** returns and stationarity; autocorrelation; volatility
clustering; regimes; missing observations; timestamp alignment; look-ahead,
survivorship, and selection bias.

**QT exercise:** run the data-source freshness view, inspect OHLCV timestamps,
and identify which signal inputs would be known at a chosen decision time.

### Module 4 — Portfolio and risk architecture

**Outcome:** Translate uncertain signals into survivable positions.

**Core knowledge:** diversification; beta; volatility; drawdown; Value at Risk
and its limitations; expected shortfall; position sizing; risk budgets;
correlation breakdown; ruin and liquidity risk.

**QT exercise:** read `qt.risk`, change one risk limit in a paper-only scenario,
and explain the effect on exposure and maximum loss before running it.

### Module 5 — Strategy research

**Outcome:** Turn an economic hypothesis into a falsifiable trading rule.

**Core knowledge:** economic rationale; signal definition; benchmark and null
hypothesis; transaction costs; parameter sensitivity; in-sample versus
out-of-sample evaluation; capacity and decay.

**QT exercise:** write a one-page hypothesis for DCA, trend, carry, or
capitulation, including a reason it may stop working, before running a backtest.

### Module 6 — Backtesting and validation

**Outcome:** Reject attractive but unreliable historical results.

**Core knowledge:** realistic fills and fees; data snooping; overfitting;
walk-forward validation; purging where labels overlap; Monte Carlo stress;
benchmark comparison; reproducibility.

**QT exercise:** run a synthetic backtest, inspect its artifact, run
walk-forward and Monte Carlo checks, then compare the result with plain DCA.
Synthetic success must be described as a software check, not evidence of an
edge.

### Module 7 — Crypto-specific market structure

**Outcome:** Understand risks that traditional spot-only examples omit.

**Core knowledge:** custody and counterparty risk; exchange fragmentation;
funding rates; basis; liquidations; stablecoin risk; on-chain metrics; 24/7
markets; regime-dependent liquidity.

**QT exercise:** trace one carry or wick signal from source data through fees,
risk gating, paper execution, and portfolio accounting.

### Module 8 — Execution and operating discipline

**Outcome:** Operate a research system without confusing automation with
safety.

**Core knowledge:** slippage; order lifecycle; reconciliation; idempotency;
monitoring; kill switches; paper-to-live gates; incident review; behavioral
bias and trading journals.

**QT exercise:** complete `docs/live-checklist.md`, but remain in paper mode;
review heartbeat, ledger, fees, and benchmark reports for four weeks before
even evaluating live readiness.

## 5. Learning method

The page recommends a repeatable four-step loop for every module:

1. **Learn:** read one primary resource and write a five-sentence summary.
2. **Derive:** reproduce the central equation or concept by hand or in a small
   notebook.
3. **Apply:** complete the linked QT exercise using paper or historical data.
4. **Challenge:** document assumptions, failure modes, and what evidence would
   change the conclusion.

Recommended pacing is 12 weeks at five to seven hours per week:

| Weeks | Focus | Exit evidence |
| --- | --- | --- |
| 1–2 | Modules 1–2 | Explain returns, orders, distributions, and estimation error |
| 3–4 | Module 3 | Produce a leakage-free dataset note |
| 5–6 | Module 4 | Write and defend a paper risk budget |
| 7–8 | Modules 5–6 | Complete a reproducible, cost-aware research report |
| 9–10 | Module 7 | Explain funding, basis, liquidation, and venue risk |
| 11–12 | Module 8 | Complete an operations review and benchmark postmortem |

The page must tell learners not to advance because a backtest looks profitable.
They advance when they can explain assumptions, reproduce the result, identify
failure modes, and compare it with a simple benchmark.

## 6. Evidence and citation policy

Every displayed resource has an evidence type:

- **Foundational research:** peer-reviewed paper or canonical academic text.
- **Official curriculum:** university, regulator, standards body, or recognized
  professional curriculum.
- **Practitioner experience:** a named practitioner's method or operational
  lesson; never presented as universal proof.
- **Project documentation:** the exact QT file or command used for an exercise.

The first release uses a compact source registry with stable identifiers and
direct links. Modules refer to those identifiers, and the bottom of the page
renders the complete bibliography. External links open in a new tab with
`rel="noopener noreferrer"`.

### Initial source set

1. CFA Institute, *Investment Foundations Certificate* — broad official
   curriculum covering markets, instruments, ethics, and investment roles:
   <https://investmentfoundations.cfainstitute.org/>
2. MIT OpenCourseWare, *Introduction to Probability and Statistics* — formal
   probability and estimation foundation:
   <https://ocw.mit.edu/search/?q=Introduction+to+Probability+and+Statistics>
3. MIT OpenCourseWare, *Finance Theory I* — asset valuation and portfolio
   foundations:
   <https://ocw.mit.edu/courses/15-401-finance-theory-i-fall-2008/>
4. Markowitz, “Portfolio Selection,” *The Journal of Finance* 7(1), 1952 —
   expected return and variance portfolio framework:
   <https://doi.org/10.2307/2975974>
5. Sharpe, “Capital Asset Prices,” *The Journal of Finance* 19(3), 1964 —
   equilibrium risk and expected return:
   <https://doi.org/10.2307/2977928>
6. Fama, “Efficient Capital Markets,” *The Journal of Finance* 25(2), 1970 —
   market-efficiency framework and empirical tests:
   <https://doi.org/10.2307/2325486>
7. Lo, “The Adaptive Markets Hypothesis,” *The Journal of Portfolio
   Management* 30(5), 2004 — changing competition and strategy performance:
   <https://doi.org/10.3905/jpm.2004.442611>
8. Bailey et al., “The Probability of Backtest Overfitting,” 2016 — selection
   bias caused by trying many strategy variants:
   <https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf>
9. Harvey, Liu, and Zhu, “… and the Cross-Section of Expected Returns,”
   *Review of Financial Studies* 29(1), 2016 — multiple testing and elevated
   significance thresholds:
   <https://doi.org/10.1093/rfs/hhv059>
10. Bank for International Settlements, *The crypto ecosystem: key elements
    and risks*, 2023 — crypto structure and systemic risk:
    <https://www.bis.org/publ/othp72.htm>
11. U.S. SEC Investor.gov, *Crypto Assets* — investor-facing custody, fraud,
    and volatility risks:
    <https://www.investor.gov/additional-resources/spotlight/crypto-assets>
12. Robert Carver, *Systematic Trading*, Harriman House, 2015 — practitioner
    framework for forecast scaling, diversification, costs, and operational
    discipline:
    <https://www.harriman-house.com/systematictrading>

QT's existing research documents remain supplementary reading. Claims copied
from them must keep their original citations; the page must not silently turn a
secondary summary into a primary source.

## 7. Technical design

### Files and responsibilities

- `src/qt/dashboard/learning.py`
  - immutable `LearningSource` and `LearningModule` dataclasses;
  - the ordered curriculum and source registry;
  - validation that every cited source exists and identifiers are unique;
  - HTML renderer for the complete learning page.
- `src/qt/dashboard/server.py`
  - map `GET /learn` to the learning renderer;
  - add a visible `Learn` link to the home header;
  - no curriculum copy lives in this file.
- `tests/test_dashboard_learning.py`
  - validate curriculum integrity independently of HTML;
  - verify `/learn` returns UTF-8 HTML;
  - verify the eight modules, learning loop, disclaimer, citations, external
    link safety, mobile viewport, and home navigation.
- `docs/ROADMAP.md`
  - record the learning center as delivered only after tests and visual review
    pass.

### Interfaces

```python
@dataclass(frozen=True)
class LearningSource:
    id: str
    title: str
    publisher: str
    year: int | None
    url: str
    evidence_type: str


@dataclass(frozen=True)
class LearningModule:
    number: int
    title: str
    outcome: str
    concepts: tuple[str, ...]
    experience_notes: tuple[str, ...]
    qt_exercise: str
    source_ids: tuple[str, ...]


def validate_curriculum(
    modules: tuple[LearningModule, ...],
    sources: tuple[LearningSource, ...],
) -> None: ...


def render_learning_page() -> str: ...
```

Validation fails fast for duplicate source IDs, duplicate module numbers,
unknown citations, non-HTTPS external URLs, or modules without citations.

### Presentation

**Visual thesis:** a calm field guide rather than a dashboard—a strong numbered
learning path, restrained typography, one teal accent, and generous whitespace.

**Content plan:** start-here summary; knowledge architecture; 12-week route;
eight detailed modules; learning method; source bibliography; final safety
reminder.

**Interaction thesis:** sticky in-page module navigation on wide screens;
native expandable module details; restrained link and focus transitions. Motion
must honor `prefers-reduced-motion` and no JavaScript is required.

The page reuses QT's color language but avoids a grid of decorative cards.
Sections, numbered rails, dividers, and typographic hierarchy carry the page.
It must remain readable at 360 px and keyboard navigable.

## 8. Error handling and security

- Curriculum validation runs before rendering; invalid author data raises a
  clear `ValueError` during tests and development.
- All authored text is HTML-escaped by the renderer.
- Source URLs are fixed project data, validated as HTTPS, and never accepted
  from request parameters.
- The page contains no user input, cookies, analytics, or persistence.
- `/learn` has no automatic refresh because educational reading should not be
  interrupted.

## 9. Acceptance criteria

1. `GET /learn` returns 200 and a responsive, readable page without JavaScript
   or a new dependency.
2. The page exposes the eight-module architecture and 12-week learning route.
3. Every module includes an outcome, detailed concepts, practitioner cautions,
   a QT exercise, and at least one citation.
4. Theory, official curriculum, practitioner experience, and project material
   are visibly differentiated.
5. The page teaches how to learn—not only what to read—through the four-step
   loop and explicit exit evidence.
6. A financial-education disclaimer and paper-first guidance are prominent.
7. The home page has a visible `/learn` link.
8. Curriculum integrity and HTTP rendering are covered by automated tests.
9. Full `pytest`, Ruff, mypy, and `git diff --check` validation passes.
10. A local browser check confirms desktop and 360 px mobile layout, keyboard
    focus, and external-link behavior.

## 10. Future suggestions outside the first release

These are intentionally deferred until the static curriculum is used and
reviewed:

1. Extract shared page chrome and CSS from `server.py` after `/learn` proves the
   right abstraction; do not combine this with the first feature commit.
2. Add optional local progress tracking only if learners need resumption. Store
   it locally and make reset/export explicit.
3. Add short knowledge checks whose answers explain reasoning rather than
   awarding gamified scores.
4. Generate exercise links from a typed command registry so documentation and
   CLI names cannot drift.
5. Add a citation-audit test that periodically checks external link health,
   while keeping network access out of the normal unit-test suite.
6. Create a research notebook template for hypothesis, data provenance,
   leakage checks, costs, validation, benchmark, and failure conditions.

