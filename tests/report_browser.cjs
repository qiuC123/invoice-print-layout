// Synthetic UI/API contract; no real ledger is changed.
const assert=require('node:assert/strict');
const fs=require('node:fs/promises');
const path=require('node:path');
async function run(pw){
 const browser=await pw.chromium.launch({headless:true});
 const page=await browser.newPage({viewport:{width:1280,height:900}});
 const errors=[],requests=[];let fail=true,releaseReport,reportStarted;
 const pendingReport=new Promise(resolve=>{reportStarted=resolve;});
 const item={id:'synthetic',title:'Synthetic expense',project:'Test',category:'酒店',stage:'draft',complete:true,verified:true,ready:true,amount_cents:10000,missing:[],events:[],attachments:[]};
 page.on('pageerror',e=>errors.push(e.message));
 await page.route('http://127.0.0.1:18767/**',async route=>{
  const u=new URL(route.request().url());
  const json=(x,status=200)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(x)});
  if(u.pathname==='/api/state')return json({items:[item],inbox:[],categories:['酒店'],roles:{invoice:'发票'},stages:{draft:'待提交'},token:'synthetic',report_person:'Test',report_template_ready:true});
  if(u.pathname==='/api/expense-print-preview'){
   const body=route.request().postDataJSON();assert.equal(body.id,item.id);assert.equal(body.expected_version,item.version);
   return json({id:item.id,version:item.version,complete:item.complete,missing:item.missing,pdf:'synthetic-preview.pdf',preview_pages:[],layout:'A4',page_count:0});
  }
  if(u.pathname==='/api/report'){requests.push(route.request().postDataJSON());if(fail){await new Promise(resolve=>{releaseReport=resolve;reportStarted();});fail=false;return json({error:'Synthetic mismatch'},400);}item.stage='submitted';return json({pdf:'report.pdf',md:'report.md',xlsx:'report.xlsx'});}
  const name=u.pathname==='/'?'index.html':u.pathname.slice(1);
  if(!['index.html','workbench-theme.js','workbench-theme.css','multi-filter.js','multi-filter.css','app.js','receipt.js','report.js','report-preview.css','style.css'].includes(name))return route.abort();
  return route.fulfill({contentType:name.endsWith('.js')?'text/javascript':name.endsWith('.css')?'text/css':'text/html',body:await fs.readFile(path.resolve(__dirname,'../src/invoice_print_layout/web',name))});
 });
 try{
  await page.goto('http://127.0.0.1:18767/');await page.waitForSelector('#tabs [data-view="submitted"]');
  await page.locator('#tabs [data-view="submitted"]').click();
  assert.equal(await page.locator('[data-view="ready"]').count(),0);
  assert.equal(await page.locator('#exportSelected').innerText(),'制作报销包');
  await page.locator('#selectVisible').check();await page.locator('#exportSelected').click();
  await page.waitForSelector('#reportDialog[open]');
  assert.equal(await page.locator('#reportDialog').isVisible(),true);
  assert.match(await page.locator('#reportDialog').innerText(),/生成报销表与材料打印文件。打印请选择 A4、1×1。/);
  assert.doesNotMatch(await page.locator('#reportDialog').innerText(),/复用打印助手|本地识别|输出总 PDF/);
  if(process.env.INVOICE_BROWSER_EVIDENCE){await fs.mkdir(process.env.INVOICE_BROWSER_EVIDENCE,{recursive:true});await page.screenshot({path:path.join(process.env.INVOICE_BROWSER_EVIDENCE,'synthetic-concise-report.png')});}
  assert.equal(await page.locator('#reportForm [name=person]').inputValue(),'Test');
  assert.equal(await page.locator('#reportForm [name=payment]').count(),0);
  await page.evaluate(()=>{view='review';renderSelection();});
  await page.locator('#createReport').click();await page.waitForFunction(()=>!reportBusy);
  assert.match(await page.locator('#reportError').innerText(),/可提交/);
  assert.equal(requests.length,0,'an already open dialog must reject submission after leaving 可提交');
  await page.evaluate(()=>{view='submitted';renderSelection();});
  await page.locator('#createReport').click();await pendingReport;
  assert.equal(await page.locator('#reportProgress').innerText(),'正在制作报销包…');releaseReport();
  await page.waitForFunction(()=>!reportBusy);
  assert.match(await page.locator('#reportError').innerText(),/Synthetic mismatch/);
  assert.equal(await page.locator('#reportDownloads a').count(),0);
  await page.locator('#createReport').click();await page.waitForFunction(()=>!reportBusy);
  assert.deepEqual(requests[1].ids,['synthetic']);assert.equal(requests[1].options.payment,undefined);
  assert.equal(await page.locator('#reportDownloads a').count(),3);
  assert.equal(await page.locator('#reportDownloads a').last().getAttribute('href'),'/export/report.xlsx');
  assert.match(await page.locator('#reportProgress').innerText(),/报销包已保留在“可提交”/);
  assert.equal(await page.evaluate(()=>checked.size),0);
  assert.equal(item.stage,'submitted');
  assert.equal(await page.locator('[data-check="synthetic"]').count(),1,'generated package remains visible in 可提交');
  await page.setViewportSize({width:390,height:844});
  assert.equal(await page.locator('#reportDialog').evaluate(x=>x.scrollWidth<=x.clientWidth),true);
  await page.locator('#closeReport').click();item.stage='reimbursed';
  await page.locator('#tabs [data-view="review"]').click();
  await page.evaluate(()=>{checked.add('synthetic');renderSelection();});
  assert.equal(await page.locator('#exportSelected').isVisible(),false);
  await page.evaluate(()=>$('exportSelected').onclick());await page.waitForSelector('#error:not([hidden])');
  assert.equal(await page.locator('#reportDialog').isVisible(),false);
  assert.equal(requests.length,2);assert.deepEqual(errors,[]);
  return 'passed: report options, partial-error feedback/retry, three downloads, paid exclusion, mobile';
 }finally{await browser.close();}
}
module.exports=run;
if(require.main===module)run(require('playwright')).then(console.log).catch(e=>{console.error(e);process.exitCode=1;});
