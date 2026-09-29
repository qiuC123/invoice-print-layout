const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const {chromium} = require('playwright');
(async () => {
 const browser = await chromium.launch({headless:true});
 try {
  const page = await browser.newPage({viewport:{width:1280,height:850}});
  const errors=[]; page.on('pageerror',e=>errors.push(e.message));
  let assigned;
  const item={id:'ride',title:'测试打车',project:'待分配项目',category:'打车',stage:'draft',amount:'12.30',amount_cents:1230,complete:false,missing:['行程单'],attachments:[],events:[]};
  await page.route('http://127.0.0.1:18767/**',async route=>{
   const u=new URL(route.request().url());
   const json=x=>route.fulfill({contentType:'application/json',body:JSON.stringify(x)});
   if(u.pathname==='/api/state')return json({items:[item],inbox:assigned?[]:[{id:'f',name:'行程单.pdf',reason:'等待对应发票 <script>bad</script>'}],categories:['打车'],roles:{invoice:'发票',trip:'行程单',other:'待分类材料'},stages:{draft:'待提交'},token:'test'});
   if(u.pathname==='/api/assign'){assigned=route.request().postDataJSON();return json(item);}
   if(u.pathname==='/api/logistics')return json({projects:[]});
   const name=u.pathname==='/'?'index.html':u.pathname.slice(1);
   if(!/^(index.html|app.js|receipt.js|report.js|mail-search.js|expense-classifier.js|style.css)$/.test(name))return route.fulfill({status:404,body:''});
   return route.fulfill({contentType:name.endsWith('.js')?'text/javascript':name.endsWith('.css')?'text/css':'text/html',body:await fs.readFile(path.resolve(__dirname,'../src/invoice_print_layout/web',name))});
  });
  await page.goto('http://127.0.0.1:18767/');
  await page.locator('[data-view="inbox"]').click();
  assert.match(await page.locator('#listSummary').innerText(),/打车材料已自动整理/);
  assert.equal(await page.locator('#role-f').inputValue(),'other');
  assert.match(await page.locator('.mailrow small').innerText(),/<script>bad<\/script>/);
  await page.locator('#target-f').selectOption('ride');
  await page.locator('#role-f').selectOption('trip');
  await page.getByRole('button',{name:'手动归档',exact:true}).click();
  await page.getByText(/已配对的打车费用请到/).waitFor();
  assert.equal(assigned.role,'trip');
  assert.deepEqual(errors,[]);
  console.log('automatic ride inbox browser checks passed');
 } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
