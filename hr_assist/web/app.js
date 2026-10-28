'use strict';
const $=selector=>document.querySelector(selector);
const escapeHTML=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const E=escapeHTML;
const titles={diagnostics:'详细诊断日志',home:'工作台首页',overview:'工作概览',candidates:'候选人库',jobs:'岗位与 JD',reports:'报表与反馈',runs:'运行记录',settings:'连接与设置',detail:'候选人详情','role-editor':'编辑岗位 JD','jd-imports':'批量导入 JD','jd-review':'核对 JD','identity-review':'姓名与岗位核对'};
const state={session:null,view:'home',dashboard:null,roles:[],applications:[],detail:null,draft:null,busy:false};
const statuses={QUEUED:'待处理',WAITING_IDENTITY:'待姓名与岗位核对',WAITING_ROLE:'待分类',ROLE_INACTIVE:'岗位已停用',WAITING_JD:'待 JD',WAITING_CONFIG:'待配置',PARSE_FAILED:'解析失败',WAITING_REVIEW:'待人工核验',ASSESSED:'已评估',RUNNING:'运行中',SUCCESS:'完成',SUCCESS_WITH_EXCEPTIONS:'完成，有异常',BLOCKED:'已暂停',INTERRUPTED:'中断待续跑'};
const fieldLabels={education:'学历要求',experience:'经验年限',location:'工作地点 / base地（可选备注，不评分）',certificates:'证书要求',availability:'到岗时间（可选备注，不评分）',responsibilities:'岗位职责',skills:'核心技能'};
let toastTimer;
function toast(text){$('#toast').textContent=text;$('#toast').hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(()=>{$('#toast').hidden=true;},6500);}
function errorText(error){return error.message||'请求失败，请重试。';}
async function api(path,{method='GET',body}={}){
  const headers={Accept:'application/json'};
  if(state.session)headers['X-CSRF-Token']=state.session.csrf;
  if(body!==undefined&&!(body instanceof FormData)){headers['Content-Type']='application/json';body=JSON.stringify(body);}
  const response=await fetch('/api'+path,{method,headers,body,credentials:'same-origin'});
  const data=await response.json().catch(()=>({}));
  if(!response.ok){const error=new Error(data.error?.message||`请求失败（${response.status}）`);error.status=response.status;if(response.status===401&&path!=='/auth/login'){state.session=null;showLogin();}throw error;}
  return data;
}
function btn(text,action,primary=false,extra=''){return `<button type="button" class="button ${primary?'primary':''}" data-action="${action}" ${extra}>${text}</button>`;}
function link(fileId,text='下载文件'){return `<a class="button compact" href="/api/files/${encodeURIComponent(fileId)}">${text}</a>`;}
function pill(text,kind='neutral'){return `<span class="badge ${kind}">${E(text)}</span>`;}
function heading(title,description,extra=''){return `<div class="page-heading"><div><h1>${E(title)}</h1><p>${E(description)}</p></div>${extra}</div>`;}
function stamp(value){return value?new Date(value).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false}):'—';}
function grade(result){return result?pill(result.grade,result.grade==='强烈推荐'?'strong':result.grade==='推荐'?'recommend':result.grade==='待定'?'hold':'reject'):pill('未评估');}
function empty(title,text){return `<div class="empty-state"><h2>${title}</h2><p>${text}</p></div>`;}
function liveField(label,name,value='',type='text',extra=''){return `<label class="live-field">${label}<input name="${name}" type="${type}" value="${E(value)}" ${extra}></label>`;}
function textarea(label,name,value='',extra=''){return `<label class="live-field">${label}<textarea name="${name}" ${extra}>${E(value)}</textarea></label>`;}
function check(label,name,checked=false){return `<label class="check-field"><input type="checkbox" name="${name}" ${checked?'checked':''}><span>${label}</span></label>`;}

