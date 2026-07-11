# Quant Learning Center Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a cited, beginner-oriented quantitative-finance curriculum at the QT dashboard route `/learn`.

**Architecture:** Keep the dashboard dependency-free and server-rendered. Put immutable curriculum data, validation, and HTML rendering in a focused `qt.dashboard.learning` module; keep `server.py` limited to HTTP routing and navigation wiring.

**Tech Stack:** Python 3.10+, standard-library dataclasses and HTML escaping, pytest, Ruff, mypy, Playwright CLI for visual verification.

## Global Constraints

- The page is educational material, not personalized investment advice.
- The page must not recommend leverage, live trading, or a specific capital allocation.
- Every learning module must cite at least one registered HTTPS source.
- Evidence types must distinguish foundational research, official curriculum, practitioner experience, and project documentation.
- No JavaScript, new runtime dependency, user input, cookies, analytics, or persistence.
- The page must remain readable at 360 px and keyboard navigable.

---

### Task 1: Curriculum integrity and page contract

**Files:**
- Create: `tests/test_dashboard_learning.py`

**Interfaces:**
- Consumes: existing `served_dashboard` fixture and `_get()` helper from `tests/test_dashboard_strategies.py` are not imported because pytest test modules are not application interfaces.
- Produces: executable expectations for `LearningSource`, `LearningModule`, `SOURCES`, `MODULES`, `validate_curriculum()`, `render_learning_page()`, `/learn`, and home navigation.

- [ ] **Step 1: Write failing curriculum tests**

Create local HTTP fixture code using `DashboardContext` and `_make_handler`, then add tests equivalent to:

```python
def test_curriculum_is_complete_and_cited() -> None:
    validate_curriculum(MODULES, SOURCES)
    assert [module.number for module in MODULES] == list(range(1, 9))
    assert all(module.concepts for module in MODULES)
    assert all(module.experience_notes for module in MODULES)
    assert all(module.qt_exercise for module in MODULES)
    assert all(module.source_ids for module in MODULES)


def test_curriculum_rejects_unknown_citation() -> None:
    invalid = replace(MODULES[0], source_ids=("missing",))
    with pytest.raises(ValueError, match="unknown source"):
        validate_curriculum((invalid,), SOURCES)
```

- [ ] **Step 2: Write failing rendering and route tests**

Test that rendered HTML and live HTTP responses contain all module titles,
`Learn · Derive · Apply · Challenge`, `12-week`, `not investment advice`, a
viewport meta tag, bibliography anchors, and safe external-link attributes.
Test that `/` contains `href="/learn"` and `/learn` returns status 200 with an
HTML content type.

- [ ] **Step 3: Run tests and verify RED**

Run:

```bash
.venv/bin/pytest tests/test_dashboard_learning.py -q
```

Expected: collection fails because `qt.dashboard.learning` does not exist.

### Task 2: Typed curriculum and responsive renderer

**Files:**
- Create: `src/qt/dashboard/learning.py`
- Test: `tests/test_dashboard_learning.py`

**Interfaces:**
- Consumes: only Python standard-library modules.
- Produces:
  - `LearningSource(id, title, publisher, year, url, evidence_type)`
  - `LearningModule(number, title, outcome, concepts, experience_notes, qt_exercise, source_ids)`
  - immutable `SOURCES` and `MODULES` tuples
  - `validate_curriculum(modules, sources) -> None`
  - `render_learning_page() -> str`

- [ ] **Step 1: Implement immutable models and source registry**

Define frozen dataclasses with strictly typed fields. Add the twelve sources
approved in the design specification with concise evidence labels and direct
HTTPS URLs.

- [ ] **Step 2: Implement all eight learning modules**

Each module contains a precise outcome, at least six concepts, at least two
practitioner cautions, one QT exercise, and relevant source IDs. The modules
follow the approved prerequisite order:

```python
MODULE_TITLES = (
    "Financial system and instruments",
    "Probability and statistics",
    "Data and time series",
    "Portfolio and risk architecture",
    "Strategy research",
    "Backtesting and validation",
    "Crypto-specific market structure",
    "Execution and operating discipline",
)
```

- [ ] **Step 3: Implement fail-fast curriculum validation**

Reject duplicate source IDs, duplicate module numbers, non-HTTPS URLs, modules
without citations, and unknown citations with specific `ValueError` messages.

- [ ] **Step 4: Implement escaped semantic HTML rendering**

Render a complete document containing:

- skip link and semantic header/main/footer;
- concise disclaimer and start-here guidance;
- sticky module rail on wide viewports;
- four-step learning method and 12-week schedule;
- eight native `<details>` module sections, open by default for the first;
- evidence-type labels and linked citations;
- full bibliography;
- visible paper-first final reminder;
- responsive CSS, clear `:focus-visible`, and reduced-motion handling.

Use `html.escape(..., quote=True)` for all curriculum text and attribute values.
Render external links with `target="_blank" rel="noopener noreferrer"`.

- [ ] **Step 5: Run focused tests and verify GREEN**

Run:

```bash
.venv/bin/pytest tests/test_dashboard_learning.py -q
```

Expected: model and direct-renderer tests pass; HTTP navigation tests remain
failing until Task 3.

### Task 3: HTTP route, navigation, and project status

**Files:**
- Modify: `src/qt/dashboard/server.py`
- Modify: `docs/ROADMAP.md`
- Test: `tests/test_dashboard_learning.py`

**Interfaces:**
- Consumes: `render_learning_page() -> str` from Task 2.
- Produces: `GET /learn` and a home-header link to that route.

- [ ] **Step 1: Wire the route**

Import the renderer and add this branch immediately after the home route:

```python
if path == "/learn":
    self._send_html(render_learning_page())
    return
```

- [ ] **Step 2: Add discoverable navigation**

Change the home header utility links to include:

```html
<a href="/learn">Learn</a> · <a href="/portfolio">P&amp;L →</a>
```

- [ ] **Step 3: Run focused tests and verify GREEN**

Run:

```bash
.venv/bin/pytest tests/test_dashboard_learning.py tests/test_dashboard_strategies.py -q
```

Expected: all focused dashboard tests pass.

- [ ] **Step 4: Update the roadmap honestly**

Add a delivered learning-center row only after focused tests pass. State that
the first release is read-only and includes eight modules, a 12-week path,
evidence labels, project exercises, and `/learn`.

### Task 4: Full verification and delivery

**Files:**
- Modify only files required to fix failures originating from Tasks 1–3.

**Interfaces:**
- Consumes: completed learning center.
- Produces: verified implementation commit pushed to `origin/main`.

- [ ] **Step 1: Run automated verification**

Run:

```bash
.venv/bin/pytest -q
.venv/bin/ruff check .
.venv/bin/mypy src tests
git diff --check
```

Expected: all commands exit 0.

- [ ] **Step 2: Run browser verification**

Start the dashboard on localhost, verify `npx` exists, then use the Playwright
CLI wrapper to open `/learn`, snapshot the accessibility tree, inspect desktop
and 360 px screenshots, follow the home navigation link, and confirm module
expansion plus keyboard focus. Store temporary screenshots outside tracked
source or under ignored `output/playwright/`.

- [ ] **Step 3: Review actual diff against acceptance criteria**

Confirm every approved acceptance criterion maps to either an automated test or
the recorded browser check. Remove unused imports and inspect `git diff --stat`,
`git diff`, and `git status`.

- [ ] **Step 4: Commit and push**

Stage only the learning-center implementation, tests, and roadmap. Commit with
a concise conventional subject and a body listing functionality and exact
validation results, then push the configured current branch. If push fails,
leave the local commit intact and report the exact failure.

