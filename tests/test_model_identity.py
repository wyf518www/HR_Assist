from copy import deepcopy
import json

from fastapi.testclient import TestClient
import pytest

from hr_assist.db import Database, dump, now, uid
from hr_assist.identities import validate_identity
from hr_assist.main import create_app
from hr_assist.reports import save_followup
from hr_assist.schemas import CandidateNameReview, FollowupInput, RoleInput
from hr_assist.storage import BusinessError
from conftest import make_pdf, run
from test_core import configure, FakeModel


def answer(name,role,role_id,filename):
    ref={'span_id':'F1','interpretation':filename}
    return {'name':{'status':'identified','value':name,'evidence':[ref],'note':'原始资料明确记载'},
            'role':{'status':'identified','value':role,'role_id':role_id,'evidence':[ref],'note':'本次应聘岗位对应库内岗位'}}


class IdentityModel(FakeModel):
    def __init__(self,output):
        self.output=output;self.calls=0;self.inputs=[]
    def complete(self,system,payload):
        self.inputs.append(deepcopy(payload))
        if 'available_roles' in payload:
            self.calls+=1
            return deepcopy(self.output),{'total_tokens':10}
        return super().complete(system,payload)


def attach_model(service,monkeypatch,role,filename,name='王先生'):
    configure(service)
    model=IdentityModel(answer(name,role['name'],role['id'],filename))
    monkeypatch.setattr(service,'model',lambda:model)
    return model


def test_new_ingest_uses_model_not_filename_rules(service,role,monkeypatch):
    filename='【软件工程师_北京_25-35K】王先生_14年.pdf'
    app=service.ingest(make_pdf(),filename,subject='【BOSS直聘】王先生应聘软件工程师')
    row=service.get_application(app['id'])
    assert row['name']=='待识别' and row['role_id'] is None
    model=attach_model(service,monkeypatch,role,filename)
    result=run(service)
    assert result['status']=='SUCCESS',result['error']
    row=service.get_application(app['id'])
    assert row['name']=='王先生' and row['role_id']==role['id'] and row['assessment']['score']==100
    assert model.calls==4
    sources=model.inputs[0]['sources']
    assert any(s['id']=='M1' and '应聘' in s['text'] for s in sources)
    assert service.get_application(app['id'])['identity_complete']
    assert run(service)['status']=='SUCCESS' and model.calls==4
    assert service.ingest(make_pdf(),filename)['duplicate']


def test_arbitrary_naming_and_name_from_resume_no_rule_fallback(service,role,monkeypatch):
    filename='附件最终稿(3).pdf'
    app=service.ingest(make_pdf('Name: Alex Example. Applying Software Developer. Phone 13812345678. Email fake@example.invalid.'),filename)
    configure(service)
    output=answer('Alex Example','Software Developer',role['id'],filename)
    for key in ('name','role'):
        output[key]['evidence']=[{'span_id':'R1','interpretation':'Name: Alex Example. Applying Software Developer.'}]
    model=IdentityModel(output);monkeypatch.setattr(service,'model',lambda:model)
    result=run(service)
    assert result['status']=='SUCCESS',result['error']
    first=dump(model.inputs[0]);score=dump(model.inputs[2])
    assert 'Alex Example' in first and 'Alex Example' not in score
    assert '13812345678' not in first and 'fake@example.invalid' not in first
    assert '姓名已移除' in score
    assert service.get_application(app['id'])['name']=='Alex Example'


def test_uncertain_identity_never_calls_scoring_or_guesses(service,role,monkeypatch):
    filename='【软件工程师_北京_20K】候选人.pdf'
    app=service.ingest(make_pdf(),filename)
    model=attach_model(service,monkeypatch,role,filename,'候选人')
    model.output['role'].update(status='uncertain',role_id=None,note='来源冲突')
    result=run(service)
    row=service.get_application(app['id'])
    assert row['status']=='WAITING_IDENTITY' and row['assessment'] is None
    assert row['name']=='待识别' and row['role_id'] is None and model.calls==2
    assert service.identities.proposal(app['id'])
    run(service)
    assert model.calls==2  # No repeated paid extraction for the same unresolved source.


