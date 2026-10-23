// Synthetic read-only goods records in the original workbench, never real ledger data.
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const crypto = require('node:crypto');

async function run(playwright) {
  const web = path.resolve(__dirname, '../src/invoice_print_layout/web');
  const evidence = path.resolve(__dirname, '../workspace/concise-expense-names-20261022/ui-tests');
  await fs.mkdir(evidence, {recursive:true});
  const browser = await playwright.chromium.launch({headless:true});
  const page = await browser.newPage({viewport:{width:1280,height:900}});
  const origin='http://127.0.0.1:18992', errors=[], posts=[];
  const longName='透明防水标识材料'.repeat(90)+'完整名称末尾';
  const goods=[
    {name:'雨衣',raw_name:'一次性塑料雨衣成人款透明防水小号',quote:'一次性塑料雨衣\n33件\nx33',material_id:'receipt-one'},
    {name:'雨衣',raw_name:'雨衣（另一份原件）',quote:'商品说明：雨衣',material_id:'receipt-two'},
    {name:longName,raw_name:'完整原始型号USB B8-3-3\n'+longName,quote:'跨行商品原文\n'+longName+'\n引用末尾',material_id:'long-original'},
    {name:'<img src=x onerror="window.goodsExecuted=1">',raw_name:'<svg onload="window.goodsExecuted=2">',
      quote:'<script>window.goodsExecuted=3</script>\n原文尾',material_id:'../原件?x="<img>'},
  ];
  const fee=(id,title,extra={})=>({id,title,project:'测试项目',project_id:'p1',category:'材料采购',stage:'draft',
    project_type:'materials',project_name:title,complete:true,verified:false,ready:false,amount_cents:2870,
    missing:[],attachments:[],events:[],version:id+'-version',...extra});
  const items=[fee('goods','雨衣采购',{agent_content:{status:'ready',goods}}),fee('manual','人工录入费用')];
  const originalGoods=JSON.stringify(goods);
  const scripts=new Set(['workbench-theme.js','app.js','review-edit.js']);
  page.on('pageerror',reason=>errors.push(reason.message));
  await page.route(origin+'/**',async route=>{
    const request=route.request(),url=new URL(request.url());
    const json=value=>route.fulfill({contentType:'application/json',body:JSON.stringify(value)});
    if(url.pathname==='/api/state') return json({app_mode:'invoice',logistics_available:false,
      projects:[{id:'p1',name:'测试项目'}],items,inbox:[],categories:['材料采购'],roles:{invoice:'发票',payment:'付款截图'},
      stages:{draft:'待核对'},token:'synthetic',report_template_ready:true,
      report_types:[{code:'materials',label:'现场购材料',names:[],editable_name:true}]});
    if(request.method()==='POST'){
      posts.push({path:url.pathname,body:request.postDataJSON()});
      assert.equal(url.pathname,'/api/expense-print-preview','Goods display must never write or request model work');
      const item=items.find(x=>x.id===posts.at(-1).body.id);assert.ok(item);
      return json({id:item.id,version:item.version,complete:true,missing:[],preview_pages:[],layout:'A4',page_count:0});
    }
    if(url.pathname.startsWith('/intake-preview/'))return route.fulfill({contentType:'text/html',body:'<p>完整原件</p>'});
    const name=url.pathname==='/'?'index.html':url.pathname.slice(1);
    if(name.includes('/') || !(name==='index.html'||name.endsWith('.css')||scripts.has(name)))return route.abort();
    return route.fulfill({contentType:name.endsWith('.css')?'text/css':name.endsWith('.js')?'text/javascript':'text/html',
      body:await fs.readFile(path.join(web,name))});
  });
  try {
    await page.goto(origin+'/?project=p1');
    await page.locator('[data-item="goods"] .title').click();
    const details=page.locator('#detail .expense-goods');
    assert.equal(await details.getAttribute('open'),null,'Product information stays collapsed beside the concise title');
    assert.equal(await page.locator('#detail .print-heading h2').innerText(),'雨衣采购');
    assert.equal(await page.locator('#editReviewFields').isEnabled(),true);
    assert.equal(await page.locator('#verifyNext').isEnabled(),true);
    await details.locator('summary').click();
    assert.equal(await details.locator('.expense-good').count(),goods.length,'Different sources with identical goods names must remain distinct');
    const readRows=await details.locator('.expense-good').evaluateAll(rows=>rows.map(row=>({
      values:[...row.querySelectorAll('dd')].map(value=>value.textContent),href:row.querySelector('a').getAttribute('href')})));
    for(const [index,good] of goods.entries()){
      assert.deepEqual(readRows[index].values,[good.name,good.raw_name,good.quote],'Every full field and line break survives the readonly view');
      assert.equal(readRows[index].href,'/intake-preview/'+encodeURIComponent(good.material_id));
    }
    assert.equal(await details.locator('img,script,svg').count(),0,'Material text is rendered as text, not executable markup');
    assert.equal(await page.evaluate(()=>window.goodsExecuted),undefined);
    assert.match(await details.innerText(),/数量、规格及未识别内容请对照完整原件/);
    assert.doesNotMatch(await details.innerText(),/SHA|job|内部工具|共\s*4\s*件/);
    const previewRequests=posts.length;
    await details.evaluate(element=>{window.goodsDetailsNode=element;});
    await page.evaluate(()=>renderDetail());
    assert.equal(await details.evaluate(element=>element===window.goodsDetailsNode&&element.open),true,'Unchanged state keeps the expanded element without rebuilding a long detail');
    assert.equal(posts.length,previewRequests,'Read-only detail reuse does not repeat print work');
    assert.equal(await page.evaluate(()=>JSON.stringify(state.items.find(x=>x.id==='goods').agent_content.goods)),originalGoods);
    await page.screenshot({path:path.join(evidence,'goods-desktop.png'),fullPage:true});

    await page.locator('#search').fill('透明防水小号');
    assert.equal(await page.locator('#list [data-item="goods"]').count(),1,'Raw product detail remains searchable after shortening the title');
    assert.equal(await page.locator('#list [data-item="manual"]').count(),0);
    await page.locator('#search').fill('USB B8-3-3');
    assert.equal(await page.locator('#list [data-item="goods"]').count(),1,'Original model tokens remain searchable');
    await page.locator('#search').fill('');
    await page.locator('[data-item="manual"] .title').click();
    assert.equal(await page.locator('#detail .expense-goods').count(),0,'Ordinary manual expenses keep their original detail');
    await page.locator('#editReviewFields').click();
    await page.locator('#reviewEditDialog[open]').waitFor();
    await page.locator('#reviewEditDialog [data-close]').click();
    assert.equal(posts.filter(p=>p.path!=='/api/expense-print-preview').length,0);

    await page.setViewportSize({width:390,height:844});
    await page.locator('[data-item="goods"] .title').click();
    await page.locator('#detail .expense-goods summary').click();
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),true,'Long goods and quotes cannot overflow 390px');
    const widths=await page.locator('#detail .expense-good dd').evaluateAll(nodes=>nodes.map(node=>({scroll:node.scrollWidth,width:node.clientWidth})));
    assert.ok(widths.every(value=>value.scroll<=value.width+1),'Full text wraps instead of covering manual buttons');
    const edit=page.locator('#editReviewFields');await edit.scrollIntoViewIfNeeded();
    assert.equal(await edit.evaluate(button=>{const r=button.getBoundingClientRect();return document.elementFromPoint(r.x+r.width/2,r.y+r.height/2)===button;}),true);
    await edit.click();await page.locator('#reviewEditDialog[open]').waitFor();
    await page.locator('#reviewEditDialog [data-close]').click();
    await page.locator('#verifyNext').scrollIntoViewIfNeeded();
    assert.equal(await page.locator('#verifyNext').isEnabled(),true);
    await page.screenshot({path:path.join(evidence,'goods-mobile-manual-actions.png'),fullPage:true});
    assert.deepEqual(errors,[]);
    const sources={};for(const name of ['app.js','style.css'])sources[name]=crypto.createHash('sha256').update(await fs.readFile(path.join(web,name))).digest('hex');
    const result={status:'PASS',synthetic_browser_only:true,source_sha256:sources,completeFieldsAndLineBreaks:true,
      sameNameDifferentSourcesPreserved:true,noMarkupExecution:true,fixedOriginalLinkEncoding:true,rawSearch:true,
      unchangedStateNoRerender:true,noGoodsMutation:true,manualExpenseEditWorks:true,mobile390NoOverflowOrButtonObstruction:true,
      businessWrites:0,modelRequests:0,pageErrors:errors,screenshots:['goods-desktop.png','goods-mobile-manual-actions.png']};
    await fs.writeFile(path.join(evidence,'browser-verification.json'),JSON.stringify(result,null,2));return result;
  } finally {await browser.close();}
}
module.exports=run;
if(require.main===module)run(require('playwright')).then(value=>console.log(JSON.stringify(value))).catch(reason=>{console.error(reason);process.exitCode=1;});
