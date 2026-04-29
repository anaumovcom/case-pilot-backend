"""PostgreSQL runtime entrypoints.

The current production-ready pass uses ``app.services.postgres_store.PostgresStore``:
a psycopg pool with JSONB persistence for all CasePilot collections.
"""

from app.core.config import Settings
from app.services.postgres_store import PostgresStore


def create_postgres_store(settings: Settings) -> PostgresStore:
	return PostgresStore(
		database_url=settings.database_url,
		storage_path=settings.local_storage_path,
		min_pool_size=settings.postgres_pool_min_size,
		max_pool_size=settings.postgres_pool_max_size,
	)
