// Mail intake has an independent project rule and continues through the existing question/preview flow.
const assert=require('node:assert/strict'),fs=require('node:fs/promises'),path=require('node:path');
async function run(pw){
 const browser=await pw.chromium.launch({headless:true}),page=await browser.newPage({viewport:{width:1120,height:950}});
 const origin='http://127.0.0.1:18993',errors=[],writes=[];
 let unavailable=false;
 const mail={enabled:false,address:'synthetic@163.com',folder:'INBOX',interval_seconds:120,since:'2026-10-21',
  use_model:true,default_project_id:'',collecting:false,suspended:false,last_checked_at:null,last_error:'',next_check_at:null,
  messages:[{id:'mail-1',subject:'<img src=x onerror="throw 1"> 合成测试材料',sender:'测试发票',status:'unassigned',
   issues:['没有明确的项目名称'],material_count:2,project_id:'',job_id:''},
   {id:'mail-attention',subject:'收件失败的合成材料',sender:'测试发票',status:'attention',issues:['原件未完整保存'],material_count:1,project_id:'p2',job_id:'',route_reason:'date'}]};
 let jobs=[];
 page.on('pageerror',e=>errors.push(e.message));
 await page.route(origin+'/**',async route=>{
  const req=route.request(),url=new URL(req.url()),json=value=>route.fulfill({contentType:'application/json',body:JSON.stringify(value)});
  if(url.pathname==='/api/state')return json({token:'synthetic',projects:[{id:'p1',name:'当前浏览项目'},{id:'p2',name:'邮件实际项目'}]});
  if(url.pathname==='/api/agent/jobs')return json({token:'synthetic',model:{available:true},jobs:url.searchParams.get('project')==='p2'?jobs:[]});
  if(url.pathname==='/api/agent/mail'&&req.method()==='GET'){
   if(unavailable)return route.fulfill({status:404,contentType:'application/json',body:JSON.stringify({error:'收件服务暂不可用'})});
   if(mail.collecting)mail.collecting=false;
   return json(mail);
  }
  if(req.method()==='POST'){
   const body=req.postDataJSON();assert.equal(req.headers()['x-workbench-token'],'synthetic');writes.push({path:url.pathname,body});
   if(url.pathname==='/api/agent/mail/assign'){
    assert.equal(mail.messages[0].status,'unassigned');
    assert.deepEqual(body,{id:'mail-1',project_id:'p2'});Object.assign(mail.messages[0],{project_id:'p2',job_id:'j1',status:'needs_input'});
    jobs=[{id:'j1',project_id:'p2',models:1,tools:3,limits:{models:6,tools:48},events:[],state:'needs_input',message:'确认付款对应关系',
     question:{id:'relation-1',text:'这张付款是否对应鼠标采购？',material_ids:[],choices:[{id:'yes',action:'confirm_relation',amount_cents:1700}]}}];
    return json(mail);
   }
   if(url.pathname==='/api/agent/answer'){
    assert.deepEqual(body,{id:'j1',question_id:'relation-1',choice:'yes'});
    jobs=[{...jobs[0],state:'ready',question:null,message:'报销预览已准备',preview:{total_cents:1700,page_count:2,pdf:'synthetic.pdf',xlsx:'synthetic.xlsx',preview_pages:[]}}];
    mail.messages[0].status='ready';return json({job:jobs[0]});
   }
   if(url.pathname==='/api/agent/mail/configure'){Object.assign(mail,body);return json(mail);}
   if(url.pathname==='/api/agent/mail/collect'){
    assert.equal(mail.enabled,true,'Paused collection must not be callable');assert.deepEqual(body,{});mail.collecting=true;return json(mail);
   }
   throw new Error('Unexpected mutation '+url.pathname);
  }
  const name=url.pathname==='/agent'?'agent.html':url.pathname.slice(1);
  if(!/^[a-z-]+\.(html|js|css)$/.test(name))return route.abort();
  return route.fulfill({contentType:name.endsWith('.js')?'text/javascript':name.endsWith('.css')?'text/css':'text/html',body:await fs.readFile(path.resolve(__dirname,'../src/invoice_print_layout/web',name))});
 });
 try{
  await page.goto(origin+'/agent?project=p1');await page.getByText('synthetic@163.com · INBOX',{exact:true}).waitFor();
  const choose=page.locator('.mail-message select');
  assert.equal(await choose.count(),1,'Only unassigned messages may show project assignment');
  assert.match(await page.locator('[data-mail-id="mail-attention"]').textContent(),/按项目时间分配/);
  assert.equal(await choose.inputValue(),'');assert.equal(await page.getByRole('button',{name:'确定项目，开启收件后继续',exact:true}).isDisabled(),true);
  assert.equal(await page.locator('#mail-collect').isDisabled(),true);
  await page.getByText('自动收件已暂停，确定项目后将等待重新开启。',{exact:true}).waitFor();
  assert.equal(await page.locator('#mail-project').inputValue(),'');assert.equal(await page.locator('.mail-message img').count(),0);
  await page.getByRole('button',{name:'开启自动收件',exact:true}).click();
  await page.getByRole('button',{name:'确定项目并继续',exact:true}).waitFor();
  await choose.selectOption('p2');await page.getByRole('button',{name:'确定项目并继续',exact:true}).click();
  await page.getByRole('button',{name:'查看整理任务',exact:true}).click();
  await page.getByText('这张付款是否对应鼠标采购？',{exact:true}).waitFor();
  assert.equal(await page.locator('#project').inputValue(),'p2');assert.equal(await page.locator('#manual').getAttribute('href'),'/?project=p2');
  assert.equal(await page.locator('#mail-project').inputValue(),'');
  await page.getByRole('button',{name:/同一笔，继续生成预览/}).click();
  await page.getByText('合计 17.00 元 · 2 页 · 尚未核对',{exact:true}).waitFor();
  assert.equal(await page.getByRole('link',{name:'到人工工作台核对'}).getAttribute('href'),origin+'/?project=p2');
  await page.locator('#mail-settings summary').click();await page.locator('#mail-project').selectOption('p1');
  await page.locator('#mail-interval').fill('180');await page.locator('#mail-save').click();
  assert.deepEqual(writes.filter(x=>x.path.endsWith('/configure')).at(-1).body,
   {enabled:true,interval_seconds:180,since:'2026-10-21',use_model:true,default_project_id:'p1'});
  await page.getByRole('button',{name:'立即收件',exact:true}).click();
  await page.getByText('正在收取邮件…',{exact:true}).waitFor();
  assert.match(await page.locator('#mail-status').innerText(),/正在收取邮件/);
  assert.equal(await page.locator('#mail-collect').isDisabled(),true);
  await page.waitForFunction(()=>!document.getElementById('mail-collect').disabled,{},{timeout:8000});
  await page.getByRole('button',{name:'暂停自动收件',exact:true}).click();
  await page.getByRole('button',{name:'开启自动收件',exact:true}).waitFor();
  assert.equal(await page.locator('#mail-collect').isDisabled(),true);
  Object.assign(mail,{address:'another@163.com',folder:'INBOX',default_project_id:'',interval_seconds:90,use_model:false,
   enabled:true,suspended:true,next_check_at:1792570000,last_error:'收件连续失败已暂停，请检查网络或授权后点立即收件',messages:[]});
  await page.getByText('another@163.com · INBOX',{exact:true}).waitFor({timeout:8000});
  assert.equal(await page.locator('#mail-project').inputValue(),'','Changing accounts must clear the prior default project');
  assert.equal(await page.locator('#mail-interval').inputValue(),'90');
  assert.equal(await page.locator('#mail-model').isChecked(),false);
  assert.match(await page.locator('#mail-status').innerText(),/收件重试已暂停/);
  assert.doesNotMatch(await page.locator('#mail-status').innerText(),/下次检查/);
  assert.equal(await page.locator('#mail-collect').isEnabled(),true,'Suspended retries can be resumed explicitly');
  await page.locator('#mail-interval').fill('240');await page.locator('#mail-save').click();
  await page.waitForFunction(()=>document.getElementById('mail-save').disabled===false);
  assert.deepEqual(writes.filter(x=>x.path.endsWith('/configure')).at(-1).body,
   {enabled:true,interval_seconds:240,since:'2026-10-21',use_model:false,default_project_id:''});
  await page.setViewportSize({width:390,height:844});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),true);
  unavailable=true;await page.reload();await page.getByText('暂时无法读取收件状态；上传和整理任务仍可使用',{exact:true}).waitFor();
  await page.getByText('合计 17.00 元 · 2 页 · 尚未核对',{exact:true}).waitFor();
  assert.equal(await page.locator('#error').isVisible(),false);assert.equal(await page.locator('#manual').isVisible(),true);
  assert.deepEqual(errors,[]);
  return {passed:true,mailProjectIndependentOfCurrentView:true,mailAssignmentStartsExistingQuestionFlow:true,
   sourceQuestionIdentitySent:true,confirmationContinuesToPreview:true,explicitDefaultProjectRule:true,
   collectingStateVisible:true,enablePauseControls:true,pausedCollectDisabled:true,onlyUnassignedMayChooseProject:true,
   accountChangeClearsOldDefault:true,suspendedRetriesPresentedAccurately:true,mailFailureDoesNotBlockJobs:true,
   safeMailTextRendering:true,mobileViewport:true,manualEntrancePreserved:true,pageErrors:errors};
 }finally{await browser.close();}
}
module.exports=run;
if(require.main===module)run(require('playwright')).then(r=>console.log(JSON.stringify(r))).catch(e=>{console.error(e);process.exitCode=1;});
