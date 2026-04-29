from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.api.deps import get_store
from app.core.config import get_settings
from app.services.hid_bridge import HidBridge
from app.services.obd_source import ObdSourceClient
from app.services.store import JsonStore, new_id, now_iso

router = APIRouter(tags=['cases'])
settings = get_settings()
obd_source = ObdSourceClient(settings)
hid_bridge = HidBridge(settings)


class CaseCreate(BaseModel):
    title: str = Field(min_length=1)
    description: str | None = None
    priority: str = 'Средний'
    tags: list[str] = Field(default_factory=list)


class CasePatch(BaseModel):
    title: str | None = None
    description: str | None = None
    status: str | None = None
    priority: str | None = None
    tags: list[str] | None = None
    summary: str | None = None
    current_goal: str | None = None
    result: str | None = None


class ChatCreate(BaseModel):
    title: str = 'Новый чат'
    purpose: str | None = None
    context_mode: str = 'whole_case'


class MessageCreate(BaseModel):
    content_md: str | None = None
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    context_mode: str | None = None


class EventCreate(BaseModel):
    event_type: str = 'note.created'
    payload: dict[str, Any] = Field(default_factory=dict)


def serialize_case(item: dict[str, Any]) -> dict[str, Any]:
    return {
        'id': item['id'],
        'public_id': item['public_id'],
        'title': item['title'],
        'description': item.get('description'),
        'status': item['status'],
        'priority': item['priority'],
        'tags': item.get('tags', []),
        'source': item.get('source'),
        'deadline': item.get('deadline'),
        'summary': item.get('summary'),
        'current_goal': item.get('current_goal'),
        'result': item.get('result'),
        'created_at': item['created_at'],
        'updated_at': item['updated_at'],
        'closed_at': item.get('closed_at'),
        'chats_count': item.get('chats_count', 0),
        'materials_count': item.get('materials_count', 0),
    }


def serialize_message(item: dict[str, Any]) -> dict[str, Any]:
    return {
        'id': item['id'],
        'case_id': item['case_id'],
        'chat_id': item['chat_id'],
        'role': item['role'],
        'author_name': item.get('author_name') or ('Агент' if item['role'] == 'assistant' else 'Вы'),
        'content_md': item.get('content_md'),
        'content_json': item.get('content_json'),
        'attachments': item.get('attachments', []),
        'status': item.get('status', 'done'),
        'created_at': item['created_at'],
        'updated_at': item['updated_at'],
    }


@router.get('/health')
def health(store: JsonStore = Depends(get_store)):
    return {
        'ok': True,
        'service': 'case-pilot-backend',
        'version': '0.1.0',
        'database': f'{settings.store_backend}-store-ok',
        'storage': 'ok' if store.storage_path.exists() else 'error',
    }


@router.get('/cases')
def list_cases(
    q: str | None = None,
    status: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    store: JsonStore = Depends(get_store),
):
    items = store.list('cases')
    if q:
        needle = q.lower()
        items = [item for item in items if needle in f"{item['public_id']} {item['title']} {item.get('summary', '')}".lower()]
    if status:
        items = [item for item in items if item['status'] == status]
    items = sorted(items, key=lambda item: item['updated_at'], reverse=True)[:limit]
    return {'items': [serialize_case(item) for item in items], 'next_cursor': None, 'has_more': False}


@router.post('/cases')
def create_case(payload: CaseCreate, store: JsonStore = Depends(get_store)):
    existing_numbers = [int(item['public_id'].split('-')[-1]) for item in store.list('cases') if item['public_id'].startswith('CASE-') and item['public_id'].split('-')[-1].isdigit()]
    next_public_id = f"CASE-{(max(existing_numbers) + 1 if existing_numbers else 1):03d}"
    now = now_iso()
    item = {
        'id': new_id(),
        'workspace_id': 'workspace-default',
        'public_id': next_public_id,
        'title': payload.title.strip(),
        'description': payload.description or '',
        'status': 'Новый',
        'priority': payload.priority,
        'tags': payload.tags,
        'source': 'manual',
        'deadline': None,
        'summary': payload.description or 'Новый кейс создан через API.',
        'current_goal': None,
        'result': None,
        'created_by': 'user-local',
        'assigned_to': 'user-local',
        'created_at': now,
        'updated_at': now,
        'closed_at': None,
        'chats_count': 0,
        'materials_count': 0,
    }
    store.add('cases', item)
    store.current_chat(item['id'])
    store.event(item['id'], 'case.created', {'public_id': item['public_id'], 'title': item['title']})
    return serialize_case(item)


@router.get('/cases/{case_id}')
def get_case(case_id: str, store: JsonStore = Depends(get_store)):
    return serialize_case(store.find_case(case_id))


