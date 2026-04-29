from fastapi.testclient import TestClient

from app.main import app


def test_obd_agent_execution_search_smoke():
    with TestClient(app) as client:
        case = client.get('/api/cases').json()['items'][0]
        task_response = client.post(
            f"/api/cases/{case['id']}/obd-region-tasks",
            json={
                'user_instruction': 'Напиши, что ввести в это поле',
                'region': {'x': 640, 'y': 420, 'width': 380, 'height': 42, 'coordinate_space': 'viewport_pixels'},
                'viewport_transform': {'remote_width': 1920, 'remote_height': 1080, 'viewport_width': 1280, 'viewport_height': 720, 'scale_x': 1.5, 'scale_y': 1.5},
                'context_flags': {'case_description': True, 'case_memory': True, 'telegram': True, 'recent_chat': True, 'ocr_region': True, 'full_screen': True, 'global_memory': False, 'similar_cases': False},
            },
        )
        assert task_response.status_code == 200
        task = task_response.json()

        ocr_response = client.post(f"/api/obd-region-tasks/{task['id']}/ocr")
        assert ocr_response.status_code == 200
        assert ocr_response.json()['task']['status'] == 'ocr_done'

        agent_response = client.post(f"/api/obd-region-tasks/{task['id']}/send-to-agent")
        assert agent_response.status_code == 200
        action = agent_response.json()['proposed_action']
        assert action['status'] == 'ready'

        execution_response = client.post(f"/api/actions/{action['id']}/execute", json={'confirmed': True})
        assert execution_response.status_code == 200
        assert execution_response.json()['execution']['status'] == 'executed'

        search_response = client.get('/api/search', params={'q': 'комментарий'})
        assert search_response.status_code == 200
        assert search_response.json()['items']
