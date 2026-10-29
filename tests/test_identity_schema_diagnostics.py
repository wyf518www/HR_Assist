"""Identity contract failures expose categories, never model data or unknown keys."""
from copy import deepcopy
from datetime import datetime
import json

from openpyxl.utils.exceptions import IllegalCharacterError
from pydantic import ValidationError
import pytest

from hr_assist.db import dump, now, uid
from hr_assist.diagnostics import BEIJING, Diagnostics
from hr_assist.identities import _identity_schema_diagnostic, validate_identity
from hr_assist.storage import BusinessError
from test_core import configure


DAY = '2026-10-29'
PRIVATE = 'private-identity-sentinel'


def answer():
    return {
        'name': {'status': 'identified', 'value': PRIVATE, 'note': PRIVATE,
                 'evidence': [{'span_id': 'F1', 'interpretation': PRIVATE}]},
        'role': {'status': 'identified', 'value': PRIVATE, 'note': PRIVATE, 'role_id': 'role1',
                 'evidence': [{'span_id': 'F1', 'interpretation': PRIVATE}]},
    }


def malformed(case):
    raw = answer()
    if case == 'root_type':
        return [PRIVATE]
    if case == 'missing_object':
        del raw['role']
    elif case == 'missing_value':
        del raw['name']['value']
    elif case == 'invalid_choice':
        raw['name']['status'] = PRIVATE
    elif case == 'string_type':
        raw['name']['value'] = {PRIVATE: PRIVATE}
    elif case == 'evidence_list_type':
        raw['name']['evidence'] = {PRIVATE: PRIVATE}
    elif case == 'evidence_item_type':
        raw['name']['evidence'] = [PRIVATE]
    elif case == 'root_extra':
        raw[PRIVATE] = PRIVATE
    elif case == 'name_extra':
        raw['name'][PRIVATE] = PRIVATE
    elif case == 'evidence_extra':
        raw['role']['evidence'][0][PRIVATE] = PRIVATE
    elif case == 'note_length':
        raw['role']['note'] = PRIVATE * 100
    elif case == 'nested_type':
        raw['role']['evidence'][0]['span_id'] = {PRIVATE: PRIVATE}
    elif case == 'nested_missing':
        del raw['role']['evidence'][0]['interpretation']
    elif case == 'role_id_type':
        raw['role']['role_id'] = {PRIVATE: PRIVATE}
    return raw


@pytest.fixture
def identity_log(tmp_path, monkeypatch):
    instant = datetime(2026, 10, 29, 10, 0, tzinfo=BEIJING)
    monkeypatch.setattr(Diagnostics, '_at', staticmethod(lambda at=None: instant if at is None else at.astimezone(BEIJING)))
    return Diagnostics(tmp_path / 'state', lambda: {'diagnostics': {'enabled': True}})


def read_rows(log):
    return log.read(DAY, limit=1000)


