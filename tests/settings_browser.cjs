// Settings use the real invoice page and synthetic API responses only.
const assert=require('node:assert/strict'),fs=require('node:fs/promises'),path=require('node:path');
async function run(pw){
 const browser=await pw.chromium.launch({headless:true}),origin='http://127.0.0.1:18859';
 const source=path.resolve(__dirname,'../src/invoice_print_layout/web'),errors=[],writes=[];
 const evidence=process.env.INVOICE_BROWSER_EVIDENCE;
 let imageFails=false,organizeFails=false,mailTestFails=false;
 if(evidence)await fs.mkdir(evidence,{recursive:true});
 let settings={version:2,enabled:false,active:'workbuddy',providers:[
  {id:'workbuddy',name:'WorkBuddy 本机',kind:'workbuddy',base_url:'http://127.0.0.1:3425/v1',model:'workbuddy/glm-5.3-flash',builtin:true,key_source:'not_required'},
  {id:'glm',name:'智谱官方',kind:'openai',base_url:'https://open.bigmodel.cn/api/paas/v4',model:'glm-5.3-flash',builtin:true,key_source:'file'},
  {id:'deepseek',name:'DeepSeek 官方',kind:'openai',base_url:'https://api.deepseek.com/v1',model:'deepseek-flash',builtin:true,key_source:'none'},
  {id:'custom0',name:'合成自定义',kind:'openai',base_url:'https://synthetic.example/v1',model:'synthetic-vision',builtin:false,key_source:'none'}],organize:{auto_after_upload:true,use_model:true,model_available:false,model_label:''},mail:{address:'sample@163.com',folder:'INBOX',auth_saved:true,enabled:false,interval_seconds:120,since:'2026-10-28',approved_since:'2026-10-27',default_project_id:'',last_checked_at:null,last_error:'',projects:[{id:'p1',name:'合成项目'}]}};
 const context=await browser.newContext({viewport:{width:1440,height:900}});
 await context.addInitScript(()=>{window.settingsEvents=[];window.addEventListener('invoice-settings-changed',event=>window.settingsEvents.push(event.detail));});
 await context.route(origin+'/**',async route=>{
  const req=route.request(),url=new URL(req.url()),json=(body,status=200)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(body)});
  if(url.pathname==='/api/state'){return json({app_mode:'invoice',token:'synthetic',projects:[{id:'p1',name:'合成项目'}],items:[],inbox:[],categories:[],roles:{},stages:{}});}
  if(url.pathname==='/api/agent/mail/collect'){assert.equal(req.method(),'POST');writes.push({path:url.pathname});settings.mail.last_checked_at=1793116800;return json({message:'已开始收件'});}
  if(url.pathname==='/host')return route.fulfill({contentType:'text/html',body:'<!doctype html><html><body><iframe title="embedded" src="/?project=p1&amp;embedded=1" style="width:1250px;height:900px"></iframe></body></html>'});
  if(url.pathname.startsWith('/api/settings')){
   if(req.method()==='GET')return json(settings);
   assert.equal(req.headers()['x-workbench-token'],'synthetic');
   const data=req.postDataJSON();writes.push({path:url.pathname,action:data.action});
   if(url.pathname==='/api/settings/organize'){if(organizeFails){organizeFails=false;return json({error:'合成保存失败'},400);}Object.assign(settings.organize,data);return json(settings.organize);}
   if(url.pathname==='/api/settings/mail/test'){assert.equal(data.address,'new-sample@163.com');assert.equal(data.folder,'INBOX');assert.equal(data.auth_code,'synthetic-mail-auth-only');return json({ok:!mailTestFails,message:mailTestFails?'合成连接失败':'连接成功，文件夹可用'});}
   if(url.pathname==='/api/settings/mail'){assert.equal(data.auth_code,'synthetic-mail-auth-only');Object.assign(settings.mail,Object.fromEntries(Object.entries(data).filter(([key])=>key!=='auth_code')),{auth_saved:true});return json(settings.mail);}
   const stored=settings.providers.find(provider=>provider.id===data.provider?.id);
   if(['/api/settings/test','/api/settings/models'].includes(url.pathname)&&stored?.kind==='openai'&&!data.api_key&&(stored.key_required||new URL(stored.base_url).hostname!==new URL(data.provider.base_url).hostname))return json({error:'地址已变更，请重新填写 API Key'},400);
   if(url.pathname==='/api/settings/test')return json({ok:!imageFails,text:{ok:true,model:data.provider.model,elapsed_ms:12},image:{ok:!imageFails,model:data.provider.model,elapsed_ms:18,...imageFails?{error:'模型不支持图片'}:{}}});
   if(url.pathname==='/api/settings/models')return json({models:['synthetic-model','synthetic-vision']});
   if(data.action==='toggle')settings.enabled=data.enabled;
   else if(data.action==='activate')settings.active=data.id;
   else if(data.action==='delete')settings.providers=settings.providers.filter(provider=>provider.id!==data.id);
   else if(data.action==='clear_key'){const provider=settings.providers.find(provider=>provider.id===data.id);provider.key_source='none';delete provider.key_last4;}
   else if(data.action==='save'){
    let provider=settings.providers.find(provider=>provider.id===data.provider.id);
    if(!provider){provider={id:'custom1',builtin:false,key_source:'none'};settings.providers.push(provider);}
    if(provider.base_url&&new URL(provider.base_url).hostname!==new URL(data.provider.base_url).hostname&&!data.api_key&&provider.kind!=='workbuddy'){provider.key_required=true;provider.key_source='none';delete provider.key_last4;}
    Object.assign(provider,data.provider,{id:provider.id||'custom1'});
    if(data.api_key){provider.key_required=false;provider.key_source='page';provider.key_last4=data.api_key.slice(-4);}
   }else assert.fail('Unexpected settings mutation');
   settings.organize.model_available=settings.enabled;settings.organize.model_label=settings.enabled?'WorkBuddy 本机 · workbuddy/glm-5.3-flash':'';return json(settings);
  }
  if(url.pathname.startsWith('/api/')){assert.equal(req.method(),'GET','no business data writes');return json({queue:[]});}
  const name=url.pathname==='/'?'index.html':url.pathname.slice(1);
  if(!/^[a-z-]+\.(html|js|css)$/.test(name))return route.abort();
  return route.fulfill({contentType:name.endsWith('.js')?'text/javascript':name.endsWith('.css')?'text/css':'text/html',body:await fs.readFile(path.join(source,name))});
 });
 const page=await context.newPage();page.on('pageerror',error=>errors.push(error.message));
 const card=id=>page.locator(`[data-provider-id="${id}"]`);
 const waitStatus=text=>page.waitForFunction(text=>document.getElementById('settingsStatus').textContent.includes(text),text);
 try{
  await page.goto(origin);await page.waitForFunction(()=>typeof state!=='undefined'&&state.token==='synthetic');
  assert.equal(await page.locator('#workbenchMore').count(),0);assert.equal(await page.locator('#selectionTools #moveProject').count(),1);assert.equal(await page.locator('#moveProject').isDisabled(),true);
  await page.locator('#settingsOpen').click();assert.equal(await page.locator('#settingsDialog').evaluate(node=>node.open),true);
  await page.locator('[data-settings-theme=dark]').click();assert.equal(await page.locator('html').getAttribute('data-theme'),'dark');
  assert.equal(await page.locator('#themeSelect').inputValue(),'dark');assert.equal(await page.locator('[data-settings-theme=dark]').getAttribute('aria-checked'),'true');
  const appearanceBox=await page.locator('#settingsDialog').boundingBox();
  for(const category of ['appearance','ai','organize','mail']){
   await page.locator(`[data-settings-tab=${category}]`).click();
   await page.waitForFunction(()=>!document.getElementById('settingsDialog').hasAttribute('aria-busy'));
   assert.deepEqual(await page.locator('#settingsDialog').boundingBox(),appearanceBox,'all four categories keep identical desktop dialog bounds');
   const section=page.locator('.settings-content>section:visible');
   assert.equal(await section.locator(':scope>h3').count(),1);assert.equal(await section.locator(':scope>h3+p.muted').count(),1);
  }
  await page.keyboard.press('Escape');assert.equal(await page.locator('#settingsDialog').evaluate(node=>node.open),false);
  await page.reload();assert.equal(await page.locator('html').getAttribute('data-theme'),'dark');
  await page.locator('#settingsOpen').click();
  await page.locator('[data-settings-tab=ai]').click();await card('workbuddy').waitFor();
  assert.match(await card('workbuddy').textContent(),/使用中/);assert.equal(await card('workbuddy').getByRole('button',{name:'启用',exact:true}).count(),0);
  assert.equal(await page.locator('.settings-provider button.primary').count(),3);assert.equal(await page.locator('.settings-provider button').filter({hasText:'删除'}).count(),0);
  const listSize=await page.locator('.settings-content').evaluate(node=>({content:node.scrollHeight,viewport:node.clientHeight}));assert.ok(listSize.content<=listSize.viewport+1,'four providers fit in the desktop content viewport: '+JSON.stringify(listSize));
  const actionBox=await card('deepseek').locator('.settings-provider-actions').boundingBox(),infoBox=await card('deepseek').locator('.settings-provider-info').boundingBox();assert.ok(actionBox.x>infoBox.x+infoBox.width,'desktop actions on right');
  const contrast=()=>page.locator('.settings-provider').evaluateAll(cards=>{
   const rgb=value=>{const n=value.match(/[\d.]+/g).map(Number);return value.startsWith('color(')?n.slice(0,3).map(x=>x*255):n.slice(0,3);};
   const lum=color=>rgb(color).map(x=>{x/=255;return x<=.04045?x/12.92:((x+.055)/1.055)**2.4;}).reduce((a,x,i)=>a+x*[.2126,.7152,.0722][i],0);
   return cards.flatMap(card=>[...card.querySelectorAll('.settings-provider-details,.settings-provider-key,.settings-tag:not(.settings-active),.settings-secondary')].map(node=>{const a=lum(getComputedStyle(node).color),b=lum(getComputedStyle(card).backgroundColor);return (Math.max(a,b)+.05)/(Math.min(a,b)+.05);}));
  });
  const darkContrast=await contrast();assert.ok(darkContrast.every(value=>value>=4.5),JSON.stringify(darkContrast));
  assert.equal(await card('deepseek').locator('.settings-provider-key').textContent(),'未配置密钥');
  assert.equal(await card('deepseek').locator('.settings-provider-key').evaluate(node=>node.classList.contains('is-warning')),true);
  assert.notEqual(await card('deepseek').locator('.settings-provider-key').evaluate(node=>getComputedStyle(node).color),await card('deepseek').locator('.settings-provider-details').evaluate(node=>getComputedStyle(node).color));
  assert.equal(await card('workbuddy').getByRole('button',{name:'测试',exact:true}).evaluate(node=>getComputedStyle(node).borderTopColor),'rgba(0, 0, 0, 0)');
  assert.equal(await page.locator('#settingsEnabled').evaluate(node=>getComputedStyle(node).accentColor),await card('workbuddy').evaluate(node=>getComputedStyle(node).borderLeftColor));
  if(evidence)await page.screenshot({path:path.join(evidence,'settings-dark.png')});
  await page.locator('#settingsEnabled').check();await waitStatus('已保存');assert.equal(settings.enabled,true);
  await card('glm').getByRole('button',{name:'启用',exact:true}).click();await waitStatus('已切换');assert.match(await card('glm').textContent(),/使用中/);
  const activeBefore=settings.active,activateRequestsBefore=writes.filter(item=>item.action==='activate').length;
  await card('deepseek').getByRole('button',{name:'启用',exact:true}).click();
  assert.equal(settings.active,activeBefore);assert.equal(writes.filter(item=>item.action==='activate').length,activateRequestsBefore);
  assert.equal(await page.locator('#settingsEditor').isVisible(),true);assert.equal(await page.locator('#settingsKey').evaluate(node=>node===document.activeElement),true);
  assert.equal(await page.locator('#settingsKeyWarning').textContent(),'请先填写 API Key');assert.equal(await page.locator('#settingsKeyWarning').isVisible(),true);
  assert.equal(await page.locator('#settingsClearKey').isVisible(),false);
  await page.locator('#settingsKey').fill('synthetic-pending-key');assert.equal(await page.locator('#settingsKeyWarning').isVisible(),false);
  await page.locator('#settingsCancelEdit').click();
  await card('glm').getByRole('button',{name:'编辑',exact:true}).click();assert.equal(await page.locator('#settingsClearKey').isVisible(),false);await page.locator('#settingsCancelEdit').click();
  await card('workbuddy').getByRole('button',{name:'编辑',exact:true}).click();assert.equal(await page.locator('#settingsKeyField').isVisible(),false);
  assert.equal(await page.locator('#settingsProviderList').isVisible(),false);assert.equal(await page.locator('.settings-provider-actions:visible').count(),0);assert.equal(await page.locator('#settingsDangerZone').isVisible(),false);
  await page.locator('#settingsCancelEdit').click();
  await card('deepseek').getByRole('button',{name:'编辑',exact:true}).click();
  await page.locator('#settingsName').fill('合成供应商');await page.locator('#settingsKey').fill('synthetic-secret-key-9876');
  const nameBox=await page.locator('#settingsName').boundingBox(),modelBox=await page.locator('#settingsModel').boundingBox();assert.equal(nameBox.y,modelBox.y);assert.equal(nameBox.height,40);assert.equal(modelBox.height,40);
  assert.equal(await page.locator('#settingsBase').evaluate(node=>getComputedStyle(node.closest('label')).gridColumn),'1 / -1');
  assert.equal(await page.locator('#settingsKey').getAttribute('type'),'password');
  const testButton=await page.locator('#settingsTestEditor').evaluate(node=>{const style=getComputedStyle(node);return {height:node.getBoundingClientRect().height,border:style.borderTopStyle,width:style.borderTopWidth,color:style.borderTopColor};});
  assert.equal(testButton.height,(await page.locator('#settingsEditor button[type=submit]').boundingBox()).height);
  assert.equal(testButton.border,'solid');assert.equal(testButton.width,'1px');assert.notEqual(testButton.color,'rgba(0, 0, 0, 0)');
  const modelsButton=await page.locator('#settingsFetchModels').evaluate(node=>{const style=getComputedStyle(node);return {height:node.getBoundingClientRect().height,y:node.getBoundingClientRect().y,paddingLeft:style.paddingLeft,paddingRight:style.paddingRight,fontSize:style.fontSize,whiteSpace:style.whiteSpace};});
  assert.deepEqual(modelsButton,{height:40,y:modelBox.y,paddingLeft:'14px',paddingRight:'14px',fontSize:'13px',whiteSpace:'nowrap'});
  await page.locator('#settingsFetchModels').click();await waitStatus('已获取 2');
  assert.equal(await page.locator('#settingsModels option').count(),2);
  await page.locator('#settingsModel').fill('synthetic-model');
  await page.locator('#settingsTestEditor').click();await page.getByText('连接成功',{exact:true}).waitFor();
  assert.match(await page.locator('#settingsTestResult').textContent(),/文字 ✓.*synthetic-model.*12 ms/);
  assert.match(await page.locator('#settingsTestResult').textContent(),/图片 ✓.*18 ms/);
  imageFails=true;
  await page.locator('#settingsTestEditor').click();await page.getByText('连接测试未全部通过',{exact:true}).waitFor();
  const partialResult=await page.locator('#settingsTestResult').textContent();
  assert.match(partialResult,/文字 ✓.*synthetic-model.*12 ms/);
  assert.match(partialResult,/图片 ✗.*synthetic-model.*18 ms.*模型不支持图片/);
  assert.ok(!partialResult.includes('synthetic-secret-key-9876'));
  assert.ok(!(await page.locator('#settingsDialog').textContent()).includes('synthetic-secret-key-9876'));
  if(evidence)await page.screenshot({path:path.join(evidence,'settings-partial-failure.png')});
  await page.locator('#settingsEditor button[type=submit]').click();await waitStatus('配置已保存');
  assert.match(await card('deepseek').textContent(),/合成供应商/);assert.match(await card('deepseek').textContent(),/已配置 ••••9876（页面保存）/);
  assert.ok(!(await page.locator('#settingsDialog').textContent()).includes('synthetic-secret-key-9876'));
  assert.equal(await page.locator('#settingsKey').inputValue(),'');
  await card('deepseek').getByRole('button',{name:'编辑',exact:true}).click();assert.equal(await page.locator('#settingsClearKey').isVisible(),true);
  const clearButton=await page.locator('#settingsClearKey').evaluate(node=>{const style=getComputedStyle(node);return {minHeight:style.minHeight,height:node.getBoundingClientRect().height,paddingLeft:style.paddingLeft,paddingRight:style.paddingRight,fontSize:style.fontSize,whiteSpace:style.whiteSpace,color:style.color};});
  assert.equal(clearButton.minHeight,'32px');assert.equal(clearButton.height,32);assert.equal(clearButton.paddingLeft,'12px');assert.equal(clearButton.paddingRight,'12px');assert.equal(clearButton.fontSize,'12px');assert.equal(clearButton.whiteSpace,'nowrap');
  assert.equal(clearButton.color,await page.locator('#settingsKeyWarning').evaluate(node=>getComputedStyle(node).color));
  if(evidence)await page.screenshot({path:path.join(evidence,'settings-clear-key-dark.png')});
  await page.locator('#settingsClearKey').click();await waitStatus('已清除');assert.equal(await page.locator('#settingsProviderList').isVisible(),false);assert.equal(await page.locator('#settingsClearKey').isVisible(),false);
  await page.locator('#settingsBase').fill('https://changed.example/v1');assert.equal(await page.locator('#settingsKeyWarning').isVisible(),true);
  await page.locator('#settingsTestEditor').click();await waitStatus('地址已变更');
  await page.locator('#settingsEditor button[type=submit]').click();await waitStatus('地址已变更');assert.match(await card('deepseek').textContent(),/重新填写/);
  const boundActiveBefore=settings.active,boundActivationsBefore=writes.filter(item=>item.action==='activate').length;
  await card('deepseek').getByRole('button',{name:'启用',exact:true}).click();
  assert.equal(settings.active,boundActiveBefore);assert.equal(writes.filter(item=>item.action==='activate').length,boundActivationsBefore);
  assert.equal(await page.locator('#settingsKeyWarning').textContent(),'请先填写 API Key');assert.equal(await page.locator('#settingsKey').evaluate(node=>node===document.activeElement),true);
  await page.locator('#settingsKey').fill('synthetic-new-key-4321');assert.equal(await page.locator('#settingsKeyWarning').isVisible(),false);
  if(evidence)await page.screenshot({path:path.join(evidence,'settings-editor-dark.png')});
  await page.locator('#settingsEditor button[type=submit]').click();await waitStatus('配置已保存');
  await card('deepseek').getByRole('button',{name:'编辑',exact:true}).click();
  await page.locator('#settingsCancelEdit').click();
  await page.locator('#settingsAdd').click();await page.locator('#settingsPreset').selectOption('custom');
  await page.locator('#settingsName').fill('合成自定义');await page.locator('#settingsBase').fill('http://127.0.0.1:18860/v1');await page.locator('#settingsModel').fill('synthetic-model');
  await page.locator('#settingsEditor button[type=submit]').click();await waitStatus('配置已保存');await card('custom1').waitFor();
  await card('custom1').getByRole('button',{name:'编辑',exact:true}).click();assert.equal(await page.locator('#settingsDangerZone').isVisible(),true);assert.equal(await page.locator('#settingsDangerZone #settingsDelete').count(),1);await page.locator('#settingsDelete').click();await waitStatus('已删除');assert.equal(await card('custom1').count(),0);
  await page.locator('[data-settings-tab=appearance]').click();await page.locator('[data-settings-theme=light]').click();
  if(evidence)await page.screenshot({path:path.join(evidence,'settings-light.png')});
  await page.locator('[data-settings-tab=ai]').click();const lightContrast=await contrast();assert.ok(lightContrast.every(value=>value>=4.5),JSON.stringify(lightContrast));
  assert.equal(await card('custom0').locator('.settings-provider-key').textContent(),'未配置密钥');
  assert.notEqual(await card('custom0').locator('.settings-provider-key').evaluate(node=>getComputedStyle(node).color),await card('custom0').locator('.settings-provider-details').evaluate(node=>getComputedStyle(node).color));
  if(evidence)await page.screenshot({path:path.join(evidence,'settings-list-light.png')});
  assert.equal(await page.locator('[data-settings-tab]').count(),4);
  await page.evaluate(()=>window.invoiceSettings.open('organize'));await page.locator('#settingsAutoUpload').waitFor();
  assert.equal(await page.locator('#settingsAutoUpload').isChecked(),true);assert.equal(await page.locator('#settingsOrganizeModel').isChecked(),true);
  assert.match(await page.locator('#settingsOrganizeModelLabel').textContent(),/^当前模型：WorkBuddy/);
  assert.equal(await page.locator('#settingsModelUnavailable').isVisible(),false);
  for(const id of ['settingsAutoUpload','settingsOrganizeModel','settingsMailEnabled']){
   const control=page.locator('#'+id);assert.equal(await control.getAttribute('role'),'switch');
   const style=await control.evaluate(node=>{const s=getComputedStyle(node),thumb=getComputedStyle(node,'::before');return {appearance:s.appearance,width:s.width,height:s.height,radius:s.borderRadius,thumb:thumb.width};});
   assert.deepEqual(style,{appearance:'none',width:'40px',height:'24px',radius:'999px',thumb:'16px'});
  }
  await page.locator('#settingsAutoUpload').focus();assert.equal(await page.locator('#settingsAutoUpload').evaluate(node=>getComputedStyle(node).outlineStyle),'solid');
  await page.locator('#settingsAutoUpload').uncheck();await waitStatus('自动整理设置已保存');assert.equal(settings.organize.auto_after_upload,false);
  organizeFails=true;await page.locator('#settingsAutoUpload').check();await waitStatus('合成保存失败');assert.equal(await page.locator('#settingsAutoUpload').isChecked(),false);
  await page.locator('#settingsAutoUpload').check();await waitStatus('自动整理设置已保存');
  await page.locator('#settingsOrganizeModel').uncheck();await waitStatus('自动整理设置已保存');assert.equal(settings.organize.use_model,false);
  await page.locator('#settingsOrganizeModel').check();await waitStatus('自动整理设置已保存');
  await page.locator('#settingsChangeModel').click();await page.locator('#settingsEnabled').uncheck();await waitStatus('AI 兜底设置已保存');
  await page.locator('[data-settings-tab=organize]').click();assert.equal(await page.locator('#settingsOrganizeModel').isDisabled(),true);assert.equal(await page.locator('#settingsOrganizeModel').isChecked(),false);assert.equal(settings.organize.use_model,true);
  assert.match(await page.locator('#settingsModelUnavailable').textContent(),/还没有可用的模型/);
  assert.equal(await page.locator('#settingsModelAvailable').isVisible(),false);assert.equal(await page.getByText('未配置模型',{exact:true}).count(),0);
  await page.evaluate(()=>document.documentElement.dataset.theme='dark');await page.setViewportSize({width:390,height:844});
  const disabledSwitch=await page.locator('#settingsOrganizeModel').evaluate(node=>{const s=getComputedStyle(node);return {radius:s.borderRadius,opacity:Number(s.opacity),width:node.getBoundingClientRect().width,height:node.getBoundingClientRect().height};});
  assert.deepEqual(disabledSwitch,{radius:'999px',opacity:.5,width:40,height:24});
  const link=page.locator('#settingsConfigureModel');await link.hover();assert.equal(await link.evaluate(node=>getComputedStyle(node).textDecorationLine),'underline');
  assert.equal(await link.evaluate(node=>getComputedStyle(node).backgroundColor),'rgba(0, 0, 0, 0)');
  await page.setViewportSize({width:1440,height:900});await page.evaluate(()=>document.documentElement.dataset.theme='light');
  await page.locator('#settingsConfigureModel').click();await page.locator('#settingsEnabled').check();await waitStatus('AI 兜底设置已保存');await page.locator('[data-settings-tab=organize]').click();assert.equal(await page.locator('#settingsOrganizeModel').isChecked(),true);
  if(evidence)await page.screenshot({path:path.join(evidence,'settings-organize.png')});
  const latestEvent=await page.evaluate(()=>window.settingsEvents.at(-1));assert.ok(latestEvent.organize&&latestEvent.mail&&latestEvent.providers);
  await page.locator('[data-settings-tab=mail]').click();assert.equal(await page.locator('#settingsMailFolder').inputValue(),'INBOX');assert.equal(await page.locator('#settingsMailAuth').inputValue(),'');assert.match(await page.locator('#settingsMailAuthState').textContent(),/已保存在 Windows 凭据管理器/);
  assert.equal(await page.locator('#settingsMailSince').getAttribute('min'),'2026-10-27');assert.equal(await page.locator('#settingsMailInterval').getAttribute('min'),'1');assert.equal(await page.locator('#settingsMailInterval').getAttribute('max'),'60');
  assert.match(await page.locator('#settingsMailProject').locator('..').textContent(),/匹配不到项目时归入/);assert.equal(await page.locator('#settingsMailProject option').first().textContent(),'留在收件箱，由我分配');assert.equal(await page.locator('#settingsMailRoutingHelp').textContent(),'项目名称 → 项目时间 → 归入项目 → 留在收件箱');
  await page.locator('#settingsMailAddress').fill('new-sample@163.com');assert.match(await page.locator('#settingsMailAuthState').textContent(),/未保存/);await page.locator('#settingsMailAuth').fill('synthetic-mail-auth-only');assert.equal(await page.locator('#settingsMailAuth').getAttribute('type'),'password');
  await page.locator('#settingsMailTest').click();await page.getByText('连接成功，文件夹可用',{exact:true}).waitFor();mailTestFails=true;await page.locator('#settingsMailTest').click();await page.getByText('合成连接失败',{exact:true}).waitFor();
  await page.locator('#settingsMailInterval').fill('5');await page.locator('#settingsMailProject').selectOption('p1');await page.locator('#settingsMailEnabled').check();await page.locator('#settingsMailForm button[type=submit]').click();await waitStatus('收件邮箱设置已保存');assert.equal(settings.mail.interval_seconds,300);assert.equal(settings.mail.default_project_id,'p1');assert.equal(await page.locator('#settingsMailAuth').inputValue(),'');assert.ok(!(await page.locator('#settingsDialog').textContent()).includes('synthetic-mail-auth-only'));
  assert.equal(await page.evaluate(()=>JSON.stringify(window.settingsEvents).includes('synthetic-mail-auth-only')),false);
  await page.locator('#settingsMailCollect').click();await page.getByText('已开始收件',{exact:true}).waitFor();assert.match(await page.locator('#settingsMailLastChecked').textContent(),/上次检查/);
  if(evidence)await page.screenshot({path:path.join(evidence,'settings-mail.png')});
  await page.mouse.click(5,5);assert.equal(await page.locator('#settingsDialog').evaluate(node=>node.open),false);
  await page.setViewportSize({width:390,height:844});await page.locator('#settingsCompactOpen').click();
  const box=await page.locator('#settingsDialog').boundingBox();assert.equal(box.x,0);assert.equal(box.y,0);assert.equal(box.width,390);assert.equal(box.height,844);
  await page.locator('[data-settings-tab=ai]').click();
  const tabs=await page.locator('.settings-tabs button').all();const firstTab=await tabs[0].boundingBox(),lastTab=await tabs[1].boundingBox();assert.equal(firstTab.y,lastTab.y);assert.ok(lastTab.x>firstTab.x);
  assert.equal(await page.locator('.settings-content').evaluate(node=>getComputedStyle(node).paddingLeft),'16px');
  const heights=await page.locator('#settingsDialog button:visible').evaluateAll(nodes=>nodes.map(node=>node.getBoundingClientRect().height));assert.ok(heights.every(height=>height>=40),JSON.stringify(heights));
  assert.equal(await page.locator('#settingsDialog').evaluate(node=>node.scrollWidth<=node.clientWidth),true);
  assert.equal(await page.locator('.settings-content').evaluate(node=>node.scrollWidth<=node.clientWidth),true);
  if(evidence)await page.screenshot({path:path.join(evidence,'settings-narrow.png')});
  const fixedHeader=await page.locator('.settings-head').boundingBox(),fixedTabs=await page.locator('.settings-tabs').boundingBox();await page.locator('.settings-content').evaluate(node=>node.scrollTop=node.scrollHeight);assert.deepEqual(await page.locator('.settings-head').boundingBox(),fixedHeader);assert.deepEqual(await page.locator('.settings-tabs').boundingBox(),fixedTabs);
  await card('custom0').getByRole('button',{name:'编辑',exact:true}).click();if(evidence)await page.screenshot({path:path.join(evidence,'settings-editor-narrow.png')});await page.locator('#settingsCancelEdit').click();
  await page.locator('#settingsClose').click();
  await page.setViewportSize({width:1400,height:1000});await page.goto(origin+'/host');
  const inner=page.frames().find(frame=>frame.url().includes('embedded=1'));await inner.locator('html.embedded-workbench').waitFor();
  assert.equal(await inner.locator('#settingsCompactOpen').isVisible(),true);assert.equal(await inner.locator('#settingsOpen').isVisible(),false);
  await inner.locator('#settingsCompactOpen').click();await inner.locator('[data-settings-theme=dark]').click();await inner.locator('#settingsClose').click();
  for(const source of ['page','environment','not_required']){
   const id=source==='not_required'?'workbuddy':'custom0';settings.providers.find(provider=>provider.id===id).key_source=source;
   await page.goto(origin);await page.locator('#settingsOpen').click();await page.locator('[data-settings-tab=ai]').click();await card(id).waitFor();
   if(settings.active===id){await card('glm').getByRole('button',{name:'启用',exact:true}).click();await waitStatus('已切换');}
   await card(id).getByRole('button',{name:'启用',exact:true}).click();await waitStatus('已切换');assert.equal(settings.active,id);assert.equal(await page.locator('#settingsEditor').isVisible(),false);
  }
  assert.deepEqual(errors,[]);
  return {passed:true,cases:['dialog close and backdrop','CSS theme cards and hidden select persistence','AI toggle','provider activation and button hierarchy','missing key opens focused editor without activate request','saved page/file/environment and WorkBuddy providers can activate','outlined connection test button matches save height','small danger clear-key button visible only for page key','40px model-list button padding and no wrapping','four compact providers fit desktop','unconfigured key warning contrast >= 4.5 in both themes','secondary text contrast >= 4.5 in both themes','separate edit view and danger-zone delete','40px controls and two-column fields','models list','text success and image failure without key disclosure','host-change key warning','organize immediate save and failure rollback','unavailable model preference preserved and restored','full settings change events without auth code','mail account test and password secrecy','mail interval date and project controls','mail save and explicit collect','four narrow tabs without page overflow','narrow segmented tabs and fixed header','embedded entry'],contrast:{light:Math.min(...lightContrast),dark:Math.min(...darkContrast)},businessMutations:0,syntheticOperations:['mail collect'],writes};
 }finally{await context.close();await browser.close();}
}
module.exports=run;
if(require.main===module)run(require('playwright')).then(result=>console.log(JSON.stringify(result))).catch(error=>{console.error(error);process.exitCode=1;});
