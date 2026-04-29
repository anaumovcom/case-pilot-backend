from fastapi import APIRouter, Depends

from app.api.deps import get_store
from app.core.config import get_settings
from app.services.hid_bridge import HidBridge
from app.services.llm import LLMClient
from app.services.obd_source import ObdSourceClient
from app.services.omniparser_client import OmniParserClient
from app.services.store import JsonStore, now_iso
from app.services.vector_search import VectorSearchService

router = APIRouter(tags=['diagnostics'])
settings = get_settings()
hid_bridge = HidBridge(settings)
llm_client = LLMClient(settings)
omniparser = OmniParserClient(settings)
vector_search = VectorSearchService(settings)
obd_source = ObdSourceClient(settings)


@router.get('/diagnostics/components')
def diagnostics_components(store: JsonStore = Depends(get_store)):
    vector_status = vector_search.status()
    llm_status = llm_client.status()
    ocr_status = omniparser.probe()
    esp32_status = hid_bridge.status()
    obd_status = obd_source.status()
    return {
        'items': [
            {'id': 'backend', 'name': 'Backend API', 'status': 'online', 'latency_ms': 12, 'details': 'FastAPI running'},
            {'id': 'database', 'name': f'{settings.store_backend} store', 'status': 'online', 'latency_ms': 3, 'details': str(store.db_path)},
            {'id': 'qdrant', 'name': 'Qdrant vector search', 'status': vector_status.get('status'), 'latency_ms': None, 'details': vector_status},
            {'id': 'obd', 'name': 'OBD source', 'status': obd_status.get('status'), 'latency_ms': obd_status.get('latency_ms'), 'details': obd_status},
            {'id': 'ocr', 'name': 'OCR / OmniParser', 'status': ocr_status.get('status'), 'latency_ms': 132, 'details': ocr_status},
            {'id': 'agent', 'name': 'Agent layer', 'status': llm_status.get('status'), 'latency_ms': 240, 'details': llm_status},
            {'id': 'esp32', 'name': 'ESP32 HID bridge', 'status': esp32_status.get('status'), 'latency_ms': esp32_status.get('last_command_latency_ms'), 'details': esp32_status},
        ],
        'generated_at': now_iso(),
    }


@router.get('/diagnostics/resources')
def diagnostics_resources(store: JsonStore = Depends(get_store)):
    return {
        'storage_path': str(store.storage_path),
        'collections': {name: len(items) for name, items in store.data.items()},
        'generated_at': now_iso(),
    }


@router.get('/diagnostics/database')
def diagnostics_database(store: JsonStore = Depends(get_store)):
    return {'status': 'ok', 'provider': settings.store_backend, 'path': str(store.db_path), 'generated_at': now_iso()}


@router.get('/diagnostics/latencies')
def diagnostics_latencies():
    return {
        'items': [
            {'component': 'api', 'p50_ms': 18, 'p95_ms': 45},
            {'component': 'ocr_mock', 'p50_ms': 132, 'p95_ms': 180},
            {'component': 'agent_mock', 'p50_ms': 240, 'p95_ms': 320},
            {'component': 'esp32_mock', 'p50_ms': 48, 'p95_ms': 90},
        ],
        'generated_at': now_iso(),
    }


@router.get('/integrations/statuses')
def integrations_statuses():
    vector_status = vector_search.status()
    llm_status = llm_client.status()
    ocr_status = omniparser.probe()
    esp32_status = hid_bridge.status()
    obd_status = obd_source.status()
    return {
        'items': [
            {'id': 'telegram', 'name': 'Telegram Chrome Plugin API', 'status': 'ready', 'configured': True},
            {'id': 'obd', 'name': 'OBD', 'status': obd_status.get('status'), 'configured': settings.obd_source_mode in {'obs', 'mock'}, 'details': obd_status},
            {'id': 'postgres', 'name': 'PostgreSQL', 'status': settings.store_backend, 'configured': settings.store_backend == 'postgres'},
            {'id': 'qdrant', 'name': 'Qdrant', 'status': vector_status.get('status'), 'configured': settings.vector_backend == 'qdrant'},
            {'id': 'esp32', 'name': 'ESP32 HID', 'status': esp32_status.get('status'), 'configured': True, 'details': esp32_status},
            {'id': 'omniparser', 'name': 'OmniParser', 'status': ocr_status.get('status'), 'configured': settings.ocr_engine in {'omniparser', 'mock'}},
            {'id': 'llm', 'name': 'Ollama / ChatGPT API', 'status': llm_status.get('status'), 'configured': settings.llm_provider in {'auto', 'ollama', 'openai', 'chatgpt', 'mock'}},
        ],
        'generated_at': now_iso(),
    }
