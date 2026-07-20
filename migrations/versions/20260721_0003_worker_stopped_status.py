"""Allow workers to persist a completed shutdown state.

Revision ID: 20260721_0003
Revises: 20260721_0002
Create Date: 2026-07-21 00:02:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260721_0003"
down_revision: str | None = "20260721_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_STATUSES = "status IN ('starting', 'healthy', 'degraded', 'stopping', 'failed')"
_NEW_STATUSES = (
    "status IN ('starting', 'healthy', 'degraded', 'stopping', 'stopped', 'failed')"
)


def upgrade() -> None:
    with op.batch_alter_table("worker_heartbeats") as batch_op:
        batch_op.drop_constraint("ck_worker_heartbeats_status", type_="check")
        batch_op.create_check_constraint("ck_worker_heartbeats_status", _NEW_STATUSES)


def downgrade() -> None:
    op.execute(
        sa.text("UPDATE worker_heartbeats SET status = 'stopping' WHERE status = 'stopped'")
    )
    with op.batch_alter_table("worker_heartbeats") as batch_op:
        batch_op.drop_constraint("ck_worker_heartbeats_status", type_="check")
        batch_op.create_check_constraint("ck_worker_heartbeats_status", _OLD_STATUSES)
