"""The confirmed admission default is a policy decision, never an invented fact."""
from copy import deepcopy

import pytest

from hr_assist.assessment import assess, validate_and_score
from hr_assist.db import dump
from test_assessment import sample


def unstated_sample():
    raw, spans, criteria, policy = sample()
    spans[0]['text'] = spans[0]['text'].replace('统招 ', '')
    raw['facts'] = [fact for fact in raw['facts'] if fact['field'] != 'admission']
    raw['education'].update(admission='not_stated', interpretation='同一本科已取得，资料未说明招生方式。')
    return raw, spans, criteria, policy


def test_unstated_admission_passes_policy_without_creating_a_regular_admission_fact():
    raw, spans, criteria, policy = unstated_sample()
    before = deepcopy((raw, spans, criteria, policy))
    result = validate_and_score(raw, spans, criteria, policy, False)
    assert result['education_gate'] == 'pass'
    assert result['education']['admission'] == 'not_stated'
    assert result['education']['admission_defaulted'] is True
    assert result['policy_version'] == 'admission-default-v1'
    assert 'admission' not in result['facts']
    assert result['score'] == 100 and result['grade'] == '强烈推荐'
    assert result['base_grade'] == '强烈推荐' and result['grade_limited'] is False
    assert result['provisional'] is False and result['hard_fail'] is False
    assert result['decision_reasons'] == []
    assert (raw, spans, criteria, policy) == before


@pytest.mark.parametrize('admission,gate', [
    ('national_regular', 'pass'), ('not_stated', 'pass'),
    ('non_regular', 'fail'), ('unknown', 'unknown'),
])
def test_admission_statuses_preserve_explicit_exceptions_and_ambiguity(admission, gate):
    raw, spans, criteria, policy = unstated_sample()
    raw['education']['admission'] = admission
    result = validate_and_score(raw, spans, criteria, policy, False)
    assert result['education_gate'] == gate
    assert result['education']['admission'] == admission
    assert result['education']['admission_defaulted'] is (admission == 'not_stated')
    assert 'admission' not in result['facts']
    assert result['grade'] == ('强烈推荐' if gate == 'pass' else '待定')
    assert result['provisional'] is (gate == 'unknown')
    assert result['hard_fail'] is (gate == 'fail')


@pytest.mark.parametrize('change,gate', [
    ({'institution_match': 'unknown', 'catalog_id': None}, 'unknown'),
    ({'institution_match': 'not_in_catalog', 'catalog_id': None}, 'fail'),
    ({'completion': 'in_progress'}, 'unknown'),
    ({'completion': 'unknown'}, 'unknown'),
    ({'degree': 'unknown'}, 'unknown'),
    ({'degree': 'doctor'}, 'unknown'),
    ({'evidence': []}, 'unknown'),
])
def test_admission_default_does_not_waive_school_degree_completion_or_evidence(change, gate):
    raw, spans, criteria, policy = unstated_sample()
    raw['education'].update(change)
    result = validate_and_score(raw, spans, criteria, policy, False)
    assert result['education_gate'] == gate
    assert result['grade'] == '待定' and result['grade_limited'] is True
    assert result['decision_reasons']


@pytest.mark.parametrize('ocr_location', ['typed_education', 'education_fact', 'removed_admission_fact'])
def test_unstated_default_never_bypasses_ocr_review(ocr_location):
    raw, spans, criteria, policy = unstated_sample()
    spans.append({'id': 'R2', 'page': 2, 'source': 'ocr', 'text': '测试大学 本科 已毕业'})
    reference = {'span_id': 'R2', 'interpretation': 'OCR 显示同一教育经历，需要核对原件。'}
    if ocr_location == 'typed_education':
        raw['education']['evidence'] = [reference]
    elif ocr_location == 'removed_admission_fact':
        raw['facts'].append({'field': 'admission', 'value': '统招', 'evidence': reference})
    else:
        next(fact for fact in raw['facts'] if fact['field'] == 'education_level')['evidence'] = reference
    result = validate_and_score(raw, spans, criteria, policy, False)
    assert result['education_gate'] == 'unknown'
    assert result['provisional'] and result['grade'] == '待定'
    assert any('OCR' in reason['detail'] + reason['title'] for reason in result['decision_reasons'])


