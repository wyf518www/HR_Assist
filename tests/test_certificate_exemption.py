from copy import deepcopy

import pytest

from hr_assist.assessment import assessment_key, validate_and_score
from hr_assist.db import dump
from test_assessment import sample


@pytest.mark.parametrize('requirement', ['无', '无需证书', '无证书要求', '不要求证书', '证书不限', '无资格证书要求'])
@pytest.mark.parametrize('verdict', ['met', 'partial', 'not_met', 'unknown'])
def test_confirmed_no_certificate_requirement_gets_full_points(requirement, verdict):
    raw, spans, criteria, policy = sample()
    next(c for c in criteria if c['id'] == 'certificates')['description'] = '证书：' + requirement
    judgment = next(j for j in raw['judgments'] if j['criterion_id'] == 'certificates')
    judgment.update(requirement_mode='unrestricted', verdict=verdict, evidence=[], reason='unsupported certificate claim', question='unsupported question')
    before = deepcopy(raw)
    result = validate_and_score(raw, spans, criteria, policy, True)
    item = next(i for i in result['items'] if i['id'] == 'certificates')
    assert item['awarded'] == 3 and item['verdict'] == 'met'
    assert item['evidence'] == [] and item['question'] == '' and 'JD' in item['reason']
    assert result['score'] == 100 and not result['provisional'] and result['validation_warnings'] == []
    assert 'unsupported' not in dump(result)
    assert raw == before


@pytest.mark.parametrize('requirement', ['未提供', '未提及', '未知', '无说明', 'N/A', '无证书也可，入职后必须考取证书A', '无犯罪记录证明', '无相关证书者不考虑'])
def test_missing_or_conditional_requirements_are_not_exemptions(requirement):
    raw, spans, criteria, policy = sample()
    next(c for c in criteria if c['id'] == 'certificates')['description'] = '证书：' + requirement
    next(j for j in raw['judgments'] if j['criterion_id'] == 'certificates').update(verdict='unknown', evidence=[])
    result = validate_and_score(raw, spans, criteria, policy, True)
    item = next(i for i in result['items'] if i['id'] == 'certificates')
    assert item['awarded'] == 0 and item['verdict'] == 'unknown' and result['provisional']


def test_resume_without_certificate_does_not_waive_positive_jd_requirement():
    raw, spans, criteria, policy = sample()
    spans.append({'id': 'R2', 'page': 2, 'source': 'text', 'text': '证书：无'})
    next(j for j in raw['judgments'] if j['criterion_id'] == 'certificates').update(
        verdict='not_met', evidence=[{'span_id': 'R2', 'interpretation': '候选人明确未取得所需证书'}])
    result = validate_and_score(raw, spans, criteria, policy, True)
    item = next(i for i in result['items'] if i['id'] == 'certificates')
    assert item['awarded'] == 0 and item['verdict'] == 'not_met' and result['hard_fail']


def test_certificate_fix_does_not_invent_education_evidence():
    raw, spans, criteria, policy = sample()
    next(c for c in criteria if c['id'] == 'certificates')['description'] = '证书: 无； '
    for judgment in raw['judgments']:
        if judgment['criterion_id'] in {'certificates', 'education'}:
            judgment.update(verdict='unknown', evidence=[])
        if judgment['criterion_id'] == 'certificates':
            judgment['requirement_mode']='unrestricted'
        if judgment['criterion_id'] == 'stability':
            judgment['verdict'] = 'partial'
    result = validate_and_score(raw, spans, criteria, policy, True)
    assert result['score'] == 90 and result['provisional'] and result['grade'] == '待定'
    assert next(i for i in result['items'] if i['id'] == 'education')['awarded'] == 0


@pytest.mark.parametrize('criterion_id', ['education', 'experience'])
def test_bare_none_does_not_waive_other_hard_conditions(criterion_id):
    raw, spans, criteria, policy = sample()
    next(c for c in criteria if c['id'] == criterion_id)['description'] = '无'
    next(j for j in raw['judgments'] if j['criterion_id'] == criterion_id).update(verdict='unknown', evidence=[])
    result = validate_and_score(raw, spans, criteria, policy, True)
    assert next(i for i in result['items'] if i['id'] == criterion_id)['awarded'] == 0


def test_certificate_fix_changes_cache_version(monkeypatch):
    args = ({'file_id': 'f', 'role_id': 'r'}, {'id': 'j'}, {}, {'model': 'test'}, [])
    current = assessment_key(*args)
    monkeypatch.setattr('hr_assist.assessment.ASSESSMENT_PROMPT_VERSION', 'v4-contract')
    assert current != assessment_key(*args)


def test_unrestricted_mode_accepts_semantics_without_a_fixed_phrase_dictionary():
    raw,spans,criteria,policy=sample()
    next(c for c in criteria if c['id']=='certificates')['description']='本岗位录用不以持有资格认证为前置条件'
    next(j for j in raw['judgments'] if j['criterion_id']=='certificates').update(
        requirement_mode='unrestricted',verdict='unknown',evidence=[])
    item=next(i for i in validate_and_score(raw,spans,criteria,policy,True)['items'] if i['id']=='certificates')
    assert item['awarded']==3 and item['verdict']=='met'


@pytest.mark.parametrize('criterion_id', ['skills','achievements','stability'])
def test_model_cannot_waive_non_hard_or_global_criteria(criterion_id):
    from hr_assist.storage import BusinessError
    raw,spans,criteria,policy=sample()
    next(j for j in raw['judgments'] if j['criterion_id']==criterion_id)['requirement_mode']='unrestricted'
    with pytest.raises(BusinessError) as caught:
        validate_and_score(raw,spans,criteria,policy,True)
    assert caught.value.code=='MODEL_CRITERIA'
