// Real status rendering, intercepted synthetic source snapshots; no live ledger.
const assert=require('node:assert/strict'),fs=require('node:fs/promises'),path=require('node:path');
async function run(pw){
 const browser=await pw.chromium.launch({headless:true}),page=await browser.newPage({viewport:{width:1450,height:1000}});
 page.setDefaultTimeout(10000);const origin='http://127.0.0.1:18828',errors=[],writes=[];
 const fee=(id,cents,match,extra={})=>({id,title:id,project_id:'p1',project:'状态核对项目',category:'高铁',stage:'draft',verified:false,ready:false,complete:true,missing:[],amount_cents:cents,amount:(cents/100).toFixed(2),version:id+'-v1',expense_date:'',attachments:[{id:id+'-invoice',role:'invoice',name:id+'.pdf',available:true}],events:[],material_match:{group_id:id+'-group',provisional:true,reason:'金额相符，归属和内容仍需人工核对。',...match},...extra});
 const partial={voucher_count:1,voucher_total_cents:3300,payment_count:0,payment_total_cents:0,alignment_status:'partial'};
 const taxiProofs=id=>[{id:id+'-invoice',role:'invoice',name:id+'.pdf',available:true},{id:id+'-trip',role:'trip',name:id+'-trip.pdf',available:true}];
 const taxiOptions=id=>({category:'打车',material_basis:'invoice_payment',attachments:taxiProofs(id)});
 const taxiMatch={...partial,voucher_total_cents:2800,support_count:1};
 const items=[fee('rail',3300,partial),fee('taxi',2800,taxiMatch,{category:'打车',complete:false,missing:['行程单']}),fee('manual-rail',3400,{...partial,alignment_status:'amount_override'}),fee('both-differ',3300,{...partial,payment_count:1,payment_total_cents:3200,alignment_status:'mismatch'}),fee('unknown',0,{...partial,voucher_total_cents:null,payment_count:1,payment_total_cents:2800,alignment_status:'unknown'},{amount_pending:true,complete:false,missing:['金额']}),fee('legacy-unknown',2800,{...partial,voucher_total_cents:null,payment_count:1,payment_total_cents:2800,alignment_status:'unconfirmed'}),fee('balanced',3300,{...partial,payment_count:1,payment_total_cents:3300,alignment_status:'balanced'}),fee('payment-only',2800,{voucher_count:0,voucher_total_cents:0,payment_count:1,payment_total_cents:2800,alignment_status:'partial'},{category:'其他',complete:false,missing:['发票'],attachments:[{id:'only-pay',role:'payment',name:'only-pay.png',available:true}]}),fee('known-zero',0,{...partial,voucher_total_cents:0,payment_count:1,payment_total_cents:0,alignment_status:'balanced'}),
 fee('taxi-complete',2800,taxiMatch,taxiOptions('taxi-complete')),
 fee('taxi-override',2900,{...taxiMatch,alignment_status:'amount_override'},taxiOptions('taxi-override')),
 fee('taxi-unknown',0,{...taxiMatch,voucher_total_cents:null,alignment_status:'unknown'},{...taxiOptions('taxi-unknown'),amount_pending:true,complete:false,missing:['金额']}),
 fee('taxi-unknown-total',2800,{...taxiMatch,voucher_total_cents:null,alignment_status:'unconfirmed'},taxiOptions('taxi-unknown-total')),
 fee('taxi-optional-payment',2800,{...taxiMatch,payment_count:1,payment_total_cents:2000,alignment_status:'balanced'},{...taxiOptions('taxi-optional-payment'),attachments:[...taxiProofs('taxi-optional-payment'),{id:'optional-pay',role:'payment',name:'optional.png',available:true}]}),
 fee('taxi-other-trip',2800,taxiMatch,{...taxiOptions('taxi-other-trip'),complete:false,missing:['行程单'],attachments:[taxiProofs('taxi-other-trip')[0],{id:'other-trip',role:'other',name:'行程单.pdf',available:true}]}),
 fee('taxi-missing-original',2800,taxiMatch,{...taxiOptions('taxi-missing-original'),complete:false,missing:['行程单'],attachments:[taxiProofs('taxi-missing-original')[0],{id:'missing-trip',role:'trip',name:'trip.pdf',available:false}]}),
 fee('taxi-legacy',2800,taxiMatch,{...taxiOptions('taxi-legacy'),legacy:true,complete:false,missing:['付款截图']}),
 fee('taxi-payment-only',2800,{voucher_count:0,voucher_total_cents:0,payment_count:1,payment_total_cents:2800,alignment_status:'partial'},{category:'打车',material_basis:'payment_only',attachments:[{id:'taxi-pay',role:'payment',name:'taxi-pay.png',available:true}]})];
 const baseline=JSON.stringify(items),state={app_mode:'invoice',token:'synthetic',projects:[{id:'p1',name:'状态核对项目'}],items,inbox:[],categories:['高铁','打车','其他'],roles:{invoice:'发票',payment:'付款截图'},stages:{draft:'待核对'},category_auto_ready:false};
 page.on('pageerror',e=>errors.push(e.message));
 await page.route(origin+'/**',async route=>{const req=route.request(),url=new URL(req.url()),json=x=>route.fulfill({contentType:'application/json',body:JSON.stringify(x)});
  if(url.pathname==='/api/state')return json(state);
  if(req.method()==='POST'){const body=req.postDataJSON();writes.push({path:url.pathname,body});assert.equal(url.pathname,'/api/expense-print-preview');return json({id:body.id,version:body.expected_version,missing:[],preview_pages:[]});}
  if(url.pathname.startsWith('/api/'))return json({people:[],summary:{rows:[],total_cents:0,expense_count:0},queue:[],vouchers:[],payments:[],suggestions:[],matches:[]});
  const file=url.pathname==='/'?'index.html':url.pathname.slice(1);if(!/^[a-z-]+\.(html|js|css)$/.test(file))return route.abort();return route.fulfill({contentType:file.endsWith('.js')?'text/javascript':file.endsWith('.css')?'text/css':'text/html',body:await fs.readFile(path.resolve(__dirname,'../src/invoice_print_layout/web',file))});
 });
 const badge= id=>page.locator(`[data-item="${id}"] .badge`),detail=async id=>{const row=page.locator(`[data-item="${id}"]`);if(!/\bselected\b/.test(await row.getAttribute('class')||''))await row.locator('.title').click();return page.locator('#detail').innerText();};
 try{
  await page.goto(origin+'/?project=p1');await badge('rail').waitFor();
  assert.equal(await badge('rail').innerText(),'发票已收到 · 待核对');const rail=await detail('rail');assert.match(rail,/高铁发票无需付款截图/);assert.match(rail,/票据合计 ¥ 33\.00/);assert.doesNotMatch(rail,/金额有差额|付款合计|金额相符|金额已对齐/);
  assert.equal(await badge('taxi').innerText(),'待核对 · 仍缺材料');const taxi=await detail('taxi');assert.match(taxi,/还缺：行程单/);assert.match(taxi,/尚未选择行程单/);assert.doesNotMatch(taxi,/还缺：付款截图|尚未选择付款截图|金额有差额|付款合计|金额相符|金额已对齐/);
  for(const id of ['manual-rail','both-differ']){assert.equal(await badge(id).innerText(),'金额有差额 · 待核对');assert.match(await detail(id),/当前报销金额.*与原匹配材料合计有差额/);}
  assert.equal(await badge('unknown').innerText(),'金额待确认 · 待核对');const unknown=await detail('unknown');assert.match(unknown,/票据合计待确认/);assert.doesNotMatch(unknown,/金额已对齐|金额相符|票据合计 ¥ 0\.00|金额有差额/);
  assert.notEqual(await badge('legacy-unknown').innerText(),'金额已对齐 · 待核对');const legacy=await detail('legacy-unknown');assert.match(legacy,/票据合计待确认/);assert.doesNotMatch(legacy,/金额已对齐|金额相符/);
  assert.equal(await badge('balanced').innerText(),'金额已对齐 · 待核对');assert.match(await detail('balanced'),/付款合计 ¥ 33\.00.*票据合计 ¥ 33\.00/s);
  assert.equal(await badge('payment-only').innerText(),'待核对 · 仍缺材料');assert.doesNotMatch(await detail('payment-only'),/金额有差额|票据合计|金额相符|金额已对齐/);
  assert.equal(await badge('known-zero').innerText(),'金额已对齐 · 待核对');
  for(const id of ['taxi-complete','taxi-optional-payment','taxi-unknown-total']){
   assert.equal(await badge(id).innerText(),'发票＋行程单齐全 · 待核对');const content=await detail(id);
   assert.match(content,/已收 1 份行程单/);assert.match(content,/发票＋行程单已齐全，无需付款截图/);
   assert.doesNotMatch(content,/尚未选择付款截图|金额有差额|金额相符|金额已对齐|另有 1 份补充材料|付款合计/);
  }
  assert.match(await detail('taxi-unknown-total'),/票据合计待确认/);
  assert.equal(await badge('taxi-override').innerText(),'金额有差额 · 待核对');assert.match(await detail('taxi-override'),/当前报销金额 ¥ 29\.00 与原匹配材料合计有差额/);
  assert.equal(await badge('taxi-unknown').innerText(),'金额待确认 · 待核对');assert.doesNotMatch(await detail('taxi-unknown'),/金额相符|金额已对齐|票据合计 ¥ 0\.00/);
  for(const id of ['taxi-other-trip','taxi-missing-original']){assert.equal(await badge(id).innerText(),'待核对 · 仍缺材料');const content=await detail(id);assert.match(content,/尚未选择行程单/);assert.doesNotMatch(content,/发票＋行程单已齐全|尚未选择付款截图/);}
  assert.equal(await badge('taxi-legacy').innerText(),'待核对 · 仍缺材料');const oldTaxi=await detail('taxi-legacy');assert.match(oldTaxi,/还缺：付款截图/);assert.match(oldTaxi,/尚未选择付款截图/);assert.doesNotMatch(oldTaxi,/发票＋行程单已齐全|无需付款截图/);
  assert.equal(await badge('taxi-payment-only').innerText(),'材料齐全 · 待核对');assert.doesNotMatch(await detail('taxi-payment-only'),/尚未选择行程单|发票＋行程单已齐全|无需付款截图/);
  assert.equal(JSON.stringify(items),baseline);assert.deepEqual(errors,[]);assert.ok(writes.every(x=>x.path==='/api/expense-print-preview'));
  return {passed:true,cases:items.length,scope:'rail, native-role invoice+trip taxi, missing and invalid trip, override and unknown amounts, optional payment, legacy and explicit payment-only exclusions; source snapshots unchanged'};
 }finally{await browser.close();}
}
module.exports=run;if(require.main===module)run(require('playwright')).then(console.log).catch(e=>{console.error(e);process.exitCode=1;});
