(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  let token = '', busy = false, project = '', activePreview = '', polling = false, renderedJobs = '';
  let projects = [], mailState = null, mailBusy = false, mailPolling = false, mailConfigured = false, renderedMail = '', focusJob = '', mailRevision = 0, mailAccount = '';
  const mailSelections = new Map();
  const answersInFlight = new Set();
  let projectNavigation = null, workspaceDetails = null, projectsRefreshing = null, nextProjectsCheck = 0;
  const states = {queued:'等待整理',running:'正在整理',ready:'预览已准备',needs_input:'需要你补充',stopped:'已停止尝试',failed:'处理未完成',cancelled:'已取消'};
  function el(tag, text, cls) {const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;}
  function error(e) {$('error').textContent=e.message || String(e);$('error').hidden=false;}
  async function api(path, body) {
    const r=await fetch(path,body===undefined?{cache:'no-store'}:{method:'POST',headers:{'Content-Type':'application/json','X-Workbench-Token':token},body:JSON.stringify(body)});
    const data=await r.json();if(!r.ok)throw new Error(data.error || '处理未完成');return data;
  }
  function lock(on) {
    busy=on;$('upload').disabled=on||!project;$('organize').disabled=on||!project;$('project').disabled=on||!projects.length;
    if(projectNavigation)projectNavigation.setBusy(on);
    if(workspaceDetails)workspaceDetails.setBusy(on);
    $('empty-project').hidden=!!project;
  }
  function manualLink() {$('manual').href='/?project='+encodeURIComponent(project);}
  function projectHeading(){
    const selected=projects.find(p=>p.id===project);
    $('workspace-title').textContent=selected?selected.name:'项目工作台';
    if(projectNavigation)projectNavigation.update(projects,project);
    lock(busy);
  }
  function projectOptions(){
    const selected=project,mailDefault=$('mail-project').value;
    $('project').replaceChildren();
    for(const p of projects){const option=el('option',p.name);option.value=p.id;$('project').append(option);}
    if(projects.some(p=>p.id===selected))$('project').value=selected;
    $('mail-project').replaceChildren();mailProjectOptions($('mail-project'),'自动识别；无法确定时询问我');
    const desired=mailConfigured?mailDefault:(mailState?mailState.default_project_id:mailDefault);
    if(projects.some(p=>p.id===desired))$('mail-project').value=desired;
    renderedMail='';if(mailState)renderMailMessages(mailState.messages||[]);
    projectHeading();
  }
  async function reloadProjects(preferred=''){
    if(projectsRefreshing){if(!preferred)return;await projectsRefreshing.catch(()=>{});}
    const loading=(async()=>{
      const state=await api('/api/state');token=state.token;
      const incoming=Array.isArray(state.projects)?state.projects:[];
      const changed=JSON.stringify(projects.map(p=>[p.id,p.name]))!==JSON.stringify(incoming.map(p=>[p.id,p.name]));
      projects=incoming;if(changed||preferred)projectOptions();
      const next=projects.some(p=>p.id===preferred)?preferred:projects.some(p=>p.id===project)?project:(projects[0]?.id||'');
      if(next!==project||preferred)await switchProject(next);
    })();
    projectsRefreshing=loading;
    try{await loading;}finally{if(projectsRefreshing===loading)projectsRefreshing=null;}
  }
  async function begin(ids,targetProject=project) {
    if(!ids.length)throw new Error('当前没有未整理材料；已关联材料不会重复计费');
    if(ids.length>20)throw new Error('每个整理任务最多20份材料，请先在人工工作台选择范围');
    await api('/api/agent/start',{project_id:targetProject,material_ids:ids,use_model:$('model').checked,idempotency_key:crypto.randomUUID(),options:{}});
    if(project===targetProject){await refresh();if(workspaceDetails)await workspaceDetails.refresh();}
  }
  function fileData(file) {return new Promise((resolve,reject)=>{const r=new FileReader();r.onload=()=>resolve(String(r.result).split(',')[1]);r.onerror=()=>reject(new Error('文件读取失败'));r.readAsDataURL(file);});}
  $('upload').addEventListener('click',()=>{if(project&&!busy)$('files').click();});
  $('files').addEventListener('change',async e=>{
    const files=Array.from(e.target.files || []);e.target.value='';if(!files.length)return;
    $('error').hidden=true;const uploadProject=project;lock(true);
    try {
      if(!project)throw new Error('请先选择项目');
      if(files.length>20 || files.some(f=>!/\.(pdf|png|jpe?g|webp)$/i.test(f.name)||!f.size||f.size>20*1024*1024))throw new Error('请选择1至20份PDF或图片，每份最多20MB');
      const ids=[],batch='agent-'+crypto.randomUUID();
      for(let i=0;i<files.length;i++){
        $('connection').textContent='正在保存到电脑 '+(i+1)+' / '+files.length;
        const entry=await api('/api/intake/receive',{name:files[i].name,data:await fileData(files[i]),project_id:uploadProject,batch});
        if(entry.project_id===uploadProject&&!entry.item_id&&['received','review'].includes(entry.status))ids.push(entry.id);
      }
      await begin(Array.from(new Set(ids)),uploadProject);$('connection').textContent='材料已保存到电脑，后台正在整理';
    }catch(err){error(err);$('connection').textContent='已保存的材料会保留，可用“整理项目待处理材料”继续';}finally{lock(false);}
  });
  $('organize').addEventListener('click',async()=>{
    const targetProject=project;lock(true);$('error').hidden=true;
    try{const q=await api('/api/intake');await begin(q.queue.filter(e=>e.project_id===targetProject&&!e.item_id&&['received','review'].includes(e.status)).map(e=>e.id),targetProject);}
    catch(e){error(e);}finally{lock(false);}
  });
  function button(text,action){const b=el('button',text);b.type='button';b.addEventListener('click',async()=>{b.disabled=true;try{await action();}catch(e){error(e);}finally{b.disabled=false;}});return b;}
  function mailError(e){$('mail-error').textContent=e.message || String(e);$('mail-error').hidden=false;}
  function mailTime(value){
    if(!value)return '';
    const date=new Date(typeof value==='number'?value*1000:value);
    return Number.isNaN(date.getTime())?'':date.toLocaleString('zh-CN',{month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'});
  }
  function mailControls(){
    const collecting=!!(mailState&&(mailState.collecting||mailState.running));
    $('mail-collect').disabled=mailBusy||collecting||!mailState||!mailState.enabled;
    $('mail-save').disabled=mailBusy||!mailState;
    $('mail-toggle').disabled=mailBusy||!mailState;
    $('mail-toggle').textContent=mailState&&mailState.enabled?'暂停自动收件':'开启自动收件';
  }
  function mailConfiguration(){
    return {enabled:!!mailState.enabled,interval_seconds:mailState.interval_seconds,since:mailState.since,
      use_model:!!mailState.use_model,default_project_id:mailState.default_project_id||''};
  }
  function mailProjectOptions(select,placeholder){
    const first=el('option',placeholder);first.value='';select.append(first);
    for(const p of projects){const option=el('option',p.name);option.value=p.id;select.append(option);}
  }
  async function switchProject(id,jobId=''){
    if(busy)throw new Error('材料正在保存，请稍后切换项目');
    if(id&&!projects.some(p=>p.id===id))throw new Error('该项目已不可用，请刷新项目列表');
    project=id;$('project').value=id;activePreview='';renderedJobs='';focusJob=jobId;$('result').hidden=true;
    $('pages').replaceChildren();$('downloads').replaceChildren();$('jobs').replaceChildren(el('p',project?'正在读取项目任务…':'新建项目后即可开始整理。'));
    $('pending-count').textContent='0';
    const url=new URL(location.href);if(project)url.searchParams.set('project',project);else url.searchParams.delete('project');history.replaceState(null,'',url);
    manualLink();projectHeading();
    const loadingDetails=workspaceDetails?workspaceDetails.setProject(project):Promise.resolve();
    if(project)await refresh();await loadingDetails;
  }
  function renderMailMessages(messages){
    const signature=JSON.stringify({messages,enabled:!!mailState.enabled,projects:projects.map(p=>[p.id,p.name])});if(signature===renderedMail)return;renderedMail=signature;
    $('mail-messages').replaceChildren();
    if(!messages.length){$('mail-messages').append(el('p','尚未收到新材料。可将发票 PDF、照片或付款凭证发到此邮箱。','muted'));return;}
    const labels={unassigned:'需要确定项目',waiting_agent:'等待开始整理',attention:'需要处理收件问题',
      queued:'等待整理',running:'正在整理',ready:'报销预览已准备',needs_input:'需要补充依据',
      failed:'处理未完成',stopped:'已停止尝试',ignored:'无需整理',no_materials:'没有可整理材料',
      duplicate:'材料已存在',received:'材料已收到',cancelled:'已取消'};
    for(const [index,message] of messages.entries()){
      const card=el('article',undefined,'mail-message');card.dataset.mailId=message.id;
      card.append(el('h3',message.subject||'无主题邮件'));
      if(message.sender)card.append(el('p','来自：'+message.sender,'muted'));
      card.append(el('p',(labels[message.status]||'等待处理')+' · '+(Number.isInteger(message.material_count)?message.material_count:0)+' 份材料'));
      for(const issue of Array.isArray(message.issues)?message.issues:[])if(typeof issue==='string')card.append(el('p',issue,'muted'));
      const assigned=projects.find(p=>p.id===message.project_id);
      if(assigned)card.append(el('p','所属项目：'+assigned.name));
      if(message.status==='unassigned'&&!message.project_id&&message.material_count>0){
        const label=el('label','这封邮件属于哪个项目？'),select=el('select');select.id='mail-message-project-'+index;label.htmlFor=select.id;
        mailProjectOptions(select,'请选择项目');
        if(projects.some(p=>p.id===mailSelections.get(message.id)))select.value=mailSelections.get(message.id);
        const assign=button(mailState.enabled?'确定项目并继续':'确定项目，开启收件后继续',async()=>{
          if(!select.value)throw new Error('请先选择这封邮件的项目');
          await mailAction('/api/agent/mail/assign',{id:message.id,project_id:select.value});mailSelections.delete(message.id);
        });
        assign.disabled=!select.value;
        select.addEventListener('change',()=>{mailSelections.set(message.id,select.value);assign.disabled=!select.value;});
        card.append(label,select,assign);
        if(projectNavigation)card.append(button('新建项目',()=>projectNavigation.openCreate()));
        if(!mailState.enabled)card.append(el('p','自动收件已暂停，确定项目后将等待重新开启。','muted'));
      }
      if(assigned&&message.job_id){
        card.append(button('查看整理任务',async()=>switchProject(message.project_id,message.job_id)));
      }
      $('mail-messages').append(card);
    }
  }
  function showMail(data,updateSettings=false){
    const account=JSON.stringify([data.address||'',data.folder||'']);
    if(account!==mailAccount){mailAccount=account;mailConfigured=false;renderedMail='';mailSelections.clear();}
    mailState=data;$('mail-address').textContent=data.address?(data.address+' · '+(data.folder||'INBOX')):'尚未配置收件邮箱';
    $('mail-pending-count').textContent=String(Number.isInteger(data.unresolved_count)?data.unresolved_count:(data.messages||[]).filter(m=>['unassigned','needs_input','attention'].includes(m.status)).length);
    const parts=[data.collecting||data.running?'正在收取邮件…':!data.enabled?'自动收件已暂停':data.suspended?'收件重试已暂停，检查网络或授权后可点立即收件':'自动收件已开启'];
    if(data.last_checked_at)parts.push('最近检查 '+mailTime(data.last_checked_at));
    if(data.enabled&&!data.suspended&&data.next_check_at)parts.push('下次检查 '+mailTime(data.next_check_at));
    $('mail-status').textContent=parts.join(' · ');
    $('mail-error').hidden=!data.last_error;if(data.last_error)$('mail-error').textContent=data.last_error;
    if(!mailConfigured||updateSettings){
      $('mail-since').value=data.since||'';$('mail-interval').value=data.interval_seconds||120;
      $('mail-project').value=projects.some(p=>p.id===data.default_project_id)?data.default_project_id:'';
      $('mail-model').checked=!!data.use_model;mailConfigured=true;
    }
    renderMailMessages(Array.isArray(data.messages)?data.messages:[]);mailControls();
  }
  async function refreshMail(){
    if(mailPolling||mailBusy)return;mailPolling=true;const requestedRevision=mailRevision;
    try{const data=await api('/api/agent/mail');if(requestedRevision===mailRevision)showMail(data);}
    catch(e){if(requestedRevision===mailRevision){$('mail-status').textContent='暂时无法读取收件状态；上传和整理任务仍可使用';mailError(e);}}
    finally{mailPolling=false;mailControls();}
  }
  async function mailAction(path,body,updateSettings=false){
    if(mailBusy)return;mailBusy=true;mailRevision++;mailControls();$('mail-error').hidden=true;
    try{showMail(await api(path,body),updateSettings);await refresh();}
    catch(e){mailError(e);}finally{mailBusy=false;mailControls();}
  }
  $('mail-collect').addEventListener('click',()=>mailAction('/api/agent/mail/collect',{}));
  $('mail-toggle').addEventListener('click',()=>{if(mailState)mailAction('/api/agent/mail/configure',{...mailConfiguration(),enabled:!mailState.enabled});});
  $('mail-configure').addEventListener('submit',e=>{
    e.preventDefault();if(!mailState||!$('mail-configure').reportValidity())return;
    mailAction('/api/agent/mail/configure',{enabled:!!mailState.enabled,interval_seconds:Number($('mail-interval').value),
      since:$('mail-since').value,use_model:$('mail-model').checked,default_project_id:$('mail-project').value},true);
  });
  function showPreview(job){
    const preview=job.preview;if(!preview)return;
    activePreview=job.id;$('result').hidden=false;$('summary').textContent='合计 '+(preview.total_cents/100).toFixed(2)+' 元 · '+preview.page_count+' 页 · 尚未核对';
    $('downloads').replaceChildren();
    for(const [label,name] of [['查看最终PDF',preview.pdf],['下载报销表',preview.xlsx]]){const a=el('a',label);a.href='/export/'+encodeURIComponent(name);a.target='_blank';a.rel='noopener';$('downloads').append(a);}
    const manual=el('a','到人工工作台核对');manual.href=$('manual').href;$('downloads').append(manual);
    $('pages').replaceChildren();
    for(const [i,name] of preview.preview_pages.entries()){const image=el('img');image.src='/export/'+encodeURIComponent(name);image.alt='报销预览第'+(i+1)+'页';image.loading='lazy';$('pages').append(image);}
  }
  function render(jobs){
    $('jobs').replaceChildren();
    $('pending-count').textContent=String(jobs.filter(j=>j.state==='needs_input').length);
    if(!jobs.length){$('jobs').append(el('p','还没有整理任务，上传材料即可开始。'));return;}
    const ordered=[...jobs].sort((a,b)=>(a.state==='needs_input'?0:a.state==='running'?1:2)-(b.state==='needs_input'?0:b.state==='running'?1:2));
    for(const job of ordered){
      const card=el('article',undefined,'job'+(job.state==='needs_input'?' confirmation-card':''));card.dataset.jobId=job.id;card.dataset.state=job.state;
      card.append(el('span',states[job.state]||job.state,'job-status'),el('h3',job.state==='needs_input'?'补充这项事实后继续整理':job.state==='ready'?'已整理为报销预览':'本次整理'));
      if(job.state!=='needs_input')card.append(el('p',job.message));
      for(const expense of job.expenses || []){
        card.append(el('p','报销名称：'+expense.title+' · '+(expense.amount_cents/100).toFixed(2)+' 元'));
        card.append(el('p','购买内容：'+expense.goods+' · 费用类别：'+expense.category,'muted'));
        if(expense.relation_kind==='model_inference')card.append(el('p','材料对应：结合商品、金额和商家推断，仍待核对','muted'));
        if(expense.relation_kind==='user_confirmed')card.append(el('p','材料对应：你已确认是同一笔；费用仍待核对','muted'));
      }
      const actions=el('div',undefined,'actions');
      if(['queued','running'].includes(job.state))actions.append(button('停止本次整理',async()=>{await api('/api/agent/cancel',{id:job.id});await refresh();}));
      if(['failed','stopped'].includes(job.state))actions.append(button('按剩余额度重试',async()=>{await api('/api/agent/retry',{id:job.id});await refresh();}));
      if(job.state==='needs_input' && (job.question?.type==='expense_content' || (job.last_results||[]).some(row=>row.tool==='classify_expense' && row.result?.content_status==='needs_input')))actions.append(button('按新规则检查商品',async()=>{await api('/api/agent/retry',{id:job.id});await refresh();}));
      if(job.preview)actions.append(button('查看报销预览',async()=>showPreview(job)));
      for(const itemId of job.item_ids||[])if(workspaceDetails)actions.append(button('查看费用详情',async()=>workspaceDetails.openExpense(itemId)));
      const manual=el('a','打开人工工作台');manual.href=$('manual').href;actions.append(manual);card.append(actions);
      if(job.question){
        card.append(el('p',job.question.text,'question-title'));
        const evidence=job.question.evidence_summary;
        for(const line of (Array.isArray(evidence)?evidence:typeof evidence==='string'?[evidence]:[])){
          if(typeof line==='string')card.append(el('p',line,'question-evidence muted'));
        }
        const materials=el('div',undefined,'materials');
        const materialLabels=new Map((job.question.material_ids || []).map((id,i)=>[id,'材料 '+(i+1)]));
        const facts=job.question.material_facts || [];
        for(const fact of Array.isArray(facts)?facts:[]){
          if(!fact||typeof fact!=='object')continue;
          const label=materialLabels.get(fact.id)||'材料';
          const description=[fact.goods,Number.isInteger(fact.amount_cents)?(fact.amount_cents/100).toFixed(2)+' 元':'',fact.merchant].filter(Boolean).join(' · ');
          if(description)card.append(el('p',label+'：'+description));
        }
        for(const id of job.question.material_ids || []){
          const a=el('a',materialLabels.get(id)+' · 查看原件');a.href='/intake-preview/'+encodeURIComponent(id);a.target='_blank';a.rel='noopener';
          if(workspaceDetails)a.addEventListener('click',e=>{if(!e.ctrlKey&&!e.metaKey&&!e.shiftKey){e.preventDefault();Promise.resolve(workspaceDetails.openMaterial(id)).catch(error);}});
          const img=el('img');img.src='/intake-page/'+encodeURIComponent(id)+'?page=0';img.alt=materialLabels.get(id);img.loading='lazy';a.prepend(img);materials.append(a);
        }card.append(materials);
        for(const choice of job.question.choices || []){
          const parts=[];
          for(const [kind,ids] of [['票据',choice.voucher_keys || []],['付款',choice.payment_ids || []],['明细',choice.support_ids || []]]){
            const labels=ids.map(k=>materialLabels.get(k.replace(/^q:/,''))).filter(Boolean);
            if(labels.length)parts.push(kind+'：'+labels.join('、'));
          }
          const relation=choice.action==='confirm_relation'?'同一笔，继续生成预览':choice.action==='not_same_expense'?'不是同一笔，补充材料':choice.label;
          const label=relation+(Number.isInteger(choice.amount_cents)?' · '+(choice.amount_cents/100).toFixed(2)+' 元':'')+(parts.length?'（'+parts.join('；')+'）':'');
          const b=button(label,async()=>{
            if(answersInFlight.has(job.id))return;answersInFlight.add(job.id);
            for(const candidate of card.querySelectorAll('.choice'))candidate.disabled=true;
            try{await api('/api/agent/answer',{id:job.id,choice:choice.id,question_id:job.question.id});await refresh();if(workspaceDetails)await workspaceDetails.refresh();}
            finally{answersInFlight.delete(job.id);for(const candidate of card.querySelectorAll('.choice'))candidate.disabled=false;}
          });b.className='choice';card.append(b);
        }
      }
      const details=el('details'),summary=el('summary','查看处理记录'),list=el('ol');details.append(summary,el('p','模型 '+job.models+' / '+job.limits.models+' 次 · 工具 '+job.tools+' / '+job.limits.tools+' 次','muted'),list);
      for(const event of job.events)list.append(el('li',event.message));card.append(details);$('jobs').append(card);
      if(focusJob===job.id){card.scrollIntoView({block:'center'});focusJob='';}
    }
    const latest=jobs.find(j=>j.preview);if(latest&&(!activePreview||activePreview===latest.id))showPreview(latest);
  }
  async function refresh(){
    if(polling)return;polling=true;
    const requestedProject=project;if(!requestedProject){polling=false;return;}
    try{const data=await api('/api/agent/jobs?project='+encodeURIComponent(requestedProject));if(requestedProject!==project)return;token=data.token;const signature=JSON.stringify(data.jobs);if(signature!==renderedJobs){renderedJobs=signature;render(data.jobs);if(workspaceDetails)workspaceDetails.refresh();}}
    catch(e){error(e);}finally{polling=false;if(requestedProject!==project)refresh();}
  }
  $('project').addEventListener('change',()=>switchProject($('project').value).catch(error));
  $('first-project').addEventListener('click',()=>{if(projectNavigation)projectNavigation.openCreate();});
  async function boot(){
    try{
      const state=await api('/api/state');token=state.token;projects=Array.isArray(state.projects)?state.projects:[];
      projectOptions();
      const preferred=new URLSearchParams(location.search).get('project');
      if(state.projects.some(p=>p.id===preferred))$('project').value=preferred;
      project=$('project').value;manualLink();
      if(window.AgentProjects)projectNavigation=window.AgentProjects.mount({host:$('project-navigation'),api,isBusy:()=>busy,onSelect:id=>switchProject(id),onCreated:created=>reloadProjects(created.id)});
      if(window.AgentDetails)workspaceDetails=window.AgentDetails.mount({materialsHost:$('workspace-materials'),expensesHost:$('workspace-expenses'),detailHost:$('workspace-details'),api,getProject:()=>project,onChanged:()=>refresh()});
      projectHeading();
      const model=(await api('/api/agent/jobs?project='+encodeURIComponent(project))).model;
      $('model').disabled=!model.available;$('model').checked=!!model.available;
      $('connection').textContent=model.available?'复杂问题可使用已配置模型，原件保存在电脑':'当前使用本地整理；模型未配置时，不确定的问题留待确认';
      if(!project){lock(false);$('connection').textContent='先在左侧新建项目，即可上传并整理材料。';}
      await refresh();refreshMail();setInterval(()=>{if(!document.hidden)refresh();},1500);
      setInterval(()=>{if(!document.hidden){refreshMail();if(workspaceDetails)workspaceDetails.refresh();if(!busy&&Date.now()>=nextProjectsCheck){nextProjectsCheck=Date.now()+15000;reloadProjects().catch(error);}}},5000);
    }catch(e){error(e);}
  }
  boot();
})();
