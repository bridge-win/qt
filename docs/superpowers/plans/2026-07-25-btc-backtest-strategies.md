# BTC Backtest Top-20 Strategies Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the exact twenty common algorithms, buy-and-hold benchmark, QT-specific strategies, catalog discovery, shared contracts, and deterministic golden tests.

**Architecture:** Indicator strategies subclass a shared target-weight adapter that translates a point-in-time desired allocation into event-engine orders. Stateful grid and carry strategies implement the base event protocol directly. A typed registry is the sole source of built-in identifiers and metadata.

**Tech Stack:** Python 3.10+, pandas, NumPy, Pydantic, pytest, Hypothesis, Ruff, strict Mypy

## Global Constraints

- Exactly twenty common algorithms are counted.
- `buy_and_hold`, `capitulation`, and `wick_catcher` are additional and do not reduce the twenty.
- Every strategy uses only data available through the active bar.
- Every parameter has a type, conservative default, and validation bounds.
- Every strategy declares warm-up, timeframe, market, and auxiliary-data requirements.
- Spot strategies are long/flat and non-leveraged.
- Carry may use only a declared spot-long/perpetual-short pair.
- Every built-in passes no-trade, entry, exit, insufficient-data, contract, golden, sensitivity, and no-look-ahead tests.
- Strategy defaults are examples, not profitability claims.
- Every task ends with focused tests, `git diff --check`, a commit, and a push.

---

## File Map

- `strategies/registry.py`: typed catalog and aliases.
- `strategies/indicators.py`: pure point-in-time indicator functions.
- `strategies/target_weight.py`: target-weight-to-order adapter.
- `strategies/accumulation.py`: fixed and smart DCA.
- `strategies/moving_average.py`: SMA, EMA, and MACD.
- `strategies/oscillators.py`: RSI and stochastic reversal.
- `strategies/bands.py`: Bollinger mean reversion and breakout.
- `strategies/channels.py`: Donchian and Turtle.
- `strategies/momentum.py`: time-series, dual, ROC, and ADX.
- `strategies/volatility.py`: ATR breakout and Keltner.
- `strategies/vwap.py`: rolling VWAP mean reversion.
- `strategies/grid.py`: bounded inventory grid.
- `strategies/carry.py`: funding-basis carry.
- `strategies/benchmarks.py`: buy-and-hold.
- `strategies/qt_special.py`: capitulation and wick-catcher ports.
- `tests/strategies/`: per-family, contract, catalog, golden, and sensitivity tests.

### Task 1: Indicators And Target-Weight Adapter

**Files:**
- Create: `packages/btc-backtest/src/btc_backtest/strategies/indicators.py`
- Create: `packages/btc-backtest/src/btc_backtest/strategies/target_weight.py`
- Create: `packages/btc-backtest/tests/strategies/test_indicators.py`
- Create: `packages/btc-backtest/tests/strategies/test_target_weight.py`

**Interfaces:**
- Produces pure functions `sma`, `ema`, `rsi`, `stochastic`, `macd`, `atr`,
  `adx`, `bollinger`, `donchian`, `keltner`, `rolling_vwap`, and `roc`.
- Produces `TargetWeightStrategy.target_weight(context) -> Decimal`.
- Produces `TargetWeightStrategy.on_bar(context) -> tuple[OrderIntent, ...]`.

- [ ] **Step 1: Write failing hand-calculated indicator tests**

```python
def test_sma_and_ema_have_point_in_time_values() -> None:
    values = pd.Series([1.0, 2.0, 3.0, 4.0])
    assert sma(values, 3).tolist()[-2:] == [2.0, 3.0]
    assert ema(values, 3).iloc[-1] == pytest.approx(3.125)


def test_target_weight_adapter_buys_only_the_cash_needed(context) -> None:
    strategy = ConstantWeightStrategy(Decimal("0.50"))
    intents = strategy.on_bar(context(cash="1000", qty="0", close="100"))
    assert len(intents) == 1
    assert intents[0].side == OrderSide.BUY
    assert intents[0].quote_amount == Decimal("500")
```

- [ ] **Step 2: Run tests and verify RED**

```bash
.venv/bin/pytest packages/btc-backtest/tests/strategies/test_indicators.py packages/btc-backtest/tests/strategies/test_target_weight.py -q
```

Expected: import failures for indicator and adapter modules.

- [ ] **Step 3: Implement pure indicators and bounded allocation**

Use pandas rolling/ewm operations with `min_periods=window`; return `NaN` until
warm-up completes. Wilder RSI/ATR/ADX use `alpha=1/window`. The adapter clips
weight to metadata bounds, compares target BTC value to current BTC value, and
emits one market intent only when deviation exceeds `rebalance_tolerance`.

