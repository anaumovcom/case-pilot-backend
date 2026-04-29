from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.api.deps import get_store
from app.core.config import get_settings
from app.services.obd_source import ObdSourceClient
from app.services.omniparser_client import OmniParserClient
from app.services.store import JsonStore, new_id, now_iso

router = APIRouter(tags=['obd'])
settings = get_settings()
omniparser = OmniParserClient(settings)
obd_source = ObdSourceClient(settings)


class RegionPayload(BaseModel):
    x: int
    y: int
    width: int
    height: int
    coordinate_space: str = 'viewport_pixels'


class ObdRegionTaskCreate(BaseModel):
    chat_id: str | None = None
    user_instruction: str | None = None
    selected_template: str | None = None
    region: RegionPayload
    viewport_transform: dict[str, Any] = Field(default_factory=dict)
    context_flags: dict[str, bool] = Field(default_factory=lambda: {
        'case_description': True,
        'case_memory': True,
        'telegram': True,
        'recent_chat': True,
        'ocr_region': True,
        'full_screen': True,
        'global_memory': False,
        'similar_cases': False,
    })
    region_screenshot_attachment_id: str | None = None
    full_screenshot_attachment_id: str | None = None


class ObdRegionTaskPatch(BaseModel):
    status: str | None = None
    user_instruction: str | None = None
    context_flags: dict[str, bool] | None = None
    result_status: str | None = None


class OcrPatch(BaseModel):
    text: str
    correction_reason: str | None = None


class ObdWebRtcOffer(BaseModel):
    sdp: str
    type: str


class ObdWebRtcAnswer(BaseModel):
    session_id: int
    sdp: str
    type: str
    screen_width: int
    screen_height: int
    source: str


def serialize_task(item: dict[str, Any]) -> dict[str, Any]:
    return item


@router.get('/obd/status')
def obd_status():
    status = obd_source.status()
    status['ocr'] = omniparser.probe()
    return status


@router.get('/obd/frame')
def get_obd_frame():
    return obd_source.screenshot()


@router.post('/obd/screenshot')
def create_screenshot():
    return obd_source.screenshot()


@router.post('/obd/webrtc/offer', response_model=ObdWebRtcAnswer)
async def create_obd_webrtc_offer(payload: ObdWebRtcOffer):
    try:
        return await obd_source.create_webrtc_answer(payload.sdp, payload.type)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f'OBD WebRTC negotiation failed: {exc}') from exc


@router.delete('/obd/webrtc/session/{session_id}', status_code=204)
async def delete_obd_webrtc_session(session_id: int):
    await obd_source.close_webrtc_session(session_id)


@router.post('/obd/crop')
def crop_obd_region(region: RegionPayload):
    return {'id': new_id(), 'status': 'created', 'region': region.model_dump(), 'source': 'mock', 'created_at': now_iso()}


@router.post('/cases/{case_id}/obd-region-tasks')
def create_region_task(case_id: str, payload: ObdRegionTaskCreate, store: JsonStore = Depends(get_store)):
    case = store.find_case(case_id)
    chat = store.get('case_chats', payload.chat_id) if payload.chat_id else store.current_chat(case['id'])
    now = now_iso()
    task = {
        'id': new_id(),
        'workspace_id': case['workspace_id'],
        'case_id': case['id'],
        'chat_id': chat['id'],
        'created_by': 'user-local',
        'status': 'screenshot_done' if payload.region_screenshot_attachment_id else 'created',
        'user_instruction': payload.user_instruction,
        'selected_template': payload.selected_template,
        'region': payload.region.model_dump(),
        'viewport_transform': payload.viewport_transform,
        'region_screenshot_attachment_id': payload.region_screenshot_attachment_id,
        'full_screenshot_attachment_id': payload.full_screenshot_attachment_id,
        'ocr_result_id': None,
        'context_flags': payload.context_flags,
        'agent_run_id': None,
        'proposed_action_id': None,
        'execution_session_id': None,
        'result_status': None,
        'created_at': now,
        'updated_at': now,
    }
    store.add('obd_region_tasks', task)
    store.event(case['id'], 'obd.region_task_created', {'task_id': task['id'], 'region': task['region'], 'chat_id': chat['id'], 'instruction': payload.user_instruction})
    return serialize_task(task)


@router.get('/cases/{case_id}/obd-region-tasks')
def list_region_tasks(case_id: str, store: JsonStore = Depends(get_store)):
    case = store.find_case(case_id)
    items = [serialize_task(item) for item in store.list('obd_region_tasks') if item['case_id'] == case['id']]
    return {'items': sorted(items, key=lambda item: item['created_at'], reverse=True), 'has_more': False}


@router.get('/obd-region-tasks/{task_id}')
def get_region_task(task_id: str, store: JsonStore = Depends(get_store)):
    return serialize_task(store.get('obd_region_tasks', task_id))


@router.patch('/obd-region-tasks/{task_id}')
def update_region_task(task_id: str, payload: ObdRegionTaskPatch, store: JsonStore = Depends(get_store)):
    patch = {key: value for key, value in payload.model_dump().items() if value is not None}
    return serialize_task(store.update('obd_region_tasks', task_id, patch))


@router.post('/obd-region-tasks/{task_id}/ocr')
def run_ocr(task_id: str, store: JsonStore = Depends(get_store)):
    task = store.get('obd_region_tasks', task_id)
    store.update('obd_region_tasks', task_id, {'status': 'ocr_pending'})
    parsed = omniparser.parse_task_region(store, task)
    text = parsed['text']
    ocr_result = {
        'id': new_id(),
        'workspace_id': task['workspace_id'],
        'case_id': task['case_id'],
        'obd_region_task_id': task_id,
        'source_attachment_id': task.get('region_screenshot_attachment_id'),
        'engine': parsed['engine'],
        'engine_version': parsed['engine_version'],
        'language': parsed['language'],
        'text': text,
        'confidence': parsed['confidence'],
        'parsed_elements': parsed['parsed_elements'],
        'annotated_image_attachment_id': parsed['annotated_image_attachment_id'],
        'latency_ms': parsed['latency_ms'],
        'cache_hit': parsed['cache_hit'],
        'error': parsed['error'],
        'created_at': now_iso(),
    }
    store.add('ocr_results', ocr_result)
    store.update('obd_region_tasks', task_id, {'status': 'ocr_done', 'ocr_result_id': ocr_result['id']})
    store.event(task['case_id'], 'obd.ocr_completed', {'task_id': task_id, 'ocr_result_id': ocr_result['id'], 'text': text})
    store.event(task['case_id'], 'search.index_requested', {'source_type': 'ocr_result', 'source_id': ocr_result['id']})
    return {'task': store.get('obd_region_tasks', task_id), 'ocr_result': ocr_result}


@router.get('/ocr-results/{ocr_result_id}')
def get_ocr_result(ocr_result_id: str, store: JsonStore = Depends(get_store)):
    return store.get('ocr_results', ocr_result_id)


@router.patch('/ocr-results/{ocr_result_id}')
def update_ocr_result(ocr_result_id: str, payload: OcrPatch, store: JsonStore = Depends(get_store)):
    result = store.update('ocr_results', ocr_result_id, {'text': payload.text, 'correction_reason': payload.correction_reason, 'corrected_at': now_iso(), 'corrected_by': 'user-local'})
    if result.get('case_id'):
        store.event(result['case_id'], 'obd.ocr_corrected', {'ocr_result_id': result['id'], 'correction_reason': payload.correction_reason})
        store.event(result['case_id'], 'search.index_requested', {'source_type': 'ocr_result', 'source_id': result['id']})
    return result
