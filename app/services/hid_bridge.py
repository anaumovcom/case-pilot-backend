from __future__ import annotations

import json
import threading
import time
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

import httpx
from websockets.exceptions import WebSocketException
from websockets.sync.client import ClientConnection, connect

from app.core.config import Settings
from app.services.store import now_iso


STATUS_CACHE_TTL_SECONDS = 30.0
HTTP_STATUS_TIMEOUT_SECONDS = 1.5


class HidBridge:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._command_lock = threading.Lock()
        self._ws_lock = threading.Lock()
        self._http_lock = threading.Lock()
        self._http_client = httpx.Client()
        self._ws_connection: ClientConnection | None = None
        self._last_status_payload: dict[str, Any] | None = None
        self._last_status_at = 0.0
        self._last_transport: str | None = None
        self._last_command_at = 0.0
        self._last_command_latency_ms: int | None = None

    def status(self) -> dict[str, Any]:
        if self.settings.esp32_bridge_mode == 'mock':
            return self._mock_status()
        if self._prefer_ws() and self._has_fresh_command_activity():
            return self._cached_status(error='HTTP status probe skipped while WS command channel is active')
        try:
            return self._http_status(timeout_seconds=HTTP_STATUS_TIMEOUT_SECONDS)
        except Exception as exc:  # pragma: no cover - external device
            if self._prefer_ws() and self._has_recent_status_cache():
                return self._cached_status(error=str(exc))
            return {**self._mock_status(), 'status': 'offline', 'error': str(exc), 'bridge_mode': self.settings.esp32_bridge_mode}

    def send_command(self, command_type: str, payload: dict[str, Any], timeout_ms: int | None = None) -> dict[str, Any]:
        if self.settings.esp32_bridge_mode == 'mock':
            return {'ok': True, 'bridge': 'mock', 'command_type': command_type, 'payload': payload, 'ack_at': now_iso()}
        command_timeout_ms = timeout_ms or self.settings.esp32_command_timeout_ms
        with self._command_lock:
            if self._prefer_ws():
                try:
                    return self._ws_send_command(command_type, payload, command_timeout_ms)
                except Exception:
                    self._close_ws_connection()
                    if self.settings.esp32_bridge_mode == 'ws':
                        raise
            return self._http_send_command(command_type, payload, command_timeout_ms)

    def stop(self) -> dict[str, Any]:
        return self.send_command('system.stop', {}, self.settings.esp32_command_timeout_ms)

    def _prefer_ws(self) -> bool:
        return bool(self.settings.esp32_ws_url)

    def _http_status(self, timeout_seconds: float) -> dict[str, Any]:
        with self._http_lock:
            response = self._http_client.get(
                f'{self.settings.esp32_base_url.rstrip("/")}/hid/status',
                headers=self._headers(),
                timeout=timeout_seconds,
            )
        response.raise_for_status()
        payload = self._normalize_status_payload(response.json(), transport='http')
        self._remember_status(payload)
        return payload

    def _http_send_command(self, command_type: str, payload: dict[str, Any], timeout_ms: int) -> dict[str, Any]:
        body = {'id': str(uuid4()), 'type': command_type, 'payload': payload, 'timeoutMs': timeout_ms}
        started = time.perf_counter()
        with self._http_lock:
            response = self._http_client.post(
                f'{self.settings.esp32_base_url.rstrip("/")}/hid/command',
                headers=self._headers(),
                json=body,
                timeout=timeout_ms / 1000 + 5,
            )
        response.raise_for_status()
        raw_result = response.json()
        result = {
            **raw_result,
            'ok': raw_result.get('status', 'ok') == 'ok',
            'bridge': self.settings.esp32_bridge_mode,
            'transport': 'http',
        }
        self._remember_command(result.get('durationMs') or int((time.perf_counter() - started) * 1000), transport='http')
        return result

    def _ws_send_command(self, command_type: str, payload: dict[str, Any], timeout_ms: int) -> dict[str, Any]:
        websocket = self._get_ws_connection()
        command_id = str(uuid4())
        command = {'id': command_id, 'type': command_type, 'payload': payload, 'timeoutMs': timeout_ms}
        started = time.perf_counter()
        websocket.send(json.dumps(command))

        deadline = time.monotonic() + timeout_ms / 1000 + 2
        while True:
            remaining = max(0.1, deadline - time.monotonic())
            message = websocket.recv(timeout=remaining)
            payload_json = json.loads(message)
            if payload_json.get('event') == 'ready':
                continue
            if payload_json.get('id') != command_id:
                continue

            result = {
                **payload_json,
                'ok': payload_json.get('status') == 'ok',
                'bridge': self.settings.esp32_bridge_mode,
                'transport': 'ws',
            }
            self._remember_command(payload_json.get('durationMs') or int((time.perf_counter() - started) * 1000), transport='ws')
            return result

    def _get_ws_connection(self) -> ClientConnection:
        with self._ws_lock:
            if self._ws_connection is not None:
                return self._ws_connection
            websocket = connect(
                self.settings.esp32_ws_url,
                additional_headers=self._headers(),
                open_timeout=5,
                close_timeout=1,
            )
            try:
                ready_message = websocket.recv(timeout=2)
                ready_payload = json.loads(ready_message)
                if ready_payload.get('event') != 'ready':
                    raise RuntimeError('ESP32 WS handshake failed')
            except TimeoutError:
                pass
            except json.JSONDecodeError as exc:
                websocket.close()
                raise RuntimeError('ESP32 WS handshake returned invalid payload') from exc
            self._ws_connection = websocket
            self._remember_command(self._last_command_latency_ms or 0, transport='ws')
            return websocket

    def _close_ws_connection(self) -> None:
        with self._ws_lock:
            if self._ws_connection is None:
                return
            try:
                self._ws_connection.close()
            except WebSocketException:
                pass
            finally:
                self._ws_connection = None

    def _remember_command(self, latency_ms: int, transport: str) -> None:
        self._last_command_at = time.monotonic()
        self._last_command_latency_ms = latency_ms
        self._last_transport = transport
        if self._last_status_payload is None:
            self._last_status_payload = self._normalize_status_payload({}, transport=transport)
            self._last_status_at = time.monotonic()
        else:
            self._last_status_payload['status'] = 'online'
            self._last_status_payload['last_command_latency_ms'] = latency_ms
            self._last_status_payload['last_heartbeat_at'] = now_iso()
            self._last_status_payload['transport'] = transport
            self._last_status_payload['bridge_mode'] = self.settings.esp32_bridge_mode
            self._last_status_at = time.monotonic()

    def _remember_status(self, payload: dict[str, Any]) -> None:
        self._last_status_payload = dict(payload)
        self._last_status_at = time.monotonic()
        self._last_transport = str(payload.get('transport') or self._last_transport or 'http')
        latency_ms = payload.get('last_command_latency_ms')
        if isinstance(latency_ms, int):
            self._last_command_latency_ms = latency_ms

    def _cached_status(self, error: str | None = None) -> dict[str, Any]:
        payload = dict(self._last_status_payload or self._normalize_status_payload({}, transport=self._last_transport or 'ws'))
        payload['status'] = 'online'
        payload['bridge_mode'] = self.settings.esp32_bridge_mode
        payload['transport'] = self._last_transport or payload.get('transport') or 'ws'
        payload['last_heartbeat_at'] = now_iso()
        if self._last_command_latency_ms is not None:
            payload['last_command_latency_ms'] = self._last_command_latency_ms
        if error:
            payload['warning'] = error
        return payload

    def _has_recent_status_cache(self) -> bool:
        return self._last_status_payload is not None and (time.monotonic() - self._last_status_at) <= STATUS_CACHE_TTL_SECONDS

    def _has_fresh_command_activity(self) -> bool:
        return (time.monotonic() - self._last_command_at) <= STATUS_CACHE_TTL_SECONDS

    def _normalize_status_payload(self, payload: dict[str, Any], transport: str) -> dict[str, Any]:
        normalized = {**payload}
        normalized.setdefault('ok', True)
        normalized.setdefault('status', 'online' if normalized.get('ok', True) else 'offline')
        normalized.setdefault('bridge_mode', self.settings.esp32_bridge_mode)
        normalized.setdefault('transport', transport)
        normalized.setdefault('firmware_version', normalized.get('fw'))
        normalized.setdefault('hid_ready', normalized.get('hidReady', transport == 'ws'))
        normalized.setdefault('emergency_stop_active', normalized.get('emergencyStop', False))
        normalized.setdefault('queue_depth', normalized.get('queueDepth', 0))
        normalized.setdefault('last_command_latency_ms', self._last_command_latency_ms)
        normalized.setdefault('last_heartbeat_at', now_iso())
        normalized.setdefault('ip', normalized.get('ip') or urlparse(self.settings.esp32_base_url).hostname or urlparse(self.settings.esp32_ws_url).hostname)
        return normalized

    def _headers(self) -> dict[str, str]:
        if not self.settings.esp32_api_token:
            return {}
        return {'Authorization': f'Bearer {self.settings.esp32_api_token}', 'X-Api-Token': self.settings.esp32_api_token}

    def _mock_status(self) -> dict[str, Any]:
        return {
            'status': 'online',
            'bridge_mode': 'mock',
            'firmware_version': 'mock-0.1.0',
            'ip': '127.0.0.1',
            'hid_ready': True,
            'emergency_stop_active': False,
            'last_command_latency_ms': 48,
            'queue_depth': 0,
            'last_heartbeat_at': now_iso(),
        }
