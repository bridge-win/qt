"""Semantic capability inventory distinct from source-file provenance.

`migration-manifest.json` answers where a source file came from. This module
answers what an operator can actually read, edit, run, and verify. A source
archive never becomes a verified capability merely because it can be listed.
"""

from __future__ import annotations

import ast
from dataclasses import asdict, dataclass
from importlib.util import find_spec
from pathlib import Path
from typing import Literal

from btc_backtest.strategies.registry import default_strategy_registry

from qt.data.catalog import DATA_SOURCES
from qt.legacy.btc_quant_evolution.external_data.providers import all_providers
from qt.workbench.capabilities import manifest
from qt.workbench.catalog import legacy_catalog

CapabilityStatus = Literal[
    "behavior_verified",
    "implemented_pending_behavior_verification",
    "source_ported_requires_legacy_runtime",
    "source_ported_pending_adapter",
]

_PACKAGE_ROOT = Path(__file__).resolve().parents[1]
_QT_SOURCE_AREAS = (
    ("indicator", "indicators"),
    ("data_component", "data"),
    ("backtest_component", "backtest"),
    ("research_component", "research"),
    ("scanner", "intel"),
    ("risk_component", "risk"),
    ("execution_component", "execution"),
    ("portfolio_component", "portfolio"),
    ("runtime_component", "monitoring"),
    ("runtime_component", "platform"),
)


@dataclass(frozen=True)
class Capability:
    id: str
    kind: str
    name: str
    source: str
    entry_point: str
    readable: bool
    editable: bool
    runnable: bool
    execution_status: str
    behavior_test_status: str
    data_status: str
    migration_status: CapabilityStatus
    notes: str


def capability_matrix() -> list[dict[str, object]]:
    """Return every semantic capability known to the current workbench build."""

    rows: list[Capability] = [
        *_btc_backtest_strategies(),
        *_qt_strategies(),
        *_qt_indicator_families(),
        *_qt_data_sources(),
        *_qt_runtime_operations(),
        *_legacy_catalog_capabilities(),
        *_legacy_freqtrade_strategies(),
        *_legacy_btcqt_event_strategies(),
    ]
    if len({row.id for row in rows}) != len(rows):
        raise ValueError("capability matrix contains duplicate IDs")
    return [asdict(row) for row in rows]


def source_audit() -> list[dict[str, object]]:
    """Return the non-product source symbol appendix for provenance review.

    A public Python symbol is a useful migration audit record, but is not a
    separate operator-facing capability or a claim that it has its own route.
    """

    rows = [*_qt_public_source_capabilities(), *_legacy_source_capabilities()]
    if len({row.id for row in rows}) != len(rows):
        raise ValueError("source audit contains duplicate IDs")
    return [asdict(row) for row in rows]


def capability_summary() -> dict[str, int]:
    rows = capability_matrix()
    return {
        "total_semantic_capabilities": len(rows),
        "behavior_verified": sum(row["migration_status"] == "behavior_verified" for row in rows),
        "pending_behavior_verification": sum(
            row["migration_status"] == "implemented_pending_behavior_verification" for row in rows
        ),
        "requires_legacy_runtime": sum(
            row["migration_status"] == "source_ported_requires_legacy_runtime" for row in rows
        ),
        "pending_adapter": sum(
            row["migration_status"] == "source_ported_pending_adapter" for row in rows
        ),
        "runnable_now": sum(bool(row["runnable"]) for row in rows),
    }


def _btc_backtest_strategies() -> list[Capability]:
    registry = default_strategy_registry()
    strategy_ids = registry.list()
    return [
        Capability(
            id=f"btc-backtest:strategy:{strategy_id}",
            kind="strategy",
            name=strategy_id,
            source="qt dependency btc-backtest==0.1.0",
            entry_point=f"btc_backtest.strategies.registry:default_strategy_registry[{strategy_id}]",
            readable=False,
            editable=False,
            runnable=True,
            execution_status="existing registry research flow; native bridge behavior is separately versioned",
            behavior_test_status="registry and strategy research regression coverage",
            data_status="requires a ready OHLCV dataset and declared signal dependencies",
            migration_status="implemented_pending_behavior_verification",
            notes="Registry metadata is inspected at runtime; native parity is not inferred.",
        )
        for strategy_id in strategy_ids
    ]