- [ ] **Step 4: Run strategy primitives**

```bash
.venv/bin/pytest packages/btc-backtest/tests/strategies/test_indicators.py packages/btc-backtest/tests/strategies/test_target_weight.py -q
.venv/bin/ruff check packages/btc-backtest
.venv/bin/mypy --strict packages/btc-backtest/src packages/btc-backtest/tests
```

Expected: hand-calculated and allocation tests pass.

- [ ] **Step 5: Commit and push**

```bash
git add packages/btc-backtest
git commit -m "feat(backtest): add strategy indicators and allocation adapter"
git push origin codex/quantdinger-platform-upgrade
```

### Task 2: Strategy Registry And Shared Contract Suite

**Files:**
- Create: `packages/btc-backtest/src/btc_backtest/strategies/registry.py`
- Modify: `packages/btc-backtest/tests/strategies/test_contract.py`
- Create: `packages/btc-backtest/tests/strategies/test_catalog.py`

**Interfaces:**
- Produces: `StrategyFactory = Callable[[Mapping[str, object]], Strategy]`.
- Produces: `StrategyRegistry.register`, `create`, `list`, and `describe`.
- Produces: `BUILTIN_STRATEGY_IDS` frozen tuple in exact catalog order.

- [ ] **Step 1: Write failing exact-catalog test**

```python
EXPECTED = (
    "fixed_dca", "smart_dca", "sma_crossover", "ema_crossover", "macd_trend",
    "rsi_mean_reversion", "stochastic_reversal", "bollinger_mean_reversion",
    "bollinger_breakout", "donchian_breakout", "turtle_trend",
    "time_series_momentum", "dual_momentum", "rate_of_change", "adx_trend",
    "atr_volatility_breakout", "keltner_channel", "vwap_mean_reversion",
    "grid_rebalance", "funding_basis_carry",
)


def test_exact_top_twenty_catalog() -> None:
    assert BUILTIN_STRATEGY_IDS == EXPECTED
    assert len(set(BUILTIN_STRATEGY_IDS)) == 20


def test_unregistered_builtin_fails_clearly(empty_registry) -> None:
    with pytest.raises(StrategyLoadError, match="implementation not registered"):
        empty_registry.create("sma_crossover", {})
```

- [ ] **Step 2: Run tests and verify RED**

```bash
.venv/bin/pytest packages/btc-backtest/tests/strategies/test_catalog.py -q
```

Expected: import failure for registry.

- [ ] **Step 3: Implement typed registry with temporary unavailable factories**

Create the registry API and exact immutable IDs. During this task, register
only a test fixture factory; production IDs remain explicitly unregistered.
`create` raises `StrategyLoadError("strategy implementation not registered:
<id>")` until later tasks register an implementation. Aliases are separate and
cannot change the exact catalog.

- [ ] **Step 4: Run registry tests**

```bash
.venv/bin/pytest packages/btc-backtest/tests/strategies/test_catalog.py -q
.venv/bin/ruff check packages/btc-backtest
.venv/bin/mypy --strict packages/btc-backtest/src packages/btc-backtest/tests
```

Expected: exact catalog and unregistered/duplicate/alias registry behavior pass.

- [ ] **Step 5: Commit and push**

```bash
git add packages/btc-backtest
git commit -m "feat(backtest): define exact top-20 strategy catalog"
git push origin codex/quantdinger-platform-upgrade
```

### Task 3: Fixed And Smart DCA

**Files:**
- Create: `packages/btc-backtest/src/btc_backtest/strategies/accumulation.py`
- Create: `packages/btc-backtest/tests/strategies/test_accumulation.py`
- Modify: `packages/btc-backtest/src/btc_backtest/strategies/registry.py`

**Interfaces:**
- Produces: `FixedDCA`, `FixedDCAParams`, `SmartDCA`, `SmartDCAParams`.
- Registers: `fixed_dca`, `smart_dca`.

- [ ] **Step 1: Write failing schedule and scaling tests**

```python
def test_fixed_dca_buys_once_per_utc_week() -> None:
    strategy = FixedDCA({"quote_amount": 100, "weekday": 0})
    intents = run_bars(strategy, daily_contexts("2024-01-01", periods=14))
    assert [intent.quote_amount for intent in intents] == [Decimal("100")] * 2


def test_smart_dca_scales_oversold_buy_without_exceeding_cap() -> None:
    strategy = SmartDCA(
        {"base_quote": 100, "max_multiplier": 2, "rsi_oversold": 30}
    )
    intent = strategy.on_bar(context_with_features(rsi=20, fear_greed=15))[0]
    assert intent.quote_amount == Decimal("200")
```

