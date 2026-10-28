from copy import deepcopy

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from hr_assist.main import create_app
from hr_assist.schemas import SettingsInput
from hr_assist.storage import BusinessError


ENDPOINT='https://open.bigmodel.cn/api/paas/v4/chat/completions'


def configured(**updates):
    return SettingsInput(llm_endpoint=ENDPOINT,llm_model='glm-5.3',**updates)


def test_old_settings_read_defaults_without_migration_or_new_provider_parameters(service):
    config=service.settings()
    config['llm'].pop('reasoning_effort',None)
    config['llm'].pop('max_tokens',None)
    service.db.put('settings',config)
    before=deepcopy(service.db.get('settings'))
    public=service.public_settings()['llm']
    assert public['reasoning_effort']=='default' and public['max_tokens'] is None
    assert service.db.get('settings')==before


def test_legacy_settings_save_preserves_options_and_explicit_default_clears(service):
    service.save_settings(configured(llm_reasoning_effort='low',llm_max_tokens=32768))
    service.save_settings(configured(llm_timeout_seconds=450))
    actual=service.settings()['llm']
    assert (actual['reasoning_effort'],actual['max_tokens'],actual['timeout_seconds'])==('low',32768,450)
    service.save_settings(configured(llm_reasoning_effort='default',llm_max_tokens=None))
    actual=service.settings()['llm']
    assert actual['reasoning_effort']=='default' and actual['max_tokens'] is None


@pytest.mark.parametrize('values',[
    {'llm_reasoning_effort':None},{'llm_reasoning_effort':'disabled'},
    {'llm_reasoning_effort':'medium'},{'llm_reasoning_effort':{'private':'value'}},
    {'llm_max_tokens':True},{'llm_max_tokens':1023},{'llm_max_tokens':131073},
    {'llm_max_tokens':32768.5},{'llm_max_tokens':'32768'},
])
def test_invalid_input_is_rejected_without_coercion(values):
    with pytest.raises(ValidationError):
        SettingsInput(**values)


def test_unsupported_changed_endpoint_does_not_erase_saved_config_or_secret(service):
    service.save_settings(configured(llm_reasoning_effort='low',llm_max_tokens=32768,llm_api_key='test-old-secret'))
    before=deepcopy(service.db.get('settings'))
    secret=service.db.get('model_secret')
    with pytest.raises(BusinessError):
        service.save_settings(SettingsInput(llm_endpoint='https://other.example.invalid/chat/completions',
                                            llm_model='other',llm_api_key='test-new-secret'))
    assert service.db.get('settings')==before
    assert service.db.get('model_secret')==secret
    service.save_settings(SettingsInput(llm_endpoint='https://other.example.invalid/chat/completions',
                                        llm_model='other',llm_reasoning_effort='default',llm_max_tokens=None))
    assert service.settings()['llm']['model']=='other'


def test_settings_api_saves_clears_and_rejects_without_a_model_call(environment,monkeypatch):
    with TestClient(create_app(environment)) as client:
        def forbidden_model():
            raise AssertionError('Settings must never call a model')
        monkeypatch.setattr(client.app.state.service,'model',forbidden_model)
        login=client.post('/api/auth/login',json={'username':environment.admin_username,'password':environment.admin_password})
        client.headers['X-CSRF-Token']=login.json()['csrf']
        base={'llm_endpoint':ENDPOINT,'llm_model':'glm-5.3'}
        assert client.put('/api/settings',json={**base,'llm_reasoning_effort':'low','llm_max_tokens':32768}).status_code==200
        assert client.get('/api/settings').json()['llm']['max_tokens']==32768
        assert client.put('/api/settings',json={**base,'llm_reasoning_effort':'disabled'}).status_code==422
        assert client.put('/api/settings',json={**base,'llm_max_tokens':131073}).status_code==422
        assert client.get('/api/settings').json()['llm']['reasoning_effort']=='low'
        assert client.put('/api/settings',json=base).status_code==200
        assert client.get('/api/settings').json()['llm']['reasoning_effort']=='low'
        assert client.put('/api/settings',json={**base,'llm_reasoning_effort':'default','llm_max_tokens':None}).status_code==200
        assert client.get('/api/settings').json()['llm']['max_tokens'] is None
