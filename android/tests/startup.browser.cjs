// A fast local Native response must not render before all UI features register.
const assert=require('node:assert/strict');
const fs=require('node:fs/promises'),path=require('node:path');
async function run(pw){
 const browser=await pw.chromium.launch({headless:true});const cases=[];
 try{for(const delay of [0,250]){
  const context=await browser.newContext({viewport:{width:393,height:852}});
  try{
   const errors=[];const source=path.resolve(__dirname,'../app/src/main/assets');
   await context.addInitScript(()=>{
    localStorage.setItem('project','p1');window.__startup=[];
    window.Native={invoke(raw){const request=JSON.parse(raw);if(request.method!=='state')throw Error('Unexpected startup mutation');
     window.__startup.push({ready:document.readyState,features:typeof renderSettings==='function'});
     window.receiveNative({id:request.id,value:{projects:[{id:'p1',name:'合成启动测试',deleted:false,reimbursed:false}],materials:[],material_archive:[],expenses:[],reports:[],people:[],settings:{experience:[]},report_types:[],shared_count:0,custom_template:false,services:{glm:{enabled:false},jev:{enabled:false},mail:{enabled:false}}}});
    }};
   });
   await context.route('http://invoice-android.test/**',async route=>{
    const asset=new URL(route.request().url()).pathname.slice(1)||'index.html';assert.ok(['index.html','style.css','app.js','workbench.js'].includes(asset));
    if(asset==='workbench.js'&&delay)await new Promise(resolve=>setTimeout(resolve,delay));
    await route.fulfill({contentType:asset.endsWith('.css')?'text/css':asset.endsWith('.js')?'text/javascript':'text/html',body:await fs.readFile(path.join(source,asset))});
   });
   const page=await context.newPage();page.on('pageerror',error=>errors.push(error.message));
   await page.goto('http://invoice-android.test/index.html');
   await page.getByText('还没有材料',{exact:true}).waitFor();
   assert.deepEqual(await page.evaluate(()=>window.__startup),[{ready:'interactive',features:true}]);
   await page.getByRole('button',{name:'拍照',exact:true}).waitFor();
   await page.getByRole('button',{name:'自动组合',exact:true}).waitFor();
   await page.getByRole('button',{name:'归档与回收站',exact:true}).waitFor();
   assert.deepEqual(errors,[]);cases.push({workbench_delay_ms:delay,first_render_has_all_features:true});
  }finally{await context.close();}
 }
 return {passed:true,cases};
 }finally{await browser.close();}
}
module.exports=run;
if(require.main===module)run(require('playwright')).then(r=>console.log(JSON.stringify(r,null,2))).catch(e=>{console.error(e);process.exitCode=1;});