- [ ] **Step 2: Run tests and verify RED**

```bash
.venv/bin/pytest packages/btc-backtest/tests/strategies/test_accumulation.py -q
```

Expected: import failure for accumulation strategies.

- [ ] **Step 3: Implement schedule and bounded factor scaling**

Fixed DCA tracks the last UTC schedule bucket and skips when cash is
insufficient. Smart DCA multiplies the base quote using only declared RSI and
optional point-in-time fear/greed or valuation observations, clamps to
`[min_multiplier, max_multiplier]`, and degrades to base DCA when optional
features are absent.

- [ ] **Step 4: Run family and contract tests**

```bash
.venv/bin/pytest packages/btc-backtest/tests/strategies/test_accumulation.py packages/btc-backtest/tests/strategies/test_contract.py -q
.venv/bin/ruff check packages/btc-backtest
.venv/bin/mypy --strict packages/btc-backtest/src packages/btc-backtest/tests
```

Expected: no-trade, entry, insufficient-cash, and schedule tests pass.

- [ ] **Step 5: Commit and push**

```bash
git add packages/btc-backtest
git commit -m "feat(backtest): add fixed and smart DCA strategies"
git push origin codex/quantdinger-platform-upgrade
```

### Task 4: SMA, EMA, And MACD Trend Strategies

**Files:**
- Create: `packages/btc-backtest/src/btc_backtest/strategies/moving_average.py`
- Create: `packages/btc-backtest/tests/strategies/test_moving_average.py`
- Modify: `packages/btc-backtest/src/btc_backtest/strategies/registry.py`

**Interfaces:**
- Produces: `SMACrossover`, `EMACrossover`, `MACDTrend` and parameter models.
- Registers: `sma_crossover`, `ema_crossover`, `macd_trend`.

- [ ] **Step 1: Write failing crossover/warm-up tests**

```python
@pytest.mark.parametrize(
    ("strategy", "prices"),
    [
        (SMACrossover({"fast_window": 2, "slow_window": 3}), [3, 2, 1, 2, 4]),
        (EMACrossover({"fast_window": 2, "slow_window": 3}), [3, 2, 1, 2, 4]),
    ],
)
def test_crossover_enters_only_after_confirmed_cross(strategy, prices) -> None:
    weights = target_weights(strategy, prices)
    assert weights[:3] == [Decimal("0")] * 3
    assert weights[-1] == Decimal("1")


def test_macd_exits_when_histogram_crosses_below_zero() -> None:
    weights = target_weights(MACDTrend({"fast": 2, "slow": 4, "signal": 2}), cycle_prices())
    assert Decimal("1") in weights
    assert weights[-1] == Decimal("0")
```

- [ ] **Step 2: Run tests and verify RED**

```bash
.venv/bin/pytest packages/btc-backtest/tests/strategies/test_moving_average.py -q
```

Expected: import failure for moving-average strategies.

- [ ] **Step 3: Implement point-in-time cross logic**

Require `fast < slow`, declare warm-up as `slow + signal` where applicable,
compare current and previous computed values to detect a completed crossover,
hold one target until the opposite cross, and never backfill warm-up signals.

- [ ] **Step 4: Run family, contract, and no-look-ahead tests**

```bash
.venv/bin/pytest packages/btc-backtest/tests/strategies/test_moving_average.py packages/btc-backtest/tests/engine/test_no_lookahead.py -q
.venv/bin/ruff check packages/btc-backtest
.venv/bin/mypy --strict packages/btc-backtest/src packages/btc-backtest/tests
```

Expected: entry, exit, warm-up, invalid-parameter, and no-look-ahead tests pass.

- [ ] **Step 5: Commit and push**

```bash
git add packages/btc-backtest
git commit -m "feat(backtest): add moving-average trend strategies"
git push origin codex/quantdinger-platform-upgrade
```

### Task 5: RSI And Stochastic Reversal

**Files:**
- Create: `packages/btc-backtest/src/btc_backtest/strategies/oscillators.py`
- Create: `packages/btc-backtest/tests/strategies/test_oscillators.py`
- Modify: `packages/btc-backtest/src/btc_backtest/strategies/registry.py`

**Interfaces:**
- Produces: `RSIMeanReversion`, `StochasticReversal` and parameter models.
- Registers: `rsi_mean_reversion`, `stochastic_reversal`.

- [ ] **Step 1: Write failing oscillator tests**

