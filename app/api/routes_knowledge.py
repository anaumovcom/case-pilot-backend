from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field

from app.api.deps import get_store
from app.core.config import get_settings
from app.services.factory import Store
from app.services.store import JsonStore, new_id, now_iso
from app.services.vector_search import VectorSearchService

router = APIRouter(tags=['knowledge'])
settings = get_settings()
vector_search = VectorSearchService(settings)


class MemoryCreate(BaseModel):
    scope: str = 'case'
    memory_type: str = 'fact'
    text: str
    status: str = 'draft'
    structured_data: dict[str, Any] = Field(default_factory=dict)
    source_type: str | None = None
    source_id: str | None = None


class MemoryPatch(BaseModel):
    text: str | None = None
    status: str | None = None
    memory_type: str | None = None
    structured_data: dict[str, Any] | None = None


class TelegramImport(BaseModel):
    chat_title: str = 'Imported Telegram chat'
    messages: list[dict[str, Any]]


class TelegramAttach(BaseModel):
    case_id: str


class ChromePluginMessage(BaseModel):
    external_message_id: str | None = None
    author_name: str | None = None
    author_id: str | None = None
    message_date: str | None = None
    text: str | None = None
    link: str | None = None
    context_before: list[dict[str, Any]] = Field(default_factory=list)
    context_after: list[dict[str, Any]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ChromePluginImport(BaseModel):
    chat_title: str = 'Telegram Web selection'
    page_url: str | None = None
    selected_text: str | None = None
    case_id: str | None = None
    messages: list[ChromePluginMessage] = Field(default_factory=list)


class MacroCreate(BaseModel):
    name: str
    description: str | None = None


class MacroPatch(BaseModel):
    name: str | None = None
    description: str | None = None
    status: str | None = None


class MacroStepCreate(BaseModel):
    step_type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    delay_ms: int = 0
    description: str | None = None
    sequence_no: int | None = None


class MacroParameterCreate(BaseModel):
    name: str
    initial_value: str | None = None


class MacroRunCreate(BaseModel):
    case_id: str | None = None
    parameters: dict[str, str] = Field(default_factory=dict)
    dry_run: bool = False


def verify_chrome_plugin_token(authorization: str | None, x_casepilot_token: str | None) -> None:
    expected = settings.chrome_plugin_token
    if not expected or expected == 'change-me':
        return
    bearer = authorization.removeprefix('Bearer ').strip() if authorization else None
    if x_casepilot_token == expected or bearer == expected:
        return
    raise HTTPException(status_code=401, detail='invalid chrome plugin token')


@router.get('/cases/{case_id}/memory')
def list_case_memory(case_id: str, store: JsonStore = Depends(get_store)):
    case = store.find_case(case_id)
    return {'items': [item for item in store.list('memory_items') if item.get('case_id') == case['id']], 'has_more': False}


@router.post('/cases/{case_id}/memory')
def create_case_memory(case_id: str, payload: MemoryCreate, store: JsonStore = Depends(get_store)):
    case = store.find_case(case_id)
    item = _create_memory(store, payload, case['id'])
    store.event(case['id'], 'memory.item_created', {'memory_id': item['id'], 'text': item['text']})
    return item


@router.get('/memory')
def list_memory(scope: str | None = None, store: JsonStore = Depends(get_store)):
    items = store.list('memory_items')
    if scope:
        items = [item for item in items if item['scope'] == scope]
    return {'items': items, 'has_more': False}


@router.post('/memory')
def create_global_memory(payload: MemoryCreate, store: JsonStore = Depends(get_store)):
    return _create_memory(store, payload, None)


def _create_memory(store: JsonStore, payload: MemoryCreate, case_id: str | None) -> dict[str, Any]:
    now = now_iso()
    item = {
        'id': new_id(),
        'workspace_id': 'workspace-default',
        'case_id': case_id,
        'scope': 'case' if case_id else payload.scope,
        'memory_type': payload.memory_type,
        'status': payload.status,
        'text': payload.text,
        'structured_data': payload.structured_data,
        'source_type': payload.source_type,
        'source_id': payload.source_id,
        'confidence': None,
        'created_by_type': 'user',
        'created_by': 'user-local',
        'created_at': now,
        'updated_at': now,
    }
    return store.add('memory_items', item)


@router.patch('/memory/{memory_id}')
def update_memory(memory_id: str, payload: MemoryPatch, store: JsonStore = Depends(get_store)):
    patch = {key: value for key, value in payload.model_dump().items() if value is not None}
    item = store.update('memory_items', memory_id, patch)
    if item.get('case_id'):
        store.event(item['case_id'], 'memory.item_updated', {'memory_id': memory_id})
    return item


@router.delete('/memory/{memory_id}')
def delete_memory(memory_id: str, store: JsonStore = Depends(get_store)):
    item = store.get('memory_items', memory_id)
    store.delete('memory_items', memory_id)
    if item.get('case_id'):
        store.event(item['case_id'], 'memory.item_deleted', {'memory_id': memory_id})
    return {'ok': True}


@router.post('/memory/{memory_id}/confirm')
def confirm_memory(memory_id: str, store: JsonStore = Depends(get_store)):
    item = store.update('memory_items', memory_id, {'status': 'confirmed'})
    if item.get('case_id'):
        store.event(item['case_id'], 'memory.item_confirmed', {'memory_id': memory_id})
    return item


@router.post('/memory/{memory_id}/reject')
def reject_memory(memory_id: str, store: JsonStore = Depends(get_store)):
    item = store.update('memory_items', memory_id, {'status': 'rejected'})
    if item.get('case_id'):
        store.event(item['case_id'], 'memory.item_rejected', {'memory_id': memory_id})
    return item


@router.get('/search')
def search(q: str = Query(''), types: str | None = None, case_id: str | None = None, limit: int = 20, store: JsonStore = Depends(get_store)):
    return {'items': vector_search.search(store, q, types, case_id, limit), 'has_more': False, 'backend': settings.vector_backend}


@router.post('/search/reindex')
def reindex_search(store: Store = Depends(get_store)):
    return vector_search.index_all(store)


@router.get('/search/status')
def search_status():
    return vector_search.status()


@router.get('/cases/{case_id}/similar')
def similar_cases(case_id: str, store: JsonStore = Depends(get_store)):
    case = store.find_case(case_id)
    items = [item for item in store.list('cases') if item['id'] != case['id']][:3]
    return {'items': [{'case': item, 'score': 0.72, 'reason': 'Похожий статус, теги или контекст'} for item in items]}


@router.get('/memory/search')
def search_memory(q: str = '', store: JsonStore = Depends(get_store)):
    needle = q.lower()
    items = [item for item in store.list('memory_items') if not needle or needle in item['text'].lower()]
    return {'items': items, 'has_more': False}


@router.post('/telegram/import')
def import_telegram(payload: TelegramImport, store: JsonStore = Depends(get_store)):
    now = now_iso()
    chat = {'id': new_id(), 'workspace_id': 'workspace-default', 'title': payload.chat_title, 'created_at': now}
    store.add('telegram_chats', chat)
    imported = []
    for index, raw in enumerate(payload.messages, start=1):
        message = {
            'id': new_id(),
            'workspace_id': 'workspace-default',
            'telegram_chat_id': chat['id'],
            'external_message_id': str(raw.get('external_message_id') or index),
            'author_name': raw.get('author_name') or raw.get('author') or 'Unknown',
            'author_id': raw.get('author_id'),
            'message_date': raw.get('message_date') or now,
            'text': raw.get('text'),
            'link': raw.get('link'),
            'context_before': raw.get('context_before') or [],
            'context_after': raw.get('context_after') or [],
            'metadata': raw.get('metadata') or {},
            'created_at': now,
        }
        store.add('telegram_messages', message)
        imported.append(message)
    return {'chat': chat, 'items': imported}


@router.get('/telegram/messages')
def list_telegram_messages(store: JsonStore = Depends(get_store)):
    return {'items': store.list('telegram_messages'), 'has_more': False}


@router.get('/telegram/chrome-plugin/config')
def chrome_plugin_config(authorization: str | None = Header(default=None), x_casepilot_token: str | None = Header(default=None, alias='X-CasePilot-Token')):
    verify_chrome_plugin_token(authorization, x_casepilot_token)
    return {
        'ok': True,
        'api_prefix': settings.api_prefix,
        'import_endpoint': f'{settings.api_prefix}/telegram/chrome-plugin/import-selection',
        'token_required': bool(settings.chrome_plugin_token and settings.chrome_plugin_token != 'change-me'),
    }


@router.post('/telegram/chrome-plugin/ping')
def chrome_plugin_ping(authorization: str | None = Header(default=None), x_casepilot_token: str | None = Header(default=None, alias='X-CasePilot-Token')):
    verify_chrome_plugin_token(authorization, x_casepilot_token)
    return {'ok': True, 'service': 'case-pilot-backend', 'created_at': now_iso()}


@router.post('/telegram/chrome-plugin/import-selection')
def import_chrome_plugin_selection(
    payload: ChromePluginImport,
    store: JsonStore = Depends(get_store),
    authorization: str | None = Header(default=None),
    x_casepilot_token: str | None = Header(default=None, alias='X-CasePilot-Token'),
):
    verify_chrome_plugin_token(authorization, x_casepilot_token)
    now = now_iso()
    chat = {'id': new_id(), 'workspace_id': 'workspace-default', 'title': payload.chat_title, 'page_url': payload.page_url, 'created_at': now}
    store.add('telegram_chats', chat)
    raw_messages = payload.messages
    if not raw_messages and payload.selected_text:
        raw_messages = [ChromePluginMessage(text=payload.selected_text, author_name='Telegram Web selection', link=payload.page_url)]
    imported = []
    for index, raw in enumerate(raw_messages, start=1):
        message = {
            'id': new_id(),
            'workspace_id': 'workspace-default',
            'telegram_chat_id': chat['id'],
            'external_message_id': raw.external_message_id or f'chrome-{index}',
            'author_name': raw.author_name or 'Unknown',
            'author_id': raw.author_id,
            'message_date': raw.message_date or now,
            'text': raw.text,
            'link': raw.link or payload.page_url,
            'context_before': raw.context_before,
            'context_after': raw.context_after,
            'metadata': {**raw.metadata, 'source': 'chrome-plugin', 'selected_text': payload.selected_text},
            'created_at': now,
        }
        store.add('telegram_messages', message)
        imported.append(message)
        if payload.case_id:
            case = store.find_case(payload.case_id)
            link = {'id': new_id(), 'workspace_id': case['workspace_id'], 'case_id': case['id'], 'telegram_message_id': message['id'], 'created_at': now_iso()}
            store.add('case_telegram_messages', link)
            store.event(case['id'], 'telegram.chrome_plugin_attached', {'telegram_message_id': message['id'], 'author_name': message.get('author_name')})
    return {'chat': chat, 'items': imported, 'attached_case_id': payload.case_id}


@router.get('/telegram/messages/{message_id}/case-suggestions')
def telegram_suggestions(message_id: str, store: JsonStore = Depends(get_store)):
    message = store.get('telegram_messages', message_id)
    text = (message.get('text') or '').lower()
    suggestions = []
    for case in store.list('cases'):
        score = 0.55
        reason = 'Активный кейс подходит по общему контексту'
        for tag in case.get('tags', []):
            if tag.lower() in text:
                score += 0.15
                reason = f'Совпадает тег: {tag}'
        if case['status'] in {'В работе', 'Ждёт ответа', 'Нужен анализ'}:
            score += 0.1
        suggestions.append({'case_id': case['id'], 'public_id': case['public_id'], 'title': case['title'], 'score': round(min(score, 0.97), 2), 'reason': reason})
    return {'suggestions': sorted(suggestions, key=lambda item: item['score'], reverse=True)[:3]}


@router.post('/telegram/messages/{message_id}/attach-to-case')
def attach_telegram(message_id: str, payload: TelegramAttach, store: JsonStore = Depends(get_store)):
    message = store.get('telegram_messages', message_id)
    case = store.find_case(payload.case_id)
    link = {'id': new_id(), 'workspace_id': case['workspace_id'], 'case_id': case['id'], 'telegram_message_id': message['id'], 'created_at': now_iso()}
    store.add('case_telegram_messages', link)
    store.event(case['id'], 'telegram.attached', {'telegram_message_id': message['id'], 'author_name': message.get('author_name')})
    return link


@router.post('/cases/from-telegram')
def create_case_from_telegram(message_id: str, store: JsonStore = Depends(get_store)):
    message = store.get('telegram_messages', message_id)
    from app.api.routes_cases import CaseCreate, create_case

    return create_case(CaseCreate(title=(message.get('text') or 'Кейс из Telegram')[:80], description=message.get('text') or '', tags=['Telegram']), store)


@router.get('/macros')
def list_macros(store: JsonStore = Depends(get_store)):
    items = []
    for macro in store.list('macros'):
        items.append({**macro, 'steps': [step for step in store.list('macro_steps') if step['macro_id'] == macro['id']], 'parameters': [param for param in store.list('macro_parameters') if param['macro_id'] == macro['id']]})
    return {'items': items, 'has_more': False}


@router.post('/macros')
def create_macro(payload: MacroCreate, store: JsonStore = Depends(get_store)):
    now = now_iso()
    macro = {'id': new_id(), 'workspace_id': 'workspace-default', 'name': payload.name, 'description': payload.description or '', 'version': 1, 'status': 'active', 'author_id': 'user-local', 'created_at': now, 'updated_at': now}
    return store.add('macros', macro)


@router.get('/macros/{macro_id}')
def get_macro(macro_id: str, store: JsonStore = Depends(get_store)):
    macro = store.get('macros', macro_id)
    return {**macro, 'steps': [step for step in store.list('macro_steps') if step['macro_id'] == macro_id], 'parameters': [param for param in store.list('macro_parameters') if param['macro_id'] == macro_id]}


@router.patch('/macros/{macro_id}')
def update_macro(macro_id: str, payload: MacroPatch, store: JsonStore = Depends(get_store)):
    patch = {key: value for key, value in payload.model_dump().items() if value is not None}
    return store.update('macros', macro_id, patch)


@router.delete('/macros/{macro_id}')
def delete_macro(macro_id: str, store: JsonStore = Depends(get_store)):
    store.delete('macros', macro_id)
    return {'ok': True}


@router.post('/macros/{macro_id}/steps')
def add_macro_step(macro_id: str, payload: MacroStepCreate, store: JsonStore = Depends(get_store)):
    store.get('macros', macro_id)
    now = now_iso()
    current_steps = [step for step in store.list('macro_steps') if step['macro_id'] == macro_id]
    step = {'id': new_id(), 'macro_id': macro_id, 'sequence_no': payload.sequence_no or len(current_steps) + 1, 'step_type': payload.step_type, 'payload': payload.payload, 'delay_ms': payload.delay_ms, 'description': payload.description, 'created_at': now, 'updated_at': now}
    return store.add('macro_steps', step)


@router.patch('/macros/{macro_id}/steps/{step_id}')
def update_macro_step(macro_id: str, step_id: str, payload: MacroStepCreate, store: JsonStore = Depends(get_store)):
    store.get('macros', macro_id)
    return store.update('macro_steps', step_id, {key: value for key, value in payload.model_dump().items() if value is not None})


@router.delete('/macros/{macro_id}/steps/{step_id}')
def delete_macro_step(macro_id: str, step_id: str, store: JsonStore = Depends(get_store)):
    store.get('macros', macro_id)
    store.delete('macro_steps', step_id)
    return {'ok': True}


@router.post('/macros/{macro_id}/reorder')
def reorder_macro(macro_id: str, step_ids: list[str], store: JsonStore = Depends(get_store)):
    store.get('macros', macro_id)
    for index, step_id in enumerate(step_ids, start=1):
        store.update('macro_steps', step_id, {'sequence_no': index})
    return get_macro(macro_id, store)


@router.post('/macros/{macro_id}/parameters')
def add_macro_parameter(macro_id: str, payload: MacroParameterCreate, store: JsonStore = Depends(get_store)):
    store.get('macros', macro_id)
    now = now_iso()
    item = {'id': new_id(), 'macro_id': macro_id, 'name': payload.name, 'initial_value': payload.initial_value, 'created_at': now, 'updated_at': now}
    return store.add('macro_parameters', item)


@router.patch('/macros/{macro_id}/parameters/{parameter_id}')
def update_macro_parameter(macro_id: str, parameter_id: str, payload: MacroParameterCreate, store: JsonStore = Depends(get_store)):
    store.get('macros', macro_id)
    return store.update('macro_parameters', parameter_id, payload.model_dump())


@router.delete('/macros/{macro_id}/parameters/{parameter_id}')
def delete_macro_parameter(macro_id: str, parameter_id: str, store: JsonStore = Depends(get_store)):
    store.get('macros', macro_id)
    store.delete('macro_parameters', parameter_id)
    return {'ok': True}


@router.post('/macros/{macro_id}/runs')
def run_macro(macro_id: str, payload: MacroRunCreate, store: JsonStore = Depends(get_store)):
    macro = get_macro(macro_id, store)
    now = now_iso()
    run = {'id': new_id(), 'workspace_id': macro['workspace_id'], 'macro_id': macro_id, 'case_id': payload.case_id, 'status': 'completed' if not payload.dry_run else 'dry_run_completed', 'parameters': payload.parameters, 'started_by': 'user-local', 'started_at': now, 'finished_at': now_iso(), 'error': None}
    store.add('macro_runs', run)
    for step in sorted(macro['steps'], key=lambda item: item['sequence_no']):
        rendered = json_render(step['payload'], {param['name']: payload.parameters.get(param['name'], param.get('initial_value') or '') for param in macro['parameters']})
        store.add('macro_run_steps', {'id': new_id(), 'macro_run_id': run['id'], 'macro_step_id': step['id'], 'sequence_no': step['sequence_no'], 'status': 'completed', 'rendered_payload': rendered, 'output': {'bridge': 'mock', 'dry_run': payload.dry_run}, 'started_at': now, 'finished_at': now_iso(), 'error': None})
    if payload.case_id:
        case = store.find_case(payload.case_id)
        store.event(case['id'], 'macro.run_completed', {'macro_run_id': run['id'], 'macro_id': macro_id})
    return {'run': run, 'steps': [item for item in store.list('macro_run_steps') if item['macro_run_id'] == run['id']]}


def json_render(payload: Any, parameters: dict[str, str]) -> Any:
    if isinstance(payload, str):
        for key, value in parameters.items():
            payload = payload.replace('{{' + key + '}}', value)
        return payload
    if isinstance(payload, list):
        return [json_render(item, parameters) for item in payload]
    if isinstance(payload, dict):
        return {key: json_render(value, parameters) for key, value in payload.items()}
    return payload


@router.get('/macro-runs/{run_id}')
def get_macro_run(run_id: str, store: JsonStore = Depends(get_store)):
    run = store.get('macro_runs', run_id)
    return {'run': run, 'steps': [item for item in store.list('macro_run_steps') if item['macro_run_id'] == run_id]}


@router.post('/macro-runs/{run_id}/stop')
def stop_macro_run(run_id: str, store: JsonStore = Depends(get_store)):
    run = store.update('macro_runs', run_id, {'status': 'stopped', 'finished_at': now_iso()})
    if run.get('case_id'):
        store.event(run['case_id'], 'macro.run_stopped', {'macro_run_id': run_id})
    return {'ok': True, 'run': run, 'esp32_response': {'ok': True, 'command': 'system.stop', 'bridge': 'mock'}}


@router.post('/macro-runs/{run_id}/steps/{step_id}/run')
def run_macro_step(run_id: str, step_id: str, store: JsonStore = Depends(get_store)):
    run = store.get('macro_runs', run_id)
    step = store.get('macro_steps', step_id)
    item = {'id': new_id(), 'macro_run_id': run['id'], 'macro_step_id': step['id'], 'sequence_no': step['sequence_no'], 'status': 'completed', 'rendered_payload': step['payload'], 'output': {'bridge': 'mock'}, 'started_at': now_iso(), 'finished_at': now_iso(), 'error': None}
    return store.add('macro_run_steps', item)
