const assert=require('node:assert/strict');
const fs=require('node:fs/promises');
const path=require('node:path');
const {chromium}=require('playwright');
(async()=>{
 const browser=await chromium.launch({headless:true});
 try{
  for(const scenario of ['normal','edit-note','edit-category','needs-info']){
   const page=await browser.newPage({viewport:{width:390,height:844}});
   const errors=[];page.on('pageerror',e=>errors.push(e.message));
   const item={id:'a',title:'测试物资',project:'测试项目',merchant:'测试商家',category:'其他',stage:'draft',verified:false,complete:false,amount:'10.00',amount_cents:1000,note:'初始用途',invoice_state:'not_requested',waiting_days:0,expense_date:'',basis:'材料存在性检查；金额与内容需人工核对',missing:['分类'],attachments:[],events:[]};
   let status={pending:scenario!=='needs-info',status:'needs_info',reason:'需要补充具体商品'},calls=0,release=null;
   await page.route('http://127.0.0.1:18768/**',async route=>{
    const u=new URL(route.request().url()),json=x=>route.fulfill({contentType:'application/json',body:JSON.stringify(x)});
    if(u.pathname==='/api/state')return json({items:[item],inbox:[],categories:['材料采购','酒店','其他'],roles:{invoice:'发票'},stages:{draft:'待提交'},token:'synthetic',category_suggestion_ready:true,category_auto_ready:true,classification:{a:status}});
    if(u.pathname==='/api/logistics')return json({projects:[]});
    if(u.pathname==='/api/category/auto'){
     calls++;
     if(scenario.startsWith('edit'))await new Promise(resolve=>release=resolve);
     item.category='材料采购';item.missing=['发票'];
     status={pending:false,status:'saved',saved:true,category:'材料采购',source:'cache',reason:'相同内容缓存',evidence:{goods:'<img src=x onerror=alert(1)>扎带'}};
     return json(status);
    }
    if(u.pathname==='/api/update'){Object.assign(item,route.request().postDataJSON());return json(item);}
    const name=u.pathname==='/'?'index.html':u.pathname.slice(1);
    if(!['index.html','app.js','receipt.js','report.js','mail-search.js','expense-classifier.js','style.css'].includes(name))return route.fulfill({status:404,body:''});
    return route.fulfill({contentType:name.endsWith('.js')?'text/javascript':name.endsWith('.css')?'text/css':'text/html',body:await fs.readFile(path.resolve(__dirname,'../src/invoice_print_layout/web',name))});
   });
   await page.goto('http://127.0.0.1:18768/');
   await page.locator('[data-item=a]').click();
   if(scenario==='needs-info'){
    await page.getByText(/需要补充具体商品/).waitFor();
    await page.waitForTimeout(500);assert.equal(calls,0);
    await page.locator('#retryCategory').click();
   }
   if(scenario.startsWith('edit')){
    for(let n=0;!release&&n<100;n++)await new Promise(r=>setTimeout(r,25));
    assert.ok(release);
    await page.locator('#editForm [name=note]').fill('新用途仍未保存');
    if(scenario==='edit-category')await page.locator('#editForm [name=category]').selectOption('酒店');
    release();
    await page.getByText(/你的其他未保存修改仍保留/).waitFor();
    assert.equal(await page.locator('#editForm [name=note]').inputValue(),'新用途仍未保存');
    assert.equal(await page.locator('#editForm [name=category]').inputValue(),scenario==='edit-category'?'酒店':'材料采购');
    await page.getByRole('button',{name:'保存事项',exact:true}).click();
   }else{
    await page.waitForFunction(()=>document.querySelector('[data-item=a] .sub')?.textContent.includes('材料采购'));
   }
   await page.waitForTimeout(350);
   assert.equal(calls,1); // no repeated model work on re-render
   assert.equal(await page.locator('#applyCategorySuggestion').count(),0);
   assert.equal(await page.locator('#categorySuggestion img').count(),0);
   assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
   assert.deepEqual(errors,[]);
   if(scenario==='normal')await page.screenshot({path:'workspace/ui-checks/automatic-category-mobile.png',fullPage:true});
   await page.close();
  }
  console.log('automatic category browser checks passed');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1);});
