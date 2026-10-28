from copy import deepcopy
import json

from hr_assist.assessment import default_criteria, remove_availability, validate_and_score
from hr_assist.db import dump, now, uid
from hr_assist.schemas import RoleInput
from test_assessment import sample
from test_jd_imports import output, SPANS
from hr_assist.jd_imports import validate_extraction


def legacy_criteria(fields):
    rules=default_criteria(fields)
    next(c for c in rules if c['id']=='experience')['points']-=3
    rules.insert(4,dict(id='availability',dimension='hard',description='到岗时间：两周内',points=3,hard=True,source='jd'))
    return rules


def test_optional_arrival_does_not_block_approval_or_score(service,role_data):
    role_data['fields']['availability']=''
    saved=service.save_role(RoleInput(**role_data))
    assert saved['approved'] and not saved['missing']
    assert 'availability' not in {c['id'] for c in saved['criteria']}
    assert {c['id']:c['points'] for c in saved['criteria'] if c['dimension']=='hard'}==dict(education=5,experience=12,certificates=3)
    raw,spans,criteria,policy=sample()
    spans[0]['text']=spans[0]['text'].replace(' 一周','')
    for item in raw['judgments']: item['evidence'][0]['quote']=spans[0]['text']
    for fact in raw['facts']: fact['evidence']['quote']=spans[0]['text']
    result=validate_and_score(raw,spans,criteria,policy,False)
    assert result['score']==100 and not result['provisional']


def test_ai_arrival_missing_is_only_optional():
    raw=output();raw['fields']['availability']=[]
    draft=validate_extraction(raw,SPANS)
    assert draft['fields']['availability']=='' and not draft['missing']


def test_legacy_save_removes_arrival_and_preserves_totals(service,role_data):
    role_data['criteria']=legacy_criteria(role_data['fields'])
    saved=service.save_role(RoleInput(**role_data))
    assert sum(c['points'] for c in saved['criteria'])==100
    assert not any(c['id']=='availability' for c in saved['criteria'])
    assert next(c['points'] for c in saved['criteria'] if c['id']=='experience')==12


def test_migration_adds_version_preserving_history_approval_and_is_idempotent(service,role_data):
    approved=service.save_role(RoleInput(**role_data))
    old_version=approved['current_version']
    old_rules=legacy_criteria(role_data['fields'])
    service.db.execute('UPDATE jd_versions SET criteria=? WHERE id=?',(dump(old_rules),old_version))
    draft_data=deepcopy(role_data)
    draft_data.update(name='待核对岗位',approved=False)
    draft_data['fields']['availability']=''
    draft=service.save_role(RoleInput(**draft_data))
    service.db.execute('UPDATE jd_versions SET criteria=?,missing=? WHERE id=?',(dump(old_rules),dump(['到岗时间']),draft['current_version']))
    application_id=uid()
    file=service.storage.put(b'synthetic','test.pdf','resumes')
    service.db.execute('INSERT INTO applications VALUES (?,?,?,?,?,?,?,?,?)',(application_id,file['id'],approved['id'],'Test','test.pdf','ASSESSED',None,now(),now()))
    result={'score':55,'grade':'不推荐','items':old_rules}
    service.db.execute('INSERT INTO assessments VALUES (?,?,?,?,?,?,?)',(uid(),application_id,old_version,uid(),'ASSESSED',dump(result),now()))
    assert service.migrate_no_availability()==2
    updated=next(r for r in service.roles() if r['id']==approved['id'])
    assert updated['current_version']!=old_version and updated['approved']
    assert json.loads(service.db.one('SELECT criteria FROM jd_versions WHERE id=?',(old_version,))['criteria'])==old_rules
    assert json.loads(service.db.one('SELECT result FROM assessments')['result'])==result
    updated_draft=next(r for r in service.roles() if r['id']==draft['id'])
    assert not updated_draft['approved'] and updated_draft['missing']==[]
    assert service.migrate_no_availability()==0
    assert len(service.db.all('SELECT * FROM jd_versions'))==4


def test_custom_criteria_keep_dimension_totals_when_arrival_removed(role_data):
    rules=legacy_criteria(role_data['fields'])
    next(c for c in rules if c['id']=='experience')['id']='custom-years'
    normalized=remove_availability(rules,role_data['fields'])
    assert next(c for c in normalized if c['id']=='custom-years')['points']==9
    assert sum(c['points'] for c in normalized if c['dimension']=='hard')==20
    assert not any(c['id']=='availability' for c in normalized)
    only_arrival=[c for c in rules if c['dimension']!='hard']
    only_arrival.append(dict(id='availability',dimension='hard',points=20,hard=True,source='jd',description='到岗'))
    replacement=remove_availability(only_arrival,role_data['fields'])
    RoleInput(**{**role_data,'criteria':replacement})
    assert sum(c['points'] for c in replacement)==100


def test_three_score_levels_and_unknown_are_explicit():
    for verdict,awarded in [('met',30),('partial',15),('not_met',0),('unknown',0)]:
        raw,spans,criteria,policy=sample()
        next(j for j in raw['judgments'] if j['criterion_id']=='skills')['verdict']=verdict
        result=validate_and_score(raw,spans,criteria,policy,True)
        assert next(i['awarded'] for i in result['items'] if i['id']=='skills')==awarded
        assert result['provisional']==(verdict=='unknown')
    raw,spans,criteria,policy=sample()
    next(j for j in raw['judgments'] if j['criterion_id']=='education')['verdict']='partial'
    result=validate_and_score(raw,spans,criteria,policy,True)
    assert next(i['awarded'] for i in result['items'] if i['id']=='education')==2
    assert result['grade']=='待定'
