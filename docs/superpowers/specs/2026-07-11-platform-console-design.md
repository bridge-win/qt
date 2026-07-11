# Platform Console Design

## Goal

Make the QT website the operator-facing surface for understanding and accessing every major quantitative workflow: platform usage, capabilities, monitoring, paper trading, strategy detail, intelligence, portfolio P&L, and a safe trading demo.

## Scope

- Add `/platform` as the website hub and product guide.
- Add `/demo` as a deterministic, paper-only trading walkthrough.
- Add `/api/demo` for the same demo state in machine-readable form.
- Update dashboard navigation so a user can reach the whole platform from the website.
- Keep the existing dependency-free, server-rendered Python dashboard.
- Do not touch the Follow deployment or port allocation.

## User Experience

`/platform` explains how to use QT in five operator steps: learn the system, inspect data/health, review intelligence, watch paper P&L, and only then consider live-readiness. It also lists platform capabilities with links to the actual routes, plus a production-readiness checklist that makes paper mode, risk gates, alerting, deployment, and live-trading guardrails explicit.

`/demo` shows one reproducible paper trade using synthetic BTC data. The page must clearly say it sends no live order, then show signal evidence, risk decision, paper fill, and ledger result. The demo should work even when exchange APIs are blocked from the Aliyun region.

## Architecture

- Create `src/qt/dashboard/platform.py` for static capability data, demo construction, and HTML rendering.
- Keep `src/qt/dashboard/server.py` responsible for HTTP routing only.
- Use existing domain code where useful: deterministic synthetic BTC data, `RiskEngine`, `PaperBroker`, `Signal`, `Position`, and `Order`.
- Return JSON from `/api/demo` with the exact demo snapshot rendered by `/demo`.

## Production Posture

This is a production-capable paper-trading dashboard, not a claim of live-trading readiness. The website must surface that distinction. Live trading remains gated by `docs/live-checklist.md`, environment configuration, live broker guardrails, and exchange API key restrictions.

## Validation

- Failing tests first for platform data, demo state, HTML rendering, HTTP routes, JSON API, and home navigation.
- Focused dashboard tests.
- Full pytest and Ruff.
- Targeted Mypy for new/changed modules.
- Deploy with `deploy/ssh-deploy.sh`.
- Verify public Aliyun URL on port `8765`.
