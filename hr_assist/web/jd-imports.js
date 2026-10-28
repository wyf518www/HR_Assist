'use strict';
const jdUI={items:[],review:null,uploading:false,uploads:[],polling:false,analyzingText:false};
const jdStates={QUEUED:'排队中',EXTRACTING:'读取原文',ANALYZING:'模型解析中',READY:'待核对',FAILED:'解析失败',INTERRUPTED:'中断待重试',SAVED:'已保存',ROLE_DELETED:'关联岗位已删除'};

function confirmRoleAction(role,action){
  const deleting=action==='delete-role',enabling=action==='enable-role';
  const verb=deleting?'删除':enabling?'启用':'停用';
  const description=deleting?`将删除此岗位及其 ${role.version_count} 个 JD 版本，删除后无法在页面恢复。仅当没有关联候选人或评分时才能删除。上传原件和导入日志会保留。`:
    enabling?'此岗位将重新参与新简历匹配。已有待处理简历需手动继续处理，历史评分保持不变。':
    '此岗位将停止参与新简历匹配，也不再接受新申请。新收到的同名岗位简历保留在待分类区；已有未评估记录暂停评估。历史 JD、评分、报表和人工跟进保留，可随时重新启用。';
  return new Promise(resolve=>{
    const dialog=document.createElement('dialog');
    dialog.className='jd-confirm-dialog';
    dialog.setAttribute('aria-labelledby','role-action-title');
    dialog.setAttribute('aria-describedby','role-action-description');
    dialog.innerHTML=`<h2 id="role-action-title">确认${verb}岗位“${E(role.name)}”？</h2><p id="role-action-description">${E(description)}</p><div class="actions"><button type="button" class="button" data-role-confirm="cancel" autofocus>取消</button><button type="button" class="button ${deleting?'danger':'primary'}" data-role-confirm="accept">确认${verb}</button></div>`;
    const finish=accepted=>{dialog.close();dialog.remove();resolve(accepted);};
    dialog.querySelector('[data-role-confirm=cancel]').addEventListener('click',()=>finish(false),{once:true});
    dialog.querySelector('[data-role-confirm=accept]').addEventListener('click',()=>finish(true),{once:true});
    dialog.addEventListener('cancel',event=>{event.preventDefault();finish(false);},{once:true});
    document.body.append(dialog);dialog.showModal();
  });
}

async function handleRoleAction(action,button){
  const role=state.roles.find(r=>r.id===button.dataset.id);
  if(!role)throw new Error('岗位列表已变化，请刷新后重试。');
  if(action==='delete-role'&&!role.can_delete)throw new Error('岗位已关联招聘记录，请使用停用。');
  if(!await confirmRoleAction(role,action))return;
  const body={confirmed:true,expected_version:role.current_version};
  if(action==='delete-role')await api('/roles/'+role.id,{method:'DELETE',body});
  else await api('/roles/'+role.id+'/status',{method:'POST',body:{...body,active:action==='enable-role'}});
  toast(action==='delete-role'?'岗位和未使用的 JD 版本已删除。':action==='enable-role'?'岗位已启用。':'岗位已停用，历史记录已保留。');
  await navigate('jobs');
}

function confirmJDTextOverwrite(){
  return new Promise(resolve=>{
    const dialog=document.createElement('dialog');
    dialog.className='jd-confirm-dialog';
    dialog.setAttribute('aria-labelledby','jd-overwrite-title');
    dialog.setAttribute('aria-describedby','jd-overwrite-description');
    dialog.innerHTML=`<h2 id="jd-overwrite-title">智能分析将覆盖已填写内容</h2>
      <div id="jd-overwrite-description"><p>将调用已批准的模型，根据当前 JD 原文重新填入：岗位名称、学历、经验年限、地点、证书、岗位职责、核心技能和到岗时间备注。</p>
      <p><b>未在原文中识别到的项目会清空。</b>分析成功后会取消“已核对”勾选，需要重新确认。</p>
      <p>JD 原文、销售岗位标记和评分权重保持不变。仅回填当前表单，点击“保存 JD 版本”后才会保存。</p></div>
      <div class="actions"><button type="button" class="button" data-jd-dialog="cancel" autofocus>取消</button><button type="button" class="button primary" data-jd-dialog="confirm">确认覆盖并分析</button></div>`;
    const finish=accepted=>{dialog.close();dialog.remove();resolve(accepted);};
    dialog.querySelector('[data-jd-dialog=cancel]').addEventListener('click',()=>finish(false),{once:true});
    dialog.querySelector('[data-jd-dialog=confirm]').addEventListener('click',()=>finish(true),{once:true});
    dialog.addEventListener('cancel',event=>{event.preventDefault();finish(false);},{once:true});
    document.body.append(dialog);dialog.showModal();
  });
}

