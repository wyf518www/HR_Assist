"""Saved decisions and historical explanations remain separate from rescoring."""
from copy import deepcopy

import pytest

from hr_assist.assessment import validate_and_score
from hr_assist.assessment_explanations import decision_reasons
from test_assessment import sample


def assert_reason_contract(reasons):
    assert isinstance(reasons, list) and reasons
    for reason in reasons:
        assert {'code', 'title', 'detail', 'action'} <= reason.keys()
        assert all(isinstance(reason[key], str) and reason[key].strip()
                   for key in ('code', 'title', 'detail', 'action'))
        if 'criterion_id' in reason:
            assert isinstance(reason['criterion_id'], str) and reason['criterion_id']


@pytest.mark.parametrize('verdict,hard,score,provisional,code', [
    ('unknown', False, 90, True, 'CRITERION_UNKNOWN'),
    ('partial', True, 95, True, 'HARD_CRITERION_PARTIAL'),
    ('not_met', True, 90, False, 'HARD_CRITERION_NOT_MET'),
])
def test_stability_dimension_explains_unknown_and_custom_hard_constraints(verdict, hard, score, provisional, code):
    raw, spans, criteria, policy = sample()
    next(criterion for criterion in criteria if criterion['id'] == 'stability')['hard'] = hard
    next(judgment for judgment in raw['judgments'] if judgment['criterion_id'] == 'stability').update(
        verdict=verdict, reason='最近任职时间尚未充分满足约定要求。', question='请核验相关任职年月。')
    result = validate_and_score(raw, spans, criteria, policy, False)
    assert result['score'] == score and result['grade'] == '待定'
    assert result['base_grade'] == '强烈推荐' and result['grade_limited'] is True
    assert result['provisional'] is provisional
    assert result['education_gate'] == 'pass'
    reasons = result['decision_reasons']
    assert_reason_contract(reasons)
    assert any(reason.get('criterion_id') == 'stability' and reason['code'] == code for reason in reasons)


def test_default_nonhard_stability_partial_lowers_score_without_limiting_recommendation():
    raw, spans, criteria, policy = sample()
    next(judgment for judgment in raw['judgments'] if judgment['criterion_id'] == 'stability')['verdict'] = 'partial'
    result = validate_and_score(raw, spans, criteria, policy, False)
    assert result['score'] == 95 and result['grade'] == '强烈推荐'
    assert result['base_grade'] == '强烈推荐' and result['grade_limited'] is False
    assert result['provisional'] is False
    assert not result['decision_reasons']


def test_unknown_zero_score_has_distinct_score_grade_and_saved_waiting_decision():
    raw, spans, criteria, policy = sample()
    for judgment in raw['judgments']:
        judgment.update(verdict='unknown', evidence=[], reason='资料没有相应经历。')
    result = validate_and_score(raw, spans, criteria, policy, False)
    assert result['score'] == 0 and result['base_grade'] == '不推荐'
    assert result['grade'] == '待定' and result['grade_limited'] is True
    assert result['provisional'] and not result['hard_fail']
    assert {reason.get('criterion_id') for reason in result['decision_reasons']} == {c['id'] for c in criteria}


def test_hard_ocr_reason_explains_protection_instead_of_repeating_positive_model_reason():
    raw, spans, criteria, policy = sample()
    spans.append({'id': 'R2', 'page': 2, 'source': 'ocr', 'text': '证书A'})
    judgment = next(item for item in raw['judgments'] if item['criterion_id'] == 'certificates')
    judgment.update(verdict='met', reason='确认满足全部条件。',
                    evidence=[{'span_id': 'R2', 'interpretation': '识别出证书A。'}])
    result = validate_and_score(raw, spans, criteria, policy, False)
    reason = next(item for item in result['decision_reasons'] if item.get('criterion_id') == 'certificates')
    assert result['grade'] == '待定'
    assert 'OCR' in reason['detail'] and '核验' in reason['detail']
    assert '确认满足全部条件' not in reason['detail']
    item = next(item for item in result['items'] if item['id'] == 'certificates')
    assert item['verdict'] == 'unknown' and item['model_verdict'] == 'met'


def test_score_band_waiting_grade_is_explained_without_claiming_missing_information():
    raw, spans, criteria, policy = sample()
    next(judgment for judgment in raw['judgments'] if judgment['criterion_id'] == 'skills')['verdict'] = 'not_met'
    next(judgment for judgment in raw['judgments'] if judgment['criterion_id'] == 'stability')['verdict'] = 'partial'
    result = validate_and_score(raw, spans, criteria, policy, False)
    assert result['score'] == 65 and result['grade'] == '待定'
    assert result['base_grade'] == '待定' and result['grade_limited'] is False
    assert result['provisional'] is False and result['hard_fail'] is False
    assert_reason_contract(result['decision_reasons'])
    assert any(reason['code'] == 'SCORE_BAND_HOLD' for reason in result['decision_reasons'])
    text = ' '.join(reason['detail'] + reason['title'] for reason in result['decision_reasons'])
    assert '60' in text and '69' in text


