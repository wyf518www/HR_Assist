from copy import deepcopy
from io import BytesIO
import json

from openpyxl import load_workbook
import pytest

from hr_assist.assessment import ASSESSMENT_PROMPT_VERSION, assessment_key, validate_and_score
from hr_assist.db import dump
from hr_assist.reports import overview_export
from hr_assist.storage import BusinessError
from conftest import run
from test_assessment import sample
from test_core import FakeModel, configure


@pytest.mark.parametrize('criterion_id,points', [('education', 5), ('skills', 30), ('achievements', 10)])
@pytest.mark.parametrize('verdict', ['met', 'partial', 'not_met'])
def test_missing_evidence_is_unknown_and_not_a_rejection(criterion_id, points, verdict):
    raw, spans, criteria, policy = sample()
    source = next(j for j in raw['judgments'] if j['criterion_id'] == criterion_id)
    source.update(verdict=verdict, evidence=[], reason='unsupported-private-assertion', question='unsupported-private-question')
    before = deepcopy(raw)
    result = validate_and_score(raw, spans, criteria, policy, False)
    item = next(i for i in result['items'] if i['id'] == criterion_id)
    assert item['verdict'] == 'unknown' and item['awarded'] == 0 and item['model_verdict'] == verdict
    assert result['score'] == 100 - points
    assert result['grade'] == '待定' and result['provisional'] and not result['hard_fail']
    assert '待人工核验' in item['reason'] and item['question']
    assert 'unsupported-private' not in dump(result)
    assert len(result['validation_warnings']) == 1
    assert result['validation_warnings'][0]['code'] == 'EVIDENCE_DOWNGRADED'
    assert raw == before  # Input and original evidence aren't mutated.


def test_unknown_without_evidence_discards_unsupported_prose_and_is_not_an_api_error():
    raw, spans, criteria, policy = sample()
    for judgment in raw['judgments']:
        judgment.update(verdict='unknown', reason='private-negative-claim', question='private-negative-question')
        del judgment['evidence']
    result = validate_and_score(raw, spans, criteria, policy, True)
    assert result['score'] == 0 and result['grade'] == '待定' and result['provisional']
    assert result['validation_warnings'] == [] and result['advantages'] == []
    assert 'private-negative' not in dump(result)


def test_actual_negative_evidence_is_not_downgraded():
    raw, spans, criteria, policy = sample()
    spans.append({'id': 'R2', 'page': 2, 'source': 'text', 'text': '尚未取得证书A'})
    item = next(j for j in raw['judgments'] if j['criterion_id'] == 'certificates')
    item.update(verdict='not_met', evidence=[{'span_id': 'R2', 'interpretation': '该来源明确说明尚未取得证书'}], reason='原文明确未取得要求的证书')
    result = validate_and_score(raw, spans, criteria, policy, False)
    assert result['hard_fail'] and not result['provisional']
    assert result['validation_warnings'] == []
    assert next(i for i in result['items'] if i['id'] == 'certificates')['verdict'] == 'not_met'


@pytest.mark.parametrize('verdict', ['met', 'partial', 'not_met', 'unknown'])
@pytest.mark.parametrize('evidence,code', [
    ({'span_id': 'private-bad-id', 'interpretation': '模型认为支持该条件'}, 'EVIDENCE_SOURCE_INVALID'),
    ({'span_id': 'R1', 'interpretation': ' '}, 'MODEL_SCHEMA'),
    ({'span_id': 'R1', 'quote': 'private-bad-quote'}, 'MODEL_SCHEMA'),
])
def test_missing_source_or_empty_interpretation_still_stops_scoring(verdict, evidence, code):
    raw, spans, criteria, policy = sample()
    raw['judgments'][0].update(verdict=verdict, evidence=[evidence])
    with pytest.raises(BusinessError) as error:
        validate_and_score(raw, spans, criteria, policy, False)
    assert error.value.code == code
    if code == 'EVIDENCE_SOURCE_INVALID':
        assert error.value.diagnostics['criterion_index'] == 1
        assert error.value.diagnostics['criterion_dimension'] == 'hard'
    assert 'private-bad' not in error.value.message


def test_reviewed_interpretation_is_not_presented_as_a_source_quote():
    raw, spans, criteria, policy = sample()
    interpretation='该教育经历可对应本科要求，表述可以经过整理'
    raw['judgments'][0]['evidence']=[{'span_id':'R1','interpretation':interpretation}]
    result=validate_and_score(raw,spans,criteria,policy,False)
    reference=result['items'][0]['evidence'][0]
    assert reference['interpretation']==interpretation
    assert reference['quote']==spans[0]['text']
    assert reference['semantic'] is True


