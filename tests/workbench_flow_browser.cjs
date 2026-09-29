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
   if(u.pathname==='/api/review/confirm'){
    assert.equal(data.version,state.items[0].version);correctionCount++;
    Object.assign(state.items[0],{title:data.title,amount:data.amount,amount_cents:Math.round(Number(data.amount)*100),version:'v2',confirmation:{face_amount_cents:24000,payments_cents:[],reason:data.reason}});return json(state.items[0]);
   }
   if(u.pathname==='/api/projects/move'){assert.equal(data.versions.receipt,'v2');Object.assign(state.items[0],{project:'项目乙',project_id:'p2',version:'v3'});return json({moved:1});}
   if(u.pathname==='/api/intake/receive'){
    queue.push({id:'q1',name:data.name,status:'received',revision:0,source:'upload',issues:[],fields:{},result:'已保存'});return json(queue[0]);
   }
   if(u.pathname==='/api/intake/analyze'){Object.assign(queue[0],{status:'review',revision:1,issues:['商品不明'],fields:{title:'打印与胶带',amount:'14',category:'材料采购'},role:'purchase',text:'收据14元',result:'待确认'});return json(queue[0]);}
   if(u.pathname==='/api/intake/resolve'){assert.equal(data.item_id,'receipt');assert.equal(data.expected_version,'v3');Object.assign(queue[0],{status:'linked',item_id:'receipt',issues:[],result:'补充材料'});return json(queue[0]);}
   if(u.pathname==='/api/preferences/save')return json({...data,id:'rule1'});
   if(u.pathname==='/api/logistics/projects'){const project={id:'p3',name:data.name};state.projects.push(project);return json(project);}
   return json({});
  }
  if(u.pathname==='/api/state')return json(state);
  if(u.pathname==='/api/intake')return json({queue});
  if(u.pathname==='/api/preferences')return json({rules:[],learned:[],sections:{dining:'现场饮用水、夜宵'}});
  if(u.pathname==='/api/logistics')return json({projects:state.projects});
  if(u.pathname.startsWith('/intake-file/'))return route.fulfill({contentType:'text/plain',body:'synthetic receipt'});
  const name=u.pathname==='/'?'index.html':u.pathname.slice(1);
  if(!/^[a-z-]+\.(html|js|css)$/.test(name))return route.abort();
  return route.fulfill({contentType:name.endsWith('.js')?'text/javascript':name.endsWith('.css')?'text/css':'text/html',body:await fs.readFile(path.resolve(__dirname,'../src/invoice_print_layout/web',name))});
 });
 try{
  await page.goto('http://127.0.0.1:18771/');
  await page.getByRole('button',{name:'新建项目',exact:true}).click();
  await page.getByRole('textbox',{name:'项目名称',exact:true}).fill('新增验收项目');
  await page.getByRole('button',{name:'创建项目',exact:true}).click();
  await page.locator('#projectFilter').filter({has:page.locator('option:checked',{hasText:'新增验收项目'})}).waitFor();
  assert.ok(state.projects.some(p=>p.name==='新增验收项目'));
  await page.locator('#projectFilter').selectOption('');await page.locator('[data-item="receipt"]').click();
  await page.getByRole('button',{name:'确认当前费用金额／分笔付款'}).click();
  const confirm=page.locator('dialog[open]');await confirm.locator('[name="title"]').fill('香烟');await confirm.locator('[name="amount"]').fill('239');await confirm.locator('[name="reason"]').fill('收据误写240，实际支付239');await confirm.getByRole('button',{name:'保存本笔确认'}).click();
  await page.locator('[data-check="receipt"]').check();await page.getByRole('button',{name:'移动勾选事项到项目'}).click();await page.locator('dialog[open] select').selectOption('p2');await page.getByRole('button',{name:'确认移动',exact:true}).click();
  await page.getByRole('button',{name:'＋ 收图片／PDF并整理'}).click();
  await page.locator('#intakeFiles').setInputFiles({name:'receipt.png',mimeType:'image/png',buffer:Buffer.from('synthetic')});
  await page.getByRole('button',{name:'对照原件处理'}).waitFor();
  await page.locator('#intakeDialog [data-close]').click();await page.reload();
  await page.getByRole('button',{name:'收件／待处理',exact:true}).click();await page.getByRole('button',{name:'对照原件处理'}).click();
  const resolve=page.locator('dialog[open]').last();await resolve.locator('[name="item_id"]').selectOption('receipt');await resolve.getByRole('button',{name:'保存关系'}).click();await page.locator('#intakeRows').getByText('补充材料',{exact:false}).waitFor();
  assert.equal(state.items.length,1);assert.equal(state.items[0].amount_cents,23900);assert.equal(correctionCount,1);
  await page.locator('#intakeDialog [data-close]').click();await page.getByRole('button',{name:'经验管理',exact:true}).click();
  const rules=page.locator('dialog[open]');await rules.locator('[name="match"]').selectOption('receipt');await rules.locator('[name="value"]').selectOption('材料采购');await rules.getByRole('button',{name:'保存规则',exact:true}).click();
  await page.screenshot({path:'workspace/implementation/workbench-desktop.png',fullPage:true});
  await page.setViewportSize({width:390,height:844});await page.screenshot({path:'workspace/implementation/workbench-mobile.png',fullPage:true});
  assert.deepEqual(errors,[]);assert.ok(writes.some(x=>x.path==='/api/preferences/save'));
  return {passed:true,scope:'simulated HTTP frontend only',manualCorrections:correctionCount,repeatedQuestions:0,liveEndToEnd:'not run'};
 }finally{await browser.close();}
}
module.exports=run;
if(require.main===module)run(require('playwright')).then(console.log).catch(e=>{console.error(e);process.exitCode=1;});
