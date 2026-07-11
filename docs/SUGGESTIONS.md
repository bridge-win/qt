# Code Review & Future Suggestions Spec

A prioritized, code-grounded backlog produced from a review of the dashboard
and surrounding modules. Each item lists **what**, **why**, **where**, and a
rough **effort**. Items are ordered by value-to-effort within each tier.

The review that produced this spec also **shipped one change** (see
[§ Addressed in this change](#addressed-in-this-change)) so the highest-value,
lowest-risk item is already done and the rest have a working pattern to follow.

---

## Tier 1 — high value, low risk

### 1.1 Fold the strategy-detail page into the shared shell ✅ pattern established
- **What:** `_render_strategy_detail` (`src/qt/dashboard/server.py`) still
  carries its own `<head>`/`<style>` block and has **no top navigation**, so a
  user on `/strategy/<name>` can't reach Learn/Intel/P&L without editing the
  URL.
- **Why:** The other four full pages now share `_page()`; the detail page is
  the last duplicate. Folding it in removes ~25 lines of CSS and gives it the
  nav bar for free.
- **Caveat:** it relies on `.mono { white-space: pre-wrap }` for its JSON
  blocks. Add a `.mono.block { white-space: pre-wrap }` rule to `_PAGE_STYLE`
  and use `<div class="mono block">` for those blocks, then switch the page to
  `_page(...)`.
- **Effort:** ~30 min.

### 1.2 Cache per-request reads with a short TTL
- **What:** Every `GET` re-reads the Parquet catalog and JSON state from disk
  (`_sources`, `_monitor`, `_strategies`, `_portfolios`, `_intel` in
  `server.py`). The home page reads **all of them on every hit**, and the page
  auto-refreshes every 60s.
- **Why:** Under `ThreadingHTTPServer`, concurrent tabs multiply disk I/O for
  data that changes at most once per cycle. A 2–5s TTL memoization per reader
  would cut I/O by orders of magnitude with no staleness a human would notice.
- **Where:** wrap the `_sources/_monitor/...` helpers in a tiny time-boxed
  cache keyed by `DashboardContext`.
- **Effort:** ~1–2 hrs (plus a test that the cache expires).

### 1.3 Replace full-page meta-refresh with JSON polling
- **What:** Pages use `<meta http-equiv="refresh" content="60">`, re-fetching
  the entire HTML document every minute.
- **Why:** The `/api/*` endpoints already return the same data as JSON. A ~30
  lines of vanilla JS that polls `/api/monitor`, `/api/strategies`, etc. and
  patches the DOM would be lighter, flicker-free, and keep scroll position.
- **Where:** `_PAGE_STYLE`/`_page()` could grow an optional `<script>` slot.
- **Effort:** ~half a day. Keep meta-refresh as a `<noscript>` fallback.

### 1.4 Single-source the learning content
- **What:** The Learn page content exists both as `docs/LEARNING.md` (detailed)
  and as `_LEARN_LAYERS/_LEARN_REFS/...` data in `server.py` (summarized).
- **Why:** Two copies drift. Options, cheapest first: (a) accept the split and
  add a test asserting the page's reference count matches the doc's; (b) move
  the page data into a `learning.py` data module imported by both a doc
  generator and the server; (c) parse the doc at render time.
- **Recommendation:** (b) — keep the summarized data as the single source and
  generate the doc's tables from it, so the page stays fast and the doc stays
  in sync.
- **Effort:** ~2–3 hrs.

---

## Tier 2 — structural / maintainability

### 2.1 Turn `do_GET` into a dispatch table
- **What:** Routing is a long `if path == ... return` chain in
  `_make_handler.do_GET`.
- **Why:** A `dict[str, Callable]` for exact paths plus an ordered list of
  `(prefix, handler)` for the `/strategy/`, `/api/strategy/` families would be
  easier to test in isolation and harder to leave a route half-wired.
- **Effort:** ~2 hrs; behavior-preserving, covered by existing route tests.

### 2.2 Extract a `views` module from `server.py`
- **What:** `server.py` is ~900 lines mixing HTTP handling, data reads, and
  ~20 `_render_*` HTML builders.
- **Why:** Splitting `dashboard/views.py` (pure `data -> str`) from
  `dashboard/server.py` (HTTP) makes the renderers trivially unit-testable
  (they already are pure functions) and shrinks the HTTP surface.
- **Effort:** ~half a day (mechanical move + import updates).

### 2.3 Add `prefers-color-scheme` (dark mode)
- **What:** `_PAGE_STYLE` pins `color-scheme: light` and light hex values.
- **Why:** A `@media (prefers-color-scheme: dark)` block overriding the CSS
  variables is a small, self-contained win for a monitoring UI people leave
  open. All colors are already variables, so it's a one-block change.
- **Effort:** ~1 hr.

### 2.4 Add `/healthz` and a favicon route
- **What:** No liveness endpoint; `/favicon.ico` 404s on every page load.
- **Why:** `/healthz` returning `200 {"ok": true}` helps the watchdog and any
  reverse proxy; a 1×1 favicon (or 204) silences noise.
- **Effort:** ~30 min.

---

## Tier 3 — robustness & polish

### 3.1 Security/response headers
- Add `Cache-Control: no-store` on `/api/*`, and static `Cache-Control` on the
  (soon) versioned CSS. Consider a minimal `Content-Security-Policy` since the
  UI is self-contained. The dashboard binds `127.0.0.1` by default (good) but
  `serve_dashboard` accepts `0.0.0.0`; document the exposure and keep any live
  controls off the network surface.

### 3.2 Graceful bind failure
- `serve_dashboard` will raise `OSError: address already in use` with a bare
  traceback. Catch it and print an actionable message (port, how to change it).

### 3.3 Snapshot tests for pages
- The `_render_*` functions are pure; add golden-file snapshot tests (store
  expected HTML, diff on change) to catch accidental layout regressions cheaply.

### 3.4 Strategy-detail nav parity
- Covered by 1.1, called out separately because it's a UX papercut: the detail
  page is currently a navigational dead-end except for the two hand-rolled
  `← back` / `P&L` links.

---

## Non-dashboard observations

These come from the README/architecture rather than a line-by-line read of
every module; treat as leads to confirm, not confirmed defects.

- **Live-trading safety** is already gated (`QT_LIVE_TRADING_ENABLED`, kill
  switch, `docs/live-checklist.md`). Keep the invariant that *no* code path
  enables it implicitly; a test asserting `LiveBroker.submit` raises unless the
  flag is set would lock that in. (Check whether
  `tests/test_live_broker_guardrails.py` already covers this and extend if not.)
- **Backtest trust:** `docs/ROADMAP.md` makes walk-forward + Monte Carlo a
  precondition for capital. Consider a CI check that fails if a strategy's
  exported `summary.json` lacks the walk-forward/Monte-Carlo provenance fields,
  so "validated" can't be claimed without the artifacts.
- **Data hygiene:** the `ParquetStore` replay design is the right call. A
  targeted test that a signal computed at time *t* never reads a bar dated
  `> t` (an explicit look-ahead guard) would protect the most expensive class
  of bug in this domain.
- **Typing debt:** `mypy` reports pre-existing `Class cannot subclass
  "BaseModel"` errors across `core/config.py` and the strategies — install
  `types-PyYAML` and the pydantic mypy plugin (or add per-module ignores) so
  real type errors aren't buried in known noise.

---

## Learning-page follow-ups (from the second pass)

### 4.1 Link glossary terms from the live pages
- **What:** The `/learn` glossary defines funding rate, drawdown, Sharpe,
  etc. The strategy and intel pages use those exact words with no link back.
- **Why:** For a beginner operator, a `<a href="/learn#glossary">funding
  rate</a>` (or a tooltip) turns every dashboard page into a teaching
  surface. Requires adding `id=` anchors to the learn-page sections first.
- **Effort:** ~1–2 hrs.

### 4.2 Keep `_LEARN_METHODS` and `docs/RESEARCH-EARNING.md` in sync
- **What:** The verified-methods cards condense RESEARCH-EARNING.md by hand.
- **Why:** When the research doc gains/retires a method, the page will drift.
  A cheap guard: a test asserting each `_LEARN_METHODS` name appears in the
  research doc (string containment), failing loudly on drift.
- **Effort:** ~30 min.

### 4.3 Progress tracking for the study plan
- **What:** The 6-month plan and 5-step beginner path are static tables.
- **Why:** A tiny `localStorage`-backed checkbox per row (no backend) would
  let the operator track progress across visits — high beginner value, zero
  server change.
- **Effort:** ~2 hrs (needs the `<script>` slot from item 1.3).

---

## Addressed in this change

- **Extracted a shared page shell** (`_PAGE_STYLE`, `_page()`,
  `_top_nav()`), removing duplicated `<head>`/`<style>` blocks from the Home,
  Intelligence, and Portfolio pages and giving every page a consistent top
  navigation bar. (Tier 1.1's pattern; the strategy-detail page remains as
  the last follow-up.)
- **Added the `/learn` page** — a summarized, citation-backed quant-learning
  curriculum backed by `docs/LEARNING.md`.
- **Second pass (beginner + earning focus):** added to `/learn` and
  `docs/LEARNING.md` — a 5-step "start here" path for someone with little
  knowledge, a bilingual 10-term plain-language glossary, and a
  **verified-methods-that-earn** section (funding carry, capitulation buying,
  wick catching, episodic dislocations, and the explicit do-not-attempt speed
  arbitrage) with realistic returns, failure modes, and citations condensed
  from `docs/RESEARCH-EARNING.md`.
- **Added tests** (`tests/test_dashboard_learn.py`) for the page, shell,
  navigation, beginner on-ramp, and verified-methods sections.
