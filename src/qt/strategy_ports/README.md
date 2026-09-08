# btc-qt strategy-port integration contract

`create_btcqt_port("btcqt_s0_trend")`,
`create_btcqt_port("btcqt_s1_wick_confirm")`,
`create_btcqt_port("btcqt_s1_wick_ladder")`, and
`create_btcqt_port("btcqt_s2_crowding_fader", {"enabled": true})` construct
the original `qt.legacy.btcqt` strategy classes. They are stateful and must be
created once per research run.

The native engine must:

1. Feed source events into `BtcqtIndicatorStateBuilder` (which owns the
   original `qt.legacy.btcqt.indicators.engine.IndicatorEngine`) in observed
   time order, with each event's `available_at`. The builder queues an event
   until both timestamps are eligible for the next closed candle. Call
   `on_candle()` only after the candle closes, wrap its result in `CausalState`,
   and include a versioned `DataVersion` for every required entry in
   `port.metadata.required_data`.
2. Call `port.decide(timeline, timestamp=bar_close, equity=equity)` after the
   candle closes. `timeline.at()` exposes only snapshots whose `available_at`
   is no later than that close.
3. Submit each returned legacy `Intent` with the original action semantics.
   Feed every actual native fill back as legacy `Fill` through
   `port.on_fill(fill, causal_state)`, then submit its follow-up intents.

`port.order_intents(decision)` is a narrow compatibility path. It produces
true `btc_backtest.engine.models.OrderIntent` values for market and stop
commands only: long entries/exits use spot and short entries/exits use the
perpetual instrument, matching the current accounting protocol. It explicitly
rejects S1 IOC entries, ladder replacement, cancellation, and take-profit
commands because the current btc-backtest protocol cannot express time-in-
force, reduce-only, cancel, or fill callbacks. The Nautilus bridge therefore
needs this port's state/fill hook rather than flattening those commands into
normal limit orders.

Required input identities are declared in `BTCQT_PORTS`; absent S2 funding or
open-interest inputs raise `MissingRequiredDataError` rather than becoming a
zero percentile or synthetic signal.

Run the focused checks with:

```sh
./.venv/bin/python -m pytest tests/strategy_ports -q
./.venv/bin/python -m ruff check src/qt/strategy_ports tests/strategy_ports
```

## btc-quant Freqtrade-family contract

`create_freqtrade_port()` accepts `btc_quant_catalog` (with an explicit
`profile_id`), `btc_atr_fear_volume`, `btc_donchian_atr`,
`btc_low_freq_trend`, `btc_multisource_regime`, and `btc_sota`. Catalog profile
selection is per port instance; it never reads or writes `FREQTRADE_PROFILE`.

Call `port.decide(FreqtradeCausalFrame(...), equity=..., max_stake=...)` at a
closed 4-hour candle. The native engine must retain the returned `protections`
policy and carry a `FreqtradeProtectionState` across calls. Call
`on_trade_closed(state, closed_at=..., equity_after=...)` on every completed
trade: its executable cooldown and rolling maximum-drawdown rules suppress
new entry orders, while exits and stops remain available.

Submit `enter_long` and `exit_long` orders with their `order_id`, then call
`on_fill()` for every actual partial fill with its order ID, fill price,
quantity, timestamp, and quote fees. It retains average entry price, remaining
quantity, accumulated fees, and a closed zero-quantity state. Immediately
submit `initial_stop_order()` after the first entry fill and confirm it with
`on_stop_accepted()`. Later decisions emit `replace_stop`, which requires
native cancel-and-replace support. Sota retains its first ATR stop; other
families calculate the source max(current ATR stop, entry-rate/current-ATR
stop). The adapter then enforces a monotonic accepted-stop boundary because a
native stop cannot be loosened. All source hard stoploss values (-25%, except
LowFreqTrend at -35%) are active on the fill state. `minimal_roi={"0": 100}`
is enforced by `roi_exit_reached()` and therefore adds no normal synthetic ROI
exit.

`BtcLowFreqTrend` has no source `custom_stake_amount`; its adapter therefore
requires the engine's validated `proposed_stake` when it produces an entry and
passes that amount through unchanged. The other families use their original
ATR-risk sizing (and Sota's volatility cap).

`btc_atr_fear_volume`, `btc_donchian_atr`, and `btc_low_freq_trend` require
the real `talib.abstract` module because their source strategies use TA-Lib.
The supplied Python environments currently do not provide it, so evaluating
those ports raises `FreqtradeRuntimeUnavailableError`; no handwritten TA
approximation or Freqtrade fallback class is used.

For the external-evidence strategies, every active feature row must have an
`available_at` (and each source-specific `*_available_at`) no later than the
decision time. Missing or late values are a validation failure, never zeros.

The workbench's existing `CatalogProfileStrategy` is not an
execution-equivalent replacement: the integration owner should call
`create_freqtrade_port()` for catalog runs and preserve returned sizing, stop,
ROI, and protection metadata. This package intentionally does not edit the
workbench.

## Qt5 gallery contract

`create_gallery_port()` exposes nine original Qt5 strategies under separate,
non-interchangeable interfaces. The five live ports (`qt5_smart_dca`,
`qt5_capitulation`, `qt5_weekly_trend`, `qt5_basis_carry`, and
`qt5_wick_catcher`) call the original signal-generator classes and return a
`GalleryLiveDecision` containing the original `EvaluationResult` and optional
`Opportunity`. They do not create native orders: SmartDCA and Capitulation are
operator buy proposals; WeeklyTrend is an open/close proposal; BasisCarry is a
paired spot-long/perp-short proposal; WickCatcher contains its original limit
ladder and take-profit prices in `Opportunity.details`.

The four simulator ports (`qt5_sim_smart_dca`, `qt5_sim_weekly_trend`,
`qt5_sim_basis_carry`, and `qt5_sim_wick_catcher`) call their original batch
`run()` implementations and return `GallerySimulationDecision.result` as the
source `StrategyResult`, including its source equity, trades, diagnostics, and
long/short weights. They are research replays, not a native execution bridge.

Construct `GalleryCausalInput(data=..., decision_at=..., available_at=...,
inputs=(DataVersion(...),))`; time-indexed frames and series are copied and
clipped at `decision_at`. Supplied live data must be versioned, while an absent
live input intentionally retains each source strategy's own `watch` result.
Batch ports require their metadata-declared inputs and never synthesize
funding or missing OHLCV.

Run the focused tests with:

```sh
./.venv/bin/python -m pytest tests/strategy_ports/test_gallery.py -q
```
