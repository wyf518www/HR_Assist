"""Detail reads add explanations without rewriting historical hiring records."""
from copy import deepcopy

from hr_assist.db import dump, now, uid


def test_detail_explains_old_admission_unknown_without_regrading_or_model_call(service, application, role, monkeypatch):
    def forbidden():
        raise AssertionError('Reading historical explanations must not invoke a model')
    monkeypatch.setattr(service, 'model', forbidden)
    historical = {
        'score': 95, 'grade': '待定', 'provisional': True, 'hard_fail': False,
        'education_gate': 'unknown', 'gate_reason': '旧版本科政策说明',
        'education': {'degree': 'bachelor', 'completion': 'attained', 'admission': 'unknown',
                      'institution_match': 'catalog', 'catalog_name': '示例大学',
                      'evidence': [{'span_id': 'R1', 'source': 'text', 'quote': '示例大学，本科，已毕业'}]},
        'prompt_version': 'v8-semantic-review', 'items': [], 'facts': {},
        'scores': {'hard': 20, 'skills': 30, 'experience': 30, 'achievements': 10, 'stability': 5},
    }
    identity = uid()
    service.db.execute('INSERT INTO assessments VALUES (?,?,?,?,?,?,?)',
                       (identity, application['id'], role['current_version'], uid(), 'COMPLETED', dump(historical), now()))
    service.db.execute("UPDATE applications SET assessment_id=?,status='ASSESSED' WHERE id=?", (identity, application['id']))
    before_assessment = service.db.one('SELECT * FROM assessments WHERE id=?', (identity,))
    before_application = service.db.one('SELECT * FROM applications WHERE id=?', (application['id'],))
    before_followup = service.db.one('SELECT * FROM followups WHERE application_id=?', (application['id'],))
    result = service.get_application(application['id'])['assessment']
    assert result['score'] == 95 and result['grade'] == '待定' and result['education_gate'] == 'unknown'
    assert result['education']['admission'] == 'unknown'
    assert any('招生' in reason['title'] + reason['detail'] for reason in result['decision_reasons'])
    original_part = deepcopy(result)
    original_part.pop('decision_reasons')
    assert original_part == historical
    assert service.db.one('SELECT * FROM assessments WHERE id=?', (identity,)) == before_assessment
    assert service.db.one('SELECT * FROM applications WHERE id=?', (application['id'],)) == before_application
    assert service.db.one('SELECT * FROM followups WHERE application_id=?', (application['id'],)) == before_followup