@pytest.mark.parametrize('case,field,kind,index', [
    ('root_type', 'root', 'invalid_type', None),
    ('missing_object', 'role', 'missing', None),
    ('missing_value', 'name.value', 'missing', None),
    ('invalid_choice', 'name.status', 'invalid_choice', None),
    ('string_type', 'name.value', 'invalid_type', None),
    ('evidence_list_type', 'name.evidence', 'invalid_type', None),
    ('evidence_item_type', 'name.evidence.item', 'invalid_type', 1),
    ('root_extra', 'root.extra', 'unexpected_field', None),
    ('name_extra', 'name.extra', 'unexpected_field', None),
    ('evidence_extra', 'role.evidence.extra', 'unexpected_field', 1),
    ('note_length', 'role.note', 'length_limit', None),
    ('nested_type', 'role.evidence.span_id', 'invalid_type', 1),
    ('nested_missing', 'role.evidence.interpretation', 'missing', 1),
    ('role_id_type', 'role.role_id', 'invalid_type', None),
])
def test_schema_diagnostics_survive_write_read_export_without_values(identity_log, case, field, kind, index):
    raw = malformed(case)
    before = deepcopy(raw)
    with pytest.raises(BusinessError) as raised:
        with identity_log.operation('identity', run_id=uid(), application_id=uid()):
            validate_identity(raw, [{'id': 'F1', 'text': PRIVATE, 'source': 'filename'}], [{'id': 'role1'}])
    error = raised.value
    assert error.code == 'IDENTITY_SCHEMA'
    assert raw == before
    expected = {'validation_kind': 'identity_schema', 'model_step': 'review',
                'identity_schema_field': field, 'identity_schema_error': kind,
                'identity_schema_error_count': 1}
    if index is not None:
        expected['identity_evidence_index'] = index
    assert error.diagnostics == expected
    assert PRIVATE not in str(error) and PRIVATE not in error.message
    row = read_rows(identity_log)[-1]
    assert row['error_code'] == 'IDENTITY_SCHEMA'
    assert row['exception_type'] == 'BusinessError'
    assert all(row[key] == value for key, value in expected.items())
    if index is None:
        assert 'identity_evidence_index' not in row
    exported = identity_log.export(DAY).decode()
    raw_disk = ''.join(path.read_text() for path in identity_log._directory.glob('*.jsonl'))
    assert PRIVATE not in exported and PRIVATE not in raw_disk
    assert not {'input', 'msg', 'ctx', 'context', 'message', 'raw'} & row.keys()
    assert json.loads(exported.splitlines()[-1]) == row


def test_multiple_schema_errors_have_bounded_count_and_value_free_message(identity_log):
    raw = answer()
    for key in range(1100):
        raw[f'{PRIVATE}-{key}'] = PRIVATE
    with pytest.raises(BusinessError) as raised:
        validate_identity(raw, [], [])
    error = raised.value
    assert error.diagnostics['identity_schema_error_count'] == 1000
    assert error.diagnostics['identity_schema_field'] == 'root.extra'
    assert error.diagnostics['identity_schema_error'] == 'unexpected_field'
    assert len(error.message) < 1000 and PRIVATE not in error.message
    identity_log.failure('identity', error)
    assert read_rows(identity_log)[-1]['identity_schema_error_count'] == 1000
    assert PRIVATE not in identity_log.export(DAY).decode()


@pytest.mark.parametrize('field,value', [
    ('identity_schema_field', PRIVATE),
    ('identity_schema_field', 'name.' + PRIVATE),
    ('identity_schema_error', PRIVATE),
    ('identity_schema_error', {'input': PRIVATE}),
    ('identity_schema_error_count', 0),
    ('identity_schema_error_count', 1001),
    ('identity_schema_error_count', True),
    ('identity_schema_error_count', '1'),
    ('identity_evidence_index', 0),
    ('identity_evidence_index', 1001),
    ('identity_evidence_index', True),
    ('identity_evidence_index', 1.5),
])
def test_new_metadata_rejects_untrusted_paths_types_and_counts(identity_log, field, value):
    identity_log.write('PHASE_FAILED', phase='identity', **{field: value})
    assert field not in read_rows(identity_log)[-1]
    assert PRIVATE not in identity_log.export(DAY).decode()


def test_read_and_export_resanitize_injected_identity_fields(identity_log):
    identity_log.write('PHASE_STARTED', phase='identity')
    target = next(identity_log._directory.glob('*.jsonl'))
    planted = {'timestamp': '2026-10-29T10:00:00+08:00', 'event': 'PHASE_FAILED',
               'phase': 'identity', 'error_code': 'IDENTITY_SCHEMA', 'model_step': 'review',
               'validation_kind': 'identity_schema', 'identity_schema_field': 'name.' + PRIVATE,
               'identity_schema_error': PRIVATE, 'identity_schema_error_count': 1,
               'identity_evidence_index': 20001, 'raw': PRIVATE, 'input': PRIVATE,
               'msg': PRIVATE, 'ctx': {'input': PRIVATE}}
    with target.open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(planted) + '\n')
    row = read_rows(identity_log)[-1]
    assert row['validation_kind'] == 'identity_schema'
    assert row['identity_schema_error_count'] == 1 and row['model_step'] == 'review'
    assert not {'identity_schema_field', 'identity_schema_error', 'identity_evidence_index', 'raw', 'input', 'msg', 'ctx'} & row.keys()
    assert PRIVATE not in identity_log.export(DAY).decode()


