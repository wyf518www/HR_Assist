'use strict';

// Presentation data only. No network, filesystem, persistent storage, or live scoring.
const candidates = [
  { id:'DEMO-A', name:'示例候选人甲', short:'甲', role:'后端研发工程师', company:'示例科技公司 A', years:5, date:'2026-10-23', scores:[20,27,26,9,6], gate:'pass', advantage:'有数据库优化与服务拆分的具体成果', risk:'需面试核验性能指标与本人贡献', file:'【后端研发工程师_候选人甲_5年】.pdf', jd:'JD-BE-01', education:'统招本科，院校符合示例名录', skill:'数据库性能优化', jdQuote:'具备关系型数据库性能优化经验，能独立定位慢查询问题。', resumeQuote:'负责订单查询索引调整，耗时由 1.8 秒降至 0.5 秒；参与服务拆分。', refs:['J-A-03','R-A-02'], page:'第 2 页', question:'请说明数据量、指标测量方法，以及你在方案中的具体职责。', manual:['2026-10-23','','','','已推荐'] },
  { id:'DEMO-B', name:'示例候选人乙', short:'乙', role:'销售经理', company:'示例商贸公司 B', years:4, date:'2026-10-23', scores:[20,22,23,6,5], gate:'pass', advantage:'客户开发职责明确，披露年度回款成果', risk:'团队管理深度需要进一步核验', file:'【销售经理_候选人乙_4年】.pdf', jd:'JD-SALES-01', education:'按销售岗位 JD 核验，不适用全局院校门槛', skill:'客户开发与销售成果', jdQuote:'具有 3 年以上企业客户销售经历，能够独立开发和维护客户。', resumeQuote:'从事企业客户销售 4 年，负责区域客户开发，年度回款 320 万元。', refs:['J-B-01','R-B-02'], page:'第 1 页', question:'请拆分新增客户与存量客户的回款贡献，并说明你负责的环节。', manual:['2026-10-23','2026-10-26','是','','待面试'] },
  { id:'DEMO-C', name:'示例候选人丙', short:'丙', role:'后端研发工程师', company:'示例软件公司 C', years:6, date:'2026-10-23', scores:[10,29,28,8,7], gate:'fail', advantage:'后端技术栈与项目职责匹配度较高', risk:'本科院校不符合示例学历硬条件', file:'【后端研发工程师_候选人丙_6年】.pdf', jd:'JD-BE-01', education:'统招本科，示例院校不在批准的 985/211 名录', skill:'学历硬条件', jdQuote:'非销售岗位，本科须为统招且毕业院校属于 985 或 211 名录。', resumeQuote:'学历：统招本科，示例普通院校 C。', refs:['J-C-01','R-C-01'], page:'第 1 页', question:'先核对学历证据和适用规则，业务方如有例外意见应单独记录。', manual:['','','','',''] },
  { id:'DEMO-D', name:'示例候选人丁', short:'丁', role:'产品经理', company:'示例服务公司 D', years:3, date:'2026-10-23', scores:[20,12,13,5,6], gate:'pass', advantage:'有需求整理与版本验收经历', risk:'现有证据显示职责范围弱于岗位要求', file:'【产品经理_候选人丁_3年】.pdf', jd:'JD-PM-01', education:'硕士学历，满足示例 JD', skill:'独立负责产品全流程', jdQuote:'独立负责产品从需求研究、方案设计到上线复盘的完整流程。', resumeQuote:'协助产品负责人整理需求文档，参与版本验收与问题跟踪。', refs:['J-D-02','R-D-03'], page:'第 2 页', question:'是否曾独立负责完整项目？需补充可核验材料后再重新评估。', manual:['','','','',''] },
  { id:'DEMO-E', name:'示例候选人戊', short:'戊', role:'数据工程师', company:'示例数据公司 E', years:4, date:'', scores:null, gate:'blocked', pending:'待 JD', advantage:'尚未评估', risk:'JD 缺少核心技能与经验要求', file:'【数据工程师_候选人戊_4年】.pdf', jd:'草稿，未生效', education:'尚未进入评估', manual:['','','','',''] },
  { id:'DEMO-F', name:'示例候选人己', short:'己', role:'待分类', company:'示例咨询公司 F', years:2, date:'', scores:null, gate:'blocked', pending:'待分类', advantage:'尚未评估', risk:'附件名中未找到【岗位_姓名】', file:'简历.pdf', jd:'未绑定', education:'尚未进入评估', manual:['','','','',''] }
];

