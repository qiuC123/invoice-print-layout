// Real project UI with synthetic HTTP; never starts a service or reads a real ledger.
const assert=require('node:assert/strict'),fs=require('node:fs/promises'),path=require('node:path');
async function run(pw){
 const browser=await pw.chromium.launch({headless:true}),page=await browser.newPage({viewport:{width:1400,height:1000}});
 const origin='http://127.0.0.1:18863',source=path.resolve(__dirname,'../src/invoice_print_layout/web'),errors=[],writes=[];
 const evidence=process.env.INVOICE_BROWSER_EVIDENCE;if(evidence)await fs.mkdir(evidence,{recursive:true});
 const projects=[{id:'p1',name:'合成甲',project_revision:1,start_date:'2026-10-01',end_date:'2026-10-31'},
  {id:'p2',name:'合成乙',project_revision:0,start_date:'',end_date:''}];
 let conflict=false,overlaps=['合成乙'],rerouted=2;
 page.on('pageerror',error=>errors.push(error.message));
 await page.route(origin+'/**',async route=>{
  const req=route.request(),url=new URL(req.url()),json=(data,status=200)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(data)});
  if(url.pathname==='/api/state')return json({app_mode:'invoice',token:'synthetic',projects,items:[],inbox:[],categories:[],roles:{},stages:{}});
  if(req.method()==='POST'){
   const body=req.postDataJSON();writes.push({path:url.pathname,body});assert.equal(req.headers()['x-workbench-token'],'synthetic');
   if(url.pathname==='/api/projects/period'){
    assert.deepEqual(Object.keys(body).sort(),['end_date','expected_revision','project_id','start_date']);const project=projects.find(row=>row.id===body.project_id);assert.ok(project);assert.equal(body.expected_revision,project.project_revision);
    if(conflict){conflict=false;project.project_revision++;return json({error:'项目版本冲突，请刷新'},409);}
    Object.assign(project,{start_date:body.start_date,end_date:body.end_date,project_revision:project.project_revision+1});return json({...project,overlaps,rerouted});
   }
   if(url.pathname==='/api/projects'){assert.deepEqual(Object.keys(body).sort(),['end_date','name','start_date']);const project={...body,id:'created',project_revision:0};projects.push(project);return json(project);}
   assert.fail('Unexpected write '+url.pathname);
  }
  if(url.pathname==='/api/settings')return json({version:2,enabled:false,active:'workbuddy',providers:[],organize:{auto_after_upload:true,use_model:true,model_available:false,model_label:''},mail:{address:'',folder:'INBOX',auth_saved:false,enabled:false,interval_seconds:120,since:'2026-10-29',approved_since:'2026-10-29',projects}});
  if(url.pathname==='/api/agent/jobs')return json({jobs:[],model:{available:false}});
  if(url.pathname==='/api/agent/mail')return json({address:'',enabled:false});
  if(url.pathname.startsWith('/api/'))return json({queue:[]});
  const file=url.pathname==='/'?'index.html':url.pathname.slice(1);if(!/^[a-z-]+\.(html|js|css)$/.test(file))return route.abort();
  return route.fulfill({contentType:file.endsWith('.js')?'text/javascript':file.endsWith('.css')?'text/css':'text/html',body:await fs.readFile(path.join(source,file))});
 });
 const edit=()=>page.locator('#projectActionDialog'),date=(where,key)=>where.locator(`[name=${key}]`);
 const openMenu=async()=>{await page.locator('#projectLinks [data-project-menu-id=p1]').click({button:'right'});await page.locator('[data-project-action=period]').click();await edit().waitFor();};
 try{
  await page.goto(origin+'/?project=p1');await page.locator('#projectPeriod').getByText('项目时间：2026-10-01 至 2026-10-31',{exact:true}).waitFor();
  await openMenu();assert.equal(await date(edit(),'start_date').inputValue(),'2026-10-01');assert.equal(await date(edit(),'end_date').inputValue(),'2026-10-31');
  if(evidence)await page.screenshot({path:path.join(evidence,'mock-period-edit-light.png')});
  await date(edit(),'start_date').fill('2026-10-03');await date(edit(),'end_date').fill('2026-11-02');await edit().getByRole('button',{name:'保存项目时间',exact:true}).click();await edit().waitFor({state:'detached'});
  await page.locator('#notice').filter({hasText:'与 合成乙 时间重叠，重叠期间的发票需要你手动分配'}).waitFor();assert.match(await page.locator('#notice').textContent(),/已把 2 封未分配邮件分到本项目/);assert.equal(await page.locator('#projectPeriod').textContent(),'项目时间：2026-10-03 至 2026-11-02');
  overlaps=[];rerouted=0;
  await page.evaluate(()=>window.openProjectPeriod(projectContext()));await date(edit(),'end_date').fill('');await date(edit(),'start_date').fill('');await edit().getByRole('button',{name:'保存项目时间'}).click();await edit().waitFor({state:'detached'});await page.locator('[data-set-project-period]').waitFor();assert.match(await page.locator('#projectPeriod').textContent(),/未设置项目时间/);
  await page.locator('[data-set-project-period]').click();assert.equal(await date(edit(),'start_date').inputValue(),'');const beforeInvalid=writes.length;await date(edit(),'end_date').fill('2026-10-10');await edit().locator('form').evaluate(form=>form.requestSubmit());assert.equal(writes.length,beforeInvalid,'end-only dates do not submit');
  await date(edit(),'start_date').fill('2026-10-11');assert.equal(await date(edit(),'end_date').evaluate(node=>node.validity.rangeUnderflow),true);await edit().locator('form').evaluate(form=>form.requestSubmit());assert.equal(writes.length,beforeInvalid,'reverse intervals do not submit');
  await date(edit(),'end_date').fill('');await edit().getByRole('button',{name:'保存项目时间'}).click();await edit().waitFor({state:'detached'});await page.locator('#projectPeriod').getByText('项目时间：2026-10-11 起，进行中',{exact:true}).waitFor();
  await page.locator('#themeSelect').selectOption('dark',{force:true});await openMenu();if(evidence)await page.screenshot({path:path.join(evidence,'mock-period-edit-dark.png')});
  await date(edit(),'start_date').fill('2026-10-12');conflict=true;const beforeConflict=writes.length;await edit().getByRole('button',{name:'保存项目时间'}).click();await edit().locator('[data-project-error]').filter({hasText:'重新打开'}).waitFor();
  assert.equal(await date(edit(),'start_date').inputValue(),'2026-10-12');assert.equal(await edit().getByRole('button',{name:'保存项目时间'}).isDisabled(),true);await edit().locator('form').evaluate(form=>form.requestSubmit());assert.equal(writes.length,beforeConflict+1);await edit().getByRole('button',{name:'取消',exact:true}).click();
  await page.goto(origin+'/');await page.waitForFunction(()=>state.projects?.length===2);assert.equal(await page.locator('#projectPeriod').isVisible(),false);
  await page.locator('#createProject').click();const create=page.locator('#project-create');assert.equal(await date(create,'start_date').inputValue(),'');assert.equal(await date(create,'end_date').inputValue(),'');assert.match(await create.textContent(),/填写后，自动收件会把这段时间内的发票分到这个项目；结束日期不填表示进行中。/);
  await create.locator('[name=name]').fill('合成时间新项目');await date(create,'start_date').fill('2026-11-01');await date(create,'end_date').fill('2026-11-30');if(evidence)await page.screenshot({path:path.join(evidence,'mock-project-create-dates.png')});await create.getByRole('button',{name:'创建项目',exact:true}).click();await page.waitForURL(origin+'/?project=created');await page.locator('#projectPeriod').getByText('项目时间：2026-11-01 至 2026-11-30',{exact:true}).waitFor();
  assert.deepEqual(writes.find(row=>row.path==='/api/projects').body,{name:'合成时间新项目',start_date:'2026-11-01',end_date:'2026-11-30'});
  await page.evaluate(()=>window.invoiceSettings.open('mail'));await page.locator('#settingsMailProject').waitFor();assert.match(await page.locator('#settingsMailProject').locator('..').textContent(),/匹配不到项目时归入/);assert.equal(await page.locator('#settingsMailProject option').first().textContent(),'留在收件箱，由我分配');assert.equal(await page.locator('#settingsMailRoutingHelp').textContent(),'项目名称 → 项目时间 → 归入项目 → 留在收件箱');
  assert.deepEqual(errors,[]);return {passed:true,cases:['create project with optional dates','menu dates prefill and revisioned save','overlap and rerouted notifications','clear dates and title setup link','end-only and reversed interval validation','ongoing title','period revision conflict blocks resubmit','unscoped heading hides period','mail fallback project copy'],syntheticWrites:writes};
 }finally{await browser.close();}
}
module.exports=run;
if(require.main===module)run(require('playwright')).then(result=>console.log(JSON.stringify(result))).catch(error=>{console.error(error);process.exitCode=1;});
