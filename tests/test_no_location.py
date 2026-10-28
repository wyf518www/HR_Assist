from copy import deepcopy
import json

import pytest

from hr_assist.assessment import default_criteria, scoring_criteria, missing_jd_fields
from hr_assist.db import dump, now, uid
from hr_assist.schemas import RoleInput, CandidateNameReview
from conftest import run
from test_core import FakeModel, configure


def old_rules(fields):
    rules=default_criteria(fields)
    next(c for c in rules if c['id']=='experience')['points']=8
    rules.insert(2,dict(id='location',dimension='hard',description='地点：需要base北京',points=4,hard=True,source='jd'))
    return rules


@pytest.mark.parametrize('text',[
    'database administration and base64 encoding',
    '熟悉知识库（knowledge base）的建设和检索优化',
    '具备跨地点项目协调能力',
    '了解常驻服务和守护进程开发',
    '需要在多任务环境中工作，熟悉Python',
    '在北京工作经验5年',
    '维护北京区域客户，有商务谈判能力',
])
def test_semantic_requirements_are_preserved_through_save_and_migration(service,role_data,text):
    role_data['fields']['skills']=text
    role=service.save_role(RoleInput(**role_data))
    assert next(c['description'] for c in role['criteria'] if c['id']=='skills')==text
    assert not role['missing']
    assert service.migrate_no_availability()==0
    assert next(c['description'] for c in service.roles()[0]['criteria'] if c['id']=='skills')==text


def test_location_optional_new_defaults_and_mixed_requirements(service,role_data):
    role_data['fields'].update(location='',availability='',experience='运营商工作经验，需要base北京 5年以上销售经验；行业经验优先')
    saved=service.save_role(RoleInput(**role_data))
    assert not saved['missing'] and saved['approved']
    rules=saved['criteria']
    assert {c['id']:c['points'] for c in rules if c['dimension']=='hard'}=={'education':5,'experience':12,'certificates':3}
    requirement=next(c['description'] for c in rules if c['id']=='experience')
    assert '需要base北京' in requirement  # Both model stages ignore its meaning; code never clips the text.
    assert '5年以上销售经验' in requirement and '行业经验优先' in requirement


def test_legacy_migration_preserves_jd_assessment_and_manual_cells(service,role_data,application):
    role=service.roles()[0]
    old=role['current_version']
    criteria=old_rules(role_data['fields'])
    service.db.execute('UPDATE jd_versions SET criteria=?,missing=? WHERE id=?',(dump(criteria),dump(['地点']),old))
    result={'score':61,'items':criteria}
    aid=uid()
    service.db.execute('INSERT INTO assessments VALUES (?,?,?,?,?,?,?)',(aid,application['id'],old,uid(),'ASSESSED',dump(result),now()))
    service.db.execute('UPDATE applications SET assessment_id=? WHERE id=?',(aid,application['id']))
    manual=service.db.one('SELECT * FROM followups')
    assert service.migrate_no_availability()==1
    assert service.migrate_no_availability()==0
    current=service.roles()[0]
    assert current['current_version']!=old and current['approved'] and not current['missing']
    assert json.loads(service.db.one('SELECT criteria FROM jd_versions WHERE id=?',(old,))['criteria'])==criteria
    assert service.get_application(application['id'])['assessment']==result
    assert service.db.one('SELECT * FROM followups')==manual


@pytest.mark.parametrize('identity',['location','base','base_location','work_location','city','LOCATION'])
def test_legacy_explicit_location_ids_and_totals(identity,role_data):
    rules=old_rules(role_data['fields'])
    rules[2]['id']=identity
    before=deepcopy(rules)
    normalized=scoring_criteria(rules,role_data['fields'])
    assert rules==before and all(c['id']!=identity for c in normalized)
    RoleInput(**{**role_data,'criteria':normalized})
    assert scoring_criteria(normalized,role_data['fields'])==normalized


def test_custom_requirement_id_and_description_are_not_classified_by_substrings(role_data):
    rules=old_rules(role_data['fields'])
    rules[2].update(id='custom-location',description='常驻服务开发；可以在北京工作')
    assert scoring_criteria(rules,role_data['fields'])==rules


def test_entire_location_dimension_replacement_keeps_custom_ids_unique(role_data):
    rules=[c for c in default_criteria(role_data['fields']) if c['dimension']!='hard']
    next(c for c in rules if c['id']=='stability')['id']='replacement_hard'
    rules.append(dict(id='location',dimension='hard',description='工作地点：北京',points=20,hard=True,source='jd'))
    before=deepcopy(rules)
    normalized=scoring_criteria(rules,role_data['fields'])
    assert rules==before
    assert len({c['id'] for c in normalized})==len(normalized)
    assert next(c for c in normalized if c['dimension']=='hard')['id']=='replacement_hard_'
    assert sum(c['points'] for c in normalized)==100
    RoleInput(**{**role_data,'criteria':normalized})
    assert scoring_criteria(normalized,role_data['fields'])==normalized


@pytest.mark.parametrize('identity',['availability','arrival','arrival_time','ARRIVAL'])
def test_explicit_arrival_ids_retire_without_changing_other_requirements(identity,role_data):
    rules=old_rules(role_data['fields'])
    rules[2].update(id=identity,description='到岗时间：一个月')
    normalized=scoring_criteria(rules,role_data['fields'])
    assert all(c['id']!=identity for c in normalized)
    assert next(c for c in normalized if c['id']=='experience')['points']==12
    RoleInput(**{**role_data,'criteria':normalized})


def test_location_and_arrival_fields_are_optional_without_interpreting_other_fields(role_data):
    fields={**role_data['fields'],'location':'','availability':''}
    assert missing_jd_fields(fields)==[]
    fields['skills']='  '
    assert missing_jd_fields(fields)==['核心技能']


def test_both_model_stages_ignore_actual_base_conditions_and_keep_region_experience(service,role_data,monkeypatch,pdf):
    configure(service)
    role_data['fields']['experience']='需要base北京 5年以上政府销售经验'
    role_data['fields']['skills']='维护北京区域客户，有商务谈判能力'
    role=service.save_role(RoleInput(**role_data))
    app=service.ingest(pdf,'【软件工程师_Test】.pdf',role['id'])
    service.review_candidate_name(app['id'],CandidateNameReview(name='Test',expected_name='待识别',confirmed=True))
    stages=[]
    class Model(FakeModel):
        def complete(self,system,payload):
            stages.append(payload['task_stage'])
            assert '工作地点、base地' in system
            assert '即使混在经验或技能描述里也必须忽略' in system
            assert '不能因此扣分、待定、否决、提示风险或追问' in system
            assert '知识库、跨地点协作、常驻服务等要求仍应完整考察' in system
            assert all(c['id']!='location' for c in payload['criteria'])
            assert '需要base北京' in dump(payload['criteria'])
            assert '维护北京区域客户' in dump(payload['criteria'])
            return super().complete(system,payload)
    model=Model();monkeypatch.setattr(service,'model',lambda:model)
    run(service)
    assert service.get_application(app['id'])['assessment']['score']==100
    assert model.calls==2
    assert stages==['analysis','semantic_review']
