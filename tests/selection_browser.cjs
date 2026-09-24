// Synthetic workbench data only. Run: node tests/selection_browser.cjs
const assert=require('node:assert/strict');
const fs=require('node:fs/promises');
const path=require('node:path');
async function run(playwright){
  const browser=await playwright.chromium.launch({headless:true});
  const page=await browser.newPage({viewport:{width:1280,height:900}});
  const errors=[],exports=[],transitions=[];let failVerify=true;
  page.on('pageerror',e=>errors.push(e.message));
  const item=(id,project,category,stage='draft')=>({id,title:'Synthetic '+id,project,category,stage,complete:true,verified:false,ready:false,amount_cents:1000,missing:[],attachments:[],events:[]});
  const items=[item('a','项目甲','材料采购'),item('b','项目甲','外卖'),item('c','项目乙','材料采购'),item('d','项目甲','材料采购','reimbursed')];
  await page.route('http://127.0.0.1:18766/**',async route=>{
    const url=new URL(route.request().url());
    const json=x=>route.fulfill({contentType:'application/json',body:JSON.stringify(x)});
    if(url.pathname==='/api/state')return json({items,inbox:[],categories:['材料采购','外卖'],roles:{},stages:{reimbursed:'已报销'},token:'synthetic'});
    if(url.pathname==='/api/export'){exports.push(route.request().postDataJSON().ids);return json({pdf:'synthetic.pdf',md:'synthetic.md'});}
    if(url.pathname==='/api/transition'){
      const body=route.request().postDataJSON();transitions.push(body);
      if(body.id==='a'&&failVerify){failVerify=false;return route.fulfill({status:400,contentType:'application/json',body:JSON.stringify({error:'synthetic failure'})});}
      const x=items.find(x=>x.id===body.id);x.verified=true;x.ready=true;return json(x);
    }
    const name=url.pathname==='/'?'index.html':url.pathname.slice(1);
    if(!['index.html','app.js','receipt.js','style.css'].includes(name))return route.abort();
    return route.fulfill({contentType:name.endsWith('.js')?'text/javascript':name.endsWith('.css')?'text/css':'text/html',body:await fs.readFile(path.resolve(__dirname,'../src/invoice_print_layout/web',name))});
  });
  const selected=()=>page.evaluate(()=>[...checked].sort());
  try{
    await page.goto('http://127.0.0.1:18766/');
    await page.waitForSelector('[data-check="a"]');
    assert.equal(await page.locator('#exportSelected').isDisabled(),true);
    await page.locator('#projectFilter').selectOption('项目甲');
    await page.locator('#selectVisible').check();assert.deepEqual(await selected(),['a','b','d']);
    await page.locator('[data-check="b"]').uncheck();
    assert.equal(await page.locator('#selectVisible').evaluate(el=>el.indeterminate),true);
    await page.locator('#selectVisible').check();assert.deepEqual(await selected(),['a','b','d']);
    await page.locator('#categoryFilter').selectOption('外卖');
    assert.match(await page.locator('#listSummary').innerText(),/其中 2 笔不在当前列表/);
    await page.locator('#selectVisible').uncheck();assert.deepEqual(await selected(),['a','d']);
    await page.locator('#clearSelected').click();assert.deepEqual(await selected(),[]);
    await page.locator('#categoryFilter').selectOption('');
    await page.locator('#tabs [data-view="review"]').click();
    await page.locator('#selectVisible').check();assert.deepEqual(await selected(),['a','b']);
    assert.equal(await page.locator('#exportSelected').isVisible(),false);
    assert.equal(await page.locator('#verifySelected').isVisible(),true);
    await page.locator('[data-item="a"] .title').click();
    assert.deepEqual(await page.locator('[data-action]').evaluateAll(xs=>xs.map(x=>x.dataset.action)),['verify']);
    await page.locator('#clearSelected').click();
    await page.locator('#search').fill('Synthetic b');
    await page.locator('#selectVisible').check();assert.deepEqual(await selected(),['b']);
    await page.locator('#search').fill('no-match');
    assert.equal(await page.locator('#selectVisible').isDisabled(),true);
    assert.equal(await page.locator('#selectVisible').isChecked(),false);
    await page.locator('#clearSelected').click();assert.deepEqual(await selected(),[]);
    await page.locator('[data-view="inbox"]').click();
    assert.equal(await page.locator('#selectionTools').isVisible(),false);
    assert.equal(await page.locator('#exportSelected').isVisible(),false);
    await page.locator('#search').fill('');
    await page.locator('#tabs [data-view="all"]').evaluate(el=>el.click());
    await page.setViewportSize({width:390,height:844});
    assert.equal(await page.locator('#selectionTools').isVisible(),true);
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
    // Mixed selection: fail one item, continue others, skip missing and paid.
    items.push({...item('e','项目甲','材料采购'),complete:false,missing:['发票']});
    await page.evaluate(()=>load());
    await page.locator('#projectFilter').selectOption('项目甲');
    await page.locator('#selectVisible').check();
    await page.locator('#tabs [data-view="review"]').click();
    await page.locator('#search').fill('Synthetic b');
    await page.evaluate(()=>{window.confirm=()=>{throw Error('Native dialog must not be used');};});
    await page.locator('#verifySelected').click();
    await page.locator('#cancelVerify').click();
    await page.waitForFunction(()=>!batchVerifyBusy);assert.deepEqual(transitions,[]);
    await page.locator('#verifySelected').click();
    const prompt=await page.locator('#verifyScope').innerText();
    await page.locator('#confirmVerify').click();
    await page.waitForFunction(()=>!batchVerifyBusy);
    assert.match(prompt,/不在当前列表中的 1 笔/);
    assert.deepEqual(transitions,[{id:'a',action:'verify'},{id:'b',action:'verify'}]);
    assert.match(await page.locator('#notice').innerText(),/成功 1 笔，跳过 2 笔，失败 1 笔/);
    assert.equal(items.find(x=>x.id==='b').stage,'draft');
    assert.equal(items.find(x=>x.id==='d').stage,'reimbursed');
    assert.equal(items.find(x=>x.id==='e').verified,false);
    await page.locator('#verifySelected').click();
    await page.locator('#confirmVerify').click();await page.waitForFunction(()=>!batchVerifyBusy);
    assert.equal(transitions.length,3);assert.equal(transitions[2].id,'a');
    assert.match(await page.locator('#notice').innerText(),/成功 1 笔，跳过 3 笔，失败 0 笔/);
    await page.locator('#verifySelected').click();await page.waitForFunction(()=>!batchVerifyBusy);
    assert.equal(transitions.length,3);
    assert.match(await page.locator('#notice').innerText(),/成功 0 笔，跳过 4 笔/);
    await page.locator('#search').fill('');
    await page.locator('#tabs [data-view="ready"]').click();
    assert.equal(await page.locator('#verifySelected').isVisible(),false);
    assert.equal(await page.locator('#exportSelected').isVisible(),true);
    await page.locator('[data-item="b"] .title').click();
    assert.deepEqual(await page.locator('[data-action]').evaluateAll(xs=>xs.map(x=>x.dataset.action)),['submitted']);
    items.find(x=>x.id==='b').stage='submitted';await page.evaluate(()=>load());
    await page.locator('#tabs [data-view="submitted"]').click();
    assert.equal(await page.locator('#exportSelected').isVisible(),false);
    assert.deepEqual(await page.locator('[data-action]').evaluateAll(xs=>xs.map(x=>x.dataset.action)),['reimbursed','draft']);
    await page.locator('#tabs [data-view="all"]').click();
    assert.equal(await page.locator('#verifySelected').isVisible(),false);
    assert.equal(await page.locator('#exportSelected').isVisible(),false);
    assert.deepEqual(errors,[]);
    return 'passed: selection/filter/export/mobile; batch verify confirmation/cancel, hidden selections, skip invalid, partial failure and retry without re-verification';
  }finally{await browser.close();}
}
module.exports=run;
if(require.main===module)run(require('playwright')).then(console.log).catch(e=>{console.error(e);process.exitCode=1;});
