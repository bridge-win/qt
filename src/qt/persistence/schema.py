"""Shared SQLAlchemy metadata. No network calls may execute inside its transactions."""

from decimal import Decimal, localcontext

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.types import TypeDecorator


class ExactMoney(TypeDecorator):
    """PostgreSQL NUMERIC, and exact decimal text for the offline SQLite backend."""

    impl = Numeric(38, 18)
    cache_ok = True

    def load_dialect_impl(self, dialect):
        return dialect.type_descriptor(String(80) if dialect.name == "sqlite" else Numeric(38, 18))

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if not isinstance(value, (Decimal, int)) or isinstance(value, bool):
            raise ValueError("money_requires_exact_decimal")
        amount = Decimal(value)
        if not amount.is_finite():
            raise ValueError("money_must_be_finite")
        with localcontext() as context:
            context.prec = 80
            if amount.copy_abs() >= Decimal("1e20") or amount != amount.quantize(Decimal("1e-18")):
                raise ValueError("money_does_not_fit_numeric_38_18_exactly")
            amount = amount.quantize(Decimal("1e-18"))
        return str(amount) if dialect.name == "sqlite" else amount

    def process_result_value(self, value, dialect):
        return Decimal(value) if value is not None else None

metadata = MetaData(schema="qt_v4")
money = ExactMoney()
clock = BigInteger

