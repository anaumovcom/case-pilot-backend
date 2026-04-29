from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', env_file_encoding='utf-8', extra='ignore')

    app_env: str = 'dev'
    app_name: str = 'case-pilot-backend'
    version: str = '0.1.0'
    api_prefix: str = '/api'
    cors_origins: str = 'http://localhost:5173'
    cors_allow_origin_regex: str | None = r'chrome-extension://.*'
    local_storage_path: Path = Field(default=Path('./storage'))
    store_backend: str = 'json'
    database_url: str = 'postgresql+psycopg://casepilot:casepilot@localhost:5432/casepilot'
    postgres_pool_min_size: int = 1
    postgres_pool_max_size: int = 10
    redis_url: str = 'redis://localhost:6379/0'

    vector_backend: str = 'disabled'
    qdrant_url: str = 'http://localhost:6333'
    qdrant_api_key: str | None = None
    qdrant_collection: str = 'casepilot_documents'
    embedding_provider: str = 'local-hash'
    embedding_dimensions: int = 384

    omniparser_url: str = 'http://127.0.0.1:8001'
    ocr_engine: str = 'mock'
    ocr_fallback_to_mock: bool = True

    obd_source_mode: str = 'mock'
    obd_ws_url: str = 'ws://127.0.0.1:4455'
    obd_ws_password: str | None = None
    obd_source_name: str | None = None
    obd_frame_width: int = 0
    obd_timeout_seconds: float = 5.0
    obd_webrtc_fps: int = 12
    obd_webrtc_width: int = 1920
    obd_webrtc_quality: int = 80
    obd_webrtc_public_ip: str | None = None

    llm_provider: str = 'mock'
    ollama_base_url: str = 'http://localhost:11434'
    ollama_model: str = 'llama3.1'
    openai_api_key: str | None = None
    openai_base_url: str = 'https://api.openai.com/v1'
    openai_model: str = 'gpt-4o-mini'
    llm_timeout_seconds: float = 60.0

    esp32_bridge_mode: str = 'mock'
    esp32_base_url: str = 'http://192.168.31.234'
    esp32_ws_url: str = 'ws://192.168.31.234:81/hid'
    esp32_api_token: str = 'change-me'
    esp32_command_timeout_ms: int = 5000

    chrome_plugin_token: str = 'change-me'

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(',') if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
