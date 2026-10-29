// Real page scripts and isolated synthetic service generations.
const assert=require('node:assert/strict'),fs=require('node:fs/promises'),path=require('node:path');
async function run(pw){
 const browser=await pw.chromium.launch({headless:true}),origin='http://127.0.0.1:18863',results=[];
 try{for(const scenario of ['initial','restart','build','dialog','typing','same-service']){
  const page=await browser.newPage();let reads=0,documents=0,offline=scenario==='initial',writes=0;
  let runtime={started_at:'synthetic-start-1',build_id:'synthetic-build-1'};const errors=[];
  const state={app_mode:'invoice',token:'synthetic',projects:[],items:[{id:'synthetic-fee',title:'Recovered synthetic expense',category:'其他',amount_cents:1000,stage:'draft',verified:false,complete:false,missing:[],attachments:[],events:[]}],inbox:[],categories:['其他'],roles:{},stages:{},category_auto_ready:false};
  await page.clock.install();
  await page.addInitScript(()=>{window.testHidden=false;Object.defineProperty(document,'hidden',{get:()=>window.testHidden});});
  page.on('pageerror',error=>errors.push(error.message));
  await page.route(origin+'/**',async route=>{
   const req=route.request(),url=new URL(req.url());const json=value=>route.fulfill({contentType:'application/json',body:JSON.stringify(value)});
   if(url.pathname==='/api/state'){reads++;if(offline)return route.abort('connectionrefused');return json(state);}
   if(url.pathname==='/api/runtime'){if(offline)return route.abort('connectionrefused');return json(runtime);}
   if(url.pathname==='/api/settings')return json({version:2,enabled:false,providers:[],organize:{},mail:{projects:[]}});
   if(url.pathname.startsWith('/api/')){if(req.method()!=='GET'){writes++;return route.abort('connectionrefused');}return json({queue:[]});}
   const name=url.pathname==='/'?'index.html':url.pathname.slice(1);if(name==='index.html')documents++;
   if(!/^[a-z-]+\.(html|js|css)$/.test(name))return route.abort();
   return route.fulfill({contentType:name.endsWith('.js')?'text/javascript':name.endsWith('.css')?'text/css':'text/html',body:await fs.readFile(path.resolve(__dirname,'../src/invoice_print_layout/web',name))});
  });
  await page.goto(origin);
  if(scenario==='initial'){
   await page.locator('#error').waitFor();offline=false;
   await page.clock.runFor(3000);await page.waitForFunction(()=>typeof loadCompleted!=='undefined'&&loadCompleted);
   assert.equal(documents,2,'first recovery reloads all scripts');
   await page.locator('#settingsOpen').click();await page.locator('#settingsDialog[open]').waitFor();
  }else{
   await page.waitForFunction(()=>typeof loadCompleted!=='undefined'&&loadCompleted);
   if(scenario==='same-service'){
    offline=true;await page.evaluate(()=>Promise.allSettled([load(),load()]));const failedReads=reads;
    await page.evaluate(()=>{window.testHidden=true;document.dispatchEvent(new Event('visibilitychange'));});
    await page.clock.runFor(9000);assert.equal(reads,failedReads,'hidden page pauses retries');
    offline=false;await page.evaluate(()=>{window.testHidden=false;document.dispatchEvent(new Event('visibilitychange'));});
    await page.clock.runFor(3000);await page.waitForFunction(()=>!loadDisconnected);
    assert.equal(documents,1,'same service only reloads data');assert.equal(reads,failedReads+1);
    await page.evaluate(()=>act(()=>api('projects/move',{ids:['synthetic-fee'],project_id:'synthetic'})));
    await page.clock.runFor(9000);assert.equal(writes,1,'writes never replay');
   }else{
    if(scenario==='dialog')await page.evaluate(()=>{document.querySelector('dialog').showModal();});
    if(scenario==='typing')await page.locator('#search').focus();
    runtime={...runtime,...(scenario==='build'?{build_id:'synthetic-build-2'}:{started_at:'synthetic-start-2'})};
    if(scenario==='restart'||scenario==='build'){
     const navigation=page.waitForEvent('framenavigated', {predicate:frame=>frame===page.mainFrame()});
     await page.clock.runFor(30000);await navigation;await page.waitForFunction(()=>typeof loadCompleted!=='undefined'&&loadCompleted);assert.equal(documents,2);
    }else{
     await page.evaluate(()=>window.dispatchEvent(new Event('focus')));
     await page.locator('#notice button').waitFor();assert.equal(documents,1);
     assert.match(await page.locator('#notice').innerText(),/工作台已更新，请刷新页面/);
     await page.evaluate(()=>document.querySelectorAll('dialog[open]').forEach(d=>d.close()));
     const navigation=page.waitForEvent('framenavigated', {predicate:frame=>frame===page.mainFrame()});
     await page.locator('#notice button').click();await navigation;await page.waitForFunction(()=>typeof loadCompleted!=='undefined'&&loadCompleted);assert.equal(documents,2);
    }
   }
  }
  assert.deepEqual(errors,[]);results.push({scenario,documents,reads,writes});await page.close();
 }return {passed:true,scenarios:results};}finally{await browser.close();}
}
module.exports=run;
if(require.main===module)run(require('playwright')).then(result=>console.log(JSON.stringify(result))).catch(error=>{console.error(error);process.exitCode=1;});