function jdFormSnapshot(form){
  return JSON.stringify(Array.from(form.elements).filter(el=>el.name).map(el=>[el.name,el.type==='checkbox'?el.checked:el.value]));
}

function resetJDTextAnalysis(){
  const form=$('#role-form');
  if(!form)return;
  const evidence=form.querySelector('#jd-analysis-evidence');
  evidence.hidden=true;evidence.innerHTML='';
  const status=form.querySelector('#jd-analysis-status');
  status.classList.remove('jd-error');
  if(status.textContent)status.textContent='JD 原文已修改，请重新智能分析或手工核对填写内容。';
}

async function analyzeJDText(button){
  const form=$('#role-form');
  if(!form||jdUI.analyzingText)return;
  const text=form.elements.jd_text.value;
  const status=form.querySelector('#jd-analysis-status');
  if(!text.trim())throw new Error('请先填写或上传 JD 原文。');
  if(text.length>100000)throw new Error('JD 原文不能超过100000字，请精简后重试。');
  if(!await confirmJDTextOverwrite())return;
  const snapshot=jdFormSnapshot(form);
  const controls=Array.from(document.querySelectorAll('#role-form input,#role-form textarea,#role-form select,#role-form button,#jd-upload-form input,#jd-upload-form button'));
  const disabled=controls.map(el=>el.disabled);
  controls.forEach(el=>{el.disabled=true;});
  jdUI.analyzingText=true;
  button.textContent='正在智能分析…';
  status.classList.remove('jd-error');
  status.textContent='正在分析 JD 原文，请稍候。失败时会保留当前已填写内容。';
  form.querySelector('[role=alert]').textContent='';
  try{
    const result=await api('/jd/analyze-text',{method:'POST',body:{text,confirmed:true}});
    if(!form.isConnected||state.view!=='role-editor'){
      toast('分析已完成，但页面已切换，未覆盖其他页面的内容。');return;
    }
    if(jdFormSnapshot(form)!==snapshot){
      status.textContent='分析期间表单发生变化，本次结果未覆盖；请核对当前原文后重新分析。';return;
    }
    const draft=result.draft;
    form.elements.name.value=draft.name||'';
    for(const key of Object.keys(fieldLabels))form.elements[key].value=draft.fields[key]||'';
    form.elements.approved.checked=false;
    status.textContent='已回填，尚未保存。'+(draft.missing.length?'缺失项：'+draft.missing.join('、')+'。':'')+(draft.warnings||[]).join(' ');
    const evidence=form.querySelector('#jd-analysis-evidence');
    evidence.hidden=false;
    evidence.innerHTML=`<summary>查看本次智能分析的原文依据</summary><p class="form-help">${E(result.model)} · 请核对各项引文与填写内容。默认评分说明会在保存时随要求更新，自定义细则请自行核对。</p>${Object.entries({name:'岗位名称',...fieldLabels}).map(([key,label])=>`<div class="jd-evidence"><b>${E(label)}</b>${(draft.evidence[key]||[]).map(ref=>`<blockquote>${E(ref.quote)}</blockquote>`).join('')||`<p class="form-help">${['availability','location'].includes(key)?'原文未提及，可留空。':'原文未找到依据，已留空。'}</p>`}</div>`).join('')}`;
    toast('智能分析完成，请核对填入内容后保存 JD 版本。');
  }catch(error){
    if(form.isConnected){status.classList.add('jd-error');status.textContent='分析失败：'+errorText(error)+' 已填写内容保持不变。';form.querySelector('[role=alert]').textContent=errorText(error);}
    else toast(errorText(error));
  }finally{
    jdUI.analyzingText=false;
    controls.forEach((el,index)=>{el.disabled=disabled[index];});
    button.textContent='智能分析';
  }
}

