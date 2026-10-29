'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'..','hr_assist','web','app.js'),'utf8').replace(/\ninit\(\);\s*$/,'\n');
const ctx=vm.createContext({document:{addEventListener(){}},window:{addEventListener(){}},setInterval(){}});
vm.runInContext(source,ctx);
const render=(fn,result)=>{ctx.result=result;return vm.runInContext(`${fn}(result)`,ctx);};
const reason={code:'EDUCATION_ADMISSION_UNKNOWN',title:'招生方式待核验 <标题>',detail:'资料存在冲突 <说明>',action:'核对同一教育经历 <处理>'};

test('a limited grade shows the score-based grade and every reason with escaped actions',()=>{
  const html=render('decisionExplanation',{score:92,grade:'待定',base_grade:'强烈推荐',grade_limited:true,decision_reasons:[reason,{code:'CRITERION_UNKNOWN',title:'年限待核验',detail:'任职年月不明确',action:'核对任职年月'}]});
  assert.match(html,/<h3>为何待定<\/h3>/);
  assert.match(html,/按分数建议等级：<b>强烈推荐<\/b>/);
  assert.match(html,/因下列条件，最终等级调整为<b>待定<\/b>/);
  assert.match(html,/招生方式待核验 &lt;标题&gt;/);
  assert.match(html,/资料存在冲突 &lt;说明&gt;/);
  assert.match(html,/核对同一教育经历 &lt;处理&gt;/);
  assert.match(html,/年限待核验/);assert.match(html,/核对任职年月/);
  assert.doesNotMatch(html,/<标题>|<说明>|<处理>/);
});
test('a score-band hold is explained without inventing a grade restriction',()=>{
  const html=render('decisionExplanation',{score:65,grade:'待定',base_grade:'待定',grade_limited:false,decision_reasons:[{code:'SCORE_BAND_HOLD',title:'分数位于待定区间',detail:'65分属于60—69分区间',action:'查看逐项得分'}]});
  assert.match(html,/65分属于60—69分区间/);
  assert.doesNotMatch(html,/因下列条件|强烈推荐/);
});
test('incomplete historical result has an explicit fallback without recomputing its grade',()=>{
  const html=render('decisionExplanation',{score:95,grade:'待定'});
  assert.match(html,/旧评估未记录具体原因/);
  assert.match(html,/申请重新评估/);
  assert.doesNotMatch(html,/按分数建议等级|强烈推荐|招生方式/);
  assert.equal(render('decisionExplanation',{score:90,grade:'强烈推荐',decision_reasons:[]}),'');
});
test('not-stated admission is labelled as a policy default, never confirmed unified admission',()=>{
  const html=render('educationPolicyDetails',{policy_version:'admission-default-v1',education_gate:'pass',education:{degree:'bachelor',completion:'attained',admission:'not_stated',admission_defaulted:true,institution_match:'catalog'}});
  assert.match(html,/<dt>招生方式<\/dt><dd>未说明，招生方式按规则默认通过<\/dd>/);
  assert.match(html,/不代表已确认是统招/);
  assert.doesNotMatch(html,/已说明为统招/);
  assert.match(html,/<dt>学历政策判定<\/dt><dd>通过<\/dd>/);
});
test('new unknown means ambiguity while historical unknown remains under its saved policy',()=>{
  const result={education_gate:'unknown',education:{degree:'bachelor',completion:'attained',admission:'unknown',institution_match:'catalog'}};
  const legacy=render('educationPolicyDetails',result);
  assert.match(legacy,/未明确，按历史规则待核验/);
  assert.match(legacy,/原分数和等级保留/);
  assert.match(legacy,/只有申请重新评估后/);
  assert.doesNotMatch(legacy,/信息不明或冲突|按规则默认通过/);
  const current=render('educationPolicyDetails',{...result,policy_version:'admission-default-v1'});
  assert.match(current,/信息不明或冲突，待核验/);
});
test('detail page places the reason immediately after the score and before dimension scores',()=>{
  ctx.detail={id:'synthetic',file_id:'synthetic-file',name:'测试记录',role_name:'测试岗位',role_id:'role',status:'ASSESSED',assessment:{score:90,grade:'待定',base_grade:'强烈推荐',grade_limited:true,decision_reasons:[reason],scores:{hard:20},gate_reason:'政策说明',items:[]},manual:{},document:null,history:[]};
  vm.runInContext("state.detail=detail; reviewEditor=()=>'';",ctx);
  const html=vm.runInContext('detailPage()',ctx);
  assert.ok(html.indexOf('score-summary')<html.indexOf('为何待定'));
  assert.ok(html.indexOf('为何待定')<html.indexOf('score-rows'));
});
test('settings confirmation describes the current admission default and school requirement',()=>{
  assert.match(source,/非销售本科须在上述名录；未注明招生方式默认通过，明确非全日制等例外按规则处理/);
  assert.doesNotMatch(source,/非销售本科须统招且在上述名录/);
});
