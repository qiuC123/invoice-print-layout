const assert=require('node:assert/strict');
const fs=require('node:fs/promises');
const path=require('node:path');
const {chromium}=require('playwright');
(async()=>{
 const browser=await chromium.launch({headless:true});
 const page=await browser.newPage({viewport:{width:1280,height:900}});
 const errors=[],requests=[];let fail=false,ready=true;
 page.on('pageerror',e=>errors.push(e.message));
 const item={id:'a',title:'测试采购',project:'项目甲',merchant:'测试商家',category:'材料采购',stage:'draft',complete:false,verified:false,ready:false,amount:'24.60',amount_cents:2460,missing:['发票'],attachments:[],events:[]};
 const source={subject:'<img src=x onerror=alert(1)>',sender:'mail@example.com',date:'2026-09-22'};
 await page.route('http://127.0.0.1:18766/**',async route=>{
  const url=new URL(route.request().url()),json=x=>route.fulfill({contentType:'application/json',body:JSON.stringify(x)});
  if(url.pathname==='/api/state')return json({items:[item],inbox:[],categories:['材料采购'],roles:{invoice:'发票'},stages:{draft:'待提交'},token:'synthetic',mail_search_ready:ready});
  if(url.pathname==='/api/logistics')return json({projects:[]});
  if(url.pathname==='/api/mail/search'){
   requests.push(route.request().postDataJSON());
   if(fail)return route.fulfill({status:400,contentType:'application/json',body:JSON.stringify({error:'邮箱临时不可用，请重试'})});
   return json({search_id:'s',item_id:'a',scanned:5,invoice_mails:2,warnings:['范围内有邮件未读到'],candidates:[
    {id:'c',name:'发票.pdf',status:'downloaded',source,reasons:['出现金额24.60元（仅线索）'],preview:'/mail-candidate/s/c'},
    {id:'q',name:'扫码领取发票',status:'needs_action',source,reasons:[],detail:'需要微信扫码领取',link:'https://www.fapiao.com/claim'},
   ]});
  }
  if(url.pathname==='/api/mail/associate'){requests.push(route.request().postDataJSON());return json({ok:true,duplicate:false});}
  const name=url.pathname==='/'?'index.html':url.pathname.slice(1);
  if(!['index.html','app.js','receipt.js','report.js','mail-search.js','style.css'].includes(name))return route.fulfill({status:404,body:''});
  return route.fulfill({contentType:name.endsWith('.js')?'text/javascript':name.endsWith('.css')?'text/css':'text/html',body:await fs.readFile(path.resolve(__dirname,'../src/invoice_print_layout/web',name))});
 });
 try{
  await page.goto('http://127.0.0.1:18766/');
  await page.locator('[data-item="a"]').click();
  await page.locator('#findInvoice').click();
  assert.match(await page.locator('#mailSearchMatter').innerText(),/项目甲.*24.60/);
  await page.locator('#mailSearchStart').fill('2026-09-01');
  await page.locator('#mailSearchEnd').fill('2026-09-24');
  await page.locator('#mailSearchRun').click();
  await page.getByRole('button',{name:'关联到此事项',exact:true}).waitFor();
  assert.equal(requests.length,1);assert.equal(requests[0].id,'a');
  assert.equal(requests[0].start,'2026-09-01');
  assert.equal(await page.locator('.mail-source img').count(),0);
  assert.equal(await page.getByRole('button',{name:'关联到此事项',exact:true}).count(),1);
  assert.match(await page.locator('#mailSearchStatus').innerText(),/未读到/);
  await page.getByRole('button',{name:'关联到此事项',exact:true}).click();
  assert.equal(requests.length,1);
  await page.locator('.mail-confirm').getByRole('button',{name:'取消'}).click();
  assert.equal(requests.length,1);
  await page.getByRole('button',{name:'关联到此事项',exact:true}).click();
  await page.getByRole('button',{name:'确认关联',exact:true}).click();
  await page.waitForFunction(()=>document.getElementById('mailSearchStatus').textContent.includes('已关联'));
  assert.deepEqual(requests[1],{id:'a',search_id:'s',candidate_id:'c',role:'invoice'});
  assert.equal(await page.getByRole('button',{name:'关联到此事项',exact:true}).count(),0);
  await fs.mkdir('workspace/ui-checks',{recursive:true});
  await page.screenshot({path:'workspace/ui-checks/mail-search-desktop.png'});
  await page.setViewportSize({width:390,height:844});
  assert.equal(await page.locator('#mailSearchDialog').evaluate(e=>e.scrollWidth<=e.clientWidth),true);
  await page.screenshot({path:'workspace/ui-checks/mail-search-mobile.png'});
  fail=true;await page.locator('#mailSearchRun').click();
  await page.waitForFunction(()=>document.getElementById('mailSearchStatus').textContent.includes('临时不可用'));
  assert.equal(await page.locator('.mail-candidate').count(),0);
  assert.equal(await page.locator('#mailSearchRun').isDisabled(),false);
  ready=false;await page.reload();await page.locator('[data-item="a"]').click();
  assert.equal(await page.locator('#findInvoice').count(),0);
  assert.deepEqual(errors,[]);
  console.log('mail search browser checks passed');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1);});
