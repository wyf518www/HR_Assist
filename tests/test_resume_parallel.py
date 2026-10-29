"""Synthetic requests exercise real two-pass scheduling, never external models."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
import threading
from zoneinfo import ZoneInfo

import pytest

from conftest import make_pdf, run
from test_core import FakeModel, configure
from hr_assist.db import uid
from hr_assist.parallel import BatchControl, BatchStopped
from hr_assist.schemas import CandidateNameReview
from hr_assist.storage import BusinessError


def candidates(service,role,count,*,confirmed=True):
    rows=[]
    for index in range(count):
        row=service.ingest(make_pdf(f'Synthetic applicant number {index}. Python engineer with 5 years experience.'),f'candidate-{index}.pdf',role['id'] if confirmed else None)
        if confirmed:
            service.review_candidate_name(row['id'],CandidateNameReview(name='Test',expected_name='待识别',confirmed=True))
        rows.append(row)
    return rows


def options(service,concurrency=1,maximum=100):
    configure(service)
    settings=service.settings()
    settings['llm'].update(resume_concurrency=concurrency,max_calls_per_run=maximum)
    service.db.put('settings',settings)


def join(service,record):
    service.worker.join(20)
    assert not service.worker.is_alive()
    assert not service.run_lock.locked()
    return service.db.one('SELECT * FROM runs WHERE id=?',(record['id'],))


def watch_stop(monkeypatch):
    stopped=threading.Event()
    original=BatchControl.stop
    def stop(control,error,phase=None):
        original(control,error,phase)
        stopped.set()
    monkeypatch.setattr(BatchControl,'stop',stop)
    return stopped


class OverlapModel(FakeModel):
    def __init__(self,width):
        self.gate=threading.Barrier(width)
        self.lock=threading.Lock()
        self.requests=[]
        self.active=0
        self.peak=0
        self.active_keys=set()
        self.width=width

    def complete(self,system,payload):
        sources=payload.get('resume_spans',payload.get('sources'))
        key=tuple((item['id'],item['text']) for item in sources)
        with self.lock:
            assert key not in self.active_keys, 'one candidate must never analyze and review concurrently'
            self.active_keys.add(key)
            self.active+=1
            self.peak=max(self.peak,self.active)
            self.requests.append((key,payload['task_stage']))
            first=len(self.requests)<=self.width
        try:
            if first:
                self.gate.wait(10)
            return super().complete(system,payload)
        finally:
            with self.lock:
                self.active-=1
                self.active_keys.remove(key)


@pytest.mark.parametrize('width',[1,3])
def test_real_overlap_is_bounded_and_each_candidate_is_sequential(service,role,monkeypatch,width):
    options(service,width)
    rows=candidates(service,role,6)
    model=OverlapModel(width)
    monkeypatch.setattr(service,'model',lambda:model)
    result=run(service)
    assert result['status']=='SUCCESS'
    stats=json.loads(result['stats'])
    assert stats['assessed']==6 and stats['model_calls']==12
    assert stats['resume_concurrency']==width and model.peak==width
    grouped={key:[stage for source,stage in model.requests if source==key] for key,_ in model.requests}
    assert len(grouped)==6
    assert all(stages==['analysis','semantic_review'] for stages in grouped.values())
    assert all(service.get_application(row['id'])['assessment_id'] for row in rows)
    records=service.diagnostics.read(datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat(),run_id=result['id'],limit=1000)
    phase_records=[r for r in records if r.get('phase')=='assessment']
    traces={row['id']:set(r['trace_id'] for r in phase_records if r.get('application_id')==row['id']) for row in rows}
    assert all(len(value)==1 for value in traces.values())
    assert len(set.union(*traces.values()))==6


def test_atomic_reservations_never_exceed_shared_budget():
    control=BatchControl(37,10,threading.Event())
    gate=threading.Barrier(10)
    def reserve():
        gate.wait(10)
        successes=0
        for _ in range(50):
            try:
                control.reserve()
                successes+=1
            except (BatchStopped,BusinessError):
                break
        return successes
    with ThreadPoolExecutor(max_workers=10) as pool:
        assert sum(pool.map(lambda _:reserve(),range(10)))==37
    assert control.calls==37 and control.error.code=='MODEL_BUDGET'


def test_parallel_budget_preserves_analysis_drafts_and_completed_scores(service,role,monkeypatch):
    options(service,4,5)
    candidates(service,role,8)
    model=OverlapModel(4)
    monkeypatch.setattr(service,'model',lambda:model)
    result=run(service)
    assert result['status']=='BLOCKED'
    stats=json.loads(result['stats'])
    assert stats['model_calls']==5 and len(model.requests)==5
    assert stats['assessed']==1 and stats['failed_phase']=='assessment'
    assert stats['exceptions']==1
    assert len(service.db.all("SELECT key FROM meta WHERE key LIKE 'semantic_draft:%'"))==4
    assert len(service.db.all('SELECT id FROM assessments'))==1


def test_rate_limit_stops_queue_but_joins_inflight_review_before_reports(service,role,monkeypatch):
    options(service,2)
    candidates(service,role,6)
    stopped=watch_stop(monkeypatch)
    review_gate=threading.Barrier(2)
    survivor_waiting=threading.Event()
    release=threading.Event()
    lock=threading.Lock()
    requests=[]
    class Model(FakeModel):
        survivor=None
        def complete(self,system,payload):
            key=payload['resume_spans'][0]['text']
            with lock:
                requests.append((key,payload['task_stage']))
                if self.survivor is None:self.survivor=key
            if payload['task_stage']=='semantic_review':
                review_gate.wait(10)
                if key==self.survivor:
                    survivor_waiting.set()
                    assert release.wait(10)
                else:
                    raise BusinessError('MODEL_RATE_LIMIT','Synthetic 429',429)
            return super().complete(system,payload)
    monkeypatch.setattr(service,'model',lambda:model)
    model=Model()
    exports=[]
    from hr_assist import service as module
    original=module.role_reports
    def reports(*args):
        assert release.is_set()
        assert len(service.db.all('SELECT id FROM assessments'))==1
        exports.append(True)
        return original(*args)
    monkeypatch.setattr(module,'role_reports',reports)
    record=service.start_run('pending',uid())
    try:
        assert survivor_waiting.wait(10) and stopped.wait(10)
        assert service.run_lock.locked() and service.worker.is_alive()
        assert service.db.one('SELECT status FROM runs WHERE id=?',(record['id'],))['status']=='RUNNING'
        assert not exports and len(requests)==4
    finally:
        release.set()
    result=join(service,record)
    stats=json.loads(result['stats'])
    assert result['status']=='BLOCKED' and 'Synthetic 429' in result['error']
    assert exports==[True] and stats['assessed']==1 and stats['model_calls']==4
    assert stats['failed_phase']=='assessment' and stats['exceptions']==1
    assert len(requests)==4


@pytest.mark.parametrize('reason',['rate_limit','shutdown'])
def test_stop_prevents_review_after_inflight_analysis_and_preserves_draft(service,role,monkeypatch,reason):
    options(service,2)
    candidates(service,role,5)
    stopped=watch_stop(monkeypatch)
    gate=threading.Barrier(3)
    release=threading.Event()
    lock=threading.Lock()
    calls=[]
    class Model(FakeModel):
        def complete(self,system,payload):
            with lock:
                calls.append(payload['task_stage'])
                number=len(calls)
            assert payload['task_stage']=='analysis'
            gate.wait(10)
            if reason=='rate_limit' and number==2:
                raise BusinessError('MODEL_RATE_LIMIT','Synthetic 429',429)
            assert release.wait(10)
            return super().complete(system,payload)
    model=Model()
    monkeypatch.setattr(service,'model',lambda:model)
    record=service.start_run('pending',uid())
    try:
        gate.wait(10)
        if reason=='shutdown':service.stop_event.set()
        else:assert stopped.wait(10)
        assert service.worker.is_alive() and service.run_lock.locked()
    finally:
        release.set()
    result=join(service,record)
    assert result['status']=='BLOCKED'
    stats=json.loads(result['stats'])
    assert stats['model_calls']==2 and stats['assessed']==0 and calls==['analysis','analysis']
    assert len(service.db.all("SELECT key FROM meta WHERE key LIKE 'semantic_draft:%'"))==(1 if reason=='rate_limit' else 2)


def test_identity_review_batches_share_concurrency_and_budget(service,role,monkeypatch):
    options(service,2,3)
    rows=candidates(service,role,5,confirmed=False)
    model=OverlapModel(2)
    monkeypatch.setattr(service,'model',lambda:model)
    result=run(service,'identity')
    stats=json.loads(result['stats'])
    assert result['status']=='BLOCKED' and stats['model_calls']==3
    assert stats['identity_analyzed']==1 and model.peak==2
    assert len([row for row in rows if service.identities.proposal(row['id'])])==1
    assert all(service.get_application(row['id'])['name']=='待识别' for row in rows)


def test_batch_settings_are_frozen_and_queue_is_not_eager(service,role,monkeypatch):
    options(service,2,20)
    candidates(service,role,6)
    gate=threading.Barrier(3)
    release=threading.Event()
    lock=threading.Lock()
    requests=[]
    class Model(FakeModel):
        def complete(self,system,payload):
            with lock:
                requests.append(payload['task_stage'])
                first=len(requests)<=2
            if first:
                gate.wait(10)
                assert release.wait(10)
            return super().complete(system,payload)
    model=Model()
    monkeypatch.setattr(service,'model',lambda:model)
    record=service.start_run('pending',uid())
    try:
        gate.wait(10)
        records=service.diagnostics.read(datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat(),run_id=record['id'],limit=1000)
        assert len([r for r in records if r['event']=='PHASE_STARTED' and r.get('phase')=='identity'])==2
        assert len(requests)==2
        settings=service.settings()
        settings['llm'].update(resume_concurrency=5,max_calls_per_run=1)
        service.db.put('settings',settings)
    finally:
        release.set()
    result=join(service,record)
    stats=json.loads(result['stats'])
    assert result['status']=='SUCCESS'
    assert stats['resume_concurrency']==2 and stats['model_calls']==12 and stats['assessed']==6
    row=service.ingest(make_pdf('Synthetic next batch Python engineer with 5 years experience.'),'next.pdf',role['id'])
    service.review_candidate_name(row['id'],CandidateNameReview(name='Test',expected_name='待识别',confirmed=True))
    second=run(service)
    stats=json.loads(second['stats'])
    assert second['status']=='BLOCKED'
    assert stats['resume_concurrency']==5 and stats['model_calls']==1 and stats['assessed']==0


def test_mail_batch_freezes_limits_before_fetching_messages(service,role,monkeypatch):
    options(service,2,20)
    candidates(service,role,3)
    fetching=threading.Event()
    release=threading.Event()
    class Inbox:
        uid_validity='test'
        def __init__(self,*args):pass
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def messages(self,*args):
            fetching.set()
            assert release.wait(10)
            return iter(())
    model=FakeModel()
    monkeypatch.setattr('hr_assist.service.Inbox',Inbox)
    monkeypatch.setattr(service,'model',lambda:model)
    record=service.start_run('mail',uid())
    try:
        assert fetching.wait(10)
        settings=service.settings()
        settings['llm'].update(resume_concurrency=1,max_calls_per_run=1)
        service.db.put('settings',settings)
    finally:
        release.set()
    result=join(service,record)
    stats=json.loads(result['stats'])
    assert result['status']=='SUCCESS'
    assert stats['resume_concurrency']==2 and stats['model_calls']==6 and stats['assessed']==3
