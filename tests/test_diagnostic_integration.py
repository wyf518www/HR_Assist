from datetime import datetime
from zoneinfo import ZoneInfo
import json

from fastapi.testclient import TestClient
import pytest

from conftest import run
from test_core import configure, FakeModel
from hr_assist.db import uid
from hr_assist.diagnostics import emit
from hr_assist.main import create_app
from hr_assist.schemas import SettingsInput
from hr_assist.storage import BusinessError


def today():
    return datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()


def test_old_settings_get_default_and_optional_fields_preserve_choices(service):
    config=service.db.get('settings');config.pop('diagnostics',None);service.db.put('settings',config)
    assert service.public_settings()['diagnostics']=={'enabled':True,'retention_days':7,'cleanup_time':'03:00'}
    service.save_settings(SettingsInput(diagnostics_enabled=False,diagnostics_retention_days=3,diagnostics_cleanup_time='04:30'))
    service.save_settings(SettingsInput())
    assert service.settings()['diagnostics']=={'enabled':False,'retention_days':3,'cleanup_time':'04:30'}
    for data in ({'diagnostics_retention_days':0},{'diagnostics_retention_days':31},{'diagnostics_cleanup_time':'25:00'}):
        with pytest.raises(ValueError):SettingsInput(**data)


def test_diagnostic_api_auth_filters_and_safe_download(environment):
    app=create_app(environment)
    with TestClient(app) as client:
        for path in ('/api/diagnostics','/api/diagnostics/records?day='+today(),'/api/diagnostics/download?day='+today()):
            assert client.get(path).status_code==401
        client.post('/api/auth/login',json={'username':environment.admin_username,'password':environment.admin_password})
        first,second=uid(),uid()
        for key in (first,second):
            with app.state.service.diagnostics.operation('identity',run_id=key):
                emit('MODEL_REQUEST_STARTED',request_id=uid(),payload_characters=42,raw='Secret resume')
        response=client.get('/api/diagnostics/records',params={'day':today(),'run_id':first,'limit':2})
        assert len(response.json())==2 and all(r['run_id']==first for r in response.json())
        response=client.get('/api/diagnostics/download',params={'day':today(),'run_id':first})
        assert response.status_code==200 and 'attachment;' in response.headers['content-disposition']
        assert response.headers['cache-control']=='no-store'
        assert 'Secret resume' not in response.text and second not in response.text
        assert len(response.text.splitlines())==3
        assert client.get('/api/diagnostics/records',params={'day':'../secret'}).status_code==400
        assert client.get('/api/diagnostics/download',params={'day':today(),'run_id':'invalid'}).status_code==400
        assert client.get('/api/diagnostics/records',params={'day':today(),'limit':1001}).status_code==422


@pytest.mark.parametrize('phase',['identity','assessment'])
def test_pending_failure_distinguishes_phase_and_preserves_manual_data(service,application,monkeypatch,phase):
    configure(service)
    original=service.db.one('SELECT * FROM followups WHERE application_id=?',(application['id'],))
    def fail(*args,**kwargs):
        emit('MODEL_REQUEST_STARTED',request_id=uid(),read_timeout_seconds=300)
        raise BusinessError('MODEL_TIMEOUT','测试响应等待超时。')
    if phase=='identity':monkeypatch.setattr(service.identities,'resolve',fail)
    else:monkeypatch.setattr('hr_assist.service.assess',fail)
    result=run(service)
    assert result['status']=='BLOCKED'
    assert json.loads(result['stats'])['failed_phase']==phase
    assert ('姓名与岗位识别' if phase=='identity' else '简历评分') in result['error']
    records=service.diagnostics.read(today(),run_id=result['id'])
    failed=[r for r in records if r['event']=='PHASE_FAILED']
    assert failed[-1]['phase']==phase and failed[-1]['application_id']==application['id']
    request=next(r for r in records if r['event']=='MODEL_REQUEST_STARTED')
    assert request['trace_id']==failed[-1]['trace_id']
    assert service.db.one('SELECT * FROM followups WHERE application_id=?',(application['id'],))==original


def test_unwritable_diagnostics_does_not_break_assessment(service,application,monkeypatch):
    configure(service)
    model=FakeModel();monkeypatch.setattr(service,'model',lambda:model)
    def denied(*args,**kwargs):raise PermissionError('private-folder')
    monkeypatch.setattr(service.diagnostics,'_open',denied)
    assert run(service)['status']=='SUCCESS' and model.calls==1
    assert service.diagnostics.status()['write_error']=='WRITE_FAILED'


def test_cleanup_runs_without_recruitment_scheduler(service,monkeypatch):
    calls=[]
    monkeypatch.setattr(service.diagnostics,'cleanup_due',lambda:calls.append(True))
    service.startup()
    assert calls==[True] and service.maintenance_thread.is_alive()
    assert service.scheduler_thread is None
    service.shutdown()
    assert not service.maintenance_thread.is_alive()


def test_internal_failure_diagnostic_omits_exception_text(service,application,monkeypatch):
    configure(service)
    def broken(*args):raise RuntimeError('private-candidate private-api-key')
    monkeypatch.setattr(service.identities,'resolve',broken)
    result=run(service)
    assert result['status']=='BLOCKED'
    data=service.diagnostics.export(today(),run_id=result['id']).decode()
    assert 'private-candidate' not in data and 'private-api-key' not in data
    assert 'RuntimeError' in data and 'stack' in data
