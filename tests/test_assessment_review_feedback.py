"""Contract feedback guides the existing second pass without another request."""
from copy import deepcopy

import pytest

import hr_assist.assessment as assessment_module
from hr_assist.assessment import assess, assessment_review_feedback
from hr_assist.db import dump
from hr_assist.semantic import REVIEW_INSTRUCTIONS, two_pass
from hr_assist.storage import BusinessError
from test_assessment import sample
from test_semantic_assessment import PairedModel, assess_args
from test_semantic_pipeline import DraftStore, Model, PAYLOAD


PRIVATE = 'private-value-not-allowed-in-validation-feedback'


def invalid_first(case, raw):
    first = deepcopy(raw)
    if case == 'schema':
        first['judgments'][0]['reason'] = {PRIVATE: PRIVATE}
    elif case == 'catalog_missing':
        first['education']['catalog_id'] = None
    elif case == 'catalog_unknown':
        first['education']['catalog_id'] = PRIVATE
    elif case == 'catalog_conflict':
        first['education']['institution_match'] = 'unknown'
    else:
        raise AssertionError(case)
    return first


@pytest.mark.parametrize('case,code,reason', [
    ('schema', 'MODEL_SCHEMA', None),
    ('catalog_missing', 'EDUCATION_CATALOG_INVALID', 'catalog_id_missing'),
    ('catalog_unknown', 'EDUCATION_CATALOG_INVALID', 'catalog_id_unknown'),
    ('catalog_conflict', 'EDUCATION_CATALOG_INVALID', 'status_id_conflict'),
])
def test_existing_review_receives_safe_failure_and_can_correct_it_in_two_calls(case, code, reason):
    raw, spans, criteria, policy = sample()
    first = invalid_first(case, raw)
    before = deepcopy((first, spans, criteria, policy))
    model = PairedModel(first, raw)
    reservations = []
    result = assess(model, *assess_args(raw, spans, criteria, policy), reserve=lambda: reservations.append(True))
    assert [p['task_stage'] for p in model.calls] == ['analysis', 'semantic_review']
    assert len(reservations) == 2 and result['score'] == 100
    assert result['prompt_version'] == 'v10-validation-feedback'
    assert 'validation_feedback' not in model.calls[0]
    feedback = model.calls[1]['validation_feedback']
    assert feedback['status'] == 'invalid' and feedback['error_code'] == code
    assert feedback['scope'] == 'structure_and_source_ids_only'
    assert feedback['diagnostics']['model_step'] == 'analysis'
    if reason:
        assert feedback['diagnostics']['education_catalog_reason'] == reason
    else:
        assert feedback['diagnostics']['score_schema_field'] == 'judgments.reason'
        assert feedback['diagnostics']['criterion_index'] == 1
    assert PRIVATE not in dump(feedback)
    assert model.calls[1]['analysis_result'] == first
    assert (first, spans, criteria, policy) == before
    assert 'validation_feedback' not in result


@pytest.mark.parametrize('case,code', [
    ('schema', 'MODEL_SCHEMA'), ('catalog_unknown', 'EDUCATION_CATALOG_INVALID'),
])
def test_invalid_review_is_rejected_without_using_valid_analysis_or_retrying(case, code):
    raw, spans, criteria, policy = sample()
    model = PairedModel(raw, invalid_first(case, raw))
    reservations = []
    store = DraftStore()
    with pytest.raises(BusinessError) as caught:
        assess(model, *assess_args(raw, spans, criteria, policy), db=store, reserve=lambda: reservations.append(True))
    assert caught.value.code == code
    assert len(model.calls) == len(reservations) == 2
    assert model.calls[1]['validation_feedback']['status'] == 'valid'
    assert len(store.rows) == 1 and next(iter(store.rows.values()))['raw'] == raw


def test_valid_feedback_does_not_determine_final_score():
    raw, spans, criteria, policy = sample()
    review = deepcopy(raw)
    next(j for j in review['judgments'] if j['criterion_id'] == 'skills')['verdict'] = 'not_met'
    model = PairedModel(raw, review)
    result = assess(model, *assess_args(raw, spans, criteria, policy))
    feedback = model.calls[1]['validation_feedback']
    assert set(feedback) == {'stage', 'scope', 'instructions', 'status'}
    assert feedback['status'] == 'valid'
    assert result['score'] == 70 and result['grade'] == '推荐'
    assert len(model.calls) == 2


