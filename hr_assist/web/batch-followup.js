'use strict';
const batchFollowupFields=['推荐给用人部门日期','用人部门复筛结果','约面时间','面试状态','面试结果','面试评价','当前状态'];
function batchFollowupToolbar(){
  return `<div class="batch-followup-toolbar mb"><div class="actions">${btn('全选当前结果','batch-select-all')}${btn('取消选择','batch-select-none')}<span id="batch-selection-count" role="status">已选 0 人</span>${btn('批量跟进','batch-open',true,'disabled')}</div><p class="form-help">先勾选候选人，再统一填写跟进。筛选或刷新列表会清空选择，一次最多500人。</p></div>`;
}
function updateFollowupSelection(){
  const count=document.querySelectorAll('[name=batch-candidate]:checked').length;
  const label=$('#batch-selection-count'),button=$('[data-action=batch-open]');
  if(label)label.textContent=`已选 ${count} 人`;
  if(button)button.disabled=!count;
}
function closeBatchFollowup(){
  const dialog=$('#batch-followup-dialog');
  if(dialog&&!dialog._saving){dialog.close();dialog.remove();}
}
function batchFollowupEditor(key,index){
  const choices=followupChoices[key],name='batch-value-'+index;
  let control;
  if(choices){
    control=`<label class="live-field">统一填写${E(key)}<select name="${name}" disabled><option value="">请选择</option>${choices.map(c=>`<option>${E(c)}</option>`).join('')}</select></label>`;
  }else if(key==='推荐给用人部门日期'||key==='约面时间'){
    control=liveField('统一填写'+E(key),name,'',key==='约面时间'?'datetime-local':'date','disabled');
  }else{
    control=textarea('统一填写'+E(key),name,'','disabled rows="2" maxlength="20000"');
  }
  return `<section class="batch-field" data-field-index="${index}">${check('修改'+E(key),'batch-enable-'+index)}<div class="batch-field-controls"><label class="live-field">${E(key)}操作<select name="batch-mode-${index}" disabled><option value="set">统一填写</option><option value="clear">清空该字段</option></select></label>${control}</div></section>`;
}
function openBatchFollowup(){
  const ids=[...document.querySelectorAll('[name=batch-candidate]:checked')].map(x=>x.value);
  if(!ids.length)throw new Error('请先勾选候选人。');
  if(ids.length>500)throw new Error('一次最多选择500人，请缩小筛选范围。');
  closeBatchFollowup();
  const rows=ids.map(id=>state.applications.find(a=>a.id===id));
  if(rows.some(a=>!a))throw new Error('候选人列表已变化，请刷新后重新选择。');
  const dialog=document.createElement('dialog');dialog.id='batch-followup-dialog';dialog.className='followup-dialog batch-dialog';
  dialog._rows=structuredClone(rows);
  dialog.innerHTML=`<form id="batch-followup-form" class="form-stack"><div class="panel-head"><h2>批量跟进 · ${rows.length} 人</h2>${btn('关闭','batch-close')}</div><div id="batch-followup-editor"><p class="form-help mb">只修改勾选的字段，其他内容保持原样。约面时间使用北京时间。如需删除已有内容，请明确选择“清空该字段”。</p>${batchFollowupFields.map(batchFollowupEditor).join('')}<div class="actions mt"><button type="submit" class="button primary">预览本次修改</button>${btn('取消','batch-close')}</div></div><div id="batch-followup-preview" hidden></div><p class="error-message" role="alert"></p></form>`;
  dialog.addEventListener('close',()=>dialog.remove(),{once:true});
  dialog.addEventListener('cancel',event=>{if(dialog._saving)event.preventDefault();});
  document.body.append(dialog);dialog.showModal();
}
function updateBatchField(target){
  const row=target.closest('.batch-field');if(!row)return;
  const i=row.dataset.fieldIndex,enabled=row.querySelector(`[name="batch-enable-${i}"]`).checked;
  const mode=row.querySelector(`[name="batch-mode-${i}"]`),value=row.querySelector(`[name="batch-value-${i}"]`);
  mode.disabled=!enabled;value.disabled=!enabled||mode.value==='clear';
}
function previewBatchFollowup(form,fd){
  const dialog=form.closest('dialog'),fields={};
  batchFollowupFields.forEach((key,i)=>{
    if(!fd.has('batch-enable-'+i))return;
    if(fd.get('batch-mode-'+i)==='clear'){fields[key]=null;return;}
    const value=String(fd.get('batch-value-'+i)||'');
    if(!value.trim())throw new Error(`请填写“${key}”，或明确选择“清空该字段”。`);
    fields[key]=value;
  });
  if(!Object.keys(fields).length)throw new Error('请至少勾选一个需要修改的字段。');
  dialog._patch=fields;
  const rows=dialog._rows;
  form.querySelector('[role=alert]').textContent='';
  $('#batch-followup-preview').innerHTML=`<h3>确认修改 ${rows.length} 人的跟进</h3><p class="form-help mt-small">以下字段将对所有选中候选人统一生效，包括覆盖已有值。未列出的字段保持原样。</p><dl class="batch-change-preview">${Object.entries(fields).map(([key,value])=>{
    const occupied=rows.filter(a=>a.manual[key]!==null&&a.manual[key]!==undefined&&a.manual[key]!=='').length;
    return `<dt>${E(key)}</dt><dd><strong>${value===null?'清空':E(followupDisplay(value))}</strong><small>所选 ${rows.length} 人中，${occupied} 人已有内容。</small></dd>`;
  }).join('')}</dl><details open class="batch-selected-list"><summary>本次选中 ${rows.length} 人</summary><ul>${rows.map(a=>`<li>${E(a.name)} · ${E(a.role_name||'待分类')} <span class="muted">${E(a.id.slice(0,8))}</span></li>`).join('')}</ul></details><p class="form-help mt">只记录内部跟进，不发送面试邀请，也不改变评分。</p><div class="actions mt">${btn('确认更新 '+rows.length+' 人','batch-confirm',true)}${btn('返回修改','batch-edit')}${btn('取消','batch-close')}</div>`;
  $('#batch-followup-editor').hidden=true;$('#batch-followup-preview').hidden=false;dialog.scrollTop=0;
}
async function handleBatchFollowupAction(action){
  if(action==='batch-close'){closeBatchFollowup();return;}
  if(action==='batch-select-all'||action==='batch-select-none'){
    const boxes=document.querySelectorAll('[name=batch-candidate]');
    if(action==='batch-select-all'&&boxes.length>500)throw new Error('当前结果超过500人，请先用岗位或姓名筛选。');
    boxes.forEach(x=>x.checked=action==='batch-select-all');updateFollowupSelection();return;
  }
  if(action==='batch-open'){openBatchFollowup();return;}
  const dialog=$('#batch-followup-dialog');if(!dialog||dialog._saving)return;
  if(action==='batch-edit'){$('#batch-followup-editor').hidden=false;$('#batch-followup-preview').hidden=true;dialog._patch=null;return;}
  if(action==='batch-confirm'){
    if(!dialog._patch)throw new Error('请先预览本次修改。');
    dialog._saving=true;dialog.querySelectorAll('button').forEach(x=>x.disabled=true);
    try{
      const result=await api('/applications/followup-batch',{method:'PUT',body:{confirmed:true,items:dialog._rows.map(a=>({application_id:a.id,expected_revision:a.revision})),fields:dialog._patch}});
      dialog._saving=false;closeBatchFollowup();await navigate('candidates');
      toast(`批量跟进已保存：选中 ${result.selected_rows} 人，实际更新 ${result.updated_rows} 人。其他字段保持原样。`);
    }catch(error){
      dialog.querySelector('[role=alert]').textContent=errorText(error)+(error.status===409?' 请关闭窗口，刷新列表后重新选择；本次全部未写入。':'');
    }finally{
      dialog._saving=false;dialog.querySelectorAll('button').forEach(x=>x.disabled=false);
    }
  }
}
