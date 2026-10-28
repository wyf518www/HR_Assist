import json

import httpx
import pytest
from fastapi.testclient import TestClient

from hr_assist.assessment import CloudModel, MODEL_CALL_ERRORS
from hr_assist.db import dump
from hr_assist.main import create_app
from hr_assist.schemas import SettingsInput, CandidateNameReview
from hr_assist.storage import BusinessError
from conftest import make_pdf, run
from test_core import configure, FakeModel


def install_transport(monkeypatch, handler):
    client = httpx.Client
    monkeypatch.setattr('hr_assist.assessment.httpx.Client', lambda **kwargs:
                        client(transport=httpx.MockTransport(handler), **kwargs))


def model(timeout=None):
    settings = {'endpoint': 'https://model.example.invalid/chat/completions', 'approved': True, 'model': 'synthetic'}
    if timeout is not None:
        settings['timeout_seconds'] = timeout
    return CloudModel(settings, 'private-api-key')


@pytest.mark.parametrize('timeout', [None, 30, 90, 300, 600])
def test_wait_setting_sent_to_transport(monkeypatch, timeout):
    calls = []
    def handler(request):
        calls.append(request)
        assert request.extensions['timeout']['read'] == (timeout or 300)
        assert request.extensions['timeout']['connect'] == 30
        return httpx.Response(200, json={'choices': [{'message': {'content': '{"ok":true}'}}]})
    install_transport(monkeypatch, handler)
    assert model(timeout).complete('test', {})[0] == {'ok': True}
    assert len(calls) == 1


@pytest.mark.parametrize('exception,code,hint', [
    (httpx.ReadTimeout, 'MODEL_TIMEOUT', '300'),
    (httpx.ConnectTimeout, 'MODEL_TIMEOUT', '30'),
    (httpx.ConnectError, 'MODEL_CONNECT', 'DNS'),
    (httpx.RemoteProtocolError, 'MODEL_TRANSPORT', '传输'),
])
def test_transport_errors_are_specific_safe_and_not_retried(monkeypatch, exception, code, hint):
    calls = []
    def handler(request):
        calls.append(request)
        raise exception('private-api-key private-resume', request=request)
    install_transport(monkeypatch, handler)
    with pytest.raises(BusinessError) as error:
        model().complete('test', {})
    assert error.value.code == code and hint in error.value.message
    assert 'private' not in error.value.message
    assert len(calls) == 1


@pytest.mark.parametrize('status,code', [(401,'MODEL_AUTH'), (403,'MODEL_FORBIDDEN'), (404,'MODEL_ENDPOINT'),
    (429,'MODEL_RATE_LIMIT'), (400,'MODEL_REQUEST'), (422,'MODEL_REQUEST'), (503,'MODEL_UNAVAILABLE'), (302,'MODEL_HTTP')])
def test_http_errors_are_safe_and_do_not_expire_app_session(monkeypatch, status, code):
    install_transport(monkeypatch, lambda request: httpx.Response(status, text='private-api-key private-resume'))
    with pytest.raises(BusinessError) as error:
        model().complete('test', {})
    assert error.value.code == code
    assert str(status) in error.value.message and 'private' not in error.value.message
    assert error.value.status != 401


@pytest.mark.parametrize('content', ['{"ok":true}', '```json\n{"ok":true}\n```', '\ufeff {"ok":true}'])
def test_complete_json_with_optional_code_fence(monkeypatch, content):
    install_transport(monkeypatch, lambda request: httpx.Response(200, json={
        'choices': [{'message': {'content': content, 'reasoning_content': 'Never use this as a result'}}]}))
    assert model().complete('test', {})[0] == {'ok': True}


