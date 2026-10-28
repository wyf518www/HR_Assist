'use strict';
const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const context=vm.createContext({});
vm.runInContext(fs.readFileSync(path.join(__dirname,'../hr_assist/web/candidate-query.js'),'utf8'),context);
const query=vm.runInContext('candidateQuery',context);
const statusOrder=Array.from(vm.runInContext('candidateStatusOrder',context));
const ids=result=>Array.from(result.rows,row=>row.id);
const candidate=(id,overrides={})=>({
  id,name:'测试候选人'+id,role_id:'engineering',role_name:'开发工程师',original_name:'合成简历.pdf',
  status:'ASSESSED',created_at:'2026-10-28T08:00:00+00:00',recommended_at:'2026-10-28',
  assessment:{score:80,grade:'推荐',provisional:false},manual:{},...overrides,
});

test('combined search, job, score, processing and human follow-up filters intersect',()=>{
  const target=candidate('target',{name:'Synthetic Alpha',assessment:{score:90,grade:'强烈推荐'},
    manual:{'用人部门复筛结果':'通过','面试状态':'待面试','面试结果':'待定'}});
  const rows=[target,candidate('other'),candidate('wrong-job',{...target,id:'wrong-job',role_id:'sales'}),
    candidate('wrong-stage',{...target,id:'wrong-stage',status:'WAITING_REVIEW'}),
    candidate('wrong-followup',{...target,id:'wrong-followup',manual:{...target.manual,'面试结果':'通过'}})];
  const result=query(rows,{search:'  ALPHA ',role:'engineering',status:'ASSESSED',grade:'强烈推荐',
    scoreMin:'85',scoreMax:100,department:'通过',interviewStatus:'待面试',interviewResult:'待定'});
  assert.deepEqual(ids(result),['target']);
  assert.equal(result.total,5);assert.equal(result.filtered,1);
});

test('search finds role and original filename and treats punctuation as literal text',()=>{
  const rows=[candidate('a',{original_name:'【架构】Synthetic.PDF'}),candidate('b',{role_name:'算法设计师'}),
    candidate('c',{name:'测试[甲]'}),candidate('d')];
  assert.deepEqual(ids(query(rows,{search:'synthetic.pdf'})),['a']);
  assert.deepEqual(ids(query(rows,{search:'算法'})),['b']);
  assert.deepEqual(ids(query(rows,{search:'[甲]'})),['c']);
  assert.equal(query(rows,{search:'   '}).filtered,4);
});

test('unassigned and empty human fields include old records with missing columns',()=>{
  const rows=[candidate('null',{role_id:null,manual:{'面试状态':null}}),
    candidate('missing',{role_id:null,manual:undefined}),candidate('space',{role_id:null,manual:{'面试状态':'  '}}),
    candidate('filled',{role_id:null,manual:{'面试状态':'待面试'}}),candidate('assigned')];
  assert.deepEqual(ids(query(rows,{role:'unassigned',interviewStatus:'empty'})),['missing','null','space']);
  assert.equal(query(rows,{department:'empty',interviewResult:'empty'}).filtered,5);
});

test('unassessed includes absent or invalid scores; a verified zero is still assessed',()=>{
  const rows=[candidate('none',{assessment:null}),candidate('empty',{assessment:{}}),
    candidate('null',{assessment:{score:null}}),candidate('invalid',{assessment:{score:'0'}}),
    candidate('zero',{assessment:{score:0,grade:'不推荐'}}),candidate('positive')];
  assert.deepEqual(ids(query(rows,{grade:'unassessed'})),['empty','invalid','none','null']);
  assert.deepEqual(ids(query(rows,{scoreMin:0,scoreMax:0})),['zero']);
  assert.deepEqual(ids(query(rows,{scoreMin:0})),['positive','zero']);
  assert.equal(query(rows,{grade:'unassessed',scoreMin:0}).filtered,0);
});

test('score bounds are inclusive and invalid ranges fail before filtering',()=>{
  const rows=[candidate('low',{assessment:{score:70}}),candidate('high',{assessment:{score:85}}),
    candidate('out',{assessment:{score:86}})];
  assert.deepEqual(ids(query(rows,{scoreMin:'70',scoreMax:'85'})),['high','low']);
  for(const scoreMin of [-1,101,NaN,Infinity,'bad','5.5','0x10','1e2',false]){
    assert.throws(()=>query(rows,{scoreMin}),/最低分.*0—100.*整数/);
  }
  assert.throws(()=>query(rows,{scoreMax:'101'}),/最高分.*0—100.*整数/);
  assert.throws(()=>query(rows,{scoreMin:86,scoreMax:70}),/最低分不能高于最高分/);
  assert.equal(query(rows,{scoreMin:' ',scoreMax:null}).filtered,3);
});

test('score sorting keeps unassessed last in both directions and preserves zero',()=>{
  const rows=[candidate('none',{assessment:null}),candidate('zero',{assessment:{score:0}}),
    candidate('high',{assessment:{score:100}}),candidate('low',{assessment:{score:30}})];
  assert.deepEqual(ids(query(rows,{sort:'score_desc'})),['high','low','zero','none']);
  assert.deepEqual(ids(query(rows,{sort:'score_asc'})),['zero','low','high','none']);
});

