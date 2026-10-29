from copy import deepcopy
import json

import pytest
from pydantic import ValidationError

from hr_assist.assessment import ASSESSMENT_PROMPT_VERSION, assessment_key, validate_and_score
from hr_assist.db import dump
from hr_assist.model_contract import output_schema, parse_model_result, schema_diagnostic
from hr_assist.schemas import ModelResult, RoleInput, SettingsInput
from hr_assist.storage import BusinessError
from conftest import run
from test_assessment import sample
from test_core import FakeModel, configure


def test_shape_only_compatibility_keeps_scores_and_sources_unchanged():
    raw, spans, criteria, policy = sample()
    baseline = validate_and_score(raw, spans, criteria, policy, False)
    raw['score'] = 999
    raw['private-extra-key'] = 'private-extra-value'
    for judgment in raw['judgments']:
        judgment['question'] = None
        judgment['evidence'] = judgment['evidence'][0]
        judgment['evidence']['private-extra-key'] = 'private-extra-quote'
        judgment['verdict'] = '满足'
        judgment['awarded'] = 999
        judgment['hard'] = False
    for fact in raw['facts']:
        fact['evidence'] = [fact['evidence']]
        fact['score'] = 999
    original = deepcopy(raw)
    result = validate_and_score(raw, spans, criteria, policy, False)
    for key in ('score', 'scores', 'grade', 'hard_fail', 'education_gate', 'facts', 'fact_refs'):
        assert result[key] == baseline[key]
    assert 'private-extra' not in dump(result)
    assert raw == original
    assert result['format_adjustments']['judgment_evidence_object_to_list'] == len(criteria)
    assert result['format_adjustments']['fact_evidence_single_list_to_object'] == len(raw['facts'])


@pytest.mark.parametrize('label,canonical', [(' 满足 ', 'met'), ('部分满足', 'partial'), ('不满足', 'not_met'), ('未知', 'unknown'), (' MET ', 'met'), ('Not_Met', 'not_met')])
def test_only_exact_verdict_labels_are_normalized(label, canonical):
    raw, *_ = sample()
    raw['judgments'][0]['verdict'] = label
    parsed, changes = parse_model_result(raw)
    assert parsed.judgments[0].verdict == canonical
    assert changes['verdict_label_normalized'] == 1


def test_null_optional_evidence_becomes_unknown_not_positive_or_negative():
    raw, spans, criteria, policy = sample()
    raw['facts'] = None
    for judgment in raw['judgments']:
        judgment.update(evidence=None, question=None, verdict='not_met', reason='unsupported-private-result')
    result = validate_and_score(raw, spans, criteria, policy, False)
    assert result['facts'] == {} and result['score'] == 0 and result['grade'] == '待定'
    assert result['provisional'] and not result['hard_fail']
    assert 'unsupported-private' not in dump(result)
    assert len(result['validation_warnings']) == len(criteria)


@pytest.mark.parametrize('field', ['criterion_id', 'verdict', 'reason'])
def test_missing_required_fields_are_not_filled(field):
    raw, *_ = sample()
    raw['judgments'][0].pop(field)
    with pytest.raises(BusinessError) as error:
        parse_model_result(raw)
    assert error.value.code == 'MODEL_SCHEMA'
    assert f'judgments[1].{field}：缺少必填字段' in error.value.message


@pytest.mark.parametrize('field,value,expected', [
    ('verdict', 'private-invalid-verdict', '判定或字段名不在允许范围'),
    ('reason', None, '应为文本'),
    ('reason', ['private-reason'], '应为文本'),
    ('evidence', 'private-citation', '应为数组'),
    ('question', {'private-key': 'private-value'}, '应为文本'),
])
def test_schema_errors_show_position_and_type_without_values(field, value, expected):
    raw, *_ = sample()
    raw['judgments'][0][field] = value
    with pytest.raises(BusinessError) as error:
        parse_model_result(raw)
    assert f'judgments[1].{field}' in error.value.message and expected in error.value.message
    assert 'private-' not in error.value.message


