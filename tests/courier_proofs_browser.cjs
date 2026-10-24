// Real UI with synthetic local responses; no real files or ledger writes.
const assert=require('node:assert/strict');
const fs=require('node:fs/promises');
const path=require('node:path');
async function run(pw){
 const browser=await pw.chromium.launch({headless:true});
 const page=await browser.newPage({viewport:{width:1500,height:1000}});
 const origin='http://127.0.0.1:18772',errors=[],writes=[];
 const courier={id:'courier',title:'顺丰',project:'合成项目',project_id:'p1',category:'其他',
  project_type:'other',project_name:'顺丰',report_section:'other',courier_waybill_required:true,
  material_basis:'invoice_payment',amount:'43.00',amount_cents:4300,stage:'draft',verified:false,
  complete:true,ready:false,missing:[],version:'v1',events:[],expense_date:'',note:'',merchant:'',
  order_number:'',followup_date:'',invoice_state:'received',waiting_days:0,
  attachments:[{id:'bill',name:'invoice.pdf',role:'invoice',available:true},
   {id:'waybill',name:'detail.pdf',role:'detail',available:true},
   {id:'payment',name:'payment.png',role:'payment',available:true}],
  material_match:{provisional:true,voucher_count:1,voucher_total_cents:4300,payment_count:1,
   payment_total_cents:9999,support_count:1,alignment_status:'balanced'}};
 const state={app_mode:'invoice',items:[courier],projects:[{id:'p1',name:'合成项目'}],inbox:[],
  categories:['其他','顺丰'],roles:{invoice:'发票',detail:'运单明细',payment:'付款截图'},
  stages:{draft:'待核对',submitted:'可提交'},token:'synthetic-token',report_template_ready:true};
 page.on('pageerror',e=>errors.push(e.message));
 await page.route(origin+'/**',async route=>{
  const request=route.request(),url=new URL(request.url());
  const json=body=>route.fulfill({contentType:'application/json',body:JSON.stringify(body)});
  if(request.method()==='POST'){
   writes.push(url.pathname);
   assert.equal(url.pathname,'/api/expense-print-preview');
   return json({id:'courier',version:courier.version,complete:courier.complete,missing:courier.missing,
    pdf:null,preview_pages:[],page_count:0,layout:'发票＋运单明细'});
  }
  if(url.pathname==='/api/state')return json(state);
  if(url.pathname==='/api/intake')return json({queue:[]});
  if(url.pathname==='/api/preferences')return json({rules:[],learned:[],sections:{}});
  const name=url.pathname==='/'?'index.html':url.pathname.slice(1);
  if(!/^[a-z-]+\.(html|js|css)$/.test(name))return route.abort();
  return route.fulfill({contentType:name.endsWith('.js')?'text/javascript':name.endsWith('.css')?'text/css':'text/html',
   body:await fs.readFile(path.resolve(__dirname,'../src/invoice_print_layout/web',name))});
 });
 try{
  await page.goto(origin+'/?project=p1');
  await page.locator('[data-item="courier"]').waitFor();
  assert.ok((await page.locator('[data-item="courier"]').innerText()).includes('发票＋运单明细齐全'));
  assert.ok(!(await page.locator('[data-item="courier"]').innerText()).includes('金额有差额'),
   'Mistaken payment amount must not be a courier invoice/waybill discrepancy');
  await page.locator('[data-item="courier"]').click();
  const preview=page.locator('#detail');
  assert.ok((await preview.innerText()).includes('已收 1 份运单明细'));
  assert.ok((await preview.innerText()).includes('无需付款截图'));
  assert.ok((await preview.innerText()).includes('付款截图保留为附件'));
  assert.ok(!(await preview.innerText()).includes('另有 1 份补充材料'), 'Waybill must not be counted twice');
  assert.ok(!(await preview.innerText()).includes('付款合计 ¥ 99.99'));
  await page.locator('#themeSelect').selectOption('dark',{force:true});
  assert.ok((await preview.innerText()).includes('发票＋运单明细已齐全'));
  courier.attachments=courier.attachments.filter(a=>a.role!=='detail');
  Object.assign(courier,{complete:false,pending_review:true,review_requested:true,missing:['运单明细'],version:'v2'});
  await page.reload();
  await page.locator('[data-item="courier"]').click();
  assert.ok((await preview.innerText()).includes('尚未选择运单明细'));
  assert.ok(!(await preview.innerText()).includes('发票＋运单明细已齐全'));
  assert.ok((await page.locator('[data-item="courier"]').innerText()).includes('仍缺材料'));
  assert.ok(writes.every(name=>name==='/api/expense-print-preview'));
  assert.deepEqual(errors,[]);
 }finally{await browser.close();}
 return {cases:4,status:'passed'};
}
if(require.main===module)run(require('playwright')).then(result=>console.log(JSON.stringify(result))).catch(error=>{console.error(error);process.exit(1);});
module.exports={run};
