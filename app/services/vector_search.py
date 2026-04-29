from __future__ import annotations

import hashlib
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from app.core.config import Settings
from app.services.factory import Store

try:
    from qdrant_client import QdrantClient
    from qdrant_client.models import Distance, FieldCondition, Filter, MatchAny, MatchValue, PointStruct, VectorParams
except ImportError:  # pragma: no cover - optional runtime integration
    QdrantClient = None  # type: ignore[assignment]
    Distance = None  # type: ignore[assignment]
    FieldCondition = None  # type: ignore[assignment]
    Filter = None  # type: ignore[assignment]
    MatchAny = None  # type: ignore[assignment]
    MatchValue = None  # type: ignore[assignment]
    PointStruct = None  # type: ignore[assignment]
    VectorParams = None  # type: ignore[assignment]


SEARCH_COLLECTIONS = {'cases', 'chat_messages', 'ocr', 'memory', 'telegram'}


def local_hash_embedding(text: str, dimensions: int) -> list[float]:
    vector = [0.0] * dimensions
    tokens = [token.strip().lower() for token in text.replace('\n', ' ').split() if token.strip()]
    if not tokens:
        tokens = ['empty']
    for token in tokens:
        digest = hashlib.sha256(token.encode('utf-8')).digest()
        index = int.from_bytes(digest[:4], 'big') % dimensions
        sign = 1.0 if digest[4] % 2 == 0 else -1.0
        vector[index] += sign
    norm = sum(value * value for value in vector) ** 0.5 or 1.0
    return [value / norm for value in vector]


def collect_search_documents(store: Store) -> list[dict[str, Any]]:
    documents: list[dict[str, Any]] = []

    def add(source_type: str, source_id: str, title: str, content: str, case_id: str | None, metadata: dict[str, Any] | None = None) -> None:
        text = f'{title}\n{content}'.strip()
        if not text:
            return
        documents.append({
            'source_type': source_type,
            'source_id': source_id,
            'case_id': case_id,
            'title': title,
            'content': content,
            'text': text,
            'metadata': metadata or {},
        })

    for item in store.list('cases'):
        add('cases', item['id'], f"{item['public_id']} {item['title']}", item.get('summary') or '', item['id'], {'status': item.get('status'), 'tags': item.get('tags', [])})
    for item in store.list('chat_messages'):
        add('chat_messages', item['id'], f"Сообщение {item.get('author_name', '')}", item.get('content_md') or str(item.get('content_json') or ''), item.get('case_id'), {'chat_id': item.get('chat_id')})
    for item in store.list('ocr_results'):
        add('ocr', item['id'], 'OCR OBD-области', item.get('text') or '', item.get('case_id'), {'task_id': item.get('obd_region_task_id'), 'engine': item.get('engine')})
    for item in store.list('memory_items'):
        add('memory', item['id'], item.get('memory_type') or 'memory', item.get('text') or '', item.get('case_id'), {'scope': item.get('scope'), 'status': item.get('status')})
    for item in store.list('telegram_messages'):
        add('telegram', item['id'], item.get('author_name') or 'Telegram', item.get('text') or '', None, {'telegram_chat_id': item.get('telegram_chat_id')})
    return documents