test('status order groups pending stages and keeps unknown statuses last both ways',()=>{
  const rows=[candidate('unknown',{status:'FUTURE_STATUS'}),...statusOrder.map(status=>candidate(status,{status}))];
  assert.deepEqual(ids(query(rows,{sort:'status_asc'})),[...statusOrder,'unknown']);
  assert.deepEqual(ids(query(rows,{sort:'status_desc'})),[...statusOrder].reverse().concat('unknown'));
  assert.deepEqual(ids(query(rows,{status:'WAITING_IDENTITY'})),['WAITING_IDENTITY']);
});

test('default recommendation order uses latest date then job, with empty dates last',()=>{
  const rows=[candidate('old',{recommended_at:'2026-10-27'}),candidate('b',{role_name:'B'}),
    candidate('a',{role_name:'A'}),candidate('none',{recommended_at:null})];
  assert.deepEqual(ids(query(rows)),['a','b','old','none']);
  assert.deepEqual(ids(query(rows,{sort:'recommended_asc'})),['old','a','b','none']);
  assert.deepEqual(ids(query(rows,{sort:'unsupported'})),['a','b','old','none']);
  assert.deepEqual(ids(query(rows,{sort:'__proto__'})),['a','b','old','none']);
});

test('creation dates compare instants across ISO offsets, with missing values last',()=>{
  const rows=[candidate('old',{created_at:'2026-10-28T00:00:00Z'}),
    candidate('new',{created_at:'2026-10-28T09:00:00+08:00'}),candidate('none',{created_at:null})];
  assert.deepEqual(ids(query(rows,{sort:'created_desc'})),['new','old','none']);
  assert.deepEqual(ids(query(rows,{sort:'created_asc'})),['old','new','none']);
});

test('interview dates accept Excel date/datetime values and keep invalid or empty last',()=>{
  const rows=[candidate('later',{manual:{'约面时间':'2026-11-01T09:30:00'}}),
    candidate('date',{manual:{'约面时间':'2026-10-29'}}),
    candidate('space',{manual:{'约面时间':'2026-10-29 13:05'}}),
    candidate('fraction',{manual:{'约面时间':'2026-10-29T13:05:00.001000'}}),
    candidate('empty'),candidate('invalid',{manual:{'约面时间':'2026-02-30T09:30'}})];
  assert.deepEqual(ids(query(rows,{sort:'interview_asc'})),['date','space','fraction','later','empty','invalid']);
  assert.deepEqual(ids(query(rows,{sort:'interview_desc'})),['later','fraction','space','date','empty','invalid']);
});

test('ties use newest creation date then stable id, never input order',()=>{
  const rows=[candidate('b'),candidate('old',{created_at:'2026-10-27T08:00:00Z'}),candidate('a')];
  for(const sort of ['score_asc','score_desc','status_asc','status_desc','recommended_desc','interview_asc']){
    assert.deepEqual(ids(query(rows,{sort})),['a','b','old']);
    assert.deepEqual(ids(query([...rows].reverse(),{sort})),['a','b','old']);
  }
});

test('pagination follows filtering and sorting and clamps stale page numbers',()=>{
  const rows=Array.from({length:120},(_,index)=>candidate(String(index).padStart(3,'0'),{
    role_id:index%2?'sales':'engineering',assessment:{score:index%101}}));
  const result=query(rows,{sort:'score_desc',page:2,pageSize:50});
  assert.equal(result.total,120);assert.equal(result.filtered,120);assert.equal(result.pages,3);
  assert.equal(result.page,2);assert.equal(result.pageSize,50);assert.equal(result.rows.length,50);
  const filtered=query(rows,{role:'sales',page:999,pageSize:50});
  assert.equal(filtered.filtered,60);assert.equal(filtered.page,2);assert.equal(filtered.rows.length,10);
  assert.equal(query(rows,{pageSize:'100',page:2}).rows.length,20);
  assert.equal(query(rows,{pageSize:200}).rows.length,120);
  assert.equal(query(rows,{pageSize:500}).rows.length,120);
  for(const pageSize of [0,10,10000,'bad'])assert.equal(query(rows,{pageSize}).pageSize,50);
  for(const page of [0,-1,1.5,Infinity,'bad'])assert.equal(query(rows,{page}).page,1);
});

test('no matches keep valid pagination metadata',()=>{
  const result=query([candidate('a')],{search:'missing',page:10});
  assert.deepEqual(ids(result),[]);assert.equal(result.total,1);assert.equal(result.filtered,0);
  assert.equal(result.page,1);assert.equal(result.pages,1);
});

test('query never mutates source records or the input array',()=>{
  const rows=[candidate('b'),candidate('a')];
  const before=JSON.stringify(rows);
  rows.forEach(row=>{Object.freeze(row.manual);Object.freeze(row.assessment);Object.freeze(row);});
  Object.freeze(rows);
  const options=Object.freeze({sort:'score_desc',page:2});
  const result=query(rows,options);
  assert.equal(JSON.stringify(rows),before);assert.notEqual(result.rows,rows);
  assert.equal(result.rows[0],rows[1]);assert.equal(options.page,2);
});
