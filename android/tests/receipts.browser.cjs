// Shipped WebView UI, synthetic Native only. Device tests cover SQLite/HTTP/OCR.
const assert=require('node:assert/strict'),fs=require('node:fs/promises'),path=require('node:path');
async function run(pw){
 const browser=await pw.chromium.launch({headless:true}),context=await browser.newContext({viewport:{width:393,height:852}}),errors=[];
 try{
  await context.addInitScript(()=>{
   const categories=['材料采购','酒店','高铁','外卖','餐饮','打车','快递','打印','其他'];
   const catalog={categories,projects:[{id:'p1',name:'合成项目一',main_person_id:'lead',people:[{id:'lead',label:'负责人一'}]},{id:'p2',name:'合成项目二',people:[{id:'person2',label:'负责人二'}]}]};
   window.__calls=[];window.__ocr={done:false};window.__inbox={rows:[],paired:false,catalog:{categories,projects:[]}};
   window.Native={invoke(raw){const r=JSON.parse(raw);window.__calls.push(r);let value=true;
    if(r.method==='receiptPair'&&window.__pairError){setTimeout(()=>window.receiveNative({id:r.id,error:window.__pairError}),0);return;}
    const defaults={project_id:window.__inbox.paired?'p1':'',category:'其他',catalog:window.__inbox.catalog,people_defaults:{p1:'lead',p2:'person2'}};
    switch(r.method){
     case 'state':value={projects:[],materials:[],expenses:[],people:[],reports:[],settings:{experience:[]},services:{},report_types:[],receipt_inbox:window.__inbox};break;
     case 'receiptCamera':value={token:'capture',captured_at:'2026-11-01T10:00:00',defaults};break;
     case 'receiptOcr':value=window.__ocr;break;
     case 'receiptSave':window.__inbox.rows.unshift({...r.args,amount_cents:r.args.amount?Math.round(Number(r.args.amount)*100):null,project_name:window.__inbox.catalog.projects.find(p=>p.id===r.args.project_id)?.name||'',client_id:'uuid',status:r.args.project_id?'pending':'reselect'});break;
     case 'receiptCancel':break;
     case 'receiptThumbnail':value='data:image/gif;base64,R0lGODlhAQABAAD/ACwAAAAAAQABAAACADs=';break;
     case 'receiptPair':window.__inbox.paired=true;window.__inbox.server_name='合成电脑';window.__inbox.catalog=catalog;break;
     case 'receiptDefaults':value=defaults;break;
     case 'receiptEdit':Object.assign(window.__inbox.rows[0],r.args,{amount_cents:Math.round(Number(r.args.amount)*100),status:'pending'});break;
     case 'receiptSync':for(const row of window.__inbox.rows)if(row.project_id)row.status='received';break;
     case 'receiptDelete':window.__inbox.rows=[];break;
     case 'receiptUnpair':window.__inbox.paired=false;window.__inbox.catalog={categories,projects:[]};break;
     default:throw Error('Unexpected method '+r.method);
    }
    setTimeout(()=>window.receiveNative({id:r.id,value:structuredClone(value)}),0);
   }};
  });
  await context.route('http://invoice-android.test/**',async route=>{const asset=new URL(route.request().url()).pathname.slice(1)||'index.html';assert.ok(['index.html','style.css','app.js','workbench.js','receipts.js'].includes(asset));await route.fulfill({contentType:asset.endsWith('.js')?'text/javascript':asset.endsWith('.css')?'text/css':'text/html',body:await fs.readFile(path.resolve(__dirname,'../app/src/main/assets',asset))});});
  const page=await context.newPage();page.on('pageerror',e=>errors.push(e.message));await page.goto('http://invoice-android.test/');await page.locator('#receiptCamera').click();
  await page.getByText('先连接电脑获取项目；现在仍可保存，之后再选项目。').waitFor();assert.equal(await page.locator('#dialog select').count(),3);assert.equal(await page.locator('#dialog [name=role]').count(),0);await page.locator('#confirmDialog').click();await page.getByText('需要重新选择',{exact:true}).waitFor();assert.equal(await page.getByText('金额待确认',{exact:true}).count(),1);
  await page.locator('[data-receipt-delete]').click();await page.locator('#confirmDialog').click();await page.getByText('还没有收票',{exact:true}).waitFor();
  await page.locator('#receiptConnect').click();await page.locator('[name=url]').fill('http://192.168.1.5:8769');await page.locator('[name=code]').fill('12345678');
  await page.evaluate(()=>window.__pairError='配对未完成，请在电脑重新生成配对码');await page.locator('#confirmDialog').click();
  await page.getByRole('alert').filter({hasText:'配对未完成，请在电脑重新生成配对码'}).waitFor();assert.equal(await page.locator('#dialog').isVisible(),true);
  await page.evaluate(()=>window.__pairError='');await page.locator('#confirmDialog').click();await page.waitForFunction(()=>!document.getElementById('dialog').open);await page.locator('nav [data-tab=receipts]').click();
  await page.locator('#receiptCamera').click();await page.locator('[name=person_id]').selectOption('lead');await page.locator('[name=project_id]').selectOption('p2');assert.equal(await page.locator('[name=person_id]').inputValue(),'person2');assert.equal(await page.locator('[name=person_id] option[value=lead]').count(),0);
  await page.locator('[name=amount]').fill('123.45');await page.locator('[name=category]').selectOption('餐饮');await page.evaluate(()=>window.__ocr={done:true,amount:'89.30',category:'酒店'});await page.getByText('本地识别完成，请确认后保存',{exact:true}).waitFor();assert.equal(await page.locator('[name=amount]').inputValue(),'123.45');assert.equal(await page.locator('[name=category]').inputValue(),'餐饮');await page.locator('[name=note]').fill('合成备注');
  await page.setViewportSize({width:393,height:300});const save=await page.locator('#confirmDialog').boundingBox();assert.ok(save.y>=0&&save.y+save.height<=300);await page.locator('#confirmDialog').click();await page.setViewportSize({width:393,height:852});await page.getByText('待上传',{exact:true}).waitFor();assert.equal(await page.locator('#receiptRows').innerText().then(t=>t.includes('合成项目二')),true);
  await page.locator('#receiptRetryAll').click();await page.getByText('已到电脑',{exact:true}).waitFor();assert.equal(await page.locator('[data-receipt-delete]').count(),0);assert.equal(await page.evaluate(()=>window.__calls.some(r=>['save','import','camera'].includes(r.method))),false);
  await page.locator('#receiptCamera').click();await page.locator('#cancelDialog').click();await page.waitForFunction(()=>window.__calls.some(r=>r.method==='receiptCancel'));assert.equal(await page.locator('[data-receipt]').count(),1);await page.evaluate(async()=>{await api('receiptUnpair');await refresh();});assert.equal(await page.locator('#receiptRows').innerText().then(t=>t.includes('合成项目二')),true);assert.deepEqual(errors,[]);
  return {passed:true,cases:['offline unknown amount and missing project save','delete pending only','manual pairing and initial catalog failure message','project switches people and default','late OCR never overwrites user edits','keyboard viewport keeps Save visible','upload status and ledger separation','cancel discards capture'],page_errors:errors};
 }finally{await context.close();await browser.close();}
}
module.exports=run;
if(require.main===module)run(require('playwright')).then(r=>console.log(JSON.stringify(r,null,2))).catch(e=>{console.error(e);process.exitCode=1;});
