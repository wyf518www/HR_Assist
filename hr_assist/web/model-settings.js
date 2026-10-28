'use strict';
function modelGenerationSettings(llm){
  const effort=llm.reasoning_effort??'default';
  return `<div class="wide form-grid"><label class="live-field">GLM-5.3 思考强度<select name="llm_reasoning_effort">${[['default','沿用供应商默认（GLM-5.3 为 max）'],['low','low · 轻量思考'],['high','high · 增强思考'],['max','max · 深度思考']].map(([value,label])=>`<option value="${value}" ${effort===value?'selected':''}>${label}</option>`).join('')}</select></label>${liveField('最大输出 tokens（留空沿用供应商默认）','llm_max_tokens',llm.max_tokens??'','number','min="1024" max="131072" step="1" placeholder="默认 65536；可设 1024—131072"')}<div class="actions wide">${btn('填入轻量建议：low / 32768','model-light-preset')}${btn('恢复供应商默认','model-default-preset')}</div><p class="form-help wide">GLM-5.3 强制开启思考，不能关闭；未指定强度时供应商默认使用 max。轻量建议用于减少推理开销，实际耗时与准确率仍需对比。按钮只填表，点击“保存配置”后生效，不发起模型调用。</p><p class="form-help wide">这两个控制项目前仅用于智谱标准官方接口的 glm-5.3。使用其他模型或接口时请恢复供应商默认。最大输出是请求上限，不是最终正文长度保证，也不是独立的思考预算；过低可能导致 JSON 截断，系统会拒绝不完整结果。<a href="https://docs.bigmodel.cn/cn/guide/models/text/glm-5.3" target="_blank" rel="noopener noreferrer">模型说明</a> · <a href="https://docs.bigmodel.cn/cn/guide/start/concept-param" target="_blank" rel="noopener noreferrer">参数说明</a></p></div>`;
}
function applyModelGenerationPreset(form,light){
  if(light){
    let url;
    try{url=new URL(form.elements.llm_endpoint.value);}catch{throw new Error('请先填写智谱标准官方接口地址和模型 glm-5.3。');}
    if(url.protocol!=='https:'||url.hostname!=='open.bigmodel.cn'||url.port||url.username||url.password||url.search||url.hash||url.pathname.replace(/\/$/,'')!=='/api/paas/v4/chat/completions'||form.elements.llm_model.value!=='glm-5.3'){
      throw new Error('轻量建议仅适用于智谱标准官方接口的 glm-5.3；请先核对接口地址与模型标识。');
    }
  }
  form.elements.llm_reasoning_effort.value=light?'low':'default';
  form.elements.llm_max_tokens.value=light?'32768':'';
}
function readModelGenerationSettings(fd){
  const raw=String(fd.get('llm_max_tokens')??'').trim();
  return {llm_reasoning_effort:fd.get('llm_reasoning_effort')??'default',llm_max_tokens:raw===''?null:Number(raw)};
}
