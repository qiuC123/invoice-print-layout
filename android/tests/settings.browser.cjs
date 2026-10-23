// Exercises shipped Android assets with a synthetic Native bridge; no device data.
const assert=require('node:assert/strict');
const fs=require('node:fs/promises'),path=require('node:path');
async function run(pw){
 const browser=await pw.chromium.launch({headless:true});
 const context=await browser.newContext({viewport:{width:393,height:852}});
 const source=path.resolve(__dirname,'../app/src/main/assets'),errors=[],cases=[];
 const fixture={projects:[{id:'p1',name:'设置测试项目',deleted:false,reimbursed:false}],people:[{id:'person1',name:'测试人员',active:true}],materials:[],material_archive:[],expenses:[{id:'e1',state:'verified',deleted:false,cents:2500,title:'合成费用',owner:'测试人员',owner_id:'person1',date:'2026-10-17',category:'其他',materials:[],allocations:[]}],reports:[],shared_count:0,custom_template:false,settings:{experience:[]},report_types:[{code:'other',label:'其他',items:[]}],services:{glm:{enabled:false,configured:false,images:false},jev:{enabled:false,configured:false},mail:{enabled:false,configured:false},mail_account:''}};
 await context.addInitScript(data=>{
  window.__fixture=data;window.__nativeCalls=[];localStorage.setItem('project','p1');
  window.Native={invoke(raw){const request=JSON.parse(raw);window.__nativeCalls.push(request);setTimeout(()=>{
   let value=true,error=null;
   if(request.method==='state')value=structuredClone(window.__fixture);
   else if(request.method==='projectEdit')Object.assign(window.__fixture.projects.find(p=>p.id===request.args.id),request.args);
   else if(request.method==='save'){
    const a=request.args,e=window.__fixture.expenses.find(e=>e.id===a.id);
    Object.assign(e,a,{cents:Math.round(Number(a.amount)*100),state:'review'});value={id:e.id};
   }
   else if(request.method==='person'){
    const a=request.args;
    if(a.id)Object.assign(window.__fixture.people.find(p=>p.id===a.id),a);
    else if(!a.name.trim())error='名称不能为空，最多120字';
    else window.__fixture.people.push({id:'person2',name:a.name,active:true});
   }
   else if(request.method==='mergePeople')window.__fixture.people=window.__fixture.people.filter(p=>p.id!==request.args.from);
   else if(request.method==='experience'){
    const a=request.args,old=window.__fixture.settings.experience.find(e=>e.key===a.key);
    if(old)Object.assign(old,a);else window.__fixture.settings.experience.push(a);
   }
   else if(['template','backup','restoreSelect'].includes(request.method))value=false;
   else if(request.method==='resetTemplate')window.__fixture.custom_template=false;
   else if(request.method==='mail')error='请先配置邮箱';
   else if(request.method==='claim')error='领票链接无效';
   else if(request.method==='configure')error='合成配置错误';
   else throw Error('Unexpected Native mutation: '+request.method);
   window.receiveNative({id:request.id,value,error});
  },0);}};
 },fixture);
 await context.route('http://invoice-android.test/**',async route=>{
  const asset=new URL(route.request().url()).pathname.slice(1)||'index.html';
  assert.ok(['index.html','style.css','app.js','workbench.js'].includes(asset));
  const type=asset.endsWith('.css')?'text/css':asset.endsWith('.js')?'text/javascript':'text/html';
  await route.fulfill({contentType:type,body:await fs.readFile(path.join(source,asset))});
 });
 const page=await context.newPage();page.on('pageerror',e=>errors.push(e.message));
 const settings=async()=>{await page.locator('nav [data-tab=settings]').click();assert.equal(await page.locator('#settingsPage').isVisible(),true);assert.equal(await page.locator('#workbenchMain').isVisible(),false);assert.equal(await page.locator('#actions').isVisible(),false);};
 const isSettings=async()=>{assert.equal(await page.locator('#dialog').isVisible(),false);assert.equal(await page.locator('#settingsPage').isVisible(),true);assert.equal(await page.locator('nav button.active').getAttribute('data-tab'),'settings');};
 const open=async(key,title)=>{await page.locator(`[data-setting=${key}]`).click();assert.equal(await page.locator('#dialogTitle').innerText(),title);assert.equal(await page.locator('#dialog').isVisible(),true);};
 try{
  await page.goto('http://invoice-android.test/');await page.locator('#project option[value=p1]').waitFor({state:'attached'});
  await page.locator('nav [data-tab=reports]').click();await settings();
  assert.equal(await page.locator('[data-setting]').count(),8);assert.equal(await page.locator('#manage').count(),0);
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);cases.push('full-page grouped settings with fixed navigation');
  for(const [key,title]of[['projects','项目管理'],['people','人员管理'],['template','公司报销模板'],['backup','备份与恢复'],['services','可选联网服务'],['mail','163 邮箱收件'],['claim','二维码领票'],['experience','分类经验']]){
   await open(key,title);await page.locator('#closeDialog').click();await isSettings();
  }
  cases.push('all eight child functions close back to Settings');
  await open('projects','项目管理');await page.locator('#cancelDialog').click();await isSettings();
  await open('template','公司报销模板');await page.locator('#confirmDialog').click();await page.waitForFunction(()=>!document.getElementById('dialog').open);await isSettings();
  await open('backup','备份与恢复');await page.keyboard.press('Escape');await isSettings();cases.push('cancel, close confirmation and Escape preserve Settings');
  assert.ok((await page.evaluate(()=>window.__nativeCalls)).every(r=>r.method==='state'));
  await open('projects','项目管理');await page.locator('[data-project-name=p1]').click();assert.equal(await page.locator('#dialogTitle').innerText(),'修改项目名称');await page.locator('#cancelDialog').click();await isSettings();
  await open('projects','项目管理');await page.locator('[data-project-name=p1]').click();await page.locator('input[name=name]').fill('更新后的测试项目');await page.locator('#confirmDialog').click();await page.waitForFunction(()=>document.getElementById('settingsProject').textContent.includes('更新后的测试项目'));await isSettings();cases.push('nested project editor cancel and save return to Settings');
  await open('services','可选联网服务');await page.locator('#confirmDialog').click();await page.getByRole('alert').filter({hasText:'合成配置错误'}).waitFor();assert.equal(await page.locator('#dialog').isVisible(),true);assert.equal(await page.evaluate(()=>window.handleBack()),true);await isSettings();cases.push('failed child save stays open; Android Back returns to Settings');
  assert.equal(await page.evaluate(()=>window.handleBack()),true);assert.equal(await page.locator('#settingsPage').isVisible(),false);assert.equal(await page.locator('nav button.active').getAttribute('data-tab'),'reports');assert.equal(await page.evaluate(()=>window.handleBack()),false);cases.push('second Android Back restores originating business tab');
  await page.locator('nav [data-tab=verified]').click();await page.locator('[data-expense-select=e1]').check();await settings();await page.locator('#settingsBack').click();assert.equal(await page.locator('[data-expense-select=e1]').isChecked(),true);assert.equal(await page.locator('nav button.active').getAttribute('data-tab'),'verified');cases.push('page Back restores stage and selected expenses');
  await settings();await page.locator('#settingsTheme').selectOption('dark');assert.equal(await page.locator('html').getAttribute('data-theme'),'dark');assert.equal(await page.locator('.settings-row').first().evaluate(e=>getComputedStyle(e).backgroundColor),'rgb(32, 32, 32)');await page.reload();await settings();assert.equal(await page.locator('#settingsTheme').inputValue(),'dark');await page.locator('#settingsTheme').selectOption('light');assert.equal(await page.locator('.settings-row').first().evaluate(e=>getComputedStyle(e).backgroundColor),'rgb(255, 255, 255)');cases.push('desktop light and dark colors persist after reload');
  assert.deepEqual(await page.evaluate(()=>window.__nativeCalls.filter(r=>r.method!=='state')),[]);
  await open('people','人员管理');await page.getByRole('textbox',{name:'新增人员姓名',exact:true}).click();await page.keyboard.insertText('回归人员');assert.equal(await page.getByRole('textbox',{name:'新增人员姓名',exact:true}).inputValue(),'回归人员');await page.locator('#confirmDialog').click();await page.waitForFunction(()=>!document.getElementById('dialog').open);
  await open('people','人员管理');assert.equal(await page.locator('#dialogBody h3').filter({hasText:'回归人员'}).count(),1);await page.locator('[data-person-name=person2]').click();await page.getByRole('textbox',{name:'姓名',exact:true}).fill('改名人员');await page.locator('#confirmDialog').click();await page.waitForFunction(()=>!document.getElementById('dialog').open);await open('people','人员管理');await page.locator('[data-person-active=person2]').click();await page.waitForFunction(()=>!document.getElementById('dialog').open);await open('people','人员管理');assert.equal(await page.locator('[data-person-active=person2]').innerText(),'启用');await page.locator('[data-person-active=person2]').click();await page.waitForFunction(()=>!document.getElementById('dialog').open);await open('people','人员管理');await page.getByText('合并重复人员',{exact:true}).click();await page.locator('select[name=from]').selectOption('person2');await page.locator('select[name=to]').selectOption('person1');await page.locator('[data-action=merge]').click();await page.waitForFunction(()=>!document.getElementById('dialog').open);await open('people','人员管理');assert.equal(await page.locator('[data-person-name=person2]').count(),0);await page.locator('#closeDialog').click();cases.push('Chinese person add, rename, disable, enable and merge');
  await open('people','人员管理');await page.locator('#confirmDialog').click();await page.getByRole('alert').filter({hasText:'名称不能为空'}).waitFor();assert.equal(await page.locator('#dialog').isVisible(),true);await page.locator('#cancelDialog').click();await open('people','人员管理');assert.equal(await page.getByRole('alert').isVisible(),false);await page.locator('#cancelDialog').click();cases.push('empty person name shows an accessible inline error and reopening clears it');
  await open('projects','项目管理');await page.locator('[data-project-pin=p1]').click();await page.waitForFunction(()=>!document.getElementById('dialog').open);await open('projects','项目管理');assert.equal(await page.locator('[data-project-pin=p1]').innerText(),'取消置顶');await page.locator('[data-project-delete=p1]').click();await page.waitForFunction(()=>!document.getElementById('dialog').open);await open('projects','项目管理');assert.equal(await page.locator('[data-project-delete=p1]').innerText(),'恢复');await page.locator('[data-project-delete=p1]').click();await page.waitForFunction(()=>!document.getElementById('dialog').open);cases.push('project pin, recycle and restore keep Settings reachable');
  for(const [key,title,action]of[['template','公司报销模板','import'],['backup','备份与恢复','backup'],['backup','备份与恢复','restore']]){await open(key,title);const count=await page.evaluate(()=>window.__nativeCalls.length);await page.locator(`[data-action=${action}]`).click();await page.waitForFunction(n=>window.__nativeCalls.length>n&&!document.body.hasAttribute('aria-busy'),count);assert.equal(await page.locator('#dialog').isVisible(),true);await page.locator('#cancelDialog').click();}cases.push('cancelled template, backup and restore file pickers retain the child form');
  await open('services','可选联网服务');await page.setViewportSize({width:393,height:300});await page.getByRole('textbox',{name:'API Key／邮箱授权码',exact:true}).fill('FAKE-TEST-ONLY');await page.getByRole('textbox',{name:'163 邮箱账号',exact:true}).fill('test@example.invalid');const geometry=await page.evaluate(()=>{const dialog=document.getElementById('dialog').getBoundingClientRect(),save=document.getElementById('confirmDialog').getBoundingClientRect(),body=document.getElementById('dialogBody');return {inside:save.top>=dialog.top&&save.bottom<=dialog.bottom&&save.bottom<=innerHeight,scrollable:body.clientHeight>0&&body.scrollHeight>body.clientHeight};});assert.ok(geometry.inside&&geometry.scrollable);await page.locator('#confirmDialog').click();await page.getByRole('alert').filter({hasText:'合成配置错误'}).waitFor();assert.equal(await page.getByRole('textbox',{name:'API Key／邮箱授权码',exact:true}).inputValue(),'FAKE-TEST-ONLY');await page.locator('#cancelDialog').click();await page.setViewportSize({width:393,height:852});cases.push('compact keyboard viewport keeps Save visible and errors preserve typed values');
  await open('experience','分类经验');await page.getByRole('textbox',{name:'明确商品关键词',exact:true}).fill('合成螺丝');await page.locator('#confirmDialog').click();await page.waitForFunction(()=>!document.getElementById('dialog').open);await open('experience','分类经验');await page.locator('[data-experience="合成螺丝"]').click();await page.waitForFunction(()=>!document.getElementById('dialog').open);await open('experience','分类经验');assert.match(await page.locator('#dialogBody').innerText(),/合成螺丝.*停用/);await page.locator('#closeDialog').click();cases.push('Chinese classification experience saves and toggles');
  for(const [key,title,error]of[['mail','163 邮箱收件','请先配置邮箱'],['claim','二维码领票','领票链接无效']]){await open(key,title);if(key==='claim')await page.getByRole('textbox',{name:'正式领票链接',exact:true}).fill('https://example.invalid/qa');await page.locator('#confirmDialog').click();await page.getByRole('alert').filter({hasText:error}).waitFor();assert.equal(await page.locator('#dialog').isVisible(),true);await page.locator('#cancelDialog').click();}cases.push('disabled mail and invalid invoice link fail without losing the Settings route');
  await page.evaluate(()=>{window.__fixture.expenses[0].state='review';});await page.evaluate(()=>window.refresh());await page.locator('nav [data-tab=review]').click();await page.locator('[data-edit-v2=e1]').click();await page.setViewportSize({width:393,height:300});await page.getByRole('textbox',{name:'费用名称',exact:true}).fill('中文费用录入');await page.locator('#dialog input[name=amount]').fill('26.50');const saveRect=await page.locator('#confirmDialog').boundingBox();assert.ok(saveRect.y>=0&&saveRect.y+saveRect.height<=300);await page.locator('#confirmDialog').click();await page.waitForFunction(()=>!document.getElementById('dialog').open);await page.setViewportSize({width:393,height:852});assert.equal(await page.locator('#content h3').filter({hasText:'中文费用录入'}).count(),1);assert.equal(await page.evaluate(()=>window.__nativeCalls.filter(r=>r.method==='save').at(-1).args.amount),'26.50');cases.push('expense edit accepts Chinese text and amount with keyboard-sized viewport');
  assert.deepEqual(errors,[]);return {passed:true,cases,child_entries:8,page_errors:errors};
 }finally{await context.close();await browser.close();}
}
module.exports=run;
if(require.main===module)run(require('playwright')).then(r=>console.log(JSON.stringify(r,null,2))).catch(e=>{console.error(e);process.exitCode=1;});
