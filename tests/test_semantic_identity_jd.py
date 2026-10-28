"""Model interpretation/review contract; no verbatim-language matching gate."""
from copy import deepcopy

import pytest

import hr_assist.identities as identities_module
import hr_assist.semantic as semantic_module
from hr_assist.identities import VERSION, validate_identity
from hr_assist.jd_imports import validate_extraction
from hr_assist.schemas import CandidateNameReview
from hr_assist.storage import BusinessError
from conftest import make_pdf, run
from test_core import configure
from test_jd_imports import SPANS, TEXT, output
from test_model_identity import answer


class ReviewingModel:
    settings = {'model': 'synthetic-semantic-review', 'endpoint': 'https://example.invalid'}

    def __init__(self, analysis, reviewed):
        self.analysis = analysis
        self.reviewed = reviewed
        self.fail_analysis = False
        self.fail_review = False
        self.calls = []

    def complete(self, system, payload):
        self.calls.append(deepcopy(payload))
        if payload['task_stage'] == 'semantic_review':
            if self.fail_review:
                raise BusinessError('MODEL_TIMEOUT', 'synthetic review timeout')
            return deepcopy(self.reviewed), {'total_tokens': 7}
        if self.fail_analysis:
            raise BusinessError('MODEL_TIMEOUT', 'synthetic analysis timeout')
        return deepcopy(self.analysis), {'total_tokens': 5}


def setup_identity(service, role, monkeypatch):
    configure(service)
    row = service.ingest(make_pdf(), '称呼林 先 生_投递开发方向.pdf')
    first = answer('北京', '软件工程师', role['id'], '第一次误判')
    reviewed = answer('林先生', '后端服务开发', role['id'], '称呼排版分隔，整理为林先生；投递方向是后端开发')
    model = ReviewingModel(first, reviewed)
    monkeypatch.setattr(service, 'model', lambda: model)
    return service.get_application(row['id']), model


def test_identity_semantic_value_and_explanation_are_not_required_to_be_substrings():
    source = {'id': 'F1', 'text': '称呼：林 先 生；希望做后端服务开发', 'source': 'filename'}
    raw = answer('林先生', '后端研发工程师', 'role1', '称呼中的空白是排版；明确表达后端研发投递意向')
    result = validate_identity(raw, [source], [{'id': 'role1', 'name': '软件工程师'}])
    assert result['name']['value'] not in source['text']
    for key in ('name', 'role'):
        ref = result[key]['evidence'][0]
        assert ref['interpretation'] == raw[key]['evidence'][0]['interpretation']
        assert ref['quote'] == source['text'] and ref['semantic'] is True


def test_identity_only_reviewed_result_is_saved_and_each_call_reserves(service, role, monkeypatch):
    app, model = setup_identity(service, role, monkeypatch)
    reservations = []
    proposal = service.identities.analyze(app, lambda: reservations.append('reserved'))
    assert proposal['result']['name']['value'] == '林先生'
    assert proposal['version'] == VERSION and proposal['evidence_mode'] == 'model_semantic_review'
    assert proposal['usage'] == {'total_tokens': 12}
    assert reservations == ['reserved', 'reserved']
    assert [call['task_stage'] for call in model.calls] == ['analysis', 'semantic_review']
    assert model.calls[1]['analysis_result']['name']['value'] == '北京'
    assert model.calls[0]['sources'] == model.calls[1]['sources']
    assert service.get_application(app['id'])['name'] == '待识别'


@pytest.mark.parametrize('changed', ['analysis_prompt', 'review_prompt', 'schema'])
def test_identity_proposal_invalidates_when_contract_changes_without_version_bump(service, role, monkeypatch, changed):
    app, model = setup_identity(service, role, monkeypatch)
    original = service.identities.analyze(app, lambda: None)
    if changed == 'analysis_prompt':
        monkeypatch.setattr(identities_module, 'SYSTEM', identities_module.SYSTEM + '\n新分析约束')
    elif changed == 'review_prompt':
        monkeypatch.setattr(semantic_module, 'REVIEW_INSTRUCTIONS', semantic_module.REVIEW_INSTRUCTIONS + '\n新复核约束')
    else:
        schema = deepcopy(identities_module.IdentityResult.model_json_schema())
        schema['description'] = 'Synthetic contract revision'
        monkeypatch.setattr(identities_module.IdentityResult, 'model_json_schema', lambda: deepcopy(schema))
    assert service.identities.context(app)[1] != original['fingerprint']
    with pytest.raises(BusinessError) as caught:
        service.identities.apply([{'application_id': app['id'], 'proposal_id': original['id'],
                                  'name': '林先生', 'role_id': role['id']}])
    assert caught.value.code == 'IDENTITY_STALE'
    current = service.identities.analyze(app, lambda: None)
    assert len(model.calls) == 4 and current['id'] != original['id']
    assert not current['analysis_cached']
    assert service.get_application(app['id'])['name'] == '待识别'


