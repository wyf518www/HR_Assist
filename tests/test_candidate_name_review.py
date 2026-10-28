from copy import deepcopy
import json

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from hr_assist.assessment import assessment_key, name_looks_like_location, redact
from hr_assist.db import dump
from hr_assist.main import create_app
from hr_assist.schemas import CandidateNameReview
from hr_assist.storage import BusinessError
from conftest import make_pdf, run
from test_core import FakeModel, configure


def review(name='Alex Example', expected='Beijing'):
    return CandidateNameReview(name=name, expected_name=expected, confirmed=True)


@pytest.mark.parametrize('phrase', ['工作可以在北京，也可以出差。', '工作地点：北京', '意向城市:北京', 'base 北京', 'BASE: Beijing'])
def test_ambiguous_filename_name_is_blocked_before_redaction(phrase):
    name = 'Beijing' if 'Beijing' in phrase else '北京'
    spans = [{'id': 'R1', 'text': phrase}]
    original = deepcopy(spans)
    with pytest.raises(BusinessError) as error:
        redact(spans, name)
    assert error.value.code == 'NAME_REVIEW_REQUIRED'
    assert name not in error.value.message and spans == original


def test_correct_name_preserves_location_but_hides_person_and_contact():
    spans = [{'id': 'R1', 'text': '姓名：张测试\n张测试 工作可以在北京，也可以出差。\n北京市项目\n张测试 test@example.com 13812345678'}]
    before = deepcopy(spans)
    safe = redact(spans, '张测试', name_confirmed=True)[0]['text']
    assert '工作可以在北京' in safe and '北京市项目' in safe
    assert all(value not in safe for value in ['张测试', 'test@example.com', '13812345678'])
    assert spans == before


def test_confirmed_name_still_redacted_even_when_same_as_location():
    spans = [{'id': 'R1', 'text': '姓名：北京\n工作可以在北京'}]
    assert '北京' not in redact(spans, '北京', name_confirmed=True)[0]['text']
    assert not name_looks_like_location(spans, '待确认')


def test_name_and_confirmation_participate_in_cache_key():
    application = {'file_id': 'f', 'role_id': 'r', 'name': 'Beijing'}
    rest = ({'id': 'j'}, {}, {'model': 'test'}, [])
    original = assessment_key(application, *rest)
    changed = assessment_key({**application, 'name': 'Alex Example'}, *rest)
    confirmed = assessment_key({**application, 'name_confirmed': True}, *rest)
    assert len({original, changed, confirmed}) == 3


def test_city_name_blocked_then_human_correction_preserves_data_and_allows_scoring(service, role, monkeypatch):
    configure(service)
    pdf = make_pdf('Alex Example. Work base Beijing. Python engineering with 5 years of project experience.')
    application = service.ingest(pdf, '【软件工程师_Beijing_Alex Example】.pdf',role['id'])
    service.db.execute("UPDATE applications SET name='Beijing' WHERE id=?",(application['id'],))
    before = service.db.one('SELECT * FROM followups WHERE application_id=?', (application['id'],))
    class CapturingModel(FakeModel):
        def complete(self, system, payload):
            self.payload = deepcopy(payload)
            if 'available_roles' in payload:
                self.calls+=1
                return {'name':{'status':'uncertain','value':'','evidence':[],'note':'待核验'},
                        'role':{'status':'uncertain','value':'','evidence':[],'role_id':None,'note':'人工选定岗位保留'}},{}
            return super().complete(system, payload)
    model = CapturingModel()
    monkeypatch.setattr(service, 'model', lambda: model)
    record = run(service)
    assert model.calls == 1 and json.loads(record['stats'])['assessed'] == 0
    detail = service.get_application(application['id'])
    assert detail['name_review_required'] and not detail['name_confirmed']
    assert detail['status'] == 'WAITING_IDENTITY'
    service.review_candidate_name(application['id'], review())
    detail = service.get_application(application['id'])
    assert detail['name'] == 'Alex Example' and detail['name_confirmed'] and not detail['name_review_required']
    assert model.calls == 1 and detail['assessment'] is None  # Save itself does not call a model.
    record = run(service)
    assert json.loads(record['stats'])['assessed'] == 1 and model.calls == 2
    text = dump(model.payload)
    assert 'base Beijing' in text and 'Alex Example' not in text and '姓名已移除' in text
    assessed = service.get_application(application['id'])
    service.review_candidate_name(application['id'], review(expected='Alex Example'))
    assert service.get_application(application['id'])['assessment_id'] == assessed['assessment_id']
    assert len(service.get_application(application['id'])['history']) == 1
    assert service.storage.file(application['file_id'])[1].read_bytes() == pdf
    assert service.db.one('SELECT * FROM followups WHERE application_id=?', (application['id'],)) == before
    assert len(service.db.all("SELECT * FROM meta WHERE key LIKE 'candidate_name_revision:%'")) == 2
    events = dump(service.db.all('SELECT code,message FROM events'))
    assert 'Alex Example' not in events and 'Beijing' not in events