```python
def test_rsi_enters_oversold_and_exits_on_normalization() -> None:
    strategy = RSIMeanReversion({"window": 3, "entry": 25, "exit": 55})
    weights = target_weights(strategy, [10, 9, 8, 7, 8, 9, 10])
    assert Decimal("1") in weights
    assert weights[-1] == Decimal("0")


def test_stochastic_requires_k_cross_above_d_while_oversold() -> None:
    strategy = StochasticReversal({"k_window": 3, "d_window": 2, "entry": 20})
    weights = target_weights_from_ohlc(strategy, stochastic_fixture())
    assert first_long_index(weights) == expected_cross_index()
```

- [ ] **Step 2: Run tests and verify RED**

```bash
.venv/bin/pytest packages/btc-backtest/tests/strategies/test_oscillators.py -q
```

Expected: import failure for oscillators.

- [ ] **Step 3: Implement hysteresis and completed-cross rules**

Validate `0 <= entry < exit <= 100`. RSI holds long between entry and exit.
Stochastic enters only on a completed `%K` above `%D` cross below the entry
zone and exits on the inverse cross above the exit zone.

- [ ] **Step 4: Run family and contract tests**

```bash
.venv/bin/pytest packages/btc-backtest/tests/strategies/test_oscillators.py packages/btc-backtest/tests/strategies/test_contract.py -q
.venv/bin/ruff check packages/btc-backtest
.venv/bin/mypy --strict packages/btc-backtest/src packages/btc-backtest/tests
```

Expected: no-trade, entry, exit, warm-up, and parameter bounds pass.

- [ ] **Step 5: Commit and push**

```bash
git add packages/btc-backtest
git commit -m "feat(backtest): add oscillator reversal strategies"
git push origin codex/quantdinger-platform-upgrade
```

### Task 6: Bollinger Mean Reversion And Breakout

**Files:**
- Create: `packages/btc-backtest/src/btc_backtest/strategies/bands.py`
- Create: `packages/btc-backtest/tests/strategies/test_bands.py`
- Modify: `packages/btc-backtest/src/btc_backtest/strategies/registry.py`

**Interfaces:**
- Produces: `BollingerMeanReversion`, `BollingerBreakout`.
- Registers: `bollinger_mean_reversion`, `bollinger_breakout`.

- [ ] **Step 1: Write failing band-behavior tests**

```python
def test_bollinger_mean_reversion_buys_lower_band_and_exits_middle() -> None:
    weights = target_weights(
        BollingerMeanReversion({"window": 3, "stddev": 1.0}),
        [10, 10, 10, 7, 9, 10],
    )
    assert weights[3] == Decimal("1")
    assert weights[-1] == Decimal("0")


def test_bollinger_breakout_enters_upper_band_and_uses_trailing_exit() -> None:
    strategy = BollingerBreakout({"window": 3, "stddev": 1.0, "atr_stop": 1.5})
    intents = run_strategy(strategy, breakout_then_reversal_fixture())
    assert [intent.reason for intent in intents] == ["upper_band_breakout", "atr_trailing_exit"]
```

- [ ] **Step 2: Run tests and verify RED**

```bash
.venv/bin/pytest packages/btc-backtest/tests/strategies/test_bands.py -q
```

Expected: import failure for band strategies.

- [ ] **Step 3: Implement separate mean-reversion and breakout state**

Both use population rolling standard deviation consistently. Mean reversion
enters below the lower band and exits at/above the center. Breakout enters only
after a close above the upper band and tracks a monotonically rising ATR stop.

- [ ] **Step 4: Run family tests**

```bash
.venv/bin/pytest packages/btc-backtest/tests/strategies/test_bands.py -q
.venv/bin/ruff check packages/btc-backtest
.venv/bin/mypy --strict packages/btc-backtest/src packages/btc-backtest/tests
```

Expected: all pass.

- [ ] **Step 5: Commit and push**

```bash
git add packages/btc-backtest
git commit -m "feat(backtest): add Bollinger strategies"
git push origin codex/quantdinger-platform-upgrade
```

### Task 7: Donchian And Turtle Breakouts

**Files:**
- Create: `packages/btc-backtest/src/btc_backtest/strategies/channels.py`
- Create: `packages/btc-backtest/tests/strategies/test_channels.py`
- Modify: `packages/btc-backtest/src/btc_backtest/strategies/registry.py`

**Interfaces:**
- Produces: `DonchianBreakout`, `TurtleTrend`.
- Registers: `donchian_breakout`, `turtle_trend`.

- [ ] **Step 1: Write failing prior-channel tests**

