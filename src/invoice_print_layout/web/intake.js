'use strict';
(() => {
 const rulesButton=document.createElement('button');rulesButton.textContent='经验管理';rulesButton.className='nav';document.querySelector('.rail').append(rulesButton);
 rulesButton.onclick=()=>act(async()=>{
  const response=await fetch('/api/preferences'), data=await response.json();
  if(projectScope)data.rules=data.rules.filter(r=>r.scope!=='item'||state.items.some(x=>x.id===r.match));
  const d=document.createElement('dialog');d.style.width='900px';
  const kinds={category:'分类经验',display_name:'显示简称',report_section:'报表栏目',report_group:'展示合并规则',material_basis:'材料要求'};
  d.innerHTML='<div class="dialoghead"><h2>可管理经验</h2><button data-close>关闭</button></div><p>相同商品与用途才复用；冲突停止套用。材料例外仅限本笔。</p><div data-rules></div><form class="formgrid"><input name="id" type="hidden"><input name="revision" type="hidden"><label>种类<select name="kind">'+Object.entries(kinds).map(([k,v])=>`<option value="${k}">${v}</option>`).join('')+'</select></label><label>范围<select name="scope"><option value="item">本笔费用</option><option value="exact_content">相同明确商品与用途</option><option value="receipt_default">线下收据无明确商品（用户业务默认）</option></select></label><label>适用费用／商品文字<input name="match" required></label><label>用途（复用时必填）<input name="purpose"></label><label>结果<input name="value" required></label><label>启用<input type="checkbox" name="enabled" checked></label><p class="wide">材料例外仅限本笔；金额纠正请在费用确认窗口操作。</p><button>保存规则</button><button type="button" data-apply>在当前费用应用规则</button></form>';
  d.querySelector('[data-rules]').innerHTML=data.rules.map(r=>`<article class="mailrow"><b>${esc(kinds[r.kind])} · ${esc(r.value)}</b><p>${r.scope==='item'?'本笔费用':r.scope==='receipt_default'?'线下收据业务默认':'相同商品及用途'} · ${esc(r.scope==='item'?(state.items.find(x=>x.id===r.match)?.title||r.match):r.match)} · ${esc(r.purpose)} · 来源：人工确认 · 最近使用：${esc(r.last_used||'尚未使用')} · ${r.enabled?'启用':'停用'}</p><button data-edit="${r.id}">修改／停用</button></article>`).join('');
  document.body.append(d);d.showModal();d.querySelector('[data-close]').onclick=()=>d.remove();
  const learned=document.createElement('section');learned.innerHTML='<h3>已有分类缓存与确认</h3>'+data.learned.map(r=>`<p>${esc(r.origin==='human'?'人工确认':'模型缓存')} · ${esc(r.category)} · ${r.conflict?'有冲突':r.enabled?'启用':'停用'} <button data-learned="${r.evidence_key}">修改／停用</button></p>`).join('');d.append(learned);learned.onclick=e=>act(async()=>{const r=data.learned.find(x=>x.evidence_key===e.target.dataset.learned);if(!r)return;const category=prompt('确认类别（取消则不改）',r.category);if(!category)return;const enabled=confirm('启用这条经验？取消表示停用。');await api('preferences/learned',{key:r.evidence_key,category,enabled});d.remove();rulesButton.click();});
  const form=d.querySelector('form');
  function ruleFields(){
   const match=form.elements.match,value=form.elements.value,oldMatch=match.value,oldValue=value.value;
   const matchControl=document.createElement(form.elements.scope.value==='item'?'select':'input');matchControl.name='match';matchControl.required=true;
   if(matchControl.tagName==='SELECT')matchControl.innerHTML=state.items.map(x=>`<option value="${esc(x.id)}">${esc(x.title)} · ${money(x.amount_cents)}元</option>`).join('');
   match.replaceWith(matchControl);matchControl.value=oldMatch||active||'';
   const kind=form.elements.kind.value,choices=kind==='category'?Object.fromEntries(state.categories.map(x=>[x,x])):kind==='report_section'?data.sections:kind==='material_basis'?{standard:'按类别要求准备',payment_only:'仅支付凭证（本笔例外）'}:null;
   const valueControl=document.createElement(choices?'select':'input');valueControl.name='value';valueControl.required=true;
   if(choices)valueControl.innerHTML=Object.entries(choices).map(([k,v])=>`<option value="${esc(k)}">${esc(v)}</option>`).join('');
   value.replaceWith(valueControl);if(oldValue)valueControl.value=oldValue;
  }
  form.elements.kind.onchange=ruleFields;form.elements.scope.onchange=ruleFields;ruleFields();
  d.querySelector('[data-rules]').onclick=e=>{const r=data.rules.find(x=>x.id===e.target.dataset.edit);if(!r)return;form.elements.kind.value=r.kind;form.elements.scope.value=r.scope;ruleFields();for(const [k,v] of Object.entries(r))if(form.elements[k]){if(k==='enabled')form.elements[k].checked=v;else form.elements[k].value=v;}};
  form.onsubmit=e=>{e.preventDefault();act(async()=>{const values=Object.fromEntries(new FormData(form));await api('preferences/save',{...values,revision:Number(values.revision),enabled:form.elements.enabled.checked});d.remove();rulesButton.click();});};
  form.querySelector('[data-apply]').onclick=()=>act(async()=>{const item=state.items.find(x=>x.id===active);if(!item)throw Error('请先选择费用');await api('preferences/apply',{id:item.id,version:item.version,goods:form.elements.match.value,purpose:form.elements.purpose.value});await load();notify('已应用没有冲突的经验；仍需人工核对。');});
 });
 const dialog=document.createElement('dialog');dialog.id='intakeDialog';dialog.style.cssText='width: min(1200px,95vw);max-width:95vw';
 dialog.innerHTML='<div class="dialoghead"><h2>统一收件与待处理</h2><button data-close>关闭</button></div><p>材料已保存在本机。补充材料不增加金额，自动整理不会核对或提交。</p><div class="actions"><input id="intakeFiles" type="file" multiple accept=".pdf,.png,.jpg,.jpeg,.webp"><button id="claimInvoice">二维码／链接领票</button><button id="intakeMail">纳入已补收邮件</button><select id="intakeFilter"><option value="">全部收件</option><option>金额冲突</option><option>商品不明</option><option>疑似重复</option><option>归属不明</option><option>手写难辨</option></select><button id="intakeRefresh">刷新</button></div><p id="intakeSummary" role="status"></p><div id="intakeRows"></div>';
 document.body.append(dialog);
 const openButton=document.createElement('button');openButton.textContent='收件／待处理';openButton.className='nav';document.querySelector('.rail').append(openButton);
 let entries=[];
 async function refresh(){const res=await fetch('/api/intake');if(!res.ok)throw Error('无法读取收件');entries=(await res.json()).queue.filter(q=>!projectScope||q.project_id===projectScope);draw();}
 function draw(){
  const counts={};for(const q of entries)counts[q.result]=(counts[q.result]||0)+1;
  $('intakeSummary').textContent=`已保存 ${entries.length} 份 · 新增费用 ${counts['新增费用']||0} · 补充材料 ${counts['补充材料']||0} · 重复文件 ${entries.filter(q=>q.status==='duplicate').length} · 待处理 ${entries.filter(q=>!['linked','duplicate'].includes(q.status)).length}`;
  $('intakeRows').innerHTML=entries.filter(q=>!$('intakeFilter').value||q.issues.includes($('intakeFilter').value)).map(q=>`<article class="mailrow"><a target="_blank" rel="noopener" href="/intake-file/${q.id}">${esc(q.name)}</a><p>${esc(q.source)} · ${esc(q.result)} · ${esc(q.issues.join('、'))}</p>${q.item_id?`<a href="/?matter=${q.item_id}${projectScope?'&project='+encodeURIComponent(projectScope):''}" target="_blank">查看所属费用</a>`:`<button data-analyze="${q.id}">识别／重试</button><button data-claim="${q.id}">识别领票二维码</button><button data-resolve="${q.id}">对照原件处理</button>`}</article>`).join('');
 }
 async function open(){await refresh();$('intakeMail').hidden=!!projectScope;dialog.showModal();}
 window.openUnifiedIntake=open;
 openButton.onclick=()=>act(open);$('recognizeReceipt').onclick=()=>act(open);$('recognizeReceipt').textContent='＋ 收图片／PDF并整理';
 dialog.querySelector('[data-close]').onclick=()=>dialog.close();$('intakeRefresh').onclick=()=>act(refresh);$('intakeFilter').onchange=draw;
 $('intakeFiles').onchange=()=>act(async()=>{
  const files=[...$('intakeFiles').files];if(files.length>50)throw Error('每批最多50份');
  const batch=crypto.randomUUID(),project=(state.projects||[]).find(p=>p.name===$('projectFilter').value);
  let duplicates=0,failed=0;
  for(const file of files){
   try{
    const data=await new Promise((resolve,reject)=>{const r=new FileReader();r.onload=()=>resolve(r.result.split(',')[1]);r.onerror=reject;r.readAsDataURL(file);});
    const q=await api('intake/receive',{name:file.name,data,project_id:project?.id||'',batch});
    if(q.duplicate||q.status==='duplicate')duplicates++;else await api('intake/analyze',{id:q.id});
   }catch(error){failed++;notify(file.name+'：'+error.message,true);}
  }
  $('intakeFiles').value='';await refresh();await load();notify(`本批 ${files.length} 份，重复 ${duplicates} 份，失败 ${failed} 份。已收到的材料可在收件区重试。`);
 });
 $('claimInvoice').onclick=()=>act(async()=>{const url=prompt('粘贴领票二维码对应的网址');if(!url)return;const project=(state.projects||[]).find(p=>p.name===$('projectFilter').value);const result=await api('intake/claim',{url,project_id:project?.id||''});$('intakeSummary').textContent=result.message;if(result.url){const a=document.createElement('a');a.href=result.url;a.target='_blank';a.rel='noopener noreferrer';a.textContent='打开领取入口';$('intakeSummary').append(a);}if(result.entry){await api('intake/analyze',{id:result.entry.id});await refresh();await load();}});
 $('intakeMail').onclick=()=>act(async()=>{await api('intake/mail');await refresh();for(const q of entries.filter(x=>x.status==='received'))await api('intake/analyze',{id:q.id});await refresh();await load();});
 $('intakeRows').onclick=e=>act(async()=>{
  if(e.target.dataset.claim){const result=await api('intake/claim',{id:e.target.dataset.claim});$('intakeSummary').textContent=result.message;if(result.url){const a=document.createElement('a');a.href=result.url;a.target='_blank';a.rel='noopener noreferrer';a.textContent='打开领取入口';$('intakeSummary').append(a);}if(result.entry){await api('intake/analyze',{id:result.entry.id});await refresh();}return;}
  const key=e.target.dataset.analyze;if(key){await api('intake/analyze',{id:key});await refresh();await load();return;}
  const q=entries.find(x=>x.id===e.target.dataset.resolve);if(q)edit(q);
 });
 function edit(q){
  const d=document.createElement('dialog');d.style.cssText='width:min(1200px,95vw);max-width:95vw';
  const fields={...q.fields,project_id:q.project_id,project:(state.projects||[]).find(p=>p.id===q.project_id)?.name||q.fields.project||''};
  d.innerHTML=`<div class="dialoghead"><h2>确认材料关系</h2><button data-close>关闭</button></div><div style="display:grid;grid-template-columns:1fr 1fr;gap:16px"><iframe title="原件" src="/${q.name.toLowerCase().endsWith('.pdf')?'intake-preview':'intake-file'}/${q.id}" style="width:100%;height:65vh"></iframe><form><label>所属费用<select name="item_id"><option value="">新增一笔未核对费用</option>${state.items.filter(x=>x.stage==='draft').map(x=>`<option value="${x.id}" ${(q.candidates||[]).includes(x.id)?'selected':''}>${esc(x.title)} · ${money(x.amount_cents)}元</option>`).join('')}</select></label><p>补到已有费用时只增加材料；多笔付款可都补到同一笔费用。</p><label>材料用途<select name="role">${Object.entries(state.roles).filter(([k])=>k!=='package').map(([k,v])=>`<option value="${k}" ${k===q.role?'selected':''}>${esc(v)}</option>`).join('')}</select></label>${formFields(fields)}<button class="primary">保存关系</button><pre style="white-space:pre-wrap">${esc(q.text)}</pre></form></div>`;
  document.body.append(d);d.showModal();d.querySelector('[data-close]').onclick=()=>d.remove();
  d.querySelector('form').onsubmit=e=>{e.preventDefault();act(async()=>{const f=Object.fromEntries(new FormData(e.target)),item=state.items.find(x=>x.id===f.item_id);await api('intake/resolve',{id:q.id,revision:q.revision,fields:f,role:f.role,item_id:f.item_id,expected_version:item?.version||''});d.remove();await refresh();await load();});};
 }
 const reviewButton=document.createElement('button');reviewButton.textContent='确认当前费用金额／分笔付款';document.querySelector('.toolbar').append(reviewButton);
 reviewButton.onclick=()=>act(async()=>{
  const item=state.items.find(x=>x.id===active);if(!item)throw Error('请先选择一笔费用');
  const c=item.confirmation||{};const d=document.createElement('dialog');d.style.width='900px';
  d.innerHTML=`<h2>确认购买内容及实际支出</h2><div class="actions">${item.attachments.map(a=>`<a href="/file/${a.id}" target="_blank">${esc(a.name)}</a>`).join('')}</div><form class="formgrid"><label class="wide">购买内容<input name="title" value="${esc(item.title)}" required></label><label>票面金额<input name="face_amount" type="number" step="0.01" value="${c.face_amount_cents===undefined?item.amount:c.face_amount_cents/100}" required></label><label>实际支出<input name="amount" type="number" step="0.01" value="${item.amount}" required></label><label class="wide">分笔付款（用加号分隔，例如11+3）<input name="parts" value="${(c.payments_cents||[]).map(x=>x/100).join('+')}"></label><label class="wide">纠正／确认依据<textarea name="reason">${esc(c.reason||'')}</textarea></label><button>保存本笔确认</button><button type="button" data-close>取消</button></form>`;
  document.body.append(d);d.showModal();d.querySelector('[data-close]').onclick=()=>d.remove();d.querySelector('form').onsubmit=e=>{e.preventDefault();act(async()=>{const f=Object.fromEntries(new FormData(e.target));await api('review/confirm',{...f,id:item.id,version:item.version,payments:f.parts.trim()?f.parts.split('+').map(x=>x.trim()):[]});d.remove();await load();notify('本笔确认已保存；没有扩展为其他费用的规则。');});};
 });
 const mergeButton=document.createElement('button');mergeButton.textContent='合并勾选的重复费用';document.querySelector('.toolbar').append(mergeButton);
 mergeButton.onclick=()=>act(async()=>{
  const ids=[...checked];if(ids.length<2||!checked.has(active))throw Error('勾选至少两笔费用，再点选要保留的费用');
  const preview=await api('review/merge-preview',{ids,target:active});
  const d=document.createElement('dialog');d.innerHTML=`<h2>审阅重复费用合并</h2><p>合并前 ${money(preview.before_cents)} 元，合并后 ${money(preview.after_cents)} 元。保留当前费用金额，不累加重复记录。</p><p>原记录保留为已取消，并标记合并去向。</p><ul>${preview.attachments.map(a=>`<li><a href="/file/${a.id}" target="_blank">${esc(a.name)}</a></li>`).join('')}</ul><textarea placeholder="确认是重复支出的依据"></textarea><button data-confirm>确认合并</button><button data-close>取消</button>`;
  document.body.append(d);d.showModal();d.querySelector('[data-close]').onclick=()=>d.remove();d.querySelector('[data-confirm]').onclick=()=>act(async()=>{await api('review/merge',{ids,target:preview.target,token:preview.token,reason:d.querySelector('textarea').value});d.remove();checked.clear();await load();});
 });
})();
