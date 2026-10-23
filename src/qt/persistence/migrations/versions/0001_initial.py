"""Frozen initial V4 schema; future revisions must not regenerate this DDL."""

import json
from pathlib import Path

from alembic import op

revision = "0001_v4"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    dialect = connection.dialect.name
    statements = json.loads(Path(__file__).with_name("0001_schema.json").read_text())[dialect]
    for statement in statements:
        op.execute(statement)


def downgrade():
    raise RuntimeError("V4 financial history cannot be destructively downgraded; restore a verified backup")
