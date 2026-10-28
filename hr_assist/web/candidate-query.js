'use strict';

const candidateStatusOrder=Object.freeze([
  'WAITING_IDENTITY','WAITING_ROLE','WAITING_JD','WAITING_CONFIG',
  'PARSE_FAILED','WAITING_REVIEW','ROLE_INACTIVE','QUEUED','ASSESSED',
]);

function candidateQuery(rows,query={}){
  const bound=(value,label)=>{
    if(value===undefined||value===null||String(value).trim()==='')return null;
    const text=String(value).trim();
    if(!/^\d{1,3}$/.test(text)||Number(text)>100)
      throw new Error(`匹配度${label}请填写0—100的整数。`);
    return Number(text);
  };
  const min=bound(query.scoreMin,'最低分'),max=bound(query.scoreMax,'最高分');
  if(min!==null&&max!==null&&min>max)throw new Error('匹配度最低分不能高于最高分。');

  // No score and a real score of zero have different meanings throughout the query.
  const score=row=>typeof row.assessment?.score==='number'&&Number.isFinite(row.assessment.score)
    &&row.assessment.score>=0&&row.assessment.score<=100?row.assessment.score:null;
  const isEmpty=value=>value===undefined||value===null||String(value).trim()==='';
  const manualMatch=(row,column,value)=>!value||(value==='empty'
    ?isEmpty(row.manual?.[column]):row.manual?.[column]===value);
  const search=String(query.search??'').trim().toLowerCase();
  const filtered=rows.filter(row=>{
    const points=score(row);
    return (!search||[row.name,row.role_name,row.original_name].some(value=>String(value??'').toLowerCase().includes(search)))
      &&(!query.role||(query.role==='unassigned'?!row.role_id:row.role_id===query.role))
      &&(!query.status||row.status===query.status)
      &&(!query.grade||(query.grade==='unassessed'?points===null:row.assessment?.grade===query.grade))
      &&manualMatch(row,'用人部门复筛结果',query.department)
      &&manualMatch(row,'面试状态',query.interviewStatus)
      &&manualMatch(row,'面试结果',query.interviewResult)
      &&(min===null||(points!==null&&points>=min))
      &&(max===null||(points!==null&&points<=max));
  });

  // Date-only and timezone-free Excel/web dates are compared as calendar values,
  // independent of the viewer's local timezone or daylight-saving rules.
  const parseDate=value=>{
    if(typeof value!=='string')return null;
    const match=value.trim().match(/^(\d{4})[-/](\d{2})[-/](\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2})(\.\d{1,6})?)?(Z|[+-]\d{2}:\d{2})?)?$/);
    if(!match)return null;
    const [,year,month,day,hour='00',minute='00',second='00',fraction='',zone='']=match;
    const utc=Date.UTC(+year,+month-1,+day,+hour,+minute,+second);
    const date=new Date(utc);
    if(date.getUTCFullYear()!==+year||date.getUTCMonth()!==+month-1||date.getUTCDate()!==+day
      ||+hour>23||+minute>59||+second>59)return null;
    const timestamp=Date.parse(`${year}-${month}-${day}T${hour}:${minute}:${second}${fraction}${zone||'Z'}`);
    return Number.isFinite(timestamp)?timestamp:null;
  };
  const dateCache=new Map();
  const dateValue=value=>{
    if(!dateCache.has(value))dateCache.set(value,parseDate(value));
    return dateCache.get(value);
  };
  const compare=(a,b,direction=1)=>{
    if(a===null||b===null)return a===b?0:a===null?1:-1;
    return (a<b?-1:a>b?1:0)*direction;
  };
  const textValue=value=>isEmpty(value)?null:String(value);
  const byCreated=(a,b)=>compare(dateValue(a.created_at),dateValue(b.created_at),-1);
  const byId=(a,b)=>compare(textValue(a.id),textValue(b.id));
  const rank=row=>{
    const index=candidateStatusOrder.indexOf(row.status);
    return index<0?null:index;
  };
  const sorts={
    recommended_desc:(a,b)=>compare(dateValue(a.recommended_at),dateValue(b.recommended_at),-1)
      ||compare(textValue(a.role_name),textValue(b.role_name)),
    recommended_asc:(a,b)=>compare(dateValue(a.recommended_at),dateValue(b.recommended_at))
      ||compare(textValue(a.role_name),textValue(b.role_name)),
    created_desc:byCreated,
    created_asc:(a,b)=>compare(dateValue(a.created_at),dateValue(b.created_at)),
    score_desc:(a,b)=>compare(score(a),score(b),-1),
    score_asc:(a,b)=>compare(score(a),score(b)),
    status_asc:(a,b)=>compare(rank(a),rank(b)),
    status_desc:(a,b)=>compare(rank(a),rank(b),-1),
    interview_asc:(a,b)=>compare(dateValue(a.manual?.['约面时间']),dateValue(b.manual?.['约面时间'])),
    interview_desc:(a,b)=>compare(dateValue(a.manual?.['约面时间']),dateValue(b.manual?.['约面时间']),-1),
  };
  const sort=Object.hasOwn(sorts,query.sort)?sorts[query.sort]:sorts.recommended_desc;
  filtered.sort((a,b)=>sort(a,b)||byCreated(a,b)||byId(a,b));
  const pageSize=[50,100,200,500].includes(Number(query.pageSize))?Number(query.pageSize):50;
  const pages=Math.max(1,Math.ceil(filtered.length/pageSize));
  const requestedPage=Number(query.page);
  const page=Math.min(pages,Number.isInteger(requestedPage)&&requestedPage>0?requestedPage:1);
  return {rows:filtered.slice((page-1)*pageSize,page*pageSize),total:rows.length,
    filtered:filtered.length,page,pageSize,pages};
}
