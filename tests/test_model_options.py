"""Documented GLM controls, transport isolation, cache identity and safe metadata."""
from copy import deepcopy
import hashlib
import json

import httpx
import pytest

from hr_assist import assessment
from hr_assist.assessment import CloudModel, assessment_key, ASSESSMENT_PROMPT_VERSION, assess
from hr_assist.db import dump
from hr_assist.diagnostics import _sanitize
from hr_assist.model_options import generation_profile, request_options
from hr_assist.storage import BusinessError
from test_assessment import sample


def settings(**overrides):
    return {'endpoint': 'https://open.bigmodel.cn/api/paas/v4/chat/completions',
            'model': 'glm-5.3', 'approved': True, **overrides}


def mock_transport(monkeypatch, handler):
    client = httpx.Client
    monkeypatch.setattr(assessment.httpx, 'Client', lambda **kw: client(transport=httpx.MockTransport(handler), **kw))


@pytest.mark.parametrize('effort', ['low', 'high', 'max'])
@pytest.mark.parametrize('limit', [None, 1024, 32768, 131072])
def test_exact_documented_options(effort, limit):
    profile = {'reasoning_effort': effort}
    if limit is not None:
        profile['max_tokens'] = limit
    config = settings(reasoning_effort=effort, max_tokens=limit)
    assert generation_profile(config) == profile
    assert request_options(config) == {**profile, 'thinking': {'type': 'enabled'}}


def test_default_profile_keeps_other_providers_and_legacy_request_unchanged(monkeypatch):
    requests = []
    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={'choices': [{'message': {'content': '{"ok":true}'}}]})
    mock_transport(monkeypatch, handler)
    old = settings(endpoint='https://company-approved.example.invalid/completions', model='synthetic')
    for config in (old, {**old, 'reasoning_effort': 'default', 'max_tokens': None}):
        assert generation_profile(config) == request_options(config) == {}
        assert CloudModel(config, 'secret').complete('system', {'body': 'private-resume'})[0] == {'ok': True}
    assert requests[0] == requests[1] == {
        'model': 'synthetic', 'temperature': 0,
        'messages': [{'role': 'system', 'content': 'system'}, {'role': 'user', 'content': dump({'body': 'private-resume'})}],
        'response_format': {'type': 'json_object'},
    }


@pytest.mark.parametrize('override', [
    {'reasoning_effort': 'disabled'}, {'reasoning_effort': 'medium'}, {'reasoning_effort': None},
    {'reasoning_effort': {'private-value': 'private-secret'}},
    {'max_tokens': 0}, {'max_tokens': 1023}, {'max_tokens': 131073},
    {'max_tokens': True}, {'max_tokens': 32768.0}, {'max_tokens': 'private-value'},
    {'model': 'glm-5.2', 'reasoning_effort': 'low'},
    {'model': 'glm-5.3-flash', 'reasoning_effort': 'low'},
    {'endpoint': 'https://open.bigmodel.cn.private.invalid/api/paas/v4/chat/completions', 'max_tokens': 4096},
    {'endpoint': 'http://open.bigmodel.cn/api/paas/v4/chat/completions', 'reasoning_effort': 'low'},
    {'endpoint': 'https://open.bigmodel.cn:444/api/paas/v4/chat/completions', 'reasoning_effort': 'low'},
    {'endpoint': 'https://private:secret@open.bigmodel.cn/api/paas/v4/chat/completions', 'reasoning_effort': 'low'},
    {'endpoint': 'https://open.bigmodel.cn/api/coding/paas/v4/chat/completions', 'reasoning_effort': 'low'},
    {'endpoint': 'https://open.bigmodel.cn/api/paas/v4/chat/completions?private=secret', 'reasoning_effort': 'low'},
    {'endpoint': 'https://open.bigmodel.cn/api/paas/v4/chat/completions#private', 'reasoning_effort': 'low'},
])
def test_invalid_profiles_fail_before_network_without_leaking_input(monkeypatch, override):
    calls = []
    mock_transport(monkeypatch, lambda request: calls.append(request))
    with pytest.raises(BusinessError) as error:
        CloudModel(settings(**override), 'private-api-key').complete('private-prompt', {})
    assert error.value.code == 'MODEL_REQUEST'
    assert 'private' not in error.value.message and 'secret' not in error.value.message
    assert calls == []


@pytest.mark.parametrize('endpoint', ['https://open.bigmodel.cn/api/paas/v4/chat/completions/',
                                      'https://open.bigmodel.cn:443/api/paas/v4/chat/completions'])
def test_standard_endpoint_variants(endpoint):
    assert request_options(settings(endpoint=endpoint, max_tokens=4096)) == {'max_tokens': 4096}


