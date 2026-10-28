"""Mock-only checks for compact final-output instructions and evidence preservation."""
from copy import deepcopy

import pytest

from hr_assist.assessment import assess, validate_and_score
from hr_assist.db import dump
from hr_assist.model_contract import ASSESSMENT_OUTPUT_INSTRUCTIONS, FINAL_JSON_INSTRUCTIONS
from hr_assist.storage import BusinessError
from test_assessment import sample
from test_core import configure
from test_jd_imports import TEXT, output


class CapturingModel:
    settings = {'model': 'synthetic-output-constraints'}

    def __init__(self, result):
        self.result = result
        self.calls = []

    def complete(self, system, payload):
        self.calls.append((system, deepcopy(payload)))
        return deepcopy(self.result), {'total_tokens': 10}


def test_assessment_sends_final_output_constraints_without_truncating_sources():
    raw, spans, criteria, policy = sample()
    spans[0]['text'] += '\n' + '用于测试的完整背景文字。' * 1000
    original = deepcopy(spans)
    model = CapturingModel(raw)
    fields = dict(education='本科', experience='三年', certificates='证书A',
                  skills='Python', responsibilities='开发')
    result = assess(model, {'name': 'Test', 'is_sales': False},
                    {'criteria': dump(criteria), 'fields': dump(fields)}, spans,
                    {**policy, 'confirmed': True})
    system, payload = model.calls[0]
    assert ASSESSMENT_OUTPUT_INSTRUCTIONS in system
    assert payload['resume_spans'] == original == spans
    assert [item['id'] for item in payload['criteria']] == [item['id'] for item in criteria]
    assert len(result['items']) == len(criteria)
    assert result['prompt_version'] == 'v8-semantic-review'
    assert len(model.calls)==2 and model.calls[1][1]['task_stage']=='semantic_review'


def test_identity_constraints_keep_full_sources_and_reuse_existing_proposal(service, application, monkeypatch):
    configure(service)
    app = service.get_application(application['id'])
    source = '任职资料完整保留。' * 1000
    monkeypatch.setattr(service, 'parse_document', lambda *a, **kw: [
        {'id': 'R1', 'page': 1, 'source': 'text', 'text': source}])
    reference = {'span_id': 'F1', 'interpretation': '附件标题表示本次投递的姓名和岗位'}
    result = {
        'name': {'status': 'identified', 'value': 'Test', 'evidence': [reference], 'note': '附件明确记载'},
        'role': {'status': 'identified', 'value': '软件工程师', 'role_id': app['role_id'],
                 'evidence': [reference], 'note': '附件明确应聘岗位'},
    }
    model = CapturingModel(result)
    monkeypatch.setattr(service, 'model', lambda: model)
    proposal = service.identities.analyze(app, lambda: None)
    repeated = service.identities.analyze(app, lambda: None)
    system, payload = model.calls[0]
    assert FINAL_JSON_INSTRUCTIONS in system
    assert next(s['text'] for s in payload['sources'] if s['id'] == 'R1') == source
    assert proposal['version'] == 'identity-v2-semantic-review'
    assert repeated['id'] == proposal['id'] and len(model.calls) == 2
    assert service.get_application(app['id'])['name'] == app['name']


def test_jd_constraints_preserve_all_source_requirements_and_leave_draft_unapproved(service, monkeypatch):
    configure(service)
    text = TEXT.split('联系：')[0] + '公司背景资料。' * 1000
    model = CapturingModel(output())
    monkeypatch.setattr(service, 'model', lambda: model)
    result = service.jd_imports.analyze_text(text)
    system, payload = model.calls[0]
    assert FINAL_JSON_INSTRUCTIONS in system
    assert payload['jd_spans'][0]['text'] == text
    assert result['draft']['fields']['skills'] == '技能：Python、SQL，Docker 优先'
    assert result['draft']['approved'] is False
    assert result['prompt_version'] == 'jd-extract-v4-semantic-review'
    assert len(model.calls) == 2 and model.calls[1][1]['task_stage'] == 'semantic_review'
    assert service.roles() == []


def test_compact_evidence_target_is_not_a_hard_three_source_limit():
    raw, spans, criteria, policy = sample()
    refs = [{'span_id': 'R1', 'interpretation': description} for description in ['本科', '统招', '测试大学', '证书A']]
    raw['judgments'][0]['evidence'] = refs
    result = validate_and_score(raw, spans, criteria, policy, False)
    assert len(result['items'][0]['evidence'])==4
    assert [r['interpretation'] for r in result['items'][0]['evidence']]==[r['interpretation'] for r in refs]
    assert all(r['quote']==spans[0]['text'] for r in result['items'][0]['evidence'])
    raw['judgments'][0]['evidence'][3]['span_id'] = 'unknown-source'
    with pytest.raises(BusinessError) as error:
        validate_and_score(raw, spans, criteria, policy, False)
    assert error.value.code == 'EVIDENCE_SOURCE_INVALID'