def test_identity_and_score_share_budget_and_resume_completed_steps(service,role,monkeypatch):
    filename='【软件工程师_北京】王先生.pdf'
    app=service.ingest(make_pdf(),filename)
    model=attach_model(service,monkeypatch,role,filename)
    settings=service.settings();settings['llm']['max_calls_per_run']=1;service.db.put('settings',settings)
    first=run(service)
    assert first['status']=='BLOCKED' and model.calls==1
    assert not service.get_application(app['id'])['identity_complete']
    assert run(service)['status']=='BLOCKED' and model.calls==2
    assert service.get_application(app['id'])['identity_complete']
    assert run(service)['status']=='BLOCKED' and model.calls==3
    assert run(service)['status']=='SUCCESS' and model.calls==4


def test_review_preserves_records_until_confirmation_then_keeps_history_and_followup(service,role,monkeypatch):
    filename='【软件工程师_北京_25-35K】王先生_14年.pdf'
    app=service.ingest(make_pdf(),filename)
    wrong=service.save_role(RoleInput(name='其他岗位',jd_text='旧岗位'))
    aid=uid();old={'score':65,'grade':'待定'}
    service.db.execute('INSERT INTO assessments VALUES (?,?,?,?,?,?,?)',(aid,app['id'],wrong['current_version'],uid(),'COMPLETED',dump(old),now()))
    service.db.execute("UPDATE applications SET name='北京',role_id=?,assessment_id=?,status='ASSESSED',recommended_at='2026-10-24' WHERE id=?",(wrong['id'],aid,app['id']))
    before=service.db.one('SELECT * FROM followups')
    model=attach_model(service,monkeypatch,role,filename)
    batch=run(service,'identity')
    assert batch['status']=='SUCCESS'
    current=service.get_application(app['id'])
    assert current['name']=='北京' and current['role_id']==wrong['id'] and current['assessment']==old
    p=service.identities.proposal(app['id'])
    service.identities.apply([dict(application_id=app['id'],proposal_id=p['id'],name='王先生',role_id=role['id'])])
    current=service.get_application(app['id'])
    assert current['id']==app['id'] and current['name']=='王先生' and current['role_id']==role['id']
    assert current['assessment'] is None and len(current['history'])==1 and current['status']=='QUEUED'
    assert service.db.one('SELECT * FROM followups')==before and model.calls==2
    assert json.loads(service.db.one('SELECT result FROM assessments')['result'])==old
    assert service.identities.review_rows()[0]['proposal_applied']
    assert '王先生' not in dump(service.db.all('SELECT message FROM events'))


def test_human_values_are_preserved_during_automatic_processing(service,role,monkeypatch):
    app=service.ingest(make_pdf(),'【软件工程师_北京】王先生.pdf',role['id'])
    service.review_candidate_name(app['id'],CandidateNameReview(name='已核验姓名',expected_name='待识别',confirmed=True))
    configure(service);model=FakeModel();monkeypatch.setattr(service,'model',lambda:model)
    assert run(service)['status']=='SUCCESS' and model.calls==2
    assert service.get_application(app['id'])['name']=='已核验姓名'


@pytest.mark.parametrize('case',['unsafe_name','legacy_quote','wrong_role','unknown_span'])
def test_model_cannot_invent_identity_or_role(case):
    filename='软件工程师 王先生.pdf'
    result=answer('王先生','软件工程师','role1',filename)
    if case=='unsafe_name':result['name']['value']='<unsafe>'
    if case=='legacy_quote':result['name']['evidence'][0]={'span_id':'F1','quote':'旧契约已不用于新分析'}
    if case=='wrong_role':result['role']['role_id']='not-in-library'
    if case=='unknown_span':result['name']['evidence'][0]={'span_id':'R9','interpretation':filename}
    with pytest.raises(BusinessError):
        validate_identity(result,[{'id':'F1','text':filename,'source':'filename'}],[{'id':'role1','name':'软件工程师'}])


def test_identity_apply_requires_auth_csrf_and_confirmation(environment):
    with TestClient(create_app(environment)) as client:
        assert client.get('/api/identities').status_code==401
        assert client.post('/api/identities/apply',json={}).status_code==401
        session=client.post('/api/auth/login',json={'username':'管理员','password':environment.admin_password}).json()
        assert client.post('/api/identities/apply',json={}).status_code==403
        client.headers['X-CSRF-Token']=session['csrf']
        assert client.post('/api/identities/apply',json={'confirmed':False,'items':[]}).status_code==422


