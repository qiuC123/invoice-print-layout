const assert=require('node:assert/strict'),fs=require('node:fs/promises'),path=require('node:path');
async function run(pw){
 const browser=await pw.chromium.launch({headless:true});
 try{
  const page=await browser.newPage();
  const source=await fs.readFile(path.resolve(__dirname,'../src/invoice_print_layout/web/app.js'),'utf8');
  const start=source.indexOf('window.invoiceCheckMarkup='),end=source.indexOf('\nfunction matchesExpenseFilters',start);
  assert.ok(start>=0&&end>start);
  await page.setContent('<div id="checks"></div>');
  await page.addScriptTag({content:"const esc=x=>String(x??'').replace(/[&<>\"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',\"'\":'&#39;'}[c]));\n"+source.slice(start,end)});
  await page.evaluate(()=>document.getElementById('checks').innerHTML=window.invoiceCheckMarkup([
   {name:'<img src=x onerror=alert(1)>',qr_check:{status:'match',checked:['invoice_number','amount','issue_date']}},
   {qr_check:{status:'mismatch',checked:['invoice_number'],mismatches:['amount']}},
   {qr_check:{status:'unreadable'}},
   {company_check:{status:'unreadable'}},{company_check:{status:'mismatch',tax_id_read:true}},
  ]));
  assert.match(await page.locator('#checks').innerText(),/号码✓ 金额✓ 日期✓/);
  assert.match(await page.locator('#checks').innerText(),/金额不一致/);
  assert.equal(await page.locator('#checks img').count(),0);
  assert.match(await page.locator('#checks').innerText(),/未读取抬头/);
  assert.match(await page.locator('#checks').innerText(),/发票抬头\/税号与公司信息不一致/);
  assert.match(await page.locator('small').nth(1).getAttribute('style'),/color/);
  return {passed:true,readable:true,mismatch:true,unreadable:true,escaped:true};
 }finally{await browser.close();}
}
module.exports=run;
if(require.main===module)run(require('playwright')).then(x=>console.log(JSON.stringify(x))).catch(e=>{console.error(e);process.exitCode=1;});