function showLogin(){
  $('#app').innerHTML=`<div class="login-wrap"><section class="login-card"><div class="brand"><span class="brand-mark">人</span><span><b>HR Assist</b><small>人事工作台</small></span></div><h1>登录我的工作台</h1><p>招聘资料保存在你的服务中，请使用部署时设置的管理员账号。</p><form id="login-form" class="form-stack">${liveField('账号','username','','text','required autocomplete="username"')}${liveField('密码','password','','password','required autocomplete="current-password"')}<button class="button primary" type="submit">登录</button></form><div id="login-error" class="error-message" role="alert"></div></section></div>`;
}
function shell(){
  const home=state.view==='home';
  const nav=(id,name,icon)=>`<button class="nav-item ${state.view===id||(id==='jobs'&&['role-editor','jd-imports','jd-review'].includes(state.view))||(id==='candidates'&&['detail','identity-review'].includes(state.view))||(id==='runs'&&state.view==='diagnostics')?'active':''}" data-action="nav" data-view="${id}"><span class="nav-glyph" aria-hidden="true">${icon}</span>${name}</button>`;
  $('#app').innerHTML=`<div class="shell"><aside class="sidebar"><a class="brand" href="#home"><span class="brand-mark">人</span><span><b>HR Assist</b><small>人事工作台</small></span></a><div class="workspace"><span class="workspace-icon">${home?'全':'招'}</span><div><b>${home?'全部人事功能':'招聘流程助手'}</b><small>${home?'选择工作模块':'招聘业务模块'}</small></div></div><nav class="${home?'home-nav':''}" aria-label="主导航">${nav('home','工作台首页','⌂')}<div class="nav-section-label">${home?'业务模块':'招聘流程助手'}</div>${home?nav('overview','招聘流程助手','▣'):[['overview','工作概览','▦'],['candidates','候选人库','▤'],['jobs','岗位与 JD','▣'],['reports','报表与反馈','▥'],['runs','运行记录','◷'],['settings','连接与设置','⚙']].map(x=>nav(...x)).join('')}</nav><div class="sidebar-bottom"><b>${E(state.session.username)}</b><p>群晖部署 · 单用户</p><button class="sidebar-logout" data-action="logout">退出登录</button></div></aside><div class="main-wrap"><header class="topbar"><div class="breadcrumb"><a href="#home">人事工作台</a>${home?'<span>/</span>首页':`<span>/</span><a href="#overview">招聘流程助手</a><span>/</span><b>${E(titles[state.view]||'招聘助手')}</b>`}</div><div class="demo-pill">私人工作台 · v${E(state.session.version||'0.1.0')}</div></header><main id="content"><div id="page"></div><footer class="live-footer"><span>数据存于服务器挂载目录 · 所有显示时间为北京时间</span><span>推荐供人工核验</span><button class="sidebar-logout" data-action="logout">退出登录</button></footer></main></div></div>`;
}
let navigationRevision=0;
let runPollInFlight=false;
async function refreshBase(view){
  const dependencies={
    home:{dashboard:'/dashboard'},overview:{dashboard:'/dashboard'},
    candidates:{roles:'/roles?summary=true',applications:'/applications?summary=true'},
    jobs:{roles:'/roles'},'jd-review':{roles:'/roles'},
    'identity-review':{roles:'/roles?summary=true'},detail:{roles:'/roles?summary=true'}
  };
  const entries=Object.entries(dependencies[view]||{});
  return Object.fromEntries(await Promise.all(entries.map(async([key,path])=>[key,await api(path)])));
}
async function navigate(view,{reload=true}={}){
  if(!titles[view])view='home';
  const revision=++navigationRevision;
  state.view=view;
  if(location.hash!==`#${view}`)history.pushState(null,'',`#${view}`);
  shell();$('#page').innerHTML='<p class="notice" role="status">正在载入…</p>';
  try{
    const loaded=reload?await refreshBase(view):{};
    if(revision!==navigationRevision||!state.session)return;
    Object.assign(state,loaded);
    await render(view,revision);
    if(revision===navigationRevision)window.scrollTo(0,0);
  }catch(error){
    if(revision===navigationRevision&&state.session){
      $('#page').innerHTML=`<div class="error-banner" role="alert">${E(errorText(error))}</div><div class="actions mt">${btn('重试载入','refresh')}</div>`;
      toast(errorText(error));
    }
  }
}
function homePage(){
  const counts=state.dashboard.counts;
  const planned=[['档','员工档案','人员资料、合同与证照。'],['转','入转调离','办理清单与流程跟进。'],['勤','考勤与休假','考勤核对与休假事项。'],['薪','薪酬与社保','核算资料与申报清单。'],['育','绩效与培训','评价反馈与培训计划。']];
  return `${heading('人事工作台','选择工作模块，开始今天的人事工作。')}<div class="home-section-heading"><h2>功能入口</h2><span class="tiny muted">招聘助手已接入</span></div><section class="module-launch"><div class="module-intro"><div class="module-identity"><span class="module-icon">招</span><div><h2>招聘流程助手</h2>${pill('第一版','strong')}</div></div><p class="module-description">从收到一份简历，到形成一份有据可查的推荐。</p><p class="module-detail">上传简历与 JD，核对分析依据，下载推荐报告，通过 Excel 回传人工跟进。</p><div class="module-capabilities"><span>邮件收集与归档</span><span>JD 匹配与证据</span><span>报表与人工反馈</span></div><div class="module-entry">${btn('进入招聘助手','enter',true)}</div></div><aside class="module-activity"><h3>招聘工作空间</h3><div class="activity-metrics"><div><strong>${counts.applications}</strong><span>份岗位申请</span></div><div><strong>${counts.pending}</strong><span>份待处理申请</span></div></div><p class="activity-note">${state.dashboard.preflight.ready?'配置已齐备，可以手动处理或启用计划。':'首次使用请先配置岗位 JD、模型与规则。'}</p>${btn('连接与设置','settings')}</aside></section><section class="future-modules"><div class="home-section-heading"><h2>后续功能方向</h2>${pill('尚未开发')}</div><div class="planned-grid">${planned.map(([icon,name,desc])=>`<article class="planned-module"><div class="planned-title"><span class="planned-icon">${icon}</span><h3>${name}</h3><span class="planned-status">待规划</span></div><p>${desc}</p></article>`).join('')}</div></section>`;
}
function overviewPage(){
  const d=state.dashboard,c=d.counts,run=d.latest_run;
  return `${heading('工作概览','收集、评估、跟进。每条推荐都能回到证据。',`<div class="actions">${btn('处理已上传简历','process',true)}${btn('收取邮箱新简历','mail')}</div>`)}${!d.preflight.ready?`<div class="live-banner">尚需配置：${E(d.preflight.missing.join('；'))}。本地上传和人工跟进仍可使用。</div>`:''}<section class="metrics"><div class="metric"><div class="metric-label">岗位申请</div><div class="metric-number">${c.applications}</div></div><div class="metric"><div class="metric-label">待处理</div><div class="metric-number">${c.pending}</div></div><div class="metric"><div class="metric-label">推荐及以上</div><div class="metric-number">${c.grades['强烈推荐']+c.grades['推荐']}</div></div><div class="metric"><div class="metric-label">岗位</div><div class="metric-number">${c.roles}</div></div></section><div class="section-grid"><section class="panel"><div class="panel-head"><h2>最近任务</h2>${run?pill(statuses[run.status]||run.status,run.status==='BLOCKED'?'hold':'neutral'):''}</div><div class="panel-body">${run?`<p>开始：${stamp(run.started_at)}</p><p class="muted tiny mt-small">${E(run.error||'处理阶段：'+run.stage)}</p><div class="pill-row mt">${Object.entries(run.stats.grades||{}).map(([g,n])=>pill(g+' '+n)).join('')}</div>${btn('查看运行记录','runs')}`:empty('尚未运行','先添加岗位 JD，再上传简历或配置邮箱。')}</div></section><section class="panel"><div class="panel-head"><h2>通知中心</h2><span class="tiny muted">仅在工作台内显示</span></div><div class="panel-body notification-list">${d.notifications.length?d.notifications.map(n=>`<div class="notification-item ${n.read?'':'notification-unread'}"><p>${E(n.message)}</p><small>${stamp(n.created_at)}</small>${!n.read?btn('标为已读','read-notice',false,`data-id="${n.id}"`):''}</div>`).join(''):'<p class="muted tiny">暂无通知。</p>'}</div></section></div><div class="notice mt">计划：${d.schedule.enabled?`每天 ${E(d.schedule.time)}（北京时间）`:'尚未启用'}。程序不会自动对外发送候选人资料或作出录用决定。</div>`;
}
function candidatePage(){
  return `${heading('候选人库','PDF 原件保留，按岗位建立申请，重复投递保留来源。',`<div class="actions">${btn('姓名与岗位核对','identity-review')}${btn('解析与评估待处理项','process',true)}</div>`)}<section class="upload-zone mb"><h2>上传简历</h2><form id="resume-form" class="upload-form"><label class="live-field">PDF 原件（每份最多20MB）<input name="files" type="file" accept=".pdf" multiple required></label><label class="live-field">岗位<select name="role_id"><option value="">由大模型识别应聘岗位</option>${state.roles.filter(r=>r.active).map(r=>`<option value="${r.id}">${E(r.name)}</option>`).join('')}</select></label><button class="button primary" type="submit">上传并登记</button></form><p class="form-help mt-small">大模型结合附件名、邮件主题及简历文字识别姓名和应聘岗位；无法确定时等待核对。手工选定岗位会保留。</p></section>${candidateList()}`;
}
const followupFields=['推荐给用人部门日期','用人部门复筛结果','约面时间','面试状态','面试结果'];
const followupChoices={'用人部门复筛结果':['通过','不通过'],'面试状态':['待面试','已面','拒绝面试'],'面试结果':['通过','待定','未通过']};
function followupDisplay(value){return value?String(value).replace('T',' ').replace(/ 00:00:00$/,'').replace(/(\d{2}:\d{2}):00$/,'$1'):'—';}
function closeFollowup(){const dialog=$('#followup-dialog');if(dialog){dialog.close();dialog.remove();}}
function openFollowup(id){
  closeFollowup();
  const a=state.view==='detail'&&state.detail?.id===id?state.detail:state.applications.find(a=>a.id===id);
  if(!a)return;
  const dialog=document.createElement('dialog');dialog.id='followup-dialog';dialog.className='followup-dialog';
  dialog.innerHTML=`<form id="candidate-followup-form" class="form-stack"><div class="panel-head"><h2>候选人跟进 · ${E(a.name)}</h2>${btn('关闭','close-followup')}</div><p class="form-help">约面时间使用北京时间。只保存你改动的字段；面试评价和原有状态备注保留，可在 Excel 中维护。</p>${followupFields.map((key,index)=>{
    const value=String(a.manual[key]??''),name='followup_'+index,choices=followupChoices[key];
    if(choices)return `<label class="live-field">${E(key)}<select name="${name}"><option value="">未填写</option>${value&&!choices.includes(value)?`<option value="${E(value)}" selected>保留原值：${E(value)}</option>`:''}${choices.map(c=>`<option value="${E(c)}" ${value===c?'selected':''}>${E(c)}</option>`).join('')}</select></label>`;
    const display=key==='推荐给用人部门日期'?value.slice(0,10):value.replace(' ','T').slice(0,16);
    return liveField(E(key),name,display,key==='约面时间'?'datetime-local':'date')+(value?`<p class="form-help">已保存：${E(followupDisplay(value))}。未编辑则保持原值。</p>`:'');
  }).join('')}<p class="error-message" role="alert"></p><div class="actions"><button type="submit" class="button primary">保存跟进</button>${btn('取消','close-followup')}</div></form>`;
  dialog.dataset.applicationId=id;dialog.dataset.revision=a.revision;
  document.body.append(dialog);
  // Compare with rendered controls, not legacy values (which may be dates or formulas).
  dialog._baseline=Object.fromEntries(new FormData(dialog.querySelector('form')));
  dialog.addEventListener('close',()=>dialog.remove(),{once:true});dialog.showModal();
}
async function submitFollowup(form,fd){
  const dialog=form.closest('dialog'),changes={};
  followupFields.forEach((key,i)=>{const name='followup_'+i,value=String(fd.get(name)||'');if(value!==dialog._baseline[name])changes[key]=value||null;});
  if(!Object.keys(changes).length){closeFollowup();return;}
  await api('/applications/'+dialog.dataset.applicationId+'/followup',{method:'PUT',body:{expected_revision:Number(dialog.dataset.revision),changes}});
  const view=state.view;
  if(view==='detail')state.detail=await api('/applications/'+dialog.dataset.applicationId);
  closeFollowup();await navigate(view);toast('人工跟进已保存，可在列表和新导出的 Excel 中查看。');
}

