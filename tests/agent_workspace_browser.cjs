// Verify the integrated Agent page across project refresh, creation, details and upload races.
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');

async function run(pw) {
  const browser = await pw.chromium.launch({headless:true});
  const origin = 'http://127.0.0.1:18995';
  const cases = [];
  async function scenario(initiallyEmpty) {
    const page = await browser.newPage({viewport:{width:1260,height:960}});
    const errors=[],writes=[],projects=initiallyEmpty?[]:[{id:'p-old',name:'已有项目'},{id:'p-mail',name:'邮件专用项目'}];
    const newProject={id:'p-new',name:'新建测试项目',site_name:'南昌'};
    const mailDefault=initiallyEmpty?'':'p-mail';
    let holdState=false,heldStates=[],nextIntakeHeld=false,releaseIntake,releaseUpload,created=false,uploadWaiting=false;
    const allMaterials=()=>projects.map(project=>({id:'m-'+project.id,project_id:project.id,name:project.name+'购买凭证.png',source:'upload',role:'purchase',status:'review',issues:[],fields:{goods:project.name+'鼠标',amount:'17.00',merchant:'合成测试商店'}}));
    const allExpenses=()=>projects.map(project=>({id:'e-'+project.id,project_id:project.id,project_name:project.name+'办公鼠标',amount_cents:1700,goods:'鼠标',category:'办公用品',merchant:'合成测试商店',stage:'draft',complete:true,verified:false,attachments:[]}));
    await page.addInitScript(()=>{window.testIntervals=[];window.setInterval=(fn,ms)=>{window.testIntervals.push({fn,ms});return window.testIntervals.length;};});
    page.on('pageerror',error=>errors.push(error.message));
    await page.route(origin+'/**',async route=>{
      const req=route.request(),url=new URL(req.url());
      const json=data=>route.fulfill({contentType:'application/json',body:JSON.stringify(data)});
      if(url.pathname==='/api/state'){
        const snapshot={token:'synthetic',projects:projects.map(project=>({...project})),items:allExpenses()};
        if(holdState)await new Promise(resolve=>heldStates.push(resolve));
        return json(snapshot);
      }
      if(url.pathname==='/api/intake'){
        const snapshot={queue:allMaterials()};
        if(nextIntakeHeld){
          nextIntakeHeld=false;
          snapshot.queue=snapshot.queue.filter(entry=>entry.project_id==='p-old');
          await new Promise(resolve=>{releaseIntake=resolve;});
        }
        return json(snapshot);
      }
      if(url.pathname==='/api/agent/jobs')return json({token:'synthetic',model:{available:false},jobs:[]});
      if(url.pathname==='/api/agent/mail')return json({enabled:false,address:'synthetic@163.com',folder:'INBOX',default_project_id:mailDefault,messages:[{id:'mail-unassigned',subject:'待定项目测试邮件',status:'unassigned',material_count:1,project_id:'',job_id:'',issues:[]}]});
      if(req.method()==='POST'){
        assert.equal(req.headers()['x-workbench-token'],'synthetic');
        const body=req.postDataJSON();writes.push({path:url.pathname,body});
        if(url.pathname==='/api/projects'){
          assert.deepEqual(body,{name:newProject.name,site_name:newProject.site_name});
          projects.push(newProject);created=true;return json(newProject);
        }
        if(url.pathname==='/api/intake/receive'){
          assert.equal(body.project_id,'p-new');uploadWaiting=true;
          await new Promise(resolve=>{releaseUpload=resolve;});
          return json({id:'upload-new',project_id:'p-new',status:'review',item_id:''});
        }
        if(url.pathname==='/api/agent/start'){
          assert.equal(body.project_id,'p-new');assert.deepEqual(body.material_ids,['upload-new']);
          return json({id:'j-upload',project_id:'p-new',state:'queued'});
        }
        throw new Error('Unexpected mutation '+url.pathname);
      }
      const asset=url.pathname==='/agent'?'agent.html':url.pathname.slice(1);
      if(!/^[a-z-]+\.(html|js|css)$/.test(asset))return route.abort();
      return route.fulfill({contentType:asset.endsWith('.html')?'text/html':asset.endsWith('.js')?'text/javascript':'text/css',body:await fs.readFile(path.resolve(__dirname,'../src/invoice_print_layout/web',asset))});
    });
    async function waitFor(predicate) {
      for(let attempt=0;attempt<100;attempt++){
        if(predicate())return;
        await new Promise(resolve=>setTimeout(resolve,10));
      }
      assert.ok(predicate(),'Expected mocked request to begin');
    }
    try {
      await page.goto(origin+'/agent');
      await page.getByText('synthetic@163.com · INBOX',{exact:true}).waitFor();
      assert.equal(await page.locator('#mail-project').inputValue(),mailDefault);
      if(initiallyEmpty)assert.equal(await page.locator('#upload').isDisabled(),true);
      else {
        assert.equal(await page.locator('#project').inputValue(),'p-old');
        await page.locator('.mail-message select').selectOption('p-old');
      }
      await page.waitForFunction(()=>window.testIntervals.length===2);
      holdState=true;
      await page.evaluate(()=>testIntervals.find(interval=>interval.ms===5000).fn());
      await waitFor(()=>heldStates.length>=(initiallyEmpty?1:2));
      await page.getByRole('button',{name:'＋ 新建项目',exact:true}).click();
      await page.locator('#agent-projects-name').fill(newProject.name);
      await page.locator('#agent-projects-site').fill(newProject.site_name);
      await page.getByRole('button',{name:'创建项目',exact:true}).click();
      await waitFor(()=>created);
      await page.getByRole('button',{name:'正在创建…',exact:true}).waitFor();
      assert.equal(await page.locator('#agent-projects-dialog').isVisible(),true,'Create waits for in-flight project polling and refreshes again');
      holdState=false;heldStates.splice(0).forEach(resolve=>resolve());
      await page.locator('#agent-projects-dialog').waitFor({state:'hidden'});
      assert.equal(await page.locator('#project').inputValue(),'p-new');
      assert.equal(await page.locator('#upload').isEnabled(),true,'First project creation must enable uploading');
      assert.equal(await page.locator('#empty-project').isHidden(),true);
      assert.equal(await page.locator('#workspace-title').innerText(),newProject.name);
      assert.equal(await page.locator('[data-project-id="p-new"]').getAttribute('aria-current'),'page');
      assert.equal(await page.locator('#manual').getAttribute('href'),'/?project=p-new');
      assert.equal(await page.locator('#mail-project').inputValue(),mailDefault,'Current project must not rewrite mailbox default');
      assert.equal(await page.locator('.mail-message select').inputValue(),initiallyEmpty?'':'p-old','Refreshing project options preserves explicit unassigned-mail selection');
      assert.equal(await page.locator('.mail-message select option[value="p-new"]').count(),1);
      assert.equal(writes.filter(write=>write.path.includes('/mail/')).length,0,'Creating a project does not assign mail');

      if(!initiallyEmpty){
        nextIntakeHeld=true;
        await page.locator('#project').selectOption('p-old');await waitFor(()=>!!releaseIntake);
        await page.locator('#project').selectOption('p-new');
        await page.locator('#workspace-materials [data-detail-id="m-p-new"]').waitFor();
        const oldResponse=page.waitForResponse(response=>new URL(response.url()).pathname==='/api/intake');
        releaseIntake();releaseIntake=null;await oldResponse;
        await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
        assert.equal(await page.locator('#workspace-materials [data-detail-id="m-p-old"]').count(),0);
        assert.equal(await page.locator('#workspace-expenses [data-detail-id="e-p-old"]').count(),0);
        assert.equal(await page.locator('#workspace-expenses [data-detail-id="e-p-new"]').count(),1);
        assert.equal(await page.locator('#mail-project').inputValue(),'p-mail');
      }

      await page.locator('#files').setInputFiles({name:'synthetic.png',mimeType:'image/png',buffer:Buffer.from('synthetic-only')});
      await waitFor(()=>uploadWaiting);
      assert.equal(await page.locator('#project').isDisabled(),true);
      assert.equal(await page.getByRole('button',{name:'＋ 新建项目',exact:true}).isDisabled(),true);
      for(const button of await page.locator('.agent-projects-item').all())assert.equal(await button.isDisabled(),true);
      assert.equal(await page.locator('#agent-projects-select').isDisabled(),true);
      releaseUpload();releaseUpload=null;
      await page.getByText('材料已保存到电脑，后台正在整理',{exact:true}).waitFor();
      assert.equal(await page.locator('#upload').isEnabled(),true);
      assert.equal(writes.filter(write=>write.path==='/api/projects').length,1);
      assert.equal(writes.filter(write=>write.path==='/api/agent/start').length,1);
      assert.equal(await page.locator('#error').isVisible(),false);
      assert.deepEqual(errors,[]);
      return {initiallyEmpty,createdDuringPolling:true,createdProjectSelected:true,firstProjectUploadEnabled:true,mailDefaultIndependent:true,
        unassignedMailSelectionPreserved:true,creationDoesNotAssignMail:true,staleDetailsSuppressed:initiallyEmpty?'not-applicable':true,busyLocksProjectNavigation:true,uploadKeepsSelectedProject:true,pageErrors:errors};
    } finally {
      holdState=false;heldStates.splice(0).forEach(resolve=>resolve());
      if(releaseIntake)releaseIntake();if(releaseUpload)releaseUpload();
      await page.close();
    }
  }
  try {cases.push(await scenario(true));cases.push(await scenario(false));return {passed:true,cases};}
  finally {await browser.close();}
}
module.exports=run;
if(require.main===module)run(require('playwright')).then(result=>console.log(JSON.stringify(result))).catch(error=>{console.error(error);process.exitCode=1;});