@pytest.mark.parametrize('change',['name','document','roles','proposal'])
def test_changed_sources_or_records_reject_stale_correction(service,role,monkeypatch,change):
    filename='软件工程师 王先生.pdf'
    app=service.ingest(make_pdf(),filename)
    attach_model(service,monkeypatch,role,filename)
    p=service.identities.analyze(service.get_application(app['id']),lambda:None)
    if change=='name':
        service.review_candidate_name(app['id'],CandidateNameReview(name='手工核验姓名',expected_name='待识别',confirmed=True))
    elif change=='document':
        service.verify_document(app['id'],'This is a locally verified synthetic resume text.','synthetic review')
    elif change=='roles':
        service.db.execute('UPDATE roles SET name=? WHERE id=?',('岗位已改名',role['id']))
    else:
        service.identities.analyze(service.get_application(app['id']),lambda:None,force=True)
    before=service.db.one('SELECT * FROM applications WHERE id=?',(app['id'],))
    with pytest.raises(BusinessError) as error:
        service.identities.apply([{'application_id':app['id'],'proposal_id':p['id'],'name':'王先生','role_id':role['id']}])
    assert error.value.code=='IDENTITY_STALE'
    assert service.db.one('SELECT * FROM applications WHERE id=?',(app['id'],))==before
    assert not service.run_lock.locked()


def test_batch_collision_rolls_back_all_without_merging_followups(service,role,monkeypatch):
    filename='软件工程师 王先生.pdf'
    first=service.ingest(make_pdf(),filename)
    occupied=service.ingest(make_pdf(),filename,role['id'])
    other=service.ingest(make_pdf('Different synthetic source text.'),filename)
    attach_model(service,monkeypatch,role,filename)
    items=[]
    for app in (other,first):
        p=service.identities.analyze(service.get_application(app['id']),lambda:None)
        items.append({'application_id':app['id'],'proposal_id':p['id'],'name':'王先生','role_id':role['id']})
    before=service.db.all('SELECT * FROM applications ORDER BY id')
    feedback=service.db.all('SELECT * FROM followups ORDER BY application_id')
    with pytest.raises(BusinessError) as error:
        service.identities.apply(items)
    assert error.value.code=='DUPLICATE_APPLICATION'
    assert service.db.all('SELECT * FROM applications ORDER BY id')==before
    assert service.db.all('SELECT * FROM followups ORDER BY application_id')==feedback


def test_ocr_identity_requires_human_review_and_no_unneeded_model_call(service,role,monkeypatch):
    filename='扫描简历.pdf'
    app=service.ingest(make_pdf(),filename)
    service.parse_document(service.get_application(app['id']))
    source='姓名：测试候选人 应聘岗位：软件工程师'
    service.db.execute('UPDATE documents SET spans=? WHERE file_id=?',(dump([{'id':'R1','page':1,'text':source,'source':'ocr'}]),app['file_id']))
    model=attach_model(service,monkeypatch,role,filename,'测试候选人')
    for key in ('name','role'):
        model.output[key]['evidence']=[{'span_id':'R1','interpretation':source}]
    run(service)
    assert model.calls==2 and service.get_application(app['id'])['status']=='WAITING_IDENTITY'
    p=service.identities.proposal(app['id'])
    assert p['result']['name']['status']=='uncertain' and p['result']['role']['role_id'] is None


def test_model_unavailable_leaves_identity_unknown_instead_of_falling_back(service,role,monkeypatch):
    filename='【软件工程师_北京_25-35K】王先生_14年.pdf'
    app=service.ingest(make_pdf(),filename)
    configure(service)
    model=FakeModel();model.fail=True;monkeypatch.setattr(service,'model',lambda:model)
    result=run(service)
    row=service.get_application(app['id'])
    assert result['status']=='BLOCKED' and row['name']=='待识别' and row['role_id'] is None and row['assessment'] is None


