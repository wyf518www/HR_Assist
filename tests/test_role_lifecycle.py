import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from hr_assist.db import Database, dump, now, uid
from hr_assist.main import create_app
from hr_assist.schemas import RoleAction, RoleInput, RoleStatusInput
from hr_assist.storage import BusinessError
from conftest import make_pdf, run
from test_core import configure, FakeModel
from test_jd_imports import QuoteModel, TEXT, confirmation


def action(role, active=None):
    body={'confirmed':True,'expected_version':role['current_version']}
    return RoleAction(**body) if active is None else RoleStatusInput(**body,active=active)


def test_existing_database_migrates_roles_without_losing_versions(tmp_path):
    db_path=tmp_path/'hr-assist.db'
    con=sqlite3.connect(db_path)
    con.execute('CREATE TABLE roles(id TEXT PRIMARY KEY,name TEXT UNIQUE NOT NULL,is_sales INTEGER NOT NULL,current_version TEXT,created_at TEXT NOT NULL)')
    con.execute("INSERT INTO roles VALUES ('old','旧岗位',0,'version','date')")
    con.commit();con.close()
    db=Database(tmp_path)
    assert db.one("SELECT * FROM roles WHERE id='old'")['active']==1
    assert db.one("SELECT * FROM roles WHERE id='old'")['current_version']=='version'
    db.execute("UPDATE roles SET active=0 WHERE id='old'")
    assert Database(tmp_path).one("SELECT active FROM roles WHERE id='old'")['active']==0


def test_delete_unused_role_versions_retains_shared_source(service,role_data):
    source=service.upload_jd(b'Example JD with Python skills and development responsibilities.','shared.txt')
    role_data['source_file_id']=source['file_id']
    role=service.save_role(RoleInput(**role_data))
    role=service.save_role(RoleInput(**role_data),role['id'])
    other=service.save_role(RoleInput(**{**role_data,'name':'另一个岗位'}))
    assert role['can_delete'] and role['version_count']==2
    service.change_role(role['id'],action(role),delete=True)
    assert [r['id'] for r in service.roles()]==[other['id']]
    assert not service.db.all('SELECT * FROM jd_versions WHERE role_id=?',(role['id'],))
    assert service.storage.file(source['file_id'])[1].exists()
    assert service.db.one("SELECT 1 FROM events WHERE code='ROLE_DELETED'")
    with pytest.raises(BusinessError,match='删除'):
        service.save_role(RoleInput(**role_data),role['id'])


def test_delete_rechecks_newly_linked_candidates(service,role,pdf):
    assert role['can_delete']
    receipt=service.ingest(pdf,'【软件工程师_Test】.pdf',role['id'])
    with pytest.raises(BusinessError) as error:
        service.change_role(role['id'],action(role),delete=True)
    assert error.value.code=='ROLE_IN_USE'
    assert not service.roles()[0]['can_delete']
    assert service.get_application(receipt['id'])['role_id']==role['id']
    assert not service.run_lock.locked()


def test_historical_score_alone_prevents_deletion(service,role,pdf):
    # A historical score can refer to this JD even if its application has no current role.
    application=service.ingest(pdf,'historical.pdf')
    service.db.execute('INSERT INTO assessments VALUES (?,?,?,?,?,?,?)',
        (uid(),application['id'],role['current_version'],uid(),'COMPLETED','{}',now()))
    assert not service.roles()[0]['can_delete']
    with pytest.raises(BusinessError) as error:
        service.change_role(role['id'],action(role),delete=True)
    assert error.value.code=='ROLE_IN_USE'


def test_delete_imported_role_keeps_audit_and_cannot_be_recreated_by_stale_confirm(service,monkeypatch):
    configure(service)
    monkeypatch.setattr(service,'model',QuoteModel)
    row=service.jd_imports.enqueue(TEXT.encode(),'JD.txt')
    service.jd_imports.process_next()
    row=service.jd_imports.get(row['id'])
    payload=confirmation(row)
    saved=service.jd_imports.confirm(row['id'],payload)
    role=service.roles()[0]
    service.change_role(role['id'],action(role),delete=True)
    record=service.jd_imports.get(row['id'])
    assert record['status']=='ROLE_DELETED' and record['role_id'] is None and record['version_id'] is None
    assert service.storage.file(record['file_id'])[1].exists()
    with pytest.raises(BusinessError):
        service.jd_imports.confirm(row['id'],payload)
    assert service.roles()==[]


