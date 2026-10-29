"""Independent cross-record and cache checks for candidate concurrency."""
from copy import deepcopy
from datetime import datetime
import hashlib
import json
import threading
from zoneinfo import ZoneInfo

import pytest

from hr_assist.assessment import assessment_key
from hr_assist.diagnostics import emit
from hr_assist.reports import save_followup
from hr_assist.schemas import CandidateNameReview, FollowupInput, RoleInput
from hr_assist.semantic import two_pass
from hr_assist.storage import BusinessError
from conftest import run
from test_core import configure, FakeModel
from test_semantic_pipeline import Model, PAYLOAD


def test_parallel_same_pdf_two_roles_keep_archives_scores_and_manual_fields_separate(
        service, role, role_data, pdf, monkeypatch):
    other_data = deepcopy(role_data)
    other_data.update(name='第二个岗位')
    other_data['fields']['skills'] = 'Different specialized skill'
    other = service.save_role(RoleInput(**other_data))
    configure(service)
    settings = service.settings()
    settings['llm']['resume_concurrency'] = 2
    service.db.put('settings', settings)
    identities = []
    for index, target in enumerate((role, other)):
        app = service.ingest(pdf, 'same-original.pdf', target['id'])
        identities.append(app['id'])
        service.review_candidate_name(app['id'], CandidateNameReview(
            name='Alex', expected_name='待识别', confirmed=True))
        save_followup(service.db, app['id'], FollowupInput(
            changes={'面试结果': '通过' if index == 0 else '待定', '面试状态': '待面试' if index == 0 else '已面'},
            expected_revision=0))
    assert len(service.db.all('SELECT * FROM files WHERE kind="resumes"')) == 1
    before_followup = service.db.all('SELECT * FROM followups ORDER BY application_id')
    barrier = threading.Barrier(2)
    model = FakeModel()
    calls = []
    lock = threading.Lock()

    # Keep the mocked response builder independent from the concurrent wrapper.
    original_builder = FakeModel()

    def concurrent_complete(system, payload):
        skill = next(c['description'] for c in payload['criteria'] if c['id'] == 'skills')
        with lock:
            calls.append((skill, payload['task_stage']))
        emit('MODEL_REQUEST_STARTED', request_bytes=100)
        barrier.wait(timeout=8)
        result, usage = original_builder.complete(system, payload)
        if skill == 'Different specialized skill':
            next(j for j in result['judgments'] if j['criterion_id'] == 'skills')['verdict'] = 'partial'
        return result, usage

    monkeypatch.setattr(model, 'complete', concurrent_complete)
    monkeypatch.setattr(service, 'model', lambda: model)
    batch = run(service)
    assert batch['status'] == 'SUCCESS', batch['error']
    assert json.loads(batch['stats'])['model_calls'] == 4
    assert len(calls) == 4
    assert [service.get_application(identity)['assessment']['score'] for identity in identities] == [100, 85]
    assert service.db.all('SELECT * FROM followups ORDER BY application_id') == before_followup
    assert len(service.db.all('SELECT * FROM documents')) == 1
    for target in (role, other):
        archives = list((service.env.data_dir / 'resumes' / target['name']).glob('*.pdf'))
        assert len(archives) == 1
        assert hashlib.sha256(archives[0].read_bytes()).digest() == hashlib.sha256(pdf).digest()
    day = datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
    rows = service.diagnostics.read(day)
    requests = [r for r in rows if r['event'] == 'MODEL_REQUEST_STARTED']
    assert len(requests) == 4
    for identity in identities:
        own = [r for r in requests if r['application_id'] == identity]
        assert len(own) == 2 and {r['model_step'] for r in own} == {'analysis', 'review'}
        assert all(r['run_id'] == batch['id'] and r['phase'] == 'assessment' for r in own)


def test_scheduling_changes_do_not_invalidate_drafts_identity_or_assessment_keys(service, application):
    configure(service)
    app = service.get_application(application['id'])
    _, identity_before = service.identities.context(app)
    spans, jd = service.parse_application(app)
    settings = service.settings()
    assessment_before = assessment_key(app, jd, settings['policy'], settings['llm'], spans)
    model = Model()
    model.settings = deepcopy(settings['llm'])
    model.fail_review = True
    with pytest.raises(BusinessError, match='synthetic timeout'):
        two_pass(model, 'rules', PAYLOAD, task='identity', db=service.db)
    settings['llm'].update(resume_concurrency=10, max_calls_per_run=1000)
    service.db.put('settings', settings)
    model.settings = deepcopy(settings['llm'])
    model.fail_review = False
    result, meta = two_pass(model, 'rules', PAYLOAD, task='identity', db=service.db)
    assert result == {'finding': 'reviewed'} and meta['analysis_cached']
    assert [p['task_stage'] for _, p in model.calls] == ['analysis', 'semantic_review', 'semantic_review']
    assert service.identities.context(app)[1] == identity_before
    assert assessment_key(app, jd, settings['policy'], settings['llm'], spans) == assessment_before
