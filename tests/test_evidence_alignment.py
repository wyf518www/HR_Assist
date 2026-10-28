from copy import deepcopy

import pytest

from hr_assist.assessment import validate_and_score
from hr_assist.evidence import locate_quote
from hr_assist.storage import BusinessError
from test_assessment import sample


@pytest.mark.parametrize('text,quote',[
    ('沟通谈判、资源整合与项\n目统筹能力','沟通谈判、资源整合与项目统筹能力'),
    ('具备 Python\u00a0开发经验','具备 Python 开发经验'),
    ('负责模型训练，测试\t部署','负责模型训练，测试部署'),
])
def test_only_layout_is_aligned_to_actual_source(text,quote):
    raw,spans,criteria,policy=sample()
    spans.append({'id':'R2','page':2,'source':'text','text':text})
    item=next(j for j in raw['judgments'] if j['criterion_id']=='skills')
    item['evidence']=[{'span_id':'R2','quote':quote}]
    before=deepcopy(raw)
    result=validate_and_score(raw,spans,criteria,policy,True)
    saved=next(i for i in result['items'] if i['id']=='skills')['evidence'][0]
    assert saved['quote']==text and result['score']==100
    assert result['format_adjustments']['evidence_whitespace_aligned']==1
    assert raw==before


@pytest.mark.parametrize('text,quote',[
    ('不熟悉 Python','熟练 Python'),
    ('项目金额100万','项目金额1000万'),
    ('甲项目；乙项目','甲项目乙项目'),
    ('维护客户。负责售后。','维护客户，负责售后。'),
    ('测试 A 工作。测试 A 工作。','测试A工作'),
    ('not able to deploy','notable to deploy'),
    ('100 000元','100000元'),
])
def test_substantive_changes_and_ambiguous_alignment_fail(text,quote):
    assert locate_quote(text,quote) is None


def test_wrong_span_still_rejected_without_logging_quote():
    raw,spans,criteria,policy=sample()
    raw['judgments'][0]['evidence']=[{'span_id':'bad-private-id','quote':'private-body'}]
    with pytest.raises(BusinessError) as error:
        validate_and_score(raw,spans,criteria,policy,True)
    assert error.value.code=='EVIDENCE_INVALID' and '片段编号不存在' in error.value.message
    assert 'private' not in error.value.message


def test_normalized_ocr_evidence_keeps_manual_verification_gate():
    raw,spans,criteria,policy=sample()
    spans.append({'id':'R2','page':2,'source':'ocr','text':'具有证书 A'})
    next(j for j in raw['judgments'] if j['criterion_id']=='certificates')['evidence']=[{'span_id':'R2','quote':'具有证书A'}]
    result=validate_and_score(raw,spans,criteria,policy,True)
    assert result['provisional'] and result['grade']=='待定'
