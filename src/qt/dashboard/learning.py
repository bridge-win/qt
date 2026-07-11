"""Cited quantitative-finance curriculum and dependency-free HTML renderer."""

from __future__ import annotations

import html
from dataclasses import dataclass
from typing import Literal, TypeAlias

EvidenceType: TypeAlias = Literal[
    "Foundational research",
    "Official curriculum",
    "Practitioner experience",
    "Project documentation",
]


@dataclass(frozen=True)
class LearningSource:
    id: str
    title: str
    publisher: str
    year: int | None
    url: str
    evidence_type: EvidenceType


@dataclass(frozen=True)
class LearningModule:
    number: int
    title: str
    outcome: str
    concepts: tuple[str, ...]
    experience_notes: tuple[str, ...]
    qt_exercise: str
    source_ids: tuple[str, ...]


SOURCES: tuple[LearningSource, ...] = (
    LearningSource(
        id="cfa-foundations",
        title="Investment Foundations Certificate",
        publisher="CFA Institute",
        year=None,
        url="https://investmentfoundations.cfainstitute.org/",
        evidence_type="Official curriculum",
    ),
    LearningSource(
        id="mit-probability",
        title="Introduction to Probability and Statistics",
        publisher="MIT OpenCourseWare",
        year=None,
        url="https://ocw.mit.edu/search/?q=Introduction+to+Probability+and+Statistics",
        evidence_type="Official curriculum",
    ),
    LearningSource(
        id="mit-finance",
        title="Finance Theory I",
        publisher="MIT OpenCourseWare",
        year=2008,
        url="https://ocw.mit.edu/courses/15-401-finance-theory-i-fall-2008/",
        evidence_type="Official curriculum",
    ),
    LearningSource(
        id="markowitz",
        title="Portfolio Selection",
        publisher="The Journal of Finance",
        year=1952,
        url="https://doi.org/10.2307/2975974",
        evidence_type="Foundational research",
    ),
    LearningSource(
        id="sharpe-capm",
        title="Capital Asset Prices: A Theory of Market Equilibrium",
        publisher="The Journal of Finance",
        year=1964,
        url="https://doi.org/10.2307/2977928",
        evidence_type="Foundational research",
    ),
    LearningSource(
        id="fama-markets",
        title="Efficient Capital Markets: A Review of Theory and Empirical Work",
        publisher="The Journal of Finance",
        year=1970,
        url="https://doi.org/10.2307/2325486",
        evidence_type="Foundational research",
    ),
    LearningSource(
        id="lo-adaptive",
        title="The Adaptive Markets Hypothesis",
        publisher="The Journal of Portfolio Management",
        year=2004,
        url="https://doi.org/10.3905/jpm.2004.442611",
        evidence_type="Foundational research",
    ),
    LearningSource(
        id="backtest-overfitting",
        title="The Probability of Backtest Overfitting",
        publisher="Journal of Computational Finance",
        year=2016,
        url="https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf",
        evidence_type="Foundational research",
    ),
    LearningSource(
        id="multiple-testing",
        title="… and the Cross-Section of Expected Returns",
        publisher="The Review of Financial Studies",
        year=2016,
        url="https://doi.org/10.1093/rfs/hhv059",
        evidence_type="Foundational research",
    ),
    LearningSource(
        id="bis-crypto",
        title="The crypto ecosystem: key elements and risks",
        publisher="Bank for International Settlements",
        year=2023,
        url="https://www.bis.org/publ/othp72.htm",
        evidence_type="Official curriculum",
    ),
    LearningSource(
        id="sec-crypto",
        title="Crypto Assets",
        publisher="U.S. SEC Investor.gov",
        year=None,
        url="https://www.investor.gov/additional-resources/spotlight/crypto-assets",
        evidence_type="Official curriculum",
    ),
    LearningSource(
        id="carver-systematic",
        title="Systematic Trading",
        publisher="Harriman House",
        year=2015,
        url="https://www.harriman-house.com/systematictrading",
        evidence_type="Practitioner experience",
    ),
    LearningSource(
        id="qt-strategy",
        title="QT strategy and evidence notes",
        publisher="QT project documentation",
        year=2026,
        url="https://github.com/bridge-win/qt/blob/main/docs/strategy.md",
        evidence_type="Project documentation",
    ),
    LearningSource(
        id="qt-operations",
        title="QT operations guide",
        publisher="QT project documentation",
        year=2026,
        url="https://github.com/bridge-win/qt/blob/main/docs/operations.md",
        evidence_type="Project documentation",
    ),
)


