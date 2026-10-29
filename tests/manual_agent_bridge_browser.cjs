// Bridge callbacks refresh the original workbench without issuing organizer writes.
const assert=require('node:assert/strict'),fs=require('node:fs/promises'),path=require('node:path');
async function run(pw){
  const browser=await pw.chromium.launch({headless:true}),page=await browser.newPage({viewport:{width:1100,height:900}});
  const origin='http://127.0.0.1:18996',requests=[],errors=[];let mailAddress='synthetic@163.com';
  const harness=`<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"></head><body>
    <section id="manualAgent"></section><p id="manualMailStatus" hidden></p>
    <input id="search" value="保留搜索"><select id="category"><option>酒店</option><option>打车</option></select>
    <button id="other">返回列表</button><div id="detail">原费用详情</div><dialog id="edit"><form><input id="editing" value="尚未保存"><button type="button">保存</button></form></dialog>
    <script>
    let currentProject='p1',batchVerifyBusy=false,singleVerifyBusy=false,batchReturnBusy=false;
    let state={items:[{id:'e1',project_id:'p1',title:'原费用'},{id:'e-other',project_id:'p2',title:'其他项目费用'},{id:'e-deleted',project_id:'p1',deleted:true}]};
    const currentInvoiceProjectId=()=>currentProject;
    window.calls={loads:[],notices:[],writes:[],located:[],previews:[],opened:0,settings:[],matchingBusy:false,failLoad:false,holdLoad:false};window.invoiceSettings={open:tab=>calls.settings.push(tab)};
    window.checked=new Set(['hidden-item']);
    window.matchingSuggestionActions={isBusy:()=>calls.matchingBusy};
    window.manualInvoiceAgent={mount:options=>{window.bridgeOptions=options;return{open:()=>calls.opened++};}};
    window.showCreatedMaterialExpenses=items=>calls.located.push(items.map(item=>({...item})));
    window.openAgentReportPreview=payload=>{calls.previews.push(payload);return Promise.resolve('existing-preview-opened');};
    async function api(route,body){calls.writes.push({route,body});return {};}
    async function load(options){calls.loads.push(options);if(calls.holdLoad){calls.holdLoad=false;await new Promise(resolve=>window.releaseLoad=resolve);}if(calls.failLoad)throw Error('synthetic read failure');}
    function notify(message,error=false){calls.notices.push({message,error});}
    window.fixture={setProject:id=>{currentProject=id;},setBusy:(kind,value)=>{if(kind==='batchVerify')batchVerifyBusy=value;if(kind==='singleVerify')singleVerifyBusy=value;if(kind==='batchReturn')batchReturnBusy=value;}};
    </script><script src="/manual-agent-bridge.js"></script></body></html>`;
  await page.clock.install({time:new Date('2030-01-01T00:00:00Z')});
  await page.clock.pauseAt(new Date('2030-01-01T00:00:00Z'));
  page.on('pageerror',error=>errors.push(error.message));
  await page.route(origin+'/**',async route=>{
    const request=route.request(),url=new URL(request.url());requests.push({path:url.pathname,method:request.method()});
    if(url.pathname==='/fixture')return route.fulfill({contentType:'text/html',body:harness});
    if(url.pathname==='/manual-agent-bridge.js')return route.fulfill({contentType:'text/javascript',body:await fs.readFile(path.resolve(__dirname,'../src/invoice_print_layout/web/manual-agent-bridge.js'))});
    if(url.pathname.startsWith('/api/')){
      assert.equal(request.method(),'GET','Background bridge reads must not use the original POST helper');
      return route.fulfill({contentType:'application/json',body:JSON.stringify(url.pathname.endsWith('/mail')?{address:mailAddress,enabled:false}:{jobs:[],model:{available:false}})});
    }
    return route.abort();
  });
  const loads=()=>page.evaluate(()=>calls.loads.length);
  const changed=jobId=>page.evaluate(jobId=>bridgeOptions.onChanged({projectId:'p1',jobId,itemIds:['e1']}),jobId);
  const advance=async ms=>{await page.clock.runFor(ms);};
  try{
    await page.goto(origin+'/fixture');await page.waitForFunction(()=>!!window.bridgeOptions);
    await page.getByText('收件邮箱：synthetic@163.com · 自动收件已暂停，可在材料合成台手动补收',{exact:true}).waitFor();
    await page.locator('#manualMailStatus').getByRole('button',{name:'设置',exact:true}).click();assert.deepEqual(await page.evaluate(()=>calls.settings),['mail']);
    mailAddress='';await page.evaluate(()=>window.dispatchEvent(new CustomEvent('invoice-settings-changed')));
    await page.getByText('未设置收件邮箱',{exact:true}).waitFor();assert.equal(await page.locator('#manualMailStatus').isVisible(),true);
    mailAddress='updated@163.com';await page.evaluate(()=>window.dispatchEvent(new CustomEvent('invoice-settings-changed')));
    await page.getByText('收件邮箱：updated@163.com · 自动收件已暂停，可在材料合成台手动补收',{exact:true}).waitFor();
    await changed('j1');await changed('j1');await changed('j2');
    await advance(499);assert.equal(await loads(),0);
    await advance(2);assert.equal(await loads(),1,'Same-project result changes coalesce into one deferred refresh');
    assert.deepEqual(await page.evaluate(()=>calls.loads),[{skipClassification:true}]);
    assert.equal(await page.locator('#search').inputValue(),'保留搜索');assert.equal(await page.locator('#category').inputValue(),'酒店');
    assert.deepEqual(await page.evaluate(()=>[...checked]),['hidden-item']);
    await page.evaluate(()=>bridgeOptions.onChanged({projectId:'p2',jobId:'foreign-job',itemIds:['e-other']}));
    await advance(1200);assert.equal(await loads(),1,'Other-project results must not refresh the current ledger');

    await page.locator('#search').focus();await page.locator('#search').fill('正在输入，保留此文本');
    await changed('j-edit-input');await advance(1200);assert.equal(await loads(),1);
    await page.locator('#other').focus();await advance(510);assert.equal(await loads(),2);
    assert.equal(await page.locator('#search').inputValue(),'正在输入，保留此文本');
    await page.evaluate(()=>document.querySelector('#edit').showModal());await changed('j-edit-dialog');
    await advance(1200);assert.equal(await loads(),2);assert.equal(await page.locator('#editing').inputValue(),'尚未保存');
    await page.evaluate(()=>document.querySelector('#edit').close());await advance(510);assert.equal(await loads(),3);

    await page.evaluate(()=>calls.matchingBusy=true);await changed('j-matching');await advance(1100);assert.equal(await loads(),3);
    await page.evaluate(()=>calls.matchingBusy=false);await advance(510);assert.equal(await loads(),4);
    for(const [index,kind] of ['batchVerify','singleVerify','batchReturn'].entries()){
      await page.evaluate(kind=>fixture.setBusy(kind,true),kind);await changed('j-'+kind);await advance(1100);assert.equal(await loads(),4+index);
      await page.evaluate(kind=>fixture.setBusy(kind,false),kind);await advance(510);assert.equal(await loads(),5+index);
    }
    const beforeFailure=await loads();await page.evaluate(()=>calls.failLoad=true);await changed('j-refresh-failure');
    await advance(510);assert.equal(await loads(),beforeFailure+1);await advance(1600);assert.equal(await loads(),beforeFailure+1);
    assert.equal(await page.evaluate(()=>calls.notices.at(-1).error),true);
    assert.match(await page.evaluate(()=>calls.notices.at(-1).message),/列表刷新未完成/);
    assert.deepEqual(await page.evaluate(()=>calls.writes),[],'A failed result refresh must not restart an accepted job');
    await page.evaluate(()=>calls.failLoad=false);

    const beforeWrong=await loads();
    const wrong=await page.evaluate(async()=>{try{await bridgeOptions.onOpenExpense({projectId:'p2',itemId:'e-other'});return '';}catch(error){return error.message;}});
    assert.match(wrong,/项目已变化/);assert.equal(await loads(),beforeWrong);
    await page.evaluate(()=>bridgeOptions.onOpenExpense({projectId:'p1',itemId:'e1'}));
    assert.deepEqual(await page.evaluate(()=>calls.located),[[{id:'e1',project_id:'p1',title:'原费用'}]],'Explicit navigation uses the original expense locator');
    for(const id of ['e-other','e-deleted','missing']){
      const message=await page.evaluate(async itemId=>{try{await bridgeOptions.onOpenExpense({projectId:'p1',itemId});return '';}catch(error){return error.message;}},id);
      assert.match(message,/找不到该费用/);
    }
    assert.equal(await page.evaluate(()=>calls.located.length),1);
    await page.evaluate(()=>{calls.holdLoad=true;window.heldOpen=bridgeOptions.onOpenExpense({projectId:'p1',itemId:'e1'});});
    await page.waitForFunction(()=>typeof window.releaseLoad==='function');
    await page.evaluate(async()=>{fixture.setProject('p2');releaseLoad();await heldOpen;});
    assert.equal(await page.evaluate(()=>calls.located.length),1,'Project changes during a read cannot navigate to an old expense');
    const previewWrong=await page.evaluate(()=>{try{bridgeOptions.onPreview({projectId:'p1',itemIds:['e1']});return '';}catch(error){return error.message;}});
    assert.match(previewWrong,/项目已变化/);assert.equal(await page.evaluate(()=>calls.previews.length),0);
    await page.evaluate(()=>fixture.setProject('p1'));
    assert.equal(await page.evaluate(()=>bridgeOptions.onPreview({projectId:'p1',itemIds:['e1']})),'existing-preview-opened');
    assert.deepEqual(await page.evaluate(()=>calls.previews),[{projectId:'p1',itemIds:['e1']}]);
    await page.evaluate(()=>bridgeOptions.api('/api/agent/jobs?project=p1'));
    assert.deepEqual(await page.evaluate(()=>calls.writes),[]);
    assert.ok(requests.filter(request=>request.path.startsWith('/api/')).every(request=>request.method==='GET'));
    assert.ok((await page.evaluate(()=>calls.loads)).every(options=>options.skipClassification===true));
    assert.deepEqual(errors,[]);
    return {passed:true,deferredCoalescedRefresh:true,skipClassification:true,inputAndDialogNotInterrupted:true,
      matchingAndVerificationBusyRespected:true,filtersAndChecksPreserved:true,wrongProjectIgnored:true,
      failedRefreshDoesNotRestart:true,originalExpenseLocatorUsed:true,previewDelegatesExactIds:true,backgroundReadsOnly:true,pageErrors:errors};
  }finally{await browser.close();}
}
module.exports=run;
if(require.main===module)run(require('playwright')).then(result=>console.log(JSON.stringify(result))).catch(error=>{console.error(error);process.exitCode=1;});