@pytest.mark.parametrize('case', ['schema', 'catalog_missing', 'catalog_unknown', 'catalog_conflict'])
def test_feedback_is_pure_and_does_not_relay_original_text_or_invalid_values(case):
    raw, spans, criteria, policy = sample()
    raw = invalid_first(case, raw)
    spans[0]['text'] += '\n' + PRIVATE
    raw['facts'][0]['value'] = PRIVATE
    before = deepcopy((raw, spans, criteria, policy))
    feedback = assessment_review_feedback(raw, spans, criteria, policy, False)
    assert feedback['status'] == 'invalid' and PRIVATE not in dump(feedback)
    assert (raw, spans, criteria, policy) == before
    assert not {'raw', 'input', 'source', 'quote', 'score', 'scores', 'grade', 'result'} & feedback.keys()


def test_old_one_call_analysis_draft_resumes_with_feedback_on_review_only(monkeypatch):
    raw, spans, criteria, policy = sample()
    first = invalid_first('catalog_unknown', raw)
    model = PairedModel(first, raw)
    store = DraftStore()
    reservations = []

    # Simulate the previous caller: identical analysis system/payload with no
    # review_feedback argument. Its first-stage cache must remain reusable.
    def previous_caller(*args, **kwargs):
        kwargs.pop('review_feedback', None)
        return two_pass(*args, **kwargs)

    def one_call_budget():
        reservations.append(True)
        if len(reservations) > 1:
            raise BusinessError('MODEL_BUDGET', 'Synthetic single-call budget')

    monkeypatch.setattr(assessment_module, 'two_pass', previous_caller)
    with pytest.raises(BusinessError) as caught:
        assess(model, *assess_args(raw, spans, criteria, policy), db=store, reserve=one_call_budget)
    assert caught.value.code == 'MODEL_BUDGET'
    assert len(model.calls) == 1 and len(store.rows) == 1
    saved = deepcopy(store.rows)
    monkeypatch.setattr(assessment_module, 'two_pass', two_pass)
    second_reservations = []
    result = assess(model, *assess_args(raw, spans, criteria, policy), db=store,
                    reserve=lambda: second_reservations.append(True))
    assert [p['task_stage'] for p in model.calls] == ['analysis', 'semantic_review']
    assert len(second_reservations) == 1 and result['analysis_cached']
    assert result['usage']['total_tokens'] == 7 and result['score'] == 100
    assert model.calls[1]['validation_feedback']['error_code'] == 'EDUCATION_CATALOG_INVALID'
    assert model.calls[1]['analysis_result'] == first and store.rows == saved


@pytest.mark.parametrize('task', ['identity', 'jd'])
def test_callers_without_feedback_keep_their_existing_request_contract(task):
    model = Model()
    payload = deepcopy(PAYLOAD)
    before = deepcopy(payload)
    result, _ = two_pass(model, 'synthetic rules', payload, task=task)
    assert result == {'finding': 'reviewed'} and len(model.calls) == 2
    assert model.calls[0][1] == {**before, 'task_stage': 'analysis'}
    assert model.calls[1][1] == {**before, 'task_stage': 'semantic_review',
                              'analysis_result': {'finding': 'initial'},
                              'review_instructions': REVIEW_INSTRUCTIONS}
    assert payload == before


@pytest.mark.parametrize('cached', [False, True])
def test_mutating_feedback_callback_cannot_change_cached_analysis_or_review_input(cached):
    model = Model()
    store = DraftStore()
    if cached:
        two_pass(model, 'rules', PAYLOAD, task='assessment', db=store)
    saved = deepcopy(store.rows) if cached else None
    seen = []

    def mutating_callback(draft):
        seen.append(deepcopy(draft))
        draft['finding'] = 'Changed only inside callback'
        draft[PRIVATE] = PRIVATE
        return {'status': 'invalid', 'message': 'Synthetic feedback only'}

    result, meta = two_pass(model, 'rules', PAYLOAD, task='assessment', db=store, review_feedback=mutating_callback)
    assert result == {'finding': 'reviewed'}
    assert seen == [{'finding': 'initial'}]
    assert model.calls[-1][1]['analysis_result'] == {'finding': 'initial'}
    assert next(iter(store.rows.values()))['raw'] == {'finding': 'initial'}
    assert PRIVATE not in dump(store.rows)
    if cached:
        assert store.rows == saved and meta['analysis_cached']
        assert len(model.calls) == 3
    else:
        assert not meta['analysis_cached'] and len(model.calls) == 2
