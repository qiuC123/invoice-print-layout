// Frontend contract regression. HTTP responses are simulated; no live ledger or service.
const assert=require('node:assert/strict');
const fs=require('node:fs/promises');
const path=require('node:path');
async function run(pw){
 const browser=await pw.chromium.launch({headless:true});
 const page=await browser.newPage({viewport:{width:1500,height:1000}});
 const errors=[],writes=[];
 const state={items:[{id:'receipt',title:'待确认收据',project:'测试项目',project_id:'p1',category:'材料采购',amount:'240.00',amount_cents:24000,stage:'draft',verified:false,complete:true,ready:false,attachments:[],events:[],expense_date:'',note:'',merchant:'',order_number:'',followup_date:'',invoice_state:'not_requested',missing:[],version:'v1'}],projects:[{id:'p1',name:'测试项目'},{id:'p2',name:'项目乙'}],inbox:[],categories:['材料采购','餐饮','酒店','其他'],roles:{purchase:'收据',payment:'支付',invoice:'发票',other:'待分类'},stages:{draft:'待提交'},token:'test',report_template_ready:true};
 const queue=[];let correctionCount=0;
 page.on('pageerror',e=>errors.push(e.message));
 await page.route('http://127.0.0.1:18771/**',async route=>{
  const req=route.request(),u=new URL(req.url());
  const json=body=>route.fulfill({contentType:'application/json',body:JSON.stringify(body)});
  if(req.method()==='POST'){
   const data=req.postDataJSON();writes.push({path:u.pathname,data});
   if(u.pathname==='/api/expense-print-preview'){
    const item=state.items.find(x=>x.id===data.id);assert.ok(item);assert.equal(data.expected_version,item.version);
    return json({id:item.id,version:item.version,complete:item.complete,missing:item.missing,pdf:'synthetic-preview.pdf',preview_pages:[],layout:'A4',page_count:0});
   }
   if(u.pathname==='/api/review/confirm'){
    assert.equal(data.version,state.items[0].version);correctionCount++;
    Object.assign(state.items[0],{title:data.title,amount:data.amount,amount_cents:Math.round(Number(data.amount)*100),version:'v2',confirmation:{face_amount_cents:24000,payments_cents:[],reason:data.reason}});return json(state.items[0]);
   }
   if(u.pathname==='/api/projects/move'){assert.equal(data.versions.receipt,'v1');Object.assign(state.items[0],{project:'项目乙',project_id:'p2',version:'v2'});return json({moved:1});}
   if(u.pathname==='/api/intake/receive'){
    queue.push({id:'q1',name:data.name,project_id:data.project_id,status:'received',revision:0,source:'upload',issues:[],fields:{},result:'已保存'});return json(queue[0]);
   }
   if(['/api/intake/analyze','/api/intake/prepare'].includes(u.pathname)){Object.assign(queue[0],{status:'review',revision:1,issues:['商品不明'],fields:{title:'打印与胶带',amount:'14',category:'材料采购'},role:'purchase',text:'收据14元',result:'待确认'});return json(u.pathname==='/api/intake/prepare'?{entry:queue[0],converted_to_pdf:false}:queue[0]);}
   if(u.pathname==='/api/material-matching/auto')return json({matched:0});
   if(u.pathname==='/api/intake/resolve'){assert.equal(data.item_id,'receipt');assert.equal(data.expected_version,'v2');Object.assign(queue[0],{status:'linked',item_id:'receipt',issues:[],result:'补充材料'});return json(queue[0]);}
   if(u.pathname==='/api/preferences/save')return json({...data,id:'rule1'});
   if(u.pathname==='/api/projects'){const project={id:'p3',name:data.name};state.projects.push(project);return json(project);}
   return json({});
  }
  if(u.pathname==='/api/state')return json(state);
  if(u.pathname==='/api/intake')return json({queue});
  if(u.pathname==='/api/material-matching')return json({vouchers:[],payments:[],matches:[],suggestions:[],unused:[],
   other:queue.filter(q=>q.status!=='linked').map(q=>({...q,key:'q:'+q.id,amount_cents:1400,
    report_choice:{project_type:'materials',project_name:q.fields.title||''}})),
   report_types:[{code:'materials',label:'现场购材料',names:[],editable_name:true}]});
  if(u.pathname==='/api/preferences')return json({rules:[],learned:[],sections:{dining:'现场饮用水、夜宵'}});
  if(u.pathname==='/api/logistics')return json({projects:state.projects});
  if(u.pathname.startsWith('/intake-file/'))return route.fulfill({contentType:'text/plain',body:'synthetic receipt'});
  const name=u.pathname==='/'?'index.html':u.pathname.slice(1);
  if(!/^[a-z-]+\.(html|js|css)$/.test(name))return route.abort();
  return route.fulfill({contentType:name.endsWith('.js')?'text/javascript':name.endsWith('.css')?'text/css':'text/html',body:await fs.readFile(path.resolve(__dirname,'../src/invoice_print_layout/web',name))});
 });
 try{
  await page.goto('http://127.0.0.1:18771/');
  assert.equal(await page.locator('#newItem,#createDialog').count(),0);
  assert.equal(await page.getByRole('button',{name:'收件／待处理',exact:true}).count(),0,'duplicate sidebar intake entry must be removed');
  assert.equal(await page.locator('#recognizeReceipt,#checkMail').count(),0,'duplicate main-page collection entries must be removed');
  assert.equal(await page.locator('#materialMatching').isVisible(),true,'collection belongs to the matching desk');
  await page.locator('#createProject').click();
  await page.getByRole('textbox',{name:'项目名称',exact:true}).fill('新增验收项目');
  await page.getByRole('button',{name:'创建项目',exact:true}).click();
  await page.waitForURL(url=>url.searchParams.get('project')==='p3');
  assert.ok(state.projects.some(p=>p.name==='新增验收项目'));
  await page.goto('http://127.0.0.1:18771/');
  assert.equal(await page.locator('#projectFilter').count(),0);await page.locator('[data-item="receipt"]').click();
  assert.equal(await page.getByRole('button',{name:'确认当前费用金额／分笔付款'}).count(),0,'old split-payment button must be removed');
  assert.equal(await page.getByRole('button',{name:'材料合成台',exact:true}).count(),1,'matching desk remains the material-matching entry');
  await page.locator('[data-check="receipt"]').check();await page.getByRole('button',{name:'移动勾选事项到项目'}).click();await page.locator('dialog[open] select').selectOption('p2');await page.getByRole('button',{name:'确认移动',exact:true}).click();
  await page.goto('http://127.0.0.1:18771/?project=p2');
  // Keep this existing manual intake/relationship regression in manual mode.
  await page.waitForFunction(()=>!!document.querySelector('#manualAgent input[type="checkbox"]'));
  await page.locator('#manualAgent summary').click();
  await page.getByLabel('上传后自动整理',{exact:true}).uncheck();
  await page.locator('#materialMatching').click();
  assert.equal(await page.locator('#matchingProject').count(),0);
  await page.locator('#matchingFiles').setInputFiles({name:'receipt.png',mimeType:'image/png',buffer:Buffer.from('synthetic')});
  await page.locator('#matchingMessage').filter({hasText:'已保存 1 份'}).waitFor();
  assert.equal(await page.locator('[data-support-id="q1"]').count(),1,'uploaded material remains available before any fee is created');
  assert.equal(writes.filter(x=>x.path==='/api/intake/receive').at(-1).data.project_id,'p2');
  await page.locator('#matchingClose').click();await page.reload();
  // Keep the internal legacy relationship editor under regression coverage without restoring its removed product entry.
  await page.evaluate(()=>window.openUnifiedIntake());await page.getByRole('button',{name:'对照原件处理'}).click();
  const resolve=page.locator('dialog[open]').last();await resolve.locator('[name="item_id"]').selectOption('receipt');await resolve.getByRole('button',{name:'保存关系'}).click();await page.locator('#intakeRows').getByText('补充材料',{exact:false}).waitFor();
  assert.equal(state.items.length,1);assert.equal(state.items[0].amount_cents,24000);assert.equal(correctionCount,0);
  assert.ok(!writes.some(x=>x.path==='/api/review/confirm'),'normal page flow must not invoke obsolete confirmation');
  await page.locator('#intakeDialog [data-close]').click();
  assert.equal(await page.getByRole('button',{name:'经验管理',exact:true}).count(),0);
  assert.equal(await page.getByRole('heading',{name:'可管理经验',exact:true}).count(),0);
  const evidence=path.resolve(__dirname,'../workspace/invoice-operations/unified-intake-entry-20261010');
  await fs.mkdir(evidence,{recursive:true});
  await page.screenshot({path:path.join(evidence,'synthetic-workbench-desktop.png'),fullPage:true});
  await page.setViewportSize({width:390,height:844});await page.screenshot({path:path.join(evidence,'synthetic-workbench-mobile.png'),fullPage:true});
  assert.deepEqual(errors,[]);assert.ok(!writes.some(x=>x.path.startsWith('/api/preferences/')));
  return {passed:true,scope:'simulated HTTP frontend only',manualCorrections:correctionCount,repeatedQuestions:0,liveEndToEnd:'not run'};
 }finally{await browser.close();}
}
module.exports=run;
if(require.main===module)run(require('playwright')).then(console.log).catch(e=>{console.error(e);process.exitCode=1;});