@pytest.mark.parametrize('evidence', [None, [], [{'span_id': 'R1', 'interpretation': '本科经历'}, {'span_id': 'R1', 'interpretation': '统招方式'}]])
def test_fact_evidence_is_never_invented_or_selected_from_multiple(evidence):
    raw, *_ = sample()
    raw['facts'][0]['evidence'] = evidence
    with pytest.raises(BusinessError) as error:
        parse_model_result(raw)
    assert 'facts[1].evidence：应为对象' in error.value.message


def test_required_citation_parts_and_numbers_remain_strict():
    raw, *_ = sample()
    raw['judgments'][0]['evidence'] = {'interpretation': 'private-quote'}
    raw['facts'][0]['value'] = 123
    with pytest.raises(BusinessError) as error:
        parse_model_result(raw)
    assert 'judgments[1].evidence[1].span_id：缺少必填字段' in error.value.message
    assert 'facts[1].value：应为文本' in error.value.message
    assert 'private-quote' not in error.value.message and '123' not in error.value.message


def test_no_semantic_json_wrappers_or_criterion_arrays_are_guessed():
    raw, *_ = sample()
    for value in ({'result': raw}, {'judgments': {'private-id': raw['judgments'][0]}}, []):
        with pytest.raises(BusinessError) as error:
            parse_model_result(value)
        assert error.value.code == 'MODEL_SCHEMA' and 'private-' not in error.value.message


@pytest.mark.parametrize('target', ['judgments', 'facts', 'evidence', 'reason', 'interpretation'])
def test_collection_and_text_limits_are_not_truncated(target):
    raw, *_ = sample()
    if target == 'judgments':
        raw[target] = [deepcopy(raw[target][0]) for _ in range(61)]
    elif target == 'facts':
        raw[target] = [deepcopy(raw[target][0]) for _ in range(16)]
    elif target == 'evidence':
        raw['judgments'][0][target] = raw['judgments'][0][target] * 13
    elif target == 'reason':
        raw['judgments'][0][target] = 'X' * 1501
    else:
        raw['judgments'][0]['evidence'][0]['interpretation'] = 'X' * 5001
    with pytest.raises(BusinessError) as error:
        parse_model_result(raw)
    assert '超过长度限制' in error.value.message and 'X' * 10 not in error.value.message


def test_diagnostic_masks_extra_keys_and_limits_output():
    raw, *_ = sample()
    raw['private-sensitive-key'] = 'private-sensitive-value'
    for item in raw['judgments']:
        item['reason'] = {'private-body': 'private-value'}
    try:
        ModelResult.model_validate(raw)
    except ValidationError as error:
        message = schema_diagnostic(error)
    assert 'private-' not in message and '另有' in message and len(message) < 800
    try:
        ModelResult.model_validate({'judgments': [], 'private-sensitive-key': 'private-sensitive-value'})
    except ValidationError as error:
        assert '未知字段' in schema_diagnostic(error) and 'private-' not in schema_diagnostic(error)


def test_output_compatibility_does_not_relax_http_input_schemas():
    for model, data in ((SettingsInput, {'private-extra': True}), (RoleInput, {'name': 'test', 'private-extra': True})):
        with pytest.raises(ValidationError):
            model.model_validate(data)
    schema = output_schema()
    assert schema['additionalProperties'] is False
    assert schema['$defs']['Judgment']['properties']['evidence']['type'] == 'array'
    assert schema['$defs']['Fact']['properties']['evidence']['$ref'].endswith('/SemanticReference')


def test_compatible_shape_does_not_bypass_unknown_source_id():
    raw, spans, criteria, policy = sample()
    raw['judgments'][0].update(verdict='满足', evidence={'span_id': 'private-source-id', 'interpretation': '模型的语义解释'}, score=100)
    with pytest.raises(BusinessError) as error:
        validate_and_score(raw, spans, criteria, policy, False)
    assert error.value.code == 'EVIDENCE_SOURCE_INVALID'