artifacts = Table(
    "artifacts", metadata,
    Column("tenant_id", String(80), primary_key=True),
    Column("kind", String(48), primary_key=True),
    Column("entity_id", String(160), primary_key=True),
    Column("payload", JSON, nullable=False),
    Column("sha256", String(64), nullable=False),
    Column("created_ns", clock, nullable=False),
)
jobs = Table(
    "jobs", metadata,
    Column("job_id", String(64), primary_key=True),
    Column("tenant_id", String(80), nullable=False),
    Column("kind", String(48), nullable=False),
    Column("idempotency_key", String(200), nullable=False),
    Column("payload", JSON, nullable=False),
    Column("sha256", String(64), nullable=False),
    Column("status", String(24), nullable=False),
    Column("created_ns", clock, nullable=False),
    Column("deadline_ns", clock, nullable=False),
    Column("lease_until_ns", clock, nullable=False, default=0),
    Column("lease_token", String(64)),
    Column("attempts", Integer, nullable=False, default=0),
    Column("cancel_requested", Integer, nullable=False, default=0),
    Column("result", JSON),
    UniqueConstraint("tenant_id", "kind", "idempotency_key", name="uq_v4_job_identity"),
    CheckConstraint("attempts >= 0", name="ck_v4_job_attempts"),
    Index("ix_v4_jobs_claim", "kind", "status", "lease_until_ns", "created_ns"),
)
events = Table(
    "audit_events", metadata,
    Column("sequence", BigInteger().with_variant(Integer, "sqlite"), primary_key=True,
           autoincrement=True),
    Column("tenant_id", String(80), nullable=False),
    Column("entity_id", String(160), nullable=False),
    Column("event_type", String(64), nullable=False),
    Column("payload", JSON, nullable=False),
    Column("created_ns", clock, nullable=False),
    Index("ix_v4_events_entity", "tenant_id", "entity_id", "sequence"),
)
accounts = Table(
    "risk_accounts", metadata,
    Column("account_id", String(100), primary_key=True),
    Column("tenant_id", String(80), nullable=False),
    Column("quote_asset", String(32), nullable=False),
    Column("capital", money, nullable=False),
    Column("mode", String(24), nullable=False),
    Column("reserved_quote", money, nullable=False, default=0),
    Column("daily_new_risk", money, nullable=False, default=0),
    Column("day_utc", String(10), nullable=False),
    Column("lease_token", String(64)),
    Column("lease_owner", String(160)),
    Column("lease_until_ns", clock, nullable=False, default=0),
    Column("reconciled_ns", clock, nullable=False, default=0),
    CheckConstraint("CAST(capital AS NUMERIC) > 0 AND CAST(reserved_quote AS NUMERIC) >= 0", name="ck_v4_risk_amounts"),
    Index("ix_v4_account_tenant", "tenant_id", "account_id"),
)
orders = Table(
    "orders", metadata,
    Column("intent_id", String(160), primary_key=True),
    Column("account_id", String(100), ForeignKey("qt_v4.risk_accounts.account_id"), nullable=False),
    Column("tenant_id", String(80), nullable=False),
    Column("deployment_id", String(160), nullable=False),
    Column("signal_id", String(160), nullable=False),
    Column("leg_no", Integer, nullable=False, default=0),
    Column("venue", String(48), nullable=False),
    Column("instrument", String(160), nullable=False),
    Column("side", String(4), nullable=False),
    Column("quantity", money, nullable=False),
    Column("filled_quantity", money, nullable=False, default=0),
    Column("reserved_quote", money, nullable=False),
    Column("reserved_base", money, nullable=False, default=0),
    Column("risk_reservation_id", String(160), nullable=False, unique=True),
    Column("status", String(24), nullable=False),
    Column("settlement_status", String(24), nullable=False),
    Column("payload", JSON, nullable=False),
    Column("sha256", String(64), nullable=False),
    Column("venue_order_id", String(200)),
    Column("created_ns", clock, nullable=False),
    Column("updated_ns", clock, nullable=False),
    Column("last_reconciled_ns", clock, nullable=False, default=0),
    UniqueConstraint("account_id", "deployment_id", "signal_id", "leg_no",
                     name="uq_v4_signal_leg"),
    CheckConstraint("CAST(quantity AS NUMERIC) > 0 AND CAST(filled_quantity AS NUMERIC) >= 0 AND CAST(filled_quantity AS NUMERIC) <= CAST(quantity AS NUMERIC)",
                    name="ck_v4_order_quantity"),
    Index("ix_v4_orders_account", "account_id", "status"),
    Index("ix_v4_orders_tenant", "tenant_id", "intent_id"),
)
fills = Table(
    "fills", metadata,
    Column("fill_id", String(64), primary_key=True),
    Column("intent_id", String(160), ForeignKey("qt_v4.orders.intent_id"), nullable=False),
    Column("venue", String(48), nullable=False),
    Column("account_id", String(100), ForeignKey("qt_v4.risk_accounts.account_id"), nullable=False),
    Column("venue_trade_id", String(200), nullable=False),
    Column("quantity", money, nullable=False),
    Column("price", money, nullable=False),
    Column("fee", money, nullable=False),
    Column("fee_asset", String(32), nullable=False),
    Column("sha256", String(64), nullable=False),
    Column("created_ns", clock, nullable=False),
    UniqueConstraint("venue", "account_id", "venue_trade_id", name="uq_v4_venue_fill"),
    CheckConstraint("CAST(quantity AS NUMERIC) > 0 AND CAST(price AS NUMERIC) > 0 AND CAST(fee AS NUMERIC) >= 0", name="ck_v4_fill_amounts"),
    Index("ix_v4_fill_intent", "intent_id"),
    Index("ix_v4_fill_account", "account_id"),
)
ledger_transactions = Table(
    "ledger_transactions", metadata,
    Column("transaction_id", String(160), primary_key=True),
    Column("account_id", String(100), ForeignKey("qt_v4.risk_accounts.account_id"), nullable=False),
    Column("kind", String(32), nullable=False),
    Column("source_id", String(160), nullable=False),
    Column("created_ns", clock, nullable=False),
    UniqueConstraint("account_id", "kind", "source_id", name="uq_v4_ledger_source"),
    Index("ix_v4_ledger_account", "account_id", "created_ns"),
)
ledger_entries = Table(
    "ledger_entries", metadata,
    Column("entry_id", String(64), primary_key=True),
    Column("transaction_id", String(160), ForeignKey("qt_v4.ledger_transactions.transaction_id"),
           nullable=False),
    Column("account_id", String(100), ForeignKey("qt_v4.risk_accounts.account_id"), nullable=False),
    Column("asset", String(32), nullable=False),
    Column("bucket", String(24), nullable=False),
    Column("amount", money, nullable=False),
    Index("ix_v4_entries_tx", "transaction_id"),
    Index("ix_v4_entries_balance", "account_id", "asset", "bucket"),
)
runtime_health = Table(
    "runtime_health", metadata,
    Column("name", String(80), primary_key=True),
    Column("tenant_id", String(80), primary_key=True),
    Column("observed_ns", clock, nullable=False),
    Column("payload", JSON, nullable=False),
)
migration_imports = Table(
    "migration_imports", metadata,
    Column("source_hash", String(64), primary_key=True),
    Column("tenant_id", String(80), primary_key=True),
    Column("manifest", JSON, nullable=False),
    Column("created_ns", clock, nullable=False),
)
deployment_heads = Table(
    "deployment_heads", metadata,
    Column("tenant_id", String(80), primary_key=True),
    Column("account_id", String(100), primary_key=True),
    Column("deployment_id", String(160)),
    Column("status", String(16), nullable=False),
    Column("generation", Integer, nullable=False, default=0),
    Column("updated_ns", clock, nullable=False),
    Column("reason", String(1024), nullable=False),
    CheckConstraint("generation >= 0", name="ck_v4_deployment_head_generation"),
    Index("ix_v4_deployment_head_active", "tenant_id", "status", "deployment_id"),
)
schema_versions = Table(
    "schema_versions", metadata,
    Column("version", Integer, primary_key=True),
    Column("description", Text, nullable=False),
    Column("created_ns", clock, nullable=False),
)

