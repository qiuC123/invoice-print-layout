'use strict';
(() => {
 const el=id=>document.getElementById(id),dialog=el('settingsDialog');
 if(!dialog)return;
 const presets={
  workbuddy:{id:'workbuddy',name:'WorkBuddy 本机',kind:'workbuddy',base_url:'http://127.0.0.1:3425/v1',model:'workbuddy/glm-5.3-flash'},
  glm:{id:'glm',name:'智谱官方',kind:'openai',base_url:'https://open.bigmodel.cn/api/paas/v4',model:'glm-5.3-flash'},
  deepseek:{id:'deepseek',name:'DeepSeek 官方',kind:'openai',base_url:'https://api.deepseek.com/v1',model:'deepseek-flash'},
  custom:{id:'',name:'自定义供应商',kind:'openai',base_url:'',model:''}
 };
 let settings=null,editing=null,busy=false,opener=null,loading=null;
 const status=(text,error=false)=>{el('settingsStatus').textContent=text;el('settingsStatus').hidden=!text;el('settingsStatus').classList.toggle('is-error',error);};
 async function request(route,body){
  const response=await fetch('/api/settings'+route,body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json','X-Workbench-Token':state.token},body:JSON.stringify(body)});
  const result=await response.json();if(!response.ok)throw Error(result.error||'设置操作未完成');return result;
 }
 function changed(){window.dispatchEvent(new CustomEvent('invoice-settings-changed',{detail:settings}));}
 async function action(work){
  if(busy)return;busy=true;dialog.setAttribute('aria-busy','true');
  const controls=[...dialog.querySelectorAll('.settings-content button,.settings-content input,.settings-content select')],disabled=controls.map(node=>node.disabled);
  controls.forEach(node=>node.disabled=true);status('');
  try{await work();}catch(error){status(error.message,true);}
  finally{busy=false;dialog.removeAttribute('aria-busy');controls.forEach((node,index)=>{if(node.isConnected)node.disabled=disabled[index];});if(settings)renderOrganize();}
 }
 function needsKey(provider){return provider.kind!=='workbuddy'&&(provider.key_required||!['page','file','environment'].includes(provider.key_source));}
 function keyStatus(provider){
  if(provider.key_required)return '地址已变更，请重新填写 API Key';
  if(provider.kind==='workbuddy'||provider.key_source==='not_required')return '无需密钥';
  if(provider.key_source==='page')return `已配置 ••••${provider.key_last4||''}（页面保存）`;
  if(provider.key_source==='file')return '使用本机密钥文件';
  if(provider.key_source==='environment')return '使用环境变量密钥';
  return '未配置密钥';
 }
 function button(label,click,primary=false){const node=document.createElement('button');node.type='button';node.textContent=label;node.className=primary?'primary':'settings-secondary';node.onclick=()=>action(click);return node;}
 function render(){
  renderOrganize();renderMail();
  el('settingsEnabled').checked=settings.enabled;
  el('settingsProviders').replaceChildren(...settings.providers.map(provider=>{
   const card=document.createElement('article');card.className='settings-provider'+(settings.active===provider.id?' is-active':'');card.dataset.providerId=provider.id;
   const info=document.createElement('div');info.className='settings-provider-info';
   const title=document.createElement('div');title.className='settings-provider-title';
   const name=document.createElement('strong');name.textContent=provider.name;title.append(name);
   const tag=document.createElement('span');tag.className='settings-tag';tag.textContent=provider.kind==='workbuddy'?'WorkBuddy':'OpenAI 兼容';title.append(tag);
   if(settings.active===provider.id){const active=document.createElement('span');active.className='settings-tag settings-active';active.textContent='使用中';title.append(active);}
   info.append(title);
   let host=provider.base_url;try{host=new URL(provider.base_url).host;}catch(_){/* Invalid configurations remain editable. */}
   const details=document.createElement('p');details.className='settings-provider-details';details.textContent=`${host} · ${provider.model}`;details.title=details.textContent;info.append(details);
   const key=document.createElement('p');key.className='settings-provider-key';key.classList.toggle('is-warning',needsKey(provider));key.textContent=keyStatus(provider);info.append(key);card.append(info);
   const actions=document.createElement('div');actions.className='settings-provider-actions';
   if(settings.active!==provider.id){
    const activate=button('启用',async()=>{settings=await request('',{action:'activate',id:provider.id});render();changed();status('已切换供应商，下一次请求立即生效。');},true);
    if(needsKey(provider))activate.onclick=()=>{if(busy)return;openEditor(provider);el('settingsKeyWarning').textContent='请先填写 API Key';el('settingsKey').focus();};
    actions.append(activate);
   }
   const edit=document.createElement('button');edit.type='button';edit.className='settings-secondary';edit.textContent='编辑';edit.onclick=()=>openEditor(provider);
   actions.append(edit,button('测试',()=>test(provider)));
   card.append(actions);return card;
  }));
 }
 async function loadSettings(){
  if(!loading)loading=request('').then(result=>{settings=result;render();}).finally(()=>{loading=null;});
  return loading;
 }
 function tab(name){
  if(!['appearance','ai','organize','mail'].includes(name))name='appearance';
  dialog.dataset.settingsPage=name;
  for(const [key,id] of Object.entries({appearance:'settingsAppearance',ai:'settingsAI',organize:'settingsOrganize',mail:'settingsMail'}))el(id).hidden=name!==key;
  dialog.querySelectorAll('[data-settings-tab]').forEach(node=>{if(node.dataset.settingsTab===name){node.setAttribute('aria-current','page');node.scrollIntoView({block:'nearest',inline:'nearest'});}else node.removeAttribute('aria-current');});
  status('');if(name!=='appearance'&&!settings)action(loadSettings);
 }
 function closeEditor(){editing=null;el('settingsEditor').hidden=true;el('settingsProviderList').hidden=false;el('settingsKey').value='';el('settingsTestResult').hidden=true;}
 function host(value){try{return new URL(value).hostname.toLowerCase();}catch(_){return '';}}
 function keyWarning(){
  const changed=editing&&(editing.key_required||(editing.id&&host(editing.base_url)!==host(el('settingsBase').value))),warning=el('settingsKeyWarning');
  warning.textContent=changed?'地址已变更，请重新填写 API Key':'请先填写 API Key';
  warning.hidden=!editing||editing.kind==='workbuddy'||!!el('settingsKey').value||!(changed||needsKey(editing));
  if(el('settingsKey').value&&el('settingsStatus').textContent==='地址已变更，请重新填写 API Key')status('');
 }
 function openEditor(provider,adding=false){
  status('');
  editing={...provider};el('settingsEditorTitle').textContent=adding?'添加供应商':'编辑供应商';el('settingsPresetLabel').hidden=!adding;
  el('settingsName').value=provider.name;el('settingsBase').value=provider.base_url;el('settingsModel').value=provider.model;el('settingsKey').value='';
  el('settingsKeyField').hidden=provider.kind==='workbuddy';el('settingsClearKey').hidden=!provider.id||provider.key_source!=='page';
  el('settingsProviderList').hidden=true;el('settingsDangerZone').hidden=adding||!provider.id||provider.builtin;
  el('settingsModels').replaceChildren();el('settingsTestResult').hidden=true;el('settingsEditor').hidden=false;
  keyWarning();el('settingsName').focus();el('settingsEditor').scrollIntoView({block:'nearest'});
 }
 function formProvider(){return {id:editing.id,name:el('settingsName').value.trim(),kind:editing.kind,base_url:el('settingsBase').value.trim(),model:el('settingsModel').value.trim()};}
 function formBody(){return {provider:formProvider(),api_key:editing.kind==='workbuddy'?'':el('settingsKey').value};}
 function testResult(result){
  const box=el('settingsTestResult');box.replaceChildren();box.hidden=false;
  const title=document.createElement('strong');title.textContent=result.ok?'连接成功':'连接测试未全部通过';box.append(title);
  for(const [key,label] of [['text','文字'],['image','图片']]){
   const item=result[key]||{},p=document.createElement('p');p.classList.toggle('is-error',!item.ok);
   p.textContent=`${label} ${item.ok?'✓':'✗'} · ${item.model||'未返回模型'} · ${item.elapsed_ms??0} ms${item.error?' · '+item.error:''}`;box.append(p);
  }
  box.scrollIntoView({block:'nearest'});
 }
 async function test(provider){el('settingsTestResult').hidden=true;status('正在测试文字与合成图片…');const result=await request('/test',provider?{provider}:formBody());status('');testResult(result);}
 function open(name='appearance'){opener=document.activeElement;const refresh=!!settings&&!dialog.open;if(!dialog.open)dialog.showModal();tab(name);if(refresh&&name!=='appearance')action(loadSettings);}
 window.invoiceSettings={open};
 for(const id of ['settingsOpen','settingsCompactOpen'])el(id).onclick=()=>open();
 el('settingsClose').onclick=()=>dialog.close();
 dialog.addEventListener('click',event=>{if(event.target!==dialog)return;const box=dialog.getBoundingClientRect();if(event.clientX<box.left||event.clientX>box.right||event.clientY<box.top||event.clientY>box.bottom)dialog.close();});
 dialog.addEventListener('close',()=>{el('settingsKey').value='';el('settingsMailAuth').value='';opener?.focus();});
 dialog.querySelectorAll('[data-settings-tab]').forEach(node=>node.onclick=()=>tab(node.dataset.settingsTab));
 el('settingsAdd').onclick=()=>{el('settingsPreset').value='workbuddy';openEditor(presets.workbuddy,true);};
 el('settingsPreset').onchange=()=>openEditor(presets[el('settingsPreset').value],true);
 el('settingsCancelEdit').onclick=closeEditor;
 el('settingsEnabled').onchange=()=>action(async()=>{try{settings=await request('',{action:'toggle',enabled:el('settingsEnabled').checked});render();changed();status('AI 兜底设置已保存。');}catch(error){el('settingsEnabled').checked=settings.enabled;throw error;}});
 el('settingsEditor').onsubmit=event=>{event.preventDefault();action(async()=>{const body=formBody();settings=await request('',{action:'save',...body});const saved=settings.providers.find(item=>item.id===body.provider.id);render();closeEditor();changed();status(saved?.key_required?'地址已变更，请重新填写 API Key':'配置已保存，下一次请求立即生效。',!!saved?.key_required);});};
 el('settingsClearKey').onclick=()=>action(async()=>{settings=await request('',{action:'clear_key',id:editing.id});const provider=settings.providers.find(item=>item.id===editing.id);render();changed();el('settingsKey').value='';el('settingsClearKey').hidden=true;if(provider)editing=provider;keyWarning();status('页面保存的密钥已清除。');});
 el('settingsDelete').onclick=()=>action(async()=>{settings=await request('',{action:'delete',id:editing.id});render();closeEditor();changed();status('供应商已删除。');});
 el('settingsBase').addEventListener('input',keyWarning);el('settingsKey').addEventListener('input',keyWarning);
 el('settingsTestEditor').onclick=()=>action(()=>test());
 el('settingsFetchModels').onclick=()=>action(async()=>{const result=await request('/models',formBody());el('settingsModels').replaceChildren(...result.models.map(id=>{const option=document.createElement('option');option.value=id;return option;}));status(`已获取 ${result.models.length} 个模型，可在模型输入框中选择。`);});
 function renderOrganize(){
  const organize=settings?.organize||{auto_after_upload:true,use_model:true,model_available:false,model_label:''};
  el('settingsAutoUpload').checked=organize.auto_after_upload!==false;
  el('settingsOrganizeModel').checked=!!organize.model_available&&organize.use_model!==false;el('settingsOrganizeModel').disabled=!organize.model_available||busy;
  el('settingsOrganizeModelLabel').textContent='当前模型：'+organize.model_label;el('settingsModelAvailable').hidden=!organize.model_available;el('settingsModelUnavailable').hidden=!!organize.model_available;
 }
 for(const [id,key] of [['settingsAutoUpload','auto_after_upload'],['settingsOrganizeModel','use_model']])el(id).onchange=()=>action(async()=>{try{settings.organize=await request('/organize',{[key]:el(id).checked});renderOrganize();changed();status('自动整理设置已保存。');}catch(error){renderOrganize();throw error;}});
 for(const id of ['settingsChangeModel','settingsConfigureModel'])el(id).onclick=()=>tab('ai');
 function authState(){const saved=!!settings?.mail?.auth_saved&&el('settingsMailAddress').value.trim()===settings.mail.address;el('settingsMailAuthState').textContent=saved?'已保存在 Windows 凭据管理器':'未保存';el('settingsMailAuthState').classList.toggle('is-warning',!saved);}
 function renderMail(){
  const mail=settings?.mail||{};
  el('settingsMailAddress').value=mail.address||'';el('settingsMailFolder').value=mail.folder||'INBOX';el('settingsMailAuth').value='';authState();
  el('settingsMailEnabled').checked=!!mail.enabled;el('settingsMailInterval').value=(mail.interval_seconds||120)/60;el('settingsMailSince').value=mail.since||'';el('settingsMailSince').min=mail.approved_since||'';
  el('settingsMailProject').replaceChildren(...[{id:'',name:'留在收件箱，由我分配'},...(mail.projects||[])].map(project=>{const option=document.createElement('option');option.value=project.id;option.textContent=project.name;return option;}));el('settingsMailProject').value=mail.default_project_id||'';
  renderMailStatus(mail);
 }
 function renderMailStatus(mail){
  const checked=mail.last_checked_at?new Date(typeof mail.last_checked_at==='number'?mail.last_checked_at*1000:mail.last_checked_at):null;
  el('settingsMailLastChecked').textContent='上次检查：'+(checked&&!Number.isNaN(checked.getTime())?checked.toLocaleString('zh-CN',{hour12:false}):'尚未检查');el('settingsMailLastError').textContent=mail.last_error||'';el('settingsMailLastError').hidden=!mail.last_error;
 }
 function mailAccount(){const body={address:el('settingsMailAddress').value.trim(),folder:el('settingsMailFolder').value.trim()||'INBOX'};if(el('settingsMailAuth').value)body.auth_code=el('settingsMailAuth').value;return body;}
 function inlineResult(id,message,error=false){const node=el(id);node.textContent=message;node.hidden=false;node.classList.toggle('is-error',error);}
 el('settingsMailAddress').addEventListener('input',authState);
 el('settingsMailTest').onclick=()=>{if(!el('settingsMailAddress').reportValidity())return;action(async()=>{try{const result=await request('/mail/test',mailAccount());inlineResult('settingsMailTestResult',result.message,!result.ok);}catch(error){inlineResult('settingsMailTestResult',error.message,true);}});};
 el('settingsMailForm').onsubmit=event=>{event.preventDefault();action(async()=>{settings.mail=await request('/mail',{...mailAccount(),enabled:el('settingsMailEnabled').checked,interval_seconds:Number(el('settingsMailInterval').value)*60,since:el('settingsMailSince').value,default_project_id:el('settingsMailProject').value});renderMail();changed();status('收件邮箱设置已保存。');});};
 el('settingsMailCollect').onclick=()=>action(async()=>{const result=await api('agent/mail/collect',{});inlineResult('settingsMailCollectResult',result.message||'已开始收件，收件进度会同步到主页面。');const fresh=await request('');settings.mail=fresh.mail;renderMailStatus(settings.mail);changed();});
 const themeSelect=el('themeSelect'),themeCards=[...dialog.querySelectorAll('[data-settings-theme]')];
 function syncTheme(){themeCards.forEach(node=>{const selected=node.dataset.settingsTheme===document.documentElement.dataset.theme;node.setAttribute('aria-checked',String(selected));node.tabIndex=selected?0:-1;});}
 themeCards.forEach((node,index)=>{node.onclick=()=>{themeSelect.value=node.dataset.settingsTheme;themeSelect.dispatchEvent(new Event('change',{bubbles:true}));syncTheme();};node.onkeydown=event=>{if(!['ArrowLeft','ArrowRight','ArrowUp','ArrowDown'].includes(event.key))return;event.preventDefault();const next=themeCards[(index+1)%themeCards.length];next.click();next.focus();};});
 themeSelect.addEventListener('change',syncTheme);new MutationObserver(syncTheme).observe(document.documentElement,{attributes:true,attributeFilter:['data-theme']});syncTheme();dialog.dataset.settingsPage='appearance';
})();
