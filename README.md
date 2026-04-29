# CasePilot Backend

FastAPI MVP backend for staged CasePilot integration.

## Run locally

```text
python -m venv .venv
.venv\\Scripts\\activate
pip install -e .[dev]
uvicorn app.main:app --reload
```

OpenAPI: http://localhost:8000/docs

## Full local stack

From the workspace root:

```text
copy .env.example .env
docker compose up --build
```

Services:

- Frontend: http://localhost:5173
- Backend/OpenAPI: http://localhost:8000/docs
- PostgreSQL: localhost:5432, db/user/password `casepilot`
- Qdrant: http://localhost:6333
- OmniParser API: http://localhost:8001/probe/
- Ollama: http://localhost:11434

For local LLM, pull a model once:

```text
docker compose exec ollama ollama pull llama3.1
```

For ChatGPT API fallback, set `OPENAI_API_KEY` in the root `.env`.

For real ESP32 HID bridge, set:

```text
ESP32_BRIDGE_MODE=http
ESP32_BASE_URL=http://<esp32-ip>
ESP32_API_TOKEN=<token-if-enabled>
```

For the Telegram Chrome Extension stub, load `case-pilot-telegram-extension` via `chrome://extensions` → Developer mode → Load unpacked.

## Current scope

This backend implements the stage contracts with switchable local JSON/PostgreSQL persistence and adapters:

- cases, chats, messages, events and local files on JSON or PostgreSQL JSONB store;
- Qdrant-backed vector search with deterministic local embeddings and exact fallback;
- OBD Region Tasks, mock OBD status and OmniParser OCR with mock fallback;
- screen agent through Ollama/OpenAI-compatible ChatGPT API with mock fallback;
- ESP32 HID execution sessions through mock/http bridge and stop command;
- memory, search, Telegram import/suggestions and Chrome extension import endpoints;
- macro CRUD and mock macro runs;
- diagnostics/integration status endpoints.