def _qt_strategies() -> list[Capability]:
    names = {
        "smart_dca": "qt.strategies.dca:SmartDCA",
        "capitulation": "qt.strategies.capitulation:Capitulation",
        "weekly_trend": "qt.strategies.trend:WeeklyTrend",
        "basis_carry": "qt.strategies.carry:BasisCarry",
        "wick_catcher": "qt.strategies.wick_catcher:WickCatcher",
        "sim_smart_dca": "qt.strategies.sim.smart_dca:SmartDCA",
        "sim_weekly_trend": "qt.strategies.sim.trend_weekly:WeeklyTrend",
        "sim_basis_carry": "qt.strategies.sim.basis_carry:BasisCarry",
        "sim_wick_catcher": "qt.strategies.sim.wick_catcher:WickCatcher",
        "custom_rule_recipe": "qt.research.strategies:RuleRecipeStrategy",
    }
    return [
        _capability(
            f"qt:strategy:{capability_id}",
            "strategy",
            capability_id,
            "qt",
            entry_point,
            editable=capability_id == "custom_rule_recipe",
            runnable=True,
            execution_status="research/paper only; live execution is disabled by API policy",
            data_status="requires local data declared by the strategy",
        )
        for capability_id, entry_point in names.items()
    ]


def _qt_indicator_families() -> list[Capability]:
    modules = {
        "price": "qt.indicators.price",
        "talib_standard": "qt.indicators.talib_standard",
        "volatility": "qt.indicators.volatility",
        "regime": "qt.indicators.regime",
        "onchain": "qt.indicators.onchain",
        "derivatives": "qt.indicators.derivatives",
        "options": "qt.indicators.options",
        "sentiment": "qt.indicators.sentiment",
        "smart_money": "qt.indicators.smartmoney",
        "events": "qt.indicators.events",
        "composite": "qt.indicators.composite",
    }
    rows: list[Capability] = []
    for name, module in modules.items():
        is_talib = name == "talib_standard"
        rows.append(
            _capability(
                f"qt:indicator-family:{name}",
                "indicator_family",
                name,
                "qt",
                module,
                editable=False,
                runnable=not is_talib or find_spec("talib") is not None,
                execution_status=(
                    "requires TA-Lib==0.7.1 in the Python 3.12 native-research runtime"
                    if name == "talib_standard"
                    else "Python research computation"
                ),
                data_status="requires the family-specific historical fields",
            )
        )
    return rows


def _qt_public_source_capabilities() -> list[Capability]:
    """List concrete QT classes/functions, never package ``__init__`` files.

    The hand-curated rows above give useful product grouping.  These rows are
    the audit-grade complement: each public source entry is independently
    addressable instead of treating a whole indicator/provider/risk family as
    one capability.
    """

    rows: list[Capability] = []
    for kind, area in _QT_SOURCE_AREAS:
        root = _PACKAGE_ROOT / area
        for path, node in _public_symbols(root):
            module = _module_name("qt", path.relative_to(_PACKAGE_ROOT))
            rows.append(
                _source_discovery_capability(
                    f"qt:source:{module}:{node.name}",
                    kind,
                    node.name,
                    "qt",
                    f"{module}:{node.name}",
                    editable=False,
                    execution_status="source symbol is readable/editable; no public Workbench execution adapter is registered",
                    data_status="requires the entry point's declared historical data or local state",
                )
            )
    return rows


def _qt_data_sources() -> list[Capability]:
    rows = [
        _capability(
            f"qt:data-source:{source.id}",
            "data_source",
            source.name,
            "qt",
            f"qt.data.catalog:DATA_SOURCES[{source.id}]",
            editable=False,
            runnable=True,
            execution_status="data source is callable through the retained QT data flow; credentials/freshness gate actual output",
            data_status="credential and local freshness are reported at runtime",
        )
        for source in DATA_SOURCES
    ]
    for provider in all_providers():
        rows.append(
            _capability(
                f"btc-quant-evolution:provider:{provider.name}",
                "provider",
                provider.name,
                "btc-quant evolution",
                f"{type(provider).__module__}:{type(provider).__name__}",
                editable=False,
            runnable=True,
            execution_status="provider is callable through the unified sync flow; credentials and source policy gate network use",
                data_status="runtime provider selection reports available, missing credential, or failed honestly",
            )
        )
    return rows


