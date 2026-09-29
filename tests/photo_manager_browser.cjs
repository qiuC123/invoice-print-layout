const assert=require('node:assert/strict');
const fs=require('node:fs/promises');
const path=require('node:path');
const {spawn}=require('node:child_process');
async function run(pw){
 const python=process.env.PHOTO_TEST_PYTHON||'python';
 const child=spawn(python,['-u',path.join(__dirname,'photo_browser_server.py')],{env:{...process.env,PYTHONPATH:path.resolve(__dirname,'../src')},windowsHide:true,stdio:['ignore','pipe','pipe']});
 let stderr='',buffer='';child.stderr.on('data',b=>stderr+=b);
 let browser;
 try{
  const fixture=await new Promise((resolve,reject)=>{const timeout=setTimeout(()=>reject(Error('fixture startup timeout '+stderr)),30000);child.once('exit',code=>{clearTimeout(timeout);reject(Error('fixture failed '+code+' '+stderr));});child.stdout.on('data',b=>{buffer+=b;const end=buffer.indexOf('\n');if(end>=0){clearTimeout(timeout);resolve(JSON.parse(buffer.slice(0,end)));}});});
  browser=await pw.chromium.launch({headless:true});const page=await browser.newPage({viewport:{width:1280,height:900}}),errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.clock.install();
  await page.goto(fixture.base+'/photos?project='+fixture.projects[0]);await page.getByText('本项目尚无照片。',{exact:false}).waitFor();
  await page.locator('#uploads').setInputFiles(path.join(fixture.incoming,'one.png'));await page.locator('.card').waitFor();assert.equal(await page.locator('.card').count(),1);
  await page.locator('#uploads').setInputFiles(path.join(fixture.incoming,'one.png'));await page.waitForFunction(()=>document.querySelector('#notice').textContent.includes('重复 1'));assert.equal(await page.locator('.card').count(),1);
  await page.mouse.move(0,0);await page.clock.fastForward(5001);assert.equal(await page.locator('#notice').isHidden(),true);
  await page.locator('#briefSettings > summary').click();assert.equal(await page.locator('#sendBriefPreview').isDisabled(),true);
  await page.locator('#briefEnabled').check();await page.getByRole('button',{name:'保存简报设置',exact:true}).click();await page.waitForFunction(()=>document.querySelector('#briefStatus').textContent.includes('已启用'));
  await page.locator('#sendBriefPreview').click();await page.waitForFunction(()=>document.querySelector('#briefStatus').textContent.includes('已送达飞书'));await page.locator('#briefSettings > summary').click();
  // Hold only the client-visible state so active progress is tested without a slow OCR fixture.
  let holdRunning=true;await page.route(fixture.base+'/api/photos?*',async route=>{const response=await route.fetch(),state=await response.json();if(holdRunning&&state.jobs.length){state.jobs[0].state='running';state.jobs[0].message='测试处理中';}await route.fulfill({response,json:state});});
  await page.locator('#selectAll').check();await page.locator('#recognize').click();await page.waitForFunction(()=>document.querySelector('#jobMessage').textContent==='测试处理中');await page.clock.fastForward(9000);assert.equal(await page.locator('#job').isVisible(),true);assert.equal(await page.locator('#jobProgress').isVisible(),true);
  holdRunning=false;await page.waitForFunction(()=>document.querySelector('#jobMessage').textContent==='处理完成');assert.equal(await page.locator('#jobProgress').isHidden(),true);await page.mouse.move(0,0);await page.clock.fastForward(8001);assert.equal(await page.locator('#job').isHidden(),true);
  await page.locator('#refresh').click();await page.waitForFunction(()=>!document.getElementById('refresh').disabled);assert.equal(await page.locator('#job').isHidden(),true);await page.locator('#jobHistory > summary').click();assert.match(await page.locator('#jobHistoryList').innerText(),/识别照片 · 处理完成/);await page.locator('#jobHistory > summary').click();
  await page.getByRole('button',{name:'核对与分类',exact:true}).click();assert.equal(await page.locator('[name="captured_date"]').inputValue(),'2026-09-24');assert.equal(await page.locator('[name="plate"]').inputValue(),'赣AA0001');
  await page.locator('[name="movement"]').selectOption('进场');await page.locator('[name="place"]').selectOption('工厂');await page.locator('[name="trip_date"]').fill('2026-09-26');await page.locator('[name="label"]').fill('车头');await page.locator('#confirmed').check();await page.locator('#savePhoto').click();await page.waitForFunction(()=>document.querySelector('#summary').textContent.includes('1张已保存'));
  const old=(await page.locator('#notice').innerText()).replace('已保存到：','');assert.equal((await fs.stat(old)).isFile(),true);
  await page.getByRole('button',{name:'关闭提示',exact:true}).click();assert.equal(await page.locator('#notice').isHidden(),true);
  await page.getByRole('button',{name:'查看 / 调整分类',exact:true}).click();await page.locator('[name="place"]').selectOption('现场');await page.locator('#savePhoto').click();await page.waitForFunction(previous=>document.querySelector('#notice').textContent!==previous&&!document.querySelector('#editor').open,'已保存到：'+old);await assert.rejects(fs.stat(old));
  await page.getByText('采集照片',{exact:true}).click();await page.locator('#sourceFolder').fill(fixture.incoming);await page.getByRole('button',{name:'读取文件夹',exact:true}).click();await page.waitForFunction(()=>document.querySelector('#summary').textContent.startsWith('2张照片'));await page.waitForFunction(()=>document.querySelector('#jobMessage').textContent==='处理完成，有待核对项');assert.match(await page.locator('#jobCounts').innerText(),/重复 1/);
  await page.locator('#closeJob').click();assert.equal(await page.locator('#job').isHidden(),true);assert.match(await page.locator('#jobHistoryLabel').innerText(),/1次有未完成项/);
  await page.locator('#stateFilter').selectOption('pending');await page.getByRole('button',{name:'核对与分类',exact:true}).click();await page.locator('[name="kind"]').selectOption('人员');await page.locator('[name="movement"]').selectOption('进场');await page.locator('[name="captured_date"]').fill('2026-09-26');await page.locator('[name="label"]').fill('合影');await page.locator('[name="visible_count"]').fill('5');await page.locator('#confirmed').check();await page.locator('#savePhoto').click();await page.waitForFunction(()=>document.querySelector('#summary').textContent.includes('2张已保存'));
  await page.locator('#clearFilters').click();await page.reload();await page.waitForFunction(()=>document.querySelector('#summary').textContent.includes('2张已保存'));
  assert.equal(await page.locator('#job').isHidden(),true);assert.match(await page.locator('#jobHistoryLabel').innerText(),/未完成项/);await page.locator('#jobHistory > summary').click();await page.locator('#jobHistoryList summary').click();assert.notEqual((await page.locator('#jobHistoryList li').innerText()).trim(),'');await page.locator('#jobHistory > summary').click();
  const out=path.resolve(__dirname,'../workspace/ui-checks');await fs.mkdir(out,{recursive:true});await page.screenshot({path:path.join(out,'photo-manager-desktop.png'),fullPage:true});
  await page.setViewportSize({width:390,height:844});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);await page.screenshot({path:path.join(out,'photo-manager-mobile.png'),fullPage:true});
  await page.goto(fixture.base+'/photos?project='+fixture.projects[1]);await page.getByText('本项目尚无照片。',{exact:false}).waitFor();assert.equal(await page.locator('.card').count(),0);assert.deepEqual(errors,[]);
  await page.locator('#settings > summary').click();await page.locator('#autoClassify').check();await page.locator('#projectLocation').fill('南昌');await page.getByRole('button',{name:'保存设置',exact:true}).click();await page.waitForFunction(()=>document.querySelector('#classificationStatus').textContent.includes('已开启'));
  await page.locator('#uploads').setInputFiles(fixture.auto_file);await page.waitForFunction(()=>document.querySelector('#summary').textContent.includes('1张已保存'));await page.getByText('自动分类并保存',{exact:true}).waitFor();assert.match(await page.locator('.card').innerText(),/搭建/);
  await page.getByRole('button',{name:'查看 / 调整分类',exact:true}).click();assert.equal(await page.locator('#confirmed').isChecked(),false);const autoSaved=(await page.locator('#savedPath').innerText()).replace('已保存：','');assert.equal((await fs.readFile(autoSaved)).equals(await fs.readFile(fixture.auto_file)),true);await page.locator('#closeEditor').click();
  await page.reload();await page.getByText('自动分类并保存',{exact:true}).waitFor();assert.deepEqual(errors,[]);
  const response=await fetch(fixture.base+'/api/photos?project='+fixture.projects[0]);const state=await response.json();await fetch(fixture.base+'/api/shutdown',{method:'POST',headers:{'Content-Type':'application/json','X-Workbench-Token':state.token},body:'{}'});
  return 'passed: real HTTP upload, OCR/review/correction, folder import, isolation/mobile, notice expiry and close, active job retention, completion expiry/close without refresh/reload resurrection, collapsed issue history, automatic settings -> upload -> OCR/Jev fixture -> byte-identical save -> reload';
 }finally{if(browser)await browser.close();if(child.exitCode===null)child.kill();}
}
module.exports=run;
if(require.main===module)run(require('playwright')).then(console.log).catch(e=>{console.error(e);process.exitCode=1;});
