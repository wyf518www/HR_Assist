'use strict';

const candidateDefaults={search:'',role:'',status:'',grade:'',department:'',interviewStatus:'',interviewResult:'',scoreMin:'',scoreMax:'',sort:'recommended_desc',page:1,pageSize:50};
const candidateView={...candidateDefaults};
const candidateSortOptions=[
  ['recommended_desc','推荐日期：新到旧'],['recommended_asc','推荐日期：旧到新'],
  ['score_desc','匹配度：高到低'],['score_asc','匹配度：低到高'],
  ['status_asc','处理状态：待处理优先'],['status_desc','处理状态：已评估优先'],
  ['created_desc','入库时间：新到旧'],['created_asc','入库时间：旧到新'],
  ['interview_asc','约面时间：近到远'],['interview_desc','约面时间：远到近']
];
function candidateSelect(label,key,options){
  return `<label class="field">${E(label)}<select data-candidate-filter="${key}">${options.map(([value,text])=>`<option value="${E(value)}" ${String(candidateView[key])===String(value)?'selected':''}>${E(text)}</option>`).join('')}</select></label>`;
}
function candidateFilters(message=''){
  const extra=['scoreMin','scoreMax','department','interviewStatus','interviewResult'].some(k=>candidateView[k]!=='');
  const statusValues=[...candidateStatusOrder,...new Set(state.applications.map(a=>a.status).filter(s=>s&&!candidateStatusOrder.includes(s)))];
  const followupOptions=key=>[['','全部'],['empty','未填写'],...followupChoices[key].map(value=>[value,value])];
  return `<section class="candidate-filters panel mb" aria-label="候选人筛选与排序"><div class="candidate-filter-main">
    <label class="field search">搜索姓名、岗位、附件名<input id="candidate-search" data-candidate-filter="search" value="${E(candidateView.search)}" type="search" placeholder="输入关键词"></label>
    ${candidateSelect('岗位','role',[['','全部岗位'],['unassigned','待分类'],...state.roles.map(r=>[r.id,r.name+(r.active?'':'（已停用）')])])}
    ${candidateSelect('处理状态','status',[['','全部状态'],...statusValues.map(s=>[s,statuses[s]||s])])}
    ${candidateSelect('推荐等级','grade',[['','全部等级'],...['强烈推荐','推荐','待定','不推荐'].map(g=>[g,g]),['unassessed','未评估']])}
  </div><details class="candidate-more-filters" ${extra?'open':''}><summary>更多筛选：匹配度与面试跟进</summary><div class="candidate-filter-extra">
    <fieldset class="candidate-score-range"><legend>匹配度区间（0—100）</legend><div><label class="field">最低分<input type="text" inputmode="numeric" maxlength="3" data-candidate-filter="scoreMin" value="${E(candidateView.scoreMin)}" placeholder="不限"></label><span aria-hidden="true">—</span><label class="field">最高分<input type="text" inputmode="numeric" maxlength="3" data-candidate-filter="scoreMax" value="${E(candidateView.scoreMax)}" placeholder="不限"></label></div></fieldset>
    ${candidateSelect('用人部门复筛结果','department',followupOptions('用人部门复筛结果'))}
    ${candidateSelect('面试状态','interviewStatus',followupOptions('面试状态'))}
    ${candidateSelect('面试结果','interviewResult',followupOptions('面试结果'))}
  </div></details><div class="candidate-list-options">
    ${candidateSelect('排序方式','sort',candidateSortOptions)}
    ${candidateSelect('每页显示','pageSize',[50,100,200,500].map(n=>[n,n+' 人']))}
    ${btn('重置筛选与排序','candidate-reset')}
    ${btn('刷新列表','refresh')}
  </div><p class="form-help">分数排序中，未评估始终排在末尾；暂计分保留待核验标记。状态排序按待核对、待分类、待 JD、待配置、解析失败、待人工核验、岗位停用、待处理、已评估排列。</p>
  <p id="candidate-filter-error" class="error-message" role="alert" ${message?'':'hidden'}>${E(message)}</p></section>`;
}
function candidateResult(){
  try{
    const result=candidateQuery(state.applications,candidateView);
    candidateView.page=result.page;
    return result;
  }catch(error){
    return {rows:[],total:state.applications.length,filtered:0,page:1,pages:1,pageSize:candidateView.pageSize,error:errorText(error)};
  }
}
function candidateSummary(result){
  if(result.error)return '筛选条件无效，请调整后继续。';
  const first=result.filtered?(result.page-1)*result.pageSize+1:0,last=first?first+result.rows.length-1:0;
  return `筛选出 ${result.filtered} 人 / 共 ${result.total} 人${result.filtered?` · 本页 ${first}—${last} 人`:''}`;
}
function candidatePagination(result){
  if(result.error)return '';
  return `<span class="tiny muted">第 ${result.page} / ${result.pages} 页</span><div class="actions">${btn('上一页','candidate-page',false,`data-page="${result.page-1}" ${result.page<=1?'disabled':''}`)}${btn('下一页','candidate-page',false,`data-page="${result.page+1}" ${result.page>=result.pages?'disabled':''}`)}</div>`;
}
function candidateSortHeader(key,label){
  const active=candidateView.sort.startsWith(key+'_'),ascending=candidateView.sort.endsWith('_asc');
  return `<th data-candidate-sort-header="${key}" aria-sort="${active?(ascending?'ascending':'descending'):'none'}"><button type="button" class="candidate-sort-button" data-action="candidate-sort" data-sort="${key}">${E(label)} <span aria-hidden="true">${active?(ascending?'↑':'↓'):'↕'}</span></button></th>`;
}
function candidateRows(rows=candidateResult().rows){
  return rows.length?rows.map(a=>`<tr><td><label class="candidate-select"><input type="checkbox" name="batch-candidate" value="${E(a.id)}" aria-label="选择 ${E(a.name)} / ${E(a.role_name||'待分类')}"><span><b>${E(a.name)}</b><span class="candidate-subtitle">${E(a.role_name||'待分类')}</span></span></label></td><td>${a.assessment&&Number.isFinite(a.assessment.score)?E(a.assessment.score)+' / 100':'—'}${a.assessment?.provisional?'<span class="candidate-subtitle">暂计分 · 待核验</span>':''}</td><td>${grade(a.assessment)}</td><td>${E(statuses[a.status]||a.status)}</td>${followupFields.map(k=>`<td>${E(followupDisplay(a.manual[k]))}</td>`).join('')}<td><div class="actions">${btn('跟进','followup',false,`data-id="${E(a.id)}"`)}${btn('查看依据','detail',false,`data-id="${E(a.id)}"`)}</div></td></tr>`).join(''):'<tr><td colspan="10" class="empty">暂无匹配的申请，可调整或重置筛选条件。</td></tr>';
}
function candidateList(){
  const result=candidateResult();
  return `${candidateFilters(result.error)}<p id="candidate-result-count" class="candidate-result-count" role="status" aria-live="polite">${candidateSummary(result)}</p>${batchFollowupToolbar()}<section class="panel table-scroll"><table class="live-table candidate-table"><thead><tr><th>姓名 / 岗位</th>${candidateSortHeader('score','匹配度')}<th>建议等级</th>${candidateSortHeader('status','处理状态')}${followupFields.map(k=>`<th>${E(k)}</th>`).join('')}<th>操作</th></tr></thead><tbody id="candidate-rows">${candidateRows(result.rows)}</tbody></table></section><div id="candidate-pagination" class="candidate-pagination" aria-label="候选人分页">${candidatePagination(result)}</div>`;
}
function updateCandidateResults(){
  const result=candidateResult(),rows=$('#candidate-rows');if(!rows)return;
  rows.innerHTML=candidateRows(result.rows);
  $('#candidate-result-count').textContent=candidateSummary(result);
  $('#candidate-pagination').innerHTML=candidatePagination(result);
  const error=$('#candidate-filter-error');error.textContent=result.error||'';error.hidden=!result.error;
  const sort=$('[data-candidate-filter="sort"]');if(sort)sort.value=candidateView.sort;
  document.querySelectorAll('[data-candidate-sort-header]').forEach(header=>{
    const key=header.dataset.candidateSortHeader,active=candidateView.sort.startsWith(key+'_'),ascending=candidateView.sort.endsWith('_asc');
    header.setAttribute('aria-sort',active?(ascending?'ascending':'descending'):'none');
    header.querySelector('span').textContent=active?(ascending?'↑':'↓'):'↕';
  });
  updateFollowupSelection();
}
function changeCandidateFilter(target){
  const key=target.dataset.candidateFilter;
  if(!Object.hasOwn(candidateDefaults,key))return;
  candidateView[key]=key==='pageSize'?Number(target.value):target.value;
  candidateView.page=1;
  updateCandidateResults();
}
function handleCandidateAction(action,el){
  if(action==='candidate-reset'){
    Object.assign(candidateView,candidateDefaults);
    const old=$('.candidate-filters');if(old)old.outerHTML=candidateFilters();
  }else if(action==='candidate-page'){
    candidateView.page=Number(el.dataset.page);
  }else if(action==='candidate-sort'){
    const key=el.dataset.sort;
    if(!['score','status'].includes(key))return;
    candidateView.sort=candidateView.sort.startsWith(key+'_')?key+(candidateView.sort.endsWith('_asc')?'_desc':'_asc'):key+(key==='status'?'_asc':'_desc');
    candidateView.page=1;
  }
  updateCandidateResults();
}