@pytest.mark.parametrize('body,code', [
    ({'choices': [{'message': {'content': '{"ok":true}'}, 'finish_reason': 'length'}]}, 'MODEL_TRUNCATED'),
    ({'choices': [{'message': {'content': None, 'reasoning_content': '{"ok":true}'}}]}, 'MODEL_EMPTY_CONTENT'),
    ({'choices': [{'message': {'content': '说明：{"ok":true}'}}]}, 'MODEL_INVALID_JSON'),
    ({'choices': [{'message': {'content': '```json\n{"ok":\n```'}}]}, 'MODEL_INVALID_JSON'),
    ({'choices': [{'message': {'content': '[]'}}]}, 'MODEL_INVALID_JSON'),
    ({'choices': [{'message': {'content': '{}'}, 'finish_reason': 'content_filter'}]}, 'MODEL_REFUSED'),
    ({'choices': []}, 'MODEL_RESPONSE_FORMAT'),
])
def test_bad_outputs_rejected_without_guessing(monkeypatch, body, code):
    install_transport(monkeypatch, lambda request: httpx.Response(200, json=body))
    with pytest.raises(BusinessError) as error:
        model().complete('test', {})
    assert error.value.code == code


@pytest.mark.parametrize('previous,expected', [(None,300), (90,300), (240,240)])
def test_wait_migration_once_and_preserves_later_choices(service, previous, expected):
    settings = service.settings()
    settings['llm']['timeout_seconds'] = previous
    service.db.put('settings', settings)
    service.migrate_model_timeout()
    assert service.settings()['llm']['timeout_seconds'] == expected
    service.save_settings(SettingsInput(llm_timeout_seconds=90))
    service.migrate_model_timeout()
    assert service.settings()['llm']['timeout_seconds'] == 90
    service.save_settings(SettingsInput())
    assert service.settings()['llm']['timeout_seconds'] == 90


def test_settings_timeout_api_bounds_and_persistence(environment):
    with TestClient(create_app(environment)) as client:
        login = client.post('/api/auth/login', json={'username': '管理员', 'password': environment.admin_password})
        client.headers['X-CSRF-Token'] = login.json()['csrf']
        assert client.get('/api/settings').json()['llm']['timeout_seconds'] == 300
        for value in (29, 601):
            assert client.put('/api/settings', json={'llm_timeout_seconds': value}).status_code == 422
        assert client.put('/api/settings', json={'llm_timeout_seconds': 450}).status_code == 200
        assert client.get('/api/settings').json()['llm']['timeout_seconds'] == 450


def test_timeout_stops_batch_keeps_archives_and_manual_resume(service, role, pdf, monkeypatch):
    configure(service)
    first = service.ingest(pdf, '【软件工程师_Test】.pdf',role['id'])
    second = service.ingest(make_pdf('Another synthetic candidate with Python skills and projects.'), '【软件工程师_Second】.pdf',role['id'])
    for app,name in ((first,'Test'),(second,'Second')):
        service.review_candidate_name(app['id'],CandidateNameReview(name=name,expected_name='待识别',confirmed=True))
    calls = []
    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout('private-api-key', request=request)
    install_transport(monkeypatch, handler)
    result = run(service)
    assert result['status'] == 'BLOCKED' and len(calls) == 1
    assert json.loads(result['stats'])['archived'] == 2
    for identity in (first['id'], second['id']):
        row = service.get_application(identity)
        assert row['document']['status'] == 'PARSED' and row['assessment'] is None
        assert service.storage.file(row['file_id'])[1].exists()
    event = service.db.one("SELECT message FROM events WHERE code='MODEL_TIMEOUT'")
    assert '300' in event['message'] and 'private-api-key' not in event['message']
    fake = FakeModel()
    monkeypatch.setattr(service, 'model', lambda: fake)
    result = run(service)
    assert result['status'] == 'SUCCESS'
    assert json.loads(result['stats'])['archived'] == 0
    assert len(service.db.all('SELECT * FROM applications')) == 2
    assert all(service.get_application(identity)['assessment'] for identity in (first['id'], second['id']))


def test_jd_timeout_logged_and_form_data_not_written(service, role, monkeypatch):
    configure(service)
    def handler(request):
        raise httpx.ReadTimeout('private-api-key', request=request)
    install_transport(monkeypatch, handler)
    before = service.roles()
    with pytest.raises(BusinessError) as error:
        service.jd_imports.analyze_text('原文测试内容，不能出现在失败日志中')
    assert error.value.code == 'MODEL_TIMEOUT'
    assert service.roles() == before
    assert '原文测试内容' not in dump(service.db.all("SELECT * FROM events WHERE code='MODEL_TIMEOUT'"))
    assert not service.jd_imports.text_analysis_lock.locked()
