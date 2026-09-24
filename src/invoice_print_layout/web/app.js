'use strict';
let state={items:[],inbox:[],roles:{},categories:[],stages:{}}, active=new URLSearchParams(location.search).get('matter'), view='all', checked=new Set();
let batchVerifyBusy=false;
const $=id=>document.getElementById(id);
const esc=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const money=c=>(c/100).toLocaleString('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:2});
function notify(text,error=false){const el=$(error?'error':'notice');el.textContent=text;el.hidden=false;$(error?'notice':'error').hidden=true;}
async function api(route,body){const res=await fetch('/api/'+route,{method:'POST',headers:{'Content-Type':'application/json','X-Workbench-Token':state.token},body:JSON.stringify(body||{})});const result=await res.json();if(!res.ok)throw Error(result.error||'操作未完成');return result;}
async function act(fn){try{await fn();}catch(e){notify(e.message,true);}}
async function load(){const res=await fetch('/api/state');if(!res.ok)throw Error('无法读取工作台');state=await res.json();const project=$('projectFilter').value,category=$('categoryFilter').value;$('projectFilter').innerHTML='<option value="">所有项目</option>'+[...new Set(state.items.map(x=>x.project))].sort().map(x=>`<option>${esc(x)}</option>`).join('');$('projectFilter').value=project;$('categoryFilter').innerHTML='<option value="">所有类别</option>'+state.categories.map(x=>`<option>${esc(x)}</option>`).join('');$('categoryFilter').value=category;render();}
function base(){const q=$('search').value.trim().toLowerCase();return state.items.filter(x=>(!$('projectFilter').value||x.project===$('projectFilter').value)&&(!$('categoryFilter').value||x.category===$('categoryFilter').value)&&(!q||[x.title,x.merchant,x.order_number,x.id].join(' ').toLowerCase().includes(q)));}
function matches(x,v){return v==='all'||v==='missing'&&x.stage==='draft'&&!x.complete||v==='review'&&x.stage==='draft'&&x.complete&&!x.verified||v==='ready'&&x.stage==='draft'&&x.ready||x.stage===v;}
function renderSelection(){
  const visible=base().filter(x=>matches(x,view)), selected=visible.filter(x=>checked.has(x.id)).length;
  $('selectionTools').hidden=view==='inbox';
  $('selectVisible').disabled=!visible.length;
  $('selectVisible').checked=visible.length>0&&selected===visible.length;
  $('selectVisible').indeterminate=selected>0&&selected<visible.length;
  $('clearSelected').disabled=!checked.size;
  $('exportSelected').hidden=view!=='ready';
  $('verifySelected').hidden=view!=='review';
  $('exportSelected').disabled=!checked.size;
  $('verifySelected').disabled=batchVerifyBusy||!checked.size;
  $('verifySelected').textContent=batchVerifyBusy?'正在批量核对…':'批量核对完成';
  if(view!=='inbox')$('listSummary').textContent=`${visible.length} 笔 · 已勾选 ${checked.size} 笔`+(checked.size>selected?`（其中 ${checked.size-selected} 笔不在当前列表）`:'');
}
function badge(x){return x.stage!=='draft'?state.stages[x.stage]:!x.complete?'待补材料':!x.verified?'材料齐全 · 待核对':'可提交';}
function render(){const items=base();$('inboxCount').textContent=state.inbox.length||'';$('metrics').innerHTML=[['missing','待补材料','attention'],['review','材料齐全 · 待核对',''],['ready','可提交报销','good'],['submitted','已提交 · 等待到账','']].map(([v,label,cls])=>{const a=items.filter(x=>matches(x,v));return `<button class="metric ${cls}" data-view="${v}"><small>${label}</small><b>¥ ${money(a.reduce((t,x)=>t+x.amount_cents,0))}</b><em>${a.length} 笔事项${v==='missing'?' · '+a.filter(x=>x.overdue).length+' 笔到跟进日':''}</em></button>`;}).join('');document.querySelectorAll('[data-view]').forEach(b=>b.classList.toggle('active',b.dataset.view===view));$('tabs').hidden=view==='inbox';$('exportSelected').hidden=view==='inbox';
if(view==='inbox'){$('listSummary').textContent=`${state.inbox.length} 份待关联邮件附件`;$('list').innerHTML=state.inbox.length?state.inbox.map(f=>`<div class="mailrow"><a href="/inbox/${f.id}" target="_blank" rel="noopener">${esc(f.name)}</a><div class="actions"><select id="target-${f.id}" aria-label="关联事项"><option value="">选择所属事项</option>${state.items.filter(x=>x.stage==='draft').map(x=>`<option value="${x.id}">${esc(x.title)}</option>`).join('')}</select><button data-assign="${f.id}">关联为发票</button></div><small class="muted">仅关联到你选定的事项；可在详情中改为其他用途。</small></div>`).join(''):'<div class="empty">没有待关联附件。点击“补收邮件”检查迟到的发票。</div>';}
else{const visible=items.filter(x=>matches(x,view));$('listSummary').textContent=`${visible.length} 笔 · 已勾选 ${checked.size} 笔`;$('list').innerHTML=visible.length?visible.map(x=>`<div class="row ${active===x.id?'selected':''}" data-item="${x.id}"><input type="checkbox" aria-label="选择 ${esc(x.title)}" data-check="${x.id}" ${checked.has(x.id)?'checked':''}><div><div class="title">${esc(x.title)}</div><div class="sub">${esc(x.project)} · ${esc(x.category)}<br>${esc(x.expense_date)} · ${esc(x.merchant||'未填商家')}</div>${!x.complete?`<div class="missing">缺：${esc(x.missing.join('、'))}${x.overdue?' · 已到跟进日':''}</div>`:''}${x.alternative?'<div class="missing">无发票替代：扣费记录</div>':''}</div><div class="money">¥ ${money(x.amount_cents)}<br><span class="badge ${x.ready?'good':x.stage!=='draft'?'neutral':''}">${badge(x)}</span></div></div>`).join(''):'<div class="empty"><h2>当前没有事项</h2><p>可以先登记订单，之后再补发票和明细。</p></div>';}
renderSelection();renderDetail();
for(const row of document.querySelectorAll('[data-item]')){
 const item=state.items.find(x=>x.id===row.dataset.item);
 if(item.stage==='draft'&&!item.complete){const note=document.createElement('div');note.className='sub';note.textContent=({'requested':'已申请，等待开票','not_requested':'尚未申请发票','unavailable':'无法开票'})[item.invoice_state]+` · 登记 ${item.waiting_days} 天`+(item.followup_date?' · 跟进 '+item.followup_date:'');row.querySelector('.title').parentElement.append(note);}
}}
function formFields(x={}){const field=(label,name,type='text',wide=false)=>`<label class="${wide?'wide':''}">${label}<input name="${name}" type="${type}" value="${esc(x[name]??'')}" ${name==='title'?'required maxlength="200"':''} ${name==='amount'?'required min="0" step="0.01"':''}></label>`;return `<div class="formgrid">${field('事项名称','title','text',true)}${field('项目','project')}${field('商家／酒店','merchant')}<label>费用类别<select name="category">${state.categories.map(c=>`<option ${c===x.category?'selected':''}>${esc(c)}</option>`).join('')}</select></label>${field('支出金额（元）','amount','number')}${field('消费／预订日期','expense_date','date')}${field('订单号（可后补）','order_number')}<label>开票进度<select name="invoice_state">${[['not_requested','尚未申请'],['requested','已申请，等待开票'],['unavailable','无法开票']].map(([v,t])=>`<option value="${v}" ${v===x.invoice_state?'selected':''}>${t}</option>`).join('')}</select></label>${field('下次跟进日期','followup_date','date')}<label class="wide">用途／备注<textarea name="note">${esc(x.note||'')}</textarea></label></div>`;}
function progressActions(x){
  if(x.stage==='draft'&&x.ready)return '<button data-action="submitted" class="primary">标记已提交</button>';
  if(x.stage==='draft'&&x.complete&&!x.verified)return '<button data-action="verify" class="primary">核对完成</button>';
  if(x.stage==='submitted')return '<button data-action="reimbursed" class="primary">确认已报销</button><button data-action="draft">退回待提交</button>';
  if(x.stage==='reimbursed'||x.stage==='cancelled')return '<button data-action="draft">退回待提交</button>';
  return '<span class="muted">请先补齐材料，再进行核对。</span><button data-action="cancelled">取消事项</button>';
}
const verifyDialog=document.createElement('dialog');
verifyDialog.id='verifyDialog';
verifyDialog.innerHTML='<h2>批量核对确认</h2><p id="verifyScope" style="white-space:pre-wrap"></p><div class="actions"><button id="cancelVerify" type="button">取消</button><button id="confirmVerify" type="button" class="primary">确认核对完成</button></div>';
document.body.append(verifyDialog);
function confirmVerification(message){return new Promise(resolve=>{
  $('verifyScope').textContent=message;
  const finish=result=>{verifyDialog.close();resolve(result);};
  $('cancelVerify').onclick=()=>finish(false);
  $('confirmVerify').onclick=()=>finish(true);
  verifyDialog.oncancel=e=>{e.preventDefault();finish(false);};
  verifyDialog.showModal();
});}
function renderDetail(){const x=state.items.find(x=>x.id===active);if(!x||view==='inbox'||!base().some(item=>item.id===x.id&&matches(item,view))){$('detail').innerHTML='<div class="empty">请选择当前列表中的事项，查看材料和报销进度。</div>';return;}$('detail').innerHTML=`<div class="eyebrow">MATTER / ${x.id}</div><h2>${esc(x.title)}</h2><span class="badge ${x.ready?'good':''}">${badge(x)}</span><div class="rule ${!x.complete?'warn':''}">${x.complete?'✓ 所需材料已具备':'还缺：'+esc(x.missing.join('、'))}${x.alternative?'<br>无发票，使用扣费记录替代。':''}<br><span class="muted">${esc(x.basis)}</span></div><form id="editForm">${formFields(x)}<div class="actions"><button type="submit">保存事项</button></div></form><div class="sectiontitle">材料夹 · ${x.attachments.length} 份</div>${x.stage==='draft'&&state.mail_search_ready?'<button type="button" id="findInvoice">查找对应发票</button>':''}<div>${x.attachments.map(a=>`<div class="attachment">${a.available?`<a href="/file/${a.id}" target="_blank" rel="noopener">↗ ${esc(a.name)}</a>`:`<span>文件缺失：${esc(a.name)}</span>`}${a.role==='package'?'<span class="muted">历史打印包（不代表已报销）</span>':`<select aria-label="${esc(a.name)} 的用途" data-role="${a.id}">${Object.entries(state.roles).filter(([r])=>r!=='package').map(([r,t])=>`<option value="${r}" ${a.role===r?'selected':''}>${t}</option>`).join('')}</select>`}</div>`).join('')||'<p class="muted">尚无材料。先登记、后补齐即可。</p>'}</div><div class="actions"><select id="uploadRole" aria-label="上传材料用途">${Object.entries(state.roles).filter(([r])=>r!=='package').map(([r,t])=>`<option value="${r}">${t}</option>`).join('')}</select><label><button type="button" id="uploadButton">＋ 上传材料</button><input type="file" id="uploadFiles" accept=".pdf,.png,.jpg,.jpeg,.webp" multiple hidden></label></div><p class="muted">可多选，每份最多20MB。分类只说明用途，不自动核验金额。</p><div class="sectiontitle">报销进度</div><div class="actions">${progressActions(x)}</div><p class="muted">飞书发送“关联 ${x.id}”，随后发来的图片／PDF会收进此事项，等待你分类。发送“结束登记”退出。</p><div class="timeline">${x.events.map(e=>`<div>${esc(e.at.replace('T',' '))}<br>${esc(e.message)}</div>`).join('')}</div>`;
window.expenseClassifier?.mount(x);
$('editForm').onsubmit=e=>{e.preventDefault();act(async()=>{await api('update',{id:active,...Object.fromEntries(new FormData(e.target))});await load();notify('事项已保存。');});};$('uploadButton').onclick=()=>$('uploadFiles').click();$('uploadFiles').onchange=e=>act(async()=>{const target=active,role=$('uploadRole').value;for(const f of e.target.files){if(f.size>20*1024*1024)throw Error('附件超过20MB：'+f.name);const data=await new Promise((resolve,reject)=>{const r=new FileReader();r.onload=()=>resolve(r.result.split(',')[1]);r.onerror=reject;r.readAsDataURL(f);});await api('upload',{id:target,name:f.name,data,role});}await load();notify('材料已保存，请核对用途。');});}
document.addEventListener('click',e=>{const b=e.target.closest('button');if(b?.dataset.view){view=b.dataset.view;render();}if(e.target.dataset.check)return;const row=e.target.closest('[data-item]');if(row&&!e.target.closest('input')){active=row.dataset.item;render();}if(b?.dataset.action)act(async()=>{await api('transition',{id:active,action:b.dataset.action});await load();notify('状态已更新。');});if(b?.dataset.assign)act(async()=>{const id=$('target-'+b.dataset.assign).value;if(!id)throw Error('请先选择事项');await api('assign',{id,file_id:b.dataset.assign,role:'invoice'});active=id;await load();notify('已关联，请在右侧确认附件用途和金额。');});});
document.addEventListener('change',e=>{if(e.target.dataset.check){if(e.target.checked)checked.add(e.target.dataset.check);else checked.delete(e.target.dataset.check);render();}if(e.target.dataset.role)act(async()=>{await api('role',{attachment_id:e.target.dataset.role,role:e.target.value});await load();});});
['projectFilter','categoryFilter'].forEach(id=>$(id).onchange=render);$('search').oninput=render;
$('selectVisible').onchange=e=>{for(const x of base().filter(x=>matches(x,view))){if(e.target.checked)checked.add(x.id);else checked.delete(x.id);}render();};
$('clearSelected').onclick=()=>{checked.clear();render();};
$('verifySelected').onclick=()=>act(async()=>{
  if(batchVerifyBusy||!checked.size)return;
  const ids=[...checked], skipped=[], failed=[];let completed=0, refreshError='';
  batchVerifyBusy=true;renderSelection();
  try{
    // Refresh eligibility before confirmation; the server checks it again on each write.
    await load();
    const candidates=[];
    for(const id of ids){
      const x=state.items.find(item=>item.id===id);
      const reason=!x?'事项已不存在':x.stage!=='draft'?'不是待提交状态':!x.complete?'材料未齐全':x.verified?'已经核对完成':'';
      if(reason)skipped.push(`${x?.title||id}：${reason}`);else candidates.push(x);
    }
    const visibleIds=new Set(base().filter(x=>matches(x,view)).map(x=>x.id));
    const hidden=candidates.filter(x=>!visibleIds.has(x.id)).length;
    if(candidates.length&&!await confirmVerification(`将 ${candidates.length} 笔事项标记为核对完成${hidden?`（含不在当前列表中的 ${hidden} 笔）`:''}，跳过 ${skipped.length} 笔。\n请确认已核对这些凭证的内容和金额；此操作不会提交或标记已报销。\n\n${candidates.slice(0,10).map(x=>x.title).join('\n')}${candidates.length>10?'\n…':''}`)){notify('已取消批量核对，事项未改变。');return;}
    for(const x of candidates){
      notify(`批量核对中：${completed+failed.length+1}/${candidates.length}…`);
      try{
        const updated=await api('transition',{id:x.id,action:'verify'});
        state.items=state.items.map(item=>item.id===x.id?updated:item);completed++;
      }catch(error){failed.push(`${x.title}：${error.message}`);}
    }
    try{await load();}catch(error){refreshError='列表刷新失败，请刷新页面核实最新状态。';}
    notify(`批量核对完成：成功 ${completed} 笔，跳过 ${skipped.length} 笔，失败 ${failed.length} 笔。未改变提交／报销状态。${refreshError}`);
    if(skipped.length||failed.length){
      const detail=document.createElement('details'), title=document.createElement('summary'), list=document.createElement('ul');
      title.textContent='查看跳过与失败原因';detail.append(title,list);
      for(const text of [...skipped.map(x=>'跳过：'+x),...failed.map(x=>'失败：'+x)]){const li=document.createElement('li');li.textContent=text;list.append(li);}
      $('notice').append(detail);
    }
  }finally{batchVerifyBusy=false;renderSelection();}
});
window.addEventListener('beforeunload',e=>{if(batchVerifyBusy){e.preventDefault();e.returnValue='';}});
$('newItem').onclick=()=>{$('createForm').innerHTML=formFields({project:$('projectFilter').value||'',category:'材料采购',expense_date:new Date().toLocaleDateString('sv-SE'),amount:''})+'<div class="actions"><button class="primary" type="submit">保存事项</button></div>';$('createDialog').showModal();};$('closeDialog').onclick=()=>$('createDialog').close();$('createForm').onsubmit=e=>{e.preventDefault();act(async()=>{const result=await api('create',Object.fromEntries(new FormData(e.target)));active=result.id;$('createDialog').close();view='all';$('projectFilter').value='';$('categoryFilter').value='';$('search').value='';await load();notify('已登记，后续可随时补材料。');});};
$('importHistory').onclick=()=>act(async()=>{const result=await api('import');await load();notify(`同步完成：新增 ${result.added} 笔，缺少文件或字段 ${result.skipped} 笔。历史记录未自动标记已报销。`);});
$('checkMail').onclick=()=>act(async()=>{$('checkMail').disabled=true;notify('正在只读检查邮箱，首次检查今天，之后补收自上次成功检查以来的邮件…');try{const result=await api('mail');view='inbox';await load();notify(result.message);}finally{$('checkMail').disabled=false;}});
$('exportSelected').onclick=()=>act(async()=>{if(!checked.size)throw Error('请先勾选要导出的事项');const result=await api('export',{ids:[...checked]});notify('导出完成（未改变报销状态）。');const area=$('notice');for(const [key,label] of [['pdf','下载总 PDF'],['md','下载 MD 清单']]){const link=document.createElement('a');link.href='/export/'+encodeURIComponent(result[key]);link.textContent='　'+label;link.target='_blank';link.rel='noopener';area.append(link);} });
act(async()=>{await load();if(state.history_import?.skipped)notify(`历史记录有 ${state.history_import.skipped} 笔缺少文件或字段，未纳入台账，请核对原工作区。`,true);});
// Old running servers can serve the new static assets before a backend reload.
// Expose the new route only once that server actually supports it.
fetch('/api/logistics').then(response=>{const link=document.getElementById('logisticsNav');if(response.ok&&link)link.hidden=false;}).catch(()=>{});
