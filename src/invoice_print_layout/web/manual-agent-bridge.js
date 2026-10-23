/* Connect the bounded organizer to the original desk without duplicating its ledger. */
(() => {
  'use strict';
  let refreshTimer=null,refreshing=false;
  const pending=new Map();
  const scoped=payload=>!!payload&&payload.projectId===currentInvoiceProjectId();
  function editing(){
    const target=document.activeElement;
    return !!document.querySelector('dialog[open]')||batchVerifyBusy||singleVerifyBusy||batchReturnBusy||
      !!window.matchingSuggestionActions?.isBusy?.()||
      !!(target&&target.matches('input:not([type=button]):not([type=checkbox]),textarea,select,[contenteditable=true]'));
  }
  function scheduleRefresh(){
    if(refreshTimer||refreshing||!pending.size)return;
    refreshTimer=setTimeout(flushRefresh,500);
  }
  async function flushRefresh(){
    refreshTimer=null;if(refreshing||!pending.size)return;
    if(editing()){scheduleRefresh();return;}
    const projectId=currentInvoiceProjectId();
    const updates=[...pending.values()].filter(scoped);
    if(!updates.length){pending.clear();return;}
    refreshing=true;
    try{
      await load({skipClassification:true});
      if(projectId!==currentInvoiceProjectId())return;
      updates.forEach(update=>pending.delete(update.jobId));
      notify('智能整理结果已同步到原工作台，可在待核对列表查看费用和打印页。');
    }catch(error){
      // The job has already committed. A refresh failure must not start it again.
      updates.forEach(update=>pending.delete(update.jobId));
      notify('材料已整理，列表刷新未完成：'+error.message+'。可点击任务中的“查看费用”重读结果。',true);
    }finally{refreshing=false;scheduleRefresh();}
  }
  async function agentApi(path,body){
    if(!path.startsWith('/api/'))throw Error('整理接口无效');
    if(body!==undefined)return api(path.slice(5),body);
    const response=await fetch(path,{cache:'no-store'}),data=await response.json();
    if(!response.ok)throw Error(data.error||'无法读取整理进度');
    return data;
  }
  async function openExpense(payload){
    if(!scoped(payload))throw Error('项目已变化，请重新选择当前项目费用');
    await load({skipClassification:true});
    if(!scoped(payload))return;
    const item=state.items.find(row=>row.id===payload.itemId&&row.project_id===payload.projectId&&!row.deleted);
    if(!item)throw Error('当前项目中已找不到该费用，请检查材料记录');
    window.showCreatedMaterialExpenses([item]);
    document.getElementById('detail')?.scrollIntoView({block:'nearest'});
  }
  async function mailStatus(){
    const host=document.getElementById('manualMailStatus');
    try{
      const mail=await agentApi('/api/agent/mail');
      if(!host)return;
      host.textContent=mail.address?'收件邮箱：'+mail.address+' · '+(mail.enabled?'自动收件已开启':'自动收件已暂停，可在材料合成台手动补收'):'';
      host.hidden=!mail.address;
    }catch(_){if(host)host.hidden=true;}
  }
  function mount(){
    const host=document.getElementById('manualAgent');
    if(!host||!window.manualInvoiceAgent?.mount)return;
    window.manualInvoiceAgent.mount({host,api:agentApi,getProject:()=>currentInvoiceProjectId(),
      onChanged:payload=>{if(scoped(payload)){pending.set(payload.jobId,payload);scheduleRefresh();}},
      onOpenExpense:openExpense,
      onPreview:payload=>{
        if(!scoped(payload))throw Error('项目已变化，请重新打开报销预览');
        if(typeof window.openAgentReportPreview!=='function')throw Error('报销预览尚未加载，请稍后重试');
        return window.openAgentReportPreview(payload);
      }});
    document.addEventListener('focusout',scheduleRefresh);
    document.addEventListener('close',scheduleRefresh,true);
    void mailStatus();
  }
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',mount,{once:true});else mount();
})();
