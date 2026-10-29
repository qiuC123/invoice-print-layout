// Real HTTP acceptance and evidence from an isolated synthetic workspace.
const assert=require('node:assert/strict'),path=require('node:path'),fs=require('node:fs/promises'),{spawn}=require('node:child_process');
async function run(pw){
 const output=process.env.INVOICE_BROWSER_EVIDENCE;const python=process.env.PHOTO_TEST_PYTHON||path.resolve('.venv/Scripts/python.exe');
 const child=spawn(python,['-m','tests.organize_nonblocking_browser_server'],{cwd:path.resolve(__dirname,'..'),env:{...process.env,PYTHONUTF8:'1'},windowsHide:true,stdio:['pipe','pipe','pipe']});
 let stderr='';child.stderr.on('data',value=>stderr+=value);
 const fixture=await new Promise((resolve,reject)=>{let data='';const timer=setTimeout(()=>reject(Error('fixture timeout: '+stderr)),30000);child.once('exit',code=>{clearTimeout(timer);reject(Error('fixture exit '+code+': '+stderr));});child.stdout.on('data',value=>{data+=value;const line=data.split(/\r?\n/).find(row=>row.startsWith('{'));if(line){clearTimeout(timer);resolve(JSON.parse(line));}});});
 const browser=await pw.chromium.launch({headless:true}),page=await browser.newPage({viewport:{width:1500,height:1050}}),errors=[];
 page.on('pageerror',error=>errors.push(error.message));
 if(output)await fs.mkdir(output,{recursive:true});
 try{
  await page.goto(fixture.base+'/?project='+fixture.project);
  await page.waitForFunction(()=>typeof loadCompleted!=='undefined'&&loadCompleted);
  await page.locator('#manualAgent .mia-summary').click();
  const summary=page.locator('#manualAgent').getByText('已生成 2 笔费用，1 份材料留在材料合成台',{exact:true});await summary.waitFor();
  assert.equal(await page.locator('.mia-question').count(),0);
  const marker=page.getByText(/类别或商品明细待核对：/).first();await marker.waitFor();
  if(output){await page.screenshot({path:path.join(output,'organizer-summary.png'),fullPage:true});}
  await page.locator('#manualAgent').getByRole('button',{name:'打开材料合成台',exact:true}).click();
  await page.locator('#matchingDeskDialog[open]').waitFor();
  const materialReason=page.getByText(/自动整理未能确定组合：/).first();await materialReason.waitFor();
  await page.waitForFunction(()=>[...document.querySelectorAll('#matchingDeskDialog img')].every(img=>img.complete));
  await materialReason.scrollIntoViewIfNeeded();
  if(output)await page.screenshot({path:path.join(output,'material-review-reason.png'),fullPage:true});
  await page.locator('#matchingClose').click();
  await page.locator('#tabs [data-view=review]').click();await marker.waitFor();
  await page.locator('#manualAgent .mia-summary').click();await marker.scrollIntoViewIfNeeded();
  if(output)await page.screenshot({path:path.join(output,'expense-review-marker.png'),fullPage:true});
  await page.goto(fixture.base+'/?project='+fixture.ride_project);
  await page.waitForFunction(()=>typeof loadCompleted!=='undefined'&&loadCompleted);
  await page.evaluate(()=>window.openMaterialMatching());
  await page.locator('#matchingAuto').click();
  await page.waitForFunction(()=>document.getElementById('matchingSelectedVoucherCount')?.textContent==='1');
  await page.locator('.synthesis-row-preview').first().click();
  await page.waitForFunction(()=>document.getElementById('matchingSelectedVoucherCount')?.textContent==='1'&&document.querySelector('#matchingSupportSection h4')?.textContent.includes('1'));
  assert.match(await page.locator('#matchingSupportSection').innerText(),/1/);
  assert.equal(await page.locator('#matchingAmount').inputValue(),'17.34');
  if(output)await page.screenshot({path:path.join(output,'caocao-suggestion.png'),fullPage:true});
  await page.goto(fixture.base+'/?project='+fixture.offline_project);
  await page.waitForFunction(()=>typeof loadCompleted!=='undefined'&&loadCompleted);
  await page.locator('#manualAgent .mia-summary').click();
  await page.getByText('已生成 1 笔费用，1 份材料留在材料合成台；模型暂时不可用',{exact:true}).waitFor();
  assert.equal(await page.locator('.mia-question').count(),0);
  await page.locator('#manualAgent').getByRole('button',{name:'打开材料合成台',exact:true}).click();
  await page.locator('#matchingDeskDialog[open]').waitFor();
  await page.getByText(/模型暂时不可用，已留在材料合成台/).first().waitFor();
  if(output)await page.screenshot({path:path.join(output,'offline-local-fallback.png'),fullPage:true});
  assert.deepEqual(errors,[]);
  return {passed:true,summary:true,noQuestions:true,materialReason:true,expenseMarker:true,passengerInvoiceIncludesTrip:true,offlineLocalFallback:true,base:fixture.base};
 }finally{await browser.close();await new Promise(resolve=>{const timer=setTimeout(()=>{child.kill();resolve();},5000);child.once('exit',()=>{clearTimeout(timer);resolve();});child.stdin.end('stop\n');});}
}
module.exports=run;
if(require.main===module)run(require('playwright')).then(result=>console.log(JSON.stringify(result))).catch(error=>{console.error(error);process.exitCode=1;});
