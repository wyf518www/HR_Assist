from copy import deepcopy
import json

from fastapi.testclient import TestClient
import pytest

from hr_assist.db import dump, now, uid
from hr_assist.main import create_app
from hr_assist.reports import MANUAL_COLUMNS, application_counts, applications, empty_cells
from hr_assist.schemas import RoleInput
from hr_assist.storage import BusinessError


def seed_candidates(service, role, results):
    """Synthetic database-only candidates; never touch models or real documents."""
    ids=[]
    with service.db.connect(True) as con:
        for index,result in enumerate(results):
            file_id,app_id,score_id=uid(),uid(),uid() if result is not None else None
            ids.append(app_id)
            con.execute('INSERT INTO files VALUES (?,?,?,?,?,?,?)',
                        (file_id,f'{index:064x}','resumes','synthetic.pdf',f'temporary/{file_id}.pdf',1,now()))
            con.execute('INSERT INTO applications VALUES (?,?,?,?,?,?,?,?,?)',
                        (app_id,file_id,role['id'],'Synthetic '+str(index),'synthetic.pdf','ASSESSED' if score_id else 'QUEUED',score_id,'2026-10-28' if score_id else None,now()))
            cells=empty_cells()
            for i,name in enumerate(MANUAL_COLUMNS):
                cells[i]={'value':f'Human {name} {index}','type':'s','format':'General'}
            con.execute('INSERT INTO followups VALUES (?,?,?,?)',(app_id,dump(cells),index+3,now()))
            if score_id:
                con.execute('INSERT INTO assessments VALUES (?,?,?,?,?,?,?)',
                            (score_id,app_id,role['current_version'],uid(),'COMPLETED',dump(result),now()))
    return ids


def scored(grade='推荐',provisional=True):
    return {'score':78,'grade':grade,'provisional':provisional,
            'items':[{'quote':'Synthetic evidence only. '*1500,'reason':'Synthetic explanation'} for _ in range(2)],
            'facts':{'company':'Synthetic company'},'advice':'Synthetic advice'}


def test_summary_projects_evidence_in_sql_and_preserves_manual_records(service,role,monkeypatch):
    seed_candidates(service,role,[scored(),scored('强烈推荐',False),None])
    before=service.db.all('SELECT * FROM followups ORDER BY application_id')
    full=applications(service.db)
    captured=[]
    original=service.db.all
    def capture(sql,args=()):
        rows=original(sql,args)
        captured.append((sql,args,deepcopy(rows)))
        return rows
    monkeypatch.setattr(service.db,'all',capture)
    compact=applications(service.db,summary=True)
    assert len(captured)==1
    assert 'json_extract' in captured[0][0]
    assert 'Synthetic evidence' not in dump(captured[0][2])
    assert len(dump(compact).encode()) < len(dump(full).encode())/20
    assert [a['id'] for a in compact]==[a['id'] for a in full]
    for brief,complete in zip(compact,full):
        assert {k:v for k,v in brief.items() if k!='assessment'}=={k:v for k,v in complete.items() if k!='assessment'}
        if complete['assessment']:
            assert brief['assessment']=={k:complete['assessment'][k] for k in ('score','grade','provisional')}
            assert type(brief['assessment']['provisional']) is bool
        else:
            assert brief['assessment'] is None
    assert original('SELECT * FROM followups ORDER BY application_id')==before


def test_single_application_queries_only_requested_candidate(service,role,monkeypatch):
    ids=seed_candidates(service,role,[scored(),scored(),None])
    captured=[]
    original=service.db.all
    def capture(sql,args=()):
        rows=original(sql,args)
        if 'SELECT a.*' in sql:
            captured.append((sql,args,len(rows)))
        return rows
    monkeypatch.setattr(service.db,'all',capture)
    row=service.get_application(ids[1])
    assert row['id']==ids[1] and row['assessment']['items']
    assert len(captured)==1 and 'WHERE a.id=?' in captured[0][0]
    assert captured[0][1]==(ids[1],) and captured[0][2]==1
    with pytest.raises(BusinessError) as exc:
        service.get_application(uid())
    assert exc.value.code=='APPLICATION_NOT_FOUND'


