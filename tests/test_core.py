from copy import deepcopy
from io import BytesIO
import hashlib
import json

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader, PdfWriter

from hr_assist.documents import extract_file
from hr_assist.main import create_app
from hr_assist.schemas import RoleInput, SettingsInput, CandidateNameReview
from hr_assist.storage import BusinessError
from hr_assist.service import Service
from hr_assist.db import now
from conftest import make_pdf, run


def test_auth_csrf_origin_and_secrets(environment):
    with TestClient(create_app(environment)) as client:
        assert client.get('/api/dashboard').status_code==401
        assert client.get('/healthz').json()['status']=='ok'
        response=client.post('/api/auth/login',json={'username':'管理员','password':environment.admin_password})
        assert response.status_code==200
        csrf=response.json()['csrf']
        assert 'HttpOnly' in response.headers['set-cookie']
        assert client.post('/api/settings/preflight').status_code==403
        client.headers['X-CSRF-Token']=csrf
        assert client.post('/api/settings/preflight',headers={'Origin':'https://evil.example'}).status_code==403
        assert client.put('/api/settings',json={'mail_password':'test-imap-secret','llm_api_key':'test-api-secret'}).status_code==200
        text=client.get('/api/settings').text
        assert 'test-imap-secret' not in text and 'test-api-secret' not in text
        assert client.get('/api/settings').json()['mail']['password_configured']
        assert client.post('/api/roles',json={'name':'../escape','fields':{}}).status_code==422
        assert client.post('/api/auth/logout').status_code==200
        assert client.get('/api/files/anything').status_code==401
    contents=environment.state_dir.joinpath('hr-assist.db').read_bytes()
    assert b'test-imap-secret' not in contents


def test_single_instance_lock(service,environment):
    with pytest.raises(RuntimeError,match='worker'):
        Service(environment)


def test_chunked_upload_limit(environment):
    from dataclasses import replace
    with TestClient(create_app(replace(environment,max_upload_bytes=10))) as client:
        response=client.post('/api/resumes',content=(b'x'*1024*1024 for _ in range(2)))
        assert response.status_code==413


def test_role_version_and_missing_jd(service,role_data):
    incomplete=deepcopy(role_data);incomplete['fields']['skills']=''
    with pytest.raises(BusinessError,match='JD 缺失项'):
        service.save_role(RoleInput(**incomplete))
    first=service.save_role(RoleInput(**role_data))
    role_data['fields']['skills']='Python, SQL'
    second=service.save_role(RoleInput(**role_data),first['id'])
    assert first['current_version']!=second['current_version']
    assert len(service.db.all('SELECT * FROM jd_versions'))==2
    role_data['criteria']=second['criteria']
    role_data['fields']['skills']='Python, SQL, Docker'
    third=service.save_role(RoleInput(**role_data),first['id'])
    assert next(c for c in third['criteria'] if c['id']=='skills')['description']=='Python, SQL, Docker'


def test_file_parsing_archive_dedup_and_blocked_model(service,role,pdf):
    first=service.ingest(pdf,'【软件工程师_Test】.pdf',role['id'])
    assert service.ingest(pdf,'【软件工程师_Test】.pdf')['duplicate']
    second=service.ingest(make_pdf('Another synthetic candidate with Python engineering and project experience.'),'【软件工程师_Second】.pdf',role['id'])
    for app,name in ((first,'Test'),(second,'Second')):
        service.review_candidate_name(app['id'],CandidateNameReview(name=name,expected_name='待识别',confirmed=True))
    result=run(service)
    assert result['status']=='BLOCKED'
    assert json.loads(result['stats'])['archived']==2
    for identity in (first['id'],second['id']):
        app=service.get_application(identity)
        assert app['document']['status']=='PARSED'
        assert app['status'] in {'WAITING_CONFIG','QUEUED'}
        file,path=service.storage.file(app['file_id'])
        assert hashlib.sha256(path.read_bytes()).hexdigest()==file['sha256']
    assert json.loads(run(service)['stats'])['archived']==0
    assert len(list((service.env.data_dir/'resumes'/'软件工程师').glob('*.pdf')))==2
    assert service.db.get('latest_overview')
    assert list((service.env.state_dir/'logs').glob('*.jsonl'))


def test_encrypted_pdf_quarantined(service,role,pdf):
    writer=PdfWriter();writer.append(PdfReader(BytesIO(pdf)));writer.encrypt('test-password')
    out=BytesIO();writer.write(out)
    imported=service.ingest(out.getvalue(),'【软件工程师_Test】.pdf')
    result=run(service)
    assert service.get_application(imported['id'])['status']=='PARSE_FAILED'
    assert json.loads(result['stats'])['exceptions']==1
    assert len(list((service.env.data_dir/'quarantine').glob('*.pdf')))==1