const names = { home:'工作台首页', overview:'工作概览', candidates:'候选人库', jobs:'岗位与 JD', reports:'报表与反馈', runs:'运行记录', settings:'连接与设置' };
const state = { view:'home', selected:'DEMO-A', search:'', role:'all', grade:'all', detailTab:'evidence', excelSynced:false, excelLocked:true, mergeCompleted:false, feedbackShown:false, simulationRunning:false, simulationDone:false };
const page = document.getElementById('page');
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const scoreOf = c => c.scores ? c.scores.reduce((a,b)=>a+b,0) : null;
function recommendation(c) {
  const n=scoreOf(c);
  if(n === null) return {label:c.pending,css:'neutral'};
  if(n<60) return {label:'不推荐',css:'reject'};
  if(c.gate==='fail'||c.gate==='unknown'||n<70) return {label:'待定',css:'hold'};
  return n>=85 ? {label:'强烈推荐',css:'strong'} : {label:'推荐',css:'recommend'};
}
const badge = c => `<span class="badge ${recommendation(c).css}">${recommendation(c).label}</span>`;
const scoreHTML = c => scoreOf(c) === null ? '<span class="muted">—</span>' : `<span class="score">${scoreOf(c)}<small> / 100</small></span>`;
const button = (label,action,cls='') => `<button class="button ${cls}" data-action="${action}">${label}</button>`;
const heading = (title,description,extra='') => `<div class="page-heading"><div><h1>${title}</h1><p>${description}</p></div>${extra}</div>`;
const person = c => `<button class="candidate-link" data-action="candidate" data-id="${c.id}"><span class="candidate-cell"><span class="avatar">${c.short}</span><span><span class="candidate-name">${c.name}</span><span class="candidate-subtitle" style="display:block">${c.id} · 演示资料</span></span></span></button>`;

function home() {
  const planned=[
    ['档','员工档案','人员资料、合同与证照归集，形成统一的员工信息入口。'],
    ['转','入转调离','入职准备、转正、调岗与离职事项，按流程跟进办理。'],
    ['勤','考勤与休假','汇集考勤与休假记录，核对异常并整理审批事项。'],
    ['薪','薪酬与社保','整理核算资料、变动信息与申报清单，保留核对记录。'],
    ['育','绩效与培训','组织评价与培训计划，跟进反馈、完成情况和相关资料。']
  ];
  return `${heading('人事工作台','选择工作模块，开始今天的人事工作。','<div class="heading-meta">我的工作空间<br>Windows 本机 · 单人使用</div>')}
    <div class="home-section-heading"><h2>功能入口</h2><span class="tiny muted">当前可预览：招聘流程助手</span></div>
    <section class="module-launch" aria-label="招聘功能入口">
      <div class="module-intro"><div class="module-identity"><span class="module-icon" aria-hidden="true">招</span><div><h2>招聘流程助手</h2><span class="badge strong">原型可预览</span></div></div>
        <p class="module-description">从收到一份简历，到形成一份有据可查的推荐。</p><p class="module-detail">集中处理简历收集、岗位匹配和推荐报告，让你专注于核验证据、面试与招聘判断。</p>
        <div class="module-capabilities"><span>邮件收集与归档</span><span>JD 匹配与证据</span><span>推荐报表与反馈</span></div>
        <div class="module-entry">${button('进入招聘助手','goto-overview','primary')}<span>打开招聘工作概览</span></div>
      </div>
      <aside class="module-activity" aria-label="招聘模块演示摘要"><div class="activity-heading"><h3>最近的招聘任务</h3><span class="tiny muted">演示数据</span></div><p class="activity-date">10 月 23 日 · 09:00 批次</p><div class="activity-metrics"><div><strong>6</strong><span>份新增简历</span></div><div><strong>3</strong><span>项待处理异常</span></div></div><p class="activity-note">JD 缺失、岗位待确认、附件无法解析，各 1 项。</p>${button('查看招聘运行记录','goto-runs','subtle')}</aside>
    </section>
    <section class="future-modules" aria-label="后续功能规划"><div class="home-section-heading"><div><h2>后续功能方向</h2><p>以下为规划示意，具体范围按你的实际工作需求确定。</p></div><span class="badge neutral">尚未开发</span></div><div class="planned-grid">${planned.map(([icon,title,description])=>`<article class="planned-module"><div class="planned-title"><span class="planned-icon" aria-hidden="true">${icon}</span><h3>${title}</h3><span class="planned-status">待规划</span></div><p>${description}</p></article>`).join('')}</div></section>
    <div class="home-boundary"><b>同一个工作台，按业务进入</b><p>招聘的候选人、JD 和推荐报告保留在招聘模块内；进入任何模块后，都可以返回工作台首页切换功能。</p></div>`;
}