@pytest.mark.parametrize('master_bypass,gate', [(True, 'pass'), (False, 'unknown')])
def test_master_policy_is_not_changed_by_bachelor_default(master_bypass, gate):
    raw, spans, criteria, policy = unstated_sample()
    raw['education'].update(degree='master', institution_match='unknown', catalog_id=None, admission='unknown')
    policy['master_bypass'] = master_bypass
    result = validate_and_score(raw, spans, criteria, policy, False)
    assert result['education_gate'] == gate


def test_sales_exemption_still_applies_to_global_policy_only():
    raw, spans, criteria, policy = unstated_sample()
    raw['education'].update(admission='non_regular', institution_match='not_in_catalog', catalog_id=None)
    next(judgment for judgment in raw['judgments'] if judgment['criterion_id'] == 'education')['verdict'] = 'not_met'
    result = validate_and_score(raw, spans, criteria, policy, True)
    assert result['education_gate'] == 'pass'
    assert result['hard_fail'] and result['grade'] == '待定'
    assert any(reason.get('criterion_id') == 'education' for reason in result['decision_reasons'])


@pytest.mark.parametrize('model_fact', ['全日制', '统招'])
def test_defaulted_admission_never_publishes_model_fact_as_confirmed_admission(model_fact):
    raw, spans, criteria, policy = unstated_sample()
    spans[0]['text'] = spans[0]['text'].replace('本科', '本科 全日制')
    raw['facts'].append({'field': 'admission', 'value': model_fact,
                         'evidence': {'span_id': 'R1', 'interpretation': '资料仅注明全日制，未声称统招。'}})
    result = validate_and_score(raw, spans, criteria, policy, False)
    assert result['education_gate'] == 'pass'
    assert 'admission' not in result['facts']
    assert result['fact_refs']['admission']['quote'] == spans[0]['text']
    assert result['fact_evidence_refs']['admission'][0]['quote'] == spans[0]['text']
    assert result['education']['admission'] == 'not_stated'
    assert result['education']['admission_defaulted'] is True
    assert result['education']['evidence'][0]['quote'] == spans[0]['text']


class PolicyModel:
    settings = {'model': 'synthetic', 'endpoint': 'https://example.invalid/chat'}

    def __init__(self, first, reviewed):
        self.first, self.reviewed = first, reviewed
        self.calls = []

    def complete(self, system, payload):
        self.calls.append((system, deepcopy(payload)))
        output = self.reviewed if payload['task_stage'] == 'semantic_review' else self.first
        return deepcopy(output), {'total_tokens': 1}


def test_both_model_stages_receive_new_policy_and_only_reviewed_admission_is_scored():
    raw, spans, criteria, policy = unstated_sample()
    reviewed = deepcopy(raw)
    reviewed['education']['admission'] = 'non_regular'
    model = PolicyModel(raw, reviewed)
    result = assess(model, {'name': 'Synthetic', 'is_sales': False, 'name_confirmed': True},
                    {'criteria': dump(criteria), 'fields': dump({})}, spans, {**policy, 'confirmed': True})
    assert [payload['task_stage'] for _, payload in model.calls] == ['analysis', 'semantic_review']
    for system, payload in model.calls:
        assert 'not_stated' in system
        assert payload['education_policy']['admission_not_stated_pass'] is True
        assert payload['education_policy']['admission_policy'] == 'admission-default-v1'
        assert payload['education_policy']['admission_default_value'] == 'not_stated'
        assert 'non_regular' in system and 'unknown' in system
    assert model.calls[0][1]['education_policy'] == model.calls[1][1]['education_policy']
    assert result['prompt_version'] == 'v10-validation-feedback'
    assert result['education_gate'] == 'fail' and result['grade'] == '待定'
    assert result['education']['admission'] == 'non_regular'
