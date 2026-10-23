"""One authoritative active deployment head for each account."""

from alembic import op
from sqlalchemy import BigInteger as Clock
from sqlalchemy import CheckConstraint, Column, Integer, String

revision = "0002_deployment_heads"
down_revision = "0001_v4"
branch_labels = None
depends_on = None


def upgrade():
    schema = "qt_v4" if op.get_bind().dialect.name == "postgresql" else None
    op.create_table(
        "deployment_heads",
        Column("tenant_id", String(80), primary_key=True),
        Column("account_id", String(100), primary_key=True),
        Column("deployment_id", String(160)),
        Column("status", String(16), nullable=False),
        Column("generation", Integer, nullable=False),
        Column("updated_ns", Clock, nullable=False),
        Column("reason", String(1024), nullable=False),
        CheckConstraint("generation >= 0", name="ck_v4_deployment_head_generation"),
        schema=schema,
    )
    op.create_index("ix_v4_deployment_head_active", "deployment_heads",
                    ["tenant_id", "status", "deployment_id"], schema=schema)


def downgrade():
    raise RuntimeError("Deployment history cannot be destructively downgraded")
