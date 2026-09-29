const assert=require('node:assert/strict');
const fs=require('node:fs/promises');
const path=require('node:path');
const {chromium}=require('playwright');
(async()=>{
 const browser=await chromium.launch({headless:true});
 const page=await browser.newPage({viewport:{width:1280,height:900}});
 const errors=[],posts=[];let ready=true,fail=false,hold=null,delay=false;
 page.on('pageerror',e=>errors.push(e.message));
 const item={id:'a',title:'测试采购',project:'项目甲',merchant:'测试商家',category:'其他',stage:'draft',complete:false,verified:false,ready:false,amount:'24.60',amount_cents:2460,missing:['分类'],attachments:[],events:[]};
 await page.route('http://127.0.0.1:18767/**',async route=>{
  const url=new URL(route.request().url()),json=x=>route.fulfill({contentType:'application/json',body:JSON.stringify(x)});
  if(url.pathname==='/api/state')return json({items:[item,{...item,id:'b',title:'另一事项'}],inbox:[],categories:['材料采购','其他'],roles:{invoice:'发票'},stages:{draft:'待提交'},token:'synthetic',category_suggestion_ready:ready});
  if(url.pathname==='/api/logistics')return json({projects:[]});
  if(url.pathname==='/api/category/suggest'){
   posts.push(route.request().postDataJSON());
   if(delay)await new Promise(resolve=>hold=resolve);
   if(fail)return route.fulfill({status:400,contentType:'application/json',body:JSON.stringify({error:'Jev暂时不可用'})});
   return json({category:'材料采购',source:'jev',reason:'依据商品文字',evidence:{goods:'<img src=x onerror=alert(1)>扎带'},warnings:[]});
  }
  if(url.pathname==='/api/update'){const data=route.request().postDataJSON();posts.push(data);Object.assign(item,data);return json({ok:true});}
  const name=url.pathname==='/'?'index.html':url.pathname.slice(1);
  if(!['index.html','app.js','receipt.js','report.js','mail-search.js','expense-classifier.js','style.css'].includes(name))return route.fulfill({status:404,body:''});
  return route.fulfill({contentType:name.endsWith('.js')?'text/javascript':name.endsWith('.css')?'text/css':'text/html',body:await fs.readFile(path.resolve(__dirname,'../src/invoice_print_layout/web',name))});
 });
 try{
  await page.goto('http://127.0.0.1:18767/');await page.locator('[data-item="a"]').click();
  await page.locator('#suggestCategory').click();await page.locator('#applyCategorySuggestion').waitFor();
  assert.equal(posts.length,1);assert.equal(posts[0].id,'a');
  assert.equal(await page.locator('#editForm [name=category]').inputValue(),'其他');
  assert.equal(await page.locator('#categorySuggestion img').count(),0);
  await page.locator('#applyCategorySuggestion').click();
  await page.waitForFunction(()=>document.getElementById('notice').textContent.includes('分类已保存'));
  assert.equal(await page.locator('#editForm [name=category]').inputValue(),'材料采购');assert.equal(posts.length,2);
  assert.equal(posts[1].category,'材料采购');
  // In-flight result must not overwrite edits or another selected matter.
  delay=true;await page.locator('#suggestCategory').click();
  await page.locator('#editForm [name=note]').fill('用途修改');while(!hold)await new Promise(r=>setTimeout(r,10));hold();
  await page.getByText('判断期间事项已修改，请保存后重新判断。',{exact:true}).waitFor();assert.equal(await page.locator('#applyCategorySuggestion').count(),0);
  await page.locator('[data-item="b"]').click();hold=null;await page.locator('#suggestCategory').click();
  await page.locator('[data-item="a"]').click();while(!hold)await new Promise(r=>setTimeout(r,10));hold();
  await page.waitForTimeout(100);assert.equal(await page.locator('#applyCategorySuggestion').count(),0);
  delay=false;fail=true;await page.locator('#suggestCategory').click();await page.getByText('Jev暂时不可用',{exact:true}).waitFor();
  assert.equal(await page.locator('#suggestCategory').isDisabled(),false);
  fail=false;await page.locator('#suggestCategory').click();await page.locator('#applyCategorySuggestion').waitFor();
  await fs.mkdir('workspace/ui-checks',{recursive:true});await page.screenshot({path:'workspace/ui-checks/category-suggestion-desktop.png'});
  await page.setViewportSize({width:390,height:844});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
  await page.screenshot({path:'workspace/ui-checks/category-suggestion-mobile.png',fullPage:true});
  ready=false;await page.reload();await page.locator('[data-item="a"]').click();assert.equal(await page.locator('#suggestCategory').count(),0);
  assert.deepEqual(errors,[]);console.log('expense classifier browser checks passed');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1);});
