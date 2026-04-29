from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import HTTPException

from app.services.store import COLLECTIONS, JsonStore, new_id, now_iso

try:
    import psycopg
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb
    from psycopg_pool import ConnectionPool
except ImportError:  # pragma: no cover - handled at runtime with a clear error
    psycopg = None  # type: ignore[assignment]
    dict_row = None  # type: ignore[assignment]
    Jsonb = None  # type: ignore[assignment]
    ConnectionPool = None  # type: ignore[assignment]


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS casepilot_items (
    collection TEXT NOT NULL,
    item_id TEXT NOT NULL,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (collection, item_id)
);
CREATE INDEX IF NOT EXISTS idx_casepilot_items_collection ON casepilot_items (collection);
CREATE INDEX IF NOT EXISTS idx_casepilot_items_payload_gin ON casepilot_items USING GIN (payload);
"""


class PostgresStore(JsonStore):
    """PostgreSQL-backed implementation of the JsonStore contract.

    It stores every domain collection as JSONB rows while preserving the existing API contracts.
    This gives a working PostgreSQL runtime now and keeps the door open for a later fully relational
    SQLAlchemy/Alembic model pass without touching the frontend.
    """

    def __init__(self, database_url: str, storage_path: Path, min_pool_size: int = 1, max_pool_size: int = 10):
        if ConnectionPool is None:
            raise RuntimeError('PostgreSQL store requires psycopg[binary,pool]. Run pip install -e .[dev].')

        self.storage_path = storage_path
        self.storage_path.mkdir(parents=True, exist_ok=True)
        self.files_path = self.storage_path / 'files'
        self.files_path.mkdir(parents=True, exist_ok=True)
        self.database_url = self._normalize_url(database_url)
        self.db_path = self.database_url
        self.pool = ConnectionPool(
            conninfo=self.database_url,
            min_size=min_pool_size,
            max_size=max_pool_size,
            kwargs={'row_factory': dict_row},
            open=True,
        )
        self._ensure_schema()
        self._ensure_seed_data()

    @staticmethod
    def _normalize_url(database_url: str) -> str:
        return database_url.replace('postgresql+asyncpg://', 'postgresql://').replace('postgresql+psycopg://', 'postgresql://')

    @property
    def data(self) -> dict[str, list[dict[str, Any]]]:
        return {collection: self.list(collection) for collection in COLLECTIONS}

    def close(self) -> None:
        self.pool.close()

    def _ensure_schema(self) -> None:
        with self.pool.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(SCHEMA_SQL)
            connection.commit()

    def _ensure_seed_data(self) -> None:
        with self.pool.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT COUNT(*) AS count FROM casepilot_items WHERE collection = 'cases'")
                count = cursor.fetchone()['count']
        if count:
            return

        seed_data = {collection: [] for collection in COLLECTIONS}
        self._seed(seed_data)
        for collection, items in seed_data.items():
            for item in items:
                self.add(collection, item)

    def list(self, collection: str) -> list[dict[str, Any]]:
        self._validate_collection(collection)
        with self.pool.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    'SELECT payload FROM casepilot_items WHERE collection = %s ORDER BY created_at ASC',
                    (collection,),
                )
                return [row['payload'] for row in cursor.fetchall()]

    def add(self, collection: str, item: dict[str, Any]) -> dict[str, Any]:
        self._validate_collection(collection)
        if not item.get('id'):
            item['id'] = new_id()
        with self.pool.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO casepilot_items (collection, item_id, payload)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (collection, item_id)
                    DO UPDATE SET payload = EXCLUDED.payload, updated_at = now()
                    """,
                    (collection, item['id'], Jsonb(item)),
                )
            connection.commit()
        return item

    def update(self, collection: str, item_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        item = self.get(collection, item_id)
        item.update(patch)
        if 'updated_at' in item:
            item['updated_at'] = now_iso()
        self.add(collection, item)
        return item

    def delete(self, collection: str, item_id: str) -> None:
        self._validate_collection(collection)
        with self.pool.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute('DELETE FROM casepilot_items WHERE collection = %s AND item_id = %s', (collection, item_id))
                deleted = cursor.rowcount
            connection.commit()
        if not deleted:
            raise HTTPException(status_code=404, detail=f'{collection} item not found')

    def get(self, collection: str, item_id: str) -> dict[str, Any]:
        self._validate_collection(collection)
        with self.pool.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    'SELECT payload FROM casepilot_items WHERE collection = %s AND item_id = %s',
                    (collection, item_id),
                )
                row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f'{collection} item not found')
        return row['payload']

    def find_case(self, case_id: str) -> dict[str, Any]:
        with self.pool.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT payload
                    FROM casepilot_items
                    WHERE collection = 'cases'
                    AND (item_id = %s OR payload->>'public_id' = %s)
                    LIMIT 1
                    """,
                    (case_id, case_id),
                )
                row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail='case not found')
        return row['payload']

    def current_chat(self, case_id: str) -> dict[str, Any]:
        case = self.find_case(case_id)
        with self.pool.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT payload
                    FROM casepilot_items
                    WHERE collection = 'case_chats'
                    AND payload->>'case_id' = %s
                    AND payload->>'is_current_obd_chat' = 'true'
                    AND (payload->>'deleted_at' IS NULL OR payload->>'deleted_at' = '')
                    LIMIT 1
                    """,
                    (case['id'],),
                )
                row = cursor.fetchone()
        if row:
            return row['payload']

        chat = {
            'id': new_id(),
            'workspace_id': case['workspace_id'],
            'case_id': case['id'],
            'title': 'Работа с OBD-экраном',
            'purpose': 'current_obd',
            'context_mode': 'whole_case',
            'is_current_obd_chat': True,
            'created_by': 'user-local',
            'created_at': now_iso(),
            'updated_at': now_iso(),
            'archived_at': None,
            'deleted_at': None,
        }
        return self.add('case_chats', chat)

    def event(
        self,
        case_id: str,
        event_type: str,
        payload: dict[str, Any],
        workspace_id: str = 'workspace-default',
        actor_id: str | None = 'user-local',
        persist: bool = True,
    ) -> dict[str, Any]:
        item = {
            'id': new_id(),
            'workspace_id': workspace_id,
            'case_id': case_id,
            'event_type': event_type,
            'actor_type': 'user' if actor_id else 'system',
            'actor_id': actor_id,
            'source_type': payload.get('source_type'),
            'source_id': payload.get('source_id'),
            'payload': payload,
            'visible': True,
            'editable': False,
            'created_at': now_iso(),
        }
        if persist:
            self.add('case_events', item)
        return item

    @staticmethod
    def _validate_collection(collection: str) -> None:
        if collection not in COLLECTIONS:
            raise HTTPException(status_code=400, detail=f'unknown collection: {collection}')
