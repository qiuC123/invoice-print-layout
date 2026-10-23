// Real UI and synthetic intercepted responses only; no real invoices or projects.
const assert=require('node:assert/strict'),fs=require('node:fs/promises'),path=require('node:path');
async function run(pw){
 const browser=await pw.chromium.launch({headless:true}),page=await browser.newPage({viewport:{width:1440,height:1000}});
 page.setDefaultTimeout(10000);const origin='http://127.0.0.1:18818',errors=[],writes=[],native=[];
 const report_types=[
  {code:'expansion',label:'拓展',editable_name:false,names:['场地费用(消费)','施工押金(可退)']},
  {code:'transport',label:'车费',editable_name:false,names:['火车、飞机','打的(出租车、滴滴等)']},
  {code:'living',label:'生活费',editable_name:false,names:['员工统一购餐费(附清单)','现场饮用水、夜宵']},
  {code:'personnel',label:'人员',editable_name:false,names:['外请人员金额(附清单)']},
  {code:'materials',label:'现场购材料',editable_name:true,names:[]},{code:'other',label:'其他',editable_name:true,names:[]}];
 const fee=(id,extra={})=>({id,title:'人工名称 '+id,project:'合成核对项目',project_id:'p1',category:'其他',stage:'draft',verified:false,ready:false,complete:true,amount:'70.00',amount_cents:7000,missing:[],attachments:[{id:id+'-proof',name:id+'-original.pdf',role:'invoice',available:true,digest:id+'-source'}],events:[],version:id+'-v1',expense_date:'2026-10-13',merchant:'原件商家',note:'原始备注',invoice_state:'received',waiting_days:0,project_type:'other',project_name:'人工报销名称 '+id,report_choice:{project_type:'materials',project_name:'机器旧建议'},report_choice_suggestion:{project_type:'living',project_name:'现场饮用水、夜宵',source:'local_rule',reason:'原件文字包含饮用水 <img data-rule-injection="yes">'},...extra});
 const items=[fee('complete',{amount:'50.00',amount_cents:5000,material_match:{group_id:'g-complete50',provisional:true,alignment_status:'balanced',voucher_total_cents:5000,payment_total_cents:5000,payment_count:1,voucher_count:1,reason:'金额相符，归属和内容仍需人工核对。'},attachments:[{id:'complete-proof',name:'complete-original.pdf',role:'invoice',available:true,digest:'complete-source'},{id:'complete-payment',name:'complete-payment.png',role:'payment',available:true,digest:'complete-payment-source'}]}),fee('fee50',{amount:'50.00',amount_cents:5000,complete:false,missing:['付款截图'],material_match:{group_id:'g50',provisional:true,payment_total_cents:5000,payment_count:0,voucher_count:1}}),fee('ordinaryMissing',{complete:false,missing:['发票']}),fee('ready',{verified:true,ready:true}),fee('submitted',{stage:'submitted',verified:true,ready:true}),fee('deleted',{deleted:true}),fee('held',{returned_to_synthesis:true})];
 const originalMatch=JSON.stringify(items.find(x=>x.id==='complete').material_match);
 const originals=Object.fromEntries(items.map(x=>[x.id,JSON.stringify({attachments:x.attachments,merchant:x.merchant,expense_date:x.expense_date,note:x.note})]));
 const state={app_mode:'invoice',projects:[{id:'p1',name:'合成核对项目',reimbursement_status:'unreimbursed'}],items,inbox:[],categories:['其他','餐饮','材料采购'],roles:{invoice:'发票',payment:'付款截图'},stages:{draft:'待核对',submitted:'已提交'},report_types,token:'synthetic',category_auto_ready:false,report_template_ready:true};
 let failure=null,hold=false,release;
 page.on('pageerror',e=>errors.push(e.message));page.on('dialog',d=>{native.push(d.type());d.dismiss();});
 await page.route(origin+'/**',async route=>{
  const req=route.request(),url=new URL(req.url()),json=(x,status=200)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(x)});
  if(url.pathname==='/api/expense-print-preview'){const body=req.postDataJSON(),x=items.find(x=>x.id===body.id);return json({id:x.id,version:x.version,complete:x.complete,partial:!x.complete,missing:x.missing,preview_pages:[]});}
  if(req.method()==='POST'){
   const body=req.postDataJSON();writes.push({route:url.pathname,body});assert.equal(req.headers()['x-workbench-token'],state.token);
   if(url.pathname==='/api/review/edit-fields'){
    assert.deepEqual(Object.keys(body).sort(),['amount','expected_version','id','project_name','project_type']);
    const x=items.find(x=>x.id===body.id);assert.ok(x);assert.equal(body.expected_version,x.version);assert.equal(x.returned_to_synthesis||false,false);assert.equal(x.stage,'draft');
    if(hold){hold=false;await new Promise(resolve=>{release=resolve;});}
    if(failure){const current=failure;failure=null;if(current==='conflict'){x.version+='-external';return json({error:'合成记录已更新'},409);}return json({error:'合成服务暂时失败'},503);}
    Object.assign(x,{project_type:body.project_type,project_name:body.project_name,title:body.project_name,amount:body.amount,amount_cents:Math.round(Number(body.amount)*100),verified:false,ready:false,report_choice:{project_type:body.project_type,project_name:body.project_name},version:x.version+'-saved'});return json(x);
   }
   if(url.pathname==='/api/material-matching/undo'){
    const x=items.find(x=>x.id==='fee50');assert.equal(body.group_id,'g50');assert.equal(body.expected_version,x.version);Object.assign(x,{returned_to_synthesis:true,ready:false,version:x.version+'-returned'});delete x.material_match;return json({item:x,match:{status:'undone'}});
   }
   assert.fail('unexpected write '+url.pathname);
  }
  if(url.pathname==='/api/state')return json(state);
  if(url.pathname.startsWith('/api/'))return json({queue:[],candidates:[],suggestions:[],vouchers:[],payments:[],other:[],matches:[],report_types});
  const file=url.pathname==='/'?'index.html':url.pathname.slice(1);if(!/^[a-z-]+\.(html|js|css)$/.test(file))return route.abort();
  return route.fulfill({contentType:file.endsWith('.js')?'text/javascript':file.endsWith('.css')?'text/css':'text/html',body:await fs.readFile(path.resolve(__dirname,'../src/invoice_print_layout/web',file))});
 });
 const dialog=()=>page.locator('#reviewEditDialog'),save=()=>page.locator('#reviewEditSave'),type=()=>page.locator('#reviewEditType'),name=()=>page.locator('#reviewEditName'),amount=()=>page.locator('#reviewEditAmount');
 const open=async id=>{if(!await page.locator(`[data-item="${id}"]`).evaluate(el=>el.classList.contains("selected")))await page.locator(`[data-item="${id}"] .title`).click();await page.locator('#editReviewFields').click();await dialog().waitFor({state:'visible'});};
 const unchanged=id=>assert.equal(JSON.stringify({attachments:items.find(x=>x.id===id).attachments,merchant:items.find(x=>x.id===id).merchant,expense_date:items.find(x=>x.id===id).expense_date,note:items.find(x=>x.id===id).note}),originals[id]);
 try{
  await page.goto(origin+'/?project=p1');await page.locator('[data-item="complete"]').waitFor();
  assert.deepEqual(await page.locator('[data-item]').evaluateAll(xs=>xs.map(x=>x.dataset.item).sort()),['complete','fee50','ordinaryMissing']);
  assert.equal(await page.locator('[data-item="held"]').count(),0);assert.equal(items.length,7);
  assert.equal(await page.locator('[data-item="complete"] .badge').innerText(),'金额已对齐 · 待核对');
  await open('complete');assert.equal(await type().inputValue(),'other');assert.equal(await name().inputValue(),'人工报销名称 complete');
  assert.equal(await page.locator('#reviewEditSuggestion').innerText(),'建议：生活费 · 现场饮用水、夜宵');assert.doesNotMatch(await dialog().innerText(),/规则建议|原件文字包含饮用水|识别文字保留/);assert.equal(await page.locator('[data-rule-injection]').count(),0);
  if(process.env.INVOICE_BROWSER_EVIDENCE){await fs.mkdir(process.env.INVOICE_BROWSER_EVIDENCE,{recursive:true});await page.screenshot({path:path.join(process.env.INVOICE_BROWSER_EVIDENCE,'synthetic-concise-review-edit.png')});}
  for(const entry of report_types.filter(x=>!x.editable_name)){await type().selectOption(entry.code);assert.equal(await name().evaluate(x=>x.tagName),'SELECT');assert.equal(await name().inputValue(),'');assert.equal(await save().isDisabled(),true);await name().selectOption(entry.names[0]);assert.equal(await save().isEnabled(),true);}
  await type().selectOption('transport');await name().evaluate(x=>{x.add(new Option('现场饮用水、夜宵','现场饮用水、夜宵'));x.value='现场饮用水、夜宵';x.dispatchEvent(new Event('change',{bubbles:true}));});assert.equal(await save().isDisabled(),true);
  await type().selectOption('materials');assert.equal(await name().evaluate(x=>x.tagName),'INPUT');await name().fill('人工确定材料名称');
  for(const value of ['', '-1','12.345','NaN','1e2','1000000000']){await amount().fill(value);assert.equal(await save().isDisabled(),true);}
  await amount().fill('55.00');const pendingBefore=writes.length;hold=true;await save().click();await page.waitForFunction(()=>document.getElementById('reviewEditSave').disabled);await dialog().locator('form').evaluate(x=>x.requestSubmit());assert.equal(writes.length,pendingBefore+1);release();await dialog().waitFor({state:'detached'});
  assert.equal(items.find(x=>x.id==='complete').title,'人工确定材料名称');assert.equal(items.find(x=>x.id==='complete').verified,false);assert.equal(await page.locator('#tabs [data-view=review]').getAttribute('class'),'active');unchanged('complete');
  assert.equal(items.find(x=>x.id==='complete').amount_cents,5500);assert.equal(JSON.stringify(items.find(x=>x.id==='complete').material_match),originalMatch,'original balanced match snapshot must remain unchanged');
  assert.equal(await page.locator('[data-item="complete"] .badge').innerText(),'金额有差额 · 待核对');assert.equal(await page.locator('#detail > .badge').innerText(),'金额有差额 · 待核对');
  const alignmentText=await page.locator('#detail .rule.warn').innerText();assert.match(alignmentText,/付款合计 ¥ 50\.00/);assert.match(alignmentText,/票据合计 ¥ 50\.00/);assert.match(alignmentText,/当前报销金额 ¥ 55\.00.*有差额/);assert.doesNotMatch(await page.locator('#detail').innerText(),/金额已对齐|金额相符/);
  await open('complete');assert.equal(await name().inputValue(),'人工确定材料名称','saved manual name must survive a conflicting rule suggestion');await dialog().getByRole('button',{name:'取消',exact:true}).click();
  await open('fee50');assert.match(await dialog().innerText(),/还缺：付款截图/);await amount().fill('55.00');await name().fill('人工修改50元原费用');failure='unavailable';const failedBefore=writes.length;await save().click();await page.locator('#reviewEditError').filter({hasText:'合成服务暂时失败'}).waitFor();assert.equal(await amount().inputValue(),'55.00');assert.equal(await name().inputValue(),'人工修改50元原费用');assert.equal(await save().isEnabled(),true);assert.equal(writes.length,failedBefore+1);
  await save().click();await dialog().waitFor({state:'detached'});assert.equal(items.find(x=>x.id==='fee50').amount_cents,5500);assert.equal(items.find(x=>x.id==='fee50').complete,false);assert.equal(await page.locator('[data-item="fee50"]').count(),1);unchanged('fee50');
  await open('fee50');await amount().fill('60.00');await name().fill('冲突时应保留名称');failure='conflict';const conflictBefore=writes.length;await save().click();await page.locator('#reviewEditError').filter({hasText:'重新打开'}).waitFor();assert.equal(await save().isDisabled(),true);assert.equal(await amount().inputValue(),'60.00');assert.equal(await name().inputValue(),'冲突时应保留名称');await dialog().locator('form').evaluate(x=>x.requestSubmit());assert.equal(writes.length,conflictBefore+1);await dialog().getByRole('button',{name:'取消',exact:true}).click();
  await page.setViewportSize({width:390,height:844});await open('fee50');assert.equal(await dialog().evaluate(x=>x.scrollWidth<=x.clientWidth),true);await dialog().getByRole('button',{name:'取消',exact:true}).click();
  await page.locator('#undoExpenseMatch').click();await page.locator('#matchingDeskDialog[open]').waitFor();assert.match(await page.locator('#notice').innerText(),/已退回合成台/);assert.equal(items.length,7);assert.equal(items.find(x=>x.id==='fee50').returned_to_synthesis,true);unchanged('fee50');
  assert.equal(await page.locator('#tabs [data-view=synthesis],.metric[data-view=synthesis]').count(),0);
  await page.locator('#matchingClose').click();
  await page.locator('#tabs [data-view=review]').click();assert.deepEqual(await page.locator('[data-item]').evaluateAll(xs=>xs.map(x=>x.dataset.item).sort()),['complete','ordinaryMissing']);assert.equal(await page.locator('.metric[data-view=review] em').innerText(),'2 笔事项');
  for(const [id,view] of [['held','review'],['fee50','review'],['ready','submitted'],['submitted','submitted'],['deleted','deleted']]){await page.evaluate(({id,view})=>{active=id;window.document.getElementById('tabs').querySelector(`[data-view="${view}"]`).click();},{id,view});assert.equal(await page.locator('#editReviewFields').count(),0);const message=await page.evaluate(id=>{try{window.openReviewFieldsEditor(id);return '';}catch(e){return e.message;}},id);assert.match(message,/仅可在待核对/);}
  assert.deepEqual(native,[]);assert.deepEqual(errors,[]);assert.ok(writes.every(x=>['/api/review/edit-fields','/api/material-matching/undo'].includes(x.route)));assert.equal(items.find(x=>x.id==='ordinaryMissing').complete,false);
  return {passed:true,scope:'synthetic review editor: fixed/open-name linkage, manual values beat rule suggestions, amount validation, complete/incomplete editing with originals intact, pending duplicate prevention, 503 retention/retry, 409 retention/reopen, returned fee filtering with ordinary missing preserved, eligibility guards and mobile',writes:writes.length,originalFeesPreserved:items.length};
 }finally{await browser.close();}
}
module.exports=run;
if(require.main===module)run(require('playwright')).then(console.log).catch(e=>{console.error(e);process.exitCode=1;});