@pytest.mark.parametrize('change,code', [
    ({'admission': 'unknown'}, 'EDUCATION_ADMISSION_UNKNOWN'),
    ({'admission': 'non_regular'}, 'EDUCATION_ADMISSION_EXCEPTION'),
    ({'institution_match': 'unknown', 'catalog_id': None}, 'EDUCATION_SCHOOL_UNKNOWN'),
    ({'institution_match': 'not_in_catalog', 'catalog_id': None}, 'EDUCATION_SCHOOL_NOT_LISTED'),
    ({'completion': 'in_progress'}, 'EDUCATION_NOT_ATTAINED'),
    ({'completion': 'unknown'}, 'EDUCATION_COMPLETION_UNKNOWN'),
    ({'evidence': []}, 'EDUCATION_SOURCE_MISSING'),
])
def test_each_education_hold_has_a_specific_structured_reason(change, code):
    raw, spans, criteria, policy = sample()
    raw['education'].update(change)
    result = validate_and_score(raw, spans, criteria, policy, False)
    assert result['grade'] == '待定'
    assert_reason_contract(result['decision_reasons'])
    assert any(reason['code'] == code for reason in result['decision_reasons'])
    assert result['base_grade'] == '强烈推荐' and result['grade_limited'] is True


def test_missing_evidence_reason_points_to_actual_unknown_item():
    raw, spans, criteria, policy = sample()
    next(judgment for judgment in raw['judgments'] if judgment['criterion_id'] == 'skills')['evidence'] = []
    result = validate_and_score(raw, spans, criteria, policy, False)
    assert result['score'] == 70 and result['grade'] == '待定'
    assert result['base_grade'] == '推荐' and result['grade_limited'] is True
    reason = next(reason for reason in result['decision_reasons'] if reason.get('criterion_id') == 'skills')
    assert '依据' in reason['detail'] or '证据' in reason['detail']


def historical_unknown_result():
    raw, spans, criteria, policy = sample()
    raw['education']['admission'] = 'unknown'
    next(judgment for judgment in raw['judgments'] if judgment['criterion_id'] == 'stability')['verdict'] = 'partial'
    old = validate_and_score(raw, spans, criteria, policy, False)
    for key in ('decision_reasons', 'base_grade', 'grade_limited', 'policy_version'):
        old.pop(key, None)
    old['education'].pop('admission_defaulted', None)
    old['prompt_version'] = 'v8-semantic-review'
    old['gate_reason'] = '依据模型复核的同一教育经历：本科须为统招，且院校对应用户已确认名录；不将全日制等同统招。'
    return old


@pytest.mark.parametrize('missing_admission', [False, True])
def test_historical_unknown_admission_is_explained_under_old_policy_without_rescoring(missing_admission):
    old = historical_unknown_result()
    if missing_admission:
        old['education'].pop('admission')
    before = deepcopy(old)
    reasons = decision_reasons(old)
    assert_reason_contract(reasons)
    assert any(reason['code'] == 'EDUCATION_ADMISSION_LEGACY_UNKNOWN' for reason in reasons)
    assert old == before
    assert old['education_gate'] == 'unknown' and old['grade'] == '待定' and old['score'] == 95
    text = ' '.join(reason['detail'] + reason['action'] for reason in reasons)
    assert ('历史' in text or '旧' in text) and ('招生' in text or '统招' in text)


def test_saved_reasons_and_fallback_explanations_never_mutate_source_results():
    old = historical_unknown_result()
    for result in (old, {**old, 'decision_reasons': [
        {'code': 'SAVED_REASON', 'title': '历史原因', 'detail': '保留已保存的评估理由。', 'action': '对照原件核验。'}
    ]}):
        before = deepcopy(result)
        reasons = decision_reasons(result)
        assert_reason_contract(reasons)
        reasons[0]['detail'] = 'Changed in caller only'
        assert result == before


def test_legacy_hold_with_incomplete_metadata_gets_explanation_without_fabricating_fact():
    result = {'score': 95, 'grade': '待定', 'provisional': True}
    before = deepcopy(result)
    assert_reason_contract(decision_reasons(result))
    assert result == before


def test_historical_pass_gate_does_not_blame_education_for_custom_hard_stability():
    raw, spans, criteria, policy = sample()
    next(criterion for criterion in criteria if criterion['id'] == 'stability')['hard'] = True
    next(judgment for judgment in raw['judgments'] if judgment['criterion_id'] == 'stability')['verdict'] = 'partial'
    result = validate_and_score(raw, spans, criteria, policy, False)
    result.pop('decision_reasons')
    result['prompt_version'] = 'v8-semantic-review'
    reasons = decision_reasons(result)
    assert_reason_contract(reasons)
    assert all(reason.get('criterion_id') == 'stability' for reason in reasons)
    assert result['education_gate'] == 'pass'
