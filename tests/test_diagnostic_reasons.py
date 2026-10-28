from datetime import datetime
import json
import uuid

import pytest

import hr_assist.diagnostics as module
from hr_assist.diagnostics import Diagnostics
from hr_assist.storage import BusinessError


@pytest.fixture
def reason_log(tmp_path, monkeypatch):
    instant = datetime(2026, 10, 28, 20, 0, tzinfo=module.BEIJING)
    monkeypatch.setattr(Diagnostics, '_at', staticmethod(lambda at=None: instant if at is None else at.astimezone(module.BEIJING)))
    return Diagnostics(tmp_path / 'state', lambda: {'diagnostics': {'enabled': True}})


def records(log):
    return log.read('2026-10-28', limit=1000)


def test_failure_metadata_survives_write_read_export_without_sensitive_content(reason_log):
    run, application = uuid.uuid4().hex, uuid.uuid4().hex
    details = {'validation_kind': 'score_evidence', 'evidence_reason': 'no_match',
               'criterion_dimension': 'skills', 'criterion_index': 5, 'evidence_index': 2,
               'quote_characters': 18, 'quote': 'private-candidate-quote',
               'message': 'private-message', 'run_id': uuid.uuid4().hex,
               'application_id': uuid.uuid4().hex, 'trace_id': uuid.uuid4().hex,
               'error_code': 'MODEL_TIMEOUT', 'exception_type': 'ReadTimeout',
               'stack': [{'file': 'spoof.py', 'function': 'spoof', 'line': 1}]}
    with pytest.raises(BusinessError):
        with reason_log.operation('assessment', run_id=run, application_id=application) as trace:
            raise BusinessError('EVIDENCE_INVALID', 'private-original-exception', diagnostics=details)
    row = records(reason_log)[-1]
    assert row['event'] == 'PHASE_FAILED'
    assert row['error_code'] == 'EVIDENCE_INVALID'
    assert row['exception_type'] == 'BusinessError'
    assert (row['run_id'], row['application_id'], row['trace_id']) == (run, application, trace)
    assert row['validation_kind'] == 'score_evidence' and row['evidence_reason'] == 'no_match'
    assert row['criterion_dimension'] == 'skills'
    assert (row['criterion_index'], row['evidence_index'], row['quote_characters']) == (5, 2, 18)
    assert all(frame['file'] != 'spoof.py' for frame in row['stack'])
    raw = next(reason_log._directory.glob('*.jsonl')).read_text()
    exported = reason_log.export('2026-10-28').decode()
    assert 'private-' not in raw and 'private-' not in exported
    assert json.loads(exported.splitlines()[-1]) == row


@pytest.mark.parametrize('kind,code,details', [
    ('fact_evidence', 'FACT_EVIDENCE_INVALID', {'fact_field': 'institution', 'fact_index': 3, 'evidence_reason': 'ambiguous_match', 'quote_characters': 20000}),
    ('fact_value', 'FACT_VALUE_MISMATCH', {'fact_field': 'admission', 'fact_index': 1, 'evidence_reason': 'value_missing', 'value_characters': 0}),
])
def test_fact_classifications_are_retained(reason_log, kind, code, details):
    reason_log.failure('assessment', BusinessError(code, 'not logged', diagnostics={'validation_kind': kind, **details}))
    row = records(reason_log)[0]
    assert row['event'] == 'PHASE_FAILED' and row['validation_kind'] == kind
    for key, value in details.items():
        assert row[key] == value


@pytest.mark.parametrize('key', sorted(module.REASON_ENUMS))
@pytest.mark.parametrize('value', ['private-candidate-value', '__proto__', {}, [], None, True])
def test_reason_enum_values_are_allowlisted(reason_log, key, value):
    reason_log.failure('assessment', BusinessError('EVIDENCE_INVALID', 'private-exception', diagnostics={key: value}))
    assert key not in records(reason_log)[0]


@pytest.mark.parametrize('key', sorted(module.REASON_NUMBERS))
@pytest.mark.parametrize('value', [True, -1, 1.5, '5', None, 20001])
def test_reason_counts_are_bounded_integers(reason_log, key, value):
    reason_log.write('PHASE_FAILED', **{key: value})
    assert key not in records(reason_log)[0]


@pytest.mark.parametrize('key', ['criterion_index', 'fact_index', 'evidence_index'])
@pytest.mark.parametrize('value', [0, 1001])
def test_reason_ordinals_start_at_one(reason_log, key, value):
    reason_log.write('PHASE_FAILED', **{key: value})
    assert key not in records(reason_log)[0]


def test_read_sanitizes_injected_new_metadata_again(reason_log):
    reason_log.write('PHASE_STARTED', phase='assessment')
    target = next(reason_log._directory.glob('*.jsonl'))
    raw = {'timestamp': '2026-10-28T20:00:00+08:00', 'event': 'PHASE_FAILED',
           'error_code': 'FACT_EVIDENCE_INVALID', 'validation_kind': 'fact_evidence',
           'fact_field': 'private-candidate', 'evidence_reason': 'no_match',
           'fact_index': 2, 'evidence_index': -1, 'quote_characters': 30000,
           'quote': 'private-source', 'message': 'private-error'}
    with target.open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(raw) + '\n')
    row = records(reason_log)[-1]
    assert row['validation_kind'] == 'fact_evidence' and row['fact_index'] == 2
    assert row['evidence_reason'] == 'no_match'
    assert not {'fact_field', 'evidence_index', 'quote_characters', 'quote', 'message'} & row.keys()
    assert 'private-' not in reason_log.export('2026-10-28').decode()


@pytest.mark.parametrize('reason', sorted(module.REASON_ENUMS['identity_reason']))
def test_identity_wait_records_waiting_and_reraises_business_error(reason_log, reason):
    error = BusinessError('IDENTITY_REVIEW_REQUIRED', 'private-name', diagnostics={'identity_reason': reason})
    with pytest.raises(BusinessError) as raised:
        with reason_log.operation('identity'):
            raise error
    assert raised.value is error
    row = records(reason_log)[-1]
    assert row['event'] == 'PHASE_WAITING' and row['identity_reason'] == reason
    assert row['error_code'] == 'IDENTITY_REVIEW_REQUIRED'
    assert row['elapsed_ms'] >= 0
    reason_log.failure('identity', error)
    assert records(reason_log)[-1]['event'] == 'PHASE_WAITING'


def test_legacy_exceptions_and_stored_failure_events_remain_supported(reason_log):
    error = BusinessError('MODEL_TIMEOUT', 'private-secret')
    del error.diagnostics
    reason_log.failure('assessment', error)
    assert records(reason_log)[0]['event'] == 'PHASE_FAILED'
    reason_log.write('PHASE_FAILED', phase='identity', error_code='IDENTITY_REVIEW_REQUIRED')
    assert records(reason_log)[-1]['event'] == 'PHASE_FAILED'
    assert 'private-secret' not in reason_log.export('2026-10-28').decode()
