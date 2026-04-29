from fastapi.testclient import TestClient

from app.main import app


def test_runtime_integration_contracts():
    with TestClient(app) as client:
        assert client.get('/api/search/status').status_code == 200
        assert client.post('/api/search/reindex').status_code == 200

        plugin_response = client.post(
            '/api/telegram/chrome-plugin/import-selection',
            json={
                'chat_title': 'Telegram Web test',
                'page_url': 'https://web.telegram.org/a/#test',
                'selected_text': 'Проверить данные по заявке и подготовить комментарий.',
            },
        )
        assert plugin_response.status_code == 200
        assert plugin_response.json()['items']

        esp32_response = client.get('/api/esp32/status')
        assert esp32_response.status_code == 200
        assert esp32_response.json()['hid_ready'] is True

        integrations_response = client.get('/api/integrations/statuses')
        assert integrations_response.status_code == 200
        integration_ids = {item['id'] for item in integrations_response.json()['items']}
        assert {'postgres', 'qdrant', 'omniparser', 'llm', 'esp32', 'telegram'} <= integration_ids
