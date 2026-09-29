// Synthetic two-project regression. Does not touch the live ledger.
const assert=require('node:assert/strict');
const fs=require('node:fs/promises');
const path=require('node:path');
async function run(pw){
 const browser=await pw.chromium.launch({headless:true});
 const page=await browser.newPage({viewport:{width:1500,height:1000}}),errors=[],writes=[];
 const base='http://127.0.0.1:18772',snapshotRequests=[];
 const projects=[{id:'p1',name:'项目甲'},{id:'p2',name:'项目乙'}];
 const item=(id,p)=>({id,title:id,project:p.name,project_id:p.id,category:'其他',amount:'10.00',amount_cents:1000,stage:'draft',verified:false,complete:true,ready:false,attachments:[],events:[],expense_date:'',note:'',merchant:'',order_number:'',followup_date:'',invoice_state:'not_requested',missing:[],version:'v1'});
 const state={items:[item('甲费用',projects[0]),item('乙费用',projects[1]),{...item('同名但未归属',projects[0]),project_id:''}],projects,inbox:[],categories:['其他'],roles:{purchase:'收据'},stages:{draft:'待提交'},token:'test',report_template_ready:true};
 const queue=projects.map((p,i)=>({id:'q'+i,project_id:p.id,name:p.name+'.png',status:'received',revision:0,source:'upload',issues:[],fields:{},result:'已保存'}));
 page.on('pageerror',e=>errors.push(e.message));
 await page.route(base+'/**',async route=>{
  const req=route.request(),url=new URL(req.url()),json=x=>route.fulfill({contentType:'application/json',body:JSON.stringify(x)});
  if(req.method()==='POST'){
   const data=req.postDataJSON();writes.push({path:url.pathname,data});
   if(url.pathname==='/api/create'){const x={...item(data.title,projects[0]),...data};state.items.push(x);return json(x);}
   if(url.pathname==='/api/intake/receive'){const q={...queue[0],id:'new',name:data.name,project_id:data.project_id};queue.push(q);return json(q);}
   return json({});
  }
  if(url.pathname==='/api/state')return json(state);
  if(url.pathname==='/api/logistics')return json({projects,tasks:[],outbox:[],token:'test'});
  if(url.pathname==='/api/logistics/inbox')return json({groups:[],messages:[],receiver:{status:'disconnected'}});
  if(url.pathname==='/api/report-snapshot'){snapshotRequests.push(url.searchParams.getAll('id'));return json({rows:[]});}
  if(url.pathname==='/api/intake')return json({queue});
  const name=url.pathname==='/'?'index.html':url.pathname==='/logistics'?'logistics.html':url.pathname.slice(1);
  if(!/^[a-z-]+\.(html|js|css)$/.test(name))return route.abort();
  return route.fulfill({contentType:name.endsWith('.js')?'text/javascript':name.endsWith('.css')?'text/css':'text/html',body:await fs.readFile(path.resolve(__dirname,'../src/invoice_print_layout/web',name))});
 });
 try{
  await page.goto(base+'/?project=p1&matter=乙费用');
  await page.getByRole('heading',{name:'项目甲 · 发票报销工作台',exact:true}).waitFor();
  assert.deepEqual(await page.locator('[data-item]').evaluateAll(xs=>xs.map(x=>x.dataset.item)),['甲费用']);
  assert.equal(await page.locator('#editForm').count(),0);
  assert.equal(await page.locator('#projectFilter').isDisabled(),true);
  assert.equal(await page.locator('#logisticsNav').getAttribute('href'),'/logistics?project=p1');
  await page.locator('#newItem').click();
  assert.equal(await page.locator('#createForm [name=project]').inputValue(),'项目甲');
  assert.equal(await page.locator('#createForm [name=project]').getAttribute('readonly'),'');
  await page.locator('#createForm [name=title]').fill('甲新增');
  await page.locator('#createForm [name=amount]').fill('12');
  await page.getByRole('button',{name:'保存事项',exact:true}).click();
  await page.locator('[data-item="甲新增"]').waitFor();
  assert.equal(writes.find(x=>x.path==='/api/create').data.project_id,'p1');
  assert.equal(await page.locator('[data-item="乙费用"]').count(),0);
  await page.locator('#previewReport').click();
  await page.locator('#reportPreviewDialog').waitFor();
  assert.deepEqual(snapshotRequests.at(-1).sort(),['甲新增','甲费用'].sort());
  await page.locator('#closePreview').click();
  await page.getByRole('button',{name:'收件／待处理',exact:true}).click();
  assert.equal(await page.locator('#intakeRows').getByText('项目乙.png',{exact:true}).count(),0);
  await page.locator('#intakeFiles').setInputFiles({name:'new.png',mimeType:'image/png',buffer:Buffer.from('synthetic')});
  await page.locator('#intakeRows').getByText('new.png',{exact:true}).waitFor();
  assert.equal(writes.find(x=>x.path==='/api/intake/receive').data.project_id,'p1');
  await page.locator('#intakeDialog [data-close]').click();
  await page.getByRole('link',{name:'项目乙 · 发票报销',exact:true}).click();
  await page.getByRole('heading',{name:'项目乙 · 发票报销工作台',exact:true}).waitFor();
  assert.deepEqual(await page.locator('[data-item]').evaluateAll(xs=>xs.map(x=>x.dataset.item)),['乙费用']);
  await page.getByRole('link',{name:'全部项目台账',exact:true}).click();
  await page.locator('[data-item="同名但未归属"]').waitFor();
  assert.equal(await page.locator('[data-item]').count(),4);
  await page.goto(base+'/?project=nonexistent');
  await page.getByText('项目不存在，请返回项目列表重新选择。',{exact:true}).waitFor();
  assert.equal(await page.locator('[data-item]').count(),0);
  await page.goto(base+'/logistics?project=p1&tab=invoices');
  const panel=page.frameLocator('#invoiceFrame');
  await panel.locator('[data-item="甲费用"]').waitFor();
  assert.equal(await page.locator('[data-tab=invoices]').getAttribute('aria-pressed'),'true');
  assert.equal(await panel.locator('.rail').isVisible(),false);
  assert.equal(await panel.getByRole('button',{name:'经验管理',exact:true}).isVisible(),true);
  assert.equal(await panel.getByRole('button',{name:'收件／待处理',exact:true}).isVisible(),true);
  await panel.locator('#newItem').click();
  await panel.locator('#createForm [name=title]').fill('未保存草稿');
  await page.locator('[data-project-id=p2]').click();
  await panel.locator('[data-item="乙费用"]').waitFor();
  assert.equal(await panel.locator('[data-item="甲费用"]').count(),0);
  assert.equal(await panel.locator('#createDialog').isVisible(),false);
  assert.match(page.url(),/project=p2&tab=invoices/);
  await page.reload();await panel.locator('[data-item="乙费用"]').waitFor();
  await panel.locator('#newItem').click();
  assert.equal(await panel.locator('#createForm [name=project_id]').inputValue(),'p2');
  await panel.locator('#closeDialog').click();
  await page.locator('[data-tab=groups]').click();assert.equal(await page.locator('#invoicesPanel').isVisible(),false);
  await page.locator('[data-tab=invoices]').click();await panel.locator('[data-item="乙费用"]').waitFor();
  await page.setViewportSize({width:390,height:844});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
  assert.equal(await panel.locator('html').evaluate(x=>x.scrollWidth<=x.clientWidth),true);
  await page.locator('#unassignedInbox').click();assert.equal(await page.locator('#invoicesPanel').isVisible(),false);
  assert.deepEqual(errors,[]);
  return {passed:true,scope:'synthetic two-project UI plus unified shell, dialogs, reload, tab switch and mobile'};
 }catch(error){console.error({errors,frames:await Promise.all(page.frames().map(async f=>({url:f.url(),body:await f.locator('body').innerText()})))});throw error;}finally{await browser.close();}
}
module.exports=run;
if(require.main===module)run(require('playwright')).then(console.log).catch(e=>{console.error(e);process.exitCode=1;});
