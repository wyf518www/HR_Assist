"""Generation controls use the same snapshot for request, cache and history."""
from copy import deepcopy
import json

import httpx
import pytest

from hr_assist import service as service_module
from hr_assist.assessment import assessment_key
from hr_assist.db import dump
from hr_assist.schemas import SettingsInput
from hr_assist.storage import BusinessError
from conftest import run
from test_core import FakeModel


def configure(service, effort='default', limit=None):
    service.save_settings(SettingsInput(
        llm_endpoint='https://open.bigmodel.cn/api/paas/v4/chat/completions',
        llm_model='glm-5.3', llm_api_key='synthetic-secret', llm_approved=True,
        approval_note='Synthetic requests only', policy_confirmed=True,
        llm_reasoning_effort=effort, llm_max_tokens=limit))


def install_model(monkeypatch):
    requests=[]
    fake=FakeModel()
    original=httpx.Client
    def handler(request):
        body=json.loads(request.content)
        requests.append(body)
        output,usage=fake.complete(body['messages'][0]['content'],json.loads(body['messages'][1]['content']))
        return httpx.Response(200,json={'choices':[{'message':{'content':dump(output)},'finish_reason':'stop'}], 'usage':usage})
    monkeypatch.setattr('hr_assist.assessment.httpx.Client',lambda **kw:original(transport=httpx.MockTransport(handler),**kw))
    return requests


def test_changed_profile_only_reassesses_when_explicitly_requested(service,application,monkeypatch):
    configure(service)
    calls=install_model(monkeypatch)
    assert run(service)['status']=='SUCCESS'
    old=service.get_application(application['id'])['assessment_id']
    followup=service.db.one('SELECT * FROM followups WHERE application_id=?',(application['id'],))
    configure(service,'low',32768)
    assert run(service)['status']=='SUCCESS'
    assert len(calls)==2 and service.get_application(application['id'])['assessment_id']==old
    # Same state transition as the explicit reassess endpoint, retaining history.
    service.db.execute("UPDATE applications SET assessment_id=NULL,status='QUEUED' WHERE id=?",(application['id'],))
    assert run(service)['status']=='SUCCESS'
    current=service.get_application(application['id'])
    assert len(calls)==4 and current['assessment_id']!=old
    assert calls[2]['reasoning_effort']=='low' and calls[2]['max_tokens']==32768
    assert current['assessment']['generation_profile']=={'reasoning_effort':'low','max_tokens':32768}
    assert len(service.db.all('SELECT * FROM assessments'))==2
    assert service.db.one('SELECT * FROM followups WHERE application_id=?',(application['id'],))==followup
    # Explicitly reassess again under identical settings: reuse that profile.
    service.db.execute("UPDATE applications SET assessment_id=NULL,status='QUEUED' WHERE id=?",(application['id'],))
    assert json.loads(run(service)['stats'])['reused']==1
    assert len(calls)==4


def test_settings_saved_after_key_do_not_change_inflight_request_or_audit(service,application,monkeypatch):
    configure(service,'low',32768)
    calls=install_model(monkeypatch)
    keys=[]
    def changing_key(app,jd,policy,model,spans):
        keys.append(assessment_key(app,jd,policy,model,spans))
        configure(service,'high',65536)
        return keys[-1]
    monkeypatch.setattr(service_module,'assessment_key',changing_key)
    assert run(service)['status']=='SUCCESS'
    assert calls[0]['reasoning_effort']=='low' and calls[0]['max_tokens']==32768
    row=service.db.one('SELECT * FROM assessments')
    assert row['cache_key']==keys[0]
    assert json.loads(row['result'])['generation_profile']=={'reasoning_effort':'low','max_tokens':32768}
    assert service.settings()['llm']['reasoning_effort']=='high'


def test_snapshot_creation_detects_concurrent_setting_save_before_sending(service,monkeypatch):
    configure(service,'low')
    before=deepcopy(service.settings()['llm'])
    calls=install_model(monkeypatch)
    configure(service,'high')
    with pytest.raises(BusinessError) as error:
        service.model_snapshot(before)
    assert error.value.code=='MODEL_REQUEST' and not calls
