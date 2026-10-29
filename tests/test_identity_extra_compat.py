"""Root-only format compatibility never guesses identity or weakens evidence."""
from copy import deepcopy

import pytest

from hr_assist.db import dump, now, uid
from hr_assist.identities import IdentityResult, validate_identity
from hr_assist.storage import BusinessError
from test_core import configure


PRIVATE = 'private-extra-key-or-value'
SOURCES = [{'id': 'F1', 'text': 'Synthetic candidate applying for the supplied role.', 'source': 'filename'}]
ROLES = [{'id': 'role1', 'name': 'Synthetic role'}]


def answer():
    evidence = [{'span_id': 'F1', 'interpretation': '本次投递的姓名和岗位来自附件资料。'}]
    return {
        'name': {'status': 'identified', 'value': 'Synthetic Candidate', 'note': '明确姓名', 'evidence': deepcopy(evidence)},
        'role': {'status': 'identified', 'value': 'Synthetic role', 'role_id': 'role1',
                 'note': '明确本次岗位', 'evidence': deepcopy(evidence)},
    }


@pytest.mark.parametrize('extra', [
    {PRIVATE: PRIVATE},
    {'score': 100, PRIVATE: {PRIVATE: [PRIVATE]}},
    {'result': {PRIVATE: PRIVATE}, 'name_alternative': PRIVATE, 'role_id': PRIVATE},
])
def test_only_known_root_objects_affect_result_and_raw_is_unchanged(extra):
    raw = answer()
    expected = validate_identity(raw, SOURCES, ROLES)
    raw.update(extra)
    original = deepcopy(raw)
    changes = {}
    result = validate_identity(raw, SOURCES, ROLES, format_adjustments=changes)
    assert result == expected and raw == original
    assert set(result) == {'name', 'role'}
    assert changes == {'extra_fields_ignored': len(extra)}
    assert PRIVATE not in dump(result) and PRIVATE not in dump(changes)
    assert result['name']['evidence'][0]['quote'] == SOURCES[0]['text']


@pytest.mark.parametrize('path', [('name',), ('role',), ('name', 'evidence', 0), ('role', 'evidence', 0)])
def test_nested_extra_fields_remain_strict(path):
    raw = answer()
    raw[PRIVATE] = PRIVATE
    target = raw
    for part in path:
        target = target[part]
    target[PRIVATE] = PRIVATE
    changes = {}
    with pytest.raises(BusinessError) as raised:
        validate_identity(raw, SOURCES, ROLES, format_adjustments=changes)
    assert raised.value.code == 'IDENTITY_SCHEMA'
    assert raised.value.diagnostics['identity_schema_error'] == 'unexpected_field'
    assert PRIVATE not in raised.value.message and PRIVATE not in dump(raised.value.diagnostics)
    assert changes == {}


@pytest.mark.parametrize('missing', ['name', 'role'])
def test_root_wrapper_does_not_replace_missing_required_object(missing):
    complete = answer()
    raw = {key: value for key, value in complete.items() if key != missing}
    raw['result'] = complete
    with pytest.raises(BusinessError) as raised:
        validate_identity(raw, SOURCES, ROLES)
    assert raised.value.code == 'IDENTITY_SCHEMA'
    assert raised.value.diagnostics['identity_schema_field'] == missing
    assert raised.value.diagnostics['identity_schema_error'] == 'missing'


@pytest.mark.parametrize('case,code', [
    ('unknown_source', 'EVIDENCE_SOURCE_INVALID'), ('wrong_role', 'IDENTITY_ROLE'),
    ('empty_name', 'IDENTITY_EVIDENCE'), ('missing_evidence', 'IDENTITY_EVIDENCE'),
    ('bad_status', 'IDENTITY_SCHEMA'),
])
def test_extra_fields_do_not_relax_existing_value_source_or_role_validation(case, code):
    raw = answer()
    raw[PRIVATE] = PRIVATE
    if case == 'unknown_source':
        raw['name']['evidence'][0]['span_id'] = PRIVATE
    elif case == 'wrong_role':
        raw['role']['role_id'] = PRIVATE
    elif case == 'empty_name':
        raw['name']['value'] = ''
    elif case == 'missing_evidence':
        raw['name']['evidence'] = []
    else:
        raw['name']['status'] = PRIVATE
    with pytest.raises(BusinessError) as raised:
        validate_identity(raw, SOURCES, ROLES)
    assert raised.value.code == code
    assert PRIVATE not in raised.value.message


def test_model_prompt_schema_still_requests_only_documented_fields():
    schema = IdentityResult.model_json_schema()
    assert schema['additionalProperties'] is False
    assert set(schema['properties']) == {'name', 'role'}
    assert schema['$defs']['IdentityValue']['additionalProperties'] is False


def test_valid_review_with_extra_root_field_saves_safe_proposal_without_changing_history(
        service, application, role, monkeypatch):
    configure(service)
    assessment_id = uid()
    result = {'score': 95, 'grade': '待定', 'items': [], 'facts': {}}
    service.db.execute('INSERT INTO assessments VALUES (?,?,?,?,?,?,?)',
                       (assessment_id, application['id'], role['current_version'], uid(), 'COMPLETED', dump(result), now()))
    service.db.execute("UPDATE applications SET assessment_id=?,status='ASSESSED' WHERE id=?", (assessment_id, application['id']))
    original = {table: service.db.all('SELECT * FROM ' + table) for table in ('applications', 'assessments', 'followups')}

    class Model:
        settings = {'model': 'synthetic-only', 'endpoint': 'https://example.invalid/chat'}

        def __init__(self):
            self.calls = []

        def complete(self, system, payload):
            self.calls.append(payload['task_stage'])
            raw = answer()
            raw['role']['role_id'] = role['id']
            if payload['task_stage'] == 'semantic_review':
                raw[PRIVATE] = {'candidate_payload': PRIVATE}
            return raw, {'total_tokens': 1}

    model = Model()
    monkeypatch.setattr(service, 'model_snapshot', lambda settings: model)
    proposal = service.identities.analyze(service.get_application(application['id']), lambda: None, force=True)
    assert model.calls == ['analysis', 'semantic_review']
    assert proposal['format_adjustments'] == {'extra_fields_ignored': 1}
    assert set(proposal['result']) == {'name', 'role'} and PRIVATE not in dump(proposal)
    assert service.identities.proposal(application['id']) == proposal
    assert all(service.db.all('SELECT * FROM ' + table) == rows for table, rows in original.items())
    events = service.db.all("SELECT * FROM events WHERE code='MODEL_FORMAT_NORMALIZED'")
    assert len(events) == 1 and events[0]['application_id'] == application['id']
    assert '1个' in events[0]['message'] and PRIVATE not in dump(events)
    assert not service.db.get('identity_pending:' + application['id'])