```python
def test_donchian_uses_prior_channel_not_current_high() -> None:
    strategy = DonchianBreakout({"entry_window": 3, "exit_window": 2})
    weights = target_weights_from_ohlc(strategy, donchian_fixture())
    assert weights[3] == Decimal("1")


def test_turtle_sizes_by_atr_and_exits_short_channel() -> None:
    strategy = TurtleTrend(
        {"entry_window": 3, "exit_window": 2, "risk_fraction": 0.01, "atr_window": 2}
    )
    intents = run_strategy(strategy, turtle_fixture())
    assert intents[0].quote_amount < Decimal("10000")
    assert intents[-1].reason == "turtle_exit_channel"
```

- [ ] **Step 2: Run tests and verify RED**

```bash
.venv/bin/pytest packages/btc-backtest/tests/strategies/test_channels.py -q
```

Expected: import failure for channels.

- [ ] **Step 3: Implement shifted channels and bounded ATR sizing**

Channel extrema are shifted one bar before comparison. Donchian uses full/flat
weights. Turtle derives quote risk from equity times `risk_fraction`, divides
by ATR stop distance, and caps notional at available cash and `max_weight`.

- [ ] **Step 4: Run channel and accounting tests**

```bash
.venv/bin/pytest packages/btc-backtest/tests/strategies/test_channels.py packages/btc-backtest/tests/engine/test_accounting_properties.py -q
.venv/bin/ruff check packages/btc-backtest
.venv/bin/mypy --strict packages/btc-backtest/src packages/btc-backtest/tests
```

Expected: shifted-channel, sizing, exit, and inventory invariants pass.

- [ ] **Step 5: Commit and push**

```bash
git add packages/btc-backtest
git commit -m "feat(backtest): add Donchian and Turtle breakouts"
git push origin codex/quantdinger-platform-upgrade
```

### Task 8: Momentum And ADX Strategies

**Files:**
- Create: `packages/btc-backtest/src/btc_backtest/strategies/momentum.py`
- Create: `packages/btc-backtest/tests/strategies/test_momentum.py`
- Modify: `packages/btc-backtest/src/btc_backtest/strategies/registry.py`

**Interfaces:**
- Produces: `TimeSeriesMomentum`, `DualMomentum`, `RateOfChange`, `ADXTrend`.
- Registers: `time_series_momentum`, `dual_momentum`, `rate_of_change`, `adx_trend`.

- [ ] **Step 1: Write failing momentum tests**

```python
@pytest.mark.parametrize(
    ("strategy", "prices", "expected"),
    [
        (TimeSeriesMomentum({"lookback": 3}), [10, 9, 8, 11], Decimal("1")),
        (RateOfChange({"lookback": 3, "entry": 0.05, "exit": 0.0}), [10, 10, 10, 11], Decimal("1")),
    ],
)
def test_absolute_momentum_enters_only_positive(strategy, prices, expected) -> None:
    assert target_weights(strategy, prices)[-1] == expected


def test_dual_momentum_requires_btc_and_cash_relative_momentum() -> None:
    strategy = DualMomentum({"lookback": 3, "cash_annual_rate": 0.05})
    assert target_weights(strategy, [10, 10.01, 10.02, 10.03])[-1] == Decimal("0")


def test_adx_requires_direction_and_strength() -> None:
    strategy = ADXTrend({"window": 3, "threshold": 20})
    assert target_weights_from_ohlc(strategy, strong_uptrend_fixture())[-1] == Decimal("1")
```

- [ ] **Step 2: Run tests and verify RED**

```bash
.venv/bin/pytest packages/btc-backtest/tests/strategies/test_momentum.py -q
```

Expected: import failure for momentum strategies.

- [ ] **Step 3: Implement four distinct rules**

Time-series momentum uses signed trailing return. Dual momentum additionally
requires return above the equivalent cash hurdle for the bar span. ROC uses
entry/exit hysteresis. ADX requires `+DI > -DI` and ADX above threshold and
exits when direction or strength fails.

- [ ] **Step 4: Run family and no-look-ahead tests**

```bash
.venv/bin/pytest packages/btc-backtest/tests/strategies/test_momentum.py packages/btc-backtest/tests/engine/test_no_lookahead.py -q
.venv/bin/ruff check packages/btc-backtest
.venv/bin/mypy --strict packages/btc-backtest/src packages/btc-backtest/tests
```

Expected: all pass.

- [ ] **Step 5: Commit and push**

```bash
git add packages/btc-backtest
git commit -m "feat(backtest): add momentum and ADX strategies"
git push origin codex/quantdinger-platform-upgrade
```

### Task 9: ATR, Keltner, And VWAP Strategies

**Files:**
- Create: `packages/btc-backtest/src/btc_backtest/strategies/volatility.py`
- Create: `packages/btc-backtest/src/btc_backtest/strategies/vwap.py`
- Create: `packages/btc-backtest/tests/strategies/test_volatility.py`
- Create: `packages/btc-backtest/tests/strategies/test_vwap.py`
- Modify: `packages/btc-backtest/src/btc_backtest/strategies/registry.py`

