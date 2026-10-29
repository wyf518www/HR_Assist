"""Assessment format diagnostics expose schema categories, never model values."""
from copy import deepcopy
from datetime import datetime
import json

from pydantic import ValidationError
import pytest

from hr_assist.db import uid
from hr_assist.diagnostics import BEIJING, Diagnostics, REASON_ENUMS
from hr_assist.model_contract import SCORE_SCHEMA_FIELDS, parse_model_result, score_schema_metadata
from hr_assist.schemas import ModelResult
from hr_assist.storage import BusinessError


PRIVATE = 'private-score-sentinel'
DAY = '2026-10-29'


def answer():
    reference = {'span_id': PRIVATE, 'interpretation': PRIVATE}
    return {
        'judgments': [{'criterion_id': PRIVATE, 'verdict': 'met', 'reason': PRIVATE,
                       'evidence': [deepcopy(reference)]}],
        'facts': [{'field': 'institution', 'value': PRIVATE, 'evidence': deepcopy(reference)}],
        'education': {'degree': 'bachelor', 'completion': 'attained', 'admission': 'not_stated',
                      'institution_match': 'catalog', 'catalog_id': PRIVATE,
                      'evidence': [deepcopy(reference)], 'interpretation': PRIVATE},
    }


@pytest.fixture
def score_log(tmp_path, monkeypatch):
    instant = datetime(2026, 10, 29, 10, 0, tzinfo=BEIJING)
    monkeypatch.setattr(Diagnostics, '_at', staticmethod(lambda at=None: instant if at is None else at.astimezone(BEIJING)))
    return Diagnostics(tmp_path / 'state', lambda: {'diagnostics': {'enabled': True}})


@pytest.mark.parametrize('path,value,field,kind,indices', [
    ((), [PRIVATE], 'root', 'invalid_type', {}),
    (('judgments',), PRIVATE, 'judgments', 'invalid_type', {}),
    (('judgments', 0), PRIVATE, 'judgments.item', 'invalid_type', {'criterion_index': 1}),
    (('judgments', 0, 'verdict'), PRIVATE, 'judgments.verdict', 'invalid_choice', {'criterion_index': 1}),
    (('judgments', 0, 'reason'), {PRIVATE: PRIVATE}, 'judgments.reason', 'invalid_type', {'criterion_index': 1}),
    (('judgments', 0, 'evidence'), PRIVATE, 'judgments.evidence', 'invalid_type', {'criterion_index': 1}),
    (('judgments', 0, 'evidence', 0), PRIVATE, 'judgments.evidence.item', 'invalid_type', {'criterion_index': 1, 'evidence_index': 1}),
    (('judgments', 0, 'evidence', 0, 'span_id'), {PRIVATE: PRIVATE}, 'judgments.evidence.span_id', 'invalid_type', {'criterion_index': 1, 'evidence_index': 1}),
    (('judgments', 0, 'evidence', 0, 'interpretation'), '', 'judgments.evidence.interpretation', 'length_limit', {'criterion_index': 1, 'evidence_index': 1}),
    (('facts', 0, 'field'), PRIVATE, 'facts.field', 'invalid_choice', {'fact_index': 1}),
    (('facts', 0, 'value'), 12345678, 'facts.value', 'invalid_type', {'fact_index': 1}),
    (('facts', 0, 'evidence'), [], 'facts.evidence', 'invalid_type', {'fact_index': 1}),
    (('facts', 0, 'evidence', 'span_id'), {PRIVATE: PRIVATE}, 'facts.evidence.span_id', 'invalid_type', {'fact_index': 1, 'evidence_index': 1}),
    (('education', 'degree'), PRIVATE, 'education.degree', 'invalid_choice', {}),
    (('education', 'catalog_id'), 12345678, 'education.catalog_id', 'invalid_type', {}),
    (('education', 'evidence', 0), PRIVATE, 'education.evidence.item', 'invalid_type', {'evidence_index': 1}),
    (('education', 'evidence', 0, 'interpretation'), {PRIVATE: PRIVATE}, 'education.evidence.interpretation', 'invalid_type', {'evidence_index': 1}),
])
def test_schema_metadata_survives_write_read_export_without_model_values(score_log, path, value, field, kind, indices):
    raw = answer()
    if path:
        item = raw
        for part in path[:-1]:
            item = item[part]
        item[path[-1]] = value
    else:
        raw = value
    before = deepcopy(raw)
    with pytest.raises(BusinessError) as raised:
        with score_log.operation('assessment', run_id=uid(), application_id=uid()):
            parse_model_result(raw)
    error = raised.value
    expected = {'validation_kind': 'score_schema', 'model_step': 'review', 'score_schema_field': field,
                'score_schema_error': kind, 'score_schema_error_count': 1, **indices}
    assert error.code == 'MODEL_SCHEMA' and error.diagnostics == expected
    assert before == raw
    assert PRIVATE not in error.message and '12345678' not in error.message
    row = score_log.read(DAY)[-1]
    assert row['error_code'] == 'MODEL_SCHEMA'
    assert all(row[key] == value for key, value in expected.items())
    assert set(indices) == {'criterion_index', 'fact_index', 'evidence_index'} & row.keys()
    exported = score_log.export(DAY).decode()
    disk = ''.join(path.read_text() for path in score_log._directory.glob('*.jsonl'))
    assert PRIVATE not in exported and PRIVATE not in disk and '12345678' not in exported
    assert not {'input', 'msg', 'ctx', 'message', 'raw', 'value'} & row.keys()


