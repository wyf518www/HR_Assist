from copy import deepcopy
import json

import pytest

from hr_assist.assessment import assess, default_criteria, scoring_criteria, missing_jd_fields, without_location
from hr_assist.db import dump, now, uid
from hr_assist.schemas import RoleInput, CandidateNameReview
from conftest import run
from test_core import FakeModel, configure


def old_rules(fields):
    rules=default_criteria(fields)
    next(c for c in rules if c['id']=='experience')['points']=8
    rules.insert(2,dict(id='location',dimension='hard',description='地点：需要base北京',points=4,hard=True,source='jd'))
    return rules


@pytest.mark.parametrize('text',['database administration and base64 encoding','在北京工作经验5年','维护北京区域客户，有商务谈判能力'])
def test_location_filter_keeps_unrelated_skills_and_regional_experience(text):
    assert without_location(text)==text


@pytest.mark.parametrize('text',['需要base北京','工作地点：北京','常驻北京','需要在北京工作'])
def test_explicit_location_clauses_are_excluded(text):
    assert without_location(text)==''


def test_location_optional_new_defaults_and_mixed_requirements(service,role_data):
    role_data['fields'].update(location='',availability='',experience='运营商工作经验，需要base北京 5年以上销售经验；行业经验优先')
    saved=service.save_role(RoleInput(**role_data))
    assert not saved['missing'] and saved['approved']
    rules=saved['criteria']
    assert {c['id']:c['points'] for c in rules if c['dimension']=='hard'}=={'education':5,'experience':12,'certificates':3}
    requirement=next(c['description'] for c in rules if c['id']=='experience')
    assert 'base' not in requirement and '北京' not in requirement
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


@pytest.mark.parametrize('identity',['location','base','work_location','city','custom-location'])
def test_legacy_custom_location_and_totals(identity,role_data):
    rules=old_rules(role_data['fields'])
    rules[2]['id']=identity
    before=deepcopy(rules)
    normalized=scoring_criteria(rules,role_data['fields'])
    assert rules==before and all(c['id']!=identity for c in normalized)
    RoleInput(**{**role_data,'criteria':normalized})
    assert scoring_criteria(normalized,role_data['fields'])==normalized


def test_no_location_is_sent_and_source_region_experience_remains(service,role_data,monkeypatch,pdf):
    configure(service)
    role_data['fields']['experience']='需要base北京 5年以上政府销售经验'
    role_data['fields']['skills']='维护北京区域客户，有商务谈判能力'
    role=service.save_role(RoleInput(**role_data))
    app=service.ingest(pdf,'【软件工程师_Test】.pdf',role['id'])
    service.review_candidate_name(app['id'],CandidateNameReview(name='Test',expected_name='待识别',confirmed=True))
    class Model(FakeModel):
        def complete(self,system,payload):
            assert '工作地点、base地' in system
            assert all(c['id']!='location' for c in payload['criteria'])
            assert '需要base北京' not in dump(payload['criteria'])
            assert '维护北京区域客户' in dump(payload['criteria'])
            return super().complete(system,payload)
    model=Model();monkeypatch.setattr(service,'model',lambda:model)
    run(service)
    assert service.get_application(app['id'])['assessment']['score']==100
    assert model.calls==1