@pytest.mark.parametrize('confirmed', ['legacy', 'model', 'human'])
def test_location_name_compatibility_warning_applies_only_to_unconfirmed_legacy_records(service, role, monkeypatch, confirmed):
    row = service.ingest(make_pdf('Name: Beijing. Work base Beijing. Python developer.'), '申请资料.pdf', role['id'])
    service.db.execute('UPDATE applications SET name=? WHERE id=?', ('Beijing', row['id']))
    service.parse_document(service.get_application(row['id']))
    if confirmed == 'model':
        service.db.put('identity_applied:' + row['id'], {'name': 'Beijing', 'role_id': role['id'], 'proposal_id': 'synthetic-reviewed'})
    elif confirmed == 'human':
        service.review_candidate_name(row['id'], CandidateNameReview(name='Beijing', expected_name='Beijing', confirmed=True))
    if confirmed != 'legacy':
        monkeypatch.setattr('hr_assist.service.name_looks_like_location', lambda *args: pytest.fail('Completed identity must not run the legacy semantic check'))
    detail = service.get_application(row['id'])
    assert detail['name_review_required'] is (confirmed == 'legacy')
    assert detail['identity_complete'] is (confirmed != 'legacy')
    assert detail['name_confirmed'] is (confirmed == 'human')


def test_identity_review_timeout_resumes_saved_analysis_with_one_new_call(service, role, monkeypatch):
    app, model = setup_identity(service, role, monkeypatch)
    reservations = []
    model.fail_review = True
    with pytest.raises(BusinessError) as caught:
        service.identities.analyze(app, lambda: reservations.append('reserved'))
    assert caught.value.code == 'MODEL_TIMEOUT' and caught.value.diagnostics['model_step'] == 'review'
    assert service.identities.proposal(app['id']) is None
    assert service.db.get('identity_pending:' + app['id'])
    model.fail_review = False
    proposal = service.identities.analyze(app, lambda: reservations.append('reserved'))
    assert [call['task_stage'] for call in model.calls] == ['analysis', 'semantic_review', 'semantic_review']
    assert len(reservations) == 3 and proposal['analysis_cached'] is True
    assert proposal['usage'] == {'total_tokens': 7}
    assert proposal['analysis_usage'] == {'total_tokens': 5}
    assert service.db.get('identity_pending:' + app['id']) is None


def test_forced_identity_analysis_failure_does_not_revive_the_previous_draft(service, role, monkeypatch):
    app, model = setup_identity(service, role, monkeypatch)
    original = service.identities.analyze(app, lambda: None)
    model.fail_analysis = True
    model.analysis = answer('新称呼', '软件工程师', role['id'], '新的分析依据')
    with pytest.raises(BusinessError) as caught:
        service.identities.analyze(app, lambda: None, force=True)
    assert caught.value.code == 'MODEL_TIMEOUT' and caught.value.diagnostics['model_step'] == 'analysis'
    assert service.identities.proposal(app['id']) == original
    assert service.db.get('identity_pending:' + app['id'])
    model.fail_analysis = False
    replacement = service.identities.analyze(app, lambda: None)
    assert [call['task_stage'] for call in model.calls] == ['analysis', 'semantic_review', 'analysis', 'analysis', 'semantic_review']
    assert model.calls[-1]['analysis_result'] == model.analysis
    assert replacement['id'] != original['id'] and not replacement['analysis_cached']
    assert replacement['usage'] == {'total_tokens': 12}
    assert service.db.get('identity_pending:' + app['id']) is None


@pytest.mark.parametrize('applied', [False, True])
def test_failed_forced_identity_refresh_preserves_prior_record_and_resumes(service, role, monkeypatch, applied):
    app, model = setup_identity(service, role, monkeypatch)
    original = service.identities.analyze(app, lambda: None)
    if applied:
        service.identities.apply([{'application_id': app['id'], 'proposal_id': original['id'],
                                  'name': '林先生', 'role_id': role['id']}])
        app = service.get_application(app['id'])
    before = service.db.one('SELECT * FROM applications WHERE id=?', (app['id'],))
    model.analysis = answer('林先生', '软件工程师', role['id'], '新分析草稿')
    model.fail_review = True
    with pytest.raises(BusinessError, match='synthetic review timeout'):
        service.identities.analyze(app, lambda: None, force=True)
    assert service.identities.proposal(app['id']) == original
    assert service.db.one('SELECT * FROM applications WHERE id=?', (app['id'],)) == before
    row = service.identities.review_rows()[0]
    assert row['proposal_pending'] and row['proposal_applied'] is applied
    assert service.identities.complete(app) is applied
    with pytest.raises(BusinessError) as caught:
        service.identities.apply([{'application_id': app['id'], 'proposal_id': original['id'],
                                  'name': '林先生', 'role_id': role['id']}])
    assert caught.value.code == 'IDENTITY_STALE'
    model.fail_review = False
    if applied:
        # The batch shortcut for an already-applied identity must not skip a
        # pending forced refresh when the user continues the identity task.
        assert run(service, 'identity')['status'] == 'SUCCESS'
        replacement = service.identities.proposal(app['id'])
    else:
        replacement = service.identities.analyze(app, lambda: None)
    assert len(model.calls) == 5 and replacement['id'] != original['id']
    assert replacement['analysis_cached'] is True
    assert model.calls[-1]['analysis_result'] == model.analysis
    assert service.db.one('SELECT * FROM applications WHERE id=?', (app['id'],)) == before
    assert not service.identities.review_rows()[0]['proposal_pending']


