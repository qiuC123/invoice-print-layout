// Shared template order, standalone final pages, escaping and read-only scopes.
const assert=require('node:assert/strict'),fs=require('node:fs/promises'),path=require('node:path');
async function run(pw){
 const browser=await pw.chromium.launch({headless:true}),page=await browser.newPage({viewport:{width:1440,height:1000}});
 const errors=[],requests=[],frameNavigations=[],snapshotOrders=[];
 const png=Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j1ioAAAAASUVORK5CYII=','base64');
 const matter=(id,category,cents,extra={})=>({id,title:id,project_id:'p1',project:'测试项目',category,stage:'draft',expense_date:'2026-09-25',merchant:'测试商家',order_number:'',note:'',amount_cents:cents,amount:(cents/100).toFixed(2),complete:false,verified:false,ready:false,missing:['发票'],events:[],attachments:[],...extra});
 const items=[matter('material','材料采购',23900,{title:'<img src=x onerror="window.top.injected=1">',note:'</p><script>window.injected=1</script>'}),matter('printing','材料采购',1400,{attachments:[{id:'printing-proof',name:'printing-original.pdf',role:'invoice',available:true}]}),matter('meal','餐饮',1450),matter('service','其他',30000),...Array.from({length:6},(_,i)=>matter('small'+i,'材料采购',100)),matter('paid','酒店',20000,{stage:'reimbursed'}),matter('cancelled','酒店',20000,{stage:'cancelled'})];
 const before=JSON.stringify(items);
 const sharedSnapshot=values=>JSON.parse(require('node:child_process').execFileSync(path.resolve('.venv/Scripts/python.exe'),['-c','import sys,json;from invoice_print_layout.report_snapshot import snapshot;print(json.dumps(snapshot(json.load(sys.stdin))))'],{input:JSON.stringify(values),env:{...process.env,PYTHONUTF8:'1',PYTHONPATH:path.resolve('src')}}).toString());
 page.on('pageerror',e=>errors.push(e.message));
 await page.route('http://127.0.0.1:18767/**',async route=>{
  const request=route.request(),u=new URL(request.url());
  if(request.isNavigationRequest()&&request.frame()!==page.mainFrame())frameNavigations.push(u.href);
  if(request.method()==='POST'){
   assert.equal(u.pathname,'/api/report-preview','Preview cannot mutate business records');
   const ids=request.postDataJSON().ids,selected=ids.map(id=>items.find(x=>x.id===id)),snapshot=sharedSnapshot(selected);
   requests.push({path:u.pathname,ids});snapshotOrders.push({requested:ids,ordered:snapshot.ordered_item_ids});
   return route.fulfill({contentType:'application/json',body:JSON.stringify({combined_pdf:'combined.pdf',combined_preview_pages:['table.png',...snapshot.ordered_item_ids.map(id=>id+'.png')],combined_page_sizes:[{width:842,height:595},...ids.map(()=>({width:595,height:842}))]})});
  }
  if(u.pathname==='/api/state')return route.fulfill({contentType:'application/json',body:JSON.stringify({items,projects:[{id:'p1',name:'测试项目'}],inbox:[],categories:['材料采购','餐饮','酒店','其他'],roles:{payment:'支付记录'},stages:{draft:'待提交',reimbursed:'已报销',cancelled:'已取消'},report_template_ready:true,report_person:'测试人',token:'test'})});
  if(u.pathname.startsWith('/export/'))return route.fulfill({contentType:'image/png',body:png});
  const name=u.pathname==='/'?'index.html':u.pathname.slice(1);
  if(!/^[a-z-]+\.(html|js|css)$/.test(name))return route.abort();
  return route.fulfill({contentType:name.endsWith('.js')?'text/javascript':name.endsWith('.css')?'text/css':'text/html',body:await fs.readFile(path.resolve(__dirname,'../src/invoice_print_layout/web',name))});
 });
 const frame=page.frameLocator('#previewFrame');
 const ready=async(count,total)=>{await page.waitForFunction(({count,total})=>{
  const doc=document.querySelector('#previewFrame').contentDocument;
  return doc?.querySelectorAll('.print-page img').length===count+1&&document.querySelector('#previewScope').textContent.includes(total);
 },{count,total});};
 try{
  await page.goto('http://127.0.0.1:18767/?project=p1');await page.locator('#previewReport').waitFor();
  await page.locator('#previewReport').click();await ready(10,'573.50');
  assert.equal(await page.locator('#previewProject,#previewProjectToggle,#previewProjectMenu,.multi-filter[data-filter="previewProject"]').count(),0,'A project preview has no second project selector');
  assert.equal(await frame.locator('a,details,.expense,.notice,.brand,iframe').count(),0);
  assert.equal(await page.evaluate(()=>window.injected),undefined);
  const previewFrame=page.frames().find(child=>child.parentFrame());assert.equal(previewFrame.url(),'about:srcdoc');
  await frame.locator('img').first().click();assert.equal(previewFrame.url(),'about:srcdoc');assert.deepEqual(frameNavigations,[]);
  // Keep independent checks for table semantics and safe legacy snapshot consumers.
  const fallback=await page.evaluate(values=>{
   const doc=new DOMParser().parseFromString(buildReportPreviewHtml(values),'text/html');
   const cell=id=>doc.querySelector('[data-cell="'+id+'"]').textContent;
   return {tableLinks:doc.querySelectorAll('.template a,nav a').length,navigation:doc.querySelectorAll('a[href^="#"],iframe').length,
    total:cell('H35'),meal:cell('H19'),unifiedMeal:cell('H17'),other:cell('H30'),overflow:cell('H28'),overflowNames:cell('B28'),printing:cell('B25'),unsafe:doc.querySelectorAll('script,[onerror]').length};
  },items.slice(0,10));
  assert.deepEqual({...fallback,overflowNames:null,printing:null},{tableLinks:0,navigation:0,total:'573.50',meal:'14.50',unifiedMeal:'',other:'300.00',overflow:'4.00',overflowNames:null,printing:null,unsafe:0});
  assert.match(fallback.overflowNames,/small2.*small5/);assert.match(fallback.printing,/printing/);assert.doesNotMatch(fallback.overflowNames,/¥/);
  await page.locator('#previewCategory').selectOption('餐饮',{force:true});await ready(1,'14.50');
  const downloadPromise=page.waitForEvent('download');await page.locator('#downloadPreview').click();
  const download=await downloadPromise,html=await fs.readFile(await download.path(),'utf8');
  assert.equal(download.suggestedFilename(),'报销打印预览.html');
  assert.equal((html.match(/src="data:image\/png;base64,/g)||[]).length,2);
  assert.doesNotMatch(html,/href=["']#|<iframe|\/file\/|[?&](?:matter|project)=/);
  await page.setViewportSize({width:390,height:844});
  assert.equal(await page.locator('#reportPreviewDialog').evaluate(x=>x.scrollWidth<=x.clientWidth),true);
  assert.equal(await frame.locator('body').evaluate(x=>x.scrollWidth<=innerWidth),true);
  await page.locator('#closePreview').click();await page.evaluate(()=>{checked.add('paid');$('search').value='no-match';});
  await page.locator('#previewReport').click();await ready(1,'200.00');assert.match(await page.locator('#previewScope').innerText(),/勾选/);
  assert.equal(JSON.stringify(items),before);await page.locator('#closePreview').click();await page.setViewportSize({width:1440,height:1000});
  const mixed=[matter('hotel-a','酒店',100),matter('taxi-a','打车',200),matter('rail-a','高铁',300),matter('hotel-b','酒店',400),matter('taxi-b','打车',500),matter('rail-b','高铁',600)];
  items.splice(0,items.length,...mixed);const mixedBefore=JSON.stringify(items),inputOrder=items.map(x=>x.id);
  await page.evaluate(()=>{checked.clear();$('search').value='';});await page.locator('#previewReport').click();await ready(6,'21.00');
  const expectedOrder=['rail-a','rail-b','taxi-a','taxi-b','hotel-a','hotel-b'];
  assert.deepEqual(snapshotOrders.at(-1),{requested:inputOrder,ordered:expectedOrder});
  const imageOrder=await frame.locator('img').evaluateAll(nodes=>nodes.slice(1).map(node=>decodeURIComponent(new URL(node.src||node.dataset.pageSrc).pathname.split('/').pop()).replace(/\.png$/,'')));
  assert.deepEqual(imageOrder,expectedOrder,'Final page images follow canonical train/taxi/hotel order from the server');
  assert.deepEqual(await page.evaluate(()=>previewSnapshot.map(x=>x.id)),inputOrder,'Frozen selection order must not be mutated');
  const shared=sharedSnapshot(mixed);
  const semantics=await page.evaluate(({values,meta})=>{
   const doc=new DOMParser().parseFromString(buildReportPreviewHtml(values,meta),'text/html');
   return {names:[...doc.querySelectorAll('.expense h3')].map(node=>node.textContent),amounts:[...doc.querySelectorAll('.template td.amount.filled')].map(node=>({cell:node.dataset.cell,amount:node.textContent}))};
  },{values:mixed,meta:shared});
  assert.deepEqual(semantics,{names:expectedOrder,amounts:[{cell:'H12',amount:'9.00'},{cell:'H13',amount:'7.00'},{cell:'H16',amount:'5.00'}]});
  const readsBeforeFilter=requests.length;await page.locator('#previewCategory').selectOption('打车',{force:true});await ready(2,'7.00');
  await page.locator('#previewCategory').selectOption('',{force:true});await ready(6,'21.00');assert.equal(requests.length,readsBeforeFilter+1);
  const compatibility=await page.evaluate(values=>{
   const names=meta=>Array.from(new DOMParser().parseFromString(buildReportPreviewHtml(values,meta),'text/html').querySelectorAll('.expense h3'),node=>node.textContent);
   return {legacy:names({}),partial:names({ordered_item_ids:['unrelated-project-expense',values[1].id,values[0].id]})};
  },[mixed[0],mixed[1]]);
  assert.deepEqual(compatibility,{legacy:['hotel-a','taxi-a'],partial:['taxi-a','hotel-a']});
  assert.equal(JSON.stringify(items),mixedBefore);assert.deepEqual(errors,[]);assert.deepEqual(frameNavigations,[]);
  return 'passed: canonical backend template and final-page order; read-only selected/filter scopes and cache; standalone exact-image downloads; mapping/overflow/escaping/mobile; no link cards or nested workbench navigation';
 }finally{await browser.close();}
}
module.exports=run;
if(require.main===module)run(require('playwright')).then(console.log).catch(e=>{console.error(e);process.exitCode=1;});