async function jdImportsPage(){
  const [items,config]=await Promise.all([api('/jd/imports'),api('/settings')]);
  jdUI.items=items;
  const ready=config.llm.approved&&config.llm.api_key_configured;
  return `${heading('批量导入 JD','每份文件对应一个岗位。上传后在后台逐份解析，核对要求后保存。',btn('返回岗位列表','jobs'))}
    ${ready?'':`<div class="live-banner">请先配置并批准大模型接口，再开始智能解析。${btn('连接与设置','settings')}</div>`}
    <section class="panel panel-body jd-upload-panel"><form id="jd-import-form" class="form-stack">
      <label class="live-field">选择岗位文件（可多选）<input name="files" type="file" accept=".pdf,.docx,.txt" multiple required ${jdUI.uploading?'disabled':''}></label>
      <div class="status-inline"><p class="form-help">支持 PDF、DOCX、UTF-8 TXT。每批最多20份，每份不超过20MB。扫描 PDF 自动尝试 OCR。</p><button class="button primary" type="submit" ${!ready||jdUI.uploading?'disabled':''}>上传并智能解析</button></div>
      <p class="form-help">使用“连接与设置”中批准的模型，每份新文件调用一次；重试需手动触发。同一文件再次上传会复用导入记录。</p>
      <div id="jd-upload-results" role="status" aria-live="polite">${jdUploadResults()}</div>
      <div class="error-message" role="alert"></div>
    </form></section>
    <section class="panel"><div class="panel-head"><div><h2>导入记录</h2><p class="form-help">已上传文件会继续处理，可离开页面后回来核对。显示最近300条，未保存记录优先。</p></div>${btn('刷新进度','jd-refresh')}</div><div id="jd-import-list">${jdImportList()}</div></section>`;
}

function jdUploadResults(){
  if(!jdUI.uploads.length)return '';
  return `<ul class="jd-upload-results">${jdUI.uploads.map(x=>`<li class="${x.failed?'jd-error':''}"><span>${E(x.name)}</span><span>${E(x.message)}</span></li>`).join('')}</ul>`;
}

function jdImportList(){
  if(!jdUI.items.length)return empty('选择文件，批量建立岗位草稿','模型会提取岗位名称和五项评分要求；工作地点/base地和到岗时间仅作可选备注。原文未提及的条件留空，供你核对补充。');
  return `<div class="table-wrap"><table class="compact-table jd-import-table"><thead><tr><th scope="col">JD 文件 / 岗位</th><th scope="col">进度</th><th scope="col">核对提示</th><th scope="col">操作</th></tr></thead><tbody>${jdUI.items.map(r=>`<tr><td><b>${E(r.name||r.filename)}</b>${r.name?`<small>${E(r.filename)}</small>`:''}<small>${stamp(r.created_at)}</small></td><td>${pill(jdStates[r.status]||r.status,r.status==='SAVED'?'strong':r.status==='READY'?'hold':r.status==='FAILED'?'reject':'neutral')}</td><td>${E(r.error||(r.status==='READY'?(r.missing.length?'缺失：'+r.missing.join('、'):'已提取全部要求，请核对原文'):r.status==='SAVED'?'已保留 JD 版本与导入依据':r.status==='ROLE_DELETED'?'关联岗位已删除，原件及导入记录留存':'等待处理完成'))}</td><td><div class="actions">${['READY','FAILED','INTERRUPTED'].includes(r.status)?btn('核对 / 补充','jd-review',r.status==='READY',`data-id="${r.id}"`):''}${['FAILED','INTERRUPTED'].includes(r.status)?btn('重试解析','jd-retry',false,`data-id="${r.id}"`):''}${r.status==='SAVED'?btn('查看岗位','edit-role',false,`data-id="${r.role_id}"`):''}${link(r.file_id,'原件')}</div></td></tr>`).join('')}</tbody></table></div>`;
}

async function refreshJDImports(){
  const items=await api('/jd/imports');
  const changed=JSON.stringify(items)!==JSON.stringify(jdUI.items);
  jdUI.items=items;
  if(changed&&state.view==='jd-imports'&&$('#jd-import-list'))$('#jd-import-list').innerHTML=jdImportList();
}

