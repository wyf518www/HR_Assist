'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'..','hr_assist','web','app.js'),'utf8').replace(/\ninit\(\);\s*$/,'\n');
const ctx=vm.createContext({document:{addEventListener(){}},window:{addEventListener(){}},setInterval(){}});
vm.runInContext(source,ctx);
const render=ref=>{ctx.ref=ref;return vm.runInContext('assessmentEvidence(ref)',ctx);};
test('semantic explanation is separate from server-owned original text',()=>{
  const html=render({span_id:'R2',interpretation:'具备3年经验 <模型>',quote:'三年相关经验 <原文>',semantic:true,page:2,source:'text'});
  assert.match(html,/模型解读：/);assert.match(html,/具备3年经验 &lt;模型&gt;/);
  assert.match(html,/<summary>对应原文 R2 · 第2页<\/summary><pre class="source-text">三年相关经验 &lt;原文&gt;/);
  assert.doesNotMatch(html,/<blockquote>.*3年/);
});
test('legacy quotations are not relabelled as a new model review',()=>{
  const html=render({span_id:'R1',quote:'历史引文'});
  assert.equal(html,'<blockquote>R1：历史引文</blockquote>');
});
test('semantic facts and education show interpretations and actual source with escaping',()=>{
  ctx.result={evidence_mode:'model_semantic_review',facts:{experience:'3年'},fact_refs:{experience:{span_id:'R1',semantic:true,interpretation:'归纳',quote:'三年'}},education:{interpretation:'同一教育经历 <判断>',catalog_name:'示例院校',evidence:[]}};
  const html=vm.runInContext('semanticFacts(result)',ctx);
  assert.match(html,/工作年限：3年/);assert.match(html,/模型整理结果/);assert.match(html,/同一教育经历 &lt;判断&gt;/);
  assert.match(html,/示例院校/);assert.match(html,/>三年<\/pre>/);
});
