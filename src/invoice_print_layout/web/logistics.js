'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const node = (tag, value='', cls='') => { const el=document.createElement(tag);el.textContent=value;if(cls)el.className=cls;return el; };
  const issueLabels={source_not_fresh:'消息记录可能尚未更新',unknown_shards:'部分历史消息未能读取',no_shards_scanned:'尚未读到历史消息',freshness_unverifiable:'暂时无法确认消息是否完整',session_ahead_of_history:'会话有更新，历史消息尚未同步',window_at_limit:'消息较多，可能还有内容未读取',sender_identity_missing:'发件人身份待核验',unrecognized_quote_format:'引用内容需要核对',account_identity_unverified:'微信账号身份待核验',ambiguous_project:'所属项目需要确认'};
  const messageTypes={1:'文字',3:'图片',34:'语音',43:'视频',49:'文件或引用',10000:'系统消息',10002:'系统消息'};
  const conversationKey = item => JSON.stringify([item.account_id||'',item.chat_id||'']);
  const readableText = message => {const value=(message.text||'').trim();return Number(message.type_code)!==1&&(/^\[(图片|语音|视频|文件|引用|动画表情|系统消息)\]$/.test(value)||/^\s*<(?:\?xml|msg|appmsg)\b/i.test(value))?'':value;};
  let snapshot={projects:[],tasks:[],outbox:[]}, inbox={groups:[],messages:[]}, selected='', activeTab='inbox', initialized=false, inboxVersion=0, snapshotVersion=0;
  let todos=[], todoProject='', todoVersion=0, editingTodo=null;
  let todoKind=new URLSearchParams(location.search).get('task_kind')==='scheduled'?'scheduled':'temporary', photoGroups=[];
  const supportCategories={materials:'照片 / 进出场资料',invoices:'发票 / 报销资料'};
  let groupCategories={site:'现场沟通',meals:'订餐',lodging:'住宿',invoices:'票据',other:'其他'};
  let messageCategories={meals:'餐饮',lodging:'住宿',invoices:'票据',materials:'进出场资料',other:'其他',unclassified:'待分类'};
  const storageKey='logistics.selectedProject';
  const displayTime = value => {
    if(typeof value!=='number')return String(value??'时间未知');
    const date=new Date(value<1e12?value*1000:value);
    return Number.isNaN(date.getTime())?'时间未知':date.toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false});
  };
  const error = e => { $('logisticsError').textContent=e.message||String(e);$('logisticsError').hidden=false; };
  let noticeTimer=null,noticeDelay=5000;
  const hideNotice = () => { clearTimeout(noticeTimer);$('logisticsNotice').hidden=true; };
  function scheduleNotice(){clearTimeout(noticeTimer);const target=$('logisticsNotice');if(!target.hidden&&!target.matches(':hover')&&!target.contains(document.activeElement))noticeTimer=setTimeout(hideNotice,noticeDelay);}
  function notice(value,delay=5000){
    clearTimeout(noticeTimer);noticeDelay=delay;const target=$('logisticsNotice'),close=node('button','','notice-close');close.type='button';close.setAttribute('aria-label','关闭提示');close.onclick=hideNotice;
    target.replaceChildren(node('span',value),close);target.hidden=false;scheduleNotice();
  }
  $('logisticsNotice').onmouseenter=()=>clearTimeout(noticeTimer);$('logisticsNotice').onmouseleave=scheduleNotice;
  $('logisticsNotice').addEventListener('focusin',()=>clearTimeout(noticeTimer));$('logisticsNotice').addEventListener('focusout',()=>queueMicrotask(scheduleNotice));
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
    $('supportActions').hidden=!project;
  }
  function resetGroupForm() { const form=$('groupForm');form.reset();form.hidden=true;$('showGroupForm').setAttribute('aria-expanded','false');delete form.dataset.editing;for(const name of ['account_id','chat_id'])form.elements.namedItem(name).readOnly=false;form.querySelector('button[type=submit]').textContent='保存群';$('manualBinding').open=false;$('cancelGroupEdit').hidden=true; }
  function selectProject(id) {
    selected=id;inbox={groups:[],messages:[]};resetTodoForm();try{localStorage.setItem(storageKey,id);}catch{}
    $('logisticsError').hidden=true;hideNotice();$('messageFilter').value='';$('groupFilter').value='';resetGroupForm();showTab(id&&['invoices','photos','tasks'].includes(activeTab)?activeTab:'inbox');loadInbox();
  }
  function showTab(tab) {
    if(tab!==activeTab)hideNotice();
    activeTab=selected&&['inbox','groups','tasks','invoices','photos'].includes(tab)?tab:'inbox';
    for(const name of ['inbox','groups','tasks','invoices','photos'])$(name+'Panel').hidden=name!==activeTab;
    for(const button of document.querySelectorAll('[data-tab]')){button.classList.toggle('active',button.dataset.tab===activeTab);button.setAttribute('aria-pressed',String(button.dataset.tab===activeTab));}
    $('transportStatus').hidden=activeTab!=='groups';document.querySelector('main>footer').hidden=['invoices','photos'].includes(activeTab);
    if(activeTab==='tasks')loadTodos();
    const frame=$('invoiceFrame');
    // Replace the entire document on project change so selection and dialogs cannot leak.
    if(frame.dataset.project!==selected){frame.removeAttribute('src');delete frame.dataset.project;}
    if(activeTab==='invoices'&&frame.dataset.project!==selected){frame.dataset.project=selected;frame.src='/?project='+encodeURIComponent(selected)+'&embedded=1';frame.title=(snapshot.projects.find(p=>p.id===selected)?.name||'项目')+' · 发票报销';}
    const photos=$('photosFrame');
    if(photos.dataset.project!==selected){photos.removeAttribute('src');delete photos.dataset.project;}
    if(activeTab==='photos'&&photos.dataset.project!==selected){photos.dataset.project=selected;photos.src='/photos?project='+encodeURIComponent(selected);}
    const url=new URL(location.href);if(selected)url.searchParams.set('project',selected);else url.searchParams.delete('project');url.searchParams.set('tab',activeTab);history.replaceState(null,'',url);
    renderProjects();
  }
  function resetTodoForm() {
    editingTodo=null;$('todoForm').reset();$('todoKind').value=todoKind;$('todoSave').textContent='添加待办';$('todoCancel').hidden=true;
    $('todoForm').elements.namedItem('start_on').value=new Date(Date.now()+8*3600000).toISOString().slice(0,10);
    $('todoForm').elements.namedItem('end_on').value=snapshot.projects.find(p=>p.id===selected)?.end||'';
    syncTodoKind();
  }
  function syncTodoKind() {
    const scheduled=$('todoKind').value==='scheduled';$('todoSchedule').hidden=!scheduled;$('todoSchedule').disabled=!scheduled;$('todoDueLabel').hidden=scheduled;
    for(const name of ['times','start_on'])$('todoForm').elements.namedItem(name).required=scheduled;
  }
  function renderPhotoGroups(chosen=null) {
    const list=$('todoGroups');list.replaceChildren();
    for(const group of photoGroups){const label=node('label',group.name,'todo-check'),check=node('input');check.type='checkbox';check.name='photo_group';check.value=group.id;check.checked=chosen===null||chosen.includes(group.id);label.prepend(check);list.append(label);}
    if(!photoGroups.length)list.append(node('p','请先在“现场照片 → 采集与归档设置”配置来源群。','muted'));
  }
  function selectTodoKind(kind) {
    hideNotice();todoKind=kind;resetTodoForm();renderPhotoGroups();renderTodos();
    const url=new URL(location.href);url.searchParams.set('task_kind',kind);history.replaceState(null,'',url);
  }
  async function loadTodos() {
    const project=selected,version=++todoVersion;todoProject='';todos=[];$('logisticsTasks').replaceChildren(empty('正在读取待办…'));
    try{const data=await request('/api/logistics/todos?project_id='+encodeURIComponent(project));if(version!==todoVersion||project!==selected)return;todos=data.items;photoGroups=data.photo_groups||[];if(!editingTodo)renderPhotoGroups();todoProject=project;renderTodos();}
    catch(e){if(version!==todoVersion||project!==selected)return;error(e);$('logisticsTasks').replaceChildren(empty('待办读取失败，请刷新重试。'));}
  }
  function renderTodos() {
    const cards=$('logisticsTasks');cards.replaceChildren();if(todoProject!==selected)return;
    for(const kind of ['temporary','scheduled'])$(kind+'Tasks').setAttribute('aria-pressed',String(todoKind===kind));
    $('todoFilter').disabled=todoKind==='scheduled';$('todoFilter').closest('label').hidden=todoKind==='scheduled';
    const categoryItems=todos.filter(t=>(t.kind||'temporary')===todoKind);
    $('todoCount').textContent=todoKind==='scheduled'?`定时任务 ${categoryItems.length} · 每日执行（北京时间）`:`未完成 ${categoryItems.filter(t=>t.status==='pending').length} · 已完成 ${categoryItems.filter(t=>t.status==='done').length}`;
    const items=categoryItems.filter(t=>todoKind==='scheduled'||!$('todoFilter').value||t.status===$('todoFilter').value);
    if(!items.length)cards.append(empty('暂无符合条件的待办，可在下方添加。'));
    for(const item of items){
      const row=node('article','','card todo-item');row.dataset.todoId=item.id;
      const scheduled=item.kind==='scheduled',config=item.schedule||{};
      const line=node('div','','todo-line'),check=node('button',scheduled?(config.enabled?'暂停任务':'启用任务'):(item.status==='done'?'重新打开':'标为完成'),'todo-toggle');check.type='button';
      line.append(node('strong',item.title,item.status==='done'?'todo-done':''));row.append(line);
      const remove=node('button','×','todo-delete');remove.type='button';remove.title='删除任务';remove.setAttribute('aria-label','删除任务：'+item.title);row.append(remove);
      remove.onclick=async()=>{const project=selected;remove.disabled=true;try{const deleted=await request('/api/logistics/todos/delete',{project_id:project,id:item.id,revision:item.revision});if(selected===project){if(editingTodo?.id===item.id)resetTodoForm();await loadTodos();notice('任务已删除。'+(scheduled?'后续定时读取已停止，已开始的读取会继续完成。':''),15000);const undo=node('button','撤销删除');undo.type='button';undo.onclick=async()=>{undo.disabled=true;try{await request('/api/logistics/todos/restore',{project_id:project,id:deleted.id,revision:deleted.revision});if(selected===project){await loadTodos();notice(scheduled?'任务已恢复，当前为暂停状态，可点击“启用任务”。':'任务已恢复。');}}catch(e){if(selected===project)error(e);}finally{undo.disabled=false;}};$('logisticsNotice').append(undo);}}catch(e){if(selected===project)error(e);}finally{remove.disabled=false;}};
      if(item.note)row.append(node('p',item.note,'todo-note'));
      if(scheduled){
        const expired=config.end_on&&config.end_on<new Date(Date.now()+8*3600000).toISOString().slice(0,10);
        row.append(node('p',`${expired?'已到结束日期':config.enabled?'已启用':'已暂停'} · 每天 ${(config.times||[]).join('、')} · ${config.start_on} 至 ${config.end_on||'不设结束日期'}`,'muted'));
        row.append(node('p','来源群：'+(config.group_ids||[]).map(id=>photoGroups.find(g=>g.id===id)?.name||'来源群已移除').join('、'),'muted'));
        const run=item.last_run,labels={queued:'等待执行',running:'读取中',done:'已完成',partial:'有待核对项',failed:'读取失败',interrupted:'执行中断',cancelled:'已取消'};
        row.append(node('p',run?`最近执行 ${run.occurrence} · ${labels[run.state]||run.state} · ${run.message}`:'尚未执行，到点后自动读取。','muted'));
      }else row.append(node('p',`${item.status==='done'?'已完成':'待完成'} · ${item.due_date?'截止 '+item.due_date:'未设置截止日期'}`,'muted'));
      const edit=node('button','编辑','todo-edit');edit.type='button';edit.onclick=()=>{editingTodo={...item};for(const name of ['title','note','due_date'])$('todoForm').elements.namedItem(name).value=item[name];$('todoKind').value=item.kind||'temporary';$('todoForm').elements.namedItem('times').value=(config.times||[]).join(', ');for(const name of ['start_on','end_on'])$('todoForm').elements.namedItem(name).value=config[name]||'';$('todoForm').elements.namedItem('enabled').checked=config.enabled!==false;renderPhotoGroups(config.group_ids||[]);syncTodoKind();$('todoSave').textContent='保存修改';$('todoCancel').hidden=false;$('todoForm').elements.namedItem('title').focus();};const actions=node('div','','todo-actions');actions.append(edit,check);row.append(actions);
      check.onclick=async()=>{const project=selected;check.disabled=true;const body=scheduled?{...item,schedule:{...config,enabled:!config.enabled}}:{...item,status:item.status==='done'?'pending':'done'};try{await request('/api/logistics/todos',{...body,project_id:project});if(selected===project){if(editingTodo?.id===item.id)resetTodoForm();await loadTodos();}}catch(e){if(selected===project){error(e);}}finally{check.disabled=false;}};
      cards.append(row);
    }
  }
  function renderMessages() {
    const list=$('logisticsInbox');list.replaceChildren();
    const category=$('messageFilter').value,chat=$('groupFilter').value;
    const messages=inbox.messages.filter(message=>(selected?message.project_id===selected:message.project_id==null)&&(!category||message.category===category)&&(!chat||conversationKey(message)===chat));
    $('inboxHeading').textContent=`${chat?$('groupFilter').selectedOptions[0]?.textContent||'微信群消息':selected?'照片与报销辅助资料':'待归属资料'} · ${messages.length}`;
    if(!messages.length){list.append(empty(category||chat?'这个筛选下暂无消息。':selected?'本项目暂无照片或报销辅助消息。照片请到“现场照片”按群和日期手动读取；报销材料请到“发票报销”导入。':'暂无待归属消息。'));return;}
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
      const data=await request('/api/logistics/inbox?scope=support&project_id='+encodeURIComponent(project));if(version!==inboxVersion||project!==selected)return;
      inbox=data;groupCategories=data.group_categories||groupCategories;messageCategories=data.message_categories||messageCategories;
      $('inboxCount').textContent=(Number(data.total)>data.messages.length?`共 ${data.total} 条辅助资料，当前展示最近 ${data.messages.length} 条。`:'')+'这里只展示已入库的相关资料，不读取全群聊天。普通聊天按需读取尚未接通。';
      const oldCategory=$('messageFilter').value,oldChat=$('groupFilter').value;options($('messageFilter'),project?supportCategories:messageCategories,'全部分类');$('messageFilter').value=oldCategory;options($('groupFilter'),Object.fromEntries(data.groups.filter(g=>g.project_id===project&&g.account_id&&g.chat_id).map(g=>[conversationKey(g),g.name])),'全部群');$('groupFilter').value=oldChat;options($('newGroupCategory'),groupCategories);
      const receiver=data.receiver||{};const connected=receiver.status==='connected';$('transportStatus').textContent=`${connected?'消息接收已联通':'消息接收尚未联通'}${receiver.detail?'：'+receiver.detail:'。'} ${snapshot.transport_enabled?'自动询问已配置。':'自动询问尚未启用。'} 群绑定和待发记录不代表消息已发出。`;
      renderProjects();renderMessages();renderGroups();
    }catch(e){if(version!==inboxVersion)return;error(e);$('transportStatus').textContent='消息接收状态读取失败，当前状态未知。';$('logisticsInbox').replaceChildren(empty('收件箱读取失败，请刷新重试。'));$('logisticsGroups').replaceChildren(empty('微信群读取失败，请刷新重试。'));}
  }
  async function loadLogistics() {
    const version=++snapshotVersion;$('refreshLogistics').disabled=true;$('logisticsError').hidden=true;
    try{const data=await request('/api/logistics');if(version!==snapshotVersion)return;snapshot=data;
      if(!initialized){let saved=null;try{saved=localStorage.getItem(storageKey);}catch{}selected=new URLSearchParams(location.search).get('project')||(saved===null?(data.projects[0]?.id||''):saved);initialized=true;}
      if(selected&&!snapshot.projects.some(p=>p.id===selected)){selected='';}showTab(new URLSearchParams(location.search).get('tab')||activeTab);await loadInbox();
    }catch(e){if(version===snapshotVersion){error(e);$('transportStatus').textContent='项目状态读取失败，当前接收与任务状态未知。';}}
    finally{if(version===snapshotVersion)$('refreshLogistics').disabled=false;}
  }
  $('projectForm').onsubmit=async event=>{event.preventDefault();const form=event.currentTarget,button=form.querySelector('button');button.disabled=true;try{const project=await request('/api/logistics/projects',{name:form.elements.namedItem('name').value.trim(),site_name:form.elements.namedItem('site_name').value.trim()});snapshot.projects.push(project);form.reset();form.hidden=true;$('showProjectForm').setAttribute('aria-expanded','false');selectProject(project.id);notice('项目已创建；还未启用消息接收或定时询问。');}catch(e){error(e);}finally{button.disabled=false;}};
  $('groupForm').onsubmit=async event=>{event.preventDefault();const form=event.currentTarget,project=selected,button=form.querySelector('button[type=submit]');const body={project_id:project};for(const name of ['name','category','account_id','chat_id'])body[name]=form.elements.namedItem(name).value.trim();if(!!body.account_id!==!!body.chat_id){$('manualBinding').open=true;error(Error('账号标识和群会话标识需要一起填写，也可以都留空。'));return;}if(form.dataset.editing)body.id=form.dataset.editing;button.disabled=true;try{await request('/api/logistics/groups',body);if(project===selected){resetGroupForm();await loadInbox();}notice('群信息已保存；是否收到消息请查看接收状态。');}catch(e){error(e);}finally{button.disabled=false;}};
  $('showProjectForm').onclick=()=>{$('projectForm').hidden=!$('projectForm').hidden;$('showProjectForm').setAttribute('aria-expanded',String(!$('projectForm').hidden));if(!$('projectForm').hidden)$('projectForm').elements.namedItem('name').focus();};
  $('showGroupForm').onclick=()=>{resetGroupForm();$('groupForm').hidden=false;$('showGroupForm').setAttribute('aria-expanded','true');$('cancelGroupEdit').hidden=false;$('groupForm').elements.namedItem('name').focus();};
  const cancelEdit=node('button','取消编辑');cancelEdit.id='cancelGroupEdit';cancelEdit.type='button';cancelEdit.hidden=true;cancelEdit.onclick=resetGroupForm;$('groupForm').append(cancelEdit);
  $('unassignedInbox').onclick=()=>{selectProject('');showTab('inbox');};$('refreshLogistics').onclick=loadLogistics;$('messageFilter').onchange=renderMessages;$('groupFilter').onchange=renderMessages;
  $('todoFilter').onchange=renderTodos;$('todoCancel').onclick=resetTodoForm;$('todoKind').onchange=syncTodoKind;
  $('temporaryTasks').onclick=()=>selectTodoKind('temporary');$('scheduledTasks').onclick=()=>selectTodoKind('scheduled');
  $('todoForm').onsubmit=async event=>{event.preventDefault();const project=selected,form=event.currentTarget,body={...(editingTodo||{}),project_id:project};for(const name of ['title','note','due_date','kind'])body[name]=form.elements.namedItem(name).value;
    if(body.kind==='scheduled'){body.due_date='';body.schedule={times:form.elements.namedItem('times').value.trim().split(/[,，、\s]+/).map(t=>t.replace('：',':')),start_on:form.elements.namedItem('start_on').value,end_on:form.elements.namedItem('end_on').value,enabled:form.elements.namedItem('enabled').checked,group_ids:[...form.querySelectorAll('[name=photo_group]:checked')].map(c=>c.value)};}
    const button=$('todoSave');button.disabled=true;try{await request('/api/logistics/todos',body);if(project===selected){selectTodoKind(body.kind);await loadTodos();notice('待办已保存。');}}catch(e){if(project===selected)error(e);}finally{button.disabled=false;}};
  for(const button of document.querySelectorAll('[data-support-tab]'))button.onclick=()=>showTab(button.dataset.supportTab);
  for(const button of document.querySelectorAll('[data-tab]'))button.onclick=()=>showTab(button.dataset.tab);
  options($('newGroupCategory'),groupCategories);resetTodoForm();loadLogistics();
})();