def test_contract_prompt_cache_version(monkeypatch):
    args = ({'file_id': 'f', 'role_id': 'r'}, {'id': 'j'}, {}, {'model': 'test'}, [])
    current = assessment_key(*args)
    monkeypatch.setattr('hr_assist.assessment.ASSESSMENT_PROMPT_VERSION', 'v3-evidence')
    assert current != assessment_key(*args)


def test_schema_failure_diagnosis_then_compatible_retry_preserves_data(service, application, monkeypatch):
    configure(service)
    class ContractModel(FakeModel):
        compatible = False
        def complete(self, system, payload):
            self.system, self.payload = system, payload
            raw, usage = super().complete(system, payload)
            if not self.compatible:
                raw['judgments'][0]['reason'] = {'private-candidate': 'private-body'}
            else:
                raw.update(facts=None, score=999, **{'private-extra-key': 'private-extra-value'})
                for item in raw['judgments']:
                    item.update(question=None, verdict='满足', evidence=item['evidence'][0], points=999)
            return raw, usage
    model = ContractModel()
    monkeypatch.setattr(service, 'model', lambda: model)
    before = service.db.one('SELECT * FROM followups WHERE application_id=?', (application['id'],))
    original = service.storage.file(application['file_id'])[1].read_bytes()
    failed = run(service)
    assert model.calls == 2 and json.loads(failed['stats'])['assessed'] == 0
    assert service.get_application(application['id'])['assessment'] is None
    event = service.db.one("SELECT message FROM events WHERE code='MODEL_SCHEMA'")
    assert 'judgments[1].reason：应为文本' in event['message'] and 'private-' not in event['message']
    model.compatible = True
    success = run(service)
    assert success['status'] == 'SUCCESS' and model.calls == 3
    assert json.loads(success['stats'])['archived'] == 0
    assert json.loads(success['stats'])['exceptions'] == 0
    assert len(service.db.all('SELECT * FROM applications')) == 1
    result = service.get_application(application['id'])['assessment']
    assert result['score'] == 100 and result['prompt_version'] == ASSESSMENT_PROMPT_VERSION
    assert 'private-' not in dump(result)
    assert model.payload['output_schema'] == output_schema() and 'output_schema' in model.system
    logs = dump(service.db.all('SELECT code,message FROM events'))
    assert 'MODEL_FORMAT_NORMALIZED' in logs and 'private-' not in logs
    assert service.storage.file(application['file_id'])[1].read_bytes() == original
    assert service.db.one('SELECT * FROM followups WHERE application_id=?', (application['id'],)) == before
    assert run(service)['status'] == 'SUCCESS' and model.calls == 3


def test_model_quote_is_ignored_and_source_text_is_bound_by_server():
    raw,spans,criteria,policy=sample()
    ref=raw['judgments'][0]['evidence'][0]
    ref['quote']='模型伪装成原文的文字'
    result=validate_and_score(raw,spans,criteria,policy,False)
    assert result['items'][0]['evidence'][0]['quote']==spans[0]['text']
    assert '模型伪装成原文的文字' not in dump(result)


def test_new_schema_uses_semantic_references_and_typed_education():
    schema=output_schema()
    reference=schema['$defs']['SemanticReference']
    assert set(reference['properties'])=={'span_id','interpretation'}
    assert reference['additionalProperties'] is False
    education=schema['$defs']['EducationResult']['properties']
    assert set(education['degree']['enum'])=={'bachelor','master','doctor','other','unknown'}
    assert set(education['completion']['enum'])=={'attained','in_progress','unknown'}
    assert set(education['admission']['enum'])=={'national_regular','not_stated','non_regular','unknown'}
