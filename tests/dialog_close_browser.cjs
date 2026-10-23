// Actual pointer/keyboard dismissal, centered crosses, and no implicit saves.
const assert=require('node:assert/strict'), fs=require('node:fs/promises'), path=require('node:path');
async function run(pw){
 const browser=await pw.chromium.launch({headless:true}), page=await browser.newPage({viewport:{width:1272,height:1000}});
 const origin='http://127.0.0.1:18858', errors=[], evidence=[], writes=[];
 page.setDefaultTimeout(10000);
 const types=[{code:'other',label:'其他',editable_name:true,names:[]}];
 const item={id:'f1',title:'合成费用',project_id:'p1',project:'关闭按钮测试',category:'其他',project_type:'other',project_name:'合成费用',
  amount_cents:1000,amount:'10.00',stage:'draft',verified:false,complete:true,ready:false,version:'v1',missing:[],attachments:[],events:[]};
 const state={app_mode:'invoice',token:'synthetic',projects:[{id:'p1',name:'关闭按钮测试'},{id:'p2',name:'另一项目'}],
  items:[item],inbox:[],categories:['其他'],roles:{invoice:'发票'},stages:{draft:'待核对'},report_types:types,report_template_ready:true,category_auto_ready:false};
 page.on('pageerror',e=>errors.push(e.message));
 await page.route(origin+'/**',async route=>{
  const request=route.request(),url=new URL(request.url()),json=data=>route.fulfill({contentType:'application/json',body:JSON.stringify(data)});
  if(url.pathname==='/api/state')return json(state);
  if(url.pathname==='/api/reimbursement-people')return json({project_id:'p1',people:[],summary:{rows:[],total_cents:1000,expense_count:1}});
  if(url.pathname==='/api/material-matching')return json({vouchers:[],payments:[],other:[],matches:[],suggestions:[],report_types:types});
  if(url.pathname==='/api/report-snapshot')return json({rows:[{row:34,name:'合成费用',items:[{id:'f1'}]}]});
  if(url.pathname==='/api/report-preview')return json({combined_pdf:'synthetic-combined.pdf',combined_preview_pages:['synthetic.svg'],combined_page_sizes:[{width:842,height:595}]});
  if(url.pathname.startsWith('/export/'))return route.fulfill({contentType:'image/svg+xml',body:'<svg xmlns="http://www.w3.org/2000/svg" width="842" height="595"><rect width="842" height="595" fill="white"/></svg>'});
  if(request.method()==='POST'){
   writes.push(url.pathname);assert.equal(url.pathname,'/api/expense-print-preview','Closing or cancelling must never save a fee or change a project');
   return json({id:'f1',version:'v1',complete:true,missing:[],pdf:'synthetic.pdf',preview_pages:[],page_count:0});
  }
  if(url.pathname.startsWith('/api/'))return json({queue:[]});
  const name=url.pathname==='/'?'index.html':url.pathname.slice(1);
  if(!/^[a-z-]+\.(html|js|css)$/.test(name))return route.abort();
  return route.fulfill({contentType:name.endsWith('.js')?'text/javascript':name.endsWith('.css')?'text/css':'text/html',body:await fs.readFile(path.resolve(__dirname,'../src/invoice_print_layout/web',name))});
 });
 async function check(dialogId,keyboard=false){
  const dialog=page.locator('#'+dialogId);await dialog.waitFor({state:'visible'});
  const close=dialog.locator('.dialog-close');assert.equal(await close.count(),1);
  await page.waitForFunction(id=>!document.getElementById(id)?.querySelector('.dialog-close')?.disabled,dialogId);
  const geometry=await close.evaluate(el=>{
   const r=el.getBoundingClientRect(),s=getComputedStyle(el),before=getComputedStyle(el,'::before'),after=getComputedStyle(el,'::after');
   return {width:r.width,height:r.height,padding:s.padding,fontSize:s.fontSize,content:el.textContent,name:el.getAttribute('aria-label'),
    centeredX:parseFloat(before.left)+parseFloat(s.borderLeftWidth),centeredY:parseFloat(before.top)+parseFloat(s.borderTopWidth),
    beforeTransform:before.transform,afterTransform:after.transform,outlineStyle:s.outlineStyle};
  });
  assert.equal(geometry.width,40);assert.equal(geometry.height,40);assert.equal(geometry.padding,'0px');assert.equal(geometry.content,'×');
  assert.equal(geometry.fontSize,'0px');assert.ok(geometry.name?.startsWith('关闭'));
  assert.ok(Math.abs(geometry.centeredX-geometry.width/2)<.1);assert.ok(Math.abs(geometry.centeredY-geometry.height/2)<.1);
  assert.match(geometry.beforeTransform,/matrix/);assert.notEqual(geometry.beforeTransform,geometry.afterTransform);
  assert.equal(await dialog.locator('.dialoghead button:not(.dialog-close),.matching-header>button:not(.dialog-close)').count(),0);
  evidence.push({dialogId,keyboard,...geometry});
  if(keyboard){await close.focus();await close.press('Enter');}else{const rect=await close.boundingBox();await page.mouse.click(rect.x+20,rect.y+20);}
  await dialog.waitFor({state:'hidden'});
 }
 try{
  await page.goto(origin+'/?project=p1');await page.locator('[data-item=f1]').waitFor();
  for(const theme of ['light','dark']){
   await page.locator('#themeSelect').selectOption(theme);
   await page.locator('#reimbursementPeople').click();await check('reimbursementPeopleDialog');
   await page.locator('#materialMatching').click();await check('matchingDeskDialog',true);
   await page.locator('#previewReport').click();await check('reportPreviewDialog');
   await page.locator('#createProject').click();await page.locator('#project-create [name=name]').fill('不会保存');await check('project-create',true);
   if(!await page.locator('[data-item=f1]').evaluate(el=>el.classList.contains('selected')))await page.locator('[data-item=f1] .title').click();await page.locator('#editReviewFields').click();
   await page.locator('#reviewEditAmount').fill('88.00');await check('reviewEditDialog');assert.equal(item.amount,'10.00');
   await page.locator('[data-check=f1]').check();await page.locator('#bulkAssignExpenses').click();await check('reimbursementPeopleBulk');
   await page.locator('#batchProjectType').click();await check('reviewBatchTypeDialog');
   await page.locator('[data-move-item=f1]').click();await check('moveExpenseDialog');
   await page.locator('[data-delete-item=f1]').click();await check('deleteExpenseDialog');
   await page.locator('#projectLinks [data-project-menu-id=p1]').click({button:'right'});await page.locator('[data-project-action=rename]').click();await check('projectActionDialog');
   await page.locator('[data-check=f1]').uncheck();
  }
  await page.setViewportSize({width:390,height:844});await page.locator('#materialMatching').click();await check('matchingDeskDialog');
  await page.locator('#reimbursementPeople').click();await check('reimbursementPeopleDialog',true);
  assert.deepEqual(errors,[]);assert.ok(writes.every(route=>route==='/api/expense-print-preview'));
  return {passed:true,dialogs:evidence,mouseHitsCentered:true,keyboardDismissal:true,mobileFits:true,businessWrites:0,pageErrors:errors};
 }finally{await browser.close();}
}
module.exports=run;
if(require.main===module)run(require('playwright')).then(r=>console.log(JSON.stringify(r))).catch(e=>{console.error(e);process.exitCode=1;});
