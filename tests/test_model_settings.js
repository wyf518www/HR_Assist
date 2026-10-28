'use strict';
const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../hr_assist/web/model-settings.js'),'utf8');
function harness(){
  const context=vm.createContext({URL,fetch(){throw new Error('No network from preset');},
    liveField:(_label,name,value)=>`<input name="${name}" value="${value}">`,
    btn:(label,action)=>`<button type="button" data-action="${action}">${label}</button>`});
  vm.runInContext(source,context);
  const form={elements:Object.fromEntries(Object.entries({llm_endpoint:'https://open.bigmodel.cn/api/paas/v4/chat/completions',llm_model:'glm-5.3',llm_reasoning_effort:'default',llm_max_tokens:''}).map(([k,value])=>[k,{value}]))};
  return {context,form,run:s=>vm.runInContext(s,context)};
}
test('light preset changes only the two generation controls and does not call any service',()=>{
  const {context,form,run}=harness();context.form=form;
  run('applyModelGenerationPreset(form,true)');
  assert.equal(form.elements.llm_reasoning_effort.value,'low');
  assert.equal(form.elements.llm_max_tokens.value,'32768');
  assert.equal(form.elements.llm_model.value,'glm-5.3');
  run('applyModelGenerationPreset(form,false)');
  assert.equal(form.elements.llm_reasoning_effort.value,'default');
  assert.equal(form.elements.llm_max_tokens.value,'');
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
  assert.match(html,/<input name="llm_max_tokens" value="">/);
  assert.doesNotMatch(html,/<option value="low" selected>/);
  assert.match(html,/不能关闭/);
});
