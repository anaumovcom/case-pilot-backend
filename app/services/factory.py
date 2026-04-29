from __future__ import annotations

from app.core.config import Settings
from app.services.postgres_store import PostgresStore
from app.services.store import JsonStore


Store = JsonStore | PostgresStore


def create_store(settings: Settings) -> Store:
    if settings.store_backend == 'postgres':
        return PostgresStore(
            database_url=settings.database_url,
            storage_path=settings.local_storage_path,
            min_pool_size=settings.postgres_pool_min_size,
            max_pool_size=settings.postgres_pool_max_size,
        )
    return JsonStore(settings.local_storage_path)


def close_store(store: Store) -> None:
    close = getattr(store, 'close', None)
    if callable(close):
        close()
