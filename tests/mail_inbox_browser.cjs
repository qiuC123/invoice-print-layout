// F21 uses only intercepted synthetic mail, ledger and organizer responses.
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
async function run(pw) {
  const browser = await pw.chromium.launch({headless:true});
  const page = await browser.newPage({viewport:{width:1500,height:1000}});
  page.setDefaultTimeout(10000);
  const origin = 'http://127.0.0.1:18932', errors = [], writes = [];
  const evidence = path.resolve(process.env.INVOICE_BROWSER_EVIDENCE || 'tmp/mail-inbox-browser');
  const state = {app_mode:'invoice',token:'synthetic-token',projects:[{id:'p1',name:'合成项目甲'},{id:'p2',name:'合成项目乙'}],
    items:[],inbox:[],categories:['其他'],roles:{invoice:'发票'},stages:{draft:'待核对'}};
  const mail = {enabled:true,address:'synthetic@163.com',messages:[
    {id:'mail-a',subject:'<img src=x onerror="throw 1"> 合成发票',sender:'合成发件人',sent_date:'2026-10-21',
      status:'unassigned',project_id:'',material_count:2,attachments:[{name:'invoice.pdf',index:0},{name:'付款.png',index:1}]},
    {id:'mail-b',subject:'不需要的合成邮件',sender:'另一合成发件人',received_at:1793300000,
      status:'unassigned',project_id:'',material_count:1,attachments:[{name:'other.pdf',index:0}]},
    {id:'attention',subject:'登录提醒不参与分配',status:'attention',project_id:'',material_count:1},
    {id:'empty',subject:'没有材料',status:'unassigned',project_id:'',material_count:0},
    {id:'assigned',subject:'已有项目',status:'unassigned',project_id:'p2',material_count:1},
  ]};
  let boardReads=0, stateReads=0, materialReady=false, rejectDismiss=false;
  const board = () => ({vouchers:[],payments:[],other:materialReady?[{id:'from-mail',key:'q:from-mail',project_id:'p1',
    name:'invoice.pdf',role:'detail',status:'review',revision:1,fields:{},issues:[]}]:[],matches:[],suggestions:[],report_types:[]});
  page.on('pageerror',error=>errors.push(error.message));
  await page.context().route(origin+'/**',async route=>{
    const req=route.request(),url=new URL(req.url());
    const json=(value,status=200)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(value)});
    if(req.method()==='POST'){
      assert.equal(req.headers()['x-workbench-token'],'synthetic-token');
      const body=req.postDataJSON();writes.push({path:url.pathname,body});
      const record=mail.messages.find(message=>message.id===body.id);
      if(url.pathname==='/api/agent/mail/assign'){
        assert.deepEqual(body,{id:'mail-a',project_id:'p1'});
        Object.assign(record,{status:'waiting_agent',project_id:body.project_id});return json(mail);
      }
      if(url.pathname==='/api/agent/mail/dismiss'){
        assert.deepEqual(body,{id:'mail-b'});
        if(rejectDismiss)return json({error:'合成忽略失败'},400);
        record.status='dismissed';return json(mail);
      }
      assert.fail('Unexpected write '+url.pathname);
    }
    if(url.pathname==='/api/runtime')return json({started_at:'synthetic-start',build_id:'synthetic-build'});
    if(url.pathname==='/api/state'){stateReads++;return json(state);}
    if(url.pathname==='/api/agent/mail')return json(mail);
    if(url.pathname==='/api/agent/mail/attachment'){
      assert.equal(url.searchParams.get('id'),'mail-a');
      if(url.searchParams.get('index')==='1')return route.fulfill({contentType:'image/png',
        body:Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j1ioAAAAASUVORK5CYII=','base64')});
      assert.equal(url.searchParams.get('index'),'0');
      return route.fulfill({contentType:'application/pdf',body:Buffer.from('%PDF-1.4\n%%EOF')});
    }
    if(url.pathname==='/api/material-matching'){boardReads++;assert.equal(url.searchParams.get('project'),'p1');return json(board());}
    if(url.pathname==='/api/intake')return json({queue:[]});
    if(url.pathname==='/api/agent/jobs')return json({jobs:[],model:{available:false}});
    if(url.pathname==='/api/settings')return json({organize:{auto_after_upload:true,use_model:false,model_available:false}});
    if(url.pathname==='/api/reimbursement-people')return json({people:[],summary:{rows:[]}});
    const name=url.pathname==='/'?'index.html':url.pathname.slice(1);
    if(!/^[a-z-]+\.(html|js|css)$/.test(name))return route.abort();
    return route.fulfill({contentType:name.endsWith('.js')?'text/javascript':name.endsWith('.css')?'text/css':'text/html',
      body:await fs.readFile(path.resolve(__dirname,'../src/invoice_print_layout/web',name))});
  });
  const section=page.locator('#matchingUnassignedMail');
  const ready=()=>page.waitForFunction(()=>document.getElementById('matchingDeskDialog').open&&document.getElementById('matchingDeskDialog').getAttribute('aria-busy')==='false');
  try {
    await fs.mkdir(evidence,{recursive:true});await page.goto(origin+'/?project=p1');
    const prompt=page.locator('#manualMailStatus').getByRole('button',{name:'2 封邮件待选项目',exact:true});
    await prompt.waitFor();await prompt.click();await ready();
    await section.getByRole('heading',{name:'邮箱收到、还没选项目的邮件（2）',exact:true}).waitFor();
    assert.equal(await section.locator('article').count(),2);
    assert.equal(await section.locator('img').count(),0,'Mail text must be escaped');
    assert.match(await section.innerText(),/合成发件人 · 2026-10-21/);
    assert.equal(await page.evaluate(()=>document.activeElement.id),'matchingUnassignedMail');
    const original=section.getByRole('link',{name:'invoice.pdf',exact:true});
    assert.equal(await original.getAttribute('href'),'/api/agent/mail/attachment?id=mail-a&index=0');
    assert.equal(await original.getAttribute('target'),'_blank');
    assert.equal(await original.getAttribute('rel'),'noopener');
    // The actual local URL is readable independently of the project material pool.
    assert.match(await page.evaluate(async()=>await (await fetch('/api/agent/mail/attachment?id=mail-a&index=0')).text()),/^%PDF/);
    const opened=page.waitForEvent('popup');await section.getByRole('link',{name:'付款.png',exact:true}).click();
    const originalTab=await opened;await originalTab.waitForLoadState();
    assert.equal(originalTab.url(),origin+'/api/agent/mail/attachment?id=mail-a&index=1');await originalTab.close();
    await page.screenshot({path:path.join(evidence,'unassigned-mail.png')});
    const beforeBoard=boardReads,beforeState=stateReads;
    await section.locator('[data-mail-id="mail-a"]').getByRole('button',{name:'归入本项目',exact:true}).click();
    await section.getByRole('heading',{name:'邮箱收到、还没选项目的邮件（1）',exact:true}).waitFor();
    await page.waitForFunction(()=>document.querySelector('#matchingUnassignedMail button')?.disabled===false);
    assert.ok(boardReads>beforeBoard&&stateReads>beforeState,'Assignment refreshes both the desk and ledger');
    await page.locator('#matchingClose').click();
    await page.locator('#manualMailStatus').getByRole('button',{name:'1 封邮件待选项目',exact:true}).waitFor();
    // Completion after the first refresh becomes visible in the still-open desk.
    await page.locator('#materialMatching').click();await ready();
    materialReady=true;Object.assign(mail.messages[0],{status:'ready',job_id:'synthetic-job'});
    await page.evaluate(()=>window.dispatchEvent(new CustomEvent('invoice-mail-refresh')));
    await page.locator('#matchingOther [data-support-id="from-mail"]').waitFor();
    rejectDismiss=true;
    await section.getByRole('button',{name:'不需要',exact:true}).click();
    await page.locator('#matchingDeskDialog').getByText('合成忽略失败',{exact:true}).waitFor();
    assert.equal(await section.locator('article').count(),1,'Failed writes preserve the pending card');
    rejectDismiss=false;
    await section.getByRole('button',{name:'不需要',exact:true}).click();
    await section.waitFor({state:'hidden'});
    await page.locator('#matchingDeskDialog').getByText('邮件已忽略，原件保留。',{exact:true}).waitFor();
    assert.equal(await page.locator('#matchingDeskDialog .dialog-message.is-error').count(),0,'Successful retry clears the old error');
    await page.locator('#matchingClose').click();
    assert.equal(await page.locator('#manualMailStatus').getByRole('button',{name:/封邮件待选项目/}).count(),0);
    await page.locator('#materialMatching').click();await ready();
    assert.equal(await section.isVisible(),false,'Attention, empty and assigned records never create an inbox section');
    await page.screenshot({path:path.join(evidence,'no-unassigned-mail.png')});
    await page.locator('#matchingClose').click();
    await page.reload();await page.waitForFunction(()=>!!state.token);await page.locator('#materialMatching').click();await ready();
    assert.equal(await section.isVisible(),false,'Empty state stays hidden after reload');
    assert.equal(writes.length,3);assert.deepEqual(errors,[]);
    const result={passed:true,actionableOnly:true,originalLinks:true,safeText:true,assignmentRefresh:true,
      laterOrganizerCompletionRefresh:true,dismissalRetryAndRemoval:true,emptyStateHidden:true,writes,pageErrors:errors};
    await fs.writeFile(path.join(evidence,'mail-inbox-browser.json'),JSON.stringify(result,null,2));return result;
  } finally {await browser.close();}
}
module.exports=run;
if(require.main===module)run(require('playwright')).then(result=>console.log(JSON.stringify(result))).catch(error=>{console.error(error);process.exitCode=1;});