MODULES: tuple[LearningModule, ...] = (
    LearningModule(
        number=1,
        title="Financial system and instruments",
        outcome=(
            "Explain how cash moves through markets and distinguish ownership, borrowing, "
            "and derivative exposure before interpreting a trading signal."
        ),
        concepts=(
            "Time value of money, discounting, compounding, and inflation-adjusted returns",
            "Simple and logarithmic returns; nominal, real, gross, and net performance",
            "Cash, equities, bonds, commodities, currencies, and crypto assets",
            "Primary versus secondary markets; exchanges, brokers, clearing, and custody",
            "Order books, bid-ask spreads, market orders, limit orders, and stop orders",
            "Spot, futures, perpetual swaps, margin, leverage, funding, and liquidation",
        ),
        experience_notes=(
            "A correct market view can still lose money when spread, fees, funding, or liquidation "
            "mechanics are misunderstood.",
            "Do not trade an instrument until you can draw its cash flows and state who owes what "
            "to whom in both normal and stressed markets.",
        ),
        qt_exercise=(
            "Run `qt data sources`. Classify each dataset as price, flow, position, event, or "
            "derived indicator, then write what economic object it actually measures."
        ),
        source_ids=("cfa-foundations", "mit-finance", "bis-crypto", "qt-operations"),
    ),
    LearningModule(
        number=2,
        title="Probability and statistics",
        outcome=(
            "Reason about uncertain outcomes and noisy estimates without treating a sample metric "
            "as a durable fact."
        ),
        concepts=(
            "Random variables, probability distributions, expectation, variance, and quantiles",
            "Conditional probability, Bayes' rule, dependence, and independence",
            "Covariance, correlation, nonlinear dependence, and correlation instability",
            "Sampling distributions, standard errors, confidence intervals, and power",
            "Hypothesis tests, p-values, effect sizes, and multiple-comparison risk",
            "Skewness, kurtosis, fat tails, outliers, and robust summary statistics",
        ),
        experience_notes=(
            "A Sharpe ratio without its sample length and uncertainty is marketing, not a complete "
            "statistical statement.",
            "Trying many indicators and reporting only the winner creates evidence even when no "
            "true edge exists; record every trial."
        ),
        qt_exercise=(
            "Calculate BTC daily-return quantiles and compare observed tail counts with a fitted "
            "normal distribution. Explain why the difference matters for stops and sizing."
        ),
        source_ids=("mit-probability", "multiple-testing", "backtest-overfitting"),
    ),
    LearningModule(
        number=3,
        title="Data and time series",
        outcome=(
            "Construct a decision-time dataset that is reproducible and does not leak information "
            "from the future."
        ),
        concepts=(
            "Prices versus returns; levels, differences, and stationarity",
            "Autocorrelation, volatility clustering, seasonality, and structural breaks",
            "Sampling frequency, asynchronous markets, resampling, and timezone alignment",
            "Missing values, stale observations, revisions, and publication lags",
            "Look-ahead, survivorship, selection, and timestamp leakage",
            "Data provenance, schemas, validation, lineage, and reproducible snapshots",
        ),
        experience_notes=(
            "Most severe research errors enter before the strategy code: a clean chart can still "
            "contain revised or future-known data.",
            "For every feature, write the earliest timestamp at which a real operator could have "
            "observed it."
        ),
        qt_exercise=(
            "Inspect the dashboard data-source freshness table and one OHLCV file. For a chosen "
            "decision time, mark which rows and derived signals were genuinely available."
        ),
        source_ids=("mit-probability", "fama-markets", "qt-operations"),
    ),
    LearningModule(
        number=4,
        title="Portfolio and risk architecture",
        outcome=(
            "Translate uncertain forecasts into positions that keep losses, liquidity needs, and "
            "survival constraints explicit."
        ),
        concepts=(
            "Expected return, variance, covariance, diversification, and concentration",
            "Systematic risk, beta, factor exposure, and benchmark-relative risk",
            "Volatility, drawdown, recovery time, and path dependence",
            "Value at Risk, expected shortfall, stress scenarios, and model limitations",
            "Position sizing, volatility targeting, risk budgets, and exposure caps",
            "Liquidity, counterparty risk, correlation breakdown, and risk of ruin",
        ),
        experience_notes=(
            "Diversification measured in calm data can disappear during forced selling; include a "
            "shared-crash scenario.",
            "Sizing is a control system, not a confidence score. A compelling narrative never "
            "justifies bypassing an exposure cap."
        ),
        qt_exercise=(
            "Read `qt.risk` and select one paper-only limit. Predict its effect on position size and "
            "maximum loss, run the scenario, and reconcile prediction with output."
        ),
        source_ids=("markowitz", "sharpe-capm", "carver-systematic", "qt-strategy"),
    ),
    LearningModule(
        number=5,
        title="Strategy research",
        outcome=(
            "Turn an economic hypothesis into a falsifiable, cost-aware rule with an honest "
            "benchmark and a reason it may stop working."
        ),
        concepts=(
            "Economic mechanism, market participant, constraint, and expected compensation",
            "Signal definition, decision frequency, holding period, and exit rule",
            "Null hypothesis, benchmark, effect size, and predeclared success criteria",
            "Turnover, fees, spread, slippage, funding, borrow, and tax considerations",
            "Parameter sensitivity, capacity, crowding, competition, and edge decay",
            "Research logs, versioned hypotheses, negative results, and reproducibility",
        ),
        experience_notes=(
            "Begin with who pays the strategy and why that transfer might persist; an indicator "
            "pattern without a mechanism is fragile.",
            "Simple rules are not automatically valid, but their assumptions and failures are "
            "usually easier to inspect than a heavily tuned rule."
        ),
        qt_exercise=(
            "Before running code, write a one-page hypothesis for DCA, trend, carry, or "
            "capitulation: payer, mechanism, rule, costs, benchmark, and invalidation evidence."
        ),
        source_ids=("fama-markets", "lo-adaptive", "carver-systematic", "qt-strategy"),
    ),
    LearningModule(
        number=6,
        title="Backtesting and validation",
        outcome=(
            "Use historical simulation to reject weak ideas rather than manufacture confidence in "
            "the best-looking trial."
        ),
        concepts=(
            "Event timing, realistic fills, transaction costs, and portfolio accounting",
            "Training, validation, test sets, and true out-of-sample evaluation",
            "Data snooping, selection bias, multiple testing, and backtest overfitting",
            "Walk-forward analysis, overlapping labels, purging, and embargo concepts",
            "Bootstrap and Monte Carlo stress, parameter perturbation, and scenario tests",
            "Benchmark comparison, uncertainty intervals, reproducible artifacts, and audit trails",
        ),
        experience_notes=(
            "Synthetic data proves software behavior, not trading edge. Real-history success is also "
            "only evidence—not a guarantee.",
            "Keep failed variants in the research ledger. Deleting them makes the surviving result "
            "look more statistically independent than it is."
        ),
        qt_exercise=(
            "Run a synthetic backtest as a software check, inspect the artifact, then run "
            "walk-forward and Monte Carlo checks on real history and compare with plain DCA."
        ),
        source_ids=(
            "backtest-overfitting",
            "multiple-testing",
            "lo-adaptive",
            "qt-strategy",
        ),
    ),
    LearningModule(
        number=7,
        title="Crypto-specific market structure",
        outcome=(
            "Explain the venue, custody, collateral, and derivative risks that are absent from a "
            "simple spot-price chart."
        ),
        concepts=(
            "Self-custody, exchange custody, counterparty exposure, and operational key risk",
            "Fragmented venues, cross-venue prices, settlement, and transfer latency",
            "Perpetual funding, dated-futures basis, collateral, and margin conventions",
            "Liquidation engines, insurance funds, auto-deleveraging, and cascade dynamics",
            "Stablecoin reserves, redemption, depegs, and quote-currency risk",
            "On-chain metrics, reflexivity, 24/7 liquidity, and changing market regimes",
        ),
        experience_notes=(
            "A market-neutral position can retain venue, collateral, funding, execution, and "
            "liquidation risk; neutral delta does not mean risk-free.",
            "Do not treat an on-chain metric as ground truth until its entity labels, revisions, and "
            "economic interpretation are understood."
        ),
        qt_exercise=(
            "Trace one carry or wick opportunity from source fields through fee assumptions, risk "
            "gating, paper execution, and portfolio accounting. List every non-price failure mode."
        ),
        source_ids=("bis-crypto", "sec-crypto", "qt-strategy"),
    ),
    LearningModule(
        number=8,
        title="Execution and operating discipline",
        outcome=(
            "Operate a research system with reconciliation, monitoring, and human controls instead "
            "of assuming automation creates safety."
        ),
        concepts=(
            "Order states, partial fills, rejection, cancellation, retries, and idempotency",
            "Expected versus realized slippage, fee drift, and execution-quality review",
            "Position and cash reconciliation across broker, ledger, and strategy state",
            "Heartbeats, alerts, incident response, kill switches, and recovery procedures",
            "Paper, dry-run, small-live rollout gates, acceptance evidence, and rollback",
            "Behavioral bias, journals, change control, postmortems, and model governance",
        ),
        experience_notes=(
            "The dangerous failure is often silent disagreement between intended, submitted, "
            "filled, and recorded positions; reconcile all four.",
            "A live rollout is an operational experiment. Increase scope only after predeclared "
            "evidence, never because of recent profits or fear of missing out."
        ),
        qt_exercise=(
            "Complete `docs/live-checklist.md` while staying in paper mode. Review four weeks of "
            "heartbeats, ledger reconciliation, fees, incidents, and DCA benchmark results."
        ),
        source_ids=("carver-systematic", "sec-crypto", "qt-operations"),
    ),
)


