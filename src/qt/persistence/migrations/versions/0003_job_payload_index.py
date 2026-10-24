"""Index immutable experiment jobs for request deduplication across client keys."""

from alembic import op

revision = "0003_job_payload_index"
down_revision = "0002_deployment_heads"
branch_labels = None
depends_on = None


def upgrade():
    schema = "qt_v4" if op.get_bind().dialect.name == "postgresql" else None
    op.create_index("ix_v4_jobs_payload", "jobs", ["tenant_id", "kind", "sha256", "created_ns"], schema=schema)


def downgrade():
    schema = "qt_v4" if op.get_bind().dialect.name == "postgresql" else None
    op.drop_index("ix_v4_jobs_payload", table_name="jobs", schema=schema)