@pytest.mark.parametrize('applied', [False, True])
def test_legacy_identity_proposal_stale_but_applied_history_remains(service, role, monkeypatch, applied):
    app, model = setup_identity(service, role, monkeypatch)
    proposal = service.identities.analyze(app, lambda: None)
    if applied:
        service.identities.apply([{'application_id': app['id'], 'proposal_id': proposal['id'],
                                  'name': '林先生', 'role_id': role['id']}])
    proposal['version'] = 'identity-v1-model'
    service.db.put('identity_result:' + proposal['id'], proposal)
    row = service.identities.review_rows()[0]
    assert row['proposal_applied'] is applied and row['proposal_stale'] is (not applied)
    assert row['identity_complete'] is applied
    if not applied:
        with pytest.raises(BusinessError) as caught:
            service.identities.apply([{'application_id': app['id'], 'proposal_id': proposal['id'],
                                      'name': '林先生', 'role_id': role['id']}])
        assert caught.value.code == 'IDENTITY_STALE'


def test_jd_semantic_requirement_populates_field_and_keeps_original_source():
    raw = output()
    raw['name']['value'] = 'Python研发工程师'
    raw['fields']['skills'][0]['interpretation'] = '掌握 Python 和 SQL；Docker 属于优先项。'
    draft = validate_extraction(raw, SPANS)
    assert draft['name'] not in TEXT
    assert draft['fields']['skills'] == '掌握 Python 和 SQL；Docker 属于优先项。'
    assert draft['fields']['skills'] not in TEXT
    assert draft['evidence']['skills'][0]['quote'] == TEXT
    assert draft['evidence']['skills'][0]['interpretation'] == draft['fields']['skills']
    assert draft['evidence_mode'] == 'model_semantic_review' and not draft['approved']


@pytest.mark.parametrize('kind', ['text', 'import'])
def test_jd_review_correction_and_resumable_timeout(service, monkeypatch, kind):
    configure(service)
    first, reviewed = output(), output()
    first['fields']['skills'][0]['interpretation'] = '必须掌握 Docker。'
    reviewed['fields']['skills'][0]['interpretation'] = '掌握 Python、SQL；Docker 优先。'
    model = ReviewingModel(first, reviewed)
    model.fail_review = True
    monkeypatch.setattr(service, 'model', lambda: model)
    monkeypatch.setattr('hr_assist.jd_imports.extract_file', lambda path: deepcopy(SPANS))
    if kind == 'text':
        with pytest.raises(BusinessError) as caught:
            service.jd_imports.analyze_text(TEXT)
        assert caught.value.code == 'MODEL_TIMEOUT'
        model.fail_review = False
        result = service.jd_imports.analyze_text(TEXT)
        meta = result
    else:
        item = service.jd_imports.enqueue(TEXT.encode(), '岗位.txt')
        assert service.jd_imports.process_next()
        failed = service.jd_imports.get(item['id'])
        assert failed['status'] == 'FAILED' and failed['draft'] == {}
        model.fail_review = False
        service.jd_imports.retry(item['id'])
        assert service.jd_imports.process_next()
        result = service.jd_imports.get(item['id'])
        assert result['status'] == 'READY'
        meta = result['metadata']
    assert len(model.calls) == 3
    assert [call['task_stage'] for call in model.calls] == ['analysis', 'semantic_review', 'semantic_review']
    assert model.calls[-1]['analysis_result'] == first
    assert result['draft']['fields']['skills'] == reviewed['fields']['skills'][0]['interpretation']
    assert result['draft']['evidence']['skills'][0]['quote'] == TEXT
    assert meta['analysis_cached'] is True and meta['usage'] == {'total_tokens': 7}
    assert not result['draft']['approved'] and service.roles() == []