def test_review_blocks_stale_or_busy_writes_and_releases_lock(service, application):
    with pytest.raises(BusinessError) as error:
        service.review_candidate_name(application['id'], review(expected='wrong'))
    assert error.value.code == 'NAME_CHANGED' and not service.run_lock.locked()
    service.run_lock.acquire()
    try:
        with pytest.raises(BusinessError) as error:
            service.review_candidate_name(application['id'], review(expected='Test'))
        assert error.value.code == 'RUN_BUSY'
    finally:
        service.run_lock.release()
    assert service.get_application(application['id'])['name'] == 'Test'
    with pytest.raises(BusinessError) as error:
        service.review_candidate_name('missing-id', review())
    assert error.value.code == 'APPLICATION_NOT_FOUND' and not service.run_lock.locked()


def test_manually_confirmed_identity_skips_extraction_call_budget(service, role, monkeypatch):
    configure(service)
    settings = service.settings()
    settings['llm']['max_calls_per_run'] = 1
    service.db.put('settings', settings)
    valid = service.ingest(make_pdf('Alex Example. Synthetic Python engineering projects with 5 years experience.'), '【软件工程师_Alex Example】.pdf',role['id'])
    service.review_candidate_name(valid['id'],review(expected='待识别'))
    model = FakeModel()
    monkeypatch.setattr(service, 'model', lambda: model)
    result = run(service)
    assert model.calls == 1 and json.loads(result['stats'])['assessed'] == 1
    assert service.get_application(valid['id'])['assessment']
    assert not service.db.one("SELECT 1 FROM events WHERE code='MODEL_BUDGET'")


@pytest.mark.parametrize('name', ['', '待确认', ' 张测试', '张测试\n', '<script>', 'a'*81])
def test_review_requires_valid_confirmed_name(name):
    with pytest.raises(ValidationError):
        CandidateNameReview(name=name, expected_name='Test', confirmed=True)


def test_name_review_api_requires_login_csrf_and_explicit_confirmation(environment):
    with TestClient(create_app(environment)) as client:
        service = client.app.state.service
        application = service.ingest(make_pdf(), '【TestRole_Test】.pdf')
        url = '/api/applications/' + application['id'] + '/verify-name'
        body = {'name': 'Alex Example', 'expected_name': '待识别', 'confirmed': True}
        assert client.post(url, json=body).status_code == 401
        session = client.post('/api/auth/login', json={'username': '管理员', 'password': environment.admin_password}).json()
        assert client.post(url, json=body).status_code == 403
        client.headers['X-CSRF-Token'] = session['csrf']
        assert client.post(url, json={**body, 'confirmed': False}).status_code == 422
        assert client.post(url, json=body).status_code == 200
        assert client.get('/api/applications/' + application['id']).json()['name_confirmed']
        assert client.post(url, json=body).status_code == 409
