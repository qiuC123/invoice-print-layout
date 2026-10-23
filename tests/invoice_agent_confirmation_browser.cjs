// Source-bound confirmation presents evidence, sends question identity, and keeps the manual entrance.
const assert=require('node:assert/strict'),fs=require('node:fs/promises'),path=require('node:path');
async function run(pw){
 const browser=await pw.chromium.launch({headless:true}),page=await browser.newPage({viewport:{width:1120,height:950}});
 const origin='http://127.0.0.1:18992',errors=[],answers=[];
 const base={project_id:'p1',models:1,tools:3,limits:{models:6,tools:48},events:[],state:'needs_input',message:'需要补充对应关系'};
 let jobs=[{...base,id:'j1',question:{id:'question-one',type:'material_relation',text:'付款是否对应饮用水采购？',
   evidence_summary:['饮用水发票与付款均为29.60元；付款未写实际商家。'],
   material_ids:['v1','p1','s1'],material_facts:[{id:'v1',goods:'娃哈哈饮用水',amount_cents:2960,merchant:'合成商店'}],
   choices:[{id:'yes',label:'确认',action:'confirm_relation',voucher_keys:['q:v1'],payment_ids:['p1'],support_ids:['s1'],amount_cents:2960}]}},
  {...base,id:'j2',question:{id:'question-two',type:'material_relation',text:'另一组不是同一笔时可补材料。',material_ids:[],
   choices:[{id:'no',label:'不是同一笔',action:'not_same_expense'}]}}];
 page.on('pageerror',e=>errors.push(e.message));
 await page.route(origin+'/**',async route=>{
  const req=route.request(),url=new URL(req.url()),json=value=>route.fulfill({contentType:'application/json',body:JSON.stringify(value)});
  if(url.pathname==='/api/state')return json({token:'synthetic',projects:[{id:'p1',name:'确认界面测试'}]});
  if(url.pathname==='/api/agent/jobs')return json({token:'synthetic',model:{available:false},jobs});
  if(url.pathname==='/api/agent/answer'){
   const body=req.postDataJSON();answers.push(body);assert.equal(req.headers()['x-workbench-token'],'synthetic');
   const job=jobs.find(j=>j.id===body.id);assert.ok(job);assert.equal(body.question_id,job.question.id);
   jobs=jobs.map(j=>j.id!==job.id?j:{...j,state:body.choice==='yes'?'ready':'stopped',question:null,
    message:body.choice==='yes'?'报销预览已准备，请人工核对':'已记录不是同一笔，请补充订单明细或在人工工作台继续',
    preview:body.choice==='yes'?{pdf:'confirmed.pdf',xlsx:'confirmed.xlsx',total_cents:2960,page_count:2,preview_pages:[]}:null});
   return json({job:jobs.find(j=>j.id===body.id)});
  }
  assert.notEqual(req.method(),'POST','Only explicit question answers may write');
  if(url.pathname.startsWith('/intake-page/'))return route.fulfill({contentType:'image/svg+xml',body:'<svg xmlns="http://www.w3.org/2000/svg" width="100" height="120"><text x="5" y="20">原件</text></svg>'});
  const name=url.pathname==='/agent'?'agent.html':url.pathname.slice(1);
  if(!/^[a-z-]+\.(html|js|css)$/.test(name))return route.abort();
  return route.fulfill({contentType:name.endsWith('.js')?'text/javascript':name.endsWith('.css')?'text/css':'text/html',body:await fs.readFile(path.resolve(__dirname,'../src/invoice_print_layout/web',name))});
 });
 try{
  await page.goto(origin+'/agent?project=p1');
  await page.getByText('材料 1：娃哈哈饮用水 · 29.60 元 · 合成商店',{exact:true}).waitFor();
  assert.match(await page.locator('#jobs').innerText(),/付款未写实际商家/);
  assert.equal(await page.locator('.materials a').count(),3);
  assert.equal(await page.locator('#manual').getAttribute('href'),'/?project=p1');
  const confirm=page.getByRole('button',{name:/同一笔，继续生成预览/});
  assert.match(await confirm.innerText(),/明细：材料 3/);
  await page.setViewportSize({width:390,height:844});
  await confirm.click();await page.getByText('合计 29.60 元 · 2 页 · 尚未核对',{exact:true}).waitFor();
  assert.equal(await page.getByRole('link',{name:'到人工工作台核对'}).getAttribute('href'),origin+'/?project=p1');
  await page.getByRole('button',{name:'不是同一笔，补充材料',exact:true}).click();
  await page.getByText('已记录不是同一笔，请补充订单明细或在人工工作台继续',{exact:true}).waitFor();
  assert.equal(await page.locator('.choice').count(),0);
  assert.deepEqual(answers,[{id:'j1',choice:'yes',question_id:'question-one'},{id:'j2',choice:'no',question_id:'question-two'}]);
  assert.deepEqual(errors,[]);
  return {passed:true,evidenceVisible:true,supportMaterialsVisible:true,sourceQuestionIdentitySent:true,confirmContinuesToPreview:true,
    negativeChoiceWithoutArrays:true,mobileViewport:true,manualEntrancePreserved:true,pageErrors:errors};
 }finally{await browser.close();}
}
module.exports=run;
if(require.main===module)run(require('playwright')).then(r=>console.log(JSON.stringify(r))).catch(e=>{console.error(e);process.exitCode=1;});
