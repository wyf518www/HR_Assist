'use strict';
const diagnosticSelection={day:'',runId:''};
const diagnosticPhases={preflight:'运行条件检查',mail_fetch:'邮件获取',document_parse:'简历解析',identity:'姓名与岗位识别',identity_review:'姓名与岗位核对',archive:'岗位归档',assessment:'简历评分',report_export:'报告生成',jd_text:'JD 原文分析',jd_import:'JD 批量导入',internal:'内部处理'};
const diagnosticEvents={PHASE_STARTED:'阶段开始',PHASE_COMPLETED:'阶段完成',PHASE_FAILED:'阶段失败',MODEL_REQUEST_STARTED:'模型请求开始',MODEL_HEADERS_RECEIVED:'收到响应头',MODEL_RESPONSE_RECEIVED:'响应接收完成',MODEL_REQUEST_COMPLETED:'模型结果解析完成',MODEL_REQUEST_FAILED:'模型调用失败'};
const diagnosticNumbers={request_bytes:'请求字节',system_characters:'系统提示字符',payload_characters:'输入字符',input_characters:'原文字符',response_bytes:'响应字节',header_wait_ms:'等待响应头 ms',first_byte_ms:'首字节 ms',elapsed_ms:'耗时 ms',read_timeout_seconds:'读等待上限 s',connect_timeout_seconds:'连接上限 s',http_status:'HTTP 状态',span_count:'原文段数',content_characters:'最终正文字符',reasoning_characters:'思考内容字符',prompt_tokens:'输入 tokens',completion_tokens:'输出 tokens',total_tokens:'总 tokens',reasoning_tokens:'思考 tokens',cached_tokens:'缓存 tokens',input_tokens:'输入 tokens',output_tokens:'输出 tokens',attempt:'调用序号',retry_count:'重试次数',criterion_count:'评分条目数',max_tokens:'最大输出 tokens'};
function diagnosticSettings(s){
  const d=s.diagnostics||{enabled:true,retention_days:7,cleanup_time:'03:00'};
  return `<section class="panel settings-section"><div class="panel-head"><h2>详细诊断日志</h2></div><div class="panel-body form-grid">${check('启用详细诊断日志','diagnostics_enabled',d.enabled)}${liveField('保留天数（含当天）','diagnostics_retention_days',d.retention_days,'number','required min="1" max="30"')}${liveField('每天清理时间（北京）','diagnostics_cleanup_time',d.cleanup_time,'time','required')}<p class="form-help wide">仅记录阶段、耗时、状态码和内容长度，不记录密钥、联系方式、简历 / JD 正文或模型回答。关闭记录后仍清理过期日志。服务停止时无法清理，恢复后补执行；业务记录和审计日志保留。</p></div></section>`;
}
function diagnosticQuery(){const q=new URLSearchParams({day:diagnosticSelection.day});if(diagnosticSelection.runId)q.set('run_id',diagnosticSelection.runId);return q;}
function diagnosticRecord(row){
  const failed=row.event.endsWith('FAILED');
  const numbers=Object.entries(diagnosticNumbers).filter(([key])=>row[key]!==undefined).map(([key,label])=>`<span>${E(label)}：<b>${E(row[key])}</b></span>`).join('');
  return `<tr class="${failed?'diagnostic-failed':''}"><td>${stamp(row.timestamp)}<p class="tiny muted">${E(row.request_id?.slice(0,8)||row.trace_id?.slice(0,8)||'')}</p></td><td><b>${E(diagnosticPhases[row.phase]||row.phase)}</b><p>${E(diagnosticEvents[row.event]||row.event)}</p>${row.application_id?`<p class="tiny muted">记录 ${E(row.application_id.slice(0,8))}</p>`:''}${row.jd_import_id?`<p class="tiny muted">JD ${E(row.jd_import_id.slice(0,8))}</p>`:''}</td><td>${row.error_code?`<p class="error-message">${E(row.error_code)}</p>`:''}<div class="diagnostic-metrics">${numbers||'—'}</div>${row.reasoning_effort?`<p class="tiny muted">思考强度：${E(({default:'沿用供应商默认',low:'轻量 low',high:'增强 high',max:'深度 max'})[row.reasoning_effort]||row.reasoning_effort)}</p>`:''}${row.finish_reason?`<p class="tiny muted">结束原因：${E(row.finish_reason)}</p>`:''}<details class="mt-small"><summary>查看技术元数据</summary><pre class="diagnostic-json">${E(JSON.stringify(row,null,2))}</pre></details></td></tr>`;
}
async function diagnosticsPage(){
  const [status,runs]=await Promise.all([api('/diagnostics'),api('/runs')]);
  if(!diagnosticSelection.day)diagnosticSelection.day=status.days[0]||new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date());
  const records=await api('/diagnostics/records?'+diagnosticQuery()+'&limit=500');
  const statusText=status.enabled?'记录已开启':'记录已关闭';
  return `${heading('详细诊断日志','用于排查模型调用和处理阶段的故障。所有时间均为北京时间。',`<div class="actions">${btn('返回运行记录','runs')}${btn('刷新','diagnostic-refresh')}</div>`)}<div class="notice mb">${E(statusText)} · 保留 ${E(status.retention_days)} 天 · 每天 ${E(status.cleanup_time)} 清理 · 最近清理：${E(status.last_cleanup||'尚未到清理时间')}。业务记录和审计日志保留。</div>${(status.write_error||status.cleanup_error)?`<div class="error-banner mb">日志存储异常：${E([status.write_error,status.cleanup_error].filter(Boolean).join(" / "))}。记录或清理可能未完成，请检查磁盘空间或目录权限。</div>`:''}<section class="panel mb"><div class="panel-body"><form id="diagnostic-filter" class="filter-bar">${liveField('日期','day',diagnosticSelection.day,'date','required')}<label class="live-field">批次<select name="run_id"><option value="">全部批次（含 JD 分析）</option>${runs.map(r=>`<option value="${E(r.id)}" ${r.id===diagnosticSelection.runId?'selected':''}>${E(r.id.slice(0,10))} · ${stamp(r.started_at)}</option>`).join('')}</select></label><button class="button primary" type="submit">查看日志</button>${btn('下载该筛选日志','diagnostic-download')}</form><p class="form-help">显示最新 500 条，下载包含该日期和批次的全部记录（最多 10 MiB）。跨零点运行时请分别查看两天。日志从本次更新后开始采集，不含简历 / JD 正文、模型回答或凭据。</p></div></section><section class="panel table-scroll"><table class="live-table"><thead><tr><th>时间 / 请求</th><th>阶段 / 事件</th><th>诊断信息</th></tr></thead><tbody>${records.slice().reverse().map(diagnosticRecord).join('')||'<tr><td colspan="3" class="empty">此日期和批次暂无诊断日志。更新前的记录无法补录；发生新错误后可在这里查看。</td></tr>'}</tbody></table></section>`;
}
async function handleDiagnosticAction(action,el){
  if(action==='diagnostic-download'){
    const form=document.querySelector('#diagnostic-filter');
    if(!form.reportValidity())return;
    diagnosticSelection.day=form.elements.day.value;diagnosticSelection.runId=form.elements.run_id.value;
    const response=await fetch('/api/diagnostics/download?'+diagnosticQuery(),{credentials:'same-origin'});
    if(!response.ok){
      const data=await response.json().catch(()=>({}));
      if(response.status===401){state.session=null;showLogin();}
      throw new Error(data.error?.message||'诊断日志下载失败，请重试。');
    }
    const url=URL.createObjectURL(await response.blob());
    const a=document.createElement('a');a.href=url;a.download='diagnostic-'+diagnosticSelection.day+(diagnosticSelection.runId?'-'+diagnosticSelection.runId.slice(0,10):'')+'.jsonl';
    document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);
    toast('已按当前筛选条件下载诊断日志。');return;
  }
  if(action==='diagnostic-open'){
    diagnosticSelection.runId=el.dataset.runId||'';
    diagnosticSelection.day=el.dataset.day||'';
  }else if(action==='diagnostic-refresh'){
    const form=document.querySelector('#diagnostic-filter');
    if(form){diagnosticSelection.day=form.elements.day.value;diagnosticSelection.runId=form.elements.run_id.value;}
  }
  await navigate('diagnostics',{reload:false});
}