**Interfaces:**
- Produces: `ATRVolatilityBreakout`, `KeltnerChannel`, `VWAPMeanReversion`.
- Registers: `atr_volatility_breakout`, `keltner_channel`, `vwap_mean_reversion`.

- [ ] **Step 1: Write failing volatility/VWAP tests**

```python
def test_atr_breakout_uses_prior_close_plus_atr() -> None:
    strategy = ATRVolatilityBreakout({"atr_window": 3, "multiplier": 1.0})
    assert target_weights_from_ohlc(strategy, atr_breakout_fixture())[-1] == Decimal("1")


def test_keltner_enters_upper_channel_and_exits_center() -> None:
    intents = run_strategy(
        KeltnerChannel({"ema_window": 3, "atr_window": 2, "multiplier": 1.0}),
        keltner_cycle_fixture(),
    )
    assert [item.reason for item in intents] == ["keltner_breakout", "keltner_center_exit"]


def test_vwap_requires_positive_volume_and_reverts_to_center() -> None:
    strategy = VWAPMeanReversion({"window": 3, "entry_z": -1.0, "exit_z": 0.0})
    weights = target_weights_from_ohlc(strategy, vwap_fixture())
    assert Decimal("1") in weights and weights[-1] == Decimal("0")
```

- [ ] **Step 2: Run tests and verify RED**

```bash
.venv/bin/pytest packages/btc-backtest/tests/strategies/test_volatility.py packages/btc-backtest/tests/strategies/test_vwap.py -q
```

Expected: import failures for volatility and VWAP modules.

- [ ] **Step 3: Implement shifted thresholds and volume checks**

ATR breakout threshold uses the previous close and previous ATR. Keltner uses
EMA plus/minus ATR multiplier and a center-line exit. VWAP uses rolling
`sum(typical_price * volume) / sum(volume)`, rejects windows with zero volume,
and applies z-score entry/center exit hysteresis.

- [ ] **Step 4: Run focused families**

```bash
.venv/bin/pytest packages/btc-backtest/tests/strategies/test_volatility.py packages/btc-backtest/tests/strategies/test_vwap.py -q
.venv/bin/ruff check packages/btc-backtest
.venv/bin/mypy --strict packages/btc-backtest/src packages/btc-backtest/tests
```

Expected: all pass.

- [ ] **Step 5: Commit and push**

```bash
git add packages/btc-backtest
git commit -m "feat(backtest): add volatility and VWAP strategies"
git push origin codex/quantdinger-platform-upgrade
```

### Task 10: Bounded Grid Rebalancing

**Files:**
- Create: `packages/btc-backtest/src/btc_backtest/strategies/grid.py`
- Create: `packages/btc-backtest/tests/strategies/test_grid.py`
- Modify: `packages/btc-backtest/src/btc_backtest/strategies/registry.py`

**Interfaces:**
- Produces: `GridRebalance`, `GridParams`.
- Registers: `grid_rebalance`.

- [ ] **Step 1: Write failing bounded-inventory tests**

```python
def test_grid_places_levels_only_inside_configured_range() -> None:
    strategy = GridRebalance(
        {"lower": 80, "upper": 120, "levels": 5, "quote_per_level": 100}
    )
    intents = strategy.on_bar(context(close="100", cash="1000"))
    assert {item.limit_price for item in intents} == {
        Decimal("80"), Decimal("90"), Decimal("110"), Decimal("120")
    }


def test_grid_never_sells_more_than_spot_inventory() -> None:
    strategy = GridRebalance({"lower": 80, "upper": 120, "levels": 5})
    intents = strategy.on_bar(context(close="110", qty="0.1"))
    assert sum(item.base_quantity for item in intents if item.side == OrderSide.SELL) <= Decimal("0.1")
```

- [ ] **Step 2: Run tests and verify RED**

```bash
.venv/bin/pytest packages/btc-backtest/tests/strategies/test_grid.py -q
```

Expected: import failure for grid strategy.

- [ ] **Step 3: Implement idempotent grid order maintenance**

Validate `lower < upper`, `2 <= levels <= 100`, positive level quote, and
`max_inventory_weight <= 1`. Create deterministic level IDs, avoid duplicate
open orders, cancel levels outside a recomputed range, and cap buy/sell totals
by cash and inventory respectively.

- [ ] **Step 4: Run grid and accounting property tests**

```bash
.venv/bin/pytest packages/btc-backtest/tests/strategies/test_grid.py packages/btc-backtest/tests/engine/test_accounting_properties.py -q
.venv/bin/ruff check packages/btc-backtest
.venv/bin/mypy --strict packages/btc-backtest/src packages/btc-backtest/tests
```