def test_classification_and_storage_boundary(service,pdf):
    app=service.ingest(pdf,'任意命名【岗位_城市_薪资】姓名_经验.pdf')
    row=service.get_application(app['id'])
    assert row['name']=='待识别' and row['role_id'] is None
    with pytest.raises(BusinessError):
        service.storage.path('../../outside')


def test_long_original_filename_keeps_parseable_type(service,pdf):
    result=service.upload_jd(pdf,'Synthetic-'*20+'.pdf')
    assert 'Python' in result['text']
    record,path=service.storage.file(result['file_id'])
    assert path.suffix=='.pdf' and hashlib.sha256(path.read_bytes()).hexdigest()==record['sha256']


class FakeModel:
    settings={'model':'test-model'}
    calls=0
    fail=False

    def complete(self,system,payload):
        self.calls+=1
        if self.fail:
            raise BusinessError('MODEL_FAILED','模拟服务中断')
        if 'available_roles' in payload:
            # A literal synthetic model response, not a production parser.
            filename=next(s for s in payload['sources'] if s['id']=='F1')['text']
            return {'name':{'status':'identified','value':'Test','evidence':[{'span_id':'F1','quote':filename}],'note':'Synthetic'},
                    'role':{'status':'identified','value':'软件工程师','role_id':payload['available_roles'][0]['id'],
                            'evidence':[{'span_id':'F1','quote':filename}],'note':'Synthetic'}},{'total_tokens':10}
        span=payload['resume_spans'][0]
        return {'judgments':[{'criterion_id':c['id'],'verdict':'met','evidence':[{'span_id':span['id'],'quote':span['text']}],'reason':'测试证据支持'} for c in payload['criteria']]}, {'total_tokens':10}


def configure(service):
    service.save_settings(SettingsInput(mail_enabled=True,mail_account='test@example.invalid',mail_password='fake',
        llm_endpoint='https://example.invalid/chat',llm_model='test-model',llm_api_key='fake',llm_approved=True,
        approval_note='Synthetic tests only',policy_confirmed=True))


def test_assessment_cache_explicit_reassessment(service,application,monkeypatch):
    configure(service)
    model=FakeModel();monkeypatch.setattr(service,'model',lambda:model)
    result=run(service,key='test-idempotent')
    assert result['status']=='SUCCESS'
    assert model.calls==1
    assert service.get_application(application['id'])['assessment']['score']==100
    assert run(service)['status']=='SUCCESS' and model.calls==1
    assert service.start_run('pending','test-idempotent')['id']==result['id']
    service.db.execute('UPDATE applications SET assessment_id=NULL WHERE id=?',(application['id'],))
    assert json.loads(run(service)['stats'])['reused']==1 and model.calls==1


def test_mail_failure_cursor_and_retry(service,role,pdf,monkeypatch):
    configure(service)
    class FakeInbox:
        uid_validity='42'
        def __init__(self,*args):pass
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def messages(self,*args):
            yield {'uid':'7','received_at':now(),'message_id':'<test-message>','attachments':[('【软件工程师_Test】.pdf',pdf)]}
    monkeypatch.setattr('hr_assist.service.Inbox',FakeInbox)
    model=FakeModel();model.fail=True;monkeypatch.setattr(service,'model',lambda:model)
    assert run(service,'mail')['status']=='BLOCKED'
    assert service.db.get('mail_cursor') is None
    assert len(service.db.all('SELECT * FROM receipts'))==1
    model.fail=False
    assert run(service,'mail')['status']=='SUCCESS'
    assert service.db.get('mail_cursor')['last_uid']==7
    assert len(service.db.all('SELECT * FROM applications'))==1
    assert len(service.db.all('SELECT * FROM assessments'))==1
    assert len(service.db.all('SELECT * FROM receipts'))==1


def test_document_review_invalidates_cache_and_preserves_history(service,application,monkeypatch):
    configure(service)
    model=FakeModel();monkeypatch.setattr(service,'model',lambda:model)
    run(service)
    service.verify_document(application['id'],'Verified synthetic Python engineer, 5 years experience.','Checked against original PDF')
    assert service.get_application(application['id'])['assessment'] is None
    run(service)
    assert model.calls==2
    updated=service.get_application(application['id'])
    assert len(updated['history'])==2
    assert updated['document']['spans'][0]['source']=='verified'
    assert len(service.db.all("SELECT * FROM meta WHERE key LIKE 'document_revision:%'"))==1
