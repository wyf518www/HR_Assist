from copy import deepcopy
import pytest
from hr_assist.assessment import validate_and_score, default_criteria, redact, education_catalog
from hr_assist.storage import BusinessError


def sample():
    criteria=default_criteria(dict(education='本科',experience='三年',location='北京',certificates='证书A',availability='一周',skills='Python',responsibilities='开发'))
    spans=[{'id':'R1','page':1,'source':'text','text':'本科 统招 测试大学 Python 开发 三年 北京 证书A 一周'}]
    evidence={'span_id':'R1','interpretation':'语义支持要求'}
    raw={'judgments':[{'criterion_id':c['id'],'verdict':'met','evidence':[evidence],'reason':'原文可定位'} for c in criteria],
         'facts':[{'field':k,'value':v,'evidence':evidence} for k,v in [('education_level','本科'),('admission','统招'),('institution','测试大学')]]}
    policy={'schools':['测试大学'],'master_bypass':True}
    raw['education']={'degree':'bachelor','completion':'attained','admission':'national_regular',
        'institution_match':'catalog','catalog_id':education_catalog(policy)[0]['id'],
        'evidence':[evidence],'interpretation':'同一已取得的本科教育经历'}
    return raw,spans,criteria,policy


def test_scoring_hard_gate_and_unknown_not_rejection():
    raw,spans,criteria,policy=sample()
    assert validate_and_score(raw,spans,criteria,policy,False)['grade']=='强烈推荐'
    raw['education'].update(institution_match='not_in_catalog',catalog_id=None)
    assert validate_and_score(raw,spans,criteria,policy,False)['grade']=='待定'
    for item in raw['judgments']:item['verdict']='unknown';item['evidence']=[]
    result=validate_and_score(raw,spans,criteria,policy,True)
    assert result['score']==0 and result['grade']=='待定' and result['provisional']


def test_unknown_source_rejected_and_semantic_fact_is_not_literal_matched():
    raw,spans,criteria,policy=sample()
    raw['judgments'][0]['evidence']=[{'span_id':'unknown-source','interpretation':'模型解释'}]
    with pytest.raises(BusinessError) as caught:
        validate_and_score(raw,spans,criteria,policy,False)
    assert caught.value.code=='EVIDENCE_SOURCE_INVALID'
    raw,spans,criteria,policy=sample()
    raw['facts'].append({'field':'experience','value':'3年','evidence':{'span_id':'R1','interpretation':'三年的规范表达'}})
    result=validate_and_score(raw,spans,criteria,policy,False)
    assert result['facts']['experience']=='3年'
    assert result['fact_refs']['experience']['quote']==spans[0]['text']


def test_ocr_critical_evidence_unknown_and_redaction():
    raw,spans,criteria,policy=sample();spans[0]['source']='ocr'
    result=validate_and_score(raw,spans,criteria,policy,False)
    assert result['education_gate']=='unknown' and result['grade']=='待定'
    text=redact([{'text':'姓名：张测试\n张测试 test@example.com 13812345678 110101199001010012\nPython'}],'张测试')[0]['text']
    assert all(x not in text for x in ('张测试','test@example.com','13812345678','110101199001010012'))
    assert 'Python' in text


def test_full_time_does_not_claim_unified_admission():
    raw,spans,criteria,policy=sample()
    spans[0]['text']=spans[0]['text'].replace('统招','全日制')
    for item in raw['judgments']:
        item['evidence']=[{'span_id':'R1','interpretation':'只确定全日制，不能据此判定统招'}]
    for fact in raw['facts']:
        fact['evidence']={'span_id':'R1','interpretation':'只确定全日制，不能据此判定统招'}
        if fact['field']=='admission':fact['value']='全日制'
    raw['education']['admission']='not_stated'
    assessed=validate_and_score(raw,spans,criteria,policy,False)
    assert assessed['education_gate']=='pass'
    assert assessed['education']['admission']=='not_stated'
    assert assessed['education']['admission_defaulted']
    assert 'admission' not in assessed['facts']


def test_cloud_contract_and_sanitized_errors(monkeypatch):
    import httpx,json
    from hr_assist.assessment import CloudModel
    client_class=httpx.Client
    requests=[]
    def handler(request):
        requests.append(request)
        return httpx.Response(200,json={'choices':[{'message':{'content':'{"judgments":[]}'}}],'usage':{'total_tokens':12}})
    monkeypatch.setattr('hr_assist.assessment.httpx.Client',lambda **kwargs:client_class(transport=httpx.MockTransport(handler),**kwargs))
    model=CloudModel({'endpoint':'https://model.example.invalid/chat','approved':True,'model':'synthetic'},'test-secret')
    data,usage=model.complete('system',{'resume_spans':[]})
    assert data=={'judgments':[]} and usage['total_tokens']==12
    payload=json.loads(requests[0].content)
    assert payload['response_format']=={'type':'json_object'}
    def failure(request):return httpx.Response(401,text='upstream message test-secret')
    monkeypatch.setattr('hr_assist.assessment.httpx.Client',lambda **kwargs:client_class(transport=httpx.MockTransport(failure),**kwargs))
    with pytest.raises(BusinessError) as error:model.complete('system',{})
    assert error.value.code=='MODEL_AUTH' and 'test-secret' not in error.value.message