def test_custom_validation_context_message_and_unknown_path_are_never_relayed(identity_log):
    validation = ValidationError.from_exception_data('Synthetic', [{
        'type': 'value_error', 'loc': ('name', 'evidence', 20000, PRIVATE),
        'input': PRIVATE, 'ctx': {'error': ValueError(PRIVATE)},
    }])
    message, details = _identity_schema_diagnostic(validation)
    assert details['identity_schema_field'] == 'name.evidence.extra'
    assert details['identity_schema_error'] == 'invalid_format'
    assert 'identity_evidence_index' not in details
    assert PRIVATE not in message
    identity_log.failure('identity', BusinessError('IDENTITY_SCHEMA', message, diagnostics=details))
    row = read_rows(identity_log)[-1]
    assert row['identity_schema_field'] == 'name.evidence.extra'
    assert PRIVATE not in identity_log.export(DAY).decode()


@pytest.mark.parametrize('code', ['IDENTITY_VALUE', 'IDENTITY_EVIDENCE', 'IDENTITY_ROLE'])
def test_identity_business_error_codes_are_preserved_without_messages(identity_log, code):
    identity_log.failure('identity', BusinessError(code, PRIVATE))
    row = read_rows(identity_log)[-1]
    assert row['error_code'] == code and row['exception_type'] == 'BusinessError'
    assert PRIVATE not in identity_log.export(DAY).decode()


@pytest.mark.parametrize('error', [
    IllegalCharacterError(PRIVATE),
    UnicodeEncodeError('ascii', PRIVATE + '\u4e00', 0, 1, PRIVATE),
])
def test_export_exception_types_are_logged_without_sensitive_exception_text(identity_log, error):
    identity_log.failure('report_export', error)
    row = read_rows(identity_log)[-1]
    assert row['exception_type'] == type(error).__name__
    assert row['error_code'] == 'INTERNAL_ERROR'
    assert PRIVATE not in identity_log.export(DAY).decode()


def test_failed_review_keeps_identity_history_followup_and_existing_proposal(service, application, role, monkeypatch):
    configure(service)
    assessment_id = uid()
    saved_result = {'score': 95, 'grade': '待定', 'items': [], 'facts': {}}
    service.db.execute('INSERT INTO assessments VALUES (?,?,?,?,?,?,?)',
                       (assessment_id, application['id'], role['current_version'], uid(), 'COMPLETED', dump(saved_result), now()))
    service.db.execute("UPDATE applications SET assessment_id=?,status='ASSESSED' WHERE id=?", (assessment_id, application['id']))
    proposal_id = uid()
    proposal = {'id': proposal_id, 'result': answer(), 'version': 'historical-version'}
    service.db.put('identity_result:' + proposal_id, proposal)
    service.db.put('identity_proposal:' + application['id'], proposal_id)
    before = {table: service.db.all('SELECT * FROM ' + table) for table in ('applications', 'assessments', 'followups')}

    class BadReviewModel:
        settings = {'model': 'synthetic-only', 'endpoint': 'https://example.invalid/chat'}
        calls = []

        def complete(self, system, payload):
            self.calls.append(payload['task_stage'])
            return deepcopy(answer() if payload['task_stage'] == 'analysis' else malformed('nested_type')), {'total_tokens': 1}

    model = BadReviewModel()
    monkeypatch.setattr(service, 'model_snapshot', lambda settings: model)
    with pytest.raises(BusinessError) as raised:
        service.identities.analyze(service.get_application(application['id']), lambda: None, force=True)
    assert raised.value.code == 'IDENTITY_SCHEMA'
    assert raised.value.diagnostics['model_step'] == 'review'
    assert model.calls == ['analysis', 'semantic_review']
    assert all(service.db.all('SELECT * FROM ' + table) == data for table, data in before.items())
    assert service.identities.proposal(application['id']) == proposal
    assert service.db.get('identity_pending:' + application['id'])
