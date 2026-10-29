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

test('identity schema errors show fixed field labels and error classes without model values',()=>{
  const html=render({timestamp:base.timestamp,phase:'identity',event:'PHASE_FAILED',error_code:'IDENTITY_SCHEMA',validation_kind:'identity_schema',identity_schema_field:'role.evidence.interpretation',identity_schema_error:'missing',identity_schema_error_count:2,identity_evidence_index:1,model_step:'review'});
  assert.match(html,/岗位依据解读：缺少必填字段/);
  assert.match(html,/格式问题数量：<b>2/);
  assert.match(html,/身份依据序号：<b>1/);
  assert.match(html,/第二步 · 语义复核/);
  assert.match(html,/原记录未因格式错误被覆盖/);
});

test('historical or unrecognized identity schema metadata does not invent a cause',()=>{
  for(const metadata of [{},{identity_schema_field:'<private>',identity_schema_error:'__proto__'}]){
    context.oldIdentity={error_code:'IDENTITY_SCHEMA',...metadata};
    const html=vm.runInContext('diagnosticExplanation(oldIdentity,false)',context);
    assert.match(html,/无法仅凭该记录判断/);
    assert.doesNotMatch(html,/<private>|__proto__|缺少必填字段/);
  }
});

test('duplicate application explanation keeps manual reconciliation explicit',()=>{
  const html=render({timestamp:base.timestamp,phase:'identity',event:'PHASE_FAILED',error_code:'DUPLICATE_APPLICATION'});
  assert.match(html,/同一简历原件已有目标岗位申请/);
  assert.match(html,/评分和人工跟进/);
  assert.match(html,/不会自动合并或删除/);
});

test('batch stop is a neutral pause rather than another model failure or identity review',()=>{
  for(const event of ['PHASE_WAITING','PHASE_FAILED']){
    const html=render({timestamp:base.timestamp,phase:'assessment',event,error_code:'BATCH_STOPPED',application_id:'b'.repeat(32),model_step:'review'});
    assert.match(html,/随批次暂停/);
    assert.match(html,/本候选人的后续请求未发送，已完成步骤保留/);
    assert.match(html,/不新增模型调用失败或业务异常/);
    assert.match(html,/处理该批次的首个暂停原因/);
    assert.doesNotMatch(html,/class="diagnostic-failed"|class="error-message"/);
    assert.doesNotMatch(html,/等待人工核对|候选人库 → 姓名与岗位核对/);
  }
});

test('score schema failures explain fixed field classes and model output array positions',()=>{
  const html=render({...base,phase:'assessment',event:'PHASE_FAILED',error_code:'MODEL_SCHEMA',
    score_schema_field:'judgments.evidence.interpretation',score_schema_error:'missing',score_schema_error_count:2,
    criterion_index:4,evidence_index:2,model_step:'review'});
  assert.match(html,/评分依据解读：缺少必填字段/);
  assert.match(html,/模型输出中的评分项第 4 项，依据项第 2 项（从1开始）/);
  assert.match(html,/评分格式问题数量/);
  assert.match(html,/未采用格式无效的评分，已有历史评分和人工跟进保留/);
  assert.match(html,/第二步 · 语义复核/);
  assert.match(html,/class="diagnostic-failed"/);
  assert.doesNotMatch(html,/逐字匹配|简历原文不符/);
});

test('legacy and unrecognized score schema diagnostics do not invent a missing field',()=>{
  for(const fields of [{},{score_schema_field:'facts.private-field',score_schema_error:'private-value'}]){
    const html=render({...base,phase:'assessment',event:'PHASE_FAILED',error_code:'MODEL_SCHEMA',...fields});
    assert.match(html,/此记录未包含具体评分格式字段/);
    assert.match(html,/运行记录 → 处理日志/);
    const explanation=html.split('<details')[0];
    assert.doesNotMatch(explanation,/private-|评分依据解读：缺少必填字段/);
  }
  const html=render({...base,phase:'assessment',event:'PHASE_FAILED',error_code:'MODEL_SCHEMA',
    score_schema_field:'facts.evidence',score_schema_error:'invalid_type',fact_index:'<script>unsafe</script>',evidence_index:0});
  assert.match(html,/事实依据对象：字段类型不符合要求/);
  assert.doesNotMatch(html,/<script>|模型输出中的/);
});

test('education catalog errors describe model IDs without rejecting a candidate school',()=>{
  for(const [reason,message] of [
    ['catalog_id_missing','模型标记为名录内院校，但未给出对应名录编号'],
    ['catalog_id_unknown','模型返回的院校编号不属于本次提供的已确认名录'],
    ['status_id_conflict','模型给出的院校对应状态与名录编号相互矛盾'],
  ]){
    const html=render({...base,phase:'assessment',event:'PHASE_FAILED',error_code:'EDUCATION_CATALOG_INVALID',education_catalog_reason:reason});
    assert.ok(html.includes(message));
    assert.match(html,/不能据此认定候选人的院校不在名单或学历不符/);
    assert.match(html,/未生成本次评分/);
  }
  const old=render({...base,phase:'assessment',event:'PHASE_FAILED',error_code:'EDUCATION_CATALOG_INVALID'});
  assert.match(old,/此记录未包含具体院校编号错误原因/);
});
