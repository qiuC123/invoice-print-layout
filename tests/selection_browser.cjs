// Synthetic workbench data only. Run: node tests/selection_browser.cjs
const assert=require('node:assert/strict');
const fs=require('node:fs/promises');
const path=require('node:path');
async function run(playwright){
  const browser=await playwright.chromium.launch({headless:true});
  const page=await browser.newPage({viewport:{width:1280,height:900}});
  const errors=[],exports=[],transitions=[];let failVerify=true;
  page.on('pageerror',e=>errors.push(e.message));
  const projects=[{id:'p1',name:'项目甲'},{id:'p2',name:'项目乙'}];
  const item=(id,project,category,stage='draft')=>({id,title:'Synthetic '+id,project,project_id:projects.find(p=>p.name===project).id,category,stage,complete:true,verified:false,ready:false,amount_cents:1000,missing:[],attachments:[],events:[]});
  const items=[item('a','项目甲','材料采购'),item('b','项目甲','外卖'),item('c','项目乙','材料采购'),item('d','项目甲','材料采购','reimbursed')];
  await page.route('http://127.0.0.1:18766/**',async route=>{
    const url=new URL(route.request().url());
    const json=x=>route.fulfill({contentType:'application/json',body:JSON.stringify(x)});
    if(url.pathname==='/api/state')return json({items,projects,inbox:[],categories:['材料采购','外卖'],roles:{},stages:{reimbursed:'已报销'},token:'synthetic',report_template_ready:true});
    if(url.pathname==='/api/expense-print-preview'){
      const body=route.request().postDataJSON(),x=items.find(x=>x.id===body.id);assert.ok(x);assert.equal(body.expected_version,x.version);
      return json({id:x.id,version:x.version,complete:x.complete,missing:x.missing,pdf:'synthetic-preview.pdf',preview_pages:[],layout:'A4',page_count:0});
    }
    if(url.pathname==='/api/export'){exports.push(route.request().postDataJSON().ids);return json({pdf:'synthetic.pdf',md:'synthetic.md'});}
    if(url.pathname==='/api/transition'){
      const body=route.request().postDataJSON();transitions.push(body);
      if(body.id==='a'&&failVerify){failVerify=false;return route.fulfill({status:400,contentType:'application/json',body:JSON.stringify({error:'synthetic failure'})});}
      const x=items.find(x=>x.id===body.id);
      if(!x.complete){assert.equal(body.acknowledge_missing,true);assert.deepEqual(body.expected_missing,x.missing);x.missing_acknowledged=true;}
      x.verified=true;x.ready=true;return json(x);
    }
    const name=url.pathname==='/'?'index.html':url.pathname.slice(1);
    if(!['index.html','workbench-theme.js','workbench-theme.css','multi-filter.js','multi-filter.css','report-preview.css','app.js','project-menu.js','project-menu.css','receipt.js','report.js','style.css'].includes(name))return route.abort();
    return route.fulfill({contentType:name.endsWith('.js')?'text/javascript':name.endsWith('.css')?'text/css':'text/html',body:await fs.readFile(path.resolve(__dirname,'../src/invoice_print_layout/web',name))});
  });
  const selected=()=>page.evaluate(()=>[...checked].sort());
  try{
    await page.goto('http://127.0.0.1:18766/?project=p1');
    await page.waitForSelector('[data-check="a"]');
    assert.equal(await page.locator('#clearSelected').count(),0,'Expense stages omit the removed clear-selection button');
    assert.equal(await page.locator('#exportSelected').isDisabled(),true);
    assert.equal(await page.locator('#projectFilter').count(),0);
    await page.locator('#tabs [data-view="submitted"]').click();await page.locator('[data-check="d"]').check();
    await page.locator('#tabs [data-view="review"]').click();
    await page.locator('#selectVisible').check();assert.deepEqual(await selected(),['a','b','d']);
    await page.locator('[data-check="b"]').uncheck();
    assert.equal(await page.locator('#selectVisible').evaluate(el=>el.indeterminate),true);
    await page.locator('#selectVisible').check();assert.deepEqual(await selected(),['a','b','d']);
    await page.locator('#categoryFilter').selectOption('外卖',{force:true});
    assert.match(await page.locator('#listSummary').innerText(),/其中 2 笔不在当前列表/);
    await page.locator('#selectVisible').uncheck();assert.deepEqual(await selected(),['a','d']);
    await page.locator('#categoryFilter').selectOption('',{force:true});
    await page.locator('[data-check="a"]').uncheck();
    await page.locator('#tabs [data-view="submitted"]').click();await page.locator('[data-check="d"]').uncheck();
    assert.deepEqual(await selected(),[]);
    await page.locator('#categoryFilter').selectOption('',{force:true});
    await page.locator('#tabs [data-view="review"]').click();
    await page.locator('#selectVisible').check();assert.deepEqual(await selected(),['a','b']);
    assert.equal(await page.locator('#exportSelected').isVisible(),false);
    assert.equal(await page.locator('#verifySelected').isVisible(),true);
    await page.locator('[data-item="a"] .title').click();
    assert.deepEqual(await page.locator('[data-action]').evaluateAll(xs=>xs.map(x=>x.dataset.action)),['verify']);
    await page.locator('#selectVisible').uncheck();
    await page.locator('#search').fill('Synthetic b');
    await page.locator('#selectVisible').check();assert.deepEqual(await selected(),['b']);
    await page.locator('#search').fill('no-match');
    assert.equal(await page.locator('#selectVisible').isDisabled(),true);
    assert.equal(await page.locator('#selectVisible').isChecked(),false);
    await page.locator('#search').fill('Synthetic b');await page.locator('[data-check="b"]').uncheck();
    await page.locator('#search').fill('no-match');assert.deepEqual(await selected(),[]);
    await page.evaluate(()=>{view='inbox';render();});
    assert.equal(await page.locator('#selectionTools').isVisible(),false);
    assert.equal(await page.locator('#exportSelected').isVisible(),false);
    await page.locator('#search').fill('');
    await page.locator('#tabs [data-view="review"]').evaluate(el=>el.click());
    await page.setViewportSize({width:390,height:844});
    assert.equal(await page.locator('#selectionTools').isVisible(),true);
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
    // Mixed selection: fail one item, explicitly acknowledge missing material, skip historical expenses.
    items.push({...item('e','项目甲','材料采购'),complete:false,missing:['发票']});
    await page.evaluate(()=>load());
    assert.equal(await page.evaluate(()=>currentInvoiceProjectId()),'p1');
    await page.locator('#tabs [data-view="submitted"]').click();await page.locator('[data-check="d"]').check();
    await page.locator('#tabs [data-view="review"]').click();
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
    assert.match(prompt,/不在当前列表中的 2 笔/);
    assert.match(prompt,/Synthetic e[\s\S]*缺：发票/);
    assert.deepEqual(transitions,[{id:'a',action:'verify'},{id:'b',action:'verify'},
      {id:'e',action:'verify',acknowledge_missing:true,expected_missing:['发票']}]);
    assert.match(await page.locator('#notice').innerText(),/成功 2 笔，跳过 1 笔，失败 1 笔/);
    assert.equal(items.find(x=>x.id==='b').stage,'draft');
    assert.equal(items.find(x=>x.id==='d').stage,'reimbursed');
    assert.equal(items.find(x=>x.id==='e').verified,true);
    assert.equal(items.find(x=>x.id==='e').complete,false);
    assert.equal(items.find(x=>x.id==='e').missing_acknowledged,true);
    await page.locator('#verifySelected').click();
    await page.locator('#confirmVerify').click();await page.waitForFunction(()=>!batchVerifyBusy);
    assert.equal(transitions.length,4);assert.equal(transitions[3].id,'a');
    assert.match(await page.locator('#notice').innerText(),/成功 1 笔，跳过 1 笔，失败 0 笔/);
    await page.locator('#verifySelected').click();await page.waitForFunction(()=>!batchVerifyBusy);
    assert.equal(transitions.length,4);
    assert.match(await page.locator('#notice').innerText(),/成功 0 笔，跳过 1 笔/);
    await page.locator('#search').fill('');
    await page.locator('#tabs [data-view="review"]').click();
    assert.equal(await page.locator('[data-view="ready"]').count(),0);
    assert.equal(await page.locator('#verifySelected').isVisible(),true);
    assert.equal(await page.locator('#exportSelected').isVisible(),false);
    assert.equal(await page.locator('[data-item="a"]').count(),0);
    assert.equal(await page.locator('[data-item="b"]').count(),0);
    await page.locator('#tabs [data-view="submitted"]').click();
    assert.equal(await page.locator('[data-item="a"]').isVisible(),true);
    assert.equal(await page.locator('[data-item="b"]').isVisible(),true);
    assert.equal(await page.locator('#exportSelected').isDisabled(),true,'retained historical selection must block report production');
    await page.evaluate(()=>$('exportSelected').onclick());
    assert.match(await page.locator('#error').innerText(),/未核对|材料不齐/);
    assert.equal(await page.locator('#reportDialog[open]').count(),0);
    await page.locator('#selectVisible').uncheck();await page.locator('#selectVisible').check();
    await page.locator('[data-check="d"]').uncheck();
    await page.locator('[data-check="e"]').uncheck();
    await page.locator('#categoryFilter').selectOption('外卖',{force:true});
    await page.locator('#exportSelected').click();await page.locator('#reportDialog[open]').waitFor();
    assert.match(await page.locator('#reportScope').innerText(),/已选择 2 笔（其中 1 笔不在当前列表）/);
    await page.locator('#closeReport').click();await page.locator('#categoryFilter').selectOption('',{force:true});
    await page.locator('[data-item="b"] .title').click();
    assert.equal(await page.locator('[data-action]').count(),0);
    assert.equal(await page.locator('#makeExpenseReport').isVisible(),true);
    await page.locator('#makeExpenseReport').click();await page.locator('#reportDialog[open]').waitFor();
    assert.match(await page.locator('#reportScope').innerText(),/已选择 1 笔/);
    assert.deepEqual(await selected(),['b']);await page.locator('#closeReport').click();
    items.find(x=>x.id==='b').stage='submitted';await page.evaluate(()=>load());
    await page.locator('#tabs [data-view="submitted"]').click();
    assert.equal(await page.locator('#exportSelected').isVisible(),true);
    assert.deepEqual(await page.locator('[data-action]').evaluateAll(xs=>xs.map(x=>x.dataset.action)),['draft']);
    await page.locator('#tabs [data-view="review"]').click();
    assert.equal(await page.locator('#verifySelected').isVisible(),true);
    assert.equal(await page.locator('#exportSelected').isVisible(),false);
    for(const tab of ['all','missing','review','synthesis','deleted']){
      await page.evaluate(tab=>{view=tab;render();},tab);
      assert.equal(await page.getByRole('button',{name:'清空勾选',exact:true}).count(),0);
      assert.equal(await page.locator('#exportSelected').isVisible(),false);
      assert.equal(await page.locator('#exportSelected').isDisabled(),true);
      await page.evaluate(()=>$('exportSelected').onclick());
      assert.match(await page.locator('#error').innerText(),/已核对/);
      assert.equal(await page.locator('#reportDialog[open]').count(),0);
    }
    assert.deepEqual(errors,[]);
    assert.deepEqual(exports,[]);
    return 'passed: selection/filter/report-dialog/mobile; batch verify confirmation/cancel, hidden selections, skip invalid, partial failure and retry without re-verification; reviewed bills move to submission view';
  }finally{await browser.close();}
}
module.exports=run;
if(require.main===module)run(require('playwright')).then(console.log).catch(e=>{console.error(e);process.exitCode=1;});
