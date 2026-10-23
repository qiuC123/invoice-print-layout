// Exercise the real project navigation assets with synthetic local API responses only.
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');

async function run(pw) {
  const browser = await pw.chromium.launch({headless:true});
  const page = await browser.newPage({viewport:{width:1120,height:900}});
  const origin = 'http://127.0.0.1:18994', errors = [], writes = [];
  let fail = false, hold = false, release;
  page.on('pageerror', error => errors.push(error.message));
  const harness = `<!doctype html><html lang="zh-CN" class="invoice-assistant" data-theme="light"><head><meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/workbench-theme.css">
    <link rel="stylesheet" href="/agent.css"><link rel="stylesheet" href="/agent-projects.css">
    <style>body{padding:20px}#project-navigation{width:260px;max-width:100%}@media(max-width:760px){#project-navigation{width:100%}}</style>
    </head><body><aside id="project-navigation"></aside><p>其余工作台内容</p><script src="/agent-projects.js"></script><script>
    window.fixture={projects:[],current:'',busy:false,created:[],selected:[],callbackFailure:false};
    window.projectNavigation=AgentProjects.mount({host:document.getElementById('project-navigation'),isBusy:()=>fixture.busy,
      api:async(path,body)=>{const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json','X-Workbench-Token':'synthetic'},body:JSON.stringify(body)});const data=await r.json();if(!r.ok)throw Error(data.error);return data;},
      onCreated:async(project)=>{fixture.created.push(project.id);fixture.projects.push(project);if(fixture.callbackFailure)throw Error('refresh');fixture.current=project.id;projectNavigation.update(fixture.projects,fixture.current);},
      onSelect:async(id)=>{fixture.selected.push(id);fixture.current=id;projectNavigation.update(fixture.projects,id);}});
    </script></body></html>`;
  await page.route(origin+'/**', async route => {
    const request = route.request(), url = new URL(request.url());
    if (url.pathname === '/api/projects') {
      assert.equal(request.method(), 'POST'); assert.equal(request.headers()['x-workbench-token'], 'synthetic');
      const fields = request.postDataJSON(); writes.push(fields);
      if (hold) await new Promise(resolve => {release=resolve;});
      if (fail) return route.fulfill({status:400,contentType:'application/json',body:JSON.stringify({error:'项目名称已存在，请选择其他名称'})});
      return route.fulfill({contentType:'application/json',body:JSON.stringify({id:'p'+writes.length,...fields})});
    }
    if (url.pathname === '/fixture') return route.fulfill({contentType:'text/html',body:harness});
    const name = url.pathname.slice(1);
    if (!/^[a-z-]+\.(js|css)$/.test(name)) return route.abort();
    return route.fulfill({contentType:name.endsWith('.js')?'text/javascript':'text/css',body:await fs.readFile(path.resolve(__dirname,'../src/invoice_print_layout/web',name))});
  });
  const create = page.getByRole('button',{name:'＋ 新建项目',exact:true});
  const dialog = page.locator('#agent-projects-dialog');
  const name = page.locator('#agent-projects-name'), site = page.locator('#agent-projects-site');
  const submit = page.getByRole('button',{name:'创建项目',exact:true});
  try {
    await page.goto(origin+'/fixture'); assert.deepEqual(errors, [], 'Project assets initialize without page errors');
    assert.equal(await page.locator('.agent-projects-mobile').isVisible(),false,'Desktop must hide the mobile project selector despite theme label rules');
    await page.getByText('还没有项目，先新建一个项目开始整理。',{exact:true}).waitFor();
    assert.equal(await create.isEnabled(),true); assert.equal(await page.locator('#agent-projects-select').isDisabled(),true);
    await create.click(); assert.equal(await name.evaluate(node=>node===document.activeElement),true);
    assert.equal(await submit.isDisabled(),true);
    await name.fill('   '); assert.equal(await submit.isDisabled(),true);
    await name.fill('第一测试项目'); await site.fill('南昌');
    await page.evaluate(()=>projectNavigation.update([],''));
    assert.equal(await name.inputValue(),'第一测试项目'); assert.equal(await site.inputValue(),'南昌');
    await submit.click(); await dialog.waitFor({state:'hidden'});
    assert.deepEqual(writes[0],{name:'第一测试项目',site_name:'南昌'});
    assert.equal(await page.locator('[data-project-id="p1"]').getAttribute('aria-current'),'page');
    assert.equal(await create.evaluate(node=>node===document.activeElement),true);

    await page.evaluate(()=>{
      fixture.projects.push({id:'p2',name:'<img src=x onerror="throw 1"> 第二项目',site_name:'上海'});
      projectNavigation.update(fixture.projects,fixture.current);
    });
    assert.equal(await page.locator('#project-navigation img').count(),0);
    const search = page.locator('#agent-projects-search');
    await search.fill('上海'); assert.equal(await page.locator('.agent-projects-item').count(),1);
    await page.evaluate(()=>projectNavigation.update(fixture.projects,fixture.current));
    assert.equal(await search.inputValue(),'上海');
    await page.locator('[data-project-id="p2"]').click();
    assert.equal(await page.locator('[data-project-id="p2"]').getAttribute('aria-current'),'page');
    assert.deepEqual(await page.evaluate(()=>fixture.selected),['p2']);
    await search.fill('不存在'); await page.getByText('没有找到匹配的项目',{exact:true}).waitFor();
    await search.fill('');

    await page.evaluate(()=>projectNavigation.setBusy(true));
    assert.equal(await create.isDisabled(),true);
    assert.equal(await page.locator('[data-project-id="p1"]').isDisabled(),true);
    assert.equal(await page.locator('#agent-projects-select').isDisabled(),true);
    await page.evaluate(()=>document.querySelector('[data-project-id="p1"]').dispatchEvent(new MouseEvent('click',{bubbles:true})));
    assert.deepEqual(await page.evaluate(()=>fixture.selected),['p2']);
    await page.evaluate(()=>{fixture.busy=true;projectNavigation.setBusy(false);}); assert.equal(await create.isDisabled(),true);
    await page.evaluate(()=>{fixture.busy=false;projectNavigation.setBusy(false);});
    await create.click(); await name.fill('未提交'); await page.keyboard.press('Escape');
    await dialog.waitFor({state:'hidden'}); assert.equal(await create.evaluate(node=>node===document.activeElement),true);
    assert.equal(writes.length,1);

    fail=true;
    await create.click(); await name.fill('重复项目'); await site.fill('保留地点'); await submit.click();
    await page.getByText('项目名称已存在，请选择其他名称',{exact:true}).waitFor();
    assert.equal(await name.inputValue(),'重复项目'); assert.equal(await site.inputValue(),'保留地点'); assert.equal(await submit.isEnabled(),true);
    await page.getByRole('button',{name:'取消',exact:true}).click(); await dialog.waitFor({state:'hidden'});
    fail=false; hold=true;
    await create.click(); await name.fill('第三测试项目'); await submit.click();
    await page.waitForFunction(()=>document.querySelector('#agent-projects-name').disabled);
    await page.evaluate(()=>{const form=document.querySelector('#agent-projects-dialog form');form.dispatchEvent(new Event('submit',{bubbles:true,cancelable:true}));});
    await page.keyboard.press('Escape'); assert.equal(await dialog.isVisible(),true);
    assert.equal(writes.length,3,'One API write for repeated submit during an in-flight create');
    hold=false; release(); await dialog.waitFor({state:'hidden'});
    assert.equal(await page.locator('[data-project-id="p3"]').getAttribute('aria-current'),'page');

    await page.setViewportSize({width:390,height:844});
    assert.equal(await page.locator('.agent-projects-search').isVisible(),false,'Mobile must hide the desktop search despite theme label rules');
    assert.equal(await page.locator('.agent-projects-mobile').isVisible(),true);
    assert.equal(await page.locator('#agent-projects-select').isVisible(),true);
    assert.equal(await page.locator('.agent-projects-list').isVisible(),false);
    await page.locator('#agent-projects-select').selectOption('p1');
    assert.equal(await page.evaluate(()=>fixture.current),'p1');
    await create.click(); await name.fill('手机新项目');
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),true);
    const box = await dialog.boundingBox(); assert.ok(box.x>=0 && box.x+box.width<=390);
    await page.evaluate(()=>projectNavigation.update(fixture.projects,fixture.current)); assert.equal(await name.inputValue(),'手机新项目');
    await page.getByRole('button',{name:'取消',exact:true}).click();

    await page.evaluate(()=>fixture.callbackFailure=true);
    await create.click(); await name.fill('已保存但刷新失败'); await submit.click();
    await page.getByText('项目已创建，但列表刷新失败；请关闭窗口并刷新页面查看。',{exact:true}).waitFor();
    assert.equal(await page.getByRole('button',{name:'已创建',exact:true}).isDisabled(),true);
    const count = writes.length;
    await page.evaluate(()=>document.querySelector('#agent-projects-dialog form').dispatchEvent(new Event('submit',{bubbles:true,cancelable:true})));
    assert.equal(writes.length,count,'Successful creation cannot be repeated after callback failure');
    await page.getByRole('button',{name:'取消',exact:true}).click();
    assert.deepEqual(errors,[]);
    return {passed:true,zeroProjectCreate:true,tokenedProjectApi:true,projectSwitch:true,searchAndFormPreservedOnPoll:true,
      failedCreateRetainsInputs:true,cancelAndEscapeRestoreFocus:true,inFlightDuplicatePrevented:true,busyPreventsProjectChange:true,
      successfulCreateWithFailedRefreshNotRepeated:true,xssRenderedAsText:true,mobile390:true,pageErrors:errors};
  } finally {await browser.close();}
}
module.exports = run;
if (require.main === module) run(require('playwright')).then(result=>console.log(JSON.stringify(result))).catch(error=>{console.error(error);process.exitCode=1;});