function jdEvidence(key){
  const refs=jdUI.review?.draft?.evidence?.[key]||[];
  if(!refs.length&&['availability','location'].includes(key))return '<p class="form-help">可留空，不参与评分。</p>';
  if(!refs.length)return '<p class="jd-missing">未提取到有原文依据的内容，请核对补充。</p>';
  return `<details class="jd-evidence"><summary>查看原文依据（${refs.length}处）</summary>${refs.map(ref=>`<blockquote><small>${E(ref.span_id)}${ref.page?' · 第'+E(ref.page)+'页':''}${ref.source==='ocr'?' · OCR':''}</small>${E(ref.quote)}</blockquote>`).join('')}</details>`;
}

function jdReviewPage(){
  const row=jdUI.review;
  if(!row)return empty('请先选择一份 JD','返回批量导入页面，点击“核对 / 补充”。');
  const draft=row.draft||{},fields=draft.fields||{};
  return `${heading('核对 JD 提取结果',row.filename,btn('返回导入列表','jd-back'))}
    ${row.error?`<div class="live-banner">${E(row.error)}${['FAILED','INTERRUPTED'].includes(row.status)?btn('重试解析','jd-retry',false,`data-id="${row.id}"`):''}</div>`:''}
    ${(draft.warnings||[]).map(w=>`<div class="live-banner">${E(w)}</div>`).join('')}
    ${draft.missing?.length?`<div class="live-banner">JD 缺失项：${E(draft.missing.join('、'))}。可先保存草稿，补齐并确认后才能用于简历评估。</div>`:''}
    <div class="jd-review-layout"><aside class="panel panel-body jd-original"><div class="status-inline"><h2>JD 原文</h2>${link(row.file_id,'下载原件')}</div><p class="form-help">核对优先条件、例外及 OCR 数字。原始文件始终保留。</p><div class="jd-source">${row.spans.map(s=>`<section><small>${E(s.id)}${s.page?' · 第'+E(s.page)+'页':''}${s.source==='ocr'?' · OCR':''}</small><p>${E(s.text)}</p></section>`).join('')||'<p>原文未读取成功，请提供可解析的文件后重试。</p>'}</div>${row.metadata.model?`<p class="form-help">解析模型：${E(row.metadata.model)}<br>提取规则：${E(row.metadata.prompt_version)}</p>`:''}</aside>
    <section class="panel panel-body">${row.text?`<form id="jd-review-form" class="form-stack">
      <label class="live-field">保存到<select name="target_role_id" id="jd-target-role"><option value="">新建岗位</option>${state.roles.map(r=>`<option value="${r.id}">${E(r.name)}（新建 JD 版本${r.active?'':'，仍保持停用'}）</option>`).join('')}</select></label>
      <p class="form-help" id="jd-target-note">同名岗位不会自动覆盖；更新时请选择已有岗位。</p>
      <div>${liveField('岗位名称','name',draft.name||'','text','required maxlength="60"')}${jdEvidence('name')}</div>
      ${check('我确认这是销售岗位，不适用全局本科院校门槛','is_sales',false)}
      <div class="form-grid">${Object.entries(fieldLabels).map(([key,label])=>`<div>${textarea(label,key,fields[key]||'',`maxlength="${['responsibilities','skills'].includes(key)?5000:2000}" rows="3"`)}${jdEvidence(key)}</div>`).join('')}</div>
      <p class="form-help">提取内容为原文引句，可以在此修订。未提及的条件保留空白；只有你确认该条件不受限制时才填写“不限”。工作地点/base地和到岗时间仅作可选备注，不参与评分。销售岗位须由你确认。</p>
      ${check('我已逐项核对提取结果和原文；补充内容代表本岗位的实际要求','reviewed',false)}
      ${check('要求完整，批准本版 JD 用于简历评估（未勾选则保存草稿）','approved',false)}
      <div class="actions"><button class="button primary" type="submit">保存岗位 JD</button>${btn('返回导入列表','jd-back')}</div><div class="error-message" role="alert"></div>
    </form>`:'<p class="form-help">原文提取成功后，即可在这里核对和保存岗位。</p>'}</section></div>`;
}

