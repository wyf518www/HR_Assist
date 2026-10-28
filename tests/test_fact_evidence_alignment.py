"""Semantic facts keep model interpretations separate from source-owned text."""
from copy import deepcopy
import pytest
from hr_assist.assessment import validate_and_score
from hr_assist.storage import BusinessError
from test_assessment import sample


def test_semantic_fact_values_need_not_be_source_substrings():
    raw,spans,criteria,policy=sample()
    raw['facts']=[{'field':'experience','value':'3年',
                  'evidence':{'span_id':'R1','interpretation':'资料明确陈述三年，统一数字表达'}}]
    before=deepcopy((raw,spans))
    result=validate_and_score(raw,spans,criteria,policy,False)
    assert result['facts']['experience']=='3年'
    ref=result['fact_refs']['experience']
    assert ref['quote']==spans[0]['text'] and ref['semantic']
    assert ref['interpretation']=='资料明确陈述三年，统一数字表达'
    assert (raw,spans)==before


def test_model_supplied_quote_cannot_replace_server_source():
    raw,spans,criteria,policy=sample()
    raw['facts'][0]['evidence']['quote']='private invented quote'
    result=validate_and_score(raw,spans,criteria,policy,False)
    assert result['fact_refs']['education_level']['quote']==spans[0]['text']
    assert 'private invented quote' not in str(result)


def test_unknown_fact_source_reports_only_safe_metadata():
    raw,spans,criteria,policy=sample()
    raw['facts'][0]['evidence']['span_id']='private-model-identifier'
    with pytest.raises(BusinessError) as caught:
        validate_and_score(raw,spans,criteria,policy,False)
    error=caught.value
    assert error.code=='EVIDENCE_SOURCE_INVALID'
    assert error.diagnostics['validation_kind']=='fact_evidence'
    assert error.diagnostics['fact_field']=='education_level' and error.diagnostics['fact_index']==1
    assert 'private' not in str(error.diagnostics)+error.message


def test_score_source_metadata_uses_jd_order_and_ref_position():
    raw,spans,criteria,policy=sample()
    index=next(i for i,c in enumerate(criteria,1) if c['id']=='skills')
    item=next(j for j in raw['judgments'] if j['criterion_id']=='skills')
    item['evidence']=[deepcopy(item['evidence'][0]),{'span_id':'private-id','interpretation':'private explanation'}]
    raw['judgments'].reverse()
    with pytest.raises(BusinessError) as caught:
        validate_and_score(raw,spans,criteria,policy,False)
    error=caught.value
    assert error.code=='EVIDENCE_SOURCE_INVALID'
    assert error.diagnostics=={'evidence_reason':'span_missing','evidence_index':2,
        'validation_kind':'score_evidence','criterion_index':index,'criterion_dimension':'skills'}
    assert 'private' not in str(error.diagnostics)+error.message
