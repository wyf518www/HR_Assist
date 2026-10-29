"""Typed education policy and two-pass semantic assessment use synthetic data only."""
from copy import deepcopy
import pytest
from hr_assist.assessment import assess, education_catalog, validate_and_score
from hr_assist.db import dump
from hr_assist.storage import BusinessError
from test_assessment import sample


def test_catalog_ids_are_stable_and_server_resolves_name():
    raw,spans,criteria,policy=sample()
    before=education_catalog(policy)
    policy['schools']=['其他院校','测试大学','测试大学']
    after=education_catalog(policy)
    assert next(item for item in after if item['name']=='测试大学')==before[0]
    assert len(after)==2
    result=validate_and_score(raw,spans,criteria,policy,False)
    assert result['education']['catalog_name']=='测试大学'


@pytest.mark.parametrize('degree,completion,admission,match,master_bypass,gate',[
    ('bachelor','attained','national_regular','catalog',True,'pass'),
    ('bachelor','attained','non_regular','catalog',True,'fail'),
    ('bachelor','attained','unknown','catalog',True,'unknown'),
    ('bachelor','attained','national_regular','not_in_catalog',True,'fail'),
    ('bachelor','attained','national_regular','unknown',True,'unknown'),
    ('bachelor','in_progress','national_regular','catalog',True,'unknown'),
    ('master','attained','unknown','unknown',True,'pass'),
    ('master','attained','unknown','unknown',False,'unknown'),
    ('master','in_progress','national_regular','catalog',True,'unknown'),
    ('master','unknown','national_regular','catalog',True,'unknown'),
    ('doctor','attained','national_regular','catalog',True,'unknown'),
    ('other','attained','national_regular','catalog',True,'unknown'),
    ('unknown','attained','national_regular','catalog',True,'unknown'),
])
def test_typed_policy_covers_only_confirmed_policy(degree,completion,admission,match,master_bypass,gate):
    raw,spans,criteria,policy=sample()
    policy['master_bypass']=master_bypass
    raw['education'].update(degree=degree,completion=completion,admission=admission,institution_match=match,
                            catalog_id=education_catalog(policy)[0]['id'] if match=='catalog' else None)
    result=validate_and_score(raw,spans,criteria,policy,False)
    assert result['education_gate']==gate
    assert result['grade']==('强烈推荐' if gate=='pass' else '待定')


@pytest.mark.parametrize('education',[None,False])
def test_absent_education_never_defaults_to_passing_non_sales(education):
    raw,spans,criteria,policy=sample()
    if education is False:raw.pop('education')
    else:raw['education']=None
    assert validate_and_score(raw,spans,criteria,policy,False)['education_gate']=='unknown'
    assert validate_and_score(raw,spans,criteria,policy,True)['education_gate']=='pass'


def test_education_without_sources_cannot_pass_even_with_valid_typed_fields():
    raw,spans,criteria,policy=sample()
    raw['education']['evidence']=[]
    assert validate_and_score(raw,spans,criteria,policy,False)['education_gate']=='unknown'


def test_free_text_facts_cannot_bypass_typed_policy():
    raw,spans,criteria,policy=sample()
    raw['facts'][0]['value']='硕士及以上名校学历'
    raw['facts'][2]['value']='某测试大学独立学院'
    raw['education'].update(admission='non_regular',institution_match='not_in_catalog',catalog_id=None)
    assert validate_and_score(raw,spans,criteria,policy,False)['education_gate']=='fail'


@pytest.mark.parametrize('match,catalog_id',[('catalog','private-unlisted-id'),('catalog',None),('unknown','private-unlisted-id')])
def test_catalog_requires_a_real_id_and_consistent_status(match,catalog_id):
    raw,spans,criteria,policy=sample()
    raw['education'].update(institution_match=match,catalog_id=catalog_id)
    with pytest.raises(BusinessError) as caught:validate_and_score(raw,spans,criteria,policy,False)
    assert caught.value.code=='EDUCATION_CATALOG_INVALID'
    assert 'private-' not in caught.value.message+str(caught.value.diagnostics)


def test_ocr_education_keeps_review_gate_even_for_attained_master():
    raw,spans,criteria,policy=sample()
    spans.append({'id':'R2','source':'ocr','page':2,'text':'已取得硕士学位'})
    raw['education'].update(degree='master',evidence=[{'span_id':'R2','interpretation':'识别到已取得硕士，需核验OCR'}])
    result=validate_and_score(raw,spans,criteria,policy,False)
    assert result['education_gate']=='unknown' and result['provisional']


def test_education_source_id_is_checked():
    raw,spans,criteria,policy=sample()
    raw['education']['evidence'][0]['span_id']='missing-source'
    with pytest.raises(BusinessError) as caught:validate_and_score(raw,spans,criteria,policy,False)
    assert caught.value.code=='EVIDENCE_SOURCE_INVALID'


def test_global_criteria_cannot_be_waived_by_requirement_mode():
    raw,spans,criteria,policy=sample()
    next(j for j in raw['judgments'] if j['criterion_id']=='achievements')['requirement_mode']='unrestricted'
    with pytest.raises(BusinessError) as caught:validate_and_score(raw,spans,criteria,policy,False)
    assert caught.value.code=='MODEL_CRITERIA'


class PairedModel:
    settings={'model':'synthetic','endpoint':'https://example.invalid/chat','approved':True}
    def __init__(self,first,review):self.first=first;self.review=review;self.calls=[]
    def complete(self,system,payload):
        self.calls.append(deepcopy(payload))
        return deepcopy(self.review if payload['task_stage']=='semantic_review' else self.first),{'total_tokens':7}


def assess_args(raw,spans,criteria,policy):
    return {'name':'Synthetic Person','is_sales':False,'name_confirmed':True}, {
        'criteria':dump(criteria),'fields':dump(dict(education='本科',experience='三年',certificates='证书A',skills='Python',responsibilities='开发'))},spans,{**policy,'confirmed':True}


def test_only_second_pass_result_is_scored_and_sources_remain_whole():
    raw,spans,criteria,policy=sample()
    first=deepcopy(raw)
    first['judgments'][0]['evidence'][0]['span_id']='wrong-source'
    reviewed=deepcopy(raw)
    reviewed['judgments'][0].update(verdict='unknown',evidence=[],reason='复核认为条件未明确')
    model=PairedModel(first,reviewed)
    calls=[]
    result=assess(model,*assess_args(raw,spans,criteria,policy),reserve=lambda:calls.append(1))
    assert result['score']==95 and result['provisional']
    assert [p['task_stage'] for p in model.calls]==['analysis','semantic_review']
    assert len(calls)==2 and model.calls[1]['analysis_result']==first
    assert model.calls[0]['resume_spans']==spans==model.calls[1]['resume_spans']
    assert result['usage']=={'total_tokens':14} and not result['analysis_cached']
    assert result['evidence_mode']=='model_semantic_review' and result['prompt_version']=='v10-validation-feedback'


def test_review_failure_never_scores_unreviewed_first_result():
    raw,spans,criteria,policy=sample()
    class BrokenReview(PairedModel):
        def complete(self,system,payload):
            if payload['task_stage']=='semantic_review':raise BusinessError('MODEL_TIMEOUT','synthetic timeout')
            return super().complete(system,payload)
    with pytest.raises(BusinessError) as caught:
        assess(BrokenReview(raw,raw),*assess_args(raw,spans,criteria,policy))
    assert caught.value.code=='MODEL_TIMEOUT'