def _qt_runtime_operations() -> list[Capability]:
    capabilities = {
        "event_backtester": "qt.backtest.engine:Backtester",
        "strategy_backtest": "qt.backtest.strategy_backtest:run_strategy_backtest",
        "walk_forward": "qt.backtest.walkforward:run_walk_forward",
        "bootstrap_and_dsr": "qt.backtest.montecarlo:bootstrap_trade_returns",
        "paper_broker": "qt.execution.paper:PaperBroker",
        "live_broker": "qt.execution.live:LiveBroker",
        "risk_engine": "qt.risk.engine:RiskEngine",
        "opportunity_scanners": "qt.intel.runner:run_intel_once",
        "opportunity_ranking": "qt.intel.ranker:rank_opportunities",
        "portfolio_ledger": "qt.portfolio.ledger:PortfolioLedger",
        "portfolio_reader": "qt.portfolio.reader:read_all_portfolios",
        "strategy_supervisor": "qt.strategies.runner:run_strategy_forever",
        "runtime_monitoring": "qt.monitoring.supervisor:Supervisor",
        "platform_commands": "qt.platform.commands:CommandRepository",
        "platform_audit": "qt.platform.operations:record_audit_event",
    }
    rows: list[Capability] = []
    for name, entry_point in capabilities.items():
        live = name == "live_broker"
        rows.append(
            _capability(
                f"qt:operation:{name}",
                "operation",
                name,
                "qt",
                entry_point,
                editable=False,
                runnable=not live,
                execution_status=(
                    "implemented but deliberately unavailable in the research API; no order route exists"
                    if live
                    else "retained research/paper operation with its own QT workflow"
                ),
                data_status="depends on its declared local state and/or provider data",
            )
        )
    return rows


def _legacy_catalog_capabilities() -> list[Capability]:
    catalog = legacy_catalog()
    raw_signals = catalog.get("signals", [])
    raw_profiles = catalog.get("profiles", [])
    signals = raw_signals if isinstance(raw_signals, list) else []
    profiles = raw_profiles if isinstance(raw_profiles, list) else []
    rows = [
        Capability(
            id=f"btc-quant:catalog-signal:{item['id']}",
            kind="signal",
            name=str(item["id"]),
            source="btc-quant main catalog_profiles.json",
            entry_point=f"qt.workbench.catalog_signals:evaluate_signal[{item['id']}]",
            readable=True,
            editable=False,
            runnable=True,
            execution_status="catalog signal evaluator; full Freqtrade profile lifecycle remains separate",
            behavior_test_status="evaluator smoke tests are not profile/lifecycle parity evidence",
            data_status="requires the profile-declared completed-bar features",
            migration_status="implemented_pending_behavior_verification",
            notes="Rule evaluation does not claim stoploss, ROI, leverage, or sizing equivalence.",
        )
        for item in signals
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    ]
    rows.extend(
        Capability(
            id=f"btc-quant:catalog-profile:{item['id']}",
            kind="strategy_profile",
            name=str(item["id"]),
            source="btc-quant main catalog_profiles.json",
            entry_point="qt.legacy.btc_quant_main.freqtrade_strategies.BtcCatalogStrategy:BtcCatalogStrategy",
            readable=True,
            editable=False,
            runnable=False,
            execution_status="requires isolated, version-pinned Freqtrade plus TA-Lib runtime",
            behavior_test_status="not verified until the real legacy runtime parity suite runs",
            data_status="requires source profile data and Freqtrade candles",
            migration_status="source_ported_requires_legacy_runtime",
            notes="Profiles are readable assets; they are not flattened to target weights.",
        )
        for item in profiles
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    )
    return rows


def _legacy_freqtrade_strategies() -> list[Capability]:
    names = ("BtcCatalogStrategy", "BtcDonchianAtr", "BtcAtrFearVolume", "BtcLowFreqTrend")
    return [
        Capability(
            id=f"btc-quant-main:freqtrade:{name}",
            kind="strategy",
            name=name,
            source="btc-quant main",
            entry_point=f"qt.legacy.btc_quant_main.freqtrade_strategies.{name}:{name}",
            readable=True,
            editable=False,
            runnable=False,
            execution_status="requires isolated, version-pinned Freqtrade plus TA-Lib runtime",
            behavior_test_status="not verified until source stop, ROI, leverage, sizing, and order callbacks run under the real runtime",
            data_status="requires Freqtrade-compatible historical candles",
            migration_status="source_ported_requires_legacy_runtime",
            notes="No compatibility shim or mock Trade object is used.",
        )
        for name in names
    ]


def _legacy_btcqt_event_strategies() -> list[Capability]:
    from qt.strategy_ports.btcqt import BTCQT_PORTS

    return [
        Capability(
            id=f"btc-qt:event:{metadata.strategy_id}",
            kind="event_strategy",
            name=metadata.strategy_id,
            source="btc-qt",
            entry_point="qt.strategy_ports.btcqt:create_btcqt_port",
            readable=True,
            editable=False,
            runnable=False,
            execution_status=(
                "causal source adapter is tested; native event-engine factory integration remains required "
                "before it is a Web-executable capability"
            ),
            behavior_test_status="source semantic parity covered by tests/strategy_ports/test_btcqt.py",
            data_status="; ".join(item.dataset_id for item in metadata.required_data),
            migration_status="source_ported_pending_adapter",
            notes="No same-name WeeklyTrend, generic wick, or target-weight strategy is treated as equivalent.",
        )
        for metadata in BTCQT_PORTS.values()
    ]


