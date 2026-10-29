'use strict';
const diagnosticSelection={day:'',runId:''};
const diagnosticPhases={preflight:'运行条件检查',mail_fetch:'邮件获取',document_parse:'简历解析',identity:'姓名与岗位识别',identity_review:'姓名与岗位核对',archive:'岗位归档',assessment:'简历评分',report_export:'报告生成',jd_text:'JD 原文分析',jd_import:'JD 批量导入',internal:'内部处理'};
const diagnosticEvents={PHASE_STARTED:'阶段开始',PHASE_COMPLETED:'阶段完成',PHASE_FAILED:'阶段失败',PHASE_WAITING:'等待人工核对',SEMANTIC_DRAFT_REUSED:'复用已完成的模型分析',MODEL_REQUEST_STARTED:'模型请求开始',MODEL_HEADERS_RECEIVED:'收到响应头',MODEL_RESPONSE_RECEIVED:'响应接收完成',MODEL_REQUEST_COMPLETED:'模型结果解析完成',MODEL_REQUEST_FAILED:'模型调用失败'};
const diagnosticNumbers={request_bytes:'请求字节',system_characters:'系统提示字符',payload_characters:'输入字符',input_characters:'原文字符',response_bytes:'响应字节',header_wait_ms:'等待响应头 ms',first_byte_ms:'首字节 ms',elapsed_ms:'耗时 ms',read_timeout_seconds:'读等待上限 s',connect_timeout_seconds:'连接上限 s',http_status:'HTTP 状态',span_count:'原文段数',content_characters:'最终正文字符',reasoning_characters:'思考内容字符',prompt_tokens:'输入 tokens',completion_tokens:'输出 tokens',total_tokens:'总 tokens',reasoning_tokens:'思考 tokens',cached_tokens:'缓存 tokens',input_tokens:'输入 tokens',output_tokens:'输出 tokens',attempt:'调用序号',retry_count:'重试次数',criterion_count:'评分条目数',criterion_index:'评分项序号',fact_index:'事实项序号',evidence_index:'引文序号',quote_characters:'引文字符数',value_characters:'取值字符数',max_tokens:'最大输出 tokens'};
function diagnosticSettings(s){
  const d=s.diagnostics||{enabled:true,retention_days:7,cleanup_time:'03:00'};
  return `<section class="panel settings-section"><div class="panel-head"><h2>详细诊断日志</h2></div><div class="panel-body form-grid">${check('启用详细诊断日志','diagnostics_enabled',d.enabled)}${liveField('保留天数（含当天）','diagnostics_retention_days',d.retention_days,'number','required min="1" max="30"')}${liveField('每天清理时间（北京）','diagnostics_cleanup_time',d.cleanup_time,'time','required')}<p class="form-help wide">仅记录阶段、耗时、状态码、内容长度及固定分类的校验原因和条目序号，不记录密钥、联系方式、简历 / JD 正文或模型回答。关闭记录后仍清理过期日志。服务停止时无法清理，恢复后补执行；业务记录和审计日志保留。</p></div></section>`;
}
function diagnosticQuery(){const q=new URLSearchParams({day:diagnosticSelection.day});if(diagnosticSelection.runId)q.set('run_id',diagnosticSelection.runId);return q;}
const diagnosticValidationKinds={score_evidence:'评分引文',fact_evidence:'事实引文',fact_value:'事实取值'};
const diagnosticEvidenceReasons={span_missing:'引用的原文片段不存在',empty_quote:'引文为空',no_match:'在指定片段中找不到相同原文（已检查允许的空格、换行差异）',ambiguous_match:'忽略允许的排版空白后出现多处匹配，无法唯一定位',value_missing:'事实取值不能在已定位的原文引句中逐字核验'};
const diagnosticDimensions={hard:'硬性条件',skills:'核心技能',experience:'工作经验',achievements:'项目成果',stability:'稳定性'};
const diagnosticFacts={company:'当前公司',education_level:'学历',admission:'招生方式',institution:'毕业院校',experience:'工作年限'};
const diagnosticIdentityReasons={name_unresolved:'姓名尚不能确认',role_unresolved:'应聘岗位尚不能确认或未对应启用岗位',name_and_role_unresolved:'姓名和应聘岗位均需核对',history_confirmation:'已有历史评分，身份建议需人工确认后应用'};
const diagnosticIdentitySchemaFields={root:'整体结果','root.extra':'顶层额外字段',unknown:'未归类字段',name:'姓名结果','name.status':'姓名识别状态','name.value':'姓名文字','name.note':'姓名说明','name.evidence':'姓名依据列表','name.evidence.item':'姓名依据条目','name.evidence.span_id':'姓名依据来源编号','name.evidence.interpretation':'姓名依据解读','name.evidence.extra':'姓名依据额外字段','name.extra':'姓名结果额外字段',role:'岗位结果','role.status':'岗位识别状态','role.value':'岗位文字','role.note':'岗位说明','role.role_id':'岗位编号','role.evidence':'岗位依据列表','role.evidence.item':'岗位依据条目','role.evidence.span_id':'岗位依据来源编号','role.evidence.interpretation':'岗位依据解读','role.evidence.extra':'岗位依据额外字段','role.extra':'岗位结果额外字段'};
const diagnosticIdentitySchemaErrors={missing:'缺少必填字段',invalid_choice:'取值不在允许选项中',invalid_type:'字段类型不符合要求',length_limit:'长度或条目数量超限',unexpected_field:'包含未约定的额外字段',invalid_format:'字段格式不符合要求'};
Object.assign(diagnosticNumbers,{identity_schema_error_count:'格式问题数量',identity_evidence_index:'身份依据序号'});
function diagnosticMapped(map,value){return Object.hasOwn(map,value)?map[value]:'';}
function diagnosticExplanation(row,waiting){
  if(row.error_code==='BATCH_STOPPED')return '<div class="notice mt-small">同批次其他模型请求已触发暂停，本候选人的后续请求未发送，已完成步骤保留。此项随批次暂停，不新增模型调用失败或业务异常；请先处理该批次的首个暂停原因，再继续处理。</div>';
  if(waiting){
    const reason=diagnosticMapped(diagnosticIdentityReasons,row.identity_reason);
    return `<div class="notice mt-small">${E(reason||'姓名或应聘岗位需要人工核对')}。请到“候选人库 → 姓名与岗位核对”，对照原件确认姓名与岗位，再点击“确认修正所选”。需要模型给出新建议时使用“重新分析所选”。</div>`;
  }
  if(row.error_code==='IDENTITY_SCHEMA'){
    const field=diagnosticMapped(diagnosticIdentitySchemaFields,row.identity_schema_field),reason=diagnosticMapped(diagnosticIdentitySchemaErrors,row.identity_schema_error);
    return `<div class="notice mt-small">${field&&reason?`${E(field)}：${E(reason)}。`:'此记录未包含具体格式字段，无法仅凭该记录判断缺少哪项或哪项类型错误。'}原记录未因格式错误被覆盖。可在“姓名与岗位核对”重新分析所选记录，或对照原件人工填写后确认；诊断只记录字段类别，不保存模型取值。</div>`;
  }
  if(row.error_code==='DUPLICATE_APPLICATION')return '<div class="notice mt-small">同一简历原件已有目标岗位申请，已停止写入以保留各自的评分和人工跟进。请在候选人库对照原始文件和岗位核对两条记录；系统不会自动合并或删除。</div>';
  const kind=diagnosticMapped(diagnosticValidationKinds,row.validation_kind),reason=diagnosticMapped(diagnosticEvidenceReasons,row.evidence_reason);
  const dimension=diagnosticMapped(diagnosticDimensions,row.criterion_dimension),field=diagnosticMapped(diagnosticFacts,row.fact_field);
  const labels=[kind,dimension&&'评分维度：'+dimension,field&&'事实字段：'+field].filter(Boolean);
  if(labels.length||reason)return `<p class="mt-small">${E(labels.join(' · '))}</p>${reason?`<p class="form-help">原因：${E(reason)}</p>`:''}`;
  if(['EVIDENCE_INVALID','FACT_EVIDENCE_INVALID','FACT_VALUE_MISMATCH'].includes(row.error_code))return '<p class="form-help mt-small">此记录未包含具体条目和匹配原因，请查看“运行记录”中的处理日志；无法仅凭该记录判断失败原因。</p>';
  return '';
}
function diagnosticRecord(row){
  const paused=row.error_code==='BATCH_STOPPED';
  const waiting=!paused&&(row.event==='PHASE_WAITING'||(row.event==='PHASE_FAILED'&&row.error_code==='IDENTITY_REVIEW_REQUIRED'));
  const failed=!waiting&&!paused&&String(row.event).endsWith('FAILED');
  const eventLabel=paused?'随批次暂停':waiting?'等待人工核对':diagnosticEvents[row.event]||row.event;
  const numbers=Object.entries(diagnosticNumbers).filter(([key])=>row[key]!==undefined).map(([key,label])=>`<span>${E(label)}：<b>${E(row[key])}</b></span>`).join('');
  return `<tr class="${failed?'diagnostic-failed':''}"><td>${stamp(row.timestamp)}<p class="tiny muted">${E(row.request_id?.slice(0,8)||row.trace_id?.slice(0,8)||'')}</p></td><td><b>${E(diagnosticPhases[row.phase]||row.phase)}</b><p>${E(eventLabel)}</p>${row.application_id?`<p class="tiny muted">记录 ${E(row.application_id.slice(0,8))}</p>`:''}${row.jd_import_id?`<p class="tiny muted">JD ${E(row.jd_import_id.slice(0,8))}</p>`:''}</td><td>${row.error_code?`<p class="${waiting||paused?'tiny muted':'error-message'}">${E(row.error_code)}</p>`:''}${diagnosticExplanation(row,waiting)}${row.model_step?`<p class="tiny muted">模型步骤：${E(row.model_step==='review'?'第二步 · 语义复核':'第一步 · 分析')}</p>`:''}<div class="diagnostic-metrics">${numbers||'—'}</div>${row.reasoning_effort?`<p class="tiny muted">思考强度：${E(({default:'沿用供应商默认',low:'轻量 low',high:'增强 high',max:'深度 max'})[row.reasoning_effort]||row.reasoning_effort)}</p>`:''}${row.finish_reason?`<p class="tiny muted">结束原因：${E(row.finish_reason)}</p>`:''}<details class="mt-small"><summary>查看技术元数据</summary><pre class="diagnostic-json">${E(JSON.stringify(row,null,2))}</pre></details></td></tr>`;
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