def test_missing_fields_report_array_position_without_guessing_a_value():
    raw = answer()
    raw['judgments'].append(deepcopy(raw['judgments'][0]))
    raw['judgments'][1]['evidence'].append(deepcopy(raw['judgments'][1]['evidence'][0]))
    del raw['judgments'][1]['evidence'][1]['interpretation']
    with pytest.raises(BusinessError) as raised:
        parse_model_result(raw)
    assert raised.value.diagnostics == {
        'validation_kind': 'score_schema', 'model_step': 'review',
        'score_schema_field': 'judgments.evidence.interpretation', 'score_schema_error': 'missing',
        'score_schema_error_count': 1, 'criterion_index': 2, 'evidence_index': 2,
    }
    assert raw['judgments'][1]['evidence'][1] == {'span_id': PRIVATE}


@pytest.mark.parametrize('path,field,indices', [
    ((), 'root.extra', {}),
    (('judgments', 0), 'judgments.extra', {'criterion_index': 1}),
    (('judgments', 0, 'evidence', 0), 'judgments.evidence.extra', {'criterion_index': 1, 'evidence_index': 1}),
    (('facts', 0), 'facts.extra', {'fact_index': 1}),
    (('facts', 0, 'evidence'), 'facts.evidence.extra', {'fact_index': 1, 'evidence_index': 1}),
    (('education',), 'education.extra', {}),
    (('education', 'evidence', 0), 'education.evidence.extra', {'evidence_index': 1}),
])
def test_unknown_extra_keys_are_fixed_categories_even_with_private_field_names(path, field, indices):
    raw = answer()
    item = raw
    for part in path:
        item = item[part]
    item[PRIVATE] = {'sensitive-value': PRIVATE}
    with pytest.raises(ValidationError) as raised:
        ModelResult.model_validate(raw)
    metadata = score_schema_metadata(raised.value)
    assert metadata['score_schema_field'] == field and metadata['score_schema_error'] == 'unexpected_field'
    assert all(metadata[key] == value for key, value in indices.items())
    assert PRIVATE not in json.dumps(metadata)
    # Existing normalization still ignores extras; this diagnostic extension
    # does not broaden or narrow format acceptance.
    parsed, changes = parse_model_result(raw)
    assert changes == {'extra_fields_ignored': 1}
    assert parsed.model_extra is None


def test_error_count_is_bounded_and_allowlist_stays_in_sync():
    raw = answer()
    raw.update({PRIVATE + str(i): PRIVATE for i in range(1100)})
    with pytest.raises(ValidationError) as raised:
        ModelResult.model_validate(raw)
    metadata = score_schema_metadata(raised.value)
    assert metadata['score_schema_error_count'] == 1000
    assert PRIVATE not in json.dumps(metadata)
    assert REASON_ENUMS['score_schema_field'] == SCORE_SCHEMA_FIELDS


@pytest.mark.parametrize('key,value', [
    ('score_schema_field', PRIVATE), ('score_schema_field', 'facts.' + PRIVATE),
    ('score_schema_error', PRIVATE), ('score_schema_error', {'input': PRIVATE}),
    ('score_schema_error_count', 0), ('score_schema_error_count', 1001),
    ('score_schema_error_count', True), ('score_schema_error_count', '1'),
    ('education_catalog_reason', PRIVATE), ('education_catalog_reason', {'value': PRIVATE}),
])
def test_new_metadata_rejects_unknown_values_and_noninteger_counts(score_log, key, value):
    score_log.failure('assessment', BusinessError('MODEL_SCHEMA', PRIVATE, diagnostics={key: value}))
    assert key not in score_log.read(DAY)[-1]
    assert PRIVATE not in score_log.export(DAY).decode()


def test_read_export_resanitize_tampered_metadata_and_old_records(score_log):
    score_log.write('PHASE_STARTED', phase='assessment')
    target = next(score_log._directory.glob('*.jsonl'))
    old = {'timestamp': '2026-10-29T10:00:00.000+08:00', 'event': 'PHASE_FAILED',
           'phase': 'assessment', 'error_code': 'MODEL_SCHEMA'}
    planted = {**old, 'score_schema_field': 'facts.' + PRIVATE, 'score_schema_error': PRIVATE,
               'score_schema_error_count': True, 'education_catalog_reason': PRIVATE,
               'message': PRIVATE, 'raw': PRIVATE}
    with target.open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(old) + '\n' + json.dumps(planted) + '\n')
    rows = score_log.read(DAY)
    assert rows[-1] == old and rows[-2] == old
    assert PRIVATE not in score_log.export(DAY).decode()


@pytest.mark.parametrize('reason', ['catalog_id_missing', 'catalog_id_unknown', 'status_id_conflict'])
def test_catalog_failure_fixed_reason_is_retained_without_id_or_institution(score_log, reason):
    score_log.failure('assessment', BusinessError('EDUCATION_CATALOG_INVALID', PRIVATE, diagnostics={
        'validation_kind': 'fact_value', 'fact_field': 'institution', 'education_catalog_reason': reason,
        'catalog_id': PRIVATE, 'institution': PRIVATE,
    }))
    row = score_log.read(DAY)[-1]
    assert row['education_catalog_reason'] == reason
    assert row['fact_field'] == 'institution' and 'catalog_id' not in row and 'institution' not in row
    assert PRIVATE not in score_log.export(DAY).decode()