def _legacy_source_capabilities() -> list[Capability]:
    """Map every preserved legacy class/function to its exact source commit.

    This deliberately does not call a copied file 'runnable'.  The named
    source entry is readable and editable, while an adapter plus behavior
    evidence is still required before the workbench offers execution.
    """

    provenance = {str(item["target_path"]): item for item in manifest()}
    rows: list[Capability] = []
    symbols_by_target: dict[str, list[ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef]] = {}
    for path, node in _public_symbols(_PACKAGE_ROOT / "legacy"):
        target_path = f"src/{path.relative_to(_PACKAGE_ROOT.parent).as_posix()}"
        symbols_by_target.setdefault(target_path, []).append(node)
    for target_path, item in provenance.items():
        if not target_path.endswith(".py"):
            continue
        module = _module_name("qt", Path(target_path).relative_to("src/qt"))
        symbols = symbols_by_target.get(target_path, [])
        for node in symbols:
            rows.append(
                Capability(
                    id=f"legacy-source:{module}:{node.name}",
                    kind=("legacy_class" if isinstance(node, ast.ClassDef) else "legacy_function"),
                    name=node.name,
                    source=f"{item['source_repository']}@{item['source_commit']}",
                    entry_point=f"{module}:{node.name}",
                    readable=True,
                    editable=True,
                    runnable=False,
                    execution_status="source preserved; semantic adapter and runtime evidence pending",
                    behavior_test_status="not counted as an implemented user capability",
                    data_status=str(item["data_validation_status"]),
                    migration_status="source_ported_pending_adapter",
                    notes="Exact source symbol mapped to its version-preserved file; no runtime equivalence is inferred.",
                )
            )
        if symbols or Path(target_path).name == "__init__.py":
            continue
        rows.append(
            Capability(
                id=f"source-module:{target_path}",
                kind="source_module_pending_classification",
                name=str(item["source_path"]),
                source=f"{item['source_repository']}@{item['source_commit']}",
                entry_point=str(item["entry_point"]),
                readable=True,
                editable=False,
                runnable=False,
                execution_status="source preserved; semantic adapter and runtime evidence pending",
                behavior_test_status="not counted as an implemented user capability",
                data_status=str(item["data_validation_status"]),
                migration_status="source_ported_pending_adapter",
                notes="Tracked for audit coverage without inflating runnable capability counts.",
            )
        )
    return rows


def _public_symbols(
    root: Path,
) -> list[tuple[Path, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef]]:
    symbols: list[tuple[Path, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef]] = []
    for path in sorted(root.rglob("*.py")):
        if path.name == "__init__.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if isinstance(
                node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
            ) and not node.name.startswith("_"):
                symbols.append((path, node))
    return symbols


def _module_name(package: str, path: Path) -> str:
    suffix = path.with_suffix("").as_posix().replace("/", ".")
    return f"{package}.{suffix}"


def _capability(
    capability_id: str,
    kind: str,
    name: str,
    source: str,
    entry_point: str,
    *,
    editable: bool,
    runnable: bool,
    execution_status: str,
    data_status: str,
) -> Capability:
    return Capability(
        id=capability_id,
        kind=kind,
        name=name,
        source=source,
        entry_point=entry_point,
        readable=True,
        editable=editable,
        runnable=runnable,
        execution_status=execution_status,
        behavior_test_status="grouped workflow regression coverage; per-source parity is recorded separately",
        data_status=data_status,
        migration_status="implemented_pending_behavior_verification",
        notes="Status is not a claim of trading profitability or live authorization.",
    )


def _source_discovery_capability(
    capability_id: str,
    kind: str,
    name: str,
    source: str,
    entry_point: str,
    *,
    editable: bool,
    execution_status: str,
    data_status: str,
) -> Capability:
    """Represent a discovered source symbol without promoting it to runtime."""

    return Capability(
        id=capability_id,
        kind=kind,
        name=name,
        source=source,
        entry_point=entry_point,
        readable=True,
        editable=editable,
        runnable=False,
        execution_status=execution_status,
        behavior_test_status="AST/source discovery only; no adapter behavior test",
        data_status=data_status,
        migration_status="source_ported_pending_adapter",
        notes="A source symbol is not a user-callable capability until an adapter and behavior evidence are registered.",
    )
