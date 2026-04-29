from __future__ import annotations

import json
import re
from typing import Any

import httpx

from app.core.config import Settings


class LLMClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    def status(self) -> dict[str, Any]:
        provider = self.settings.llm_provider
        if provider == 'mock':
            return {'provider': provider, 'status': 'online'}
        if provider in {'ollama', 'auto'}:
            try:
                response = httpx.get(f'{self.settings.ollama_base_url.rstrip("/")}/api/tags', timeout=5.0)
                response.raise_for_status()
                return {'provider': 'ollama', 'status': 'online', 'model': self.settings.ollama_model}
            except Exception as exc:  # pragma: no cover - external service
                if provider == 'ollama':
                    return {'provider': 'ollama', 'status': 'offline', 'error': str(exc)}
        if provider in {'openai', 'chatgpt', 'auto'}:
            return {'provider': 'openai', 'status': 'configured' if self.settings.openai_api_key else 'missing-key', 'model': self.settings.openai_model}
        return {'provider': provider, 'status': 'unknown'}

    def screen_action(self, context: dict[str, Any], instruction: str) -> dict[str, Any]:
        provider = self.settings.llm_provider
        if provider == 'mock':
            return self.mock_screen_action(context, instruction)
        if provider in {'ollama', 'auto'}:
            try:
                return self._ollama_screen_action(context, instruction)
            except Exception as exc:  # pragma: no cover - external service
                if provider == 'ollama':
                    return self.mock_screen_action(context, instruction, warning=f'Ollama unavailable: {exc}')
        if provider in {'openai', 'chatgpt', 'auto'} and self.settings.openai_api_key:
            try:
                return self._openai_screen_action(context, instruction)
            except Exception as exc:  # pragma: no cover - external service
                return self.mock_screen_action(context, instruction, warning=f'OpenAI unavailable: {exc}')
        return self.mock_screen_action(context, instruction, warning='No LLM provider configured')

    def _ollama_screen_action(self, context: dict[str, Any], instruction: str) -> dict[str, Any]:
        response = httpx.post(
            f'{self.settings.ollama_base_url.rstrip("/")}/api/chat',
            json={
                'model': self.settings.ollama_model,
                'stream': False,
                'format': 'json',
                'messages': self._messages(context, instruction),
            },
            timeout=self.settings.llm_timeout_seconds,
        )
        response.raise_for_status()
        content = response.json().get('message', {}).get('content', '{}')
        return self._parse_decision(content)

    def _openai_screen_action(self, context: dict[str, Any], instruction: str) -> dict[str, Any]:
        response = httpx.post(
            f'{self.settings.openai_base_url.rstrip("/")}/chat/completions',
            headers={'Authorization': f'Bearer {self.settings.openai_api_key}', 'Content-Type': 'application/json'},
            json={
                'model': self.settings.openai_model,
                'temperature': 0.2,
                'response_format': {'type': 'json_object'},
                'messages': self._messages(context, instruction),
            },
            timeout=self.settings.llm_timeout_seconds,
        )
        response.raise_for_status()
        content = response.json()['choices'][0]['message']['content']
        return self._parse_decision(content)

    @staticmethod
    def _messages(context: dict[str, Any], instruction: str) -> list[dict[str, str]]:
        system = (
            'Ты CasePilot screen agent. Верни только JSON без markdown. '
            'Схема: {"message": string, "text_to_insert": string, "explanation": string, '
            '"risk_level": "low|medium|high|blocked", "confidence": number, "warnings": string[], '
            '"execution_plan": string[]}.'
        )
        user = json.dumps({'instruction': instruction, 'context': context}, ensure_ascii=False, default=str)
        return [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}]

    @staticmethod
    def _parse_decision(content: str) -> dict[str, Any]:
        text = content.strip()
        if not text.startswith('{'):
            match = re.search(r'\{.*\}', text, flags=re.DOTALL)
            if match:
                text = match.group(0)
        payload = json.loads(text)
        return {
            'message': str(payload.get('message') or 'Готова карточка действия.'),
            'text_to_insert': str(payload.get('text_to_insert') or 'Просьба проверить и подтвердить данные по кейсу CASE-024.'),
            'explanation': str(payload.get('explanation') or 'Сформулировано на основе контекста кейса.'),
            'risk_level': payload.get('risk_level') if payload.get('risk_level') in {'low', 'medium', 'high', 'blocked'} else 'low',
            'confidence': float(payload.get('confidence') or 0.75),
            'warnings': payload.get('warnings') if isinstance(payload.get('warnings'), list) else [],
            'execution_plan': payload.get('execution_plan') if isinstance(payload.get('execution_plan'), list) else [
                'Кликнуть в целевую область.',
                'Выделить текущий текст.',
                'Ввести предложенный текст.',
                'Сохранить событие в кейс.',
            ],
        }

    @staticmethod
    def mock_screen_action(context: dict[str, Any], instruction: str, warning: str | None = None) -> dict[str, Any]:
        public_id = context.get('case', {}).get('public_id') or 'CASE-024'
        ocr_text = context.get('ocr', {}).get('text') or ''
        text_to_insert = f'Просьба проверить и подтвердить данные по кейсу {public_id}.'
        warnings = [warning] if warning else []
        return {
            'message': 'В это поле нужно ввести короткий официальный комментарий для клиента.',
            'text_to_insert': text_to_insert,
            'explanation': f'Сформулировано на основе инструкции “{instruction}”, OCR и памяти кейса. OCR: {ocr_text[:120]}',
            'risk_level': 'low',
            'confidence': 0.86,
            'warnings': warnings,
            'execution_plan': [
                'Кликнуть в центр выделенной области.',
                'Выделить текущий текст через Ctrl+A.',
                'Очистить поле.',
                'Ввести предложенный текст.',
                'Сделать контрольный скриншот и записать событие в кейс.',
            ],
        }
