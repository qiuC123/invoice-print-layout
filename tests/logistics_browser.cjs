// Synthetic UI regression: no real WeChat, groups, projects, or sending.
const assert=require('node:assert/strict');
const fs=require('node:fs/promises');
const path=require('node:path');
async function run(pw){
 const browser=await pw.chromium.launch({headless:true});
 const page=await browser.newPage({viewport:{width:1280,height:900}});
 await page.clock.install();
 const base='http://127.0.0.1:18768', errors=[],posts=[];
 let fail=false,holdP1=false,releaseP1=null,failDelete=false;
 const todos=[];
 const projects=[{id:'p1',name:'项目一',site_name:'测试地点甲',status:'draft'},{id:'p2',name:'项目二',status:'draft'}];
 const groups=[{id:'g1',project_id:'p1',name:'一号现场群',category:'site',account_id:'a',chat_id:'c1'},{id:'g2',project_id:'p2',name:'二号订餐群',category:'meals',account_id:'a',chat_id:'c2'}];
 const messages=[{key:'m1',project_id:'p1',chat_id:'c1',sender_id:'synthetic',text:'项目一消息 <script>bad()</script>',timestamp:1,category:'materials',issues:[]},{key:'m2',project_id:'p2',chat_id:'c2',sender_id:'synthetic',text:'项目二消息',timestamp:2,category:'invoices',issues:[]},{key:'m0',project_id:null,chat_id:'unknown',sender_id:'synthetic',text:'待归属资料',timestamp:3,category:'unclassified',issues:[]}];
 for(const message of messages)Object.assign(message,{account_id:'a',type_code:1});
 messages[0].issues=['sender_identity_missing','unexpected_internal_code'];
 const categories={site:'现场沟通',meals:'订餐',lodging:'住宿',invoices:'票据',other:'其他'};
 const messageCategories={meals:'餐饮',lodging:'住宿',invoices:'票据',materials:'进出场资料',other:'其他',unclassified:'待分类'};
 const tasks=projects.map((p,i)=>({id:'t'+i,project_id:p.id,kind:'breakfast',business_day:'2026-09-24',state:'review',quantity:i,revision:2,ask_at:'2026-09-23T20:00:00+08:00',direct_at:'2026-09-23T20:30:00+08:00',deadline:'2026-09-23T21:00:00+08:00'}));
 page.on('pageerror',e=>errors.push(e.message));
 await page.route(base+'/**',async route=>{
  const url=new URL(route.request().url()),json=(data,status=200)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(data)});
  if(route.request().method()==='POST'){
   assert.equal(route.request().headers()['x-workbench-token'],'synthetic-token');const body=route.request().postDataJSON();posts.push({path:url.pathname,body});
   if(['/api/logistics/todos/delete','/api/logistics/todos/restore'].includes(url.pathname)){if(failDelete){failDelete=false;return json({error:'删除失败，请重试'},400);}const item=todos.find(t=>t.id===body.id);assert.equal(item.project_id,body.project_id);assert.equal(item.revision,body.revision);item.deleted_at=url.pathname.endsWith('/delete')?'deleted':'';item.revision++;if(item.kind==='scheduled')item.schedule.enabled=false;return json(item);}
   if(url.pathname==='/api/logistics/todos'){let item=todos.find(t=>t.id===body.id);if(item){assert.equal(item.project_id,body.project_id);Object.assign(item,body,{revision:item.revision+1});}else{item={id:'todo'+(todos.length+1),status:'pending',...body,revision:1};todos.push(item);}return json(item);}
   if(url.pathname==='/api/logistics/projects'){const project={id:'p'+(projects.length+1),name:body.name,site_name:body.site_name,status:'draft'};projects.push(project);return json(project);}
   if(url.pathname==='/api/logistics/groups/category'){Object.assign(groups.find(g=>g.id===body.id),{category:body.category});return json({ok:true});}
   if(url.pathname==='/api/logistics/groups'){let group=groups.find(g=>g.id===body.id);if(group)Object.assign(group,body);else{group={id:'g'+(groups.length+1),...body};groups.push(group);}return json(group);}
   if(url.pathname==='/api/logistics/messages/assign'){Object.assign(messages.find(m=>m.key===body.key),{project_id:body.project_id,category:body.category});return json({ok:true});}
   throw Error('unexpected mutation '+url.pathname);
  }
  if(url.pathname==='/api/logistics')return json({token:'synthetic-token',transport_enabled:false,projects,tasks,outbox:tasks.map((t,i)=>({task_id:t.id,stage:'supplier',state:'uncertain',payload:{text:'项目'+(i+1)+'发送进度'}}))},fail?500:200);
  if(url.pathname==='/api/logistics/todos')return json({items:todos.filter(t=>t.project_id===url.searchParams.get('project_id')&&!t.deleted_at),photo_groups:[{id:'photo-source',name:'照片来源群'}]});
  if(url.pathname==='/api/logistics/inbox'){
   assert.equal(url.searchParams.get('scope'),'support');
   const project=url.searchParams.get('project_id');const result={groups:groups.filter(g=>g.project_id===project),messages:messages.filter(m=>project?m.project_id===project&&['materials','invoices'].includes(m.category):m.project_id===null),receiver:{status:'disconnected',detail:'测试接收器未运行'},group_categories:categories,message_categories:messageCategories};
   if(holdP1&&project==='p1'){holdP1=false;await new Promise(resolve=>{releaseP1=resolve;});}
   return json(result);
  }
  if(url.pathname==='/photos')return route.fulfill({contentType:'text/html',body:'<p>'+url.searchParams.get('project')+' photos</p>'});
  const filename=url.pathname==='/logistics'?'logistics.html':url.pathname.slice(1);
  if(!['logistics.html','logistics.js','logistics.css'].includes(filename))return route.abort();
  return route.fulfill({contentType:filename.endsWith('.js')?'text/javascript':filename.endsWith('.css')?'text/css':'text/html',body:await fs.readFile(path.resolve(__dirname,'../src/invoice_print_layout/web',filename))});
 });
 const inboxText=()=>page.locator('#logisticsInbox').innerText();
 const choose=async id=>{await page.locator(`[data-project-id="${id}"]`).click();await page.waitForFunction(()=>!document.getElementById('logisticsInbox').textContent.includes('正在读取'));};
 try{
  await page.goto(base+'/logistics');await page.locator('[data-message-key="m1"]').waitFor();
  assert.match(await inboxText(),/项目一消息/);assert.doesNotMatch(await inboxText(),/项目二消息|待归属资料/);assert.equal(await page.locator('#logisticsInbox script').count(),0);
  assert.match(await inboxText(),/发件人身份待核验/);assert.match(await inboxText(),/来源需要核验/);assert.doesNotMatch(await inboxText(),/sender_identity_missing|unexpected_internal_code/);
  assert.match(await page.locator('#transportStatus').textContent(),/尚未联通/);
  assert.match(await page.locator('#projectSummary').innerText(),/测试地点甲/);
  assert.equal(await page.locator('.project-subnav').count(),0);
  assert.equal(await page.locator('#projectList button').count(),projects.length);assert.equal(await page.locator('[data-purpose="site"]').count(),0);
  await page.locator('#groupFilter').selectOption(JSON.stringify(['a','c1']));assert.match(await inboxText(),/项目一消息/);assert.match(await page.locator('#inboxHeading').innerText(),/一号现场群/);
  await page.locator('#messageFilter').selectOption('invoices');assert.match(await inboxText(),/这个筛选下暂无消息/);
  await page.locator('#groupFilter').selectOption('');await page.locator('#messageFilter').selectOption('');assert.match(await inboxText(),/项目一消息/);assert.equal(await page.locator('#groupFilter').inputValue(),'');assert.equal(await page.locator('#messageFilter').inputValue(),'');
  await choose('p2');assert.match(await inboxText(),/项目二消息/);assert.doesNotMatch(await inboxText(),/项目一消息/);
  await page.locator('[data-tab="tasks"]').click();
  await page.locator('#todoForm [name="title"]').fill('核对照片 <script>bad()</script>');
  await page.locator('#todoForm [name="note"]').fill('补充工厂装货记录');
  await page.locator('#todoForm [name="due_date"]').fill('2026-09-27');
  await page.locator('#todoSave').click();await page.locator('[data-todo-id="todo1"]').waitFor();
  await page.waitForFunction(()=>!document.getElementById('logisticsNotice').hidden);await page.mouse.move(0,0);await page.clock.fastForward(4000);
  assert.equal(await page.locator('#logisticsTasks script').count(),0);
  assert.match(await page.locator('#logisticsTasks').innerText(),/截止 2026-09-27/);
  await page.locator('[data-todo-id="todo1"] .todo-edit').click();await page.locator('#todoForm [name="title"]').fill('核对现场照片');await page.locator('#todoSave').click();
  await page.waitForFunction(()=>document.querySelector('[data-todo-id="todo1"] strong')?.textContent==='核对现场照片');
  await page.waitForFunction(()=>!document.getElementById('logisticsNotice').hidden);await page.mouse.move(0,0);await page.clock.fastForward(1500);assert.equal(await page.locator('#logisticsNotice').isVisible(),true);await page.clock.fastForward(3501);assert.equal(await page.locator('#logisticsNotice').isHidden(),true);
  await page.locator('[data-todo-id="todo1"] .todo-toggle').click();await page.waitForFunction(()=>document.getElementById('todoCount').textContent.includes('已完成 1'));
  assert.equal(await page.locator('[data-todo-id="todo1"]').count(),0);
  await page.locator('#todoFilter').selectOption('done');await page.locator('[data-todo-id="todo1"] .todo-toggle').click();
  await page.waitForFunction(()=>document.getElementById('todoCount').textContent.includes('未完成 1'));await page.locator('#todoFilter').selectOption('pending');
  await choose('p1');await page.waitForFunction(()=>document.getElementById('logisticsTasks').textContent.includes('暂无符合'));
  assert.doesNotMatch(await page.locator('#logisticsTasks').innerText(),/核对现场照片/);
  await page.locator('#todoForm [name="title"]').fill('不应串到第二项目');await choose('p2');
  await page.locator('[data-todo-id="todo1"]').waitFor();assert.equal(await page.locator('#todoForm [name="title"]').inputValue(),'');
  await page.reload();await page.locator('[data-todo-id="todo1"]').waitFor();assert.match(page.url(),/tab=tasks/);
  await page.setViewportSize({width:320,height:844});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);await page.setViewportSize({width:1280,height:900});

  await page.locator('#scheduledTasks').click();assert.equal(await page.locator('[data-todo-id="todo1"]').count(),0);
  await page.locator('#todoForm [name="title"]').fill('每日整理飞检照片');await page.locator('#todoForm [name="times"]').fill('18：00');
  await page.locator('#todoForm [name="start_on"]').fill('2026-09-27');await page.locator('#todoForm [name="end_on"]').fill('2026-10-05');
  await page.locator('#todoSave').click();await page.locator('[data-todo-id="todo2"]').waitFor();
  assert.match(await page.locator('[data-todo-id="todo2"]').innerText(),/每天 18:00/);
  assert.match(await page.locator('[data-todo-id="todo2"]').innerText(),/照片来源群/);
  assert.deepEqual(todos[1].schedule.group_ids,['photo-source']);assert.equal(todos[1].schedule.enabled,true);
  await page.locator('[data-todo-id="todo2"] .todo-toggle').click();await page.waitForFunction(()=>document.querySelector('[data-todo-id="todo2"]').textContent.includes('已暂停'));
  await page.locator('[data-todo-id="todo2"] .todo-edit').click();assert.equal(await page.locator('#todoForm [name="times"]').inputValue(),'18:00');
  await page.locator('#todoForm [name="times"]').fill('18:00,22:00');await page.locator('#todoSave').click();
  await page.waitForFunction(()=>document.querySelector('[data-todo-id="todo2"]').textContent.includes('18:00、22:00'));
  await page.locator('[data-todo-id="todo2"] .todo-toggle').click();await page.waitForFunction(()=>document.querySelector('[data-todo-id="todo2"]').textContent.includes('已启用'));
  await page.reload();await page.locator('[data-todo-id="todo2"]').waitFor();assert.match(page.url(),/task_kind=scheduled/);
  await page.setViewportSize({width:320,height:844});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);await page.setViewportSize({width:1280,height:900});
  await choose('p1');await page.waitForFunction(()=>document.getElementById('logisticsTasks').textContent.includes('暂无符合'));assert.equal(await page.locator('[data-todo-id="todo2"]').count(),0);
  await choose('p2');await page.locator('[data-todo-id="todo2"]').waitFor();await page.locator('#temporaryTasks').click();await page.locator('[data-todo-id="todo1"]').waitFor();
  assert.equal(await page.locator('.todo-item input[type=checkbox]').count(),0);
  failDelete=true;await page.locator('[data-todo-id="todo1"] .todo-delete').click();await page.waitForSelector('#logisticsError:not([hidden])');assert.equal(await page.locator('[data-todo-id="todo1"]').count(),1);
  await page.locator('[data-todo-id="todo1"] .todo-delete').click();await page.getByRole('button',{name:'撤销删除',exact:true}).waitFor();assert.equal(await page.locator('[data-todo-id="todo1"]').count(),0);
  await page.getByRole('button',{name:'撤销删除',exact:true}).focus();await page.mouse.move(0,0);await page.clock.fastForward(16000);assert.equal(await page.locator('#logisticsNotice').isVisible(),true);
  await page.locator('#scheduledTasks').focus();await page.clock.fastForward(14999);assert.equal(await page.locator('#logisticsNotice').isVisible(),true);await page.clock.fastForward(2);assert.equal(await page.locator('#logisticsNotice').isHidden(),true);
  await page.reload();await page.waitForFunction(()=>document.getElementById('logisticsTasks').textContent.includes('暂无符合'));assert.equal(await page.locator('[data-todo-id="todo1"]').count(),0);
  await page.locator('#scheduledTasks').click();await page.locator('[data-todo-id="todo2"] .todo-delete').click();await page.getByRole('button',{name:'撤销删除',exact:true}).waitFor();assert.equal(await page.locator('[data-todo-id="todo2"]').count(),0);assert.equal(todos[1].schedule.enabled,false);
  await page.getByRole('button',{name:'撤销删除',exact:true}).click();await page.locator('[data-todo-id="todo2"]').waitFor();assert.match(await page.locator('[data-todo-id="todo2"]').innerText(),/已暂停/);
  await page.waitForFunction(()=>document.getElementById('logisticsNotice').textContent.includes('任务已恢复'));await page.getByRole('button',{name:'关闭提示',exact:true}).click();assert.equal(await page.locator('#logisticsNotice').isHidden(),true);
  await page.locator('[data-tab="groups"]').click();await page.locator('[data-group-id="g2"] select').selectOption('lodging');await page.waitForFunction(()=>document.querySelector('[data-group-id="g2"]')?.closest('section').querySelector('h3').textContent==='住宿');
  assert.equal(await page.locator('[data-purpose="meals"]').count(),0);
  assert.equal(await page.locator('[data-purpose="lodging"]').count(),0);await page.locator('[data-tab="inbox"]').click();await page.locator('#groupFilter').selectOption(JSON.stringify(['a','c2']));assert.match(await inboxText(),/项目二消息/);assert.match(await page.locator('#inboxHeading').innerText(),/二号订餐群/);
  await page.locator('[data-tab="groups"]').click();assert.equal(await page.locator('#groupForm').isHidden(),true);
  await page.locator('#showGroupForm').click();await page.locator('#cancelGroupEdit').click();assert.equal(await page.locator('#groupForm').isHidden(),true);await page.locator('#showGroupForm').click();
  await page.locator('#groupForm [name="name"]').fill('未绑定的测试群');await page.locator('#groupForm button[type="submit"]').click();await page.locator('[data-group-id="g3"]').waitFor();assert.equal(groups[2].account_id,'');assert.equal(groups[2].chat_id,'');
  await page.waitForFunction(()=>!document.getElementById('logisticsNotice').hidden);await page.locator('[data-tab="inbox"]').click();assert.equal(await page.locator('#logisticsNotice').isHidden(),true);await page.locator('[data-tab="groups"]').click();
  await page.locator('[data-group-id="g3"] button').click();await page.locator('#groupForm [name="account_id"]').fill('synthetic-account');await page.locator('#groupForm [name="chat_id"]').fill('synthetic-chat');await page.locator('#groupForm button[type="submit"]').click();await page.waitForFunction(()=>document.querySelector('[data-group-id="g3"]').textContent.includes('已保存会话标识'));
  groups.push({id:'g-other-account',project_id:'p1',name:'另一个账号的现场群',category:'site',account_id:'b',chat_id:'c1'});
  const mediaCases=[[3,'图片'],[34,'语音'],[49,'文件或引用'],[43,'视频'],[10000,'系统消息']];
  for(const [code] of mediaCases)messages.push({key:'media-'+code,project_id:'p1',account_id:'b',chat_id:'c1',sender_id:'synthetic-other-account',text:code===3?'[图片]':'',type_code:code,timestamp:4,category:'materials',issues:[]});
  await choose('p1');await page.locator('[data-tab="inbox"]').click();assert.match(await page.locator('[data-message-key="media-3"] .message-meta').innerText(),/另一个账号的现场群/);assert.match(await page.locator('[data-message-key="m1"] .message-meta').innerText(),/一号现场群/);
  await page.locator('#groupFilter').selectOption(JSON.stringify(['a','c1']));assert.equal(await page.locator('[data-message-key="m1"]').count(),1);assert.equal(await page.locator('[data-message-key="media-3"]').count(),0);
  await page.locator('#groupFilter').selectOption(JSON.stringify(['b','c1']));assert.equal(await page.locator('[data-message-key="m1"]').count(),0);
  assert.equal(await page.locator('#projectList button').count(),projects.length);
  for(const [code,label] of mediaCases){const message=await page.locator(`[data-message-key="media-${code}"]`).innerText();assert.match(message,new RegExp(label+'内容待核对'));assert.doesNotMatch(message,/已识别|OCR/);}
  messages.splice(3);groups.pop();messages[0].issues=[];
  await page.locator('#unassignedInbox').click();await page.locator('[data-message-key="m0"]').waitFor();assert.doesNotMatch(await inboxText(),/项目一消息|项目二消息/);
  await page.locator('[data-message-key="m0"] summary').click();await page.locator('[data-message-key="m0"] [name="project_id"]').selectOption('p1');await page.locator('[data-message-key="m0"] [name="category"]').selectOption('materials');await page.locator('[data-message-key="m0"] button').click();await page.waitForFunction(()=>document.getElementById('logisticsInbox').textContent.includes('暂无待归属消息'));
  await choose('p1');await page.locator('[data-message-key="m0"]').waitFor();assert.match(await inboxText(),/进出场资料/);
  await page.reload();await page.locator('[data-message-key="m1"]').waitFor();assert.equal(await page.locator('#projectTitle').innerText(),'项目一');
  await choose('p2');holdP1=true;await page.locator('[data-project-id="p1"]').click();while(!releaseP1)await new Promise(resolve=>setTimeout(resolve,10));await choose('p2');releaseP1();await page.waitForTimeout(50);assert.equal(await page.locator('#projectTitle').innerText(),'项目二');assert.doesNotMatch(await inboxText(),/项目一消息/);
  await page.locator('#showProjectForm').click();await page.locator('#projectForm [name="name"]').fill('第三个测试项目');await page.locator('#projectForm [name="site_name"]').fill('测试地点丙');await page.locator('#projectForm button').click();await page.waitForFunction(()=>document.getElementById('projectTitle').textContent==='第三个测试项目');await page.waitForFunction(()=>document.getElementById('logisticsInbox').textContent.includes('暂无照片或报销辅助消息'));assert.match(await page.locator('#projectSummary').innerText(),/测试地点丙/);
  const screenshots=path.resolve(__dirname,'../workspace/ui-checks');await fs.mkdir(screenshots,{recursive:true});
  Object.assign(messages[0],{text:'今晚住宿21人，双床房按4人安排，请核对。',timestamp:'2026-09-23 22:45',direction:'inbound'});Object.assign(messages[2],{text:'明天进场材料清单，待核对附件。',timestamp:'2026-09-23 22:40',direction:'inbound'});
  await choose('p1');await page.locator('[data-tab="inbox"]').click();await page.screenshot({path:path.join(screenshots,'logistics-projects-desktop.png'),fullPage:true});
  await page.emulateMedia({colorScheme:'dark'});assert.notEqual(await page.locator('body').evaluate(el=>getComputedStyle(el).color),'rgb(38, 60, 50)');await page.screenshot({path:path.join(screenshots,'logistics-projects-dark.png'),fullPage:true});await page.emulateMedia({colorScheme:'light'});
  await page.setViewportSize({width:320,height:844});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
  await page.setViewportSize({width:390,height:844});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);await page.screenshot({path:path.join(screenshots,'logistics-projects-mobile.png'),fullPage:true});await page.locator('[data-tab="groups"]').click();assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
  await choose('p1');await page.locator('[data-tab="photos"]').click();await page.frameLocator('#photosFrame').getByText('p1 photos',{exact:true}).waitFor();
  await choose('p2');await page.frameLocator('#photosFrame').getByText('p2 photos',{exact:true}).waitFor();assert.equal(await page.locator('[data-tab="photos"]').getAttribute('aria-pressed'),'true');
  await page.reload();await page.frameLocator('#photosFrame').getByText('p2 photos',{exact:true}).waitFor();assert.match(page.url(),/tab=photos/);
  await page.locator('[data-tab="inbox"]').click();
  fail=true;await page.locator('#refreshLogistics').click();await page.waitForSelector('#logisticsError:not([hidden])');assert.equal(await page.locator('#refreshLogistics').isDisabled(),false);
  fail=false;projects.splice(0);groups.splice(0);messages.splice(0);tasks.splice(0);await page.locator('#refreshLogistics').click();await page.waitForFunction(()=>document.getElementById('projectList').textContent.includes('还没有项目'));await page.locator('#showProjectForm').click();await page.locator('#projectForm [name="name"]').fill('空工作台新项目');await page.locator('#projectForm button').click();await page.waitForFunction(()=>document.getElementById('projectTitle').textContent==='空工作台新项目');await page.reload();await page.waitForFunction(()=>document.getElementById('projectTitle').textContent==='空工作台新项目');
  assert.deepEqual(errors,[]);assert.equal(posts.some(p=>p.path.includes('send')),false);
  return 'passed: project isolation, group categories, name-only group and binding, unassigned assignment, persistent selection, stale-response rejection, draft project creation, safe text, temporary todo and daily schedule creation/edit/pause/enable/persistence/project isolation, notice expiry/replacement/close/tab dismissal, undo timeout and keyboard focus pause, account-aware group filtering, media labels, readable issue labels, mobile, fetch failure';
 }finally{if(releaseP1)releaseP1();await browser.close();}
}
module.exports=run;
if(require.main===module)run(require('playwright')).then(console.log).catch(e=>{console.error(e);process.exitCode=1;});
