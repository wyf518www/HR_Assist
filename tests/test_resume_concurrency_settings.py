from copy import deepcopy

from fastapi.testclient import TestClient
import pytest

from hr_assist.config import DEFAULT_SETTINGS
from hr_assist.main import create_app
from hr_assist.schemas import SettingsInput


@pytest.mark.parametrize('value', [1, 2, 10])
def test_supported_resume_concurrency_values(value):
    assert SettingsInput(resume_concurrency=value).resume_concurrency == value


@pytest.mark.parametrize('value', [None, 0, -1, 11, 1000, True, False, 2.0, 1.5, '2', ''])
def test_resume_concurrency_rejects_invalid_or_coerced_values(value):
    with pytest.raises(ValueError):
        SettingsInput(resume_concurrency=value)


def test_old_settings_default_to_serial_and_omitted_field_preserves_saved_choice(service):
    assert DEFAULT_SETTINGS['llm']['resume_concurrency'] == 1
    raw = service.db.get('settings')
    raw['llm'].pop('resume_concurrency')
    service.db.put('settings', raw)
    assert service.public_settings()['llm']['resume_concurrency'] == 1
    service.save_settings(SettingsInput(resume_concurrency=3))
    assert service.db.get('settings')['llm']['resume_concurrency'] == 3
    service.save_settings(SettingsInput())
    assert service.public_settings()['llm']['resume_concurrency'] == 3


def test_concurrency_api_auth_validation_and_persistence(environment):
    with TestClient(create_app(environment)) as client:
        assert client.put('/api/settings', json={'resume_concurrency': 2}).status_code == 401
        session = client.post('/api/auth/login', json={'username':environment.admin_username, 'password':environment.admin_password}).json()
        assert client.put('/api/settings', json={'resume_concurrency': 2}).status_code == 403
        client.headers['X-CSRF-Token'] = session['csrf']
        for value in (2, 10):
            response = client.put('/api/settings', json={'resume_concurrency': value, 'max_calls_per_run': 1000})
            assert response.status_code == 200
            assert response.json()['llm']['resume_concurrency'] == value
        before = deepcopy(client.get('/api/settings').json())
        for value in (0, 11, None, True, 1.5, '2'):
            assert client.put('/api/settings', json={'resume_concurrency': value}).status_code == 422
            assert client.get('/api/settings').json() == before
        client.put('/api/settings', json={'max_calls_per_run':1000})
        assert client.get('/api/settings').json()['llm']['resume_concurrency'] == 10
        assert client.get('/api/settings').json()['llm']['max_calls_per_run'] == 1000