def validate_curriculum(
    modules: tuple[LearningModule, ...],
    sources: tuple[LearningSource, ...],
) -> None:
    """Raise a precise error when authored curriculum data is inconsistent."""

    source_ids = [source.id for source in sources]
    if len(source_ids) != len(set(source_ids)):
        raise ValueError("duplicate source id")

    for source in sources:
        if not source.url.startswith("https://"):
            raise ValueError(f"source {source.id!r} must use HTTPS")

    module_numbers = [module.number for module in modules]
    if len(module_numbers) != len(set(module_numbers)):
        raise ValueError("duplicate module number")

    known_sources = set(source_ids)
    for module in modules:
        if not module.source_ids:
            raise ValueError(f"module {module.number} has no citations")
        for source_id in module.source_ids:
            if source_id not in known_sources:
                raise ValueError(
                    f"module {module.number} cites unknown source {source_id!r}"
                )


def _e(value: str) -> str:
    return html.escape(value, quote=True)


def _source_by_id() -> dict[str, LearningSource]:
    return {source.id: source for source in SOURCES}


def _render_module(module: LearningModule, sources: dict[str, LearningSource]) -> str:
    concepts = "".join(f"<li>{_e(concept)}</li>" for concept in module.concepts)
    cautions = "".join(f"<li>{_e(note)}</li>" for note in module.experience_notes)
    citations = "".join(
        (
            f'<a class="citation" href="#source-{_e(source_id)}">'
            f'[{_e(source_id)}]</a>'
        )
        for source_id in module.source_ids
        if source_id in sources
    )
    open_attribute = " open" if module.number == 1 else ""
    return f"""
      <details class="module" id="module-{module.number}"{open_attribute}>
        <summary>
          <span class="module-number">{module.number:02d}</span>
          <span><strong>{_e(module.title)}</strong><small>{_e(module.outcome)}</small></span>
          <span class="summary-mark" aria-hidden="true">+</span>
        </summary>
        <div class="module-body">
          <section aria-labelledby="concepts-{module.number}">
            <h3 id="concepts-{module.number}">What to understand</h3>
            <ul>{concepts}</ul>
          </section>
          <section class="experience" aria-labelledby="experience-{module.number}">
            <h3 id="experience-{module.number}">Experience notes</h3>
            <ul>{cautions}</ul>
          </section>
          <section class="exercise" aria-labelledby="exercise-{module.number}">
            <div>
              <span class="eyebrow">QT LAB</span>
              <h3 id="exercise-{module.number}">Apply it in this project</h3>
            </div>
            <p>{_e(module.qt_exercise)}</p>
          </section>
          <p class="module-sources"><strong>Sources</strong> {citations}</p>
        </div>
      </details>
    """


