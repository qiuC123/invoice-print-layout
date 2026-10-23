// Actual project menu with intercepted synthetic HTTP; no live projects or files.
const assert=require('node:assert/strict');
const fs=require('node:fs/promises');
const path=require('node:path');
async function run(pw){
 const browser=await pw.chromium.launch({headless:true}),page=await browser.newPage({viewport:{width:1500,height:1000}});
 page.setDefaultTimeout(10000);
 const origin='http://127.0.0.1:18816',errors=[],writes=[],native=[];
 const projects=[{id:'p2',name:'合成删除项目',project_revision:2,pinned:false,reimbursement_status:'unreimbursed',reimbursement_revision:0},
  {id:'p1',name:'合成当前项目',project_revision:0,pinned:false,reimbursement_status:'unreimbursed',reimbursement_revision:0},
  {id:'p3',name:'合成历史项目',project_revision:1,pinned:false,reimbursement_status:'reimbursed',reimbursement_revision:4}];
 const item=p=>({id:p.id+'-fee',project_id:p.id,project:p.name,title:p.id+'费用',stage:p.id==='p3'?'submitted':'draft',complete:p.id==='p3',ready:p.id==='p3',verified:p.id==='p3',category:'酒店',amount_cents:1200,amount:'12.00',missing:['发票'],attachments:[],events:[],version:'v1',expense_date:'',merchant:'',note:'',waiting_days:0});
 const items=projects.map(item),state={app_mode:'invoice',projects,items,inbox:[],categories:['酒店'],roles:{invoice:'发票'},stages:{draft:'待核对',submitted:'已提交'},token:'synthetic',report_template_ready:true};
 let failure=null,hold=false,release;
 page.on('pageerror',e=>errors.push(e.message));page.on('dialog',d=>{native.push(d.type());d.dismiss();});
 await page.route(origin+'/**',async route=>{
  const req=route.request(),url=new URL(req.url()),json=(x,status=200)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(x)});
  if(url.pathname==='/api/expense-print-preview')return json({complete:false,preview_pages:[],missing:['发票']});
  if(req.method()==='POST'){
   const body=req.postDataJSON();writes.push({route:url.pathname,body});assert.equal(req.headers()['x-workbench-token'],state.token);
   const project=projects.find(p=>p.id===body.id);assert.ok(project);
   const reimbursement=url.pathname.endsWith('/reimbursement');assert.equal(body.expected_revision,reimbursement?project.reimbursement_revision:project.project_revision);
   if(hold){hold=false;await new Promise(resolve=>{release=resolve;});}
   if(failure){const current=failure;failure=null;if(current==='conflict'){project.name='他处更名后的项目';project.project_revision++;return json({error:'项目版本冲突，请刷新'},409);}if(current==='malformed')return json({deleted:false,id:'wrong-id'});return json({error:'合成服务暂时失败'},503);}
   if(url.pathname.endsWith('/rename')){project.name=body.name;for(const x of items)if(x.project_id===project.id)x.project=body.name;}
   else if(url.pathname.endsWith('/pin'))project.pinned=body.pinned;
   else if(reimbursement){project.reimbursement_status=body.status;project.reimbursement_revision++;}
   else if(url.pathname.endsWith('/delete-permanently')){
    assert.equal(body.confirmed,true);assert.equal(body.confirmation_name,project.name);
    projects.splice(projects.indexOf(project),1);for(let i=items.length-1;i>=0;i--)if(items[i].project_id===project.id)items.splice(i,1);
    return json({deleted:true,id:project.id,name:project.name,counts:{expenses:1},message:'原件保留'});
   }else assert.fail('unexpected write '+url.pathname);
   project.project_revision++;return json(project);
  }
  if(url.pathname==='/api/state')return json(state);
  if(url.pathname==='/api/expense-print-preview')return json({complete:false,preview_pages:[],missing:['发票']});
  if(url.pathname.startsWith('/api/'))return json({candidates:[],suggestions:[],vouchers:[],payments:[],other:[],matches:[],report_types:[]});
  const file=url.pathname==='/'?'index.html':url.pathname.slice(1);if(!/^[a-z-]+\.(html|js|css)$/.test(file))return route.abort();
  return route.fulfill({contentType:file.endsWith('.js')?'text/javascript':file.endsWith('.css')?'text/css':'text/html',body:await fs.readFile(path.resolve(__dirname,'../src/invoice_print_layout/web',file))});
 });
 const menu=()=>page.locator('#projectMenu'),dialog=()=>page.locator('#projectActionDialog');
 const link=id=>page.locator(`#projectLinks [data-project-menu-id="${id}"]`);
 const open=async id=>{await link(id).click({button:'right'});await menu().waitFor({state:'visible'});};
 const action=async(id,label)=>{await open(id);await menu().getByRole('menuitem',{name:label,exact:true}).click();};
 const idle=()=>page.waitForFunction(()=>document.getElementById('projectMenu').getAttribute('aria-busy')!=='true');
 const counts=async(a,b)=>{assert.equal(await page.locator('[data-project-status=unreimbursed]').innerText(),'未报销 '+a);assert.equal(await page.locator('[data-project-status=reimbursed]').innerText(),'已报销 '+b);};
 try{
  await page.goto(origin+'/?project=p1');await page.locator('[data-item="p1-fee"]').waitFor();await counts(2,1);
  assert.equal(await page.locator('#projectActions').isVisible(),false,'standalone desktop keeps the sidebar right-click entry');
  assert.equal(await page.locator('#projectReimbursementStatus,#requestReviewSelected,[data-view=all],[data-view=missing]').count(),0);
  await link('p1').focus();await page.keyboard.press('Shift+F10');await menu().waitFor({state:'visible'});
  assert.deepEqual(await menu().getByRole('menuitem').allTextContents(),['重命名','置顶','永久删除','移到已报销']);
  await page.keyboard.press('ArrowDown');assert.equal(await page.evaluate(()=>document.activeElement.textContent),'置顶');
  await page.keyboard.press('Escape');assert.equal(await menu().isVisible(),false);assert.equal(await link('p1').evaluate(x=>x===document.activeElement),true);
  await link('p1').dispatchEvent('keydown',{key:'ContextMenu'});await menu().waitFor({state:'visible'});await page.locator('header h1').click();assert.equal(await menu().isVisible(),false);
  await action('p1','重命名');const input=dialog().locator('[name=name]'),save=dialog().getByRole('button',{name:'保存名称',exact:true});
  await input.fill('新项目 <img data-project-injection="yes">');failure='unavailable';await save.click();await dialog().locator('[data-project-error]').filter({hasText:'合成服务暂时失败'}).waitFor();await idle();
  assert.equal(await input.inputValue(),'新项目 <img data-project-injection="yes">');assert.equal(await save.isEnabled(),true);
  const before=writes.length;hold=true;await save.click();await page.waitForFunction(()=>document.getElementById('projectMenu').getAttribute('aria-busy')==='true');
  await dialog().locator('form').evaluate(form=>form.requestSubmit());assert.equal(writes.length,before+1);assert.equal(await save.isDisabled(),true);release();await dialog().waitFor({state:'detached'});await idle();
  assert.ok((await page.locator('h1').innerText()).includes(projects.find(p=>p.id==='p1').name));assert.equal(await page.locator('[data-project-injection]').count(),0);
  await action('p1','置顶');await idle();assert.deepEqual(await page.locator('#projectLinks a').evaluateAll(xs=>xs.map(x=>x.dataset.projectMenuId)),['p1','p2']);
  await page.reload();await link('p1').waitFor();assert.equal(await link('p1').locator('.project-pin').count(),1);
  await action('p1','移到已报销');await idle();await counts(1,2);assert.equal(await page.locator('[data-project-status=reimbursed]').getAttribute('aria-pressed'),'true');
  assert.equal(items.find(x=>x.id==='p1-fee').stage,'draft','project classification never verifies or submits an expense');
  await action('p1','移回未报销');await idle();await counts(2,1);
  await open('p1');await link('p1').dispatchEvent('contextmenu',{clientX:1499,clientY:999});let box=await menu().boundingBox();assert.ok(box.x+box.width<=1500&&box.y+box.height<=1000);
  await page.setViewportSize({width:390,height:844});await page.locator('#projectActions').waitFor({state:'visible'});box=await menu().boundingBox();assert.ok(box.x+box.width<=390&&box.y+box.height<=844);await page.keyboard.press('Escape');assert.equal(await page.locator('#projectActions').evaluate(x=>x===document.activeElement),true);
  await page.locator('#projectActions').click();await menu().waitFor({state:'visible'});assert.equal(await menu().getByRole('menuitem').count(),4);await page.keyboard.press('Escape');await page.setViewportSize({width:1500,height:1000});
  await page.evaluate(src=>{const frame=document.createElement('iframe');frame.id='syntheticEmbedded';frame.src=src;frame.style.cssText='width:100%;height:600px';document.body.append(frame);},origin+'/?project=p1&embedded=1');
  const embedded=page.frameLocator('#syntheticEmbedded');await embedded.locator('#projectActions').waitFor({state:'visible'});assert.equal(await embedded.locator('.rail').isVisible(),false);
  await embedded.locator('#projectActions').click();await embedded.locator('#projectMenu').getByRole('menuitem',{name:'取消置顶',exact:true}).click();
  await page.frames().find(f=>f.url()===origin+'/?project=p1&embedded=1').waitForFunction(()=>document.getElementById('projectMenu').getAttribute('aria-busy')!=='true');
  assert.equal(projects.find(p=>p.id==='p1').pinned,false);assert.equal(await embedded.locator('#projectActions').evaluate(x=>x===document.activeElement),true);
  await page.locator('#syntheticEmbedded').evaluate(x=>x.remove());await page.evaluate(()=>load());
  await action('p2','永久删除');const oldName=projects.find(p=>p.id==='p2').name;assert.ok((await dialog().innerText()).includes('原始文件、源 PDF 和后勤项目目录仍保留'));
  failure='conflict';const beforeDelete=writes.length;await dialog().getByRole('button',{name:'确认永久删除',exact:true}).click();await dialog().locator('[data-project-error]').filter({hasText:'重新打开'}).waitFor();await idle();
  assert.ok((await dialog().innerText()).includes(oldName));assert.equal(await dialog().getByRole('button',{name:'确认永久删除'}).isDisabled(),true);
  await dialog().locator('form').evaluate(form=>form.requestSubmit());assert.equal(writes.length,beforeDelete+1);assert.ok(page.url().includes('project=p1'));
  await dialog().getByRole('button',{name:'取消',exact:true}).click();await action('p2','永久删除');failure='malformed';const malformedBefore=writes.length;await dialog().getByRole('button',{name:'确认永久删除'}).click();await dialog().locator('[data-project-error]').filter({hasText:'无法确认'}).waitFor();await idle();
  assert.ok(page.url().includes('project=p1'));await dialog().locator('form').evaluate(form=>form.requestSubmit());assert.equal(writes.length,malformedBefore+1);
  await dialog().getByRole('button',{name:'取消',exact:true}).click();await action('p2','永久删除');await dialog().getByRole('button',{name:'确认永久删除'}).click();await dialog().waitFor({state:'detached'});await idle();await counts(1,1);assert.equal(await link('p2').count(),0);
  await action('p1','重命名');await dialog().locator('[name=name]').fill('应保留的输入');failure='conflict';const renameBefore=writes.length;await dialog().getByRole('button',{name:'保存名称'}).click();await dialog().locator('[data-project-error]').filter({hasText:'重新打开'}).waitFor();await idle();
  assert.equal(await dialog().locator('[name=name]').inputValue(),'应保留的输入');assert.equal(await dialog().getByRole('button',{name:'保存名称'}).isDisabled(),true);await dialog().locator('form').evaluate(form=>form.requestSubmit());assert.equal(writes.length,renameBefore+1);
  await dialog().getByRole('button',{name:'取消',exact:true}).click();await action('p1','永久删除');await dialog().getByRole('button',{name:'确认永久删除'}).click();await page.waitForURL(url=>!url.searchParams.has('project'));await page.getByRole('heading',{name:'全部项目台账',exact:true}).waitFor();await counts(0,1);
  assert.deepEqual(projects.map(p=>p.id),['p3']);assert.equal(items[0].stage,'submitted');assert.equal(items[0].amount_cents,1200);
  const invalidBefore=writes.length;await page.goto(origin+'/?project=p1');await page.locator('#error').filter({hasText:'项目不存在'}).waitFor();assert.equal(await page.locator('[data-item]').count(),0);
  const recovery=page.getByRole('link',{name:'返回项目列表',exact:true});assert.equal(await recovery.getAttribute('href'),'/');await recovery.click();await page.waitForURL(url=>!url.searchParams.has('project'));assert.equal(writes.length,invalidBefore,'an old deleted project URL recovers without writes or another project ledger');
  assert.deepEqual(native,[]);assert.deepEqual(errors,[]);
  return {passed:true,scope:'synthetic menu: revisions, names, pin persistence, reimbursement isolation, keyboard/dismissal/viewport, pending duplicate suppression, failed input retention, stale rename/delete refusal, malformed deletion acknowledgement, confirmed projection deletion and root navigation',writes:writes.length};
 }finally{await browser.close();}
}
module.exports=run;
if(require.main===module)run(require('playwright')).then(console.log).catch(e=>{console.error(e);process.exitCode=1;});