function overview() {
  return `${heading('工作概览','收集、评估、跟进。每条推荐都能回到原始证据。','<div class="heading-meta">2026 年 10 月 23 日 · 星期五<br>计划每日 09:00（北京时间）· 尚未启用</div>')}
    <section class="run-ribbon" aria-label="最近演示批次">
      <div><div class="ribbon-label"><span class="dot"></span>最近演示批次 / 09:00–09:06</div><h2>6 份简历已整理，3 项异常需要处理</h2><p>4 份已完成评估，2 份等待补充信息。建议等级供人工审核；未评估的简历保留为空分数。</p></div>
      ${button('查看运行明细','goto-runs')}
    </section>
    <section class="metrics" aria-label="演示批次统计">
      <div class="metric"><div class="metric-label">新增简历</div><div class="metric-number">6<small>份</small></div><div class="metric-note">8 封命中邮件 / 1 个重复附件</div></div>
      <div class="metric"><div class="metric-label">成功归档</div><div class="metric-number">5<small>份</small></div><div class="metric-note">另有 1 份简历等待确认岗位</div></div>
      <div class="metric"><div class="metric-label">推荐及以上</div><div class="metric-number">2<small>份申请</small></div><div class="metric-note">强烈推荐 1 / 推荐 1</div></div>
      <div class="metric"><div class="metric-label">岗位推荐报告</div><div class="metric-number">3<small>份</small></div><div class="metric-note">另有 1 份 Excel 总览</div></div>
    </section>
    <div class="section-grid"><section class="panel"><div class="panel-head"><div><h2>今天的推荐依据</h2><p>先看岗位匹配，再核对证据与风险</p></div>${button('全部候选人','goto-candidates','subtle compact')}</div><div class="table-scroll"><table class="compact-table overview-table"><thead><tr><th>候选人 / 岗位</th><th>匹配度</th><th>建议等级</th></tr></thead><tbody>${candidates.slice(0,4).map(c=>`<tr><td>${person(c)}<p class="candidate-subtitle" style="margin-left:46px">${c.role}</p></td><td>${scoreHTML(c)}</td><td>${badge(c)}${c.gate==='fail'?'<p class="candidate-subtitle">硬条件限制等级</p>':''}</td></tr>`).join('')}</tbody></table></div><div class="panel-body" style="padding-top:0"><div class="distribution"><span><i class="legend-dot"></i>强烈推荐<strong>1</strong></span><span><i class="legend-dot blue"></i>推荐<strong>1</strong></span><span><i class="legend-dot amber"></i>待定<strong>1</strong></span><span><i class="legend-dot red"></i>不推荐<strong>1</strong></span><span><i class="legend-dot gray"></i>未评估<strong>2</strong></span></div></div></section>
    <section class="panel"><div class="panel-head"><h2>待我处理</h2><span class="badge hold">3 项异常</span></div><div class="panel-body queue-list">
      <div class="queue-item"><div class="queue-item-top"><b>补齐数据工程师 JD</b><span class="badge hold">暂停该岗位评估</span></div><p>缺少核心技能与经验要求，示例候选人戊暂不评分。</p>${button('查看 JD 缺失项','goto-jobs','subtle compact')}</div>
      <div class="queue-item"><div class="queue-item-top"><b>确认 1 份简历的岗位</b><span class="badge neutral">待分类</span></div><p>“简历.pdf”未包含岗位标识，原件保留在待分类区。</p><button class="button subtle compact" data-action="candidate" data-id="DEMO-F">查看待分类简历</button></div>
      <div class="queue-item"><div class="queue-item-top"><b>1 个 PDF 无法解析</b><span class="badge reject">文件已加密</span></div><p>原件已隔离并登记错误，其他简历继续完成处理。</p>${button('查看异常记录','goto-runs','subtle compact')}</div>
    </div></section></div>
    <div class="notice mt">Excel 中的五个人工字段由你维护。工作台仅同步读取，自动更新和排序均按记录 ID 保留原值。</div>`;
}

function candidateRows() {
  const list=candidates.filter(c=>(state.role==='all'||c.role===state.role)&&(state.grade==='all'||recommendation(c).label===state.grade)&&`${c.name} ${c.company} ${c.role} ${c.id}`.toLowerCase().includes(state.search.toLowerCase()));
  return {items:list,count:list.length,html:list.length ? list.map(c=>`<tr class="${state.selected===c.id?'selected-row':''}"><td>${person(c)}</td><td class="nowrap">${c.role}</td><td>${scoreHTML(c)}</td><td>${badge(c)}</td><td class="nowrap">${c.years} 年</td><td>${c.risk}</td><td><button class="button compact" data-action="candidate" data-id="${c.id}">看依据</button></td></tr>`).join('') : '<tr><td colspan="7" class="empty">没有符合当前筛选的演示申请。请调整岗位、等级或搜索词。</td></tr>'};
}

