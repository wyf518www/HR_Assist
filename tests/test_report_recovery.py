"""A model pause must not hide prior scores behind an Excel export failure."""
from datetime import datetime
from zoneinfo import ZoneInfo
import json

from openpyxl import load_workbook

from conftest import make_pdf, run
from test_core import configure
from test_semantic_reports import seed_score
from hr_assist.db import dump
from hr_assist.schemas import CandidateNameReview
from hr_assist.storage import BusinessError


def test_model_timeout_still_exports_previous_control_character_evidence(service, application, role, monkeypatch):
    configure(service)
    seed_score(service, application, role)
    saved = service.db.one('SELECT * FROM assessments WHERE application_id=?', (application['id'],))
    result = json.loads(saved['result'])
    result['items'][0]['evidence'][0]['quote'] = 'Original\x0bsource\x0cwith\x01controls\tand\nlines'
    result['evidence_spans'][0]['text'] = result['items'][0]['evidence'][0]['quote']
    service.db.execute('UPDATE assessments SET result=? WHERE id=?', (dump(result), saved['id']))
    service.db.execute("UPDATE applications SET status='ASSESSED' WHERE id=?", (application['id'],))
    saved = service.db.one('SELECT * FROM assessments WHERE id=?', (saved['id'],))
    manual = service.db.all('SELECT * FROM followups')
    pending = service.ingest(make_pdf('Synthetic next candidate with another project.'), 'synthetic-next.pdf', role['id'])
    service.review_candidate_name(pending['id'], CandidateNameReview(name='Synthetic Next', expected_name='待识别', confirmed=True))
    calls = []

    def timeout(*args, **kwargs):
        calls.append(True)
        raise BusinessError('MODEL_TIMEOUT', '合成测试响应等待超时。')

    monkeypatch.setattr('hr_assist.service.assess', timeout)
    batch = run(service)
    stats = json.loads(batch['stats'])
    assert batch['status'] == 'BLOCKED' and batch['stage'] == 'complete'
    assert stats['failed_phase'] == 'assessment' and stats['exceptions'] == 1
    assert stats['reports'] == 1 and stats['overview_file_id']
    assert '响应等待超时' in batch['error'] and len(calls) == 1
    output = service.storage.file(stats['overview_file_id'])[1]
    evidence = load_workbook(output)['评估证据']
    assert evidence['I2'].value == 'Original[U+000B]source[U+000C]with[U+0001]controls\tand\nlines'
    assert service.db.one('SELECT * FROM assessments WHERE id=?', (saved['id'],)) == saved
    assert service.db.one('SELECT * FROM followups WHERE application_id=?', (application['id'],)) == manual[0]
    day = datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
    records = service.diagnostics.read(day, run_id=batch['id'])
    assert any(row['event'] == 'PHASE_COMPLETED' and row['phase'] == 'report_export' for row in records)
    assert not any(row.get('error_code') == 'INTERNAL_ERROR' for row in records)
    assert not service.db.one("SELECT id FROM events WHERE code='INTERNAL_ERROR' AND run_id=?", (batch['id'],))
