"""Alembic runs only on the caller-supplied transaction, never a second DSN."""

from alembic import context

connection = context.config.attributes["connection"]
context.configure(connection=connection, target_metadata=None,
                  version_table="alembic_version",
                  version_table_schema="qt_v4" if connection.dialect.name == "postgresql" else None,
                  transaction_per_migration=False)
with context.begin_transaction():
    context.run_migrations()
