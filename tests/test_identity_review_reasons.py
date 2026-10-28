from copy import deepcopy
from types import SimpleNamespace

import pytest

from hr_assist.identities import Identities
from hr_assist.storage import BusinessError


UNRESOLVED_MESSAGE='姓名或本次应聘岗位尚不明确，已保存模型依据；请到“姓名与岗位核对”确认，不会按猜测岗位评分。'
HISTORY_MESSAGE='已生成姓名与岗位核对建议；该申请有历史评分，请在核对页面确认后再处理。'


def resolve_fixture(monkeypatch, *, name_status='identified', role_status='identified',
                    human_name=False, human_role=False, history=False, name_value='Synthetic Person'):
    app={'id':'synthetic-application','file_id':'synthetic-file','name':'Confirmed Synthetic Person',
         'role_id':'synthetic-role' if human_role else None,'assessment_id':None,
         'original_name':'synthetic-attachment.pdf','status':'QUEUED'}
    proposal={'id':'synthetic-proposal','result':{
        'name':{'status':name_status,'value':name_value,'note':'Synthetic source detail'},
        'role':{'status':role_status,'role_id':'synthetic-role','value':'Synthetic Role'}}}
    writes=[]
    def get(key, default=None):
        return {'role_id':app['role_id']} if key=='candidate_role_review:'+app['id'] and human_role else default
    def one(sql, args):
        assert sql=='SELECT 1 FROM assessments WHERE application_id=? LIMIT 1'
        assert args==(app['id'],)
        return {'1':1} if history else None
    db=SimpleNamespace(get=get,one=one,execute=lambda sql,args:writes.append((sql,args)))
    identities=Identities(SimpleNamespace(db=db,name_is_confirmed=lambda app:human_name))
    analyzed=[]
    def analyze(app, reserve):
        analyzed.append(app['id'])
        return proposal
    monkeypatch.setattr(identities,'analyze',analyze)
    def forbidden(*args,**kwargs):
        pytest.fail('A review-required identity must never be applied or invoke a model.')
    monkeypatch.setattr(identities,'apply',forbidden)
    return identities,app,proposal,writes,analyzed,forbidden


@pytest.mark.parametrize('name_status,role_status,history,reason',[
    ('uncertain','identified',False,'name_unresolved'),
    ('identified','uncertain',False,'role_unresolved'),
    ('uncertain','uncertain',False,'name_and_role_unresolved'),
    ('identified','identified',True,'history_confirmation'),
])
def test_review_reason_keeps_business_error_and_never_discloses_identity(monkeypatch,name_status,role_status,history,reason):
    identities,app,proposal,writes,analyzed,reserve=resolve_fixture(monkeypatch,name_status=name_status,role_status=role_status,history=history)
    before_app,before_proposal=deepcopy(app),deepcopy(proposal)
    with pytest.raises(BusinessError) as caught:
        identities.resolve(app,reserve)
    error=caught.value
    assert error.code=='IDENTITY_REVIEW_REQUIRED' and error.status==400
    assert error.message==(HISTORY_MESSAGE if history else UNRESOLVED_MESSAGE)
    assert error.diagnostics=={'identity_reason':reason}
    assert analyzed==[app['id']]
    assert app==before_app and proposal==before_proposal
    assert writes==([] if history else [("UPDATE applications SET status='WAITING_IDENTITY' WHERE id=?",(app['id'],))])


@pytest.mark.parametrize('human_name,human_role,reason',[
    (True,False,'role_unresolved'),
    (False,True,'name_unresolved'),
])
def test_reason_uses_effective_human_confirmed_values(monkeypatch,human_name,human_role,reason):
    identities,app,_,_,_,reserve=resolve_fixture(monkeypatch,name_status='uncertain',role_status='uncertain',human_name=human_name,human_role=human_role)
    with pytest.raises(BusinessError) as caught:
        identities.resolve(app,reserve)
    assert caught.value.diagnostics=={'identity_reason':reason}
    assert caught.value.message==UNRESOLVED_MESSAGE


def test_identified_label_with_empty_name_still_reports_name_unresolved(monkeypatch):
    identities,app,_,_,_,reserve=resolve_fixture(monkeypatch,name_value='')
    with pytest.raises(BusinessError) as caught:
        identities.resolve(app,reserve)
    assert caught.value.diagnostics=={'identity_reason':'name_unresolved'}


def test_confirmed_identity_still_short_circuits_without_new_review(monkeypatch):
    identities,app,_,writes,analyzed,reserve=resolve_fixture(monkeypatch,human_name=True,human_role=True)
    assert identities.resolve(app,reserve) is app
    assert writes==[] and analyzed==[]