@router.patch('/cases/{case_id}')
def update_case(case_id: str, payload: CasePatch, store: JsonStore = Depends(get_store)):
    item = store.find_case(case_id)
    patch = {key: value for key, value in payload.model_dump().items() if value is not None}
    if patch:
        store.update('cases', item['id'], patch)
        store.event(item['id'], 'case.updated', {'fields': list(patch)})
    return serialize_case(item)


@router.post('/cases/{case_id}/close')
def close_case(case_id: str, store: JsonStore = Depends(get_store)):
    item = store.find_case(case_id)
    store.update('cases', item['id'], {'status': 'Закрыт', 'closed_at': now_iso()})
    store.event(item['id'], 'case.closed', {'public_id': item['public_id']})
    return serialize_case(item)


@router.get('/cases/{case_id}/workspace-snapshot')
def workspace_snapshot(case_id: str, store: JsonStore = Depends(get_store)):
    case = store.find_case(case_id)
    chat = store.current_chat(case['id'])
    messages = [serialize_message(item) for item in store.list('chat_messages') if item['chat_id'] == chat['id']]
    events = [item for item in store.list('case_events') if item['case_id'] == case['id']]
    memory = [item for item in store.list('memory_items') if item.get('case_id') == case['id']]
    return {
        'case': serialize_case(case),
        'current_chat': chat,
        'recent_messages': messages[-20:],
        'recent_events': events[-50:],
        'memory_preview': memory[:5],
        'obd_status': obd_source.status(),
        'esp32_status': hid_bridge.status(),
    }


@router.get('/cases/{case_id}/chats')
def list_case_chats(case_id: str, store: JsonStore = Depends(get_store)):
    case = store.find_case(case_id)
    chats = [item for item in store.list('case_chats') if item['case_id'] == case['id'] and not item.get('deleted_at')]
    return {'items': chats, 'has_more': False}


@router.post('/cases/{case_id}/chats')
def create_chat(case_id: str, payload: ChatCreate, store: JsonStore = Depends(get_store)):
    case = store.find_case(case_id)
    now = now_iso()
    chat = {
        'id': new_id(),
        'workspace_id': case['workspace_id'],
        'case_id': case['id'],
        'title': payload.title,
        'purpose': payload.purpose,
        'context_mode': payload.context_mode,
        'is_current_obd_chat': False,
        'created_by': 'user-local',
        'created_at': now,
        'updated_at': now,
        'archived_at': None,
        'deleted_at': None,
    }
    store.add('case_chats', chat)
    store.event(case['id'], 'chat.created', {'chat_id': chat['id'], 'title': chat['title']})
    return chat


@router.delete('/chats/{chat_id}')
def delete_chat(chat_id: str, store: JsonStore = Depends(get_store)):
    chat = store.get('case_chats', chat_id)
    if chat.get('is_current_obd_chat'):
        raise HTTPException(status_code=409, detail='current OBD chat cannot be deleted')
    store.update('case_chats', chat_id, {'deleted_at': now_iso()})
    store.event(chat['case_id'], 'chat.deleted', {'chat_id': chat_id})
    return {'ok': True}


@router.get('/chats/{chat_id}/messages')
def list_messages(chat_id: str, store: JsonStore = Depends(get_store)):
    messages = [serialize_message(item) for item in store.list('chat_messages') if item['chat_id'] == chat_id]
    return {'items': messages, 'has_more': False}


@router.post('/chats/{chat_id}/messages')
def create_message(chat_id: str, payload: MessageCreate, store: JsonStore = Depends(get_store)):
    chat = store.get('case_chats', chat_id)
    now = now_iso()
    message = {
        'id': new_id(),
        'workspace_id': chat['workspace_id'],
        'case_id': chat['case_id'],
        'chat_id': chat_id,
        'role': 'user',
        'author_type': 'user',
        'author_id': 'user-local',
        'author_name': 'Вы',
        'content_md': payload.content_md,
        'content_json': {'context_mode': payload.context_mode},
        'attachments': payload.attachments,
        'status': 'done',
        'created_at': now,
        'updated_at': now,
    }
    store.add('chat_messages', message)
    store.event(chat['case_id'], 'chat.message_added', {'chat_id': chat_id, 'message_id': message['id']})
    return serialize_message(message)


@router.get('/cases/{case_id}/events')
def list_events(case_id: str, store: JsonStore = Depends(get_store)):
    case = store.find_case(case_id)
    events = [item for item in store.list('case_events') if item['case_id'] == case['id']]
    return {'items': sorted(events, key=lambda item: item['created_at'], reverse=True), 'has_more': False}


@router.post('/cases/{case_id}/events')
def create_event(case_id: str, payload: EventCreate, store: JsonStore = Depends(get_store)):
    case = store.find_case(case_id)
    event = store.event(case['id'], payload.event_type, payload.payload)
    return event
