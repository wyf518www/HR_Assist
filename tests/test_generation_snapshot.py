"""Mock transport checks for identity/JD generation audit and cache snapshots."""
from copy import deepcopy
import hashlib
import json

import httpx
import pytest

from hr_assist.db import dump
from hr_assist.identities import SYSTEM, VERSION
from hr_assist.semantic import REVIEW_INSTRUCTIONS
from hr_assist.storage import BusinessError
from conftest import make_pdf
from test_core import configure
from test_jd_imports import TEXT, SPANS, output
from test_model_identity import answer


def options(service, effort='low', limit=32768):
    settings = service.settings()
    settings['llm'].update(endpoint='https://open.bigmodel.cn/api/paas/v4/chat/completions',
                           model='glm-5.3', reasoning_effort=effort, max_tokens=limit)
    service.db.put('settings', settings)
    return deepcopy(settings['llm'])


def capture_transport(monkeypatch, result, callback=None):
    client = httpx.Client
    requests = []

    def handle(request):
        requests.append(json.loads(request.content))
        if callback:
            callback()
        return httpx.Response(200, json={'choices': [{'message': {'content': dump(result)},
                                                      'finish_reason': 'stop'}],
                                        'usage': {'total_tokens': 10}})

    monkeypatch.setattr('hr_assist.assessment.httpx.Client',
                        lambda **kwargs: client(transport=httpx.MockTransport(handle), **kwargs))
    return requests


def setup_identity(service, role):
    configure(service)
    options(service)
    filename = '软件工程师 测试候选人.pdf'
    row = service.ingest(make_pdf(), filename)
    app = service.get_application(row['id'])
    return app, answer('测试候选人', role['name'], role['id'], filename)


def test_default_identity_fingerprint_includes_complete_analysis_contract(service, role):
    app, _ = setup_identity(service, role)
    settings = options(service, 'default', None)
    payload, fingerprint = service.identities.context(app)
    contract = {'sources': payload['sources'], 'roles': payload['available_roles'],
              'manual': payload['human_confirmed'], 'model': settings['model'],
              'endpoint': settings['endpoint'], 'version': VERSION, 'system': SYSTEM,
              'review_system': REVIEW_INSTRUCTIONS, 'output_schema': payload['output_schema']}
    assert fingerprint == hashlib.sha256(dump(contract).encode()).hexdigest()
    options(service)
    assert service.identities.context(app)[1] != fingerprint


def test_new_mode_reanalyzes_pending_proposal_and_old_apply_is_rejected(service, role, monkeypatch):
    app, response = setup_identity(service, role)
    calls = capture_transport(monkeypatch, response)
    first = service.identities.analyze(app, lambda: None)
    assert service.identities.analyze(app, lambda: None)['id'] == first['id']
    options(service, 'high')
    before = service.db.one('SELECT * FROM applications WHERE id=?', (app['id'],))
    with pytest.raises(BusinessError) as caught:
        service.identities.apply([{'application_id': app['id'], 'proposal_id': first['id'],
                                  'name': '测试候选人', 'role_id': role['id']}])
    assert caught.value.code == 'IDENTITY_STALE'
    assert service.db.one('SELECT * FROM applications WHERE id=?', (app['id'],)) == before
    second = service.identities.analyze(app, lambda: None)
    assert second['id'] != first['id'] and len(calls) == 4
    assert [item['reasoning_effort'] for item in calls] == ['low', 'low', 'high', 'high']
    assert first['generation_profile'] == {'reasoning_effort': 'low', 'max_tokens': 32768}
    assert second['generation_profile'] == {'reasoning_effort': 'high', 'max_tokens': 32768}
    assert service.identities.analyze(app, lambda: None)['id'] == second['id']
    assert len(calls) == 4


