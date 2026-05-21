# QT

QT is a BTC quantitative trading system focused on real-time detection of
black-swan crashes and wick-driven extreme volatility.

The goal is to turn market stress signals into risk-controlled trading decisions
that can be researched, backtested, simulated, and eventually executed in live
markets with clear monitoring and failure controls.

## Scope

- Detect sudden crash regimes, liquidation cascades, and abnormal wick events.
- Generate trade signals with explicit position sizing, stop-loss, and exposure
  limits.
- Backtest strategies against historical market data before paper or live use.
- Support paper trading first, then live trading after strategy validation.
- Monitor PnL, slippage, drawdown, liquidity, exchange health, and API failures.

## System Design Targets

- **Data pipeline**: ingest market data, normalize events, and preserve raw
  observations for replay.
- **Signal engine**: combine volatility, liquidity, price action, and execution
  stress indicators into actionable signals.
- **Risk engine**: enforce max drawdown, max position size, cooldown windows,
  stop-loss rules, and emergency kill switches.
- **Backtesting**: replay historical scenarios with fees, slippage, latency, and
  partial fills modeled explicitly.
- **Execution**: isolate broker or exchange adapters from strategy logic so paper
  and live trading use the same decision path.
- **Observability**: expose strategy state, account state, order lifecycle, and
  infrastructure failures in one operational view.

## Development Status

This repository currently defines the product and system requirements. The first
implementation milestone should establish the project structure, data model,
backtest runner, and a paper-trading execution path before any live trading
integration is added.

## Safety

QT is intended for research and controlled execution. Live trading should remain
disabled until strategies have passed repeatable backtests, paper-trading
validation, risk-limit reviews, and exchange failure-mode testing.