risk_state = Table(
    "risk_state", metadata,
    Column("account_id", String(100), ForeignKey("qt_v4.risk_accounts.account_id"), primary_key=True),
    Column("tenant_id", String(80), nullable=False),
    Column("policy_hash", String(64), nullable=False),
    Column("day_utc", String(10), nullable=False),
    Column("day_start_equity", money, nullable=False),
    Column("high_water_equity", money, nullable=False),
    Column("last_equity", money, nullable=False),
    Column("last_mark_ns", clock, nullable=False),
    Column("market", JSON, nullable=False),
    Column("latched_reasons", JSON, nullable=False),
)
portfolio_snapshots = Table(
    "portfolio_snapshots", metadata,
    Column("snapshot_id", String(64), primary_key=True),
    Column("tenant_id", String(80), nullable=False),
    Column("account_id", String(100), ForeignKey("qt_v4.risk_accounts.account_id"), nullable=False),
    Column("observed_ns", clock, nullable=False),
    Column("market_hash", String(64), nullable=False),
    Column("payload", JSON, nullable=False),
    Column("sha256", String(64), nullable=False),
    Index("ix_v4_portfolio_snapshots", "tenant_id", "account_id", "observed_ns"),
)
usage_budgets = Table(
    "usage_budgets", metadata,
    Column("tenant_id", String(80), primary_key=True),
    Column("period", String(180), primary_key=True),
    Column("reserved", money, nullable=False),
    Column("maximum", money, nullable=False),
)
model_calls = Table(
    "model_calls", metadata,
    Column("tenant_id", String(80), primary_key=True),
    Column("call_id", String(200), primary_key=True),
    Column("run_id", String(160), nullable=False),
    Column("request_hash", String(64), nullable=False),
    Column("periods", JSON, nullable=False),
    Column("reserved", money, nullable=False),
    Column("actual", money),
    Column("status", String(24), nullable=False),
    Column("created_ns", clock, nullable=False),
    Index("ix_v4_calls_run", "tenant_id", "run_id"),
)