def test_applied_identity_survives_new_mode_and_only_force_reanalyzes(service, role, monkeypatch):
    app, response = setup_identity(service, role)
    calls = capture_transport(monkeypatch, response)
    resolved = service.identities.resolve(app, lambda: None)
    assert service.identities.complete(resolved) and len(calls) == 2
    options(service, 'high')
    assert service.identities.resolve(resolved, lambda: pytest.fail('unexpected reservation')) == resolved
    assert len(calls) == 2
    proposal = service.identities.analyze(resolved, lambda: None, force=True)
    assert len(calls) == 4 and calls[-1]['reasoning_effort'] == 'high'
    assert proposal['generation_profile']['reasoning_effort'] == 'high'
    current = service.get_application(app['id'])
    assert current['name'] == resolved['name'] and current['role_id'] == resolved['role_id']


def test_identity_config_change_during_parsing_is_rejected_before_io(service, role, monkeypatch):
    app, response = setup_identity(service, role)
    original_parse = service.parse_document
    complete_text = '完整背景经历。' * 2000

    def parse_and_change(*args, **kwargs):
        spans = original_parse(*args, **kwargs)
        spans[0]['text'] += complete_text
        options(service, 'max', 65536)
        return spans

    monkeypatch.setattr(service, 'parse_document', parse_and_change)
    calls = capture_transport(monkeypatch, response)
    with pytest.raises(BusinessError) as caught:
        service.identities.analyze(app, lambda: None)
    assert caught.value.code == 'MODEL_REQUEST'
    assert not calls and service.identities.proposal(app['id']) is None
    snapshot = deepcopy(service.settings()['llm'])
    proposal = service.identities.analyze(app, lambda: None)
    payload, expected = service.identities.context(app, settings=snapshot)
    assert proposal['fingerprint'] == expected
    assert proposal['generation_profile'] == {'reasoning_effort': 'max', 'max_tokens': 65536}
    assert calls[0]['reasoning_effort'] == 'max' and calls[0]['max_tokens'] == 65536
    sent = json.loads(calls[0]['messages'][1]['content'])
    assert sent == {**payload,'task_stage':'analysis'}
    assert complete_text in next(s['text'] for s in sent['sources'] if s['id'] == 'R1')
    assert service.settings()['llm']['reasoning_effort'] == 'max'


def test_identity_change_after_request_starts_keeps_original_cache_and_audit(service, role, monkeypatch):
    app, response = setup_identity(service, role)
    snapshot = deepcopy(service.settings()['llm'])
    expected = service.identities.context(app, settings=snapshot)[1]
    calls = capture_transport(monkeypatch, response, lambda: options(service, 'max', 65536))
    proposal = service.identities.analyze(app, lambda: None)
    assert calls[0]['reasoning_effort'] == 'low' and calls[0]['max_tokens'] == 32768
    assert proposal['generation_profile'] == {'reasoning_effort': 'low', 'max_tokens': 32768}
    assert proposal['fingerprint'] == expected
    assert service.identities.context(app)[1] != expected


@pytest.mark.parametrize('kind', ['text', 'import'])
def test_jd_audits_actual_request_profile_if_settings_change_during_call(service, monkeypatch, kind):
    configure(service)
    options(service)
    calls = capture_transport(monkeypatch, output(), lambda: options(service, 'max', 65536))
    if kind == 'text':
        result = service.jd_imports.analyze_text(TEXT)
        metadata = result
    else:
        monkeypatch.setattr('hr_assist.jd_imports.extract_file', lambda _: deepcopy(SPANS))
        row = service.jd_imports.enqueue(TEXT.encode(), '测试岗位.txt')
        assert service.jd_imports.process_next()
        result = service.jd_imports.get(row['id'])
        assert result['status'] == 'READY'
        metadata = result['metadata']
    assert metadata['generation_profile'] == {'reasoning_effort': 'low', 'max_tokens': 32768}
    assert metadata['model'] == 'glm-5.3'
    assert calls[0]['reasoning_effort'] == 'low' and calls[0]['max_tokens'] == 32768
    assert result['draft']['approved'] is False and service.roles() == []
    assert service.settings()['llm']['reasoning_effort'] == 'max'
