// UI regression test with synthetic API responses; never touches a real ledger.
// Run with Node.js and playwright available: node tests/receipt_batch_browser.cjs
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');

async function run(playwright) {
  const browser = await playwright.chromium.launch({headless:true});
  const context = await browser.newContext({viewport:{width:1280,height:900}});
  const page = await context.newPage();
  const errors = [], requests = [], saved = new Map();
  let failSave = true, failRecognition = true, concurrent = 0, maxConcurrent = 0;
  page.on('pageerror', e=>errors.push(e.message));
  const assets = path.resolve(__dirname, '../src/invoice_print_layout/web');
  const base = 'http://127.0.0.1:18765';
  const existing = {id:'existing',title:'Synthetic existing',stage:'draft'};
  const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aN9sAAAAASUVORK5CYII=', 'base64');
  const file = name=>({name,mimeType:'image/png',buffer:png});
  const select = i=>page.locator(`[data-receipt-index="${i}"]`).click();
  const idle = ()=>page.waitForFunction(()=>!document.getElementById('receiptFile').disabled);
  const summary = ()=>page.locator('#receiptProgress').innerText();
  await context.route(base+'/**', async route=>{
    const url = new URL(route.request().url());
    const json = (value,status=200)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(value)});
    if(url.pathname==='/api/state')return json({items:[],inbox:[],categories:['材料采购','外卖','其他'],roles:{purchase:'购买明细',order:'订单',invoice:'发票',payment:'扣费记录'},stages:{draft:'待提交'},token:'synthetic'});
    if(url.pathname.startsWith('/api/')){
      const body=route.request().postDataJSON(); requests.push({route:url.pathname,...body});
      concurrent++;maxConcurrent=Math.max(maxConcurrent,concurrent);
      try {
        await new Promise(resolve=>setTimeout(resolve,15));
        if(url.pathname==='/api/recognize'){
          if(body.name==='retry.png'&&failRecognition){failRecognition=false;return await json({error:'synthetic recognition failure'},400);}
          if(body.name==='existing.png')return await json({duplicates:[existing],fields:{},warnings:[]});
          return await json({duplicates:[],fields:{title:body.name,amount:body.name==='blank.png'?'':'10.00',category:'材料采购',expense_date:'',order_number:body.name==='same-order.png'?'good.png':body.name},role:'purchase',warnings:[],evidence:{},text:'synthetic'});
        }
        if(body.name==='save-fail.png'&&failSave){failSave=false;return await json({error:'synthetic save failure'},500);}
        if(saved.has(body.fields.order_number))return await json({duplicates:[existing]});
        saved.set(body.fields.order_number,body);
        return await json({duplicates:[],item:{id:'new-'+saved.size}});
      } finally {concurrent--;}
    }
    const name=url.pathname==='/'?'index.html':url.pathname.slice(1);
    if(!['index.html','app.js','receipt.js','style.css'].includes(name))return route.abort();
    await route.fulfill({contentType:name.endsWith('.js')?'text/javascript':name.endsWith('.css')?'text/css':'text/html',body:await fs.readFile(path.join(assets,name))});
  });
  try {
    await page.goto(base);
    await page.locator('#recognizeReceipt').click();
    assert.equal(await page.locator('#receiptFile').getAttribute('multiple'),'');
    await page.locator('#receiptFile').setInputFiles(['good.png','retry.png','blank.png','same-order.png','save-fail.png','existing.png'].map(file));
    await idle();
    assert.match(await summary(),/共 6 张/);
    assert.match(await summary(),/识别失败 1/);
    assert.equal(saved.size,0,'recognition must not write');
    await page.locator('#receiptForm [name=title]').fill('Edited purchase');
    await select(2);await page.locator('#receiptForm [name=merchant]').fill('Edited merchant');
    await select(0);assert.equal(await page.locator('#receiptForm [name=title]').inputValue(),'Edited purchase');
    await select(2);assert.equal(await page.locator('#receiptForm [name=merchant]').inputValue(),'Edited merchant');
    await page.locator('#saveAllReceipts').click();await idle();
    assert.equal(saved.size,1);
    assert.equal(saved.get('good.png').fields.title,'Edited purchase');
    assert.match(await summary(),/重复，未新增 2/);
    assert.match(await summary(),/保存失败／需补填 2/);
    // A failed request and a required-field error must not block later saves.
    await select(2);await page.locator('#receiptForm [name=amount]').fill('24.60');
    await page.locator('#saveAllReceipts').click();await idle();
    assert.equal(saved.size,3);
    assert.equal(saved.get('blank.png').fields.amount,'24.60');
    assert.equal(saved.get('blank.png').fields.merchant,'Edited merchant');
    assert.equal(requests.filter(x=>x.route==='/api/receipt'&&x.name==='good.png').length,1);
    await select(1);await page.locator('#retryReceipt').click();await idle();
    await page.locator('#skipReceipt').click();assert.match(await summary(),/已跳过 1/);
    await page.locator('#restoreReceipt').click();
    await page.locator('#receiptForm button[type=submit]').click();await idle();
    assert.equal(saved.size,4);
    assert.equal(await page.locator('#saveAllReceipts').isDisabled(),true);
    // Opening an existing matter must not discard the queue.
    await select(5);
    const popupPromise=page.waitForEvent('popup');
    await page.locator('[data-open-receipt]').click();
    const popup=await popupPromise;
    await popup.waitForLoadState();assert.equal(new URL(popup.url()).searchParams.get('matter'),'existing');
    assert.equal(await popup.evaluate(()=>active),'existing');await popup.close();
    assert.match(await summary(),/共 6 张/);
    // Appending does not lose previous entries; invalid files do not block valid ones.
    await page.locator('#receiptFile').setInputFiles([file('invalid.txt'),file('last.png')]);await idle();
    assert.match(await summary(),/共 8 张/);
    assert.match(await summary(),/识别失败 1/);
    await page.locator('#saveAllReceipts').click();await idle();assert.equal(saved.size,5);
    page.once('dialog',dialog=>dialog.dismiss());
    await page.locator('#closeReceipt').click();assert.equal(await page.locator('#receiptDialog').isVisible(),true);
    await select(6);await page.locator('#skipReceipt').click();
    await page.locator('#closeReceipt').click();
    await page.locator('#recognizeReceipt').click();
    assert.equal(await page.locator('[data-receipt-index]').count(),0);
    await page.locator('#receiptFile').setInputFiles(Array.from({length:51},(_,i)=>file(`limit${i}.png`)));
    assert.match(await page.locator('#receiptError').innerText(),/最多50张/);
    assert.equal(await page.locator('[data-receipt-index]').count(),0);
    await page.locator('#receiptFile').setInputFiles([file('mobile.png')]);await idle();
    await page.setViewportSize({width:390,height:844});
    assert.equal(await page.locator('#receiptDialog').evaluate(el=>el.scrollWidth<=el.clientWidth),true);
    assert.equal(maxConcurrent,1,'only one image API request at a time');
    assert.deepEqual(errors,[]);
    return {saved:saved.size, maxConcurrent, checks:'multi-select, edit retention, partial failure/retry, duplicate, skip/restore, append, close guard, size count limit, mobile layout'};
  } finally {await browser.close();}
}
module.exports=run;
if(require.main===module)run(require('playwright')).then(console.log).catch(error=>{console.error(error);process.exitCode=1;});