function assessmentEvidence(ref){
  if(!ref.semantic)return `<blockquote>${E(ref.span_id)}：${E(ref.quote)}</blockquote>`;
  return `<p><b>模型解读：</b>${E(ref.interpretation)}</p><details class="source-evidence"><summary>对应原文 ${E(ref.span_id)}${ref.page?' · 第'+E(ref.page)+'页':''}${ref.source==='ocr'?' · OCR，需对照原件核验':''}</summary><pre class="source-text">${E(ref.quote)}</pre></details>`;
}
function semanticFacts(result){
  if(result?.evidence_mode!=='model_semantic_review')return '';
  const labels={company:'当前公司',education_level:'学历',admission:'招生方式',institution:'毕业院校',experience:'工作年限'};
  const facts=Object.entries(result.facts||{}).map(([key,value])=>`<article class="evidence-item"><h3>${E(labels[key]||key)}：${E(value)}</h3><p class="form-help">模型整理结果</p>${(result.fact_evidence_refs?.[key]||[result.fact_refs?.[key]]).filter(Boolean).map(assessmentEvidence).join('')}</article>`).join('');
  const edu=result.education;
  const education=edu?`<article class="evidence-item"><h3>学历政策判断依据</h3><p>${E(edu.interpretation)}</p>${edu.catalog_name?`<p>模型选择的已核验院校名录条目：${E(edu.catalog_name)}</p>`:''}${(edu.evidence||[]).map(assessmentEvidence).join('')}</article>`:'';
  return `<section class="panel mt"><div class="panel-head"><h2>模型整理的事实与原文</h2></div><div class="panel-body">${facts||'<p class="form-help">没有可确认的事实字段。</p>'}${education}</div></section>`;
}
function detailPage(){
  const a=state.detail,r=a.assessment;
  return `${heading(a.name,a.role_name||'待分类',`<div class="actions">${link(a.file_id,'下载原始PDF')}${btn('申请重新评估','reassess',false,`data-id="${a.id}"`)}</div>`)}${!a.role_id?`<form id="classify-form" class="upload-zone mb"><label class="live-field">确认岗位<select name="role_id" required><option value="">请选择</option>${state.roles.filter(role=>role.active).map(role=>`<option value="${role.id}">${E(role.name)}</option>`).join('')}</select></label><button class="button primary mt-small" type="submit">确认分类</button></form>`:''}<section class="panel"><div class="panel-head"><h2>评估结果</h2>${pill(statuses[a.status]||a.status)}</div><div class="panel-body">${r?`<div class="score-summary"><strong>${r.score}<small> / 100</small></strong>${grade(r)}${r.provisional?pill('暂计分，信息待核验','hold'):''}</div><p class="muted mt-small">${E(r.gate_reason)}</p><div class="score-rows">${Object.entries(r.scores).map(([key,value])=>`<div class="score-row-top"><span>${({hard:'硬性条件',skills:'核心技能',experience:'工作经验',achievements:'项目成果',stability:'稳定性'})[key]}</span><b>${value} / ${({hard:20,skills:30,experience:30,achievements:10,stability:10})[key]}</b></div>`).join('')}</div>${r.evidence_mode==='model_semantic_review'?'<p class="notice mt-small">已完成模型分析与模型语义复核；模型解读和对应原文分开展示，结论仍供人工核验。</p>':''}<p class="form-help">JD版本 ${E(r.jd_version_id)} / 模型 ${E(r.model)} / 原始结果保留在历史记录中。</p>`:`<p>${E(a.document?.error||'尚未完成有效评估。请检查岗位 JD、模型配置和解析结果。')}</p>`}</div></section>${r&&r.items.some(i=>i.id==='location')?'<div class="live-banner mt">此为旧规则的历史评分，可能含地点项。请申请重新评估，再处理待办以采用不考察 base 地的新规则。</div>':''}${nameReviewEditor(a)}${reviewEditor(a)}${semanticFacts(r)}${r?`<section class="panel mt"><div class="panel-head"><h2>逐项证据</h2></div><div class="panel-body">${r.items.map(i=>`<article class="evidence-item"><h3>${E(i.description)} ${pill(i.awarded+' / '+i.points)}</h3><p>要求来源：${i.source==='jd'?'用户已确认的 JD 结构化要求':'全局评分规则'} / 判定：${E(({met:'满足',partial:'部分满足',not_met:'不满足',unknown:'未知'})[i.verdict])}</p><p>${E(i.reason)}</p>${i.evidence.map(assessmentEvidence).join('')}${i.question?`<p><b>建议追问：</b>${E(i.question)}</p>`:''}</article>`).join('')}</div></section>`:''}<section class="panel mt"><div class="panel-head"><h2>人工跟进</h2><span class="read-only-label">人工填写 · 与 Excel 同步</span></div><div class="panel-body"><dl class="manual-list">${Object.entries(a.manual).map(([k,v])=>`<dt>${E(k)}</dt><dd>${E(v??'—')}</dd>`).join('')}</dl><div class="actions">${btn('编辑跟进','followup',false,`data-id="${a.id}"`)}${btn('下载或上传人工跟进表','reports')}</div></div></section><details class="panel mt"><summary class="panel-body">查看解析原文</summary><div class="panel-body">${a.document?a.document.spans.map(s=>`<p class="tiny muted">${E(s.id)} / 第 ${s.page||'—'} 页 / ${s.source==='ocr'?'OCR，关键字段待核验':'文本'}</p><pre class="source-text">${E(s.text)}</pre>`).join(''):'尚未解析。'}</div></details><details class="panel mt"><summary class="panel-body">历史评估（${a.history.length}）</summary><div class="panel-body">${a.history.map(h=>`<p class="tiny muted">${stamp(h.created_at)} / JD ${E(h.jd_version_id)} / ${E(h.id)}</p>`).join('')||'暂无历史评估。'}</div></details>`;
}
function nameReviewEditor(a){
  return `<details class="panel mt" ${a.name_review_required?'open':''}><summary class="panel-body">核对候选人姓名 · ${a.name_confirmed?'已人工核对':a.identity_complete?'大模型已识别':'待大模型识别或人工核对'}</summary><form id="name-review-form" class="panel-body form-stack">${a.name_review_required?'<p class="error-message">当前姓名与原文工作地点重合，可能把城市误当姓名。请先核对，避免脱敏时误删地点证据。</p>':''}<p class="form-help">原始文件名：${E(a.original_name)}</p><p class="form-help">姓名与应聘岗位改由大模型结合来源识别。旧版本可能曾把城市等信息当姓名，可到“姓名与岗位核对”批量修正；这里也可对照原始 PDF 手工核验。评分步骤会移除已识别姓名。保存保留已有评分；需要更新结果时再点击“申请重新评估”。</p>${liveField('已核验的候选人姓名','name',a.name==='待确认'?'':a.name,'text','required maxlength="80"')}${check('我已对照原始 PDF 核对姓名','confirmed')}<button class="button primary" type="submit">保存核验姓名</button><p class="error-message" role="alert"></p></form></details>`;
}
function reviewEditor(a){
  if(!a.document || a.document.status!=='PARSED')return '';
  return `<details class="panel mt"><summary class="panel-body">核验与更正解析文字</summary><form id="document-review-form" class="panel-body form-stack"><p class="form-help">对照原始 PDF 核对全文，尤其是 OCR 学历、日期和院校。保存后同原件申请会加入重新评估；原件、旧文字与历史评分保留。</p>${textarea('已核验文字','text',a.document.spans.map(s=>s.text).join('\n\n'),'required rows="12" maxlength="100000"')}${liveField('本次核验说明','note','','text','required minlength="2" maxlength="1000"')}${check('我已对照原始 PDF 核验，未补写简历没有的信息','confirmed')}<button class="button primary" type="submit">保存核验文字并加入重评</button></form></details>`;
}
function criteriaEditor(r){
  const ds={hard:'硬性条件（合计20）',skills:'核心技能（合计30）',experience:'工作经验（合计30）',achievements:'项目成果（合计10）',stability:'稳定性（合计10）'};
  if(!r.criteria?.length)return '<p class="form-help">初次保存采用默认细则。保存后可在编辑页面逐项修订描述与分值，各维度总分固定。</p>';
  return `<details class="panel"><summary class="panel-body">根据招聘反馈修订评分细则</summary><div class="panel-body form-stack"><p class="form-help">根据筛选与面试反馈修改描述、同维度分值，保存为新的 JD 版本。模型不会自行学习或改权重，已有评分需要显式重评。</p>${r.criteria.map((c,i)=>`<fieldset class="criterion"><legend>${E(ds[c.dimension])} · ${E(c.id)}</legend>${textarea('判定要求','criterion_desc_'+i,c.description,'required maxlength="6000"')}${liveField('分值','criterion_points_'+i,c.points,'number','required min="1" max="30"')}${check('属于硬性条件，不满足时限制推荐等级','criterion_hard_'+i,c.hard)}</fieldset>`).join('')}</div></details>`;
}
function jobsPage(){
  return `${heading('岗位与 JD','管理岗位要求、启用状态及未使用的 JD。历史评分保留原有依据。',`<div class="actions">${btn('手工添加','new-role')}${btn('批量上传并智能解析','jd-imports',true)}</div>`)}<div class="jd-grid">${state.roles.map(r=>`<section class="job-card ${r.active?'':'inactive'}"><div class="job-top"><h2>${E(r.name)}</h2>${pill(r.active?(r.approved?'已确认':'草稿'):'已停用',r.active&&r.approved?'strong':'hold')}</div><p>${r.is_sales?'销售岗位':'非销售岗位'} / ${r.missing.length?'缺失：'+E(r.missing.join('、')):'结构化要求齐备'}</p><div class="job-meta"><span>JD版本 ${E(r.current_version.slice(0,8))} · ${r.application_count} 份申请 · ${r.assessment_count} 条历史评分</span></div><details><summary>查看要求</summary><dl class="role-fields">${Object.entries(r.fields).map(([k,v])=>`<dt>${fieldLabels[k]}</dt><dd>${E(v||'未填写')}</dd>`).join('')}</dl></details><div class="actions mt-small">${btn('编辑并保存新版本','edit-role',false,`data-id="${r.id}"`)}${r.source_file_id?link(r.source_file_id,'下载原始JD'):''}${btn(r.active?'停用':'启用',r.active?'disable-role':'enable-role',false,`data-id="${r.id}"`)}${btn('删除','delete-role',false,`data-id="${r.id}" ${r.can_delete?'':'disabled title="已有关联招聘记录，请使用停用"'}`)}</div><p class="form-help mt-small">${r.can_delete?'尚无关联候选人或评分，可删除此岗位及其 JD 版本。':'已有关联招聘记录，可停用，不能删除。'}${r.active?'':' 已退出新简历匹配；保存 JD 不会自动启用。'}</p></section>`).join('')||`<section class="panel">${empty('先添加第一个岗位','上传 JD，补齐学历、年限、技能等要求。不限制的条件请明确填写“不限”。')}</section>`}</div>`;
}
function roleEditor(){
  const r=state.draft||{name:'',is_sales:false,jd_text:'',fields:{},approved:false};
  return `${heading(r.id?'编辑岗位 JD':'添加岗位 JD','上传原文后确认结构化要求。保存会产生新版本，历史评分不会被改写。',btn('返回岗位列表','jobs'))}${r.active===false?'<div class="live-banner">该岗位已停用，保存新 JD 版本不会自动启用。请在岗位列表中单独启用。</div>':''}<section class="panel panel-body"><form id="jd-upload-form" class="upload-form mb"><label class="live-field">上传 PDF / DOCX / UTF-8 TXT<input name="file" type="file" accept=".pdf,.docx,.txt" required></label><button class="button" type="submit">读取 JD 原文</button></form><form id="role-form" class="form-stack"><div class="form-grid">${liveField('岗位名称','name',r.name,'text','required maxlength="60"')}${check('这是销售岗位，不适用全局本科院校门槛','is_sales',r.is_sales)}<div class="wide">${textarea('JD 原文','jd_text',r.jd_text,'required rows="8" maxlength="100000"')}<div class="jd-analysis-bar">${btn('智能分析','analyze-jd-text',true)}<p class="form-help">根据上方原文填入岗位名称和要求；分析前会提示覆盖。</p></div><p id="jd-analysis-status" class="form-help" role="status" aria-live="polite"></p><details id="jd-analysis-evidence" hidden></details></div>${Object.entries(fieldLabels).map(([key,label])=>textarea(label, key,r.fields[key]||'')).join('')}</div><p class="form-help">工作地点 / base地和到岗时间仅作可选备注，不参与评分或推荐限制，也不影响 JD 完整性。学历、年限、证书、职责和技能需明确；确认不受限制时填写“不限”。你补充的内容将作为已确认的结构化要求，与原始文件分别留存。</p><details><summary>本版初始评分细则</summary><p class="form-help">硬条件20分（学历5、年限12、证书3）；技能30、工作职责经验30、项目成果10、稳定性10。工作地点 / base地和到岗时间均不考察。每个评分条目采用固定档位：满足得满分，部分满足得满分的一半并向下取整，不满足得0分；证据不足暂计0分并标为待核验，不等同于不满足。例如30分条目只能得30、15或0分，5分条目只能得5、2或0分。总分是各条目之和，不限于三个数值。硬条件不符或信息不全时还会限制推荐等级。</p></details>${criteriaEditor(r)}${check('我已核对原文与以上要求，并确认采用本版评分细则；勾选后才允许评估','approved',false)}<div class="actions"><button class="button primary" type="submit">保存 JD 版本</button>${btn('取消','jobs')}</div><div class="error-message" id="role-error" role="alert"></div></form></section>`;
}
async function reportsPage(){
  const [files,feedback]=await Promise.all([api('/reports'),api('/feedback')]);
  return `${heading('报表与反馈','通过网页传递 Excel，无须访问群晖共享目录。')}<div class="notice mb">人工跟进与自动推荐总览分开。上传旧表时只合并你修改的字段；若与较新的反馈冲突，本次全部停止写入。</div><div class="report-steps"><section class="report-step"><h2>1. 下载人工跟进表</h2><p>包含当前全部申请和既有人工内容。跟进字段可在工作台或 Excel 中编辑；原有面试评价和状态备注保留。记录 ID 与表头请保持不变。</p>${btn('下载人工跟进.xlsx','export-followup',true)}</section><section class="report-step"><h2>2. 上传编辑后的表</h2><p>服务器保存原始上传文件，并按下载时的基准做逐字段合并。删掉的行不会删除服务器记录。</p><form id="followup-form"><input name="file" type="file" accept=".xlsx" required aria-label="选择人工跟进表"><button class="button primary" type="submit">校验并导入反馈</button></form></section><section class="report-step"><h2>3. 下载完整推荐总览</h2><p>合并评估结果与最新人工反馈，展示五个跟进字段，同时保留面试评价和原有状态备注，并附逐项证据表。每次生成独立文件。</p>${btn('生成并下载总览','export-overview',true)}</section></div><section class="panel"><div class="panel-head"><h2>反馈观察</h2>${pill('不自动调权')}</div><div class="panel-body"><p class="form-help">${E(feedback.policy)}</p>${feedback.roles.length?feedback.roles.map(g=>`<div class="queue-item"><b>${E(g.role)}</b><p>已评估 ${g.assessed} / 明确反馈 ${g.with_feedback} / 通过 ${g.passed} / 暂无明确结果 ${g.unknown}</p>${g.with_feedback<20?'<span class="badge hold">样本有限，仅供观察</span>':''}</div>`).join(''):'<p class="muted mt-small">暂无已评估申请的人工反馈。</p>'}</div></section><section class="panel mt"><div class="panel-head"><h2>已生成报告</h2></div><div class="table-scroll"><table><thead><tr><th>文件</th><th>时间（北京）</th><th>下载</th></tr></thead><tbody>${files.map(f=>`<tr><td>${E(f.name)}</td><td>${stamp(f.created_at)}</td><td>${link(f.id)}</td></tr>`).join('')||'<tr><td colspan="3" class="empty">暂无报告。</td></tr>'}</tbody></table></div></section>`;
}
async function runsPage(){
  const [runs,events]=await Promise.all([api('/runs'),api('/events')]);
  return `${heading('运行记录','批次、异常与完成的步骤均留有记录。',`<div class="actions">${btn('详细诊断日志','diagnostic-open')}${btn('姓名与岗位核对','identity-review')}${btn('刷新','refresh')}</div>`)}<section class="panel table-scroll"><table class="live-table"><thead><tr><th>批次</th><th>开始时间</th><th>状态</th><th>进度 / 结果</th></tr></thead><tbody>${runs.map(r=>`<tr><td>${E(r.id.slice(0,10))}<p class="tiny muted">${E(r.trigger)}</p>${btn('诊断','diagnostic-open',false,`data-run-id="${E(r.id)}" data-day="${new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date(r.started_at))}"`)}</td><td>${stamp(r.started_at)}</td><td>${pill(statuses[r.status]||r.status,r.status==='BLOCKED'?'hold':'neutral')}</td><td>${r.error?E(r.error):`阶段：${E(diagnosticPhases[r.stage]||r.stage)}；身份核对 ${r.stats.identity_analyzed||0}；评估 ${r.stats.assessed||0}，异常 ${r.stats.exceptions||0}`}</td></tr>`).join('')||'<tr><td colspan="4" class="empty">尚无运行记录。</td></tr>'}</tbody></table></section><section class="panel mt"><div class="panel-head"><h2>处理日志</h2><span class="tiny muted">不包含凭据和简历正文</span></div><div class="panel-body">${events.map(e=>`<div class="queue-item"><b class="tiny">${E(e.code)}</b><p>${E(e.message)}</p><small class="muted">${stamp(e.created_at)}${e.application_id?' / 记录 '+E(e.application_id.slice(0,8)):''}</small></div>`).join('')||'<p class="muted">暂无日志。</p>'}</div></section>`;
}
async function settingsPage(){
  const s=await api('/settings');
  return `${heading('连接与设置','云接口必须先获公司批准；密钥在服务器加密保存，不会回显。')}<form id="settings-form"><section class="panel settings-section"><div class="panel-head"><h2>邮箱</h2></div><div class="panel-body form-grid">${liveField('邮箱账号','mail_account',s.mail.account,'email')}${liveField(`IMAP 授权码${s.mail.password_configured?'（已保存，留空不改）':''}`,'mail_password','','password','autocomplete="new-password"')}${liveField('IMAP 服务器','mail_host',s.mail.host)}${liveField('TLS 端口','mail_port',s.mail.port,'number','min="1" max="65535"')}<label class="live-field">主题关键词规则<select name="subject_mode"><option value="all" ${s.mail.match_mode==='all'?'selected':''}>同时包含“【BOSS直聘】”与“应聘”</option><option value="any" ${s.mail.match_mode==='any'?'selected':''}>包含任一关键词</option></select></label>${check('启用收件箱读取，不标记已读、不移动或删除邮件','mail_enabled',s.mail.enabled)}</div></section><section class="panel settings-section"><div class="panel-head"><h2>公司批准的云模型</h2></div><div class="panel-body form-grid">${liveField('完整 JSON 对话接口地址（HTTPS）','llm_endpoint',s.llm.endpoint,'url','placeholder="https://供应商域名/v1/chat/completions"')}${liveField('模型标识','llm_model',s.llm.model)}${liveField(`API 密钥${s.llm.api_key_configured?'（已保存，留空不改）':''}`,'llm_api_key','','password','autocomplete="new-password"')}${liveField('每批次调用上限','max_calls_per_run',s.llm.max_calls_per_run,'number','min="1" max="200"')}${liveField('模型读等待超时（秒）','llm_timeout_seconds',s.llm.timeout_seconds??300,'number','required min="30" max="600"')}<p class="form-help">默认300秒，可设置30—600秒，指等待下一段响应数据的上限，并非调用总时长。超时后需手动继续处理；关闭连接不代表供应商已停止处理。</p>${modelGenerationSettings(s.llm)}<div class="wide">${textarea('公司批准记录与数据处理限制','approval_note',s.llm.approval_note)}</div><div class="wide">${check('已获准向该供应商发送招聘所需文字和岗位要求（身份识别含姓名，评分移除姓名）；已确认留存与训练条款','llm_approved',s.llm.approved)}</div><p class="form-help wide">身份识别需要发送附件名、邮件主题及含姓名的简历文字；联系方式、证件号等先移除。评分步骤再移除已识别姓名，不上传 PDF 或照片。任职经历仍可能识别人，去标识化并不等于匿名。接口需支持 messages、JSON 格式响应及 choices[0].message.content。</p></div></section><section class="panel settings-section"><div class="panel-head"><h2>学历与评分规则</h2></div><div class="panel-body form-stack">${check('非销售岗位取得硕士后放宽院校要求，不再追溯本科院校；其他特殊学历情况转人工核验','master_bypass',s.policy.master_bypass)}${textarea('已核验的985/211院校名单（每行一所，使用简历中的规范全称）','schools',s.policy.schools.join('\n'),'rows="6"')}${textarea('院校名单来源与版本','school_source',s.policy.school_source)}${check('已确认：非销售本科须统招且在上述名录；销售按岗位标记识别；未知信息待核验，不自动录用或淘汰','policy_confirmed',s.policy.confirmed)}</div></section><section class="panel settings-section"><div class="panel-head"><h2>每日任务</h2></div><div class="panel-body form-grid">${liveField('北京时间','schedule_time',s.schedule.time,'time')}${check('启用每日收件与评估任务（服务器需要保持运行）','schedule_enabled',s.schedule.enabled)}<p class="form-help wide">首次收件回溯24小时，之后从上次成功截止时间继续。服务恢复后，已到计划时间的当日任务会补触发；失败批次请在运行记录核对后手动重试。</p></div></section>${diagnosticSettings(s)}<div class="actions"><button class="button primary" type="submit">保存配置</button>${btn('检查运行条件','preflight')}</div><div id="settings-error" class="error-message" role="alert"></div></form>`;
}
async function render(view=state.view,revision=navigationRevision){
  document.title=`HR Assist · ${titles[view]}`;
  const renderers={diagnostics:diagnosticsPage,home:homePage,overview:overviewPage,candidates:candidatePage,jobs:jobsPage,detail:detailPage,'role-editor':roleEditor,'jd-imports':jdImportsPage,'jd-review':jdReviewPage,'identity-review':identitiesPage,reports:reportsPage,runs:runsPage,settings:settingsPage};
  const html=await renderers[view]();
  if(revision===navigationRevision&&state.view===view&&state.session&&$('#page'))$('#page').innerHTML=html;
}
async function startRun(mode){const run=await api('/runs',{method:'POST',body:{mode,idempotency_key:crypto.randomUUID()}});toast('任务已登记：'+run.id.slice(0,8));await navigate('runs');}
async function downloadExport(path){const f=await api(path,{method:'POST'});const a=document.createElement('a');a.href='/api/files/'+encodeURIComponent(f.id);a.download=f.name;document.body.append(a);a.click();a.remove();toast('文件已生成，正在下载。');}
document.addEventListener('click',async event=>{
  const el=event.target.closest('[data-action]');if(!el)return;
  const action=el.dataset.action;el.disabled=true;
  try{
    if(action==='model-light-preset'||action==='model-default-preset'){applyModelGenerationPreset($('#settings-form'),action==='model-light-preset');toast('已填入表单，保存配置后生效；未调用模型。');return;}
    if(action.startsWith('diagnostic-')){await handleDiagnosticAction(action,el);return;}
    if(action.startsWith('candidate-')){handleCandidateAction(action,el);return;}
    if(action.startsWith('batch-')){await handleBatchFollowupAction(action);return;}
    if(action.startsWith('identity-')){await handleIdentityAction(action);return;}
    if(action==='followup'){openFollowup(el.dataset.id);return;}
    if(action==='close-followup'){closeFollowup();return;}
    if(action==='analyze-jd-text'){await analyzeJDText(el);return;}
    if(['delete-role','disable-role','enable-role'].includes(action)){await handleRoleAction(action,el);return;}
    if(action.startsWith('jd-')){await handleJDAction(action,el);return;}
    if(action==='nav'){await navigate(el.dataset.view);return;}
    if(['settings','runs','jobs','reports'].includes(action)){await navigate(action);return;}
    if(action==='enter'){await navigate('overview');return;}
    if(action==='refresh'){await navigate(state.view);return;}
    if(action==='logout'){await api('/auth/logout',{method:'POST'});state.session=null;showLogin();return;}
    if(action==='process'||action==='mail'){await startRun(action==='mail'?'mail':'pending');return;}
    if(action==='detail'){state.detail=await api('/applications/'+el.dataset.id);await navigate('detail');return;}
    if(action==='new-role'){state.draft=null;await navigate('role-editor',{reload:false});return;}
    if(action==='edit-role'){state.draft=await api('/roles/'+encodeURIComponent(el.dataset.id));await navigate('role-editor',{reload:false});return;}
    if(action==='reassess'){await api('/applications/'+el.dataset.id+'/reassess',{method:'POST'});toast('已保留历史结果并加入待处理，请点击“解析与评估”。');await navigate('candidates');return;}
    if(action==='export-followup'){await downloadExport('/reports/followup/export');return;}
    if(action==='export-overview'){await downloadExport('/reports/overview');await navigate('reports');return;}
    if(action==='preflight'){const result=await api('/settings/preflight',{method:'POST'});toast(result.ready?'配置条件已齐备；真实连接将在执行任务时验证。':'尚需补齐：'+result.missing.join('；'));return;}
    if(action==='read-notice'){await api('/notifications/'+el.dataset.id+'/read',{method:'POST'});await navigate(state.view);}
  }catch(error){toast(errorText(error));}finally{el.disabled=false;}
});
document.addEventListener('submit',async event=>{
  event.preventDefault();const form=event.target,button=form.querySelector('[type=submit]'),fd=new FormData(form);if(button)button.disabled=true;
  try{
    if(form.id==='batch-followup-form'){previewBatchFollowup(form,fd);return;}
    if(form.id==='candidate-followup-form'){await submitFollowup(form,fd);return;}
    if(form.id==='jd-import-form'){await submitJDImports(fd);return;}
    if(form.id==='jd-review-form'){await submitJDReview(fd);return;}
    if(form.id==='login-form'){state.session=await api('/auth/login',{method:'POST',body:Object.fromEntries(fd)});state.session.version='0.1.0';await navigate(titles[location.hash.slice(1)]?location.hash.slice(1):'home');}
    if(form.id==='resume-form'){let count=0,duplicates=0;for(const file of fd.getAll('files')){const payload=new FormData();payload.append('file',file);payload.append('role_id',fd.get('role_id'));const r=await api('/resumes',{method:'POST',body:payload});count++;duplicates+=Number(r.duplicate);}toast(`登记 ${count} 份，重复 ${duplicates} 份。点击“解析与评估”继续。`);await navigate('candidates');}
    if(form.id==='document-review-form'){if(!fd.has('confirmed'))throw new Error('请先勾选原件核验确认。');await api('/applications/'+state.detail.id+'/verify-document',{method:'POST',body:{text:fd.get('text'),note:fd.get('note'),confirmed:true}});toast('核验文字已保存，请执行解析与评估。');await navigate('candidates');}
    if(form.id==='name-review-form'){if(!fd.has('confirmed'))throw new Error('请先对照原始 PDF 核对姓名并勾选确认。');await api('/applications/'+state.detail.id+'/verify-name',{method:'POST',body:{name:String(fd.get('name')).trim(),expected_name:state.detail.name,confirmed:true}});state.detail=await api('/applications/'+state.detail.id);await navigate('detail',{reload:false});toast('姓名已保存，已有评分保留。需更新结果时请申请重新评估，再处理待办。');}
    if(form.id==='classify-form'){await api('/applications/'+state.detail.id+'/classify',{method:'POST',body:{role_id:fd.get('role_id')}});toast('岗位已确认。');await navigate('candidates');}
    if(form.id==='jd-upload-form'){const data=await api('/jd/upload',{method:'POST',body:fd});$('#role-form [name=jd_text]').value=data.text;resetJDTextAnalysis();state.draft=state.draft||{fields:{}};state.draft.source_file_id=data.file_id;toast('已读取 JD 原文，可点击下方“智能分析”自动填写要求。');}
    if(form.id==='role-form'){const fields=Object.fromEntries(Object.keys(fieldLabels).map(k=>[k,String(fd.get(k)||'').trim()]));const body={name:String(fd.get('name')).trim(),is_sales:fd.has('is_sales'),jd_text:fd.get('jd_text'),source_file_id:state.draft?.source_file_id||null,fields,approved:fd.has('approved')};if(state.draft?.criteria?.length)body.criteria=state.draft.criteria.map((c,i)=>({...c,description:String(fd.get('criterion_desc_'+i)),points:Number(fd.get('criterion_points_'+i)),hard:fd.has('criterion_hard_'+i)}));await api('/roles'+(state.draft?.id?'/'+state.draft.id:''),{method:state.draft?.id?'PUT':'POST',body});toast('新的 JD 版本已保存。');await navigate('jobs');}
    if(form.id==='followup-form'){const result=await api('/reports/followup/import',{method:'POST',body:fd});toast(`已更新 ${result.updated_rows} 条人工记录；未上传的行保持不变。`);await navigate('reports');}
    if(form.id==='diagnostic-filter'){diagnosticSelection.day=fd.get('day');diagnosticSelection.runId=fd.get('run_id');await navigate('diagnostics',{reload:false});return;}
    if(form.id==='settings-form'){const body=Object.fromEntries(fd);Object.assign(body,readModelGenerationSettings(fd));for(const key of ['mail_enabled','llm_approved','schedule_enabled','policy_confirmed','master_bypass','diagnostics_enabled'])body[key]=fd.has(key);for(const key of ['mail_port','max_calls_per_run','llm_timeout_seconds','diagnostics_retention_days'])body[key]=Number(fd.get(key));body.schools=String(fd.get('schools')).split(/\r?\n/).map(s=>s.trim()).filter(Boolean);if(!body.mail_password)delete body.mail_password;if(!body.llm_api_key)delete body.llm_api_key;await api('/settings',{method:'PUT',body});toast('配置已保存。');await navigate('settings');}
  }catch(error){const box=form.id==='login-form'?$('#login-error'):form.querySelector('[role=alert]');if(box)box.textContent=errorText(error);else toast(errorText(error));}finally{if(button)button.disabled=false;}
});
document.addEventListener('input',event=>{
  if(event.target.matches('#role-form [name=jd_text]'))resetJDTextAnalysis();
  if(event.target.matches('input[data-candidate-filter]'))changeCandidateFilter(event.target);
});
document.addEventListener('change',event=>{
  if(event.target.name==='batch-candidate')updateFollowupSelection();
  if(event.target.closest('.batch-field'))updateBatchField(event.target);
  if(event.target.matches('select[data-candidate-filter]'))changeCandidateFilter(event.target);
});
window.addEventListener('hashchange',()=>{if(state.session){const view=location.hash.slice(1);if(titles[view]&&!['detail','role-editor','jd-review'].includes(view))navigate(view);}});
async function init(){try{state.session=await api('/auth/session');let view=location.hash.slice(1);if(!titles[view]||['detail','role-editor','jd-review'].includes(view))view='home';await navigate(view);}catch{showLogin();}}
async function pollRunStatus(){
  if(runPollInFlight||!state.session||document.hidden||!['overview','runs'].includes(state.view))return;
  runPollInFlight=true;
  const view=state.view,revision=navigationRevision;
  try{
    const d=await api('/dashboard');
    if(revision!==navigationRevision||state.view!==view||!state.session)return;
    const changed=d.latest_run?.id!==state.dashboard?.latest_run?.id||d.latest_run?.status!==state.dashboard?.latest_run?.status;
    const refresh=changed||d.latest_run?.status==='RUNNING'||state.dashboard?.latest_run?.status==='RUNNING';
    state.dashboard=d;
    if(refresh)await render(view,revision);
  }catch{}finally{runPollInFlight=false;}
}
setInterval(pollRunStatus,8000);
init();