def test_bulk_identity_resumes_budget_without_repaying_and_force_is_explicit(service,role,monkeypatch):
    filename='软件工程师 王先生.pdf'
    first=service.ingest(make_pdf(),filename)
    second=service.ingest(make_pdf('Second synthetic resume text.'),filename)
    model=attach_model(service,monkeypatch,role,filename)
    settings=service.settings();settings['llm']['max_calls_per_run']=1;service.db.put('settings',settings)
    for expected in (1,2,3):
        assert run(service,'identity')['status']=='BLOCKED' and model.calls==expected
    assert run(service,'identity')['status']=='SUCCESS' and model.calls==4
    repeated=run(service,'identity')
    assert repeated['status']=='SUCCESS' and model.calls==4
    assert json.loads(repeated['stats'])['identity_reused']==2
    settings['llm']['max_calls_per_run']=2;service.db.put('settings',settings)
    record=service.start_run('identity',uid(),application_ids=[second['id']],force_identity=True)
    service.worker.join(timeout=25)
    assert not service.worker.is_alive()
    assert service.db.one('SELECT status FROM runs WHERE id=?',(record['id'],))['status']=='SUCCESS'
    assert model.calls==6
    for app in (first,second):
        assert service.get_application(app['id'])['name']=='待识别'
        assert service.get_application(app['id'])['assessment'] is None


def register_mail(service,label):
    identity=uid()
    service.db.execute('INSERT INTO mail_events VALUES (?,?,?,?,?,?,?)',
                       (identity,'test@example.invalid','test-mailbox',label,label,now(),'REGISTERED'))
    return identity


def test_same_pdf_and_filename_different_subjects_keep_separate_job_histories(service,role,role_data,monkeypatch):
    other_data=deepcopy(role_data);other_data['name']='系统架构师'
    other=service.save_role(RoleInput(**other_data))
    subjects={'王先生应聘软件工程师':role,'王先生应聘系统架构师':other}
    class SubjectModel(FakeModel):
        def __init__(self):self.calls=0;self.identity_inputs=[]
        def complete(self,system,payload):
            if 'available_roles' not in payload:
                return super().complete(system,payload)
            self.calls+=1;self.identity_inputs.append(deepcopy(payload))
            subject=next(s['text'] for s in payload['sources'] if s['id']=='M1')
            selected=subjects[subject]
            result=answer('王先生',selected['name'],selected['id'],subject)
            for field in ('name','role'):
                result[field]['evidence']=[{'span_id':'M1','interpretation':subject}]
            return result,{'total_tokens':10}
    configure(service);model=SubjectModel();monkeypatch.setattr(service,'model',lambda:model)
    first=service.ingest(make_pdf(),'个人简历.pdf',mail_event_id=register_mail(service,'first'),subject='王先生应聘软件工程师')
    assert run(service)['status']=='SUCCESS'
    save_followup(service.db,first['id'],FollowupInput(expected_revision=0,changes={'面试状态':'已面','面试结果':'通过'}))
    original=service.db.one('SELECT * FROM applications WHERE id=?',(first['id'],))
    followup=service.db.one('SELECT * FROM followups WHERE application_id=?',(first['id'],))
    original_score=service.db.one('SELECT * FROM assessments WHERE application_id=?',(first['id'],))
    second=service.ingest(make_pdf(),'个人简历.pdf',mail_event_id=register_mail(service,'second'),subject='王先生应聘系统架构师')
    assert not second['duplicate'] and second['id']!=first['id'] and second['file_id']==first['file_id']
    assert service.get_application(second['id'])['role_id'] is None
    assert run(service)['status']=='SUCCESS'
    assert service.get_application(second['id'])['role_id']==other['id']
    assert service.db.one('SELECT * FROM applications WHERE id=?',(first['id'],))==original
    assert service.db.one('SELECT * FROM followups WHERE application_id=?',(first['id'],))==followup
    assert service.db.one('SELECT * FROM assessments WHERE application_id=?',(first['id'],))==original_score
    # The identical PDF and scoring requirements may reuse the first scoring
    # analysis; both delivery identities and both reviews remain independent.
    assert model.calls==7 and len(model.identity_inputs)==4
    assert [s['text'] for s in model.identity_inputs[2]['sources'] if s['id'].startswith('M')]==['王先生应聘系统架构师']


