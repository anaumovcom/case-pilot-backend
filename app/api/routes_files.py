from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, File, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.api.deps import get_store
from app.services.store import JsonStore, new_id, now_iso

router = APIRouter(tags=['files'])


class AttachmentCreate(BaseModel):
    kind: str = 'document'
    file_id: str | None = None
    title: str
    preview_text: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


@router.post('/files/upload')
def upload_file(file: UploadFile = File(...), store: JsonStore = Depends(get_store)):
    return store.save_upload(file)


@router.get('/files/{file_id}')
def get_file(file_id: str, store: JsonStore = Depends(get_store)):
    file_object = store.get('file_objects', file_id)
    return FileResponse(store.file_path(file_id), media_type=file_object['content_type'], filename=file_object['object_key'])


@router.post('/cases/{case_id}/attachments')
def create_attachment(case_id: str, payload: AttachmentCreate, store: JsonStore = Depends(get_store)):
    case = store.find_case(case_id)
    item = {
        'id': new_id(),
        'workspace_id': case['workspace_id'],
        'case_id': case['id'],
        'kind': payload.kind,
        'file_id': payload.file_id,
        'thumbnail_file_id': None,
        'title': payload.title,
        'preview_text': payload.preview_text,
        'metadata': payload.metadata,
        'created_by': 'user-local',
        'created_at': now_iso(),
    }
    store.add('attachments', item)
    store.event(case['id'], 'attachment.created', {'attachment_id': item['id'], 'kind': item['kind']})
    return item


@router.get('/cases/{case_id}/attachments')
def list_attachments(case_id: str, store: JsonStore = Depends(get_store)):
    case = store.find_case(case_id)
    return {'items': [item for item in store.list('attachments') if item.get('case_id') == case['id']], 'has_more': False}
