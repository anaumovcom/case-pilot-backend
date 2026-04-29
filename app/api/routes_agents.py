from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.api.deps import get_store
from app.core.config import get_settings
from app.services.llm import LLMClient
from app.services.store import JsonStore, new_id, now_iso

router = APIRouter(tags=['agents'])
settings = get_settings()
llm_client = LLMClient(settings)


class AgentRunCreate(BaseModel):
    case_id: str
    chat_id: str | None = None
    obd_region_task_id: str | None = None
    instruction: str
    context_flags: dict[str, bool] = Field(default_factory=dict)


def build_context(store: JsonStore, case_id: str, task_id: str | None, chat_id: str | None, flags: dict[str, bool]) -> dict[str, Any]:
    case = store.find_case(case_id)
    task = store.get('obd_region_tasks', task_id) if task_id else None
    chat = store.get('case_chats', chat_id) if chat_id else store.current_chat(case['id'])
    recent_messages = [item for item in store.list('chat_messages') if item['chat_id'] == chat['id']][-10:]
    memory = [item for item in store.list('memory_items') if item.get('case_id') in (case['id'], None)][:8]
    ocr = store.get('ocr_results', task['ocr_result_id']) if task and task.get('ocr_result_id') else None
    return {
        'case': {'id': case['id'], 'public_id': case['public_id'], 'title': case['title'], 'status': case['status'], 'summary': case.get('summary')},
        'task': task,
        'ocr': ocr,
        'recent_messages': recent_messages,
        'memory': memory,
        'flags': flags,
        'source_refs': {
            'case_id': case['id'],
            'chat_id': chat['id'],
            'obd_region_task_id': task_id,
            'ocr_result_id': ocr['id'] if ocr else None,
        },
    }


def create_action_for_run(store: JsonStore, case_id: str, task: dict[str, Any] | None, agent_run_id: str, decision: dict[str, Any] | None = None) -> dict[str, Any]:
    action_payload = store.mock_action_payload()
    if decision:
        action_payload.update({
            'text_to_insert': decision['text_to_insert'],
            'explanation': decision['explanation'],
            'execution_plan': decision['execution_plan'],
            'risk_level': decision['risk_level'],
        })
    if task:
        action_payload['target_region'] = task['region']
    action = {
        'id': new_id(),
        'workspace_id': 'workspace-default',
        'case_id': case_id,
        'obd_region_task_id': task['id'] if task else None,
        'agent_run_id': agent_run_id,
        'status': 'ready',
        'risk_level': action_payload['risk_level'],
        'payload': action_payload,
        'created_at': now_iso(),
        'updated_at': now_iso(),
    }
    store.add('proposed_actions', action)
    return action


def create_assistant_message(store: JsonStore, case_id: str, chat_id: str, action: dict[str, Any]) -> dict[str, Any]:
    now = now_iso()
    message = {
        'id': new_id(),
        'workspace_id': 'workspace-default',
        'case_id': case_id,
        'chat_id': chat_id,
        'role': 'assistant',
        'author_type': 'agent',
        'author_id': 'screen_agent',
        'author_name': 'Агент',
        'content_md': 'Готова карточка действия. Проверьте текст и подтвердите выполнение через ESP32.',
        'content_json': {'action_id': action['id'], 'action': action['payload']},
        'attachments': [],
        'status': 'done',
        'created_at': now,
        'updated_at': now,
    }
    store.add('chat_messages', message)
    return message


@router.post('/agent-runs')
def create_agent_run(payload: AgentRunCreate, store: JsonStore = Depends(get_store)):
    case = store.find_case(payload.case_id)
    chat = store.get('case_chats', payload.chat_id) if payload.chat_id else store.current_chat(case['id'])
    return _run_screen_agent(store, case['id'], chat['id'], payload.obd_region_task_id, payload.instruction, payload.context_flags)


@router.post('/obd-region-tasks/{task_id}/send-to-agent')
def send_task_to_agent(task_id: str, store: JsonStore = Depends(get_store)):
    task = store.get('obd_region_tasks', task_id)
    instruction = task.get('user_instruction') or 'Напиши, что ввести в это поле'
    return _run_screen_agent(store, task['case_id'], task['chat_id'], task_id, instruction, task.get('context_flags', {}))


def _run_screen_agent(store: JsonStore, case_id: str, chat_id: str, task_id: str | None, instruction: str, flags: dict[str, bool]) -> dict[str, Any]:
    context_payload = build_context(store, case_id, task_id, chat_id, flags)
    decision = llm_client.screen_action(context_payload, instruction)
    context_package = {
        'id': new_id(),
        'workspace_id': 'workspace-default',
        'case_id': case_id,
        'obd_region_task_id': task_id,
        'chat_id': chat_id,
        'flags': flags,
        'payload': context_payload,
        'source_refs': context_payload['source_refs'],
        'estimated_tokens': 1450,
        'created_at': now_iso(),
    }
    store.add('context_packages', context_package)
    run = {
        'id': new_id(),
        'workspace_id': 'workspace-default',
        'case_id': case_id,
        'chat_id': chat_id,
        'agent_id': 'screen_agent',
        'model_config_id': settings.llm_provider,
        'context_package_id': context_package['id'],
        'status': 'completed',
        'instruction': instruction,
        'output': {
            'message': decision['message'],
            'sources_used': ['case.summary', 'ocr.region', 'case.memory'],
            'confidence': decision['confidence'],
            'warnings': decision['warnings'],
            'llm_provider': settings.llm_provider,
        },
        'error': None,
        'started_at': now_iso(),
        'finished_at': now_iso(),
        'created_at': now_iso(),
    }
    store.add('agent_runs', run)
    task = store.get('obd_region_tasks', task_id) if task_id else None
    action = create_action_for_run(store, case_id, task, run['id'], decision)
    message = create_assistant_message(store, case_id, chat_id, action)
    if task_id:
        store.update('obd_region_tasks', task_id, {'status': 'action_ready', 'agent_run_id': run['id'], 'proposed_action_id': action['id']})
    store.event(case_id, 'agent.run_started', {'agent_run_id': run['id'], 'context_package_id': context_package['id']})
    store.event(case_id, 'agent.action_proposed', {'agent_run_id': run['id'], 'action_id': action['id'], 'message_id': message['id']})
    return {'agent_run': run, 'context_package': context_package, 'proposed_action': action, 'assistant_message': message}


@router.get('/agent-runs/{run_id}')
def get_agent_run(run_id: str, store: JsonStore = Depends(get_store)):
    return store.get('agent_runs', run_id)


@router.get('/agent-runs/{run_id}/events')
def get_agent_run_events(run_id: str):
    return {'items': [{'event': 'agent.completed', 'run_id': run_id, 'created_at': now_iso()}], 'has_more': False}


@router.post('/agent-runs/{run_id}/cancel')
def cancel_agent_run(run_id: str, store: JsonStore = Depends(get_store)):
    run = store.update('agent_runs', run_id, {'status': 'cancelled', 'finished_at': now_iso()})
    store.event(run['case_id'], 'agent.cancelled', {'agent_run_id': run_id})
    return run
