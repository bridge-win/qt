# Platform Console Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expand QT's existing dashboard into a website console with a platform guide, complete capability map, and safe paper trading demo.

**Architecture:** Add a focused `qt.dashboard.platform` module for capability metadata, demo construction, and HTML rendering. Wire `/platform`, `/demo`, and `/api/demo` into the current dependency-free HTTP server while keeping existing routes stable.

**Tech Stack:** Python 3.10+, `http.server`, server-rendered HTML, existing QT risk/execution/backtest domain modules, pytest.

## Global Constraints

- Keep the website dependency-free and server-rendered.
- Do not send live orders from the demo.
- Use deterministic synthetic data so the demo works without exchange network access.
- Keep all existing routes working.
- Deploy only to `/opt/qt` on port `8765`; do not modify Follow under `/srv/kol`.

---

### Task 1: Platform and Demo Contract

**Files:**
- Create: `tests/test_dashboard_platform.py`

**Interfaces:**
- Produces expectations for `PLATFORM_CAPABILITIES`, `build_trading_demo()`, `render_platform_page()`, `render_demo_page()`, `/platform`, `/demo`, `/api/demo`, and home navigation.

- [ ] **Step 1: Write failing tests**

Run: `.venv/bin/pytest tests/test_dashboard_platform.py -q`

Expected: FAIL because `qt.dashboard.platform` does not exist.

### Task 2: Platform Renderer and Demo Snapshot

**Files:**
- Create: `src/qt/dashboard/platform.py`
- Modify: `src/qt/dashboard/server.py`

**Interfaces:**
- `PLATFORM_CAPABILITIES: tuple[PlatformCapability, ...]`
- `build_trading_demo() -> dict[str, object]`
- `render_platform_page() -> str`
- `render_demo_page() -> str`

- [ ] **Step 1: Implement module**

Create capability metadata and a deterministic demo using `synthetic_btc_ohlcv`, `RiskEngine`, `PaperBroker`, `Signal`, `Position`, and `Order`.

- [ ] **Step 2: Wire routes**

Add `GET /platform`, `GET /demo`, and `GET /api/demo` to `_make_handler()`.

- [ ] **Step 3: Update home navigation**

Add `Platform` and `Demo` links to the home header.

- [ ] **Step 4: Run focused tests**

Run: `.venv/bin/pytest tests/test_dashboard_platform.py tests/test_dashboard_learning.py tests/test_dashboard_strategies.py -q`

Expected: PASS.

### Task 3: Validate, Deploy, Review, Push

**Files:**
- Review all changed files.

- [ ] **Step 1: Validation**

Run:

```bash
.venv/bin/pytest -q
.venv/bin/ruff check .
.venv/bin/mypy src/qt/dashboard/platform.py tests/test_dashboard_platform.py
git diff --check
```

- [ ] **Step 2: Deploy**

Run: `deploy/ssh-deploy.sh`

Expected: remote localhost and public checks return HTTP 200.

- [ ] **Step 3: Review and commit**

Inspect `git diff`, stage only the platform-console files, commit, and push `main` to origin.
