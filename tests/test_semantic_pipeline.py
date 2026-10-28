from copy import deepcopy
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from hr_assist.diagnostics import Diagnostics, emit
from hr_assist.semantic import SemanticReference, bind_references, two_pass
from hr_assist.storage import BusinessError


class DraftStore:
    def __init__(self):self.rows={}
    def get(self,key):return deepcopy(self.rows.get(key))
    def put(self,key,value):self.rows[key]=deepcopy(value)


class Model:
    settings={'model':'synthetic','endpoint':'https://example.invalid/chat'}
    def __init__(self):self.calls=[];self.fail_review=False
    def complete(self,system,payload):
        self.calls.append((system,deepcopy(payload)))
        emit('MODEL_REQUEST_STARTED',request_bytes=100)
        if payload['task_stage']=='semantic_review':
            if self.fail_review:raise BusinessError('MODEL_TIMEOUT','synthetic timeout')
            return {'finding':'reviewed'}, {'total_tokens':20,'prompt_tokens':12,'completion_tokens':8}
        return {'finding':'initial'}, {'total_tokens':10,'prompt_tokens':6,'completion_tokens':4}


PAYLOAD={'sources':[{'id':'R1','text':'三年相关经验','page':1,'source':'text'}],
         'output_schema':{'type':'object'},'available_roles':[]}


def test_semantic_binding_accepts_meaning_without_copying_model_text_as_source():
    sources=deepcopy(PAYLOAD['sources']);before=deepcopy(sources)
    ref=SemanticReference(span_id='R1',interpretation='具备3年相关工作经验')
    result=bind_references([ref],sources)
    assert result==[{'span_id':'R1','interpretation':'具备3年相关工作经验','quote':'三年相关经验','page':1,'source':'text','semantic':True}]
    assert sources==before


def test_unknown_source_id_is_rejected_without_exposing_model_values():
    with pytest.raises(BusinessError) as error:
        bind_references([{'span_id':'private-model-id','interpretation':'private-description'}],PAYLOAD['sources'])
    assert error.value.code=='EVIDENCE_SOURCE_INVALID'
    assert 'private' not in str(error.value)+str(error.value.diagnostics)


def test_two_pass_review_receives_original_sources_and_first_result():
    model=Model();reserved=[];before=deepcopy(PAYLOAD)
    result,meta=two_pass(model,'synthetic rules',PAYLOAD,task='identity',reserve=lambda:reserved.append(True))
    assert result=={'finding':'reviewed'} and len(model.calls)==len(reserved)==2
    assert model.calls[1][1]['sources']==before['sources']
    assert model.calls[1][1]['analysis_result']=={'finding':'initial'}
    assert model.calls[1][1]['task_stage']=='semantic_review'
    assert meta['usage']['total_tokens']==30 and not meta['analysis_cached']
    assert meta['evidence_mode']=='model_semantic_review' and PAYLOAD==before


def test_review_timeout_continues_from_saved_analysis_without_repaying_first_call():
    db=DraftStore();model=Model();model.fail_review=True
    with pytest.raises(BusinessError) as error:
        two_pass(model,'rules',PAYLOAD,task='assessment',db=db)
    assert error.value.diagnostics['model_step']=='review'
    model.fail_review=False
    result,meta=two_pass(model,'rules',PAYLOAD,task='assessment',db=db)
    assert [p['task_stage'] for _,p in model.calls]==['analysis','semantic_review','semantic_review']
    assert meta['analysis_cached'] and meta['usage']['total_tokens']==20
    assert meta['analysis_usage']['total_tokens']==10


def test_one_call_budget_saves_first_stage_and_next_batch_can_review():
    db=DraftStore();model=Model();count=0
    def reserve():
        nonlocal count
        count+=1
        if count>1:raise BusinessError('MODEL_BUDGET','synthetic budget')
    with pytest.raises(BusinessError) as error:
        two_pass(model,'rules',PAYLOAD,task='assessment',db=db,reserve=reserve)
    assert error.value.code=='MODEL_BUDGET' and error.value.diagnostics['model_step']=='review'
    assert len(model.calls)==1
    result,meta=two_pass(model,'rules',PAYLOAD,task='assessment',db=db)
    assert meta['analysis_cached'] and len(model.calls)==2


def test_force_analysis_failure_cannot_silently_resume_an_older_draft():
    class RefreshModel(Model):
        fail_analysis=False
        def complete(self,system,payload):
            if self.fail_analysis and payload['task_stage']=='analysis':
                self.calls.append((system,deepcopy(payload)))
                raise BusinessError('MODEL_TIMEOUT','synthetic analysis timeout')
            return super().complete(system,payload)
    db=DraftStore();model=RefreshModel()
    two_pass(model,'rules',PAYLOAD,task='identity',db=db)
    model.fail_analysis=True
    with pytest.raises(BusinessError):
        two_pass(model,'rules',PAYLOAD,task='identity',db=db,force=True)
    model.fail_analysis=False
    result,meta=two_pass(model,'rules',PAYLOAD,task='identity',db=db)
    assert [p['task_stage'] for _,p in model.calls]==['analysis','semantic_review','analysis','analysis','semantic_review']
    assert not meta['analysis_cached']


@pytest.mark.parametrize('change',['source','schema','system','model','profile','force'])
def test_source_contract_model_generation_or_force_cannot_reuse_stale_draft(change):
    db=DraftStore();model=Model()
    two_pass(model,'rules',PAYLOAD,task='identity',db=db)
    payload=deepcopy(PAYLOAD);system='rules';force=False
    if change=='source':payload['sources'][0]['text']='另一条资料'
    if change=='schema':payload['output_schema']['title']='changed'
    if change=='system':system='changed'
    if change=='model':model.settings={**model.settings,'model':'different'}
    if change=='profile':
        model.settings={'model':'glm-5.3','endpoint':'https://open.bigmodel.cn/api/paas/v4/chat/completions','reasoning_effort':'low'}
    if change=='force':force=True
    result,meta=two_pass(model,system,payload,task='identity',db=db,force=force)
    assert not meta['analysis_cached'] and len(model.calls)==4


def test_diagnostics_label_both_calls_and_cache_reuse_without_source_text(tmp_path):
    log=Diagnostics(tmp_path,lambda:{'diagnostics':{'enabled':True}})
    db=DraftStore();model=Model()
    with log.operation('identity'):
        two_pass(model,'rules',PAYLOAD,task='identity',db=db)
        two_pass(model,'rules',PAYLOAD,task='identity',db=db)
    day=datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
    rows=log.read(day)
    assert [r['model_step'] for r in rows if r['event']=='MODEL_REQUEST_STARTED']==['analysis','review','review']
    assert any(r['event']=='SEMANTIC_DRAFT_REUSED' for r in rows)
    assert '三年' not in log.export(day).decode()
