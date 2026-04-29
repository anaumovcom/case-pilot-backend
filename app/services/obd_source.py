from __future__ import annotations

import asyncio
import base64
from contextlib import suppress
import hashlib
import json
import re
import time
import uuid
from fractions import Fraction
from io import BytesIO
from typing import Any

from aiortc import RTCPeerConnection, RTCSessionDescription, VideoStreamTrack
from aiortc.contrib.media import MediaRelay
from aiortc.mediastreams import MediaStreamError
from av import VideoFrame
from PIL import Image
import websockets

from app.core.config import Settings
from app.services.store import now_iso


class ObdSourceClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._peer_connections: set[RTCPeerConnection] = set()
        self._relay = MediaRelay()

    def status(self) -> dict[str, Any]:
        if self.settings.obd_source_mode != 'obs':
            return self._mock_status()
        try:
            return asyncio.run(self._obs_status())
        except Exception as exc:  # pragma: no cover - external OBD source
            return {**self._mock_status(), 'status': 'offline', 'source': 'obs-websocket', 'error': str(exc)}

    def screenshot(self) -> dict[str, Any]:
        if self.settings.obd_source_mode != 'obs':
            return {
                'id': str(uuid.uuid4()),
                'status': 'created',
                'source': 'mock',
                'image_data_url': None,
                'created_at': now_iso(),
            }
        try:
            return asyncio.run(self._obs_screenshot())
        except Exception as exc:  # pragma: no cover - external OBD source
            return {
                'id': str(uuid.uuid4()),
                'status': 'offline',
                'source': 'obs-websocket',
                'image_data_url': None,
                'error': str(exc),
                'created_at': now_iso(),
            }

    async def _connect(self):
        websocket = await websockets.connect(self.settings.obd_ws_url, open_timeout=self.settings.obd_timeout_seconds, max_size=None)
        hello = json.loads(await asyncio.wait_for(websocket.recv(), timeout=self.settings.obd_timeout_seconds))
        identify: dict[str, Any] = {'rpcVersion': min(int(hello.get('d', {}).get('rpcVersion') or 1), 1)}
        auth = hello.get('d', {}).get('authentication')
        if auth:
            password = self.settings.obd_ws_password or ''
            secret = base64.b64encode(hashlib.sha256((password + auth['salt']).encode('utf-8')).digest()).decode('ascii')
            identify['authentication'] = base64.b64encode(hashlib.sha256((secret + auth['challenge']).encode('utf-8')).digest()).decode('ascii')
        await websocket.send(json.dumps({'op': 1, 'd': identify}))
        identified = json.loads(await asyncio.wait_for(websocket.recv(), timeout=self.settings.obd_timeout_seconds))
        if identified.get('op') != 2:
            await websocket.close()
            raise RuntimeError('OBD websocket identify failed')
        return websocket, hello

    async def _request(self, websocket: Any, request_type: str, request_data: dict[str, Any] | None = None) -> dict[str, Any]:
        request_id = str(uuid.uuid4())
        await websocket.send(json.dumps({
            'op': 6,
            'd': {
                'requestType': request_type,
                'requestId': request_id,
                'requestData': request_data or {},
            },
        }))
        while True:
            message = json.loads(await asyncio.wait_for(websocket.recv(), timeout=self.settings.obd_timeout_seconds))
            if message.get('op') != 7 or message.get('d', {}).get('requestId') != request_id:
                continue
            status = message['d'].get('requestStatus') or {}
            if not status.get('result'):
                raise RuntimeError(status.get('comment') or f'{request_type} failed')
            return message['d'].get('responseData') or {}

    async def _obs_status(self) -> dict[str, Any]:
        started = time.perf_counter()
        websocket, hello = await self._connect()
        try:
            version = await self._request(websocket, 'GetVersion')
            video = await self._request(websocket, 'GetVideoSettings')
            scene = await self._request(websocket, 'GetCurrentProgramScene')
            fps_denominator = int(video.get('fpsDenominator') or 1)
            fps = int(video.get('fpsNumerator') or 0) / max(fps_denominator, 1)
            return {
                'status': 'online',
                'fps': round(fps, 2),
                'latency_ms': int((time.perf_counter() - started) * 1000),
                'screen_width': int(video.get('baseWidth') or video.get('outputWidth') or 0),
                'screen_height': int(video.get('baseHeight') or video.get('outputHeight') or 0),
                'source': 'obs-websocket',
                'transport': 'websocket',
                'scene_name': scene.get('currentProgramSceneName') or scene.get('sceneName'),
                'source_name': self.settings.obd_source_name,
                'obs_version': version.get('obsVersion') or hello.get('d', {}).get('obsStudioVersion'),
                'websocket_version': version.get('obsWebSocketVersion') or hello.get('d', {}).get('obsWebSocketVersion'),
                'last_frame_at': now_iso(),
            }
        finally:
            await websocket.close()

    async def _obs_screenshot(self) -> dict[str, Any]:
        started = time.perf_counter()
        websocket, _hello = await self._connect()
        try:
            screenshot = await self._get_obs_screenshot_payload(websocket, image_format='png', image_width=self.settings.obd_frame_width or None)
            return {
                'id': str(uuid.uuid4()),
                'status': 'created',
                'source': 'obs-websocket',
                'source_name': screenshot['source_name'],
                'screen_width': screenshot['screen_width'],
                'screen_height': screenshot['screen_height'],
                'image_data_url': screenshot.get('imageData'),
                'latency_ms': int((time.perf_counter() - started) * 1000),
                'created_at': now_iso(),
            }
        finally:
            await websocket.close()

    async def create_webrtc_answer(self, sdp: str, type_: str) -> dict[str, Any]:
        if self.settings.obd_source_mode != 'obs':
            raise RuntimeError('WebRTC OBD stream is available only in obs mode')

        peer_connection = RTCPeerConnection()
        self._peer_connections.add(peer_connection)

        track = self._relay.subscribe(ObsVideoTrack(self.settings, self))
        peer_connection.addTrack(track)

        @peer_connection.on('connectionstatechange')
        async def on_connectionstatechange() -> None:
            if peer_connection.connectionState in {'failed', 'closed', 'disconnected'}:
                await self._close_peer_connection(peer_connection)

        offer = RTCSessionDescription(sdp=sdp, type=type_)
        await peer_connection.setRemoteDescription(offer)
        answer = await peer_connection.createAnswer()
        await peer_connection.setLocalDescription(answer)
        await self._wait_for_ice_gathering(peer_connection)
        local_description = peer_connection.localDescription
        if local_description is None:
            raise RuntimeError('OBD WebRTC answer is missing local description')

        return {
            'session_id': id(peer_connection),
            'sdp': self._rewrite_answer_sdp(local_description.sdp),
            'type': local_description.type,
            'screen_width': 0,
            'screen_height': 0,
            'source': 'obs-webrtc',
        }

    async def close_webrtc_session(self, session_id: int) -> None:
        for connection in list(self._peer_connections):
            if id(connection) == session_id:
                await self._close_peer_connection(connection)
                break

    async def _close_peer_connection(self, peer_connection: RTCPeerConnection) -> None:
        if peer_connection in self._peer_connections:
            self._peer_connections.remove(peer_connection)
        with suppress(Exception):
            await peer_connection.close()

    async def _wait_for_ice_gathering(self, peer_connection: RTCPeerConnection) -> None:
        if peer_connection.iceGatheringState == 'complete':
            return

        finished = asyncio.Event()

        @peer_connection.on('icegatheringstatechange')
        def on_icegatheringstatechange() -> None:
            if peer_connection.iceGatheringState == 'complete':
                finished.set()

        await asyncio.wait_for(finished.wait(), timeout=self.settings.obd_timeout_seconds)

    async def _get_obs_screenshot_payload(
        self,
        websocket: Any,
        *,
        image_format: str,
        image_width: int | None,
        image_quality: int | None = None,
    ) -> dict[str, Any]:
        video = await self._request(websocket, 'GetVideoSettings')
        screen_width = int(video.get('baseWidth') or video.get('outputWidth') or 0)
        screen_height = int(video.get('baseHeight') or video.get('outputHeight') or 0)
        source_name = self.settings.obd_source_name
        if not source_name:
            scene = await self._request(websocket, 'GetCurrentProgramScene')
            source_name = scene.get('currentProgramSceneName') or scene.get('sceneName')

        request_data: dict[str, Any] = {
            'sourceName': source_name,
            'imageFormat': image_format,
            'imageWidth': image_width if image_width and image_width > 0 else screen_width,
        }
        if image_quality is not None and image_format in {'jpeg', 'jpg'}:
            request_data['imageCompressionQuality'] = image_quality

        screenshot = await self._request(websocket, 'GetSourceScreenshot', request_data)
        return {
            **screenshot,
            'screen_width': screen_width,
            'screen_height': screen_height,
            'source_name': source_name,
        }

    @staticmethod
    def _mock_status() -> dict[str, Any]:
        return {
            'status': 'online',
            'fps': 24,
            'latency_ms': 82,
            'screen_width': 1920,
            'screen_height': 1080,
            'source': 'mock',
            'last_frame_at': now_iso(),
        }

    def _rewrite_answer_sdp(self, sdp: str) -> str:
        public_ip = self.settings.obd_webrtc_public_ip
        if not public_ip:
            return sdp

        lines: list[str] = []
        for raw_line in sdp.splitlines():
            line = raw_line.rstrip('\r')
            if line.startswith('c=IN IP4 '):
                lines.append(f'c=IN IP4 {public_ip}')
                continue

            if line.startswith('a=candidate:') and ' typ host' in line:
                lines.append(re.sub(r'^(a=candidate:\S+ \d+ \w+ \d+ )\S+( .*)$', rf'\g<1>{public_ip}\g<2>', line))
                continue

            if line.startswith('a=candidate:') and ' typ ' in line:
                continue

            lines.append(line)

        return '\r\n'.join(lines) + '\r\n'


