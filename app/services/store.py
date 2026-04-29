from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import HTTPException, UploadFile


COLLECTIONS = [
    'users',
    'workspaces',
    'cases',
    'case_chats',
    'chat_messages',
    'case_events',
    'file_objects',
    'attachments',
    'obd_region_tasks',
    'ocr_results',
    'agents',
    'model_configs',
    'context_packages',
    'agent_runs',
    'proposed_actions',
    'execution_sessions',
    'hid_commands',
    'memory_items',
    'search_documents',
    'telegram_chats',
    'telegram_messages',
    'case_telegram_messages',
    'macros',
    'macro_steps',
    'macro_parameters',
    'macro_runs',
    'macro_run_steps',
    'audit_logs',
]


def now_iso() -> str:
    return datetime.now(UTC).isoformat().replace('+00:00', 'Z')


def new_id() -> str:
    return str(uuid4())


class JsonStore:
    """Small persistent store for local MVP development.

    The API layer is intentionally written against this repository-like boundary so it can be replaced by
    SQLAlchemy/PostgreSQL repositories without changing frontend contracts.
    """

    def __init__(self, storage_path: Path):
        self.storage_path = storage_path
        self.storage_path.mkdir(parents=True, exist_ok=True)
        self.files_path = self.storage_path / 'files'
        self.files_path.mkdir(parents=True, exist_ok=True)
        self.db_path = self.storage_path / 'dev-store.json'
        self.data = self._load()

    def _load(self) -> dict[str, list[dict[str, Any]]]:
        if self.db_path.exists():
            with self.db_path.open('r', encoding='utf-8') as file:
                loaded = json.load(file)
            for collection in COLLECTIONS:
                loaded.setdefault(collection, [])
            return loaded

        data = {collection: [] for collection in COLLECTIONS}
        self._seed(data)
        self._save(data)
        return data

    def _save(self, data: dict[str, list[dict[str, Any]]] | None = None) -> None:
        payload = data if data is not None else self.data
        tmp_path = self.db_path.with_suffix('.tmp')
        with tmp_path.open('w', encoding='utf-8') as file:
            json.dump(payload, file, ensure_ascii=False, indent=2)
        tmp_path.replace(self.db_path)

    def _seed(self, data: dict[str, list[dict[str, Any]]]) -> None:
        workspace_id = 'workspace-default'
        user_id = 'user-local'
        case_ids = {
            'CASE-024': 'case-024',
            'CASE-019': 'case-019',
            'CASE-011': 'case-011',
            'CASE-008': 'case-008',
            'CASE-003': 'case-003',
        }
        created = now_iso()
        data['workspaces'].append({'id': workspace_id, 'name': 'Default', 'slug': 'default', 'created_at': created, 'updated_at': created})
        data['users'].append({'id': user_id, 'email': 'local@casepilot.dev', 'name': 'Local User', 'role': 'admin', 'created_at': created, 'updated_at': created})
        seed_cases = [
            ('CASE-024', 'Заполнить форму', 'В работе', 'Средний', ['CRM', 'OBD', 'клиент'], 'Нужно заполнить комментарий для клиента в CRM на основе контекста переписки.', 4, 9),
            ('CASE-019', 'Ошибка авторизации', 'Нужен анализ', 'Высокий', ['логин', 'ошибка'], 'Пользователь не может войти в систему после обновления интеграции.', 2, 5),
            ('CASE-011', 'Ответ клиенту', 'Ждёт ответа', 'Средний', ['Telegram', 'ответ'], 'Нужно подготовить корректный ответ клиенту по условиям подключения.', 3, 6),
            ('CASE-008', 'Проверка настроек', 'В работе', 'Низкий', ['настройки', 'OCR'], 'Проверить параметры OCR и качество удалённого экрана.', 1, 4),
            ('CASE-003', 'Разбор переписки', 'Отложен', 'Средний', ['Telegram', 'память'], 'Разобрать длинную переписку и выделить важные факты.', 5, 14),
        ]
        for public_id, title, status, priority, tags, summary, chats_count, materials_count in seed_cases:
            case_id = case_ids[public_id]
            data['cases'].append({
                'id': case_id,
                'workspace_id': workspace_id,
                'public_id': public_id,
                'title': title,
                'description': summary,
                'status': status,
                'priority': priority,
                'tags': tags,
                'source': 'mock',
                'deadline': None,
                'summary': summary,
                'current_goal': 'Довести кейс до решения через OBD-экран и агента.',
                'result': None,
                'created_by': user_id,
                'assigned_to': user_id,
                'created_at': created,
                'updated_at': created,
                'closed_at': None,
                'chats_count': chats_count,
                'materials_count': materials_count,
            })
            chat_id = f'chat-{public_id.lower()}'
            data['case_chats'].append({
                'id': chat_id,
                'workspace_id': workspace_id,
                'case_id': case_id,
                'title': 'Работа с OBD-экраном',
                'purpose': 'current_obd',
                'context_mode': 'whole_case',
                'is_current_obd_chat': True,
                'created_by': user_id,
                'created_at': created,
                'updated_at': created,
                'archived_at': None,
                'deleted_at': None,
            })
            data['case_events'].append(self.event(case_id, 'case.created', {'public_id': public_id, 'title': title}, workspace_id, user_id, persist=False))

        data['chat_messages'].extend([
            {
                'id': 'msg-user-1',
                'workspace_id': workspace_id,
                'case_id': case_ids['CASE-024'],
                'chat_id': 'chat-case-024',
                'role': 'user',
                'author_type': 'user',
                'author_id': user_id,
                'author_name': 'Вы',
                'content_md': 'Напиши, что ввести в это поле',
                'content_json': None,
                'attachments': [{'id': 'att-region-1', 'type': 'obd_region', 'title': 'OBD-область', 'preview_text': 'Введите комментарий для клиента...'}],
                'status': 'done',
                'created_at': created,
                'updated_at': created,
            },
            {
                'id': 'msg-agent-1',
                'workspace_id': workspace_id,
                'case_id': case_ids['CASE-024'],
                'chat_id': 'chat-case-024',
                'role': 'assistant',
                'author_type': 'agent',
                'author_id': 'screen_agent',
                'author_name': 'Агент',
                'content_md': None,
                'content_json': {'action_id': 'action-1'},
                'attachments': [],
                'status': 'done',
                'created_at': created,
                'updated_at': created,
            },
        ])
        data['agents'].append({'id': 'screen_agent', 'name': 'Агент по экрану', 'description': 'Анализ OBD-областей и подготовка action card', 'created_at': created})
        data['model_configs'].append({'id': 'model-mock', 'provider': 'mock', 'name': 'Mock structured model', 'capabilities': ['text', 'json_mode'], 'created_at': created})
        data['proposed_actions'].append({
            'id': 'action-1',
            'workspace_id': workspace_id,
            'case_id': case_ids['CASE-024'],
            'obd_region_task_id': None,
            'agent_run_id': None,
            'status': 'ready',
            'risk_level': 'low',
            'payload': self.mock_action_payload(),
            'created_at': created,
            'updated_at': created,
        })
        data['memory_items'].append({
            'id': 'memory-1',
            'workspace_id': workspace_id,
            'case_id': case_ids['CASE-024'],
            'scope': 'case',
            'memory_type': 'fact',
            'status': 'confirmed',
            'text': 'Клиент просит подтвердить данные по заявке CASE-024 без внутренних технических деталей.',
            'structured_data': {},
            'source_type': 'telegram_message',
            'source_id': None,
            'confidence': 0.94,
            'created_by_type': 'user',
            'created_by': user_id,
            'created_at': created,
            'updated_at': created,
        })
        data['telegram_chats'].append({'id': 'tg-chat-1', 'workspace_id': workspace_id, 'title': 'ТехноЛюкс / интеграция', 'created_at': created})
        data['telegram_messages'].append({
            'id': 'tg-1',
            'workspace_id': workspace_id,
            'telegram_chat_id': 'tg-chat-1',
            'external_message_id': '1',
            'author_name': 'Петр Петров',
            'author_id': 'petrov',
            'message_date': created,
            'text': 'Нужно подтвердить данные по заявке и написать клиенту аккуратный комментарий.',
            'link': None,
            'context_before': [],
            'context_after': [],
            'metadata': {},
            'created_at': created,
        })
        data['macros'].append({'id': 'macro-1', 'workspace_id': workspace_id, 'name': 'Заполнить комментарий', 'description': 'Тестовый backend-driven макрос', 'version': 1, 'status': 'active', 'author_id': user_id, 'created_at': created, 'updated_at': created})
        data['macro_parameters'].append({'id': 'param-1', 'macro_id': 'macro-1', 'name': 'caseId', 'initial_value': 'CASE-024', 'created_at': created, 'updated_at': created})
        data['macro_steps'].extend([
            {'id': 'macro-step-1', 'macro_id': 'macro-1', 'sequence_no': 1, 'step_type': 'hotkey', 'payload': {'keys': ['Ctrl', 'A']}, 'delay_ms': 100, 'description': 'Выделить текущий текст', 'created_at': created, 'updated_at': created},
            {'id': 'macro-step-2', 'macro_id': 'macro-1', 'sequence_no': 2, 'step_type': 'typeText', 'payload': {'text': 'Просьба проверить и подтвердить данные по кейсу {{caseId}}.'}, 'delay_ms': 100, 'description': 'Ввести текст', 'created_at': created, 'updated_at': created},
        ])

    def mock_action_payload(self) -> dict[str, Any]:
        return {
            'type': 'text_input',
            'title': 'Агент предлагает действие',
            'text_to_insert': 'Просьба проверить и подтвердить данные по кейсу CASE-024.',
            'target_region': {'x': 640, 'y': 420, 'width': 380, 'height': 42},
            'execution_plan': [
                'Кликнуть в центр выделенной области',
                'Выделить текущий текст через Ctrl+A',
                'Очистить поле',
                'Ввести предложенный текст',
            ],
            'requires_confirmation': True,
            'risk_level': 'low',
            'explanation': 'Сформулировано на основе OCR и контекста кейса.',
        }

    def event(self, case_id: str, event_type: str, payload: dict[str, Any], workspace_id: str = 'workspace-default', actor_id: str | None = 'user-local', persist: bool = True) -> dict[str, Any]:
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
            self.data['case_events'].append(item)
            self._save()
        return item

    def list(self, collection: str) -> list[dict[str, Any]]:
        return self.data[collection]

    def add(self, collection: str, item: dict[str, Any]) -> dict[str, Any]:
        self.data[collection].append(item)
        self._save()
        return item

    def update(self, collection: str, item_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        item = self.get(collection, item_id)
        item.update(patch)
        if 'updated_at' in item:
            item['updated_at'] = now_iso()
        self._save()
        return item

    def delete(self, collection: str, item_id: str) -> None:
        before = len(self.data[collection])
        self.data[collection] = [item for item in self.data[collection] if item.get('id') != item_id]
        if len(self.data[collection]) == before:
            raise HTTPException(status_code=404, detail=f'{collection} item not found')
        self._save()

    def get(self, collection: str, item_id: str) -> dict[str, Any]:
        for item in self.data[collection]:
            if item.get('id') == item_id:
                return item
        raise HTTPException(status_code=404, detail=f'{collection} item not found')

    def find_case(self, case_id: str) -> dict[str, Any]:
        for item in self.data['cases']:
            if item['id'] == case_id or item['public_id'] == case_id:
                return item
        raise HTTPException(status_code=404, detail='case not found')

    def current_chat(self, case_id: str) -> dict[str, Any]:
        case = self.find_case(case_id)
        for chat in self.data['case_chats']:
            if chat['case_id'] == case['id'] and chat.get('is_current_obd_chat') and not chat.get('deleted_at'):
                return chat
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

    def save_upload(self, upload: UploadFile, workspace_id: str = 'workspace-default') -> dict[str, Any]:
        file_id = new_id()
        suffix = Path(upload.filename or 'upload.bin').suffix
        object_key = f'{file_id}{suffix}'
        destination = self.files_path / object_key
        with destination.open('wb') as file:
            shutil.copyfileobj(upload.file, file)
        stat = destination.stat()
        file_object = {
            'id': file_id,
            'workspace_id': workspace_id,
            'storage_provider': 'local',
            'bucket': 'local',
            'object_key': object_key,
            'content_type': upload.content_type or 'application/octet-stream',
            'size_bytes': stat.st_size,
            'sha256': None,
            'width': None,
            'height': None,
            'duration_ms': None,
            'created_by': 'user-local',
            'created_at': now_iso(),
        }
        self.add('file_objects', file_object)
        return file_object

    def file_path(self, file_id: str) -> Path:
        file_object = self.get('file_objects', file_id)
        return self.files_path / file_object['object_key']