def test_disabled_roles_excluded_from_auto_manual_matching_and_draft_updates(service,role,pdf,role_data):
    service.change_role(role['id'],action(role,False))
    received=service.ingest(pdf,'【软件工程师_Test】.pdf')
    assert service.get_application(received['id'])['role_id'] is None
    with pytest.raises(BusinessError) as error:
        service.ingest(pdf,'test.pdf',role['id'])
    assert error.value.code=='ROLE_INACTIVE'
    with pytest.raises(BusinessError) as error:
        service.classify(received['id'],role['id'])
    assert error.value.code=='ROLE_INACTIVE'
    updated=service.save_role(RoleInput(**role_data),role['id'])
    assert not updated['active']
    service.change_role(role['id'],action(updated,True))
    service.classify(received['id'],role['id'])
    assert service.get_application(received['id'])['role_id']==role['id']


def test_disabled_historical_assessment_feedback_and_reports_preserved(service,role,application,monkeypatch):
    configure(service)
    model=FakeModel()
    monkeypatch.setattr(service,'model',lambda:model)
    assert run(service)['status']=='SUCCESS'
    before=service.get_application(application['id'])
    files=service.db.all('SELECT * FROM files')
    versions=service.db.all('SELECT * FROM jd_versions')
    service.change_role(role['id'],action(role,False))
    after=service.get_application(application['id'])
    assert after==before
    assert service.db.all('SELECT * FROM files')==files
    assert service.db.all('SELECT * FROM jd_versions')==versions
    assert service.roles()[0]['assessment_count']==1
    with pytest.raises(BusinessError):
        service.change_role(role['id'],action(role),delete=True)


def test_inactive_pending_paused_and_resumes_only_after_enable(service,role,application,monkeypatch):
    configure(service)
    model=FakeModel()
    monkeypatch.setattr(service,'model',lambda:model)
    service.change_role(role['id'],action(role,False))
    run(service)
    assert model.calls==0
    assert service.get_application(application['id'])['status']=='ROLE_INACTIVE'
    service.change_role(role['id'],action(role,True))
    assert run(service)['status']=='SUCCESS'
    assert model.calls==1


def test_role_action_version_and_running_task_guards(service,role,role_data):
    service.save_role(RoleInput(**role_data),role['id'])
    with pytest.raises(BusinessError) as error:
        service.change_role(role['id'],action(role),delete=True)
    assert error.value.code=='JD_VERSION_CONFLICT'
    with service.run_lock:
        with pytest.raises(BusinessError) as error:
            service.change_role(role['id'],action(role,False))
        assert error.value.code=='RUN_BUSY'
    assert service.roles()[0]['active']


def test_role_actions_api_login_csrf_confirmation_status_and_delete(environment,role_data):
    app=create_app(environment)
    role=app.state.service.save_role(RoleInput(**role_data))
    path='/api/roles/'+role['id']
    body=action(role).model_dump()
    with TestClient(app) as client:
        assert client.request('DELETE',path,json=body).status_code==401
        assert client.post(path+'/status',json={**body,'active':False}).status_code==401
        login=client.post('/api/auth/login',json={'username':'管理员','password':environment.admin_password})
        assert client.request('DELETE',path,json=body).status_code==403
        client.headers['X-CSRF-Token']=login.json()['csrf']
        for bad in ({'expected_version':role['current_version']},{**body,'confirmed':False}):
            assert client.request('DELETE',path,json=bad).status_code==422
            assert client.post(path+'/status',json={**bad,'active':False}).status_code==422
        assert not client.post(path+'/status',json={**body,'active':False}).json()['active']
        assert client.post(path+'/status',json={**body,'active':True}).json()['active']
        assert client.request('DELETE',path,json=body).status_code==200
        assert client.get('/api/roles').json()==[]
        assert client.put(path,json=role_data).status_code==404
