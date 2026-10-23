from copy import deepcopy
import pytest
from hr_assist.assessment import validate_and_score, default_criteria, redact
from hr_assist.storage import BusinessError


def sample():
    criteria=default_criteria(dict(education='本科',experience='三年',location='北京',certificates='证书A',availability='一周',skills='Python',responsibilities='开发'))
    spans=[{'id':'R1','page':1,'source':'text','text':'本科 统招 测试大学 Python 开发 三年 北京 证书A 一周'}]
    evidence={'span_id':'R1','quote':spans[0]['text']}
    raw={'judgments':[{'criterion_id':c['id'],'verdict':'met','evidence':[evidence],'reason':'原文可定位'} for c in criteria],
         'facts':[{'field':k,'value':v,'evidence':evidence} for k,v in [('education_level','本科'),('admission','统招'),('institution','测试大学')]]}
    return raw,spans,criteria,{'schools':['测试大学'],'master_bypass':True}


def test_scoring_hard_gate_and_unknown_not_rejection():
    raw,spans,criteria,policy=sample()
    assert validate_and_score(raw,spans,criteria,policy,False)['grade']=='强烈推荐'
    policy['schools']=['另一大学']
    assert validate_and_score(raw,spans,criteria,policy,False)['grade']=='待定'
    for item in raw['judgments']:item['verdict']='unknown';item['evidence']=[]
    result=validate_and_score(raw,spans,criteria,policy,True)
    assert result['score']==0 and result['grade']=='待定' and result['provisional']


def test_fabricated_evidence_and_fact_rejected():
    raw,spans,criteria,policy=sample()
    raw['judgments'][0]['evidence']=[{'span_id':'R1','quote':'虚构经历'}]
    with pytest.raises(BusinessError,match='定位'):validate_and_score(raw,spans,criteria,policy,False)
    raw,spans,criteria,policy=sample();raw['facts'][0]['value']='硕士'
    with pytest.raises(BusinessError,match='核验'):validate_and_score(raw,spans,criteria,policy,False)


def test_ocr_critical_evidence_unknown_and_redaction():
    raw,spans,criteria,policy=sample();spans[0]['source']='ocr'
    result=validate_and_score(raw,spans,criteria,policy,False)
    assert result['education_gate']=='unknown' and result['grade']=='待定'
    text=redact([{'text':'姓名：张测试\n张测试 test@example.com 13812345678 110101199001010012\nPython'}],'张测试')[0]['text']
    assert all(x not in text for x in ('张测试','test@example.com','13812345678','110101199001010012'))
    assert 'Python' in text
