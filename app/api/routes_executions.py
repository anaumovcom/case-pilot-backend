from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api.deps import get_store
from app.core.config import get_settings
from app.services.hid_bridge import HidBridge
from app.services.store import JsonStore, new_id, now_iso

router = APIRouter(tags=['executions'])
settings = get_settings()
hid_bridge = HidBridge(settings)


class ExecuteActionPayload(BaseModel):
    confirmed: bool = True
    confirmation_note: str | None = None


class Esp32ClickPayload(BaseModel):
    x: int
    y: int
    button: str = 'left'


class Esp32CommandPayload(BaseModel):
    type: str
    payload: dict[str, Any]
    timeout_ms: int | None = None


def compile_text_input(action: dict[str, Any]) -> list[dict[str, Any]]:
    payload = action['payload']
    region = payload.get('target_region') or {'x': 640, 'y': 420, 'width': 380, 'height': 42}
    center_x = int(region['x'] + region['width'] / 2)
    center_y = int(region['y'] + region['height'] / 2)
    return [
        {'type': 'mouse.click', 'payload': {'x': center_x, 'y': center_y, 'button': 'left'}, 'timeout_ms': 5000},
        {'type': 'keyboard.hotkey', 'payload': {'keys': ['Ctrl', 'A']}, 'timeout_ms': 5000},
        {'type': 'keyboard.key', 'payload': {'key': 'Backspace'}, 'timeout_ms': 5000},
        {'type': 'keyboard.type', 'payload': {'text': payload.get('text_to_insert', '')}, 'timeout_ms': 10000},
    ]


def create_execution(store: JsonStore, action: dict[str, Any], confirmed_by: str = 'user-local') -> dict[str, Any]:
    if action.get('risk_level') == 'blocked':
        raise HTTPException(status_code=409, detail='blocked action cannot be executed')
    now = now_iso()
    session = {
        'id': new_id(),
        'workspace_id': action['workspace_id'],
        'case_id': action['case_id'],
        'proposed_action_id': action['id'],
        'obd_region_task_id': action.get('obd_region_task_id'),
        'status': 'running',
        'risk_level': action.get('risk_level', 'low'),
        'confirmed_by': confirmed_by,
        'confirmed_at': now,
        'started_at': now,
        'finished_at': None,
        'before_screenshot_attachment_id': None,
        'after_screenshot_attachment_id': None,
        'error': None,
        'created_at': now,
        'updated_at': now,
    }
    store.add('execution_sessions', session)
    commands = compile_text_input(action)
    saved_commands = []
    failed = False
    for index, command in enumerate(commands, start=1):
        started = time.perf_counter()
        error = None
        try:
            esp32_response = hid_bridge.send_command(command['type'], command['payload'], command['timeout_ms'])
            command_status = 'done' if esp32_response.get('ok', True) else 'failed'
            if command_status == 'failed':
                error = str(esp32_response.get('error') or 'ESP32 command failed')
                failed = True
        except Exception as exc:  # pragma: no cover - external device
            esp32_response = {'ok': False, 'bridge': settings.esp32_bridge_mode, 'error': str(exc)}
            command_status = 'failed'
            error = str(exc)
            failed = True
        command_item = {
            'id': new_id(),
            'execution_session_id': session['id'],
            'sequence_no': index,
            'command_type': command['type'],
            'payload': command['payload'],
            'timeout_ms': command['timeout_ms'],
            'status': command_status,
            'esp32_response': esp32_response,
            'duration_ms': int((time.perf_counter() - started) * 1000),
            'error': error,
            'created_at': now,
            'sent_at': now_iso(),
            'finished_at': now_iso(),
        }
        store.add('hid_commands', command_item)
        saved_commands.append(command_item)
        store.event(action['case_id'], 'execution.command_sent', {'execution_id': session['id'], 'command_id': command_item['id'], 'type': command_item['command_type']})
        if failed:
            break
    final_status = 'failed' if failed else 'executed'
    store.update('execution_sessions', session['id'], {'status': final_status, 'finished_at': now_iso(), 'error': saved_commands[-1].get('error') if failed else None})
    store.update('proposed_actions', action['id'], {'status': final_status})
    if action.get('obd_region_task_id'):
        store.update('obd_region_tasks', action['obd_region_task_id'], {'status': final_status, 'execution_session_id': session['id'], 'result_status': 'failed' if failed else 'success'})
    store.event(action['case_id'], 'execution.completed' if not failed else 'execution.failed', {'execution_id': session['id'], 'action_id': action['id'], 'commands_count': len(saved_commands)})
    session = store.get('execution_sessions', session['id'])
    return {'execution': session, 'commands': saved_commands}