def test_explicit_unrestricted_jd_does_not_need_resume_evidence():
    raw, spans, criteria, policy = sample()
    next(c for c in criteria if c['id'] == 'certificates')['description'] = '证书：不限'
    judgment = next(j for j in raw['judgments'] if j['criterion_id'] == 'certificates')
    judgment.update(requirement_mode='unrestricted', verdict='not_met', evidence=[], reason='private-invented-certificate', question='private-invented-question')
    result = validate_and_score(raw, spans, criteria, policy, True)
    item = next(i for i in result['items'] if i['id'] == 'certificates')
    assert item['verdict'] == 'met' and item['awarded'] == 3
    assert result['score'] == 100 and not result['provisional']
    assert result['validation_warnings'] == []
    assert 'JD' in item['reason'] and not item['question']
    assert 'private-invented' not in dump(result)


def test_custom_criterion_log_uses_saved_order_without_private_description():
    raw, spans, criteria, policy = sample()
    criteria[0].update(id='private-criterion-id', description='private-jd-text')
    raw['judgments'][0].update(criterion_id='private-criterion-id', evidence=[])
    raw['judgments'].reverse()
    result = validate_and_score(raw, spans, criteria, policy, False)
    warning = result['validation_warnings'][0]['message']
    assert '评分第1项（硬性条件）' in warning
    assert 'private-' not in warning


@pytest.mark.parametrize('duplicate', [False, True])
def test_missing_or_duplicate_judgment_still_fails(duplicate):
    raw, spans, criteria, policy = sample()
    if duplicate:
        raw['judgments'][1] = deepcopy(raw['judgments'][0])
    else:
        raw['judgments'].pop()
    with pytest.raises(BusinessError) as error:
        validate_and_score(raw, spans, criteria, policy, False)
    assert error.value.code == 'MODEL_CRITERIA'


def test_new_evidence_prompt_does_not_reuse_previous_prompt_cache(monkeypatch):
    args = ({'file_id': 'f', 'role_id': 'r'}, {'id': 'j'}, {}, {'model': 'test'}, [])
    current = assessment_key(*args)
    monkeypatch.setattr('hr_assist.assessment.ASSESSMENT_PROMPT_VERSION', 'v2-facts')
    assert current != assessment_key(*args)


def test_missing_evidence_completes_after_two_pass_review_without_further_retries(service, application, monkeypatch):
    configure(service)
    class MissingModel(FakeModel):
        def complete(self, system, payload):
            self.system = system
            raw, usage = super().complete(system, payload)
            item = next(j for j in raw['judgments'] if j['criterion_id'] == 'achievements')
            item.update(verdict='not_met', evidence=[], reason='private-unsupported-result', question='private-unsupported-question')
            return raw, usage
    model = MissingModel()
    monkeypatch.setattr(service, 'model', lambda: model)
    before = service.db.one('SELECT * FROM followups WHERE application_id=?', (application['id'],))
    original = service.storage.file(application['file_id'])[1].read_bytes()
    record = run(service)
    stats = json.loads(record['stats'])
    assert record['status'] == 'SUCCESS_WITH_EXCEPTIONS'
    assert stats['assessed'] == 1 and stats['exceptions'] == 1 and stats['grades']['待定'] == 1
    assert model.calls == 2
    row = service.get_application(application['id'])
    assert row['assessment']['score'] == 90 and row['assessment']['provisional']
    assert row['assessment']['prompt_version'] == ASSESSMENT_PROMPT_VERSION
    assert service.storage.file(application['file_id'])[1].read_bytes() == original
    assert service.db.one('SELECT * FROM followups WHERE application_id=?', (application['id'],)) == before
    events = service.db.all('SELECT code,message FROM events WHERE run_id=?', (record['id'],))
    assert 'private-unsupported' not in dump(events)
    assert any(e['code'] == 'EVIDENCE_DOWNGRADED' and '项目成果' in e['message'] for e in events)
    assert any(e['code'] == 'ASSESSED' and '暂计分' in e['message'] for e in events)
    assert '不能把缺失信息当不满足' in model.system
    # Workbook and HTML use the neutral replacement, not unsupported model prose.
    file = overview_export(service.db, service.storage)
    book = load_workbook(BytesIO(service.storage.file(file['id'])[1].read_bytes()))
    overview = list(book['招聘推荐总览'].values)
    assert overview[1][3:5] == (90, '待定')
    assert '暂计分' in overview[1][9]
    values = str([list(sheet.values) for sheet in book.worksheets])
    assert 'private-unsupported' not in values
    assert any(r[5:7] == ('unknown', '0') for r in list(book['评估证据'].values)[1:])
    reports = service.db.all("SELECT * FROM files WHERE name LIKE '%推荐报告.html'")
    assert reports
    for report in reports:
        html = service.storage.file(report['id'])[1].read_text(encoding='utf-8')
        assert '暂计分' in html and '未知，待核验' in html and 'private-unsupported' not in html
    assert run(service)['status'] == 'SUCCESS' and model.calls == 2
