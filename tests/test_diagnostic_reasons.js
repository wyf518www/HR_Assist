'use strict';
const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const escape=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const context=vm.createContext({E:escape,stamp:escape,URLSearchParams});
vm.runInContext(fs.readFileSync(path.join(__dirname,'..','hr_assist','web','diagnostics.js'),'utf8'),context);
function render(row){context.row=row;return vm.runInContext('diagnosticRecord(row)',context);}
const base={timestamp:'2026-10-28T20:00:00+08:00',phase:'identity',application_id:'a'.repeat(32),error_code:'IDENTITY_REVIEW_REQUIRED'};

test('new and historical identity waits are presented as human review, without failure styling',()=>{
  for(const event of ['PHASE_WAITING','PHASE_FAILED']){
    const html=render({...base,event,identity_reason:'role_unresolved'});
    assert.match(html,/等待人工核对/);
    assert.match(html,/应聘岗位尚不能确认或未对应启用岗位/);
    assert.match(html,/候选人库 → 姓名与岗位核对/);
    assert.match(html,/确认修正所选/);
    assert.match(html,/重新分析所选/);
    assert.doesNotMatch(html,/class="diagnostic-failed"/);
    assert.doesNotMatch(html,/class="error-message"/);
  }
});

test('actual failures keep failure styling and show evidence reason, item and dimension',()=>{
  const html=render({timestamp:base.timestamp,phase:'assessment',event:'PHASE_FAILED',error_code:'EVIDENCE_INVALID',validation_kind:'score_evidence',evidence_reason:'no_match',criterion_dimension:'skills',criterion_index:5,evidence_index:2,quote_characters:18});
  assert.match(html,/class="diagnostic-failed"/);
  assert.match(html,/评分引文 · 评分维度：核心技能/);
  assert.match(html,/找不到相同原文/);
  assert.match(html,/评分项序号：<b>5/);
  assert.match(html,/引文序号：<b>2/);
});

test('fact failures identify only fixed field labels, without a claimed candidate value',()=>{
  const html=render({timestamp:base.timestamp,phase:'assessment',event:'PHASE_FAILED',error_code:'FACT_EVIDENCE_INVALID',validation_kind:'fact_evidence',evidence_reason:'ambiguous_match',fact_field:'institution',fact_index:3});
  assert.match(html,/事实引文 · 事实字段：毕业院校/);
  assert.match(html,/多处匹配，无法唯一定位/);
  assert.match(html,/事实项序号：<b>3/);
});

test('old evidence logs clearly state that the precise cause is unavailable',()=>{
  for(const error_code of ['EVIDENCE_INVALID','FACT_EVIDENCE_INVALID','FACT_VALUE_MISMATCH']){
    const html=render({timestamp:base.timestamp,phase:'assessment',event:'PHASE_FAILED',error_code});
    assert.match(html,/未包含具体条目和匹配原因/);
    assert.match(html,/无法仅凭该记录判断失败原因/);
    assert.doesNotMatch(html,/引用的原文片段不存在|忽略允许的排版空白后出现多处匹配/);
  }
});

test('mapped explanations reject unrecognized values and all raw metadata is escaped',()=>{
  const injected='<img src=x onerror=alert(1)>';
  context.unknown={event:'PHASE_FAILED',phase:'assessment',error_code:'EVIDENCE_INVALID',validation_kind:injected,evidence_reason:'__proto__',fact_field:injected};
  const explanation=vm.runInContext('diagnosticExplanation(unknown,false)',context);
  assert.match(explanation,/未包含具体条目/);
  assert.doesNotMatch(explanation,/<img|__proto__/);
  const html=render({...context.unknown,timestamp:base.timestamp});
  assert.doesNotMatch(html,/<img|<script/);
  assert.match(html,/&lt;img/);
});
