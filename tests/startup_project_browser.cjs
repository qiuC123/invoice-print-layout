// Startup navigation uses intercepted synthetic data and the real page assets.
const assert=require('node:assert/strict'),fs=require('node:fs/promises'),path=require('node:path');
async function run(pw){
 const browser=await pw.chromium.launch({channel:'msedge',headless:true});
 const origin='http://127.0.0.1:18869',source=path.resolve(__dirname,'../src/invoice_print_layout/web');
 const projects=[{id:'p1',name:'合成普通项目'},{id:'p2',name:'合成置顶项目',pinned:true},{id:'p3',name:'合成历史项目',reimbursement_status:'reimbursed'}];
 const item=(p,i)=>({id:p.id+'-'+i,project_id:p.id,project:p.name,title:'合成费用'+i,stage:'draft',category:'其他',amount:'10.00',amount_cents:1000,attachments:[],events:[],missing:[],version:'v1'});
 const state={app_mode:'invoice',projects,items:projects.flatMap(p=>Array.from({length:20},(_,i)=>item(p,i))),inbox:[],categories:['其他'],roles:{},stages:{draft:'待核对'},token:'synthetic'};
 const errors=[],writes=[];
 let baseline=false,stateReads=0;
 const context=await browser.newContext({viewport:{width:1440,height:900}});
 await context.addInitScript(()=>{
  window.startupHistoryLength=history.length;window.startupHeadings=[];
  new MutationObserver(()=>{const text=document.querySelector('h1')?.textContent;if(text)window.startupHeadings.push(text);}).observe(document,{childList:true,subtree:true,characterData:true});
 });
 await context.route(origin+'/**',async route=>{
  const request=route.request(),url=new URL(request.url()),json=x=>route.fulfill({contentType:'application/json',body:JSON.stringify(x)});
  if(request.method()!=='GET'){writes.push(url.pathname);return json({});}
  if(url.pathname==='/api/state'){stateReads++;return json(state);}
  if(url.pathname==='/api/runtime')return json({started_at:'synthetic',build_id:'synthetic'});
  if(url.pathname.startsWith('/api/'))return json({queue:[],people:[],jobs:[],candidates:[],suggestions:[],vouchers:[],payments:[],other:[],matches:[],report_types:[]});
  const name=url.pathname==='/'?'index.html':url.pathname.slice(1);
  if(!/^[a-z-]+\.(html|js|css)$/.test(name))return route.abort();
  const file=baseline&&name==='app.js'?process.env.F19_BASELINE_APP:path.join(source,name);
  return route.fulfill({contentType:name.endsWith('.js')?'text/javascript':name.endsWith('.css')?'text/css':'text/html',body:await fs.readFile(file)});
 });
 const page=await context.newPage();page.setDefaultTimeout(10000);page.on('pageerror',e=>errors.push(e.message));
 const open=async suffix=>{await page.goto(origin+suffix);await page.waitForFunction(()=>typeof loadCompleted!=='undefined'&&loadCompleted);};
 const selected=()=>new URL(page.url()).searchParams.get('project');
 try{
  if(process.env.F19_BASELINE_APP){
   const measure=async old=>{
    baseline=old;const samples=[];
    for(let i=0;i<12;i++){
     const start=performance.now();await page.goto(origin+'/');await page.locator('#list [data-item]').first().waitFor();
     const elapsed=performance.now()-start;if(i>=2)samples.push(elapsed);
    }
    const sorted=[...samples].sort((a,b)=>a-b);
    return {samples_ms:samples,median_ms:(sorted[4]+sorted[5])/2,mean_ms:samples.reduce((a,b)=>a+b,0)/samples.length};
   };
   const before=await measure(true);
   await page.waitForTimeout(500);const baselineIdleError=await page.locator('#error').isVisible()?await page.locator('#error').innerText():'';
   const after=await measure(false),delta=after.median_ms-before.median_ms;
   const result={browser:'msedge',fixture:'3 projects, 60 synthetic expenses, intercepted HTTP, 2 warmups + 10 samples per version',before,after,median_delta_ms:delta,baseline_idle_error:baselineIdleError};
   if(process.env.INVOICE_BROWSER_EVIDENCE){await fs.mkdir(process.env.INVOICE_BROWSER_EVIDENCE,{recursive:true});await fs.writeFile(path.join(process.env.INVOICE_BROWSER_EVIDENCE,'startup-timing.json'),JSON.stringify(result,null,2));}
   assert.ok(delta<=300,JSON.stringify(result));return result;
  }
  await open('/');assert.equal(selected(),'p2');assert.equal(await page.locator('#list [data-item]').count(),20);
  assert.equal(stateReads,1,'startup reuses the state response');assert.equal(await page.locator('#error').isVisible(),false);
  assert.equal(await page.evaluate(()=>history.length===window.startupHistoryLength),true,'selection adds no history entry');
  assert.equal(await page.evaluate(()=>window.startupHeadings.includes('全部项目台账')),false,'the overview is never rendered during startup');
  assert.equal(await page.locator('h1').innerText(),'合成置顶项目 · 发票助手');
  assert.equal(await page.evaluate(()=>localStorage.getItem('invoice-last-project')),'p2');
  await open('/?project=p1');await open('/');assert.equal(selected(),'p1','last valid unreimbursed project wins over pin');
  projects[0].reimbursement_status='reimbursed';await open('/');assert.equal(selected(),'p2');
  await page.evaluate(()=>localStorage.setItem('invoice-last-project','deleted'));await open('/');assert.equal(selected(),'p2');
  await open('/?project=deleted');assert.equal(selected(),'p2');assert.equal(await page.locator('#error').isVisible(),false);
  await open('/?project=p3');assert.equal(selected(),'p3','explicit historical project is still accessible');
  await open('/?view=all');assert.equal(selected(),null);assert.equal(await page.locator('#list [data-item]').count(),60);
  assert.equal(await page.getByRole('link',{name:'全部项目',exact:true}).getAttribute('aria-current'),'page');
  await page.waitForTimeout(500);assert.equal(await page.locator('#error').isVisible(),false,'idle overview does not request the material desk');
  await page.locator('#materialMatching').click();assert.match(await page.locator('#error').innerText(),/请先从侧栏选择一个项目/);
  await page.locator('#projectLinks a').filter({hasText:'合成置顶项目'}).click();await page.waitForFunction(()=>typeof loadCompleted!=='undefined'&&loadCompleted);
  assert.equal(await page.getByRole('link',{name:'全部项目',exact:true}).getAttribute('aria-current'),null);
  await page.getByRole('link',{name:'全部项目',exact:true}).click();await page.getByRole('heading',{name:'全部项目台账',exact:true}).waitFor();
  projects.forEach(p=>p.reimbursement_status='reimbursed');await open('/');assert.equal(selected(),null);
  await open('/?project=deleted');assert.equal(selected(),null);assert.equal(await page.locator('#error').isVisible(),false);
  projects[1].reimbursement_status='unreimbursed';
  await page.addInitScript(()=>{Storage.prototype.getItem=()=>{throw Error('blocked');};Storage.prototype.setItem=()=>{throw Error('blocked');};});
  await open('/');assert.equal(selected(),'p2','storage denial still selects a project');
  state.app_mode='logistics';await open('/');assert.equal(selected(),null);assert.equal(await page.getByRole('link',{name:'全部项目',exact:true}).count(),0);
  assert.deepEqual(errors,[]);assert.deepEqual(writes,[]);
  return {passed:true,defaultPin:true,lastProject:true,staleRecovery:true,explicitOverview:true,idleOverviewNoError:true,storageDenied:true,nonInvoiceUnchanged:true};
 }finally{await browser.close();}
}
module.exports=run;
if(require.main===module)run(require('playwright')).then(r=>console.log(JSON.stringify(r))).catch(e=>{console.error(e);process.exitCode=1;});
