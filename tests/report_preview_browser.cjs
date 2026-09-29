// Read-only preview scope, totals, template mapping, escaping and download.
const assert=require('node:assert/strict');
const fs=require('node:fs/promises');
const path=require('node:path');
async function run(pw){
 const browser=await pw.chromium.launch({headless:true});
 const page=await browser.newPage({viewport:{width:1440,height:1000}});
 const errors=[],writes=[];
 const matter=(id,category,cents,extra={})=>({id,title:id,project:'测试项目',category,stage:'draft',expense_date:'2026-09-25',merchant:'测试商家',order_number:'',note:'',amount_cents:cents,amount:(cents/100).toFixed(2),complete:false,verified:false,ready:false,missing:['发票'],events:[],attachments:[],...extra});
 const items=[matter('material','材料采购',23900,{title:'<img src=x onerror="window.top.injected=1">',note:'</p><script>window.injected=1</script>'}),matter('printing','材料采购',1400),matter('meal','餐饮',1450),matter('service','其他',30000),...Array.from({length:6},(_,i)=>matter('small'+i,'材料采购',100)),matter('paid','酒店',20000,{stage:'reimbursed'}),matter('cancelled','酒店',20000,{stage:'cancelled'})];
 const before=JSON.stringify(items);
 page.on('pageerror',e=>errors.push(e.message));
 await page.route('http://127.0.0.1:18767/**',async route=>{
  const u=new URL(route.request().url());
  if(route.request().method()==='POST'){writes.push(u.pathname);if(u.pathname==='/api/report-preview')return route.fulfill({contentType:'application/json',body:JSON.stringify({pdf:'trial.pdf',preview_pages:['trial-preview-1.png']})});return route.fulfill({status:400,body:'{}'});}
  if(u.pathname==='/api/report-snapshot'){
   const ids=new Set(u.searchParams.getAll('id'));
   const body=require('node:child_process').execFileSync(path.resolve('.venv/Scripts/python.exe'),['-c','import sys,json;from invoice_print_layout.report_snapshot import snapshot;print(json.dumps(snapshot(json.load(sys.stdin))))'],{input:JSON.stringify(items.filter(x=>ids.has(x.id))),env:{...process.env,PYTHONUTF8:'1',PYTHONPATH:path.resolve('src')}}).toString();
   return route.fulfill({contentType:'application/json',body});
  }
  if(u.pathname==='/api/state')return route.fulfill({contentType:'application/json',body:JSON.stringify({items,inbox:[],categories:['材料采购','餐饮','酒店','其他'],roles:{payment:'支付记录'},stages:{draft:'待提交',reimbursed:'已报销',cancelled:'已取消'},report_template_ready:true,report_person:'测试人',token:'test'})});
  const name=u.pathname==='/'?'index.html':u.pathname.slice(1);
  if(!['index.html','app.js','receipt.js','report.js','style.css'].includes(name))return route.abort();
  return route.fulfill({contentType:name.endsWith('.js')?'text/javascript':name.endsWith('.css')?'text/css':'text/html',body:await fs.readFile(path.resolve(__dirname,'../src/invoice_print_layout/web',name))});
 });
 try{
  await page.goto('http://127.0.0.1:18767/');await page.waitForSelector('#previewReport');
  await page.locator('#previewReport').click();
  const frame=page.frameLocator('#previewFrame');
  await frame.locator('#previewTotal').waitFor();
  assert.match(await frame.locator('#previewTotal').innerText(),/573\.50/);
  assert.equal(await frame.locator('.expense').count(),10);
  assert.equal(await frame.locator('[data-cell="H35"]').innerText(),'573.50');
  assert.equal(await frame.locator('[data-cell="H28"]').innerText(),'4.00');
  assert.match(await frame.locator('[data-cell="B28"]').innerText(),/small2.*small5/);
  assert.match(await frame.locator('[data-cell="B28"]').innerText(),/small2/);
  assert.doesNotMatch(await frame.locator('[data-cell="B28"]').innerText(),/¥/);
  assert.match(await frame.locator('[data-cell="B25"]').innerText(),/printing/);
  assert.equal(await frame.locator('[data-cell="B25"] a').getAttribute('href'),'#expenses');
  assert.equal(await frame.locator('[data-cell="H17"]').innerText(),'');
  assert.equal(await frame.locator('[data-cell="H19"]').innerText(),'14.50');
  assert.equal(await frame.locator('[data-cell="H30"]').innerText(),'300.00');
  assert.equal(await frame.locator('script, img').count(),0);
  assert.equal(await page.evaluate(()=>window.injected),undefined);
  await frame.locator('summary').first().click();
  assert.equal(await frame.locator('details').first().getAttribute('open'),'');
  await page.locator('#previewCategory').selectOption('餐饮');
  await frame.locator('#previewTotal').filter({hasText:'14.50'}).waitFor();
  assert.equal(await frame.locator('.expense').count(),1);
  const downloadPromise=page.waitForEvent('download');
  await page.locator('#downloadPreview').click();
  const download=await downloadPromise;
  const html=await fs.readFile(await download.path(),'utf8');
  assert.match(html,/14\.50/);assert.doesNotMatch(html,/573\.50/);
  assert.equal(download.suggestedFilename(),'报销审阅预览.html');
  await page.setViewportSize({width:390,height:844});
  assert.equal(await page.locator('#reportPreviewDialog').evaluate(x=>x.scrollWidth<=x.clientWidth),true);
  assert.equal(await frame.locator('body').evaluate(x=>x.scrollWidth<=innerWidth),true);
  await page.locator('#closePreview').click();
  await page.evaluate(()=>{checked.add('paid');$('projectFilter').value='测试项目';$('search').value='no-match';});
  await page.locator('#previewReport').click();
  await frame.locator('#previewTotal').filter({hasText:'200.00'}).waitFor();
  assert.match(await frame.locator('.status').innerText(),/已报销/);
  assert.match(await page.locator('#previewScope').innerText(),/勾选/);
  assert.equal(JSON.stringify(items),before);assert.deepEqual(writes,[]);assert.deepEqual(errors,[]);
  await page.getByRole('button',{name:'试生成并查看实际打印页'}).click();
  await page.getByRole('img',{name:'实际打印页 第1页'}).waitFor();
  assert.equal(await page.getByRole('link',{name:'打开／下载原始PDF打印'}).getAttribute('href'),'/export/trial.pdf');
  await page.locator('#previewCategory').selectOption('酒店');
  await page.locator('#actualPdf').waitFor({state:'detached'});
  assert.deepEqual(writes,['/api/report-preview']);assert.equal(JSON.stringify(items),before);
  return 'passed: draft scope, explicit hidden selection, integer totals, overflow, dining/other mapping, escaping, filter/download, mobile, read-only review, explicit trial page preview without submission';
 }finally{await browser.close();}
}
module.exports=run;
if(require.main===module)run(require('playwright')).then(console.log).catch(e=>{console.error(e);process.exitCode=1;});
