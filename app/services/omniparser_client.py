from __future__ import annotations

import base64
import time
from pathlib import Path
from typing import Any

import httpx

from app.core.config import Settings
from app.services.factory import Store


MOCK_OCR_TEXT = 'Комментарий для клиента: Введите комментарий для клиента...'


def mock_ocr_result(task: dict[str, Any], engine: str = 'mock-omniparser', error: str | None = None) -> dict[str, Any]:
    return {
        'engine': engine,
        'engine_version': '0.1.0',
        'language': 'ru',
        'text': MOCK_OCR_TEXT,
        'confidence': 0.92 if error is None else 0.5,
        'parsed_elements': {
            'plain_text': MOCK_OCR_TEXT,
            'elements': [
                {
                    'id': 'el-1',
                    'type': 'input',
                    'text': 'Комментарий для клиента',
                    'bbox': task['region'],
                    'confidence': 0.91,
                    'interactable': True,
                }
            ],
            'raw': {'source': engine, 'error': error},
        },
        'annotated_image_attachment_id': None,
        'latency_ms': 0 if error is None else None,
        'cache_hit': False,
        'error': error,
    }


class OmniParserClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    def probe(self) -> dict[str, Any]:
        if self.settings.ocr_engine == 'mock':
            return {'status': 'online', 'engine': 'mock'}
        try:
            response = httpx.get(f'{self.settings.omniparser_url.rstrip("/")}/probe/', timeout=5.0)
            response.raise_for_status()
            return {'status': 'online', 'engine': self.settings.ocr_engine, 'response': response.json()}
        except Exception as exc:  # pragma: no cover - external service
            return {'status': 'offline', 'engine': self.settings.ocr_engine, 'error': str(exc)}

    def parse_task_region(self, store: Store, task: dict[str, Any]) -> dict[str, Any]:
        if self.settings.ocr_engine == 'mock':
            return mock_ocr_result(task)

        image_path = self._resolve_task_image_path(store, task)
        if image_path is None:
            if self.settings.ocr_fallback_to_mock:
                return mock_ocr_result(task, engine='mock-no-region-image', error='region screenshot attachment is missing')
            raise RuntimeError('region screenshot attachment is missing')

        try:
            base64_image = base64.b64encode(image_path.read_bytes()).decode('ascii')
            started = time.perf_counter()
            response = httpx.post(
                f'{self.settings.omniparser_url.rstrip("/")}/parse/',
                json={'base64_image': base64_image},
                timeout=120.0,
            )
            response.raise_for_status()
            payload = response.json()
            latency_ms = int((time.perf_counter() - started) * 1000)
            parsed_content = payload.get('parsed_content_list') or []
            text = self._extract_text(parsed_content) or MOCK_OCR_TEXT
            return {
                'engine': 'omniparser',
                'engine_version': 'external',
                'language': 'auto',
                'text': text,
                'confidence': 0.88,
                'parsed_elements': {
                    'plain_text': text,
                    'elements': parsed_content,
                    'raw': payload,
                },
                'annotated_image_attachment_id': None,
                'latency_ms': int(payload.get('latency', 0) * 1000) or latency_ms,
                'cache_hit': False,
                'error': None,
            }
        except Exception as exc:  # pragma: no cover - external service
            if self.settings.ocr_fallback_to_mock:
                return mock_ocr_result(task, engine='mock-omniparser-fallback', error=str(exc))
            raise

    @staticmethod
    def _extract_text(parsed_content: Any) -> str:
        if isinstance(parsed_content, str):
            return parsed_content
        chunks: list[str] = []
        if isinstance(parsed_content, list):
            for item in parsed_content:
                if isinstance(item, str):
                    chunks.append(item)
                elif isinstance(item, dict):
                    for key in ('text', 'content', 'caption', 'label'):
                        value = item.get(key)
                        if value:
                            chunks.append(str(value))
                            break
                    else:
                        chunks.append(str(item))
                else:
                    chunks.append(str(item))
        return '\n'.join(chunk for chunk in chunks if chunk).strip()

    @staticmethod
    def _resolve_task_image_path(store: Store, task: dict[str, Any]) -> Path | None:
        attachment_id = task.get('region_screenshot_attachment_id')
        if not attachment_id:
            return None
        file_id = None
        try:
            attachment = store.get('attachments', attachment_id)
            file_id = attachment.get('file_id') or attachment.get('thumbnail_file_id')
        except Exception:
            file_id = attachment_id
        if not file_id:
            return None
        try:
            path = store.file_path(file_id)
        except Exception:
            return None
        return path if path.exists() else None