def test_request_and_diagnostics_use_constructor_snapshot(monkeypatch):
    records, calls = [], []
    monkeypatch.setattr(assessment, 'emit', lambda event, **fields: records.append({'event': event, **fields}))
    config = settings(reasoning_effort='low', max_tokens=32768)
    model = CloudModel(config, 'private-api-key')
    config.update(reasoning_effort='max', max_tokens=131072, endpoint='https://private.invalid')
    def handler(request):
        calls.append(json.loads(request.content))
        assert str(request.url) == settings()['endpoint']
        return httpx.Response(200, json={'choices': [{'message': {'content': '{"ok":true}'}, 'finish_reason': 'stop'}]})
    mock_transport(monkeypatch, handler)
    assert model.complete('private-prompt', {'private-key': 'private-value'})[0] == {'ok': True}
    assert len(calls) == 1
    assert calls[0]['reasoning_effort'] == 'low' and calls[0]['max_tokens'] == 32768
    assert calls[0]['thinking'] == {'type': 'enabled'}
    for row in records:
        assert row['reasoning_effort'] == 'low' and row['max_tokens'] == 32768
    assert 'private' not in dump(records)


@pytest.mark.parametrize('status,choice,expected', [
    (200, {'message': {'content': None, 'reasoning_content': '{"ok":true}'}, 'finish_reason': 'stop'}, 'MODEL_EMPTY_CONTENT'),
    (200, {'message': {'content': '{"ok":true}'}, 'finish_reason': 'length'}, 'MODEL_TRUNCATED'),
    (400, None, 'MODEL_REQUEST'),
])
def test_controls_do_not_retry_or_accept_empty_or_truncated_results(monkeypatch, status, choice, expected):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(status, json={'choices': [choice], 'private': 'private-error'})
    mock_transport(monkeypatch, handler)
    with pytest.raises(BusinessError) as error:
        CloudModel(settings(reasoning_effort='low', max_tokens=32768), 'secret').complete('system', {})
    assert error.value.code == expected and len(calls) == 1
    assert 'private' not in error.value.message


def test_profile_diagnostic_allowlist_rejects_untrusted_values():
    assert _sanitize({'reasoning_effort': 'low', 'max_tokens': 32768, 'generation_profile': {'secret': 'private'}}) == {
        'reasoning_effort': 'low', 'max_tokens': 32768}
    for fields in ({'reasoning_effort': 'private'}, {'reasoning_effort': {}}, {'max_tokens': True},
                   {'max_tokens': 32768.0}, {'max_tokens': 'private'}, {'max_tokens': 131073}):
        assert _sanitize(fields) == {}


def test_default_cache_hash_exactly_preserved_and_explicit_options_separate():
    app = {'file_id': 'f', 'role_id': 'r', 'name': 'Test', 'name_confirmed': True}
    jd, policy, spans = {'id': 'j'}, {'confirmed': True}, []
    config = settings()
    old_payload = {'file': 'f', 'role': 'r', 'jd': 'j', 'policy': policy,
                   'model': config['model'], 'endpoint': config['endpoint'], 'prompt': ASSESSMENT_PROMPT_VERSION,
                   'schema': 'v1', 'redaction': 'v2-name-review', 'name': 'Test', 'name_confirmed': True,
                   'document': hashlib.sha256(dump(spans).encode()).hexdigest()}
    expected = hashlib.sha256(dump(old_payload).encode()).hexdigest()
    assert assessment_key(app, jd, policy, config, spans) == expected
    assert assessment_key(app, jd, policy, {**config, 'reasoning_effort': 'default', 'max_tokens': None}, spans) == expected
    keys = {expected}
    for opts in ({'reasoning_effort': 'low'}, {'reasoning_effort': 'high'}, {'reasoning_effort': 'max'},
                 {'max_tokens': 32768}, {'reasoning_effort': 'low', 'max_tokens': 32768}):
        keys.add(assessment_key(app, jd, policy, {**config, **opts}, spans))
    assert len(keys) == 6


def test_assessment_audits_profile_without_changing_evidence():
    raw, spans, criteria, policy = sample()
    class Fake:
        settings = settings(reasoning_effort='low', max_tokens=32768)
        def complete(self, system, payload):
            return deepcopy(raw), {}
    result = assess(Fake(), {'name': 'Test', 'is_sales': False},
                    {'criteria': dump(criteria), 'fields': dump({'education': '本科', 'experience': '三年',
                    'certificates': '证书A', 'skills': 'Python', 'responsibilities': '开发'})},
                    spans, {**policy, 'confirmed': True})
    assert result['generation_profile'] == {'reasoning_effort': 'low', 'max_tokens': 32768}
    assert result['score'] == 100 and result['evidence_spans'] == spans