class ObsVideoTrack(VideoStreamTrack):
    kind = 'video'

    def __init__(self, settings: Settings, source_client: ObdSourceClient):
        super().__init__()
        self.settings = settings
        self.source_client = source_client
        self.websocket: Any | None = None
        self.last_frame_at = 0.0
        self.frame_interval = 1 / max(1, self.settings.obd_webrtc_fps)

    async def recv(self) -> VideoFrame:
        if self.readyState != 'live':
            raise MediaStreamError

        pts, time_base = await self.next_timestamp()
        remaining = self.frame_interval - (time.perf_counter() - self.last_frame_at)
        if remaining > 0:
            await asyncio.sleep(remaining)

        if self.websocket is None:
            self.websocket, _hello = await self.source_client._connect()

        screenshot = await self.source_client._get_obs_screenshot_payload(
            self.websocket,
            image_format='jpeg',
            image_width=self.settings.obd_webrtc_width,
            image_quality=self.settings.obd_webrtc_quality,
        )
        frame = await decode_obs_frame(screenshot['imageData'])
        self.last_frame_at = time.perf_counter()
        frame.pts = pts
        frame.time_base = time_base or Fraction(1, 90000)
        return frame

    def stop(self) -> None:
        super().stop()
        if self.websocket is not None:
            asyncio.create_task(self.websocket.close())
            self.websocket = None


async def decode_obs_frame(image_data_url: str) -> VideoFrame:
    encoded = image_data_url.split(',', 1)[1] if ',' in image_data_url else image_data_url
    image_bytes = base64.b64decode(encoded)

    def load_frame() -> VideoFrame:
        with Image.open(BytesIO(image_bytes)) as image:
            return VideoFrame.from_image(image.convert('RGB'))

    return await asyncio.to_thread(load_frame)