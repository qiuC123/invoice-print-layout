const assert=require('node:assert/strict'),fs=require('node:fs/promises'),path=require('node:path');
const {chromium}=require('playwright');
(async()=>{
 const browser=await chromium.launch({headless:true}),origin='http://127.0.0.1:18864';
 try{for(const failure of ['missing','server-error','network','invalid-json','null','wrong-fields']){
  const page=await browser.newPage(),errors=[];
  let mode=failure,documents=0;
  const runtime={started_at:'synthetic-start',build_id:'synthetic-build'};
  const state={app_mode:'invoice',token:'synthetic',projects:[],items:[{id:'fee',title:'合成测试费用',category:'其他',amount_cents:1234,stage:'draft',verified:false,complete:false,missing:[],attachments:[],events:[]}],inbox:[],categories:['其他'],roles:{},stages:{},category_auto_ready:false};
  page.on('pageerror',e=>errors.push(e.message));
  await page.clock.install();
  await page.route(origin+'/**',async route=>{
   const url=new URL(route.request().url()),json=value=>route.fulfill({contentType:'application/json',body:JSON.stringify(value)});
   if(url.pathname==='/api/state')return json(state);
   if(url.pathname==='/api/runtime'){
    if(mode==='missing'||mode==='server-error')return route.fulfill({status:mode==='missing'?404:503,body:''});
    if(mode==='network')return route.abort('connectionrefused');
    if(mode==='invalid-json')return route.fulfill({contentType:'application/json',body:'{'});
    return json(mode==='null'?null:mode==='wrong-fields'?{started_at:1}:runtime);
   }
   if(url.pathname==='/api/settings')return json({version:2,enabled:false,providers:[],organize:{},mail:{projects:[]}});
   if(url.pathname.startsWith('/api/'))return json({queue:[]});
   const name=url.pathname==='/'?'index.html':url.pathname.slice(1);if(name==='index.html')documents++;
   if(!/^[a-z-]+\.(html|js|css)$/.test(name))return route.abort();
   return route.fulfill({contentType:name.endsWith('.js')?'text/javascript':name.endsWith('.css')?'text/css':'text/html',body:await fs.readFile(path.resolve(__dirname,'../src/invoice_print_layout/web',name))});
  });
  await page.goto(origin);
  await page.locator('[data-item="fee"]').waitFor();
  assert.match(await page.locator('[data-item="fee"]').innerText(),/合成测试费用/);
  assert.equal(await page.locator('#error').isVisible(),false);
  assert.equal(await page.evaluate(()=>loadDisconnected),false,'runtime failure is not a state outage');
  mode='valid';await page.evaluate(()=>load());
  mode=failure;state.items[0].title='刷新后的合成费用';await page.evaluate(()=>load());
  assert.match(await page.locator('[data-item="fee"]').innerText(),/刷新后的合成费用/);
  assert.deepEqual(await page.evaluate(()=>loadedRuntime),runtime,'keep last valid restart marker');
  await page.clock.runFor(30000);
  assert.equal(documents,1,'unavailable runtime never triggers a reload');
  mode='valid';runtime.build_id='synthetic-build-new';
  await page.evaluate(()=>document.querySelector('dialog').showModal());
  await page.evaluate(()=>load());
  await page.locator('#notice button').waitFor();
  assert.match(await page.locator('#notice').innerText(),/工作台已更新/);
  assert.deepEqual(errors,[]);await page.close();
  console.log('PASS optional runtime: '+failure);
 }}finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
