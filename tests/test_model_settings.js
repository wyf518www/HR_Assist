'use strict';
const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../hr_assist/web/model-settings.js'),'utf8');
function harness(){
  const context=vm.createContext({URL,fetch(){throw new Error('No network from preset');},
    liveField:(label,name,value,type='text',attributes='')=>`<label>${label}<input name="${name}" value="${value}" type="${type}" ${attributes}></label>`,
    btn:(label,action)=>`<button type="button" data-action="${action}">${label}</button>`});
  vm.runInContext(source,context);
  const form={elements:Object.fromEntries(Object.entries({llm_endpoint:'https://open.bigmodel.cn/api/paas/v4/chat/completions',llm_model:'glm-5.3',llm_reasoning_effort:'default',llm_max_tokens:'',resume_concurrency:'4'}).map(([k,value])=>[k,{value}]))};
  return {context,form,run:s=>vm.runInContext(s,context)};
}
test('light preset changes only the two generation controls and does not call any service',()=>{
  const {context,form,run}=harness();context.form=form;
  run('applyModelGenerationPreset(form,true)');
  assert.equal(form.elements.llm_reasoning_effort.value,'low');
  assert.equal(form.elements.llm_max_tokens.value,'32768');
  assert.equal(form.elements.llm_model.value,'glm-5.3');
  assert.equal(form.elements.resume_concurrency.value,'4');
  run('applyModelGenerationPreset(form,false)');
  assert.equal(form.elements.llm_reasoning_effort.value,'default');
  assert.equal(form.elements.llm_max_tokens.value,'');
  assert.equal(form.elements.resume_concurrency.value,'4');
});
test('preset refuses unsupported providers without changing controls, but default reset still works',()=>{
  const {context,form,run}=harness();context.form=form;
  form.elements.llm_endpoint.value='https://open.bigmodel.cn.evil.invalid/api/paas/v4/chat/completions';
  form.elements.llm_reasoning_effort.value='high';
  form.elements.llm_max_tokens.value='65536';
  assert.throws(()=>run('applyModelGenerationPreset(form,true)'),/标准官方接口/);
  assert.equal(form.elements.llm_reasoning_effort.value,'high');
  assert.equal(form.elements.llm_max_tokens.value,'65536');
  run('applyModelGenerationPreset(form,false)');
  assert.equal(form.elements.llm_reasoning_effort.value,'default');
});
test('numeric settings serialize as numbers and clearing a limit sends explicit null',()=>{
  const {context,run}=harness();
  context.fd=new Map([['llm_reasoning_effort','low'],['llm_max_tokens','32768']]);
  assert.deepEqual(JSON.parse(run('JSON.stringify(readModelGenerationSettings(fd))')),{llm_reasoning_effort:'low',llm_max_tokens:32768});
  context.fd.set('llm_max_tokens',' ');
  assert.equal(run('readModelGenerationSettings(fd).llm_max_tokens'),null);
});
test('old settings render provider default and unbounded custom field, not an implicitly enabled preset',()=>{
  const {run}=harness();
  const html=run('modelGenerationSettings({})');
  assert.match(html,/<option value="default" selected>/);
  assert.match(html,/<input name="llm_max_tokens" value="" type="number"/);
  assert.doesNotMatch(html,/<option value="low" selected>/);
  assert.match(html,/不能关闭/);
});
test('concurrency renders the default and saved value with bounds and task scope',()=>{
  const {run}=harness();
  const old=run('modelGenerationSettings({})');
  assert.match(old,/同时处理简历数<input name="resume_concurrency" value="1" type="number" required min="1" max="10" step="1"/);
  assert.match(old,/1 为串行，建议先设为 2/);
  assert.match(old,/模型和账号额度/);
  assert.match(old,/https:\/\/docs\.bigmodel\.cn\/cn\/api\/rate-limit/);
  assert.match(old,/下一批收件、待处理简历或姓名岗位核对任务生效/);
  assert.match(old,/每份仍依次分析、复核/);
  assert.match(old,/JD 任务不自动并发/);
  assert.match(old,/共用每批调用上限/);
  assert.match(old,/429 或超时会停止新调用/);
  assert.match(old,/已发出的请求可能继续完成，不自动重试/);
  const saved=run('modelGenerationSettings({resume_concurrency:10})');
  assert.match(saved,/name="resume_concurrency" value="10"/);
});
test('saving concurrency serializes an integer while an older page omits the field',()=>{
  const {context,run}=harness();
  context.fd=new Map([['llm_reasoning_effort','high'],['llm_max_tokens','65536']]);
  assert.equal(run('Object.hasOwn(readModelGenerationSettings(fd),"resume_concurrency")'),false);
  for(const value of ['1','2','10']){
    context.fd.set('resume_concurrency',value);
    const settings=JSON.parse(run('JSON.stringify(readModelGenerationSettings(fd))'));
    assert.equal(settings.resume_concurrency,Number(value));
    assert.equal(typeof settings.resume_concurrency,'number');
    assert.equal(settings.llm_reasoning_effort,'high');
    assert.equal(settings.llm_max_tokens,65536);
  }
});
test('invalid concurrency is rejected before submitting settings',()=>{
  const {context,run}=harness();
  for(const value of ['',' ','0','11','1.5','NaN','Infinity','invalid']){
    context.fd=new Map([['resume_concurrency',value]]);
    assert.throws(()=>run('readModelGenerationSettings(fd)'),/1—10 的整数/);
  }
});