def test_different_names_with_same_pdf_have_distinct_unresolved_applications(service):
    first=service.ingest(make_pdf(),'投递岗位甲.pdf')
    second=service.ingest(make_pdf(),'投递岗位乙.pdf')
    assert first['file_id']==second['file_id'] and first['id']!=second['id'] and not second['duplicate']
    assert len(service.db.all('SELECT id FROM applications WHERE role_id IS NULL'))==2
    assert service.db.get('identity_sources:'+first['id'])['filenames']==['投递岗位甲.pdf']
    assert service.db.get('identity_sources:'+second['id'])['filenames']==['投递岗位乙.pdf']


def test_receipt_dedup_takes_precedence_and_manual_same_name_reupload_reuses(service):
    event=register_mail(service,'original')
    first=service.ingest(make_pdf(),'个人简历.pdf',mail_event_id=event,subject='王先生应聘岗位甲')
    replay=service.ingest(make_pdf(),'重新解码附件名.pdf',mail_event_id=event,subject='王先生应聘岗位甲')
    repeated_mail=service.ingest(make_pdf(),'个人简历.pdf',mail_event_id=register_mail(service,'repeated'),subject='王先生应聘岗位甲')
    manual=service.ingest(make_pdf(),'个人简历.pdf')
    assert all(a['duplicate'] and a['id']==first['id'] for a in (replay,repeated_mail,manual))
    assert len(service.db.all('SELECT id FROM applications'))==1
    assert len(service.db.all('SELECT id FROM receipts'))==2


def test_legacy_record_without_subject_does_not_absorb_new_mail(service):
    legacy=service.ingest(make_pdf(),'个人简历.pdf')
    service.db.execute('DELETE FROM meta WHERE key=?',('identity_sources:'+legacy['id'],))
    new=service.ingest(make_pdf(),'个人简历.pdf',mail_event_id=register_mail(service,'new'),subject='王先生应聘岗位乙')
    assert not new['duplicate'] and new['id']!=legacy['id']
    assert service.db.get('identity_sources:'+legacy['id']) is None


def test_same_role_after_separate_delivery_is_not_merged_or_lost(service,role,monkeypatch):
    filename='软件工程师 王先生.pdf'
    first=service.ingest(make_pdf(),filename,role['id'])
    service.review_candidate_name(first['id'],CandidateNameReview(name='王先生',expected_name='待识别',confirmed=True))
    model=attach_model(service,monkeypatch,role,filename)
    assert run(service)['status']=='SUCCESS'
    second=service.ingest(make_pdf(),filename,subject='王先生再次应聘软件工程师')
    save_followup(service.db,second['id'],FollowupInput(expected_revision=0,changes={'面试状态':'待面试'}))
    followups=service.db.all('SELECT * FROM followups ORDER BY application_id')
    old=service.db.one('SELECT * FROM applications WHERE id=?',(first['id'],))
    assert run(service)['status']=='SUCCESS_WITH_EXCEPTIONS'
    assert service.db.one('SELECT * FROM applications WHERE id=?',(first['id'],))==old
    assert service.get_application(second['id'])['role_id'] is None
    assert service.get_application(second['id'])['assessment'] is None
    assert service.db.all('SELECT * FROM followups ORDER BY application_id')==followups
    assert service.db.one("SELECT 1 FROM events WHERE code='DUPLICATE_APPLICATION' AND application_id=?",(second['id'],))


def test_pending_identity_index_migration_preserves_rows_and_resolved_uniqueness(service,role):
    first=service.ingest(make_pdf(),'旧待分类.pdf')
    before=service.db.all('SELECT * FROM applications')
    with service.db.connect(True) as con:
        con.execute('DROP INDEX application_role')
        con.execute("CREATE UNIQUE INDEX application_role ON applications(file_id,ifnull(role_id,''))")
        con.execute("UPDATE meta SET value='3' WHERE key='schema_version'")
    migrated=Database(service.env.state_dir)
    assert migrated.get('schema_version')==4
    assert migrated.all('SELECT * FROM applications')==before
    second=service.ingest(make_pdf(),'新待分类.pdf')
    assert first['id']!=second['id']
    Database(service.env.state_dir)  # Idempotent with multiple unresolved rows.
    service.db.execute('UPDATE applications SET role_id=? WHERE id=?',(role['id'],first['id']))
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        service.db.execute('UPDATE applications SET role_id=? WHERE id=?',(role['id'],second['id']))
