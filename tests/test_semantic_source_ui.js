'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const escape=value=>String(value??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;');
const ctx=vm.createContext({E:escape,document:{addEventListener(){}},setInterval(){}});
for(const file of ['identities.js','jd-imports.js'])vm.runInContext(fs.readFileSync(path.join(__dirname,'..','hr_assist','web',file),'utf8'),ctx);
const render=(fn,ref)=>{ctx.ref=ref;return vm.runInContext(`${fn}(ref)`,ctx);};
for(const fn of ['identityEvidence','jdReference']){
  test(`${fn} separates and escapes model interpretation and server original`,()=>{
    const html=render(fn,{span_id:'R2',page:2,source:'text',semantic:true,interpretation:'归纳 <模型>',quote:'完整来源 <原文>'});
    assert.match(html,/<p><b>模型解读：<\/b>归纳 &lt;模型&gt;<\/p>/);
    assert.match(html,/<details><summary>查看对应原文/);
    assert.match(html,/<blockquote>完整来源 &lt;原文&gt;<\/blockquote>/);
    assert.doesNotMatch(html,/<blockquote>[^<]*归纳/);
    assert.doesNotMatch(html,/<模型>|<原文>/);
  });
  test(`${fn} keeps legacy quotations distinct from new analysis`,()=>{
    const html=render(fn,{span_id:'R1',quote:'历史引文'});
    assert.match(html,/历史引文/);
    assert.doesNotMatch(html,/模型解读|查看对应原文/);
  });
}
test('JD editor analysis evidence uses the same semantic-source renderer',()=>{
  const source=fs.readFileSync(path.join(__dirname,'..','hr_assist','web','jd-imports.js'),'utf8');
  assert.match(source,/draft\.evidence\[key\]\|\|\[\]\)\.map\(jdReference\)/);
  assert.doesNotMatch(source,/每份新文件调用一次/);
});
