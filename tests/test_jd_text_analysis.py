from fastapi.testclient import TestClient
import pytest

from hr_assist.main import create_app
from hr_assist.storage import BusinessError
from test_core import configure
from test_jd_imports import TEXT, QuoteModel, output


def test_editor_analysis_redacts_validates_and_does_not_save(service, role, monkeypatch):
    configure(service)
    model=QuoteModel()
    monkeypatch.setattr(service,'model',lambda:model)
    before=service.roles()
    result=service.jd_imports.analyze_text(TEXT)
    assert result['draft']['name']=='软件工程师'
    assert 'Python' in result['draft']['fields']['skills']
    assert result['draft']['approved'] is False
    assert 'test@example.invalid' not in model.calls[0]['jd_spans'][0]['text']
    assert '13800000000' not in model.calls[0]['jd_spans'][0]['text']
    assert service.roles()==before
    assert service.db.all('SELECT * FROM jd_imports')==[]
    assert service.db.all('SELECT * FROM files')==[]
    event=service.db.one("SELECT message FROM events WHERE code='JD_TEXT_ANALYZED'")
    assert TEXT not in event['message']


def test_editor_failure_and_concurrency_leave_roles_unchanged(service, role, monkeypatch):
    configure(service)
    model=QuoteModel();model.fail=True
    monkeypatch.setattr(service,'model',lambda:model)
    before=service.roles()
    with pytest.raises(BusinessError,match='模拟模型中断'):
        service.jd_imports.analyze_text(TEXT)
    assert service.roles()==before
    assert not service.jd_imports.text_analysis_lock.locked()
    model.fail=False
    with service.jd_imports.text_analysis_lock:
        with pytest.raises(BusinessError) as error:
            service.jd_imports.analyze_text(TEXT)
        assert error.value.code=='JD_ANALYSIS_BUSY'
    assert service.jd_imports.analyze_text(TEXT)['draft']['name']=='软件工程师'


def test_editor_hallucinated_fields_are_not_returned(service, monkeypatch):
    configure(service)
    class InvalidModel:
        settings={'model':'synthetic-invalid'}
        def complete(self,system,payload):
            raw=output();raw['fields']['skills'][0]['quote']='虚构技能 Ruby'
            return raw,{}
    monkeypatch.setattr(service,'model',InvalidModel)
    with pytest.raises(BusinessError) as error:
        service.jd_imports.analyze_text(TEXT)
    assert error.value.code=='JD_EVIDENCE_INVALID'
    assert service.roles()==[]


def test_editor_text_limits_and_approved_model(service):
    for text in ('','  \n ', '字'*100001):
        with pytest.raises(BusinessError) as error:
            service.jd_imports.analyze_text(text)
        assert error.value.code=='JD_TEXT_INVALID'
    with pytest.raises(BusinessError) as error:
        service.jd_imports.analyze_text(TEXT)
    assert error.value.code=='MODEL_NOT_CONFIGURED'


def test_editor_api_requires_login_csrf_and_confirmation(environment,monkeypatch):
    app=create_app(environment)
    configure(app.state.service)
    model=QuoteModel()
    monkeypatch.setattr(app.state.service,'model',lambda:model)
    body={'text':TEXT,'confirmed':True}
    with TestClient(app) as client:
        assert client.post('/api/jd/analyze-text',json=body).status_code==401
        login=client.post('/api/auth/login',json={'username':'管理员','password':environment.admin_password})
        assert client.post('/api/jd/analyze-text',json=body).status_code==403
        client.headers['X-CSRF-Token']=login.json()['csrf']
        for bad in ({'text':TEXT},{'text':TEXT,'confirmed':False},{'text':'字'*100001,'confirmed':True}):
            assert client.post('/api/jd/analyze-text',json=bad).status_code==422
        assert not model.calls
        response=client.post('/api/jd/analyze-text',json=body)
        assert response.status_code==200
        assert response.json()['draft']['fields']['education']=='学历：本科及以上'
        assert len(model.calls)==1
        assert client.get('/api/roles').json()==[]
