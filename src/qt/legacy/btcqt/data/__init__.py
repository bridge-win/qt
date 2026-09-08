# Migrated verbatim from btcqt; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
"""Public market-data adapters and durable storage.

Collectors only publish normalized events into the application queue. They
never hold trading credentials and never place orders.
"""