function detail() {
  const c=candidates.find(x=>x.id===state.selected);
  if(!c) return '';
  const scored=c.scores!==null;
  const gateMessage = !scored ? `${esc(c.risk)}。没有有效评分，不计入“不推荐”。` : c.gate==='fail' ? `匹配度为 ${scoreOf(c)} / 100，但学历硬条件不符，建议等级封顶为“待定”。需要人工核对规则与学历证据。` : '当前示例的硬条件已满足。分数表示 JD 匹配程度，不表示录用成功概率。';
  const manualNames=['推荐给部门日期','面试日期','简历是否通过用人部门筛选','面试评价','当前状态'];
  const right=state.detailTab==='evidence' ? (scored ? `<h3>${c.skill}</h3><div class="evidence-pair"><section class="evidence-box"><div class="evidence-label"><b>JD 要求</b><span>${c.refs[0]}</span></div><blockquote>“${esc(c.jdQuote)}”</blockquote><p class="tiny muted mt-small">${c.jd} · 第 1 页</p></section><section class="evidence-box resume"><div class="evidence-label"><b>简历原文</b><span>${c.refs[1]}</span></div><blockquote>“${esc(c.resumeQuote)}”</blockquote><p class="tiny muted mt-small">演示简历 · ${c.page}</p></section></div><p class="evidence-caption"><b>建议追问：</b>${c.question}</p><div class="notice neutral mt">当前为虚构摘录演示。正式系统将提供完整逐项证据和原件定位。</div>` : `<h3>评估尚未开始</h3><div class="notice warning mt-small">${c.risk}</div><p class="evidence-caption">${c.pending==='待 JD'?'补齐并确认 JD 后，使用同一原件继续评估，无须重复下载。':'确认规范岗位及其 JD 后再继续。不根据简历内容擅自创建岗位。'}</p>${button(c.pending==='待 JD'?'查看岗位与 JD':'查看分类规则',c.pending==='待 JD'?'goto-jobs':'classification-help','subtle mt-small')}`) : `<h3>Excel 人工跟进 <span class="read-only-label">只读镜像</span></h3><dl class="detail-facts" style="grid-template-columns:175px 1fr">${manualNames.map((n,i)=>`<dt>${n}</dt><dd>${esc(c.manual[i])||'<span class="muted">空白（保留原值）</span>'}</dd>`).join('')}</dl><div class="notice neutral mt">请在 Excel 填写。工作台同步时保留原文，不通过模型改写面试评价或当前状态。</div>`;
  return `<section class="detail-panel" aria-label="候选人详情"><div class="detail-heading"><div><h2>${c.name} ${badge(c)}</h2><p>${c.role} / ${c.company} / ${c.years} 年</p></div><div class="detail-score">${scored?scoreOf(c):'—'} <small>${scored?'/ 100':'未评估'}</small></div></div><div class="detail-content"><section><div class="notice ${c.gate==='pass'?'success':'warning'}">${gateMessage}</div>${scored?`<div class="score-rows">${['硬性条件','核心技能','工作经验','项目成果','稳定性'].map((n,i)=>`<div><div class="score-row-top"><span>${n}</span><span>${c.scores[i]} / ${[20,30,30,10,10][i]}</span></div><div class="score-track"><div class="score-fill" style="width:${c.scores[i]/[20,30,30,10,10][i]*100}%"></div></div></div>`).join('')}</div>`:''}<dl class="detail-facts"><dt>学历证据</dt><dd>${c.education}</dd><dt>原始附件</dt><dd>${esc(c.file)}</dd><dt>规则版本</dt><dd>${scored?'演示 v1 · 固定权重':'尚未适用'}</dd></dl></section><section><div class="tabs-inline" role="group" aria-label="详情内容"><button data-action="detail-evidence" class="${state.detailTab==='evidence'?'active':''}" aria-pressed="${state.detailTab==='evidence'}">证据对照</button><button data-action="detail-followup" class="${state.detailTab==='followup'?'active':''}" aria-pressed="${state.detailTab==='followup'}">人工跟进</button></div><div class="mt">${right}</div></section></div><div class="detail-bottom"><span>${c.id} / JD：${c.jd}</span><span>模型：未调用 / 原件：未读取 / 内容：演示</span></div></section>`;
}

function candidatePage() {
  const rows=candidateRows();
  return `${heading('候选人库','一份岗位申请，一条评估记录。筛选后仍可逐项核对证据。','<span class="badge neutral">6 份演示申请</span>')}
    <div class="filter-bar"><label class="field search">搜索候选人、岗位或公司<input id="search" type="search" placeholder="例如：后端、候选人甲、DEMO-A" value="${esc(state.search)}"></label><label class="field">岗位<select id="role-filter"><option value="all">全部岗位</option>${[...new Set(candidates.map(c=>c.role))].map(r=>`<option ${state.role===r?'selected':''}>${r}</option>`).join('')}</select></label><label class="field">建议等级<select id="grade-filter"><option value="all">全部等级及未评估</option>${['强烈推荐','推荐','待定','不推荐','待 JD','待分类'].map(g=>`<option ${state.grade===g?'selected':''}>${g}</option>`).join('')}</select></label><span class="filter-count" id="filter-count">${rows.count} 条结果</span></div>
    <section class="panel"><div class="table-scroll"><table class="candidate-table"><thead><tr><th>候选人</th><th>岗位</th><th>匹配度</th><th>建议等级</th><th>总年限</th><th>主要风险 / 待办</th><th>操作</th></tr></thead><tbody id="candidate-rows">${rows.html}</tbody></table></div></section><div id="candidate-detail">${detail()}</div>`;
}