async function handleJDAction(action,el){
  if(action==='jd-imports'||action==='jd-back'){await navigate('jd-imports');return;}
  if(action==='jd-refresh'){await refreshJDImports();return;}
  if(action==='jd-review'){
    jdUI.review=await api('/jd/imports/'+el.dataset.id);
    if(jdUI.review.status==='SAVED'){toast('此导入已经保存，请在岗位列表查看。');await navigate('jobs');return;}
    await navigate('jd-review');return;
  }
  if(action==='jd-retry'){
    await api('/jd/imports/'+el.dataset.id+'/retry',{method:'POST'});
    toast('已重新排队；本次会重新调用模型。');
    if(state.view==='jd-review')await navigate('jd-imports');else await refreshJDImports();
  }
}

async function submitJDImports(fd){
  if(jdUI.uploading)throw new Error('文件正在上传，请等待。');
  const files=fd.getAll('files').filter(f=>f.size);
  if(!files.length||files.length>20)throw new Error('请每批选择1至20份 JD 文件。');
  jdUI.uploading=true;
  jdUI.uploads=files.map(f=>({name:f.name,message:'等待上传',failed:false}));
  const draw=()=>{if($('#jd-upload-results'))$('#jd-upload-results').innerHTML=jdUploadResults();};
  try{
    for(let i=0;i<files.length;i++){
      const file=files[i],entry=jdUI.uploads[i];entry.message='正在上传';draw();
      try{
        if(!/\.(pdf|docx|txt)$/i.test(file.name))throw new Error('格式不支持，请提供 PDF / DOCX / TXT。');
        if(file.size>20*1024*1024)throw new Error('超过20MB，请拆分文件。');
        const data=new FormData();data.append('file',file);
        const result=await api('/jd/imports',{method:'POST',body:data});
        entry.message=result.duplicate?'文件已导入，复用已有记录':'已上传，请在导入记录查看进度';
      }catch(error){entry.failed=true;entry.message=errorText(error);}
      draw();
    }
    await refreshJDImports();
    toast(`上传完成：${jdUI.uploads.filter(x=>!x.failed).length} 份已登记或复用，${jdUI.uploads.filter(x=>x.failed).length} 份失败。`);
  }finally{
    jdUI.uploading=false;
    const input=$('#jd-import-form [name=files]');if(input)input.disabled=false;
    const button=$('#jd-import-form [type=submit]');if(button)button.disabled=false;
  }
}

async function submitJDReview(fd){
  if(!fd.has('reviewed'))throw new Error('请先勾选逐项核对确认。');
  const target=state.roles.find(r=>r.id===fd.get('target_role_id'));
  const fields=Object.fromEntries(Object.keys(fieldLabels).map(k=>[k,String(fd.get(k)||'').trim()]));
  const role={name:String(fd.get('name')).trim(),is_sales:fd.has('is_sales'),jd_text:jdUI.review.text,fields,approved:fd.has('approved')};
  await api('/jd/imports/'+jdUI.review.id+'/confirm',{method:'POST',body:{role,reviewed:true,target_role_id:target?.id||null,expected_version:target?.current_version||null}});
  toast(role.approved?'已保存并批准 JD 版本。':'已保存岗位草稿；确认完整要求后可用于评估。');
  await navigate('jd-imports');
}

document.addEventListener('change',event=>{
  if(event.target.id!=='jd-target-role')return;
  const target=state.roles.find(r=>r.id===event.target.value);
  $('#jd-target-note').textContent=target?`将为“${target.name}”创建新版本，保留历史；沿用其评分细则，默认条目随要求更新，自定义条目保留。请核对岗位名称及销售属性。`:'同名岗位不会自动覆盖；更新时请选择已有岗位。';
});

setInterval(async()=>{
  if(!state.session||document.hidden||state.view!=='jd-imports'||jdUI.polling)return;
  if(!jdUI.uploading&&!jdUI.items.some(x=>['QUEUED','EXTRACTING','ANALYZING'].includes(x.status)))return;
  jdUI.polling=true;
  try{await refreshJDImports();}catch{}finally{jdUI.polling=false;}
},3000);
