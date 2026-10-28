from copy import deepcopy
import json

import pytest

from hr_assist.assessment import ASSESSMENT_PROMPT_VERSION, assessment_key, validate_and_score
from hr_assist.db import dump
from hr_assist.storage import BusinessError
from conftest import run
from test_assessment import sample
from test_core import configure, FakeModel


def test_identical_facts_are_merged_without_changing_score():
    raw, spans, criteria, policy = sample()
    expected = validate_and_score(raw, spans, criteria, policy, False)
    raw['facts'].extend(deepcopy(raw['facts']))
    result = validate_and_score(raw, spans, criteria, policy, False)
    for key in ('score', 'scores', 'grade', 'education_gate', 'facts', 'fact_refs'):
        assert result[key] == expected[key]
    assert result['fact_duplicate_counts'] == {'education_level': 1, 'admission': 1, 'institution': 1}
    assert all(len(refs) == 1 for refs in result['fact_evidence_refs'].values())


@pytest.mark.parametrize('ocr_first', [False, True])
def test_equal_value_different_references_keeps_ocr_gate(ocr_first):
    raw, spans, criteria, policy = sample()
    spans.append({'id': 'R2', 'page': 2, 'source': 'ocr', 'text': '本科'})
    duplicate = {'field': 'education_level', 'value': '本科', 'evidence': {'span_id': 'R2', 'quote': '本科'}}
    raw['facts'].insert(0 if ocr_first else len(raw['facts']), duplicate)
    result = validate_and_score(raw, spans, criteria, policy, False)
    assert result['education_gate'] == 'unknown' and result['grade'] == '待定'
    assert len(result['fact_evidence_refs']['education_level']) == 2
    assert result['fact_refs']['education_level']['span_id'] == 'R2'


def test_outer_padding_only_is_ignored():
    raw, spans, criteria, policy = sample()
    raw['facts'][0]['value'] = ' \t本科\n'
    assert validate_and_score(raw, spans, criteria, policy, False)['facts']['education_level'] == '本科'


@pytest.mark.parametrize('value', ['硕士', '未知', '未提及', '', ' \t', '本 科'])
def test_invented_rewritten_and_empty_fact_values_are_rejected(value):
    raw, spans, criteria, policy = sample()
    raw['facts'][0]['value'] = value
    with pytest.raises(BusinessError) as error:
        validate_and_score(raw, spans, criteria, policy, False)
    assert error.value.code == 'FACT_VALUE_MISMATCH'
    assert '学历' in error.value.message and '第1项' in error.value.message
    assert spans[0]['text'] not in error.value.message


def test_experience_numeral_conversion_still_rejected():
    raw, spans, criteria, policy = sample()
    raw['facts'].append({'field': 'experience', 'value': '3年',
                         'evidence': {'span_id': 'R1', 'quote': '三年'}})
    with pytest.raises(BusinessError) as error:
        validate_and_score(raw, spans, criteria, policy, False)
    assert error.value.code == 'FACT_VALUE_MISMATCH' and '工作年限' in error.value.message


def test_conflicting_values_never_choose_a_school():
    raw, spans, criteria, policy = sample()
    spans.append({'id': 'R2', 'page': 2, 'source': 'text', 'text': '另一测试院校'})
    raw['facts'].append({'field': 'institution', 'value': '另一测试院校',
                         'evidence': {'span_id': 'R2', 'quote': '另一测试院校'}})
    with pytest.raises(BusinessError) as error:
        validate_and_score(raw, spans, criteria, policy, False)
    assert error.value.code == 'FACT_CONFLICT' and '毕业院校' in error.value.message
    assert '测试大学' not in error.value.message and '另一测试院校' not in error.value.message


@pytest.mark.parametrize('evidence', [
    {'span_id': 'private-model-id', 'quote': '本科'},
    {'span_id': 'R1', 'quote': 'private-model-text'},
    {'span_id': 'R1', 'quote': ' '},
])
def test_invalid_evidence_of_duplicate_is_not_discarded(evidence):
    raw, spans, criteria, policy = sample()
    raw['facts'].append({**raw['facts'][0], 'evidence': evidence})
    with pytest.raises(BusinessError) as error:
        validate_and_score(raw, spans, criteria, policy, False)
    assert error.value.code == 'FACT_EVIDENCE_INVALID' and '学历' in error.value.message
    assert 'private-model' not in error.value.message


def test_prompt_change_separates_assessment_cache(monkeypatch):
    application = {'file_id': 'file', 'role_id': 'role'}
    args = (application, {'id': 'jd'}, {}, {'model': 'test'}, [])
    new_key = assessment_key(*args)
    monkeypatch.setattr('hr_assist.assessment.ASSESSMENT_PROMPT_VERSION', 'v1')
    assert new_key != assessment_key(*args)


def test_fact_failure_then_manual_retry_preserves_original_and_followups(service, application, monkeypatch):
    configure(service)
    class FactsModel(FakeModel):
        valid = False
        def complete(self, system, payload):
            self.system = system
            raw, usage = super().complete(system, payload)
            span = payload['resume_spans'][0]
            fact = {'field': 'experience', 'value': '5 years' if self.valid else 'private-invalid-value',
                    'evidence': {'span_id': span['id'], 'quote': span['text']}}
            raw['facts'] = [fact, deepcopy(fact)]
            return raw, usage
    model = FactsModel()
    monkeypatch.setattr(service, 'model', lambda: model)
    before = service.db.one('SELECT * FROM followups WHERE application_id=?', (application['id'],))
    failed = run(service)
    assert failed['status'] == 'SUCCESS_WITH_EXCEPTIONS' and model.calls == 1
    assert json.loads(failed['stats'])['assessed'] == 0
    assert service.get_application(application['id'])['assessment'] is None
    assert service.get_application(application['id'])['status'] == 'WAITING_REVIEW'
    events = dump(service.db.all("SELECT * FROM events WHERE code='FACT_VALUE_MISMATCH'"))
    assert '工作年限' in events and 'private-invalid-value' not in events and '5 years' not in events
    archive = service.storage.file(application['file_id'])
    archive_bytes = archive[1].read_bytes()
    model.valid = True
    success = run(service)
    assert success['status'] == 'SUCCESS' and model.calls == 2
    assert json.loads(success['stats'])['archived'] == 0
    assert len(service.db.all('SELECT * FROM applications')) == 1
    assert len(service.db.all('SELECT * FROM assessments')) == 1
    assert service.storage.file(application['file_id'])[1].read_bytes() == archive_bytes
    result = service.get_application(application['id'])['assessment']
    assert result['facts']['experience'] == '5 years'
    assert result['fact_duplicate_counts'] == {'experience': 1}
    assert result['prompt_version'] == ASSESSMENT_PROMPT_VERSION
    assert '每种field最多出现一次' in model.system
    assert service.db.one('SELECT * FROM followups WHERE application_id=?', (application['id'],)) == before
    # A completed record is never implicitly reassessed after changing the prompt.
    assert run(service)['status'] == 'SUCCESS' and model.calls == 2