function jobs() {
  const data=[{name:'后端研发工程师',id:'JD-BE-01',count:2,sales:false,requirements:['学历：按已确认的非销售岗位规则核验。','经验：3 年以上后端研发经历。','核心技能：后端服务、关系型数据库与性能优化。','地点与到岗：按示例 JD；未明确的信息须先确认。']},{name:'销售经理',id:'JD-SALES-01',count:1,sales:true,requirements:['岗位已明确标记为销售，不适用全局本科院校门槛。','经验：3 年以上企业客户销售经历。','职责：客户开发、维护与回款。','成果：要求披露本人负责的业绩及贡献。']},{name:'产品经理',id:'JD-PM-01',count:1,sales:false,requirements:['学历：按已确认的非销售岗位规则核验。','经验：3 年以上产品工作经历。','职责：独立负责从研究到上线复盘。','技能：需求分析、方案设计、跨职能协作。']},{name:'数据工程师',id:'JD-DATA-草稿',count:1,sales:false,missing:true,requirements:['JD 缺失项：核心技能。','JD 缺失项：相关经验要求。','补充后需要确认结构化 JD 才能生效。','当前申请保留原件，不生成分数或等级。']}];
  return `${heading('岗位与 JD','先确认岗位要求，再让每份简历接受同一套规则的评估。','<div class="heading-meta">3 个有效示例 JD<br>1 个草稿等待补充</div>')}<div class="notice mb">销售岗位通过明确标记识别。JD 不完整时停止该岗位评估，其他岗位可继续。</div><div class="jd-grid">${data.map(j=>`<section class="job-card"><div class="job-top"><div><h2>${j.name}</h2><p>${j.id}</p></div><span class="badge ${j.missing?'hold':'strong'}">${j.missing?'JD 缺失项':'已确认 · 示例'}</span></div><p>${j.missing?'当前缺少核心技能和经验要求，无法形成有效评分细则。':'JD 原文、结构化要求与评分规则各自保留版本，重评需显式发起。'}</p><div class="job-meta"><span>${j.count} 份申请</span><span>${j.sales?'销售岗位':'非销售岗位'}</span><span>${j.missing?'评估已暂停':'规则 v1'}</span></div><details ${j.missing?'open':''}><summary>${j.missing?'查看缺失项':'查看示例要求摘要'}</summary><ul class="jd-requirements">${j.requirements.map(x=>`<li>${x}</li>`).join('')}</ul></details></section>`).join('')}</div><section class="panel mt"><div class="panel-head"><h2>附件岗位识别规则</h2><span class="badge neutral">程序执行</span></div><div class="panel-body"><p class="muted" style="font-size:12px">【后端研发工程师_候选人甲_5年】.pdf → 后端研发工程师 → 已确认岗位目录</p><p class="muted mt-small" style="font-size:12px">缺少括号、岗位为空、名称冲突或未知岗位 → 待分类。不会让模型猜测归档。</p></div></section>`;
}