def _render_source(source: LearningSource) -> str:
    year = f" ({source.year})" if source.year is not None else ""
    return f"""
      <li id="source-{_e(source.id)}">
        <span class="source-type">{_e(source.evidence_type)}</span>
        <a href="{_e(source.url)}" target="_blank" rel="noopener noreferrer">
          {_e(source.title)}</a>
        <span class="source-meta">— {_e(source.publisher)}{year}</span>
      </li>
    """


def render_learning_page() -> str:
    """Render the complete, validated quant learning-center document."""

    validate_curriculum(MODULES, SOURCES)
    sources = _source_by_id()
    module_links = "".join(
        f'<a href="#module-{module.number}"><span>{module.number:02d}</span>{_e(module.title)}</a>'
        for module in MODULES
    )
    modules = "".join(_render_module(module, sources) for module in MODULES)
    bibliography = "".join(_render_source(source) for source in SOURCES)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="description" content="A cited learning path from financial foundations to quantitative trading practice.">
  <link rel="icon" href="data:,">
  <title>QT — Quant Learning Center</title>
  <style>
    :root {{
      color-scheme: light;
      --paper: #f2f1eb;
      --surface: #fbfaf6;
      --ink: #18211d;
      --muted: #647069;
      --line: #d4d9d3;
      --accent: #087970;
      --accent-soft: #dceeea;
      --caution: #9a5a16;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    * {{ box-sizing: border-box; }}
    html {{ scroll-behavior: smooth; }}
    body {{ margin: 0; background: var(--paper); color: var(--ink); line-height: 1.6; }}
    a {{ color: var(--accent); text-underline-offset: 3px; }}
    a:hover {{ text-decoration-thickness: 2px; }}
    :focus-visible {{ outline: 3px solid var(--accent); outline-offset: 4px; border-radius: 2px; }}
    .skip {{ position: absolute; left: 12px; top: -60px; padding: 9px 12px; background: var(--ink); color: white; z-index: 10; }}
    .skip:focus {{ top: 12px; }}
    .shell {{ width: min(1180px, calc(100vw - 40px)); margin: 0 auto; }}
    .topbar {{ display: flex; align-items: center; justify-content: space-between; padding: 22px 0; font-size: 14px; }}
    .brand {{ color: var(--ink); font-weight: 800; text-decoration: none; letter-spacing: .08em; }}
    .hero {{ padding: 68px 0 76px; border-top: 1px solid var(--line); border-bottom: 1px solid var(--line); }}
    .hero-grid {{ display: grid; grid-template-columns: minmax(0, 1.45fr) minmax(260px, .55fr); gap: 72px; align-items: end; }}
    .eyebrow {{ color: var(--accent); font-size: 12px; font-weight: 800; letter-spacing: .13em; }}
    h1 {{ max-width: 800px; margin: 12px 0 20px; font-family: Georgia, "Times New Roman", serif; font-size: clamp(48px, 7vw, 86px); font-weight: 500; letter-spacing: -.045em; line-height: .98; }}
    .lede {{ max-width: 680px; margin: 0; color: #3f4d46; font-size: clamp(18px, 2vw, 22px); }}
    .disclaimer {{ border-left: 3px solid var(--caution); padding-left: 18px; color: var(--muted); font-size: 14px; }}
    .disclaimer strong {{ display: block; margin-bottom: 4px; color: var(--ink); }}
    .method {{ padding: 68px 0; background: var(--surface); border-bottom: 1px solid var(--line); }}
    .section-head {{ display: grid; grid-template-columns: 180px minmax(0, 640px); gap: 28px; margin-bottom: 34px; }}
    .section-head h2 {{ margin: 0; font-family: Georgia, "Times New Roman", serif; font-size: clamp(30px, 4vw, 48px); font-weight: 500; line-height: 1.08; }}
    .steps {{ display: grid; grid-template-columns: repeat(4, 1fr); border-top: 1px solid var(--line); }}
    .step {{ padding: 22px 24px 4px 0; border-right: 1px solid var(--line); }}
    .step + .step {{ padding-left: 24px; }}
    .step:last-child {{ border-right: 0; }}
    .step span {{ color: var(--accent); font: 700 12px/1 ui-monospace, monospace; }}
    .step h3 {{ margin: 10px 0 5px; font-size: 17px; }}
    .step p {{ margin: 0; color: var(--muted); font-size: 14px; }}
    .schedule {{ margin-top: 40px; width: 100%; border-collapse: collapse; font-size: 14px; }}
    .schedule th, .schedule td {{ padding: 11px 12px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }}
    .schedule th {{ color: var(--muted); font-size: 12px; letter-spacing: .08em; text-transform: uppercase; }}
    .curriculum {{ display: grid; grid-template-columns: 250px minmax(0, 1fr); gap: 56px; padding: 72px 0 88px; }}
    .rail {{ position: sticky; top: 20px; align-self: start; }}
    .rail h2 {{ margin: 8px 0 20px; font: 500 27px/1.1 Georgia, serif; }}
    .rail nav {{ display: grid; }}
    .rail nav a {{ display: grid; grid-template-columns: 30px 1fr; gap: 8px; padding: 8px 0; color: var(--muted); text-decoration: none; font-size: 13px; border-bottom: 1px solid transparent; }}
    .rail nav a:hover {{ color: var(--ink); border-color: var(--line); }}
    .rail nav span {{ color: var(--accent); font-family: ui-monospace, monospace; }}
    .module {{ border-top: 1px solid var(--line); }}
    .module:last-child {{ border-bottom: 1px solid var(--line); }}
    .module summary {{ display: grid; grid-template-columns: 58px minmax(0, 1fr) 30px; gap: 12px; align-items: start; padding: 25px 0; cursor: pointer; list-style: none; }}
    .module summary::-webkit-details-marker {{ display: none; }}
    .module-number {{ color: var(--accent); font: 700 13px/1.5 ui-monospace, monospace; }}
    .module summary strong {{ display: block; font: 500 clamp(22px, 3vw, 32px)/1.08 Georgia, serif; }}
    .module summary small {{ display: block; max-width: 730px; margin-top: 8px; color: var(--muted); font-size: 14px; font-weight: 400; }}
    .summary-mark {{ color: var(--accent); font-size: 28px; line-height: 1; transition: transform .2s ease; }}
    .module[open] .summary-mark {{ transform: rotate(45deg); }}
    .module-body {{ padding: 0 0 42px 70px; }}
    .module-body h3 {{ margin: 0 0 10px; font-size: 14px; letter-spacing: .04em; }}
    .module-body ul {{ margin: 0; padding-left: 20px; }}
    .module-body li {{ margin-bottom: 6px; }}
    .experience {{ margin-top: 28px; padding-left: 18px; border-left: 3px solid var(--caution); }}
    .experience h3 {{ color: var(--caution); }}
    .exercise {{ display: grid; grid-template-columns: 190px minmax(0, 1fr); gap: 22px; margin-top: 30px; padding: 22px; background: var(--accent-soft); }}
    .exercise p {{ margin: 0; }}
    .module-sources {{ margin: 20px 0 0; color: var(--muted); font-size: 13px; }}
    .citation {{ margin-left: 8px; font-family: ui-monospace, monospace; }}
    .bibliography {{ padding: 72px 0; background: #17221d; color: #e5ebe7; }}
    .bibliography .section-head {{ grid-template-columns: 180px minmax(0, 700px); }}
    .bibliography .eyebrow, .bibliography a {{ color: #78d0c4; }}
    .bibliography ol {{ margin: 0; padding-left: 22px; columns: 2; column-gap: 60px; }}
    .bibliography li {{ break-inside: avoid; margin: 0 0 18px; padding-left: 8px; }}
    .source-type {{ display: block; color: #9eaaa3; font-size: 11px; font-weight: 800; letter-spacing: .08em; text-transform: uppercase; }}
    .source-meta {{ color: #aeb9b2; font-size: 13px; }}
    footer {{ padding: 34px 0; color: var(--muted); font-size: 13px; }}
    @media (max-width: 820px) {{
      .hero-grid, .curriculum {{ grid-template-columns: 1fr; gap: 36px; }}
      .rail {{ position: static; }}
      .rail nav {{ grid-template-columns: repeat(2, 1fr); column-gap: 20px; }}
      .section-head {{ grid-template-columns: 1fr; gap: 10px; }}
      .steps {{ grid-template-columns: repeat(2, 1fr); }}
      .step:nth-child(2) {{ border-right: 0; }}
      .step:nth-child(n+3) {{ border-top: 1px solid var(--line); }}
      .bibliography ol {{ columns: 1; }}
    }}
    @media (max-width: 520px) {{
      .shell {{ width: min(100vw - 24px, 1180px); }}
      .topbar {{ padding: 15px 0; }}
      .hero {{ padding: 42px 0 48px; }}
      h1 {{ font-size: 46px; }}
      .method, .bibliography {{ padding: 50px 0; }}
      .steps, .rail nav {{ grid-template-columns: 1fr; }}
      .step, .step + .step {{ padding: 18px 0; border-right: 0; border-top: 1px solid var(--line); }}
      .curriculum {{ padding: 52px 0 64px; }}
      .module summary {{ grid-template-columns: 38px minmax(0, 1fr) 20px; }}
      .module-body {{ padding-left: 0; }}
      .exercise {{ grid-template-columns: 1fr; gap: 8px; padding: 18px; }}
      .schedule {{ display: block; overflow-x: auto; }}
    }}
    @media (prefers-reduced-motion: reduce) {{
      html {{ scroll-behavior: auto; }}
      .summary-mark {{ transition: none; }}
    }}
  </style>
</head>
<body>
  <a class="skip" href="#content">Skip to curriculum</a>
  <header class="shell topbar">
    <a class="brand" href="/">QT</a>
    <nav aria-label="Utility"><a href="/">Monitor</a> · <a href="/portfolio">P&amp;L</a></nav>
  </header>
  <main id="content">
    <section class="hero">
      <div class="shell hero-grid">
        <div>
          <span class="eyebrow">QUANT LEARNING CENTER</span>
          <h1>Learn the system before trading it.</h1>
          <p class="lede">A cited path from financial foundations to evidence-aware research, risk, and operations.</p>
        </div>
        <aside class="disclaimer">
          <strong>Education, not investment advice.</strong>
          This material does not promise returns or recommend leverage, live trading, or a capital allocation. Stay in paper mode while learning.
        </aside>
      </div>
    </section>
    <section class="method" aria-labelledby="method-title">
      <div class="shell">
        <div class="section-head">
          <span class="eyebrow">HOW TO LEARN</span>
          <h2 id="method-title">Learn · Derive · Apply · Challenge</h2>
        </div>
        <div class="steps">
          <article class="step"><span>01</span><h3>Learn</h3><p>Read one primary source and summarize it in five sentences.</p></article>
          <article class="step"><span>02</span><h3>Derive</h3><p>Reproduce the central equation or concept by hand or notebook.</p></article>
          <article class="step"><span>03</span><h3>Apply</h3><p>Complete the QT exercise with historical or paper data.</p></article>
          <article class="step"><span>04</span><h3>Challenge</h3><p>State assumptions, failure modes, and disconfirming evidence.</p></article>
        </div>
        <h3 style="margin-top:40px">A 12-week route · 5-7 hours per week</h3>
        <table class="schedule">
          <thead><tr><th>Weeks</th><th>Focus</th><th>Evidence before advancing</th></tr></thead>
          <tbody>
            <tr><td>1-2</td><td>Instruments + statistics</td><td>Explain returns, orders, distributions, and estimation error.</td></tr>
            <tr><td>3-4</td><td>Data + time series</td><td>Produce a decision-time, leakage-free dataset note.</td></tr>
            <tr><td>5-6</td><td>Portfolio + risk</td><td>Write and defend a paper-only risk budget.</td></tr>
            <tr><td>7-8</td><td>Research + validation</td><td>Complete a reproducible, cost-aware report against a benchmark.</td></tr>
            <tr><td>9-10</td><td>Crypto structure</td><td>Explain funding, basis, liquidation, custody, and venue risk.</td></tr>
            <tr><td>11-12</td><td>Operations</td><td>Complete reconciliation, incident, and benchmark reviews.</td></tr>
          </tbody>
        </table>
      </div>
    </section>
    <div class="shell curriculum">
      <aside class="rail">
        <span class="eyebrow">KNOWLEDGE MAP</span>
        <h2>Eight modules, in order.</h2>
        <nav aria-label="Learning modules">{module_links}</nav>
      </aside>
      <div>{modules}</div>
    </div>
    <section class="bibliography" aria-labelledby="sources-title">
      <div class="shell">
        <div class="section-head">
          <span class="eyebrow">EVIDENCE</span>
          <h2 id="sources-title">Sources and further learning</h2>
        </div>
        <ol>{bibliography}</ol>
      </div>
    </section>
  </main>
  <footer class="shell">
    Progress means you can explain assumptions, reproduce results, identify failure modes, and compare with a simple benchmark—not that a backtest is profitable. Paper first.
  </footer>
</body>
</html>
"""