def test_counts_match_full_records_without_fetching_evidence(service,role,monkeypatch):
    assert application_counts(service.db)=={'applications':0,'pending':0,'grades':{g:0 for g in ('强烈推荐','推荐','待定','不推荐')}}
    seed_candidates(service,role,[scored('强烈推荐'),scored('推荐'),scored('待定'),scored('不推荐'),None,{}])
    full=applications(service.db)
    captured=[]
    original=service.db.all
    def capture(sql,args=()):
        rows=original(sql,args);captured.append(rows);return rows
    monkeypatch.setattr(service.db,'all',capture)
    counts=application_counts(service.db)
    assert counts['applications']==len(full)
    assert counts['pending']==sum(not a['assessment'] for a in full)
    assert counts['grades']=={g:sum(bool(a['assessment']) and a['assessment']['grade']==g for a in full) for g in counts['grades']}
    assert len(captured)==1 and len(captured[0])==1
    assert 'Synthetic evidence' not in dump(captured)


def test_summary_and_dashboard_api_keep_compatibility(environment,role_data,monkeypatch):
    app=create_app(environment)
    with TestClient(app) as client:
        service=app.state.service
        role=service.save_role(RoleInput(**role_data))
        seed_candidates(service,role,[scored(),None])
        assert client.get('/api/applications?summary=true').status_code==401
        assert client.get('/api/roles?summary=true').status_code==401
        assert client.get('/api/roles/'+role['id']).status_code==401
        client.post('/api/auth/login',json={'username':environment.admin_username,'password':environment.admin_password})
        full=client.get('/api/applications').json()
        compact=client.get('/api/applications?summary=true').json()
        assert any(a['assessment'] and 'items' in a['assessment'] for a in full)
        assert all(a['assessment'] is None or set(a['assessment'])=={'score','grade','provisional'} for a in compact)
        for a,b in zip(full,compact):
            assert a['manual']==b['manual'] and a['revision']==b['revision']
        def forbidden(*args,**kwargs):raise AssertionError('Overview must not load full roles, candidates or call a model')
        monkeypatch.setattr('hr_assist.main.applications',forbidden)
        monkeypatch.setattr(service,'roles',forbidden)
        monkeypatch.setattr(service,'model',forbidden)
        result=client.get('/api/dashboard')
        assert result.status_code==200
        assert result.json()['counts']=={'applications':2,'pending':1,'roles':1,'grades':{'强烈推荐':0,'推荐':1,'待定':0,'不推荐':0}}


def test_role_summary_and_single_role_preserve_full_default(environment,role_data):
    app=create_app(environment)
    with TestClient(app) as client:
        service=app.state.service
        roles=[service.save_role(RoleInput(**{**role_data,'name':name})) for name in ('B role','A role','C role')]
        service.db.execute('UPDATE roles SET active=0 WHERE id=?',(roles[0]['id'],))
        client.post('/api/auth/login',json={'username':environment.admin_username,'password':environment.admin_password})
        full=client.get('/api/roles').json()
        compact=client.get('/api/roles?summary=true').json()
        assert [r['name'] for r in compact]==['A role','C role','B role']
        fields={'id','name','active','is_sales','current_version'}
        assert compact==[{k:v for k,v in r.items() if k in fields} for r in full]
        assert all(set(r)==fields for r in compact)
        row=client.get('/api/roles/'+roles[0]['id'])
        assert row.status_code==200 and row.json()==next(r for r in full if r['id']==roles[0]['id'])
        assert 'criteria' in row.json() and 'jd_text' in row.json()
        assert client.get('/api/roles/'+uid()).status_code==404


def test_preflight_approved_role_query_preserves_inactive_semantics(service,role,monkeypatch):
    service.db.execute('UPDATE roles SET active=0 WHERE id=?',(role['id'],))
    monkeypatch.setattr(service,'roles',lambda *args,**kwargs:pytest.fail('Full role data must not be loaded'))
    assert '至少一个已确认的完整 JD' not in service.preflight(mail=True)['missing']
    service.db.execute('UPDATE jd_versions SET approved=0 WHERE id=?',(role['current_version'],))
    assert '至少一个已确认的完整 JD' in service.preflight(mail=True)['missing']
