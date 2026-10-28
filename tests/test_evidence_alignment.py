"""Source binding replaces character matching in new assessments."""
from copy import deepcopy
import pytest
from hr_assist.assessment import validate_and_score
from hr_assist.storage import BusinessError
from test_assessment import sample


@pytest.mark.parametrize('source,interpretation',[
    ('维护\n训练数据；清洗噪声','负责训练数据质量治理'),
    ('not able to deploy independently','尚不能独立部署'),
    ('合同总额100 000元，协助而非主导','参与合同约十万元，未证明主导职责'),
])
def test_semantic_explanation_and_source_are_separate(source,interpretation):
    raw,spans,criteria,policy=sample()
    spans.append({'id':'R2','page':2,'source':'text','text':source})
    item=next(j for j in raw['judgments'] if j['criterion_id']=='skills')
    item.update(verdict='partial',reason=interpretation,
                evidence=[{'span_id':'R2','interpretation':interpretation}])
    before=deepcopy((raw,spans))
    result=validate_and_score(raw,spans,criteria,policy,False)
    saved=next(i for i in result['items'] if i['id']=='skills')
    assert saved['evidence']==[{'span_id':'R2','quote':source,'interpretation':interpretation,
                              'page':2,'source':'text','semantic':True}]
    assert saved['awarded']==15 and result['score']==85
    assert (raw,spans)==before


def test_invalid_source_still_rejected_without_recording_id_or_interpretation():
    raw,spans,criteria,policy=sample()
    raw['judgments'][0]['evidence']=[{'span_id':'private-bad-id','interpretation':'private-body'}]
    with pytest.raises(BusinessError) as caught:
        validate_and_score(raw,spans,criteria,policy,False)
    assert caught.value.code=='EVIDENCE_SOURCE_INVALID'
    assert 'private' not in caught.value.message+str(caught.value.diagnostics)


def test_ocr_hard_criteria_still_require_human_review():
    raw,spans,criteria,policy=sample()
    spans.append({'id':'R2','page':2,'source':'ocr','text':'具有证书A'})
    next(j for j in raw['judgments'] if j['criterion_id']=='certificates')['evidence']=[
        {'span_id':'R2','interpretation':'识别为持有要求的证书，需人工核验OCR'}]
    result=validate_and_score(raw,spans,criteria,policy,True)
    assert result['grade']=='待定' and result['provisional']
    assert next(i for i in result['items'] if i['id']=='certificates')['verdict']=='unknown'