@router.post('/actions/{action_id}/confirm')
def confirm_action(action_id: str, store: JsonStore = Depends(get_store)):
    action = store.get('proposed_actions', action_id)
    store.update('proposed_actions', action_id, {'confirmed_at': now_iso(), 'confirmed_by': 'user-local'})
    store.event(action['case_id'], 'agent.action_confirmed', {'action_id': action_id})
    return store.get('proposed_actions', action_id)


@router.post('/actions/{action_id}/execute')
def execute_action(action_id: str, payload: ExecuteActionPayload, store: JsonStore = Depends(get_store)):
    if not payload.confirmed:
        raise HTTPException(status_code=400, detail='confirmation required')
    action = store.get('proposed_actions', action_id)
    return create_execution(store, action)


@router.get('/executions/{execution_id}')
def get_execution(execution_id: str, store: JsonStore = Depends(get_store)):
    execution = store.get('execution_sessions', execution_id)
    commands = [item for item in store.list('hid_commands') if item['execution_session_id'] == execution_id]
    return {'execution': execution, 'commands': sorted(commands, key=lambda item: item['sequence_no'])}


@router.get('/executions/{execution_id}/events')
def get_execution_events(execution_id: str, store: JsonStore = Depends(get_store)):
    commands = [item for item in store.list('hid_commands') if item['execution_session_id'] == execution_id]
    return {'items': [{'event': 'execution.command_sent', 'command': item, 'created_at': item['created_at']} for item in commands], 'has_more': False}


@router.post('/executions/{execution_id}/stop')
def stop_execution(execution_id: str, store: JsonStore = Depends(get_store)):
    execution = store.update('execution_sessions', execution_id, {'status': 'stopped', 'finished_at': now_iso()})
    for command in store.list('hid_commands'):
        if command['execution_session_id'] == execution_id and command['status'] in {'queued', 'running'}:
            command['status'] = 'skipped'
    store.event(execution['case_id'], 'execution.stopped', {'execution_id': execution_id})
    return {'ok': True, 'execution': execution, 'esp32_response': hid_bridge.stop()}


@router.post('/esp32/stop')
def stop_esp32(store: JsonStore = Depends(get_store)):
    active = [item for item in store.list('execution_sessions') if item['status'] == 'running']
    for session in active:
        store.update('execution_sessions', session['id'], {'status': 'stopped', 'finished_at': now_iso()})
        store.event(session['case_id'], 'execution.stopped', {'execution_id': session['id'], 'global_stop': True})
    return {'ok': True, 'stopped_executions': len(active), 'esp32_response': hid_bridge.stop()}


@router.post('/esp32/click')
def click_esp32(payload: Esp32ClickPayload):
    try:
        esp32_response = hid_bridge.send_command('mouse.click', {'x': payload.x, 'y': payload.y, 'button': payload.button}, settings.esp32_command_timeout_ms)
    except Exception as exc:  # pragma: no cover - external device
        esp32_response = {'ok': False, 'bridge': settings.esp32_bridge_mode, 'error': str(exc)}
    return {
        'ok': bool(esp32_response.get('ok', True)),
        'command_type': 'mouse.click',
        'payload': {'x': payload.x, 'y': payload.y, 'button': payload.button},
        'esp32_response': esp32_response,
    }


@router.post('/esp32/command')
def command_esp32(payload: Esp32CommandPayload):
    try:
        esp32_response = hid_bridge.send_command(payload.type, payload.payload, payload.timeout_ms)
    except Exception as exc:  # pragma: no cover - external device
        esp32_response = {'ok': False, 'bridge': settings.esp32_bridge_mode, 'error': str(exc)}
    return {
        'ok': bool(esp32_response.get('ok', True)),
        'command_type': payload.type,
        'payload': payload.payload,
        'esp32_response': esp32_response,
    }


@router.get('/esp32/status')
def esp32_status():
    return hid_bridge.status()