Expected: inventory, cash, idempotency, and range tests pass.

- [ ] **Step 5: Commit and push**

```bash
git add packages/btc-backtest
git commit -m "feat(backtest): add bounded grid rebalancing"
git push origin codex/quantdinger-platform-upgrade
```

### Task 11: Funding-Basis Carry

**Files:**
- Create: `packages/btc-backtest/src/btc_backtest/strategies/carry.py`
- Create: `packages/btc-backtest/tests/strategies/test_carry.py`
- Modify: `packages/btc-backtest/src/btc_backtest/strategies/registry.py`

**Interfaces:**
- Produces: `FundingBasisCarry`, `FundingBasisCarryParams`.
- Registers: `funding_basis_carry`.
- Consumes: point-in-time `funding_rate`, spot close, and perpetual close.

- [ ] **Step 1: Write failing paired-leg and funding tests**

```python
def test_carry_opens_equal_spot_long_and_perpetual_short() -> None:
    strategy = FundingBasisCarry({"entry_apr": 0.10, "exit_apr": 0.03, "weight": 0.5})
    intents = strategy.on_bar(carry_context(funding_rate="0.0002", equity="10000"))
    assert [(item.instrument, item.side) for item in intents] == [
        ("spot", OrderSide.BUY), ("perpetual", OrderSide.SELL)
    ]
    assert intents[0].quote_amount == intents[1].quote_amount == Decimal("5000")


def test_carry_exits_both_legs_when_funding_falls() -> None:
    strategy = opened_carry_strategy()
    intents = strategy.on_bar(carry_context(funding_rate="0.00001", has_pair=True))
    assert {item.reason for item in intents} == {"carry_exit"}
    assert len(intents) == 2
```

- [ ] **Step 2: Run tests and verify RED**

```bash
.venv/bin/pytest packages/btc-backtest/tests/strategies/test_carry.py -q
```

Expected: import failure for carry strategy.

- [ ] **Step 3: Implement delta-neutral entry, funding, and exit**

Annualize the declared funding interval, require paired market timestamps,
open equal quote notionals atomically, accrue funding only at effective funding
timestamps, exit both legs on APR threshold, negative streak, missing required
data, or basis-risk cap. Reject directional leverage and unpaired fills.

- [ ] **Step 4: Run carry and accounting tests**

```bash
.venv/bin/pytest packages/btc-backtest/tests/strategies/test_carry.py packages/btc-backtest/tests/engine -q
.venv/bin/ruff check packages/btc-backtest
.venv/bin/mypy --strict packages/btc-backtest/src packages/btc-backtest/tests
```

Expected: paired legs, funding-once, exit, missing-data, and reconciliation tests pass.

- [ ] **Step 5: Commit and push**

```bash
git add packages/btc-backtest
git commit -m "feat(backtest): add funding basis carry"
git push origin codex/quantdinger-platform-upgrade
```

### Task 12: Benchmarks And QT-Specific Strategies

**Files:**
- Create: `packages/btc-backtest/src/btc_backtest/strategies/benchmarks.py`
- Create: `packages/btc-backtest/src/btc_backtest/strategies/qt_special.py`
- Create: `packages/btc-backtest/tests/strategies/test_benchmarks.py`
- Create: `packages/btc-backtest/tests/strategies/test_qt_special.py`
- Modify: `packages/btc-backtest/src/btc_backtest/strategies/registry.py`

**Interfaces:**
- Produces: `BuyAndHold`, `Capitulation`, `WickCatcher`.
- Registers extras separately from `BUILTIN_STRATEGY_IDS`.

- [ ] **Step 1: Write failing benchmark/parity tests**

```python
def test_buy_and_hold_buys_once_and_never_rebalances() -> None:
    intents = run_bars(BuyAndHold({}), daily_contexts("2024-01-01", periods=20))
    assert len(intents) == 1
    assert intents[0].reason == "initial_buy"


def test_qt_special_ids_do_not_change_top_twenty(registry) -> None:
    assert {"capitulation", "wick_catcher", "buy_and_hold"} <= set(registry.list())
    assert len(BUILTIN_STRATEGY_IDS) == 20
```

- [ ] **Step 2: Run tests and verify RED**

```bash
.venv/bin/pytest packages/btc-backtest/tests/strategies/test_benchmarks.py packages/btc-backtest/tests/strategies/test_qt_special.py -q
```

Expected: import failure for benchmark/special modules.

- [ ] **Step 3: Implement independent ports**

