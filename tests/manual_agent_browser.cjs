// The existing workbench owns projects, upload, editing and final report preview.
// This module owns only bounded Agent tasks and source-bound user answers.
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');

async function run(pw) {
  const browser = await pw.chromium.launch({headless:true});
  const page = await browser.newPage({viewport:{width:1040,height:900}});
  const origin = 'http://127.0.0.1:18996', errors = [], posts = [], answers = [];
  const state = {jobs:{alpha:[],beta:[]}, queue:[], available:true, failGet:0, failStart:false, organize:{auto_after_upload:true,use_model:true,model_available:true},failSettings:false};
  const cache = new Map();
  let delayedGet = null, startGate = null, answerGate = null, refreshGate = null, sequence = 0;
  const gate = () => { let release; const promise = new Promise(resolve => {release=resolve;}); return {promise,release}; };
  const base = (project, id, values = {}) => ({id,project_id:project,state:'queued',models:0,tools:0,
    limits:{models:6,tools:48,seconds:180},events:[],item_ids:[],message:'等待后台整理',question:null,preview:null,...values});
  const clone = value => JSON.parse(JSON.stringify(value));
  const originalStyle = await fs.readFile(path.resolve(__dirname,'../src/invoice_print_layout/web/style.css'));
  const appSource = await fs.readFile(path.resolve(__dirname,'../src/invoice_print_layout/web/app.js'),'utf8');
  const postApi = appSource.slice(appSource.indexOf('async function api('),appSource.indexOf('async function act('));
  page.on('pageerror', reason => errors.push(reason.message));
  await page.route(origin+'/**', async route => {
    const request = route.request(), url = new URL(request.url());
    const json = value => route.fulfill({contentType:'application/json',body:JSON.stringify(value)});
    if (url.pathname === '/') return route.fulfill({contentType:'text/html',body:`<!doctype html><html lang="zh-CN" class="invoice-assistant"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/style.css"><link rel="stylesheet" href="/workbench-theme.css"><link rel="stylesheet" href="/manual-agent.css"></head><body><main style="width:100%;margin:0;padding:12px;min-width:0"><label>原费用名称<input id="manualEdit" value="人工修改尚未保存"></label><div id="agentHost"></div><button id="originalControl">原工作台操作</button></main><script src="/manual-agent.js"></script><script>
      window.selectedProject='alpha';window.callbacks=[];window.parentLoads=0;window.deferredChanges=[];window.manualEditing=false;window.settingsOpened=[];window.invoiceSettings={open:tab=>settingsOpened.push(tab)};
      const projectScope='',state={token:'synthetic'};
      ${postApi}
      window.injectedApi=async(path,body)=>{if(body!==undefined)return api(path.slice(5),body);const response=await fetch(path,{cache:'no-store'});const data=await response.json();if(!response.ok)throw new Error(data.error||'处理未完成');return data;};
      window.mountTest=()=>window.controller=window.manualInvoiceAgent.mount({host:document.getElementById('agentHost'),api:window.injectedApi,getProject:()=>window.selectedProject,
        onChanged:data=>{if(window.failChanged)throw Error('合成结果回调失败');window.callbacks.push(['changed',data]);if(window.manualEditing)window.deferredChanges.push(data);else window.parentLoads++;},
        onOpenMaterial:data=>window.callbacks.push(['material',data]),onOpenExpense:data=>window.callbacks.push(['expense',data]),onPreview:data=>window.callbacks.push(['preview',data])});
      window.mountTest();
    </script></body></html>`});
    if (url.pathname === '/api/settings') {
      if(state.failSettings)return route.fulfill({status:503,contentType:'application/json',body:'{"error":"temporary"}'});
      return json({organize:state.organize});
    }
    if (url.pathname === '/api/agent/jobs') {
      const project = url.searchParams.get('project');
      const result = {jobs:clone(state.jobs[project] || []),model:{available:state.available},token:'synthetic'};
      if (state.pendingIds) result.pending_material_ids = state.pendingIds[project] || [];
      if (delayedGet && delayedGet.project === project) { const delayed=delayedGet; delayedGet=null; delayed.entered=true; await delayed.gate.promise; }
      if (state.failGet) { state.failGet--; return route.fulfill({status:503,contentType:'application/json',body:'{"error":"temporary"}'}); }
      return json(result);
    }
    if (url.pathname === '/api/intake') return json({queue:state.queue});
    if (request.method() === 'POST') {
      const body = request.postDataJSON(); posts.push({path:url.pathname,body});
      assert.equal(request.headers()['x-workbench-token'],'synthetic');
      if (url.pathname === '/api/agent/start') {
        if (state.startError) return route.fulfill({status:state.startError.status,contentType:'application/json',body:JSON.stringify(state.startError.body)});
        const prior = cache.get(body.idempotency_key);
        if (prior) { assert.deepEqual(prior.body,body); return json({job:prior.job}); }
        const job = base(body.project_id,'created-'+(++sequence));
        state.jobs[body.project_id].unshift(job); cache.set(body.idempotency_key,{body:clone(body),job});
        if (startGate) { const waiting=startGate; startGate=null; waiting.entered=true; await waiting.gate.promise; }
        if (state.failStart) { state.failStart=false; return route.abort('failed'); }
        return json({job});
      }
      if (url.pathname === '/api/agent/answer') {
        answers.push(body); const job=state.jobs[windowProject(body.id)].find(row=>row.id===body.id);
        assert.equal(body.question_id,job.question.id); assert.ok(job.question.choices.some(choice=>choice.id===body.choice));
        if (answerGate) { const waiting=answerGate; answerGate=null; waiting.entered=true; await waiting.gate.promise; }
        Object.assign(job,body.choice==='no'?{state:'stopped',message:'已记录不是同一笔',question:null}:{state:'ready',message:'报销预览已准备',question:null,tools:13,
          item_ids:['expense-alpha'],expenses:[{title:'饮用水',goods:'娃哈哈饮用水2组、派送',category:'材料',amount_cents:2960}],
          preview:{pdf:'only-this-job.pdf',xlsx:'only-this-job.xlsx',total_cents:2960,page_count:2,preview_pages:[]}});
        return json({job});
      }
      if (url.pathname === '/api/agent/refresh-question') {
        const job=state.jobs[windowProject(body.id)].find(row=>row.id===body.id);
        assert.equal(body.expected_updated_at,job.updated_at);
        if (refreshGate) { const waiting=refreshGate; refreshGate=null; waiting.entered=true; await waiting.gate.promise; }
        Object.assign(job,{updated_at:124,question:{id:'refreshed-hotel-question',type:'material_relation',
          text:'请确认四笔付款与这张住宿发票对应，两笔同额付款独立。',material_ids:['hotel-invoice','hotel-payment'],
          choices:[{id:'hotel-yes',action:'confirm_relation',amount_cents:135300,voucher_keys:['q:hotel-invoice'],
            payment_ids:['hotel-payment'],proposal:{type:'complete_partial_expense'}}]}});
        return json({job});
      }
      if (url.pathname === '/api/agent/cancel' || url.pathname === '/api/agent/retry') {
        const job=state.jobs[windowProject(body.id)].find(row=>row.id===body.id);
        Object.assign(job,{state:url.pathname.endsWith('/cancel')?'cancelled':'queued',message:'状态已更新'}); return json({job});
      }
      assert.fail('Unexpected write: '+url.pathname);
    }
    if (url.pathname.startsWith('/intake-page/')) return route.fulfill({contentType:'image/svg+xml',body:'<svg xmlns="http://www.w3.org/2000/svg" width="100" height="120"><text y="20">原件</text></svg>'});
    if (url.pathname.startsWith('/intake-preview/') || url.pathname.startsWith('/export/')) return route.fulfill({contentType:'text/html',body:'<p>完整原件</p>'});
    if (url.pathname === '/style.css') return route.fulfill({contentType:'text/css',body:originalStyle});
    const name=url.pathname.slice(1);
    if (/^(manual-agent\.(js|css)|workbench-theme\.css)$/.test(name)) return route.fulfill({contentType:name.endsWith('.js')?'text/javascript':'text/css',body:await fs.readFile(path.resolve(__dirname,'../src/invoice_print_layout/web',name))});
    return route.abort();
  });
  function windowProject(id) { return Object.keys(state.jobs).find(project=>state.jobs[project].some(job=>job.id===id)); }
  async function refresh() { await page.evaluate(()=>window.controller.refresh()); }
  async function select(project) { await page.evaluate(project=>{window.selectedProject=project;return window.controller.refresh();},project); }
  async function settingsChange(values) { Object.assign(state.organize,values);await page.evaluate(organize=>window.dispatchEvent(new CustomEvent('invoice-settings-changed',{detail:{organize}})),state.organize); }
  async function refreshAndWait(text) { await refresh(); await page.getByText(text,{exact:true}).waitFor(); }
  async function screenshot(name) {
    const output=process.env.INVOICE_BROWSER_EVIDENCE;
    if (output) { await fs.mkdir(output,{recursive:true});await page.screenshot({path:path.join(output,name+'.png')}); }
  }
  try {
    state.jobs.alpha=[base('alpha','old-ready',{state:'ready',item_ids:['old-item'],preview:{pdf:'old.pdf',xlsx:'old.xlsx',total_cents:800}})];
    await page.goto(origin+'/');
    await refresh();
    assert.equal(await page.locator('#agentHost').isVisible(),false,'completed history stays hidden');
    assert.equal(await page.locator('.mia-job').count(),0);
    await screenshot('normal-hidden');
    assert.equal(await page.evaluate(()=>window.parentLoads),0,'initial historical ready cannot reload the parent');
    assert.equal(await page.evaluate(()=>window.manualInvoiceAgent.shouldAutoOrganize('alpha')),true);
    await page.evaluate(()=>window.controller.open());
    assert.equal(await page.locator('#agentHost').isVisible(),false,'upload open hook cannot reveal ordinary history');
    assert.equal(await page.locator('.mia-check input').count(),0,'organizer preferences live only in settings');
    await settingsChange({auto_after_upload:false});assert.equal(await page.evaluate(()=>window.controller.shouldAutoOrganize('alpha')),false);
    await page.getByRole('button',{name:'整理未处理材料',exact:true}).waitFor();
    await page.getByRole('button',{name:'整理设置',exact:true}).click();assert.deepEqual(await page.evaluate(()=>window.settingsOpened),['organize']);
    assert.equal(await page.locator('.mia-job').count(),0,'manual mode still omits history');
    await screenshot('manual-entry');
    await select('beta'); assert.equal(await page.evaluate(()=>window.controller.shouldAutoOrganize('beta')),false,'preferences are global');
    await select('alpha'); assert.equal(await page.evaluate(()=>window.controller.shouldAutoOrganize('alpha')),false);
    await page.evaluate(()=>{window.mountTest();window.controller.open();}); await refresh();
    assert.equal(await page.evaluate(()=>window.controller.shouldAutoOrganize('alpha')),false,'remount reads global manual-mode preference');
    await settingsChange({auto_after_upload:true,use_model:false});
    await select('');assert.equal(await page.locator('#agentHost').isVisible(),false);assert.equal(await page.evaluate(()=>window.controller.shouldAutoOrganize('')),false);
    await select('alpha');assert.equal(await page.locator('#agentHost').isVisible(),false);

    const request={projectId:'alpha',entryIds:['new-b','new-a','new-a'],batch:'upload-one'};
    startGate={gate:gate(),entered:false}; const starting=startGate;
    const start=page.evaluate(request=>Promise.all([window.controller.startReceived(request),window.controller.startReceived(request)]),request);
    // The first start is held, so a second call cannot start a competing batch.
    for(let i=0;i<50&&!starting.entered;i++)await new Promise(resolve=>setTimeout(resolve,10));
    assert.equal(starting.entered,true);
    assert.equal(posts.filter(row=>row.path==='/api/agent/start').length,1,'double-click must send one request');
    const locked=await page.evaluate(async()=>{try{await window.controller.startReceived({projectId:'alpha',entryIds:['different'],batch:'other'});return '';}catch(reason){return reason.message;}});
    assert.match(locked,/一批材料正在启动/);
    state.failGet=1; starting.gate.release(); const accepted=await start;
    assert.equal(accepted[0].job.id,accepted[1].job.id); assert.equal(posts[0].body.use_model,false);
    assert.deepEqual(posts[0].body.material_ids,['new-a','new-b']);
    await refresh(); await refresh(); await page.evaluate(request=>window.controller.startReceived(request),request);
    assert.equal(posts.filter(row=>row.path==='/api/agent/start').length,1,'GET failures and repeated batch cannot repeat accepted writes');
    assert.equal(await page.locator('#agentHost').isVisible(),false,'queued jobs run without a panel');
    state.failGet=1;await refresh();
    assert.equal(await page.locator('#agentHost').isVisible(),true,'read errors remain visible');
    assert.equal(await page.locator('.manual-invoice-agent').evaluate(element=>element.open),true);
    await refresh();assert.equal(await page.locator('#agentHost').isVisible(),false,'read recovery hides the panel');

    const networkBatch={projectId:'alpha',entryIds:['retry-material'],batch:'uncertain-network'};
    state.failStart=true;
    const failed=await page.evaluate(async request=>{try{await window.controller.startReceived(request);return '';}catch(reason){return reason.message;}},networkBatch);
    assert.ok(failed); await page.getByRole('button',{name:'重试启动本批整理',exact:true}).waitFor();
    await screenshot('start-failure');
    assert.equal(await page.locator('.manual-invoice-agent').evaluate(element=>element.open),true);
    const uncertain=posts.filter(row=>row.path==='/api/agent/start').at(-1).body;
    await page.evaluate(()=>window.mountTest()); await refresh();
    assert.equal(await page.locator('#agentHost').isVisible(),true,'persisted failed starts reveal the panel after remount');
    await page.getByRole('button',{name:'重试启动本批整理',exact:true}).click();
    await page.getByRole('button',{name:'重试启动本批整理',exact:true}).waitFor({state:'detached'});
    assert.equal(await page.locator('#agentHost').isVisible(),false,'successful start retry hides the panel');
    assert.deepEqual(posts.filter(row=>row.path==='/api/agent/start').at(-1).body,uncertain,'retry after browser remount preserves exact key and body');
    assert.equal(state.jobs.alpha.filter(job=>job.id==='created-2').length,1,'ambiguous delivery still creates one durable job');

    const created=state.jobs.alpha.find(job=>job.id===accepted[0].job.id);
    await page.evaluate(()=>document.querySelector('.manual-invoice-agent').open=false);
    Object.assign(created,{state:'needs_input',models:1,tools:5,question:{id:'source-question',type:'material_relation',text:'这张付款确实对应饮用水采购吗？',
      material_ids:['water','pay','support'],evidence_summary:['发票29.60元；美团付款29.60元，但未写实际商家。'],
      material_facts:[{id:'water',goods:'娃哈哈饮用水2组、派送',merchant:'合成供水店',amount_cents:2960}],
      choices:[{id:'yes',action:'confirm_relation',voucher_keys:['q:water'],payment_ids:['pay'],support_ids:['support'],amount_cents:2960},
        {id:'no',action:'not_same_expense'}]}});
    await refreshAndWait('材料 1：娃哈哈饮用水2组、派送 · 29.60 元 · 合成供水店');
    assert.equal(await page.locator('.manual-invoice-agent').evaluate(element=>element.open),true);
    const card=page.locator('[data-job-id="'+created.id+'"]');
    assert.equal(await page.locator('.mia-job').count(),1,'only actionable cards appear alongside history and queued jobs');
    await screenshot('question-expanded');
    assert.equal(await card.locator('.mia-original').count(),3);
    assert.match(await card.innerText(),/付款29.60元.*未写实际商家/);
    assert.match(await card.getByRole('button',{name:/^同一笔/}).innerText(),/明细：材料 3/);
    const fullLink=card.locator('.mia-original').first();
    assert.equal(await fullLink.getAttribute('href'),'/intake-preview/water'); assert.equal(await fullLink.getAttribute('target'),'_blank');
    await fullLink.click();
    assert.deepEqual(await page.evaluate(()=>window.callbacks.find(row=>row[0]==='material')[1]),{projectId:'alpha',jobId:created.id,materialId:'water'});
    await card.locator('.mia-record summary').click();
    await card.getByRole('button',{name:/^同一笔/}).focus();
    for(let i=0;i<3;i++){
      created.timings={active_seconds:2+i,current_user_wait_seconds:5+i,stages:{}};
      await refresh();
    }
    assert.equal(await card.locator('.mia-record').evaluate(element=>element.open),true,'live telemetry must preserve expanded records');
    assert.equal(await page.evaluate(()=>document.activeElement.classList.contains('mia-choice')),true,'live telemetry must preserve confirmation keyboard focus');
    await page.locator('#manualEdit').focus();
    const beforePosts=posts.length; for(let i=0;i<3;i++)await refresh();
    assert.equal(posts.length,beforePosts,'poll is read-only');
    assert.equal(await page.locator('#manualEdit').inputValue(),'人工修改尚未保存');
    assert.equal(await page.evaluate(()=>document.activeElement.id),'manualEdit','unchanged polling must not disturb manual input');
    assert.equal(await page.evaluate(()=>window.parentLoads),0);

    answerGate={gate:gate(),entered:false}; const answering=answerGate;
    await page.evaluate(()=>{window.manualEditing=true;const button=document.querySelector('.mia-choice');button.click();button.click();});
    for(let i=0;i<50&&!answering.entered;i++)await new Promise(resolve=>setTimeout(resolve,10));
    assert.equal(answering.entered,true); assert.equal(answers.length,1);
    assert.deepEqual(answers[0],{id:created.id,choice:'yes',question_id:'source-question'});
    answering.gate.release();
    await card.waitFor({state:'detached'});
    assert.equal(await page.locator('#agentHost').isVisible(),false,'answering the last question hides the panel');
    await screenshot('answered-hidden');
    await refresh(); await refresh();
    assert.equal(await page.evaluate(()=>window.callbacks.filter(row=>row[0]==='changed').length),1,'new results notify once');
    assert.equal(await page.evaluate(()=>window.parentLoads),0,'parent can delay reload while manual editing');
    assert.equal(await page.evaluate(()=>window.deferredChanges.length),1);
    assert.equal(created.models,1); assert.equal(created.tools,13); assert.equal(created.id,accepted[0].job.id);
    // Navigation on an actionable partial result keeps the original exact-ID contract.
    created.state='needs_input';await refresh();
    await card.getByRole('button',{name:'查看费用',exact:true}).click();
    await card.getByRole('button',{name:'查看报销预览',exact:true}).click();
    const preview=await page.evaluate(()=>window.callbacks.find(row=>row[0]==='preview')[1]);
    assert.deepEqual(preview,{projectId:'alpha',jobId:created.id,itemIds:['expense-alpha'],preview:created.preview});
    assert.deepEqual(await page.evaluate(()=>window.callbacks.find(row=>row[0]==='expense')[1]),{projectId:'alpha',jobId:created.id,itemId:'expense-alpha'});
    assert.equal(await card.getByRole('link',{name:'查看本次 PDF'}).getAttribute('href'),'/export/only-this-job.pdf');
    created.state='ready';await refresh();

    state.jobs.beta=[base('beta','beta-missing',{state:'needs_input',question:{text:'缺少付款原件',material_ids:[],choices:[]}})];
    delayedGet={project:'alpha',gate:gate(),entered:false}; const delayed=delayedGet;
    const oldRead=page.evaluate(()=>window.controller.refresh());
    for(let i=0;i<50&&!delayed.entered;i++)await new Promise(resolve=>setTimeout(resolve,10));
    assert.equal(delayed.entered,true); await select('beta'); delayed.gate.release(); await oldRead;
    await page.getByText('缺少付款原件',{exact:true}).waitFor();
    assert.equal(await page.locator('[data-job-id="'+created.id+'"]').count(),0,'late old-project response cannot reappear');
    assert.equal(await page.getByRole('button',{name:'查看报销预览',exact:true}).count(),0,'old project preview cannot leak');
    assert.match(await page.locator('.mia-jobs').innerText(),/继续使用材料合成台、费用编辑/);
    assert.equal(await page.locator('.mia-choice').count(),0,'missing evidence cannot invent a confirmation');
    assert.equal(await page.getByRole('button',{name:'重新检查对应关系',exact:true}).count(),0,'no partial expense cannot expose legacy recovery');
    Object.assign(state.jobs.beta[0],{item_ids:['hotel-original-draft'],updated_at:123}); await refresh();
    refreshGate={gate:gate(),entered:false}; const rechecking=refreshGate;
    const answersBeforeRefresh=answers.length;
    await page.evaluate(()=>{const button=[...document.querySelectorAll('button')].find(b=>b.textContent==='重新检查对应关系');button.click();button.click();});
    for(let i=0;i<50&&!rechecking.entered;i++)await new Promise(resolve=>setTimeout(resolve,10));
    assert.equal(rechecking.entered,true);
    assert.deepEqual(posts.filter(row=>row.path==='/api/agent/refresh-question'),[{path:'/api/agent/refresh-question',body:{id:'beta-missing',expected_updated_at:123}}]);
    rechecking.gate.release(); await page.getByText('请确认四笔付款与这张住宿发票对应，两笔同额付款独立。',{exact:true}).waitFor();
    assert.equal(answers.length,answersBeforeRefresh,'rechecking must not answer on behalf of the user');
    assert.equal(state.jobs.beta[0].state,'needs_input');
    assert.deepEqual(state.jobs.beta[0].item_ids,['hotel-original-draft']);
    assert.equal(state.jobs.beta[0].models,0); assert.equal(state.jobs.beta[0].tools,0);
    assert.equal(await page.getByRole('button',{name:'重新检查对应关系',exact:true}).count(),0,'a refreshed answerable card cannot be overwritten again');
    assert.equal(await page.getByRole('button',{name:/^同一笔/}).count(),1,'the normal source-bound answer remains a separate action');

    for (const legacy of [false,true]) {
      const id=legacy?'content-legacy':'content-current';
      state.jobs.beta.unshift(base('beta',id,{state:'needs_input',models:1,tools:7,item_ids:['content-expense'],
        question:{text:'购买内容分类尚未确定',...(legacy?{}:{type:'expense_content'})},
        ...(legacy?{last_results:[{tool:'classify_expense',result:{content_status:'needs_input',goods:['鼠标','亚克力导视牌'],decision:{category:null}}}]}:
          {expenses:[{title:'待核对购买凭证',goods:'鼠标、亚克力导视牌',source:'jev',category:'其他',amount_cents:4800}]})}));
      await refresh();
      const card=page.locator('[data-job-id="'+id+'"]');
      assert.equal(await card.getByRole('button',{name:'重新检查对应关系',exact:true}).count(),0);
      await card.getByRole('button',{name:'按新规则检查商品',exact:true}).click();
      await card.waitFor({state:'detached'});
      assert.deepEqual(posts.at(-1),{path:'/api/agent/retry',body:{id}});
      assert.equal(state.jobs.beta[0].models,1); assert.equal(state.jobs.beta[0].tools,7);
      assert.equal(answers.length,answersBeforeRefresh,'a rule recheck cannot fabricate a user fact');
    }
    state.queue=[{id:'beta-eligible',project_id:'beta',status:'received'}, {id:'beta-review',project_id:'beta',status:'review'},
      {id:'beta-linked',project_id:'beta',status:'linked',item_id:'existing'}, {id:'alpha-not-selected',project_id:'alpha',status:'received'},
      {id:'unknown-project',status:'received'},{id:'beta-removed',project_id:'beta',status:'removed'}];
    state.available=false; await settingsChange({auto_after_upload:false,use_model:true,model_available:false});await refresh();
    const explicitStart=page.waitForResponse(response=>new URL(response.url()).pathname==='/api/agent/start');
    await page.getByRole('button',{name:'整理未处理材料',exact:true}).click();await explicitStart;
    await page.waitForFunction(()=>document.querySelector('.mia-controls button').disabled===false);
    const explicit=posts.filter(row=>row.path==='/api/agent/start').at(-1).body;
    assert.deepEqual(explicit.material_ids,['beta-eligible','beta-review']); assert.equal(explicit.project_id,'beta'); assert.equal(explicit.use_model,false);
    assert.equal(await page.locator('[data-job-id="created-3"]').count(),0,'manual mode also omits queued cards');
    assert.equal(state.jobs.beta.find(row=>row.id==='created-3').state,'queued');
    state.jobs.beta=[...state.jobs.beta.filter(row=>row.state==='queued')];
    await settingsChange({auto_after_upload:true});await refresh();
    assert.equal(await page.locator('#agentHost').isVisible(),false);
    state.jobs.beta.unshift(base('beta','retry-job',{state:'failed',models:2,tools:18,item_ids:['existing-partial-draft']})); await refresh();
    await page.locator('[data-job-id="retry-job"]').getByRole('button',{name:'按剩余额度重试'}).click();
    await page.locator('[data-job-id="retry-job"]').waitFor({state:'detached'});
    assert.equal(await page.locator('#agentHost').isVisible(),false,'successful job retry hides the panel');
    assert.equal(state.jobs.beta.find(row=>row.id==='retry-job').models,2); assert.equal(state.jobs.beta.find(row=>row.id==='retry-job').tools,18);
    assert.deepEqual(posts.at(-1),{path:'/api/agent/retry',body:{id:'retry-job'}});
    state.jobs.beta.find(row=>row.id==='retry-job').state='failed'; await refresh();
    assert.equal(await page.locator('#agentHost').isVisible(),true);
    state.jobs.beta.find(row=>row.id==='retry-job').state='stopped';await refresh();
    await page.getByRole('button',{name:'按剩余额度重试',exact:true}).waitFor();
    assert.equal(await page.evaluate(()=>window.callbacks.filter(row=>row[0]==='changed').length),1,'retry retaining the same partial draft is not a new write');
    const bounded=await page.evaluate(async()=>{try{await window.controller.startReceived({projectId:'beta',entryIds:Array.from({length:21},(_,index)=>'too-many-'+index),batch:'overflow'});return '';}catch(reason){return reason.message;}});
    assert.match(bounded,/最多整理20份/); assert.deepEqual(posts.at(-1),{path:'/api/agent/retry',body:{id:'retry-job'}},'limit failure cannot write');

    await page.setViewportSize({width:390,height:844});
    created.state='needs_input';await select('alpha');
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),true,'390px workbench module cannot overflow');
    assert.equal(await page.locator('.mia-check input').count(),0);
    state.failSettings=true;await page.evaluate(()=>{window.mountTest();window.controller.open();});await refresh();await page.waitForTimeout(50);
    assert.equal(await page.evaluate(()=>window.controller.shouldAutoOrganize('alpha')),true,'settings read failures retain default automatic mode');
    await page.getByRole('button',{name:'查看报销预览',exact:true}).first().click();
    await select(''); assert.equal(await page.locator('#agentHost').isVisible(),false);
    assert.equal(await page.evaluate(()=>window.controller.shouldAutoOrganize('')),false);
    assert.equal(await page.locator('#originalControl').count(),1,'manual workbench remains in place');
    created.state='ready';await select('alpha');
    await page.evaluate(()=>window.failChanged=true);
    const callbackJob=base('alpha','callback-error',{state:'running'});state.jobs.alpha.unshift(callbackJob);await refresh();
    assert.equal(await page.locator('#agentHost').isVisible(),false,'running tasks stay hidden');
    Object.assign(callbackJob,{state:'ready',item_ids:['callback-expense']});await refresh();
    await page.getByRole('alert').filter({hasText:'合成结果回调失败'}).waitFor();
    assert.equal(await page.locator('.manual-invoice-agent').evaluate(element=>element.open),true,'operation errors reveal an expanded panel without actionable jobs');
    assert.equal(await page.locator('.mia-job').count(),0);
    await page.evaluate(()=>window.failChanged=false);
    await page.evaluate(()=>window.controller.startReceived({projectId:'alpha',entryIds:['error-recovery'],batch:'error-recovery'}));
    await refresh();assert.equal(await page.locator('#agentHost').isVisible(),false,'successful operation clears the error panel');
    // Busy starts persist across a real reload, then drain in upload order.
    state.jobs.alpha=[base('alpha','synthetic-existing',{state:'running'})];
    state.startError={status:409,body:{error:'当前项目已有任务，请等待或取消后再开始',code:'project_busy'}};
    const busyOffset=posts.length;
    const busyRequest={projectId:'alpha',entryIds:['busy-first'],batch:'busy-batch'};
    assert.deepEqual(await page.evaluate(request=>window.controller.startReceived(request),busyRequest),{queued:true});
    const busyBody=posts.at(-1).body;
    await page.getByText('1 份材料已保存，等当前整理结束后自动开始。',{exact:true}).waitFor();
    assert.equal(await page.locator('.mia-summary-status').innerText(),'等待项目空闲');
    assert.equal(await page.locator('.mia-error').isVisible(),false);
    assert.equal(await page.getByRole('button',{name:'重试启动本批整理'}).count(),0);
    await page.evaluate(()=>window.controller.startReceived({projectId:'alpha',entryIds:['busy-second'],batch:'busy-batch'}));
    await page.evaluate(()=>window.controller.startReceived({projectId:'alpha',entryIds:['busy-third'],batch:'independent-batch'}));
    const thirdBody=posts.at(-1).body;
    await page.reload();await refresh();
    assert.equal(await page.locator('.mia-pending-starts p').count(),2);
    assert.equal(await page.locator('.mia-summary-status').innerText(),'等待项目空闲');
    assert.equal(posts.slice(busyOffset).length,2,'running project cannot trigger polling starts');
    state.jobs.alpha=[];
    await refresh();
    await page.waitForFunction(()=>JSON.parse(localStorage.getItem('invoice-manual-agent-v1:requests:alpha')).find(row=>row.batch==='busy-batch').waitingForIdle);
    await page.waitForTimeout(50);
    assert.equal(posts.slice(busyOffset).length,3,'idle polling retries even when the server reports busy again');
    assert.deepEqual(posts.at(-1).body,busyBody,'another busy response preserves the original request');
    assert.equal(await page.locator('.mia-error').isVisible(),false);
    state.startError=null;
    await refresh();
    await page.waitForFunction(()=>JSON.parse(localStorage.getItem('invoice-manual-agent-v1:requests:alpha')).find(row=>row.batch==='busy-batch').accepted);
    assert.deepEqual(posts.at(-1).body,busyBody,'idle retry preserves the complete body and idempotency key');
    const afterFirst=posts.length;
    await refresh();assert.equal(posts.length,afterFirst,'next batch waits for the accepted job to finish');
    state.jobs.alpha=[];await refresh();
    await page.waitForFunction(()=>JSON.parse(localStorage.getItem('invoice-manual-agent-v1:requests:alpha')).find(row=>row.ids.includes('busy-second')).accepted);
    assert.deepEqual(posts.at(-1).body.material_ids,['busy-second'],'chained batch precedes later independent upload');
    state.jobs.alpha=[];await refresh();
    await page.waitForFunction(()=>JSON.parse(localStorage.getItem('invoice-manual-agent-v1:requests:alpha')).find(row=>row.ids.includes('busy-third')).accepted);
    assert.deepEqual(posts.at(-1).body,thirdBody);
    // Other failures during an automatic retry retain the existing manual recovery.
    state.startError={status:409,body:{error:'项目忙',code:'project_busy'}};
    await page.evaluate(()=>window.controller.startReceived({projectId:'alpha',entryIds:['busy-failure'],batch:'busy-failure'}));
    state.startError={status:400,body:{error:'材料已关联、移除或不属于当前项目，请刷新'}};
    state.jobs.alpha=[];await refresh();
    await page.getByRole('button',{name:'重试启动本批整理',exact:true}).waitFor();
    assert.equal(await page.locator('.mia-summary-status').innerText(),'需要处理');
    assert.match(await page.locator('.mia-error').innerText(),/材料已关联/);
    // Only a successful server snapshot can retire an unaccepted request.
    state.pendingIds={alpha:[]};await refresh();
    assert.equal(await page.locator('#agentHost').isVisible(),false,'retiring a failed start clears its own error');
    state.startError={status:503,body:{error:'Synthetic start failure'}};
    await page.evaluate(()=>window.controller.startReceived({projectId:'alpha',entryIds:['linked-proof','pending-proof'],batch:'stale-check'}).catch(()=>{}));
    state.queue=[{id:'linked-proof',project_id:'alpha',status:'review',item_id:'synthetic-expense'},
      {id:'pending-proof',project_id:'alpha',status:'received',item_id:''}];
    state.pendingIds.alpha=['pending-proof'];
    const failedRecord=await page.evaluate(()=>JSON.parse(localStorage.getItem('invoice-manual-agent-v1:requests:alpha')).find(row=>row.batch==='stale-check'));
    await page.reload();await refresh();
    await page.getByRole('button',{name:'重试启动本批整理',exact:true}).waitFor();
    assert.deepEqual(await page.evaluate(()=>JSON.parse(localStorage.getItem('invoice-manual-agent-v1:requests:alpha')).find(row=>row.batch==='stale-check')),failedRecord,'one pending material preserves the entire request');
    await page.getByRole('button',{name:'重试启动本批整理',exact:true}).click();
    await page.getByRole('alert').filter({hasText:'Synthetic start failure'}).waitFor();
    state.queue[1].item_id='synthetic-expense';state.pendingIds.alpha=[];
    state.failGet=2;await refresh();
    assert.equal(await page.evaluate(()=>JSON.parse(localStorage.getItem('invoice-manual-agent-v1:requests:alpha')).some(row=>row.batch==='stale-check')),true,'failed reads cannot delete records');
    state.failGet=0;
    const pendingIds=state.pendingIds;delete state.pendingIds;await refresh();
    assert.equal(await page.getByRole('button',{name:'重试启动本批整理',exact:true}).count(),1,'missing state cannot delete records');
    state.pendingIds=pendingIds;await refresh();
    assert.equal(await page.locator('#agentHost').isVisible(),false,'retiring a manually retried request clears its propagated error');
    await page.reload();await refresh();
    assert.equal(await page.evaluate(()=>JSON.parse(localStorage.getItem('invoice-manual-agent-v1:requests:alpha')).some(row=>row.batch==='stale-check')),false);
    assert.equal(await page.locator('#agentHost').isVisible(),false,'linked materials retire the failed request after reload');
    state.startError={status:409,body:{error:'Synthetic busy',code:'project_busy'}};
    await page.evaluate(()=>window.controller.startReceived({projectId:'alpha',entryIds:['retired-waiting'],batch:'retired-waiting'}));
    const beforeRetiring=posts.length;await refresh();
    assert.equal(posts.length,beforeRetiring,'obsolete waiting starts are removed before automatic retry');
    assert.equal(await page.evaluate(()=>JSON.parse(localStorage.getItem('invoice-manual-agent-v1:requests:alpha')).some(row=>row.batch==='retired-waiting')),false);
    assert.equal(await page.locator('#agentHost').isVisible(),false);
    assert.deepEqual(errors,[]);
    return {passed:true,obsoleteFailedStartsRemoved:true,pendingFailedStartsPreserved:true,failedReadsPreserveStarts:true,obsoleteWaitingStartsRemoved:true,
      projectBusyWaitPersists:true,busyRetryKeepsIdentity:true,waitingBatchesStartInOrder:true,busyRetryOtherErrorVisible:true,
      globalPreferences:true,settingsFailureDefaults:true,noProjectPanelHidden:true,configuredModelOnly:true,oneBatchOneStart:true,networkRetryKeepsIdentity:true,
      pollingNeverPosts:true,pollingKeepsManualInput:true,parentControlsManualEditRefresh:true,sourceBoundAnswer:true,sameJobRemainingBudget:true,
      exactPreviewCallbacks:true,projectLateResponseFence:true,missingEvidenceNeedsManual:true,guardedLegacyRefresh:true,unlinkedCurrentProjectOnly:true,jobRetry:true,
      normalPanelHidden:true,questionsAutoExpand:true,answeredPanelHidden:true,startFailureVisible:true,retrySuccessHidden:true,manualModeEntry:true,operationErrorVisible:true,mobile390:true,pageErrors:errors};
  } finally { await browser.close(); }
}
module.exports=run;
if(require.main===module)run(require('playwright')).then(result=>console.log(JSON.stringify(result))).catch(reason=>{console.error(reason);process.exitCode=1;});