function reports() {
  const feedbackCount=candidates.filter(c=>c.scores&&c.manual[2]).length;
  return `${heading('报表与反馈','Excel 记录你的判断，工作台负责保留原值并建立反馈记录。')}
    <section class="report-strip"><div class="file-label"><span class="file-icon">XLSX</span><div><h2>招聘推荐总览.xlsx</h2><p>D:\\workbuddy\\简历提取\\推荐\\招聘推荐总览.xlsx</p></div></div><div class="actions"><span class="badge neutral">${state.excelSynced?'已模拟同步':'等待读取 Excel'}</span>${button(state.excelSynced?'再次查看同步结果':'模拟读取 Excel 反馈','sync-excel','primary')}</div></section>
    <div class="notice success mb">这五列仅由你在 Excel 编辑。程序通过隐藏的申请 ID 对齐记录，排序不会错配人工内容。</div>
    <section class="panel"><div class="panel-head"><div><h2>人工跟进字段</h2><p>原始值只读展示；空白保持空白</p></div><span class="read-only-label">Excel 是唯一维护入口</span></div><div class="table-scroll"><table class="manual-table"><thead><tr><th>候选人 / 记录 ID</th>${['推荐给部门日期','面试日期','简历是否通过用人部门筛选','面试评价','当前状态'].map(x=>`<th class="manual">${x}</th>`).join('')}</tr></thead><tbody>${candidates.map(c=>`<tr><td><b>${c.name}</b><div class="candidate-subtitle">${c.id}</div></td>${c.manual.map(v=>`<td class="manual">${esc(v)||'<span class="muted">—</span>'}</td>`).join('')}</tr>`).join('')}</tbody></table></div></section>
    ${state.excelSynced?'<div class="notice success mt"><b>模拟同步结果：</b>DEMO-A 的部门筛选结果读取为“是”，面试日期读取为“2026-10-26”，当前状态读取为“待面试”。面试评价仍为空。仅更新页面内存，未读取真实文件。</div>':''}
    <div class="feedback-grid"><section class="panel"><div class="panel-head"><h2>安全写表流程演示</h2><span class="badge ${state.mergeCompleted?'strong':'hold'}">${state.mergeCompleted?'模拟合并完成':'待合并演示'}</span></div><div class="panel-body"><div class="status-inline"><p style="font-size:12px">源工作簿状态：<b>${state.excelLocked?'Excel 占用':'可写（模拟）'}</b></p>${button(state.excelLocked?'模拟关闭 Excel':'模拟打开 Excel','toggle-lock','compact')}</div><div class="notice ${state.excelLocked?'warning':'neutral'} mt">${state.excelLocked?'写入受阻时保留原文件，挂起报告更新。已完成的解析和评估不重做。':'正式系统需要排他写入保护、源文件指纹检查及五字段逐项校验。当前只演示流程。'}</div><div class="actions mt">${button('模拟合并报表','merge-demo','primary')}${state.mergeCompleted?'<span class="tiny muted">6 行 / 人工五字段保持一致</span>':''}</div></div></section>
    <section class="panel"><div class="panel-head"><h2>反馈与规则校准</h2><span class="badge neutral">当前规则 v1</span></div><div class="panel-body"><div class="feedback-stat">${feedbackCount}<small>条有结果的演示反馈</small></div><p class="muted mt-small" style="font-size:12px">按岗位分别观察，空白不算失败。反馈不会自动改变权重或学历规则。</p>${button('查看反馈分析','feedback','subtle mt-small')}${state.feedbackShown?`<div class="notice warning mt">样本不足：当前仅 ${feedbackCount} 条反馈，不能支持可靠的规则调整。保留原规则，继续积累结果；不生成“准确率提高”的结论。</div>`:''}</div></section></div>
    <details class="panel mt"><summary class="panel-body" style="cursor:pointer;color:var(--teal)">总览完整结构与更新约定</summary><div class="panel-body" style="padding-top:0"><p class="muted" style="font-size:12px;line-height:2">岗位名称 / 推荐日期 / 姓名 / 匹配度 / 推荐等级 / 当前公司 / 年限 / 核心优势 / 主要风险 / 建议 / 推荐给部门日期 / 面试日期 / 简历是否通过用人部门筛选 / 面试评价 / 当前状态</p><p class="muted mt-small" style="font-size:12px">推荐日期降序、岗位名称升序，空日期放最后。评分和人工状态分开，原有人工值随整行移动。</p></div></details>`;
}

function runs() {
  const steps=[['09:00:00','读取配置与成功游标','窗口：2026-10-22 09:00 至 2026-10-23 09:00，北京时间；这是固定的演示窗口。'],['09:00:03','登记 8 封命中邮件','仅收件箱。示例按主题同时包含“【BOSS直聘】”和“应聘”过滤。'],['09:00:12','处理 8 个 PDF 附件','1 个附件重复；7 个新附件中，1 个加密隔离，6 份成功解析。'],['09:01:28','归档与 JD 检查','5 份按岗位归档；1 份待分类。已归档简历中有 1 份等待 JD。'],['09:05:21','完成 4 份示例评估','强烈推荐 1、推荐 1、待定 1、不推荐 1。学历硬条件对高分申请限制等级。'],['09:06:02','提交报告并登记异常','3 份岗位报告、1 份总览；3 项异常已登记。示例批次状态：完成但有异常。']];
  return `${heading('运行记录','失败可以定位到步骤，重跑复用已经完成的结果。',button(state.simulationRunning?'正在演示…':'模拟重复运行','simulate-run','primary'))}
    <div class="notice neutral mb">本页均为模拟事件。不会连接邮箱或生成真实报告，重复运行不会增加演示候选人。</div>
    ${state.simulationRunning||state.simulationDone?`<div class="panel panel-body mb"><b>重复运行演示</b><p id="simulation-status" class="sim-message">${state.simulationDone?'完成：复用 4 份评估，跳过已登记附件；候选人仍为 6 份。':'正在检查已登记的演示附件…'}</p><div class="progress-line"><span id="simulation-progress" style="width:${state.simulationDone?100:15}%"></span></div></div>`:''}
    <div class="section-grid"><section class="panel"><div class="panel-head"><div><h2>批次 RUN-DEMO-1023</h2><p>触发方式：09:00 计划任务（示例）</p></div><span class="badge hold">完成，有 3 项异常</span></div><div class="panel-body"><ol class="timeline">${steps.map(s=>`<li><span class="timeline-time">${s[0]}</span><span class="timeline-mark" aria-hidden="true"></span><div><h3>${s[1]}</h3><p>${s[2]}</p></div></li>`).join('')}</ol></div></section><section class="panel"><div class="panel-head"><h2>异常记录</h2><span class="code-label">3 events</span></div><div class="panel-body"><div class="queue-item"><b class="tiny">E-PDF-ENCRYPTED</b><p>1 个 PDF 需要密码。原件保留在异常附件区，不尝试破解。</p><span class="badge reject mt-small">单项隔离</span></div><div class="queue-item"><b class="tiny">E-ROLE-UNKNOWN</b><p>DEMO-F 的附件名没有岗位标识。等待人工分类。</p><span class="badge hold mt-small">等待确认</span></div><div class="queue-item"><b class="tiny">E-JD-INCOMPLETE</b><p>数据工程师 JD 缺少关键项。DEMO-E 不评分。</p><span class="badge hold mt-small">暂停该岗位评估</span></div></div></section></div>
    <div class="notice success mt">本次处理 8 封邮件，新增 6 份简历，生成 3 份岗位推荐报告，异常 3 条。（虚构演示摘要）</div>`;
}