Port formulas and conservative defaults from the existing QT implementations
without importing QT. Capitulation declares each factor and availability
requirement. Wick catcher creates bounded deep-limit levels, macro veto, and
inventory caps. Add parity fixtures that feed identical normalized inputs to
old and new implementations and compare decisions.

- [ ] **Step 4: Run parity and boundary tests**

```bash
.venv/bin/pytest packages/btc-backtest/tests/strategies/test_benchmarks.py packages/btc-backtest/tests/strategies/test_qt_special.py tests/test_strategy_backtest.py tests/test_wick_catcher.py -q
.venv/bin/pytest packages/btc-backtest/tests/test_package_boundary.py -q
```

Expected: parity fixtures pass and `btc_backtest` contains no QT imports.

- [ ] **Step 5: Commit and push**

```bash
git add packages/btc-backtest
git commit -m "feat(backtest): port QT strategies and benchmarks"
git push origin codex/quantdinger-platform-upgrade
```

### Task 13: Golden Matrix, Sensitivity, And Catalog CLI

**Files:**
- Create: `packages/btc-backtest/tests/strategies/golden/`
- Create: `packages/btc-backtest/tests/strategies/test_golden_matrix.py`
- Create: `packages/btc-backtest/tests/strategies/test_sensitivity.py`
- Modify: `packages/btc-backtest/src/btc_backtest/cli.py`
- Modify: `packages/btc-backtest/tests/test_cli.py`

**Interfaces:**
- Produces: stable golden summaries for 23 registered strategies.
- Produces CLI `strategies list`, `strategies describe`, and `run`.

- [ ] **Step 1: Write failing complete-matrix test**

```python
@pytest.mark.parametrize("strategy_id", BUILTIN_STRATEGY_IDS)
def test_builtin_matches_reviewed_golden(strategy_id, registry, golden_dataset) -> None:
    result = run_builtin(strategy_id, registry, golden_dataset)
    expected = load_golden(strategy_id)
    assert canonical_summary(result) == expected


@pytest.mark.parametrize("strategy_id", BUILTIN_STRATEGY_IDS)
def test_every_builtin_passes_shared_contract(strategy_id, registry, context) -> None:
    assert_strategy_contract(registry.create(strategy_id, {}), context)


@pytest.mark.parametrize("strategy_id", BUILTIN_STRATEGY_IDS)
def test_small_parameter_perturbation_stays_finite(strategy_id, registry, golden_dataset) -> None:
    for params in nearby_parameter_sets(registry.describe(strategy_id)):
        result = run_builtin(strategy_id, registry, golden_dataset, params)
        assert all(math.isfinite(value) for value in result.equity_values)
```

- [ ] **Step 2: Run matrix and verify RED**

```bash
.venv/bin/pytest packages/btc-backtest/tests/strategies/test_golden_matrix.py packages/btc-backtest/tests/strategies/test_sensitivity.py -q
```

Expected: missing golden fixtures and any remaining unregistered implementation fail.

- [ ] **Step 3: Review and add deterministic golden fixtures**

Use one versioned 400-bar OHLCV fixture with trend, range, crash, rebound, and
funding regimes. Store canonical JSON containing final equity, order/fill
counts, first/last action, max exposure, and event digest for each strategy.
Add CLI JSON/table output from registry metadata; `run` accepts a built-in ID
and delegates to `BacktestRunner`.

- [ ] **Step 4: Run strategy exit gate**

```bash
.venv/bin/pytest packages/btc-backtest/tests/strategies packages/btc-backtest/tests/test_cli.py -q
.venv/bin/ruff check packages/btc-backtest
.venv/bin/mypy --strict packages/btc-backtest/src packages/btc-backtest/tests
git diff --check
```

Expected: all twenty plus three extras pass contract, golden, sensitivity, and CLI tests.

- [ ] **Step 5: Commit and push**

```bash
git add packages/btc-backtest
git commit -m "test(backtest): verify complete strategy catalog"
git push origin codex/quantdinger-platform-upgrade
```

## Strategy Exit Gate

Run:

```bash
.venv/bin/pytest packages/btc-backtest/tests/strategies -q
.venv/bin/pytest packages/btc-backtest/tests/engine/test_no_lookahead.py -q
.venv/bin/ruff check packages/btc-backtest
.venv/bin/mypy --strict packages/btc-backtest/src packages/btc-backtest/tests
btc-backtest strategies list --format json
git diff --check
git status --short --branch
```

Evidence required:

- exactly 20 unique common IDs in the specified order;
- 20 registered, constructible implementations;
- 3 separate extras;
- every built-in passes contract, golden, sensitivity, entry/exit/no-trade,
  insufficient-data, and no-look-ahead coverage;
- current branch is committed, pushed, and clean.
