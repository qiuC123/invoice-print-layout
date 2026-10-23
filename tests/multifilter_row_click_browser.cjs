// Real shared filter resources, synthetic data only. Pointer holds reproduce a normal
// press/release gap so a blur timer cannot silently remove the label before click.
const assert=require('node:assert/strict'),fs=require('node:fs/promises'),path=require('node:path');

async function run(pw){
 const evidence=path.resolve(process.env.INVOICE_BROWSER_EVIDENCE||path.join(__dirname,'../workspace/invoice-operations/filter-row-hit-area-20261017'));
 const baseline=process.env.INVOICE_MULTIFILTER_BASELINE==='1';
 const web=path.resolve(__dirname,'../src/invoice_print_layout/web');
 const browser=await pw.chromium.launch({headless:true}),page=await browser.newPage({viewport:{width:1280,height:900}});
 page.setDefaultTimeout(5000);
 const origin='http://127.0.0.1:18882',errors=[],cases=[],failures=[],requests=[];
 const ids=['categoryFilter','personFilter','issueFilter','previewCategory'];
 const previewCalls=[];
 const png=Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j1ioAAAAASUVORK5CYII=','base64');
 const fee=(id,category)=>({id,title:'Synthetic '+id,project_id:'fixture',project:'Synthetic project',category,reimbursement_person_id:'person-'+id,reimbursement_person_label:'Person '+id,amount_cents:100,amount:'1.00',stage:'draft',verified:false,ready:false,complete:true,missing:[],version:'v1-'+id,expense_date:'2026-10-17',merchant:'',order_number:'',note:'',attachments:[],events:[]});
 const fullItems=[fee('hotel','酒店'),fee('rail','高铁')];
 const names={categoryFilter:'类别筛选',personFilter:'人员筛选',issueFilter:'问题筛选',previewCategory:'报销预览类别筛选'};
 const options={categoryFilter:['酒店','高铁','打车'],personFilter:['人员甲','人员乙','未指定'],issueFilter:['缺材料','金额有差额','材料已齐'],previewCategory:['酒店','高铁','材料采购']};
 const html='<!doctype html><html lang="zh-CN" class="invoice-assistant"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'+
  '<script src="/workbench-theme.js"></script><link rel="stylesheet" href="/style.css"><link rel="stylesheet" href="/workbench-theme.css"><link rel="stylesheet" href="/multi-filter.css">'+
  '<style>body{display:block;padding:24px;margin:0}main{padding:0;margin:0;width:100%;max-width:100%}.fixture-controls{display:flex;flex-wrap:wrap;gap:12px}.fixture-control{padding-bottom:360px}#outside{position:fixed;right:20px;bottom:20px}h1{margin:0 0 18px}@media(max-width:500px){body{padding:16px}.fixture-control{padding-bottom:0}.fixture-controls{flex-direction:column;gap:12px}}</style></head><body><main><h1>多选筛选交互回归</h1><select id="themeSelect"><option value="light">浅色</option><option value="dark">深色</option></select><div class="fixture-controls">'+
  ids.map(id=>'<div class="fixture-control"><select id="'+id+'" aria-label="'+names[id]+'"><option value="">全部</option>'+options[id].map((label,index)=>'<option value="'+String.fromCharCode(97+index)+'">'+label+'</option>').join('')+'<option value="disabled" disabled>不可选项目</option></select></div>').join('')+
  '</div><button id="outside" type="button">空白区域外部按钮</button></main><script src="/multi-filter.js"></script><script>invoiceMultiFilters.attach("previewCategory");window.changeCounts={};for(const id of '+JSON.stringify(ids)+'){changeCounts[id]=0;document.getElementById(id).addEventListener("change",()=>changeCounts[id]++);}</script></body></html>';
 page.on('pageerror',error=>errors.push(error.message));
 await page.route('**/*',async route=>{
  const url=new URL(route.request().url());requests.push(url.href);assert.equal(url.origin,origin,'The component never contacts the real workbench or an external service');
  if(url.pathname==='/')return route.fulfill({contentType:'text/html',body:html});
  if(url.pathname==='/full')return route.fulfill({contentType:'text/html',body:await fs.readFile(path.join(web,'index.html'))});
  const json=body=>route.fulfill({contentType:'application/json',body:JSON.stringify(body)});
  if(url.pathname==='/api/state')return json({app_mode:'invoice',token:'synthetic',items:fullItems,projects:[{id:'fixture',name:'Synthetic project'}],inbox:[],categories:['酒店','高铁'],roles:{},stages:{draft:'待核对'},report_template_ready:true,category_auto_ready:false});
  if(url.pathname==='/api/reimbursement-people')return json({project_id:'fixture',people:fullItems.map(item=>({id:item.reimbursement_person_id,label:item.reimbursement_person_label,project_id:'fixture',state:'active'})),summary:{rows:[]}});
  if(route.request().method()==='POST'){
   assert.equal(url.pathname,'/api/report-preview','Only the read-only, synthetic PDF preview endpoint may be called');const {ids}=route.request().postDataJSON();previewCalls.push(ids);
   return json({combined_pdf:'synthetic.pdf',combined_preview_pages:['table.png',...ids.map(id=>id+'.png')],combined_page_sizes:Array.from({length:ids.length+1},()=>({width:595,height:842}))});
  }
  if(url.pathname.startsWith('/export/'))return route.fulfill({contentType:'image/png',body:png});
  if(url.pathname.startsWith('/api/'))return json({queue:[]});
  const file=url.pathname.slice(1);assert.match(file,/^[a-z-]+\.(js|css)$/);
  const source=baseline&&['multi-filter.js','multi-filter.css'].includes(file)?path.join(evidence,file.replace('.', '-before.')):path.join(web,file);
  return route.fulfill({contentType:file.endsWith('.js')?'text/javascript':'text/css',body:await fs.readFile(source)});
 });
 const menu=id=>page.locator('#'+id+'Menu'),button=id=>page.locator('#'+id+'Toggle');
 const input=(id,value)=>menu(id).locator('input[value='+JSON.stringify(value)+']');
 const label=(id,value)=>menu(id).locator('label').filter({has:page.locator('input[value='+JSON.stringify(value)+']')});
 const selected=id=>page.evaluate(id=>invoiceMultiFilters.values(id),id);
 const reset=async id=>{await page.evaluate(id=>{invoiceMultiFilters.closeAll();invoiceMultiFilters.set(id,[]);changeCounts[id]=0;},id);};
 const open=async id=>{if(!await menu(id).isVisible())await button(id).click();assert.equal(await menu(id).isVisible(),true);};
 const changes=id=>page.evaluate(id=>changeCounts[id],id);
 async function pointer(locator,area='center'){
  const box=await locator.boundingBox();assert.ok(box);
  const x=area==='left-padding'?box.x+3:area==='right-padding'?box.x+box.width-3:box.x+box.width/2;
  await page.mouse.move(x,box.y+box.height/2);await page.mouse.down();await page.waitForTimeout(60);await page.mouse.up();
 }
 async function checkCase(context,name,action){
  try{await action();cases.push({...context,name,passed:true});}
  catch(error){const result={...context,name,passed:false,error:error.message};cases.push(result);failures.push(result);if(!baseline)throw error;}
 }
 try{
  await fs.mkdir(evidence,{recursive:true});await page.goto(origin+'/');await button('categoryFilter').waitFor();
  for(const theme of ['light','dark'])for(const width of [1280,390]){
   await page.setViewportSize({width,height:900});await page.locator('#themeSelect').selectOption(theme);
   assert.equal(await page.locator('html').getAttribute('data-theme'),theme);
   for(const id of ids){
    const context={theme,width,id};
    for(const area of ['text','left-padding','right-padding','checkbox'])await checkCase(context,area+' toggles once and toggles off',async()=>{
     await reset(id);await open(id);
     const target=()=>area==='text'?label(id,'a').locator('span'):area==='checkbox'?input(id,'a'):label(id,'a');
     await pointer(target(),area);assert.deepEqual(await selected(id),['a'],area+' must select by clicking anywhere on the row');
     assert.equal(await input(id,'a').isChecked(),true);assert.equal(await menu(id).isVisible(),true,'Selecting a row keeps the menu open');assert.equal(await changes(id),1,'One press/release changes selection once');
     await pointer(target(),area);assert.deepEqual(await selected(id),[],'A second click cancels selection');assert.equal(await changes(id),2);assert.equal(await menu(id).isVisible(),true);
    });
    await checkCase(context,'all text, individual cancel and indeterminate state',async()=>{
     await reset(id);await open(id);await pointer(label(id,'').locator('span'));
     assert.deepEqual(await selected(id),['a','b','c']);assert.equal(await input(id,'').isChecked(),true);assert.equal(await input(id,'disabled').isChecked(),false);
     await pointer(label(id,'b'),'right-padding');assert.deepEqual(await selected(id),['a','c']);assert.equal(await input(id,'').isChecked(),false);assert.equal(await input(id,'').evaluate(node=>node.indeterminate),true);
     await pointer(label(id,'').locator('span'));assert.deepEqual(await selected(id),['a','b','c']);
     await pointer(label(id,''),'left-padding');assert.deepEqual(await selected(id),[]);assert.equal(await changes(id),4);
    });
    await checkCase(context,'keyboard Space keeps native checkbox operation',async()=>{
     await reset(id);await button(id).focus();await page.keyboard.press('ArrowDown');assert.equal(await menu(id).isVisible(),true);assert.equal(await input(id,'').evaluate(node=>node===document.activeElement),true);
     await page.keyboard.press('Space');assert.deepEqual(await selected(id),['a','b','c']);
     await input(id,'b').focus();await page.keyboard.press('Space');assert.deepEqual(await selected(id),['a','c']);assert.equal(await input(id,'').evaluate(node=>node.indeterminate),true);
     await page.keyboard.press('Space');assert.deepEqual(await selected(id),['a','b','c']);assert.equal(await changes(id),3);
    });
    await checkCase(context,'disabled row cannot change the selection',async()=>{
     await reset(id);await open(id);await pointer(label(id,'disabled').locator('span'));assert.deepEqual(await selected(id),[]);assert.equal(await changes(id),0);assert.equal(await input(id,'disabled').isDisabled(),true);
    });
    await checkCase(context,'outside, Escape, focus departure and second menu dismiss',async()=>{
     await reset(id);await open(id);await pointer(page.locator('#outside'));assert.equal(await menu(id).isVisible(),false);
     await open(id);await page.keyboard.press('Escape');assert.equal(await menu(id).isVisible(),false);assert.equal(await button(id).evaluate(node=>node===document.activeElement),true);
     await open(id);await page.locator('#outside').focus();await page.waitForTimeout(30);assert.equal(await menu(id).isVisible(),false,'Keyboard focus leaving the control dismisses it');
     await open(id);const other=ids.find(value=>value!==id);await button(other).focus();await page.keyboard.press('Enter');assert.equal(await menu(id).isVisible(),false);assert.equal(await menu(other).isVisible(),true);await page.keyboard.press('Escape');
    });
    await checkCase(context,'menu fits viewport and retains full row hit area',async()=>{
     await reset(id);await open(id);const geometry=await menu(id).evaluate(node=>({left:node.getBoundingClientRect().left,right:node.getBoundingClientRect().right,rows:[...node.querySelectorAll('label')].map(row=>({width:row.getBoundingClientRect().width,height:row.getBoundingClientRect().height,checkbox:row.querySelector('input').getBoundingClientRect().width}))}));
     assert.ok(geometry.left>=0&&geometry.right<=width,JSON.stringify(geometry));assert.ok(geometry.rows.every(row=>row.height===(baseline?36:40)&&row.width>=180&&row.checkbox===16),JSON.stringify(geometry));
    });
   }
   await reset('categoryFilter');await open('categoryFilter');await pointer(label('categoryFilter','a').locator('span'));await open('categoryFilter');
   await page.screenshot({path:path.join(evidence,`filter-row-${baseline?'before':'after'}-${theme}-${width}.png`),fullPage:true});
  }
  // The same row activation also reaches the actual list and real preview wiring,
  // not just the isolated component. Every HTTP response remains synthetic.
  if(!baseline){
   await page.setViewportSize({width:1280,height:900});await page.goto(origin+'/full?project=fixture');await page.locator('#list [data-item="hotel"]').waitFor();
   await open('categoryFilter');await pointer(label('categoryFilter','酒店').locator('span'));assert.deepEqual(await selected('categoryFilter'),['酒店']);assert.deepEqual(await page.locator('#list [data-item]').evaluateAll(nodes=>nodes.map(node=>node.dataset.item)),['hotel']);
   await pointer(label('categoryFilter','酒店'),'right-padding');assert.deepEqual(await selected('categoryFilter'),[]);assert.equal(await page.locator('#list [data-item]').count(),2);await page.keyboard.press('Escape');
   await page.locator('#previewReport').click();await page.waitForFunction(()=>document.querySelector('#reportPreviewDialog')?.getAttribute('aria-busy')==='false');
   await open('previewCategory');await pointer(label('previewCategory','酒店').locator('span'));assert.deepEqual(await selected('previewCategory'),['酒店']);await page.waitForFunction(()=>document.querySelector('#reportPreviewDialog')?.getAttribute('aria-busy')==='false');assert.deepEqual(previewCalls.at(-1),['hotel']);
   await pointer(label('previewCategory','酒店'),'left-padding');assert.deepEqual(await selected('previewCategory'),[]);await page.waitForFunction(()=>document.querySelector('#reportPreviewDialog')?.getAttribute('aria-busy')==='false');assert.equal(await page.frameLocator('#previewFrame').locator('img').count(),3);
   await pointer(label('previewCategory','').locator('span'));assert.deepEqual(await selected('previewCategory'),['酒店','高铁']);await pointer(label('previewCategory','酒店'),'right-padding');assert.deepEqual(await selected('previewCategory'),['高铁']);await page.waitForFunction(()=>document.querySelector('#reportPreviewDialog')?.getAttribute('aria-busy')==='false');assert.deepEqual(previewCalls.at(-1),['rail']);
   await page.screenshot({path:path.join(evidence,'filter-row-full-preview-after.png'),fullPage:true});
   await page.keyboard.press('Escape');assert.equal(await menu('previewCategory').isVisible(),false);assert.equal(await page.locator('#reportPreviewDialog').isVisible(),true);
   assert.deepEqual(await selected('categoryFilter'),[]);assert.deepEqual(await selected('personFilter'),[]);assert.deepEqual(await selected('issueFilter'),[]);
   cases.push({name:'actual workbench list and report-preview wiring',passed:true});
  }
  assert.deepEqual(errors,[]);
  const result={passed:failures.length===0,baseline,componentCases:cases.length,failures,cases,onlyMockOrigin:true,realSharedResources:true,normalPointerHoldMs:60,browserErrors:errors,fullPageMainAndPreview:!baseline,syntheticReadOnlyPreviewCalls:previewCalls};
  await fs.writeFile(path.join(evidence,`multifilter-row-click-${baseline?'before':'after'}.json`),JSON.stringify(result,null,2));
  if(!baseline)assert.equal(failures.length,0);
  return result;
 }catch(error){await page.screenshot({path:path.join(evidence,`filter-row-${baseline?'before':'after'}-failure.png`),fullPage:true});await fs.writeFile(path.join(evidence,`multifilter-row-click-${baseline?'before':'after'}-failure.json`),JSON.stringify({message:error.message,cases,errors},null,2));throw error;}
 finally{await browser.close();}
}
module.exports=run;
if(require.main===module)run(require('playwright')).then(result=>console.log(JSON.stringify({passed:result.passed,baseline:result.baseline,cases:result.componentCases,failed:result.failures.length}))).catch(error=>{console.error(error);process.exitCode=1;});

