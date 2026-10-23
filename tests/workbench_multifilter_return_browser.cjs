// Synthetic end-to-end selection, filtering and return actions; never opens the real ledger.
const assert=require('node:assert/strict'),fs=require('node:fs/promises'),path=require('node:path');
async function run(pw){
 const browser=await pw.chromium.launch({headless:true}),page=await browser.newPage({viewport:{width:1400,height:1000}});
 page.setDefaultTimeout(10000);
 const origin='http://127.0.0.1:18867',errors=[],writes=[],returns=[],transitions=[];
 const fee=(id,category,owner,extra={})=>({id,title:'Synthetic '+id,project_id:'p1',project:'Synthetic project',category,
  reimbursement_person_id:owner,reimbursement_person_label:'Person '+owner,amount_cents:100,amount:'1.00',stage:'draft',verified:false,ready:false,
  complete:true,missing:[],version:'v1-'+id,expense_date:'2026-10-15',merchant:'',order_number:'',note:'',attachments:[],events:[],...extra});
 const items=[fee('rA','高铁','a',{complete:false,missing:['发票']}),fee('rB','酒店','b',{material_match:{voucher_count:1,payment_count:1,voucher_total_cents:200,payment_total_cents:100}}),
  fee('rC','打车','c'),fee('rD','高铁','b'),fee('rFail','酒店','a'),fee('ready1','酒店','a',{verified:true,ready:true}),
  fee('ready2','高铁','b',{verified:true,ready:true}),fee('amountPending','酒店','a',{amount_cents:0,amount:'0.00',amount_pending:true,complete:false,missing:['金额待确认']}),fee('unassigned','酒店','',{complete:false,missing:['发票']}),fee('history','酒店','a',{verified:true,ready:true,stage:'submitted',report_files:{pdf:'old.pdf'}})];
 let categoryOptions=['高铁','酒店','打车'],peopleIds=['a','b','c'],returnError=false,releaseReturn,returnStarted,holdReturn=false,failStateAfterTransition=false,failNextState=false;
 const people=()=>peopleIds.map(id=>({id,label:'Person '+id,project_id:'p1',state:'active',version:'pv1-'+id}));
 const state=()=>({app_mode:'invoice',token:'synthetic',items,projects:[{id:'p1',name:'Synthetic project'}],inbox:[],categories:categoryOptions,
  roles:{invoice:'发票'},stages:{draft:'待核对',submitted:'可提交'},report_template_ready:true,category_auto_ready:false});
 const peopleResponse=()=>({project_id:'p1',people:people(),summary:{rows:people().map(person=>({person_id:person.id,label:person.label,
  expense_count:items.filter(item=>item.reimbursement_person_id===person.id).length,total_cents:items.filter(item=>item.reimbursement_person_id===person.id).reduce((sum,item)=>sum+item.amount_cents,0),
  pending_cents:100,ready_cents:100,packaged_cents:0})),total_cents:items.reduce((sum,item)=>sum+item.amount_cents,0),expense_count:items.length}});
 const png=Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j1ioAAAAASUVORK5CYII=','base64');
 const board={vouchers:['高铁','酒店','打车'].map((category,index)=>({id:'v'+index,key:'q:v'+index,name:'Synthetic voucher '+index,role:'invoice',amount_cents:100,
  project_id:'p1',reimbursement_person_id:'a',fields:{category,merchant:category},report_choice:{project_type:'other',project_name:'Synthetic'},available:true})),payments:[],other:[],archived:[],matches:[],
  suggestions:['高铁','酒店','打车'].map((category,index)=>({id:'s'+index,project_id:'p1',title:category,voucher_keys:['q:v'+index],payment_ids:[],amount_cents:100,snapshot_token:'st'+index})),report_types:[]};
 page.on('pageerror',error=>errors.push(error.message));
 await page.route(origin+'/**',async route=>{
  const request=route.request(),url=new URL(request.url()),json=(body,status=200)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(body)});
  if(url.pathname==='/api/state'){if(failNextState){failNextState=false;return json({error:'Synthetic refresh read failure'},500);}return json(state());}
  if(url.pathname==='/api/reimbursement-people')return json(peopleResponse());
  if(url.pathname==='/api/material-matching')return json(board);
  if(request.method()==='POST'){
   const body=request.postDataJSON();
   if(url.pathname==='/api/expense-print-preview')return json({id:body.id,version:'v1',pdf:'synthetic.pdf',preview_pages:['synthetic.png'],page_count:1,complete:true,missing:[]});
   writes.push(url.pathname);
   if(url.pathname==='/api/transition'){
    transitions.push(body);if(body.id==='rFail')return json({error:'Synthetic verification failure'},409);
    const item=items.find(item=>item.id===body.id);Object.assign(item,{verified:true,ready:true,version:'verified-'+item.id});
    if(failStateAfterTransition){failStateAfterTransition=false;failNextState=true;}return json(item);
   }
   if(url.pathname==='/api/review/return'){
    returns.push(body);if(holdReturn){returnStarted?.();await new Promise(resolve=>{releaseReturn=resolve;});}
    if(returnError)return json({error:'Synthetic return conflict'},409);
    const selected=body.ids.map(id=>items.find(item=>item.id===id));
    for(const item of selected)Object.assign(item,{stage:'draft',verified:false,ready:false,review_requested:true,version:'returned-'+item.id});
    return json({items:selected,changed:selected.length,message:'returned'});
   }
   throw Error('Unexpected synthetic mutation '+url.pathname);
  }
  if(url.pathname.startsWith('/export/'))return route.fulfill({contentType:'image/png',body:png});
  if(url.pathname.startsWith('/api/'))return json({queue:[]});
  const name=url.pathname==='/'?'index.html':url.pathname.slice(1);
  if(!/^[a-z-]+\.(html|js|css)$/.test(name))return route.abort();
  return route.fulfill({contentType:name.endsWith('.js')?'text/javascript':name.endsWith('.css')?'text/css':'text/html',body:await fs.readFile(path.resolve(__dirname,'../src/invoice_print_layout/web',name))});
 });
 const rows=()=>page.locator('#list [data-item]').evaluateAll(nodes=>nodes.map(node=>node.dataset.item));
 const values=id=>page.evaluate(id=>window.invoiceMultiFilters.values(id),id);
 const set=async(id,chosen)=>{await page.evaluate(({id,chosen})=>{window.invoiceMultiFilters.set(id,chosen);document.getElementById(id).dispatchEvent(new Event('change',{bubbles:true}));},{id,chosen});};
 const all=async()=>{for(const id of ['categoryFilter','personFilter','issueFilter'])await set(id,[]);await page.locator('#search').fill('');};
 const check=async(id,choice)=>{
  const menu=page.locator('#'+id+'Menu');if(!await menu.isVisible())await page.locator('#'+id+'Toggle').click();
  const input=menu.locator(`input[value="${choice}"]`),previous=await input.isChecked();await input.click();
  assert.equal(await menu.locator(`input[value="${choice}"]`).isChecked(),!previous,JSON.stringify(await page.evaluate(id=>({values:window.invoiceMultiFilters.values(id),multiple:document.getElementById(id).multiple,options:[...document.getElementById(id).options].map(option=>({value:option.value,selected:option.selected}))}),id)));
 };
 try{
  await page.goto(origin+'/?project=p1');await page.locator('#categoryFilterToggle').waitFor();await page.locator('#list [data-item=rA]').waitFor();
  await check('categoryFilter','高铁');await check('categoryFilter','酒店');
  assert.deepEqual((await values('categoryFilter')).sort(),['酒店','高铁']);
  assert.deepEqual((await rows()).sort(),['amountPending','rA','rB','rD','rFail','unassigned']);
  await set('personFilter',['a']);await check('personFilter','b');
  await check('issueFilter','missing');await check('issueFilter','amount');
  assert.deepEqual((await rows()).sort(),['rA','rB'],'Options OR within one menu, categories/person/issues AND across menus');
  await check('personFilter','b');assert.deepEqual(await rows(),['rA']);await check('personFilter','b');
  await page.locator('.heading h1').click();
  assert.equal(await page.locator('#personFilterMenu').isVisible(),false);
  // Visible all/individual checkboxes are the only person-selection controls.
  const personMenu=page.locator('#personFilterMenu'),personToggle=page.locator('#personFilterToggle');
  const ensurePeopleOpen=async()=>{if(!await personMenu.isVisible())await personToggle.click();};
  const personAll=()=>personMenu.getByRole('checkbox',{name:'全选',exact:true});
  const actualPeople=()=>personMenu.locator('input[type=checkbox]:not([value=""])');
  const assertOtherScopes=async()=>{
   assert.deepEqual((await values('categoryFilter')).sort(),['酒店','高铁']);assert.deepEqual((await values('issueFilter')).sort(),['amount','missing']);
   assert.deepEqual(await page.evaluate(()=>[...checked]),['rA']);
  };
  await page.locator('[data-check=rA]').check();
  const assertPeopleAll=async()=>{
   assert.deepEqual((await values('personFilter')).sort(),['__unassigned__','a','b','c']);
   await ensurePeopleOpen();assert.equal(await personMenu.locator('label').first().innerText(),'全选');
   assert.equal(await personAll().isChecked(),true);assert.equal(await personAll().evaluate(node=>node.indeterminate),false);
   assert.equal(await actualPeople().count(),4);assert.equal(await actualPeople().evaluateAll(nodes=>nodes.filter(node=>node.checked).length),4);
   assert.equal(await personMenu.locator('button').count(),0);
   await assertOtherScopes();
   assert.deepEqual((await rows()).sort(),['rA','rB','unassigned'],'All people includes unassigned while preserving category/problem restrictions');
   assert.equal(await personToggle.locator('span').innerText(),'全部人员');
  };
  const selectionEvidence=[];
  for(const theme of ['light','dark']){
   await page.locator('#themeSelect').selectOption(theme);await set('personFilter',[]);await ensurePeopleOpen();
   assert.equal(await personAll().isChecked(),false);assert.equal(await personAll().evaluate(node=>node.indeterminate),false);
   assert.equal(await actualPeople().evaluateAll(nodes=>nodes.filter(node=>node.checked).length),0);
   assert.doesNotMatch(await personMenu.innerText(),/Alt/i);assert.doesNotMatch(await personToggle.getAttribute('title')||'',/Alt/i);
   assert.equal(await personToggle.getAttribute('aria-keyshortcuts'),null);assert.equal(await page.locator('#personFilterShortcut').count(),0);
   await personAll().click();await assertPeopleAll();selectionEvidence.push({theme,action:'visible all checkbox'});
   await check('personFilter','b');assert.deepEqual((await values('personFilter')).sort(),['__unassigned__','a','c']);
   assert.equal(await personAll().isChecked(),false);assert.equal(await personAll().evaluate(node=>node.indeterminate),true);
   assert.deepEqual((await rows()).sort(),['rA','unassigned']);await assertOtherScopes();
   await personAll().click();await assertPeopleAll();selectionEvidence.push({theme,action:'all after partial selection'});
   await personAll().click();assert.deepEqual(await values('personFilter'),[]);assert.equal(await personAll().isChecked(),false);
   assert.equal(await actualPeople().evaluateAll(nodes=>nodes.filter(node=>node.checked).length),0);
   assert.deepEqual((await rows()).sort(),['rA','rB','unassigned']);await assertOtherScopes();selectionEvidence.push({theme,action:'uncheck all restores unrestricted scope'});
   await check('personFilter','__unassigned__');assert.deepEqual(await values('personFilter'),['__unassigned__']);assert.deepEqual(await rows(),['unassigned']);
   await check('personFilter','a');assert.deepEqual((await values('personFilter')).sort(),['__unassigned__','a']);
   await check('personFilter','__unassigned__');assert.deepEqual(await values('personFilter'),['a']);assert.deepEqual(await rows(),['rA']);
   await check('personFilter','a');assert.deepEqual(await values('personFilter'),[]);assert.deepEqual((await rows()).sort(),['rA','rB','unassigned']);
   await page.keyboard.press('Escape');assert.equal(await personMenu.isVisible(),false);
   // Removed shortcuts behave exactly like normal pointer/keyboard input.
   await set('personFilter',['a']);await personToggle.click({modifiers:['Alt']});
   assert.deepEqual(await values('personFilter'),['a']);assert.equal(await personMenu.isVisible(),true);
   await personMenu.locator('input[value="b"]').click({modifiers:['Alt']});assert.deepEqual((await values('personFilter')).sort(),['a','b']);
   assert.equal(await personAll().isChecked(),false);assert.equal(await personAll().evaluate(node=>node.indeterminate),true);
   await personToggle.focus();await personToggle.press('Alt+a');assert.deepEqual((await values('personFilter')).sort(),['a','b']);
   await assertOtherScopes();selectionEvidence.push({theme,action:'Alt shortcuts removed'});
   await page.locator('.heading h1').click();assert.equal(await personMenu.isVisible(),false);
   for(const id of ['categoryFilter','issueFilter']){
    const old=await values(id);await page.locator('#'+id+'Toggle').click({modifiers:['Alt']});
    assert.deepEqual(await values(id),old);assert.equal(await page.locator('#'+id+'Menu input[value=""]').count(),1);
    await page.keyboard.press('Escape');await page.locator('#'+id+'Toggle').focus();await page.locator('#'+id+'Toggle').press('Alt+a');assert.deepEqual(await values(id),old);
   }
  }
  // Explicit all still does not silently select a newly added person.
  await set('personFilter',['a']);await ensurePeopleOpen();await personAll().click();await assertPeopleAll();
  peopleIds=['a','b','c','d'];await page.evaluate(()=>load());
  assert.deepEqual((await values('personFilter')).sort(),['__unassigned__','a','b','c']);await ensurePeopleOpen();
  assert.equal(await personMenu.locator('input[value="d"]').isChecked(),false);assert.equal(await personAll().isChecked(),false);assert.equal(await personAll().evaluate(node=>node.indeterminate),true);
  peopleIds=['a','b','c'];await page.evaluate(()=>load());await ensurePeopleOpen();await assertPeopleAll();await page.keyboard.press('Escape');
  const allFilterEvidence=[];
  const openFilter=async id=>{const menu=page.locator('#'+id+'Menu');if(!await menu.isVisible())await page.locator('#'+id+'Toggle').click();return menu;};
  const allFilter=id=>page.locator('#'+id+'Menu').getByRole('checkbox',{name:'全选',exact:true});
  const assertFeeCheck=async()=>assert.deepEqual(await page.evaluate(()=>[...checked]),['rA']);
  for(const theme of ['light','dark']){
   await page.locator('#themeSelect').selectOption(theme);
   await set('personFilter',['a','b']);await set('issueFilter',['missing','amount']);await set('categoryFilter',[]);
   let menu=await openFilter('categoryFilter');assert.equal(await menu.locator('label').first().innerText(),'全选');
   assert.equal(await allFilter('categoryFilter').isChecked(),false);assert.equal(await allFilter('categoryFilter').evaluate(node=>node.indeterminate),false);
   await allFilter('categoryFilter').click();
   assert.deepEqual((await values('categoryFilter')).sort(),['打车','酒店','高铁']);assert.equal(await allFilter('categoryFilter').isChecked(),true);
   assert.equal(await page.locator('#categoryFilterToggle span').innerText(),'所有类别');assert.deepEqual((await rows()).sort(),['rA','rB']);
   await check('categoryFilter','高铁');assert.deepEqual((await values('categoryFilter')).sort(),['打车','酒店']);
   assert.equal(await allFilter('categoryFilter').isChecked(),false);assert.equal(await allFilter('categoryFilter').evaluate(node=>node.indeterminate),true);
   assert.deepEqual(await rows(),['rB'],'Remaining categories OR together before person/problem restrictions');
   await allFilter('categoryFilter').click();assert.deepEqual((await values('categoryFilter')).sort(),['打车','酒店','高铁']);
   await allFilter('categoryFilter').click();assert.deepEqual(await values('categoryFilter'),[]);assert.deepEqual((await rows()).sort(),['rA','rB']);
   assert.equal(await menu.locator('input[type=checkbox]:checked').count(),0);assert.equal(await allFilter('categoryFilter').evaluate(node=>node.indeterminate),false);
   assert.deepEqual((await values('personFilter')).sort(),['a','b']);assert.deepEqual((await values('issueFilter')).sort(),['amount','missing']);await assertFeeCheck();
   allFilterEvidence.push({theme,id:'categoryFilter',visibleAll:true,partial:true,reselectAll:true,uncheckAllUnrestricted:true});
   await page.keyboard.press('Escape');assert.equal(await menu.isVisible(),false);

   await set('categoryFilter',['高铁','酒店']);await set('issueFilter',[]);menu=await openFilter('issueFilter');
   assert.equal(await menu.locator('label').first().innerText(),'全选');assert.equal(await allFilter('issueFilter').isChecked(),false);
   await allFilter('issueFilter').click();assert.deepEqual((await values('issueFilter')).sort(),['amount','complete','missing']);
   assert.equal(await allFilter('issueFilter').isChecked(),true);assert.equal(await page.locator('#issueFilterToggle span').innerText(),'所有问题');
   assert.ok((await rows()).includes('amountPending'),'Selecting every problem retains the existing only-missing-amount boundary');
   assert.deepEqual((await rows()).sort(),['amountPending','rA','rB','rD','rFail']);
   await check('issueFilter','complete');assert.deepEqual((await values('issueFilter')).sort(),['amount','missing']);
   assert.equal(await allFilter('issueFilter').isChecked(),false);assert.equal(await allFilter('issueFilter').evaluate(node=>node.indeterminate),true);
   assert.deepEqual((await rows()).sort(),['rA','rB'],'Problem types OR together while category and person intersect');
   await check('issueFilter','amount');assert.deepEqual(await values('issueFilter'),['missing']);assert.deepEqual(await rows(),['rA']);
   await allFilter('issueFilter').click();assert.deepEqual((await values('issueFilter')).sort(),['amount','complete','missing']);
   await allFilter('issueFilter').click();assert.deepEqual(await values('issueFilter'),[]);assert.deepEqual((await rows()).sort(),['amountPending','rA','rB','rD','rFail']);
   assert.equal(await menu.locator('input[type=checkbox]:checked').count(),0);assert.equal(await allFilter('issueFilter').evaluate(node=>node.indeterminate),false);
   assert.deepEqual((await values('personFilter')).sort(),['a','b']);assert.deepEqual((await values('categoryFilter')).sort(),['酒店','高铁']);await assertFeeCheck();
   allFilterEvidence.push({theme,id:'issueFilter',visibleAll:true,partial:true,reselectAll:true,uncheckAllUnrestricted:true});
   await page.locator('.heading h1').click();assert.equal(await menu.isVisible(),false);
  }
  // Rebuilding category options and refreshing a new issue option preserve explicit IDs.
  await set('categoryFilter',[]);await openFilter('categoryFilter');await allFilter('categoryFilter').click();
  await set('issueFilter',[]);await openFilter('issueFilter');await allFilter('issueFilter').click();
  categoryOptions=['高铁','酒店','打车','餐饮'];await page.evaluate(()=>load());
  assert.deepEqual((await values('categoryFilter')).sort(),['打车','酒店','高铁']);
  await openFilter('categoryFilter');assert.equal(await page.locator('#categoryFilterMenu input[value="餐饮"]').isChecked(),false);
  assert.equal(await allFilter('categoryFilter').evaluate(node=>node.indeterminate),true);
  await page.evaluate(()=>{document.querySelector('#issueFilter').append(new Option('Synthetic new problem','synthetic-new'));window.invoiceMultiFilters.refresh('issueFilter');});
  assert.deepEqual((await values('issueFilter')).sort(),['amount','complete','missing']);
  await openFilter('issueFilter');assert.equal(await page.locator('#issueFilterMenu input[value="synthetic-new"]').isChecked(),false);
  assert.equal(await allFilter('issueFilter').evaluate(node=>node.indeterminate),true);await assertFeeCheck();
  categoryOptions=['高铁','酒店','打车'];await page.evaluate(()=>{document.querySelector('#issueFilter option[value="synthetic-new"]').remove();window.invoiceMultiFilters.refresh('issueFilter');});
  await page.evaluate(()=>load());await set('categoryFilter',['高铁','酒店']);await set('issueFilter',['missing','amount']);await set('personFilter',['a','b']);
  await page.locator('.heading h1').click();await page.locator('[data-check=rA]').uncheck();
  const appearance=[],menuGeometry=[];
  for(const theme of ['light','dark']){
   await page.locator('#themeSelect').selectOption(theme);
   for(const width of [1400,390]){
    await page.setViewportSize({width,height:1000});
    for(const id of ['categoryFilter','personFilter','issueFilter']){
    await page.locator('#'+id+'Toggle').click();assert.equal(await page.locator('#'+id+'Menu').isVisible(),true);
    const menu=await page.locator('#'+id+'Menu').evaluate(node=>({width:innerWidth,left:node.getBoundingClientRect().left,right:node.getBoundingClientRect().right,
     labels:[...node.querySelectorAll('label')].map(label=>{const input=label.querySelector('input'),span=label.querySelector('span'),rect=input.getBoundingClientRect();
      return {height:label.getBoundingClientRect().height,textHeight:span.getBoundingClientRect().height,checkboxWidth:rect.width,checkboxHeight:rect.height};})}));
    assert.ok(menu.left>=0&&menu.right<=width,`${theme}/${width}/${id} menu stays inside the viewport: ${JSON.stringify(menu)}`);
    assert.ok(menu.labels.every(label=>label.height===40&&label.textHeight===20&&label.checkboxWidth===16&&label.checkboxHeight===16),`${theme}/${width}/${id} has one-line labels and 16px checkboxes: ${JSON.stringify(menu)}`);
    menuGeometry.push({theme,width,id,...menu});
    await page.keyboard.press('Escape');assert.equal(await page.locator('#'+id+'Menu').isVisible(),false);
    appearance.push(await page.locator('#'+id+'Toggle').evaluate(node=>{const s=getComputedStyle(node);return {theme:document.documentElement.dataset.theme,height:node.getBoundingClientRect().height,radius:s.borderRadius,arrow:s.backgroundPositionY};}));
    }
   }
  }
  await page.setViewportSize({width:1400,height:1000});
  assert.ok(appearance.every(value=>value.height===40&&value.radius==='6px'&&value.arrow==='50%'));
  await page.evaluate(()=>load());
  assert.deepEqual((await values('categoryFilter')).sort(),['酒店','高铁']);assert.deepEqual((await values('personFilter')).sort(),['a','b']);assert.deepEqual((await values('issueFilter')).sort(),['amount','missing']);
  peopleIds=['a','c'];await page.evaluate(()=>load());assert.deepEqual(await values('personFilter'),['a'],'Refreshing the roster drops only stale people');
  assert.deepEqual((await values('categoryFilter')).sort(),['酒店','高铁']);assert.deepEqual((await values('issueFilter')).sort(),['amount','missing']);
  peopleIds=['a','b','c'];await page.evaluate(()=>load());
  await set('personFilter',['a','b']);await page.locator('#search').fill('not-in-list');
  await page.locator('#reimbursementPeople').click();await page.locator('[data-person-details="a"][data-allocation-scope="all"][data-allocation-count="false"]').click();
  await page.locator('[data-open-person-expense="rA"]').click();await page.locator('#expensePrintPages').waitFor();
  assert.deepEqual(await values('personFilter'),['a']);assert.deepEqual(await values('categoryFilter'),[]);assert.deepEqual(await values('issueFilter'),[]);assert.equal(await page.locator('#search').inputValue(),'');
  assert.equal(await page.locator('[data-item=rA]').getAttribute('class').then(value=>value.includes('selected')),true,'Personnel drilldown resets to that person alone');

  await all();await set('categoryFilter',['高铁','酒店']);await page.locator('#materialMatching').click();
  await page.locator('#matchingSuggestionsList [data-synthesis-id]').first().waitFor();
  assert.deepEqual((await page.locator('#matchingSuggestionsList [data-synthesis-id]').evaluateAll(nodes=>nodes.map(node=>node.dataset.synthesisId))).sort(),['s0','s1','s2'],'Desk suggestions use the current project independently of expense category filters');
  assert.deepEqual((await values('categoryFilter')).sort(),['酒店','高铁'],'Opening the desk preserves main expense filters');
  assert.equal(await page.locator('#tabs [data-view=synthesis]').count(),0);
  await page.locator('#matchingClose').click();await page.locator('#tabs [data-view=review]').click();await all();
  await page.locator('[data-check=rC]').check();await page.locator('[data-check=rFail]').check();
  failStateAfterTransition=true;
  await page.locator('#verifySelected').click();await page.locator('#confirmVerify').click();
  await page.waitForFunction(()=>!batchVerifyBusy);
  assert.deepEqual(await page.evaluate(()=>[...checked]),['rFail'],'Only successful verification clears the checkbox');
  assert.equal(items.find(item=>item.id==='rC').ready,true);assert.equal(items.find(item=>item.id==='rFail').ready,false);
  assert.equal(await page.locator('[data-item=rC]').count(),0,'A final state-read failure still renders the locally committed success');
  assert.equal(await page.locator('[data-check=rFail]').isChecked(),true);assert.match(await page.locator('#notice').innerText(),/列表刷新失败/);
  await page.locator('#tabs [data-view=submitted]').click();assert.equal(await page.locator('[data-check=rC]').isChecked(),false,'Verified items enter the next stage without a stale checkbox');

  await page.evaluate(()=>{checked.clear();renderSelection();});
  await page.locator('[data-check=ready1]').check();await page.locator('[data-check=ready2]').check();await set('categoryFilter',['酒店']);
  const beforeCancel=writes.length;await page.locator('#returnSelected').click();await page.locator('#cancelVerify').click();
  await page.waitForFunction(()=>!batchReturnBusy);assert.equal(writes.length,beforeCancel);assert.deepEqual((await page.evaluate(()=>[...checked])).sort(),['ready1','ready2']);
  holdReturn=true;const started=new Promise(resolve=>{returnStarted=resolve;});
  await page.locator('#returnSelected').click();await page.locator('#confirmVerify').click();await started;
  assert.equal(await page.locator('#returnSelected').isDisabled(),true);await page.evaluate(()=>document.querySelector('#returnSelected').click());
  assert.equal(returns.length,1);assert.deepEqual(returns[0].ids,['ready1']);assert.equal(returns[0].project_id,'p1');assert.deepEqual(returns[0].versions,{ready1:'v1-ready1'});
  holdReturn=false;releaseReturn();await page.waitForFunction(()=>!batchReturnBusy);
  assert.deepEqual(await page.evaluate(()=>[...checked]),['ready2'],'Hidden selections survive a visible-only return');
  assert.equal(items.find(item=>item.id==='ready1').verified,false);assert.equal(await page.locator('[data-item=ready1]').count(),0);
  await page.locator('#tabs [data-view=review]').click();assert.equal(await page.locator('[data-item=ready1]').count(),1);
  await page.locator('#tabs [data-view=submitted]').click();await set('categoryFilter',['高铁']);
  assert.equal(await page.locator('[data-check=ready2]').isChecked(),true);returnError=true;
  await page.locator('#returnSelected').click();await page.locator('#confirmVerify').click();await page.waitForFunction(()=>!batchReturnBusy);
  assert.match(await page.locator('#error').innerText(),/Synthetic return conflict/);assert.deepEqual(await page.evaluate(()=>[...checked]),['ready2']);
  assert.equal(items.find(item=>item.id==='ready2').ready,true);
  assert.deepEqual(errors,[]);assert.ok(writes.every(value=>['/api/transition','/api/review/return'].includes(value)));
  return {passed:true,multiOptionOrAcrossFiltersAnd:true,blankAndEscapeDismiss:true,visiblePersonAllCheckbox:true,selectionEvidence,allFilterEvidence,categoryAndIssueExplicitAll:true,onlyAmountPendingSurvivesAllProblems:true,newOptionsDoNotExpandExplicitSelection:true,removedShortcutsStayInactive:true,personSelectionPreservesOtherScopesAndFeeChecks:true,appearance,menuGeometry,refreshPreservesSelections:true,
   rosterOnlyDropsStaleSelections:true,personDrilldownResetsOnePerson:true,deskSuggestionsIndependentOfExpenseFilters:true,
   verifySuccessClearsFailureRetains:true,verifyCommittedStateSurvivesRefreshFailure:true,returnOnlyVisibleChecked:true,cancelWrites:0,hiddenChecksPreserved:true,
   duplicateReturnSuppressed:true,returnErrorPreservesChecks:true,syntheticTransitions:transitions.length,syntheticReturnRequests:returns.length};
 }finally{releaseReturn?.();await browser.close();}
}
module.exports=run;
if(require.main===module)run(require('playwright')).then(result=>console.log(JSON.stringify(result))).catch(error=>{console.error(error);process.exitCode=1;});
