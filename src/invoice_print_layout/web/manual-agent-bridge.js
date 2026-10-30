/* Connect the bounded organizer to the original desk without duplicating its ledger. */
(() => {
  'use strict';
  let refreshTimer=null,refreshing=false;
  const pending=new Map();
  let mailState=null,mailRevision=0,mailBusy=false,mailRendered='';
  const assignedMail=new Map();
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
      notify('材料已整理，列表刷新未完成：'+error.message+'。请刷新页面后在待核对列表查看结果，无需重新上传。',true);
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
  const unassignedMail=()=> (mailState?.messages||[]).filter(message=>message.status==='unassigned'&&!message.project_id&&message.material_count>0);
  function mailNode(tag,text,className){
    const node=document.createElement(tag);if(text!==undefined)node.textContent=text;if(className)node.className=className;return node;
  }
  function showMail(mail){
    mailState=mail;
    const host=document.getElementById('manualMailStatus');
    const messages=unassignedMail(),projectId=currentInvoiceProjectId();
    if(host){
      const text=mailNode('span',mail.address?'收件邮箱：'+mail.address+' · '+(mail.enabled?'自动收件已开启':'自动收件已暂停，可在材料合成台手动补收'):'未设置收件邮箱');
      const settings=mailNode('button','设置','manual-mail-settings');settings.type='button';settings.onclick=()=>window.invoiceSettings?.open('mail');
      host.replaceChildren(text,document.createTextNode(' · '),settings);host.hidden=false;
      if(messages.length){
        const open=mailNode('button',messages.length+' 封邮件待选项目','manual-mail-settings');open.type='button';
        open.onclick=async()=>{try{await window.openMaterialMatching();const section=document.getElementById('matchingUnassignedMail');if(section&&!section.hidden){section.focus();section.scrollIntoView({block:'start'});}}catch(error){notify(error.message,true);}};
        host.append(document.createTextNode(' · '),open);
      }
    }
    const section=document.getElementById('matchingUnassignedMail');if(!section)return;
    const signature=JSON.stringify([messages,projectId,mailBusy]);if(signature===mailRendered)return;mailRendered=signature;
    section.hidden=!messages.length;section.replaceChildren();if(!messages.length)return;
    const title=mailNode('h3','邮箱收到、还没选项目的邮件（'+messages.length+'）');title.id='matchingUnassignedMailTitle';section.append(title);
    for(const message of messages){
      const card=mailNode('article',undefined,'manual-mail-card');card.dataset.mailId=message.id;
      card.append(mailNode('h4',message.subject||'无主题邮件'));
      const date=message.sent_date||(message.received_at?new Date(message.received_at*1000).toLocaleString('zh-CN'):'日期未知');
      card.append(mailNode('p',(message.sender||'发件人未知')+' · '+date,'muted'));
      const files=mailNode('ul');
      for(const file of message.attachments||[]){
        const item=mailNode('li'),link=mailNode('a',file.name);link.href='/api/agent/mail/attachment?'+new URLSearchParams({id:message.id,index:file.index});link.target='_blank';link.rel='noopener';item.append(link);files.append(item);
      }
      const actions=mailNode('div',undefined,'mia-actions');
      for(const [action,label] of [['assign','归入本项目'],['dismiss','不需要']]){
        const button=mailNode('button',label);button.type='button';button.disabled=mailBusy||(action==='assign'&&!projectId);
        button.onclick=()=>void mailAction(action,message.id,projectId);actions.append(button);
      }
      card.append(files,actions);section.append(card);
    }
  }
  async function refreshAssignedMail(){
    const projectId=currentInvoiceProjectId(),desk=document.getElementById('matchingDeskDialog');
    const completed=(mailState?.messages||[]).filter(message=>assignedMail.get(message.id)===projectId&&message.job_id&&!['queued','running'].includes(message.status));
    if(!completed.length||!desk?.open||window.matchingSuggestionActions?.isBusy?.()||
      document.activeElement?.matches('input,textarea,select,[contenteditable=true]'))return;
    await window.matchingSuggestionActions.refresh();
    completed.forEach(message=>assignedMail.delete(message.id));
  }
  async function mailAction(action,id,projectId){
    if(mailBusy)return;
    if(projectId!==currentInvoiceProjectId()||window.matchingSuggestionActions?.isBusy?.()){notify('项目或材料正在变化，请稍后重试。',true);return;}
    mailBusy=true;mailRevision++;showMail(mailState);
    try{
      const mail=await agentApi('/api/agent/mail/'+action,{id,...(action==='assign'?{project_id:projectId}:{})});
      showMail(mail);
      if(action==='assign'){
        assignedMail.set(id,projectId);
        notify(mail.enabled?'邮件已归入本项目，正在自动整理。':'邮件已归入本项目，开启自动收件后继续整理。');
        try{if(projectId===currentInvoiceProjectId()){await load({skipClassification:true});if(projectId===currentInvoiceProjectId()&&document.getElementById('matchingDeskDialog')?.open)await window.matchingSuggestionActions.refresh();}}
        catch(error){notify('邮件已归入本项目，材料列表刷新失败：'+error.message+'。请刷新材料，无需重复分配。',true);}
      }else notify('邮件已忽略，原件保留。');
    }catch(error){notify(error.message,true);}
    finally{mailBusy=false;showMail(mailState);}
  }
  async function mailStatus(){
    if(mailBusy)return;
    const revision=++mailRevision;
    try{
      const mail=await agentApi('/api/agent/mail');
      if(revision!==mailRevision)return;
      showMail(mail);
      try{await refreshAssignedMail();}catch(error){notify('邮件材料刷新失败：'+error.message,true);}
    }catch(_){if(revision===mailRevision){const host=document.getElementById('manualMailStatus');if(host)host.hidden=true;}}
  }
  function mount(){
    const host=document.getElementById('manualAgent');
    if(!host||!window.manualInvoiceAgent?.mount)return;
    window.manualInvoiceAgent.mount({host,api:agentApi,getProject:()=>currentInvoiceProjectId(),
      onChanged:payload=>{if(scoped(payload)){pending.set(payload.jobId,payload);scheduleRefresh();}},
      onOpenExpense:openExpense,
      onOpenMaterials:payload=>{if(scoped(payload))return window.openMaterialMatching();},
      onPreview:payload=>{
        if(!scoped(payload))throw Error('项目已变化，请重新打开报销预览');
        if(typeof window.openAgentReportPreview!=='function')throw Error('报销预览尚未加载，请稍后重试');
        return window.openAgentReportPreview(payload);
      }});
    document.addEventListener('focusout',scheduleRefresh);
    document.addEventListener('close',scheduleRefresh,true);
    window.addEventListener('invoice-settings-changed',()=>{void mailStatus();});
    window.addEventListener('invoice-mail-refresh',()=>{void mailStatus();});
    setInterval(()=>{if(!document.hidden)void mailStatus();},15000);
    void mailStatus();
  }
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',mount,{once:true});else mount();
})();