function settings() {
  const section=(title,rows,status='')=>`<section class="panel"><div class="panel-head"><h2>${title}</h2>${status}</div><div class="panel-body"><dl>${rows.map(r=>`<div class="setting-row"><dt>${r[0]}</dt><dd>${r[1]}</dd></div>`).join('')}</dl></div></section>`;
  return `${heading('连接与设置','配置来自本次需求。真实运行前先检查连接、规则和保存位置。',button('查看上线前检查项','preflight','primary'))}<div class="notice warning mb">当前为设计原型，真实连接和计划任务均未启用。公司批准的云模型接口尚待填写。</div><div class="settings-grid">
    ${section('邮箱连接',[['邮箱账户','your-account@qq.com'],['收取方式','IMAP over TLS / 仅 INBOX'],['读取权限','只读取件，不改变未读状态，不移动或删除邮件'],['授权信息','尚未配置；生产版使用 Windows 凭据保护'],['主题匹配','暂按两关键词同时包含，正式启用前确认']],'<span class="badge hold">未连接</span>')}
    ${section('大模型',[['部署方式','公司批准的云端接口'],['供应商 / 模型','尚未指定'],['发送内容','去标识化的必要文本与 JD，不上传原 PDF'],['数据用途','仅招聘评估，不训练或微调模型'],['启用条件','接口、公司批准记录、留存条款与预算齐备']],'<span class="badge hold">待配置</span>')}
    ${section('目录与报告',[['原件根目录','<span class="path">D:\\workbuddy\\简历提取</span>'],['JD 目录','<span class="path">D:\\workbuddy\\简历提取\\岗位JD</span>'],['推荐目录','<span class="path">D:\\workbuddy\\简历提取\\推荐</span>'],['日志目录','<span class="path">D:\\workbuddy\\简历提取\\日志</span>'],['人工字段','仅在 Excel 维护，工作台读取镜像']])}
    ${section('运行与通知',[['计划时间','每日北京时间 09:00'],['漏跑恢复','恢复运行后补处理；沿用上次成功截止时间'],['互斥控制','手动与定时任务不能并发写入'],['通知方式','工作台通知中心 + Windows 本机通知'],['对外发送','未启用；默认不发送候选人信息']],'<span class="badge neutral">计划未启用</span>')}
    </div><section class="panel mt" id="preflight-section"><div class="panel-head"><h2>上线前需要补齐</h2><span class="badge neutral">设计确认点</span></div><div class="panel-body"><ul class="checklist"><li>邮箱授权码，以及主题 AND / OR 与附件命名样本。</li><li>公司批准的模型、接口、数据处理限制和预算。</li><li>岗位清单、销售标志、学历特殊情况与 JD 评分细则。</li><li>既有 Excel 模板、资料保留期限与备份位置。</li></ul><p class="tiny muted mt-small">生产软件将先完成预检，再允许启用真实任务。此原型不接受凭据。</p></div></section>`;
}

