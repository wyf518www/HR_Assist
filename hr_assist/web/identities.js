'use strict';
let identityRows=[];
function identitySource(ref){return ref.span_id.startsWith('F')?'附件名':ref.span_id.startsWith('M')?'邮件主题':'简历 '+ref.span_id;}
function identityEvidence(ref){
  const label=identitySource(ref)+(ref.page?' · 第'+ref.page+'页':'')+(ref.source==='ocr'?' · OCR':'');
  if(ref.semantic===true)return `<div class="identity-source"><p><b>模型解读：</b>${E(ref.interpretation)}</p><details><summary>查看对应原文 · ${E(label)}</summary><blockquote>${E(ref.quote)}</blockquote></details></div>`;
  return `<blockquote><small>${E(label)} · 历史引文</small><br>${E(ref.quote)}</blockquote>`;
}
async function identitiesPage(){
  identityRows=await api('/identities');
  return `${heading('姓名与岗位核对','先查看模型依据，再确认修正。人工跟进和历史评分保留。',btn('返回候选人库','identity-back'))}
  <div class="notice mb">使用已批准的大模型理解附件名、邮件主题和简历文字，不按文件命名模板拆分。姓名识别会发送含姓名的文字；电话、邮箱和证件号码先移除。旧版本未保留邮件主题时，只使用现有附件名和简历文字。</div>
  <div class="actions mb">${btn('全选','identity-select-all')}${btn('取消选择','identity-select-none')}${btn('大模型分析所选','identity-analyze',true)}${btn('重新分析所选','identity-reanalyze')}${btn('确认修正所选','identity-apply')}${btn('刷新建议','identity-refresh')}</div>
  <p class="form-help mb">每份新分析由大模型先分析、再对照原始资料复核，通常调用两次；若分析草稿已保存，继续时只调用复核。模型分析不改动旧记录，次数受每批次调用上限限制。有冲突、无对应岗位或 OCR 依据时请人工核对。确认更正姓名或岗位后，旧评分转入历史，需手动处理待办以重评。手工核验过的值默认保留，可对照原件自行修改。</p>
  <section class="panel table-scroll"><table class="live-table identity-table"><thead><tr><th>选择 / 当前记录</th><th>原始附件名</th><th>模型识别与依据</th><th>确认后的姓名与岗位</th></tr></thead><tbody>${identityRows.map((a,index)=>{
    const p=a.proposal,r=p?.result,applied=a.proposal_applied,valid=p&&!a.proposal_stale&&!applied;
    const name=a.name_locked?a.name:(r?.name.value||(!['待确认','待识别'].includes(a.name)?a.name:''));
    const role=a.role_locked?a.role_id:(r?.role.role_id||a.role_id||'');
    const evidence=r?['name','role'].map(k=>`<div class="identity-evidence"><b>${k==='name'?'姓名':'应聘岗位'}：${E(r[k].value||'未确定')}</b> ${pill(r[k].status==='identified'?(p.evidence_mode==='model_semantic_review'?'模型判断明确':'历史原文依据'):'待核验',r[k].status==='identified'?'neutral':'hold')}<p>${E(r[k].note)}</p>${r[k].evidence.map(identityEvidence).join('')}</div>`).join(''):'';
    return `<tr data-identity-id="${a.id}"><td><label class="check-field"><input type="checkbox" name="identity-selection" value="${a.id}"><span><b>${E(a.name)}</b><br>${E(a.role_name||'待识别岗位')}</span></label><p class="tiny muted mt-small">${E(statuses[a.status]||a.status)}</p>${a.name_locked||a.role_locked?pill('含人工核验字段'):''}</td><td><p class="identity-filename">${E(a.original_name)}</p>${link(a.file_id,'下载原件')}</td><td>${p?`${a.proposal_pending?pill('新分析待完成，请继续核对任务','hold'):applied?pill('已应用','strong'):a.proposal_stale?pill('记录或分析版本已变更，请重新分析','hold'):''}${evidence}<small class="muted">${E(p.model)} / ${stamp(p.created_at)} / ${p.evidence_mode==='model_semantic_review'?'模型分析与复核':'历史分析结果'}</small>`:'尚未分析；选中此行后点击“大模型分析所选”。'}</td><td>${valid?`${liveField('确认姓名 / 原文称呼','identity-name-'+index,name,'text','maxlength="80"')}<label class="live-field mt-small">确认应聘岗位<select name="identity-role-${index}"><option value="">请选择对应岗位</option>${state.roles.filter(x=>x.active).map(x=>`<option value="${x.id}" ${x.id===role?'selected':''}>${E(x.name)}</option>`).join('')}</select></label><p class="form-help mt-small">保留原记录 ID 和人工跟进；请先核对依据再勾选确认。</p>`:a.proposal_pending?'分析或复核尚未完成；点击“大模型分析所选”继续。旧记录保留。':applied?'已应用本次核对建议。':a.proposal_stale?'重新分析后再确认。':'分析完成后显示确认项。'}</td></tr>`;
  }).join('')||'<tr><td colspan="4" class="empty">暂无候选人记录。</td></tr>'}</tbody></table></section>`;
}
async function handleIdentityAction(action){
  if(action==='identity-back'){await navigate('candidates');return;}
  if(action==='identity-review'||action==='identity-refresh'){await navigate('identity-review');return;}
  if(action==='identity-select-all'||action==='identity-select-none'){document.querySelectorAll('[name=identity-selection]').forEach(x=>x.checked=action==='identity-select-all');return;}
  const ids=[...document.querySelectorAll('[name=identity-selection]:checked')].map(x=>x.value);
  if(!ids.length)throw new Error('请先勾选需要处理的记录。');
  if(ids.length>500)throw new Error('一次最多选择500条记录。');
  if(action==='identity-analyze'||action==='identity-reanalyze'){
    if(!confirm(`将调用已批准的大模型分析并复核所选 ${ids.length} 条记录，每份新分析通常调用两次，可能产生接口费用。姓名识别会发送包含姓名的资料，电话、邮箱和证件号码先移除。本次只生成建议，不修改记录。继续吗？`))return;
    const run=await api('/runs',{method:'POST',body:{mode:'identity',application_ids:ids,force_identity:action==='identity-reanalyze',idempotency_key:crypto.randomUUID()}});
    toast('核对任务已登记：'+run.id.slice(0,8)+'。完成后返回“姓名与岗位核对”。');await navigate('runs');return;
  }
  if(action==='identity-apply'){
    const items=ids.map(id=>{const index=identityRows.findIndex(x=>x.id===id),a=identityRows[index];
      if(!a?.proposal||a.proposal_stale||a.proposal_applied)throw new Error('所选记录包含未分析、已应用或已变更的建议，请重新选择。');
      const name=document.querySelector(`[name="identity-name-${index}"]`).value.trim(),role_id=document.querySelector(`[name="identity-role-${index}"]`).value;
      if(!name||!role_id)throw new Error('请为所选记录填写已核对的姓名和应聘岗位。');
      return {application_id:id,proposal_id:a.proposal.id,name,role_id};});
    if(!confirm(`确认已核对所选 ${items.length} 条记录的姓名与应聘岗位？更正会保留人工跟进和历史评分，变更后的记录等待重新评估。本次不会调用评分模型。`))return;
    const result=await api('/identities/apply',{method:'POST',body:{confirmed:true,items}});
    toast(`已确认 ${result.updated} 条记录；需要更新评分时请处理待办。`);await navigate('identity-review');
  }
}
