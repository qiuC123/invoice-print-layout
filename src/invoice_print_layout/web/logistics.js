'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const node = (tag, value='', cls='') => { const el=document.createElement(tag);el.textContent=value;if(cls)el.className=cls;return el; };
  const labels={lunch:'午饭',dinner:'晚饭',supper:'夜宵',breakfast:'早餐',lodging:'住宿变化'};
  const states={waiting:'等待回复',clarify:'需要补充',escalated:'已生成上报事项',review:'待你核对',confirmed:'已确认，等待发送',sent:'已核验发送',expired:'业务日期已过，需人工核对'};
  const delivery={pending:'待发送',in_flight:'发送处理中，待核验',uncertain:'发送结果不明，需核验',sent:'已核验发送',cancelled:'已取消'};
  const stages={group:'群内询问',direct:'私聊催问',clarify:'追问',escalate:'截止上报',review:'请你核对',supplier:'订餐通知'};
  const issueLabels={source_not_fresh:'消息记录可能尚未更新',unknown_shards:'部分历史消息未能读取',no_shards_scanned:'尚未读到历史消息',freshness_unverifiable:'暂时无法确认消息是否完整',session_ahead_of_history:'会话有更新，历史消息尚未同步',window_at_limit:'消息较多，可能还有内容未读取',sender_identity_missing:'发件人身份待核验',unrecognized_quote_format:'引用内容需要核对',account_identity_unverified:'微信账号身份待核验',ambiguous_project:'所属项目需要确认'};
  const messageTypes={1:'文字',3:'图片',34:'语音',43:'视频',49:'文件或引用',10000:'系统消息',10002:'系统消息'};
  const conversationKey = item => JSON.stringify([item.account_id||'',item.chat_id||'']);
  const readableText = message => {const value=(message.text||'').trim();return Number(message.type_code)!==1&&(/^\[(图片|语音|视频|文件|引用|动画表情|系统消息)\]$/.test(value)||/^\s*<(?:\?xml|msg|appmsg)\b/i.test(value))?'':value;};
  let snapshot={projects:[],tasks:[],outbox:[]}, inbox={groups:[],messages:[]}, selected='', purpose='', activeTab='inbox', initialized=false, inboxVersion=0, snapshotVersion=0;
  let groupCategories={site:'现场沟通',meals:'订餐',lodging:'住宿',invoices:'票据',other:'其他'};
  let messageCategories={meals:'餐饮',lodging:'住宿',invoices:'票据',materials:'进出场资料',other:'其他',unclassified:'待分类'};
  const storageKey='logistics.selectedProject';
  const displayTime = value => {
    if(typeof value!=='number')return String(value??'时间未知');
    const date=new Date(value<1e12?value*1000:value);
    return Number.isNaN(date.getTime())?'时间未知':date.toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false});
  };
  const error = e => { $('logisticsError').textContent=e.message||String(e);$('logisticsError').hidden=false; };
  const notice = value => { $('logisticsNotice').textContent=value;$('logisticsNotice').hidden=false; };
  const empty = value => node('p',value,'empty');
  function options(select, values, first) { select.replaceChildren();if(first!==undefined)select.append(new Option(first,''));for(const [value,label] of Object.entries(values))select.append(new Option(label,value)); }
  async function request(url, body) {
    const response=await fetch(url,body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json','X-Workbench-Token':snapshot.token||''},body:JSON.stringify(body)});
    let data;try{data=await response.json();}catch{throw Error('工作台返回了无法读取的内容，请刷新重试。');}
    if(!response.ok)throw Error(typeof data.error==='string'?data.error:'读取或保存失败，请检查本机工作台。');
    return data;
  }
  function renderProjects() {
    const list=$('projectList');list.replaceChildren();
    if(!snapshot.projects.length)list.append(node('p','还没有项目，点击新建开始。','muted'));
    for(const project of snapshot.projects){
      const button=node('button',project.name,'project-option'+(project.id===selected?' active':''));button.dataset.projectId=project.id;button.setAttribute('aria-current',project.id===selected?'true':'false');
      if(project.paused||project.status==='draft')button.append(node('small',project.paused?'已暂停':'待配置'));
      button.onclick=()=>selectProject(project.id);list.append(button);
      if(project.id===selected){
        const sub=node('div','','project-subnav');sub.setAttribute('aria-label','项目微信群用途');
        const ownGroups=inbox.groups.filter(group=>group.project_id===selected);
        for(const [key,label] of Object.entries({'':'全部消息',...groupCategories})){
          const count=ownGroups.filter(group=>group.category===key).length;
          if(key&&!count)continue;
          const option=node('button','','purpose-option');option.type='button';option.dataset.purpose=key;
          option.setAttribute('aria-pressed',String(activeTab==='inbox'&&purpose===key));
          option.append(node('span',label));if(key)option.append(node('span',`${count}群`,'purpose-count'));
          option.onclick=()=>{purpose=key;$('messageFilter').value='';$('groupFilter').value='';showTab('inbox');renderMessages();};
          sub.append(option);
        }
        list.append(sub);
      }
    }
    $('unassignedInbox').classList.toggle('active',selected==='');$('unassignedInbox').setAttribute('aria-current',selected===''?'true':'false');
    const project=snapshot.projects.find(p=>p.id===selected);
    $('projectTitle').textContent=project?project.name:'待归属收件箱';
    $('projectBreadcrumb').textContent=project?`我的项目 / ${project.name}`:'我的项目 / 待归属消息';
    $('projectSummary').textContent=project?[(project.site_name||''),(project.start&&project.end?`${project.start} 至 ${project.end}`:'项目日期待配置'),project.paused?'已暂停':''].filter(Boolean).join(' · '):'这里仅展示尚未归属项目的消息，请核对后分配。';
    if(!project)$('groupForm').hidden=true;
    $('showGroupForm').hidden=!project;
    for(const button of document.querySelectorAll('[data-tab]'))button.hidden=!project&&button.dataset.tab!=='inbox';
    $('groupsHint').textContent=project?'群名称用于辨认，用途用于分类；只填名称也可先保存，之后再补接收工具提供的稳定标识。':'请先从左侧选择一个项目，再管理该项目的微信群。';
    renderTasks();
  }
  function resetGroupForm() { const form=$('groupForm');form.reset();form.hidden=true;$('showGroupForm').setAttribute('aria-expanded','false');delete form.dataset.editing;for(const name of ['account_id','chat_id'])form.elements.namedItem(name).readOnly=false;form.querySelector('button[type=submit]').textContent='保存群';$('manualBinding').open=false;$('cancelGroupEdit').hidden=true; }
  function selectProject(id) {
    selected=id;purpose='';inbox={groups:[],messages:[]};try{localStorage.setItem(storageKey,id);}catch{}
    $('logisticsError').hidden=true;$('logisticsNotice').hidden=true;$('messageFilter').value='';$('groupFilter').value='';resetGroupForm();showTab('inbox');loadInbox();
  }
  function showTab(tab) { activeTab=tab;for(const name of ['inbox','groups','tasks'])$(name+'Panel').hidden=name!==tab;for(const button of document.querySelectorAll('[data-tab]')){button.classList.toggle('active',button.dataset.tab===tab);button.setAttribute('aria-pressed',String(button.dataset.tab===tab));}renderProjects(); }
  function renderTasks() {
    const tasks=snapshot.tasks.filter(task=>selected&&task.project_id===selected), taskIds=new Set(tasks.map(task=>task.id));
    const cards=$('logisticsTasks');cards.replaceChildren();
    if(!tasks.length)cards.append(empty(selected?'本项目暂无询问任务。完成负责人、日期和群绑定后再启用。':'选择项目后查看对应任务。'));
    for(const task of tasks){const row=node('article','','card');row.append(node('strong',`${task.business_day} · ${labels[task.kind]||task.kind}`),node('p',states[task.state]||task.state,'logistics-status'),node('p',task.quantity==null?'尚无明确数量':`${task.quantity}${task.kind==='lodging'?'人':'份'} · 第${task.revision}版`));if(task.clarification)row.append(node('p',task.clarification,'error'));row.append(node('small',`群内 ${(task.ask_at||'').slice(11,16)} → 私聊 ${(task.direct_at||'').slice(11,16)} → 上报 ${(task.deadline||'').slice(11,16)}（北京时间）`));cards.append(row);}
    const out=$('logisticsOutbox');out.replaceChildren();const actions=snapshot.outbox.filter(action=>selected&&taskIds.has(action.task_id));
    if(!actions.length)out.append(node('p','暂无本项目消息动作。','muted'));
    for(const action of actions.slice().reverse()){const row=node('div','','logistics-task');row.append(node('strong',`${stages[action.stage]||action.stage} · ${delivery[action.state]||action.state}`),node('p',action.payload?.text||''));out.append(row);}
  }
  function renderMessages() {
    const list=$('logisticsInbox');list.replaceChildren();
    const category=$('messageFilter').value,chat=$('groupFilter').value;
    const messages=inbox.messages.filter(message=>(selected?message.project_id===selected:message.project_id==null)&&(!category||message.category===category)&&(!chat||conversationKey(message)===chat)&&(!purpose||inbox.groups.some(group=>group.project_id===selected&&group.account_id&&group.chat_id&&group.category===purpose&&conversationKey(group)===conversationKey(message))));
    $('inboxHeading').textContent=`${purpose?groupCategories[purpose]:'全部消息'} · ${messages.length}`;
    if(!messages.length){list.append(empty(category||chat||purpose?'这个筛选下暂无消息。':selected?'本项目还没有接收到消息。可以先整理微信群；这里不会把其他项目的消息混入。':'暂无待归属消息。接收尚未联通时，收件箱为空不代表现场没有回复。'));return;}
    for(const message of messages){
      const card=node('article','','card');card.dataset.messageKey=message.key;
      const group=message.account_id&&message.chat_id?inbox.groups.find(g=>conversationKey(g)===conversationKey(message)):null;
      const direction={inbound:'收到',outbound:'发出',incoming:'收到',outgoing:'发出',unknown:'方向未知'}[message.direction]||'方向未知';
      card.append(node('div',`${group?.name||'未绑定会话'} · ${message.sender_id||'未知发件人'} · ${displayTime(message.timestamp)} · ${direction}`,'message-meta'));
      if(message.quoted_text)card.append(node('blockquote',message.quoted_text,'message-quote'));
      const type=messageTypes[Number(message.type_code)]||'其他消息',body=readableText(message);
      if(Number(message.type_code)!==1)card.append(node('span',type,'badge'));
      card.append(node('p',body||`${type}内容待核对。`,'message-body'));
      if(message.issues?.length)card.append(node('p',[...new Set(message.issues.map(issue=>issueLabels[issue]||'来源需要核验'))].join('；'),'error'));
      const foot=node('div','','message-footer');foot.append(node('span',messageCategories[message.category]||'待分类','badge'));
      const details=node('details','','message-adjust');details.append(node('summary','调整归属 / 分类'));
      const form=node('form','','assign-form');const projectLabel=node('label','归属项目'),projectSelect=node('select');projectSelect.name='project_id';projectSelect.required=true;
      options(projectSelect,Object.fromEntries(snapshot.projects.map(p=>[p.id,p.name])),'选择项目');projectSelect.value=message.project_id||'';projectLabel.append(projectSelect);
      const categoryLabel=node('label','消息分类'),categorySelect=node('select');categorySelect.name='category';options(categorySelect,messageCategories);categorySelect.value=message.category||'unclassified';categoryLabel.append(categorySelect);
      const save=node('button','保存归属','primary');save.type='submit';form.append(projectLabel,categoryLabel,save);details.append(form);foot.append(details);card.append(foot);
      form.onsubmit=async event=>{event.preventDefault();save.disabled=true;try{await request('/api/logistics/messages/assign',{key:message.key,project_id:projectSelect.value,category:categorySelect.value});notice('消息归属与分类已保存。');await loadInbox();}catch(e){error(e);}finally{save.disabled=false;}};
      list.append(card);
    }
  }
  function renderGroups() {
    const list=$('logisticsGroups');list.replaceChildren();const groups=inbox.groups.filter(group=>selected&&group.project_id===selected);
    if(!groups.length)list.append(empty(selected?'还没有微信群。先填写名称和用途，稳定标识可以稍后补充。':'选择项目后查看对应微信群。'));
    for(const [category,label] of Object.entries(groupCategories)){
      const members=groups.filter(g=>g.category===category);if(!members.length)continue;
      const section=node('section','','card');section.append(node('h3',label));
      for(const group of members){const row=node('div','','group-row');row.dataset.groupId=group.id;const title=node('div');title.append(node('strong',group.name),node('p',group.account_id&&group.chat_id?'已保存会话标识 · 不代表接收已联通':'未绑定稳定标识','muted'));
        const edit=node('button',group.account_id&&group.chat_id?'查看绑定':'补充绑定');edit.type='button';edit.onclick=()=>{const form=$('groupForm');form.hidden=false;$('showGroupForm').setAttribute('aria-expanded','true');form.dataset.editing=group.id;for(const name of ['name','category','account_id','chat_id'])form.elements.namedItem(name).value=group[name]||'';form.elements.namedItem('account_id').readOnly=!!group.account_id;form.elements.namedItem('chat_id').readOnly=!!group.chat_id;$('manualBinding').open=true;$('cancelGroupEdit').hidden=false;form.querySelector('button[type=submit]').textContent='更新群信息';form.scrollIntoView({block:'center'});};title.append(edit);
        const select=node('select');select.setAttribute('aria-label',`${group.name}用途`);options(select,groupCategories);select.value=group.category;select.onchange=async()=>{select.disabled=true;const project=selected;try{await request('/api/logistics/groups/category',{project_id:project,id:group.id,category:select.value});if(selected===project)await loadInbox();}catch(e){select.value=group.category;error(e);}finally{select.disabled=false;}};row.append(title,select);section.append(row);
      }list.append(section);
    }
  }
  async function loadInbox() {
    const version=++inboxVersion,project=selected;inbox={groups:[],messages:[]};$('logisticsInbox').replaceChildren(empty('正在读取消息…'));$('logisticsGroups').replaceChildren(empty('正在读取微信群…'));
    try{
      const data=await request('/api/logistics/inbox?project_id='+encodeURIComponent(project));if(version!==inboxVersion||project!==selected)return;
      inbox=data;groupCategories=data.group_categories||groupCategories;messageCategories=data.message_categories||messageCategories;
      if(purpose&&!data.groups.some(group=>group.project_id===selected&&group.category===purpose))purpose='';
      $('inboxCount').textContent=Number(data.total)>data.messages.length?`共 ${data.total} 条消息，当前展示最近 ${data.messages.length} 条；筛选仅应用于已加载消息。`:'保留原消息，手动核对项目归属和业务分类。';
      const oldCategory=$('messageFilter').value,oldChat=$('groupFilter').value;options($('messageFilter'),messageCategories,'全部分类');$('messageFilter').value=oldCategory;options($('groupFilter'),Object.fromEntries(data.groups.filter(g=>g.project_id===project&&g.account_id&&g.chat_id).map(g=>[conversationKey(g),g.name])),'全部群');$('groupFilter').value=oldChat;options($('newGroupCategory'),groupCategories);
      const receiver=data.receiver||{};const connected=receiver.status==='connected';$('transportStatus').textContent=`${connected?'消息接收已联通':'消息接收尚未联通'}${receiver.detail?'：'+receiver.detail:'。'} ${snapshot.transport_enabled?'自动询问已配置。':'自动询问尚未启用。'} 群绑定和待发记录不代表消息已发出。`;
      renderProjects();renderMessages();renderGroups();
    }catch(e){if(version!==inboxVersion)return;error(e);$('transportStatus').textContent='消息接收状态读取失败，当前状态未知。';$('logisticsInbox').replaceChildren(empty('收件箱读取失败，请刷新重试。'));$('logisticsGroups').replaceChildren(empty('微信群读取失败，请刷新重试。'));}
  }
  async function loadLogistics() {
    const version=++snapshotVersion;$('refreshLogistics').disabled=true;$('logisticsError').hidden=true;
    try{const data=await request('/api/logistics');if(version!==snapshotVersion)return;snapshot=data;
      if(!initialized){let saved=null;try{saved=localStorage.getItem(storageKey);}catch{}selected=saved===null?(data.projects[0]?.id||''):saved;initialized=true;}
      if(selected&&!snapshot.projects.some(p=>p.id===selected)){selected='';purpose='';showTab('inbox');}renderProjects();await loadInbox();
    }catch(e){if(version===snapshotVersion){error(e);$('transportStatus').textContent='项目状态读取失败，当前接收与任务状态未知。';}}
    finally{if(version===snapshotVersion)$('refreshLogistics').disabled=false;}
  }
  $('projectForm').onsubmit=async event=>{event.preventDefault();const form=event.currentTarget,button=form.querySelector('button');button.disabled=true;try{const project=await request('/api/logistics/projects',{name:form.elements.namedItem('name').value.trim(),site_name:form.elements.namedItem('site_name').value.trim()});snapshot.projects.push(project);form.reset();form.hidden=true;$('showProjectForm').setAttribute('aria-expanded','false');selectProject(project.id);notice('项目已创建；还未启用消息接收或定时询问。');}catch(e){error(e);}finally{button.disabled=false;}};
  $('groupForm').onsubmit=async event=>{event.preventDefault();const form=event.currentTarget,project=selected,button=form.querySelector('button[type=submit]');const body={project_id:project};for(const name of ['name','category','account_id','chat_id'])body[name]=form.elements.namedItem(name).value.trim();if(!!body.account_id!==!!body.chat_id){$('manualBinding').open=true;error(Error('账号标识和群会话标识需要一起填写，也可以都留空。'));return;}if(form.dataset.editing)body.id=form.dataset.editing;button.disabled=true;try{await request('/api/logistics/groups',body);if(project===selected){resetGroupForm();await loadInbox();}notice('群信息已保存；是否收到消息请查看接收状态。');}catch(e){error(e);}finally{button.disabled=false;}};
  $('showProjectForm').onclick=()=>{$('projectForm').hidden=!$('projectForm').hidden;$('showProjectForm').setAttribute('aria-expanded',String(!$('projectForm').hidden));if(!$('projectForm').hidden)$('projectForm').elements.namedItem('name').focus();};
  $('showGroupForm').onclick=()=>{resetGroupForm();$('groupForm').hidden=false;$('showGroupForm').setAttribute('aria-expanded','true');$('cancelGroupEdit').hidden=false;$('groupForm').elements.namedItem('name').focus();};
  const cancelEdit=node('button','取消编辑');cancelEdit.id='cancelGroupEdit';cancelEdit.type='button';cancelEdit.hidden=true;cancelEdit.onclick=resetGroupForm;$('groupForm').append(cancelEdit);
  $('unassignedInbox').onclick=()=>{selectProject('');showTab('inbox');};$('refreshLogistics').onclick=loadLogistics;$('messageFilter').onchange=renderMessages;$('groupFilter').onchange=renderMessages;
  for(const button of document.querySelectorAll('[data-tab]'))button.onclick=()=>showTab(button.dataset.tab);
  options($('newGroupCategory'),groupCategories);loadLogistics();
})();