def exact_search(store: Store, q: str = '', types: str | None = None, case_id: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
    requested_types = set(types.split(',')) if types else None
    needle = q.lower().strip()
    case_filter = store.find_case(case_id)['id'] if case_id else None
    results = []
    for document in collect_search_documents(store):
        source_type = document['source_type']
        linked_case_id = document.get('case_id')
        if requested_types and source_type not in requested_types:
            continue
        if case_filter and linked_case_id != case_filter:
            continue
        haystack = document['text'].lower()
        if needle and needle not in haystack:
            continue
        score = 1.0 if needle else 0.5
        results.append({
            'source_type': source_type,
            'source_id': document['source_id'],
            'case_id': linked_case_id,
            'title': document['title'],
            'snippet': document['content'][:240],
            'score': score,
            'metadata': document['metadata'],
        })
    return sorted(results, key=lambda item: item['score'], reverse=True)[:limit]


class VectorSearchService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.enabled = settings.vector_backend == 'qdrant'
        self.client = None
        if self.enabled and QdrantClient is not None:
            self.client = QdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key or None, timeout=5.0)

    def status(self) -> dict[str, Any]:
        if not self.enabled:
            return {'backend': 'disabled', 'status': 'disabled'}
        if self.client is None:
            return {'backend': 'qdrant', 'status': 'missing-client'}
        try:
            collections = self.client.get_collections()
            return {'backend': 'qdrant', 'status': 'online', 'collections': len(collections.collections)}
        except Exception as exc:  # pragma: no cover - depends on external service
            return {'backend': 'qdrant', 'status': 'offline', 'error': str(exc)}

    def ensure_collection(self) -> None:
        if not self.enabled or self.client is None:
            return
        if self.client.collection_exists(self.settings.qdrant_collection):
            return
        self.client.create_collection(
            collection_name=self.settings.qdrant_collection,
            vectors_config=VectorParams(size=self.settings.embedding_dimensions, distance=Distance.COSINE),
        )

    def index_all(self, store: Store) -> dict[str, Any]:
        documents = collect_search_documents(store)
        if not self.enabled or self.client is None:
            return {'backend': self.settings.vector_backend, 'indexed': 0, 'status': 'disabled'}
        self.ensure_collection()
        points = []
        for document in documents:
            point_id = str(uuid5(NAMESPACE_URL, f"{document['source_type']}:{document['source_id']}"))
            points.append(PointStruct(
                id=point_id,
                vector=local_hash_embedding(document['text'], self.settings.embedding_dimensions),
                payload=document,
            ))
        if points:
            self.client.upsert(collection_name=self.settings.qdrant_collection, points=points)
        for document in documents:
            search_document = {
                'id': f"{document['source_type']}:{document['source_id']}",
                'workspace_id': 'workspace-default',
                'source_type': document['source_type'],
                'source_id': document['source_id'],
                'case_id': document.get('case_id'),
                'title': document['title'],
                'content': document['content'],
                'metadata': document['metadata'],
            }
            try:
                store.update('search_documents', search_document['id'], search_document)
            except Exception:
                store.add('search_documents', search_document)
        return {'backend': 'qdrant', 'indexed': len(points), 'status': 'ok'}

    def search(self, store: Store, q: str = '', types: str | None = None, case_id: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        if not self.enabled or self.client is None or not q.strip():
            return exact_search(store, q, types, case_id, limit)
        try:
            self.ensure_collection()
            if not store.list('search_documents'):
                self.index_all(store)
            conditions = []
            requested_types = [item for item in (types.split(',') if types else []) if item in SEARCH_COLLECTIONS]
            if requested_types:
                conditions.append(FieldCondition(key='source_type', match=MatchAny(any=requested_types)))
            if case_id:
                conditions.append(FieldCondition(key='case_id', match=MatchValue(value=store.find_case(case_id)['id'])))
            query_filter = Filter(must=conditions) if conditions else None
            hits = self.client.search(
                collection_name=self.settings.qdrant_collection,
                query_vector=local_hash_embedding(q, self.settings.embedding_dimensions),
                query_filter=query_filter,
                limit=limit,
            )
            return [
                {
                    'source_type': hit.payload.get('source_type'),
                    'source_id': hit.payload.get('source_id'),
                    'case_id': hit.payload.get('case_id'),
                    'title': hit.payload.get('title'),
                    'snippet': (hit.payload.get('content') or '')[:240],
                    'score': float(hit.score),
                    'metadata': hit.payload.get('metadata') or {},
                }
                for hit in hits
            ]
        except Exception:
            return exact_search(store, q, types, case_id, limit)