function render() {
  const atHome=state.view==='home';
  document.title=atHome?'HR Assist · 人事工作台原型':`HR Assist · 招聘助手 · ${names[state.view]}`;
  document.getElementById('workspace-icon').textContent=atHome?'全':'招';
  document.getElementById('workspace-name').textContent=atHome?'全部人事功能':'招聘流程助手';
  document.getElementById('workspace-description').textContent=atHome?'选择工作模块':'招聘业务模块';
  document.getElementById('breadcrumb').innerHTML=atHome?'人事工作台 <span>/</span> <b>首页</b>':`<a href="#home">人事工作台</a><span>/</span><a href="#overview">招聘流程助手</a><span>/</span><b>${names[state.view]}</b>`;
  const navItem=(id,label,icon)=>`<button class="nav-item ${state.view===id?'active':''}" data-view="${id}" ${state.view===id?'aria-current="page"':''}><span class="nav-glyph" aria-hidden="true">${icon}</span>${label}${id==='candidates'?'<span class="nav-number">6</span>':''}</button>`;
  document.getElementById('main-nav').innerHTML=navItem('home','工作台首页','⌂')+'<div class="nav-section-label">'+(atHome?'业务模块':'招聘流程助手')+'</div>'+(atHome?navItem('overview','招聘流程助手','▣'):[['overview','工作概览','▦'],['candidates','候选人库','▤'],['jobs','岗位与 JD','▣'],['reports','报表与反馈','▥'],['runs','运行记录','◷'],['settings','连接与设置','⚙']].map(x=>navItem(...x)).join(''));
  document.getElementById('main-nav').classList.toggle('home-nav',atHome);
  page.innerHTML=({home,overview,candidates:candidatePage,jobs,reports,runs,settings}[state.view])();
}
function navigate(view) { if(!names[view])return;state.view=view;if(location.hash!==`#${view}`)history.pushState(null,'',`#${view}`);render();window.scrollTo(0,0); }
let toastTimer;
function toast(message) { const el=document.getElementById('toast');el.textContent=message;el.hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(()=>{el.hidden=true;},5500); }
function updateTable() { let r=candidateRows();if(!r.items.some(c=>c.id===state.selected)){state.selected=r.items[0]?.id||null;r=candidateRows();}document.getElementById('candidate-rows').innerHTML=r.html;document.getElementById('filter-count').textContent=`${r.count} 条结果`;renderDetail(); }
function renderDetail() { document.getElementById('candidate-detail').innerHTML=detail(); }
function simulateRun() {
  if(state.simulationRunning)return;
  state.simulationRunning=true;state.simulationDone=false;render();
  const steps=['已识别演示邮件与附件，正在检查已有结果…','已复用 4 份示例评估；正在检查人工字段保护…','完成：复用 4 份评估，跳过已登记附件；候选人仍为 6 份。'];
  steps.forEach((text,i)=>setTimeout(()=>{const s=document.getElementById('simulation-status'),p=document.getElementById('simulation-progress');if(s)s.textContent=text;if(p)p.style.width=`${(i+1)/3*100}%`;if(i===2){state.simulationRunning=false;state.simulationDone=true;if(state.view==='runs')render();toast('重复运行演示完成：没有新增或改写任何真实资料。');}},450*(i+1)));
}

document.addEventListener('click',event=>{
  const nav=event.target.closest('[data-view]');if(nav){navigate(nav.dataset.view);return;}
  const el=event.target.closest('[data-action]');if(!el)return;
  const action=el.dataset.action;
  if(action.startsWith('goto-')) {navigate(action.slice(5));return;}
  switch(action) {
    case 'candidate': state.selected=el.dataset.id;state.detailTab='evidence';if(state.view!=='candidates'){state.search='';state.role='all';state.grade='all';navigate('candidates');}else{updateTable();renderDetail();}document.getElementById('candidate-detail').scrollIntoView({block:'nearest'});break;
    case 'detail-evidence': state.detailTab='evidence';renderDetail();break;
    case 'detail-followup': state.detailTab='followup';renderDetail();break;
    case 'sync-excel': state.excelSynced=true;candidates[0].manual=['2026-10-23','2026-10-26','是','','待面试'];render();toast('已展示虚构 Excel 变更；仅修改页面内存，没有读取或写入文件。');break;
    case 'toggle-lock': state.excelLocked=!state.excelLocked;state.mergeCompleted=false;render();break;
    case 'merge-demo': if(state.excelLocked){toast('模拟写入已阻止：Excel 正在占用。请先点击“模拟关闭 Excel”。');}else{state.mergeCompleted=true;render();toast('模拟合并完成：人工五字段原样保留，没有写入真实 Excel。');}break;
    case 'feedback': state.feedbackShown=true;render();break;
    case 'simulate-run': simulateRun();break;
    case 'preflight': document.getElementById('preflight-section').scrollIntoView({block:'center'});toast('尚缺真实连接、规则和报表配置；原型不能启用计划任务。');break;
    case 'classification-help': toast('从附件名第一段【】中取首个下划线前的岗位；无有效名称则等待人工确认。');break;
  }
});
document.addEventListener('input',event=>{if(event.target.id==='search'){state.search=event.target.value;updateTable();}});
document.addEventListener('change',event=>{if(event.target.id==='role-filter'){state.role=event.target.value;updateTable();}if(event.target.id==='grade-filter'){state.grade=event.target.value;updateTable();}});
window.addEventListener('hashchange',()=>{const view=location.hash.slice(1)||'home';if(names[view]){state.view=view;render();}});
const initialView=location.hash.slice(1);if(names[initialView])state.view=initialView;
render();
