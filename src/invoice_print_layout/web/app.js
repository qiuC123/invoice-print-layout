'use strict';
const projectScope=new URLSearchParams(location.search).get('project')||'';
const embeddedWorkbench=!!projectScope&&new URLSearchParams(location.search).get('embedded')==='1'&&window.parent!==window;
if(embeddedWorkbench)document.documentElement.classList.add('embedded-workbench');
if(embeddedWorkbench)document.addEventListener('DOMContentLoaded',()=>{if(projectContext())renderProjectContext();});
function scopeData(data){
 if(!projectScope)return data;
 if(!(data.projects||[]).some(p=>p.id===projectScope))throw Error('项目不存在，请返回项目列表重新选择。');
 return {...data,items:data.items.filter(x=>x.project_id===projectScope),inbox:[]};
}
function projectContext(){return (state.projects||[]).find(p=>p.id===projectScope);}
function currentInvoiceProjectId(){return projectContext()?.id||'';}
let projectReimbursementView=null;
function projectReimbursementStatus(project){return project?.reimbursement_status==='reimbursed'?'reimbursed':'unreimbursed';}
function renderProjectNavigation(){
 const projects=state.projects||[];
 if(projectReimbursementView===null)projectReimbursementView=projectReimbursementStatus(projectContext());
 let nav=$('projectWorkbenches');
 if(!nav){nav=document.createElement('nav');nav.id='projectWorkbenches';nav.setAttribute('aria-label','项目报销工作台');document.querySelector('.rail').append(nav);}
 const statuses=[['unreimbursed','未报销'],['reimbursed','已报销']];
 const visible=projects.filter(p=>projectReimbursementStatus(p)===projectReimbursementView).sort((a,b)=>Number(!!b.pinned)-Number(!!a.pinned));
 nav.innerHTML=`<div class="project-status-tabs" role="group" aria-label="项目报销分类">${statuses.map(([value,label])=>`<button type="button" data-project-status="${value}" aria-pressed="${value===projectReimbursementView}" class="${value===projectReimbursementView?'active':''}">${label} <span>${projects.filter(p=>projectReimbursementStatus(p)===value).length}</span></button>`).join('')}</div><div id="projectLinks">${visible.map(p=>`<a href="/?project=${encodeURIComponent(p.id)}" data-project-menu-id="${esc(p.id)}" aria-haspopup="menu" ${p.id===projectScope?'aria-current="page"':''}>${p.pinned?'<span class="project-pin" aria-hidden="true">⌖</span>':''}${esc(p.name)} · 发票报销</a>`).join('')||'<p class="project-empty">暂无项目</p>'}</div>`;
}
function renderProjectContext(){
 const project=projectContext();
 const invoiceAssistant=state.app_mode==='invoice';
 document.documentElement.classList.toggle('invoice-assistant',invoiceAssistant);
 document.querySelector('h1').textContent=project?`${project.name} · 发票助手`:'全部项目台账';
 document.title=project?`${project.name} · 发票助手`:'发票助手 · 全部项目';
 $('logisticsNav').hidden=!state.logistics_available;
 document.querySelector('.rail [data-view="inbox"]').hidden=invoiceAssistant||!!projectScope;
 const peopleButton=$('reimbursementPeople');if(peopleButton){peopleButton.hidden=!project;peopleButton.disabled=!project;}
 renderProjectNavigation();
 window.refreshProjectMenuTrigger?.();
 if(!projectScope)return;
 $('logisticsNav').href='/logistics?project='+encodeURIComponent(projectScope);$('logisticsNav').textContent='返回本项目';
 $('importHistory').hidden=true;$('moveProject').hidden=true;
 if(embeddedWorkbench){
  // Keep the existing actions and handlers, but place them inside the project panel.
  for(const button of document.querySelectorAll('.rail>button:not([data-view])')){button.classList.remove('nav');document.querySelector('.toolbar').append(button);}
 }
}
function normalizeExpenseView(value){return ['review','submitted','deleted','inbox'].includes(value)?value:'review';}
let state={items:[],inbox:[],roles:{},categories:[],stages:{}}, active=new URLSearchParams(location.search).get('matter'), view=normalizeExpenseView(new URLSearchParams(location.search).get('view')), checked=new Set(),initialExpenseView=true;
let pendingMaterialDeskOpen=new URLSearchParams(location.search).get('view')==='synthesis';
let batchVerifyBusy=false;
let singleVerifyBusy=false;
let batchReturnBusy=false;
const $=id=>document.getElementById(id);
const esc=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const money=c=>(c/100).toLocaleString('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:2});
function expenseFilterValues(id){const select=$(id);return window.invoiceMultiFilters?.values(id)||[...(select?.selectedOptions||[])].map(option=>option.value).filter(Boolean);}
function setExpenseFilterValues(id,values){if(window.invoiceMultiFilters)window.invoiceMultiFilters.set(id,values);else $(id).value=values[0]||'';}
function matchesExpenseCategory(category){const selected=expenseFilterValues('categoryFilter');return !selected.length||selected.includes(category);}
window.matchesExpenseCategory=matchesExpenseCategory;
function dialogLoadingMarkup(text){return `<div class="dialog-loading" role="status"><progress aria-hidden="true"></progress><p>${esc(text)}</p></div>`;}
function dialogLoadErrorMarkup(text){return `<div class="dialog-load-error" role="alert"><p>${esc(text)}</p><button type="button" data-dialog-retry>重试</button></div>`;}
function notify(text,error=false){const el=$(error?'error':'notice');el.textContent=text;el.hidden=false;$(error?'notice':'error').hidden=true;if(error&&projectScope&&text.includes('项目不存在')){const link=document.createElement('a');link.href='/';link.textContent='返回项目列表';el.append(document.createTextNode(' '),link);}const dialog=[...document.querySelectorAll('dialog[open]')].at(-1);if(dialog){let message=dialog.querySelector('.dialog-message');if(!message){message=document.createElement('p');message.className='dialog-message';message.setAttribute('role','alert');dialog.prepend(message);}message.textContent=text;message.classList.toggle('is-error',error);}}
async function api(route,body){
 if(projectScope){const project=projectContext();if(!project)throw Error('项目不存在，请返回项目列表重新选择。');
  if(['create','update'].includes(route))body={...body,project_id:project.id,project:project.name};
  if(['intake/receive','intake/claim','mail'].includes(route))body={...body,project_id:project.id};
  if(route==='intake/resolve')body={...body,fields:{...body.fields,project_id:project.id,project:project.name}};
 }
 const res=await fetch('/api/'+route,{method:'POST',headers:{'Content-Type':'application/json','X-Workbench-Token':state.token},body:JSON.stringify(body||{})});const result=await res.json();if(!res.ok){const error=Error(result.error||'操作未完成');error.status=res.status;throw error;}return result;}
async function act(fn){try{await fn();}catch(e){notify(e.message,true);}}
async function load({beforeRender,skipClassification=false}={}){
  const res=await fetch('/api/state');if(!res.ok)throw Error('无法读取工作台');
  state=scopeData(await res.json());expensePreviewRequests.clear();
  if(initialExpenseView){const item=state.items.find(x=>x.id===active);if(item){view=expenseViewFor(item);if(returnedToSynthesis(item)){pendingMaterialDeskOpen=true;active=null;}}initialExpenseView=false;}
  checked=new Set([...checked].filter(id=>state.items.some(x=>x.id===id&&!x.deleted&&!returnedToSynthesis(x))));
  const categories=expenseFilterValues('categoryFilter');
  $('categoryFilter').innerHTML='<option value="">所有类别</option>'+state.categories.map(x=>`<option>${esc(x)}</option>`).join('');setExpenseFilterValues('categoryFilter',categories.filter(value=>state.categories.includes(value)));
  try{await window.reimbursementPeopleUI?.refresh(currentInvoiceProjectId());}catch(_){/* Personnel retrieval has an independent retry in its panel. */}
  renderPersonFilter();
  beforeRender?.();
  renderProjectContext();render();if(!skipClassification)window.expenseClassifier?.schedule();openPendingMaterialDesk();
  void Promise.resolve(window.manualInvoiceAgent?.refresh?.()).catch(()=>{});
}
function openPendingMaterialDesk(){
 if(!pendingMaterialDeskOpen||!state.token||typeof window.openMaterialMatching!=='function')return;
 pendingMaterialDeskOpen=false;
 void Promise.resolve(window.openMaterialMatching()).catch(error=>notify('无法打开材料合成台：'+error.message,true));
}
document.addEventListener('DOMContentLoaded',openPendingMaterialDesk);
function renderPersonFilter(){
 const select=$('personFilter');if(!select)return;
 const previous=expenseFilterValues('personFilter');
 select.innerHTML=window.reimbursementPeopleUI?.filterOptions('')||'<option value="">所有人员</option><option value="__unassigned__">未指定</option>';
 const valid=previous.filter(value=>[...select.options].some(option=>option.value===value));
 select.disabled=!currentInvoiceProjectId();setExpenseFilterValues('personFilter',valid);
 if(previous.length!==valid.length)notify(valid.length?'部分筛选人员已退出有效名单，保留其他人员筛选。':'原筛选人员已退出有效名单，已显示所有人员；当前项目保持。');
}
function matchesReimbursementPerson(record,value=null){
 const selected=value===null?expenseFilterValues('personFilter'):Array.isArray(value)?value:value?[value]:[];
 if(!selected.length)return true;
 return selected.some(person=>window.reimbursementPeopleUI?.matchesPerson?window.reimbursementPeopleUI.matchesPerson(record,person):person==='__unassigned__'?!record?.reimbursement_person_id:record?.reimbursement_person_id===person);
}
window.matchesReimbursementPersonGroup=(cards,suggestion={})=>{
 const value=expenseFilterValues('personFilter');if(!value.length)return true;
 const expected=(suggestion.voucher_keys?.length||0)+(suggestion.payment_ids?.length||0);
 const ids=new Set(cards.map(card=>card.reimbursement_person_id||''));
 const id=cards.length&&(!expected||cards.length===expected)&&ids.size===1?[...ids][0]:'';
 const owner=id&&cards.every(card=>matchesReimbursementPerson(card,id))?id:'';
 return matchesReimbursementPerson({...suggestion,reimbursement_person_id:owner},value);
};
document.addEventListener('reimbursement-people-changed',event=>{
 if(event.detail?.projectId&&event.detail.projectId!==currentInvoiceProjectId())return;
 renderPersonFilter();render();
});
function matchesExpenseIssue(x,value=null){
 if(value===null){const values=expenseFilterValues('issueFilter');return !values.length||values.some(selected=>matchesExpenseIssue(x,selected));}
 if(value==='missing')return (x.missing||[]).some(reason=>reason!=='金额待确认');
 if(value==='amount')return materialAmountDiffers(x);
 if(value==='complete')return !!x.complete||!!x.amount_pending&&(x.missing||[]).length===1&&x.missing[0]==='金额待确认';
 return true;
}
function expenseGoods(x){return Array.isArray(x.agent_content?.goods)?x.agent_content.goods.filter(g=>g&&typeof g==='object'&&!Array.isArray(g)):[];}
window.invoiceCheckMarkup=checks=>(checks||[]).map(check=>{
 const company=check.company_check;const companyText=company?`<small class="invoice-company-check" ${company.status==='mismatch'?'style="color:var(--danger,#b42318)"':''}>${company.status==='unreadable'?'未读取抬头':company.status==='mismatch'?'发票抬头/税号与公司信息不一致':'发票抬头核对：已读取'+(company.tax_id_read?' · 税号已读取':' · 未读取税号')}</small>`:'';
 const qr=check.qr_check;if(!qr)return companyText;
 const labels={invoice_number:'号码',amount:'金额',issue_date:'日期'};
 const text=qr.status==='unreadable'?'未读取':[...(qr.checked||[]).map(key=>(labels[key]||key)+'✓'),...(qr.mismatches||[]).map(key=>(labels[key]||key)+'不一致'),...(qr.not_compared||[]).map(key=>(labels[key]||key)+'未比较')].join(' ');
 return companyText+`<small class="invoice-check" ${qr.status==='mismatch'?'style="color:var(--danger,#b42318)"':''}>${check.name?esc(check.name)+' · ':''}二维码核对：${esc(text)}</small>`;
}).join('');
function matchesExpenseFilters(x){const q=$('search').value.trim().toLowerCase();return matchesReimbursementPerson(x)&&matchesExpenseIssue(x)&&matchesExpenseCategory(x.category)&&(!q||[x.title,x.merchant,x.order_number,x.id,...expenseGoods(x).flatMap(g=>[g.name,g.raw_name].filter(v=>typeof v==='string'))].join(' ').toLowerCase().includes(q));}
function base(){return state.items.filter(matchesExpenseFilters);}
function retiredExpense(x){return !!x.deleted||x.stage==='cancelled'||!!x.merged_into;}
function returnedToSynthesis(x){return x?.returned_to_synthesis===true;}
function pendingReview(x){return !retiredExpense(x)&&!returnedToSynthesis(x)&&x.stage==='draft'&&(!x.verified||!x.ready);}
function reviewFieldsEditable(x){return view==='review'&&!!x&&pendingReview(x);}
function expenseViewFor(x){return retiredExpense(x)?'deleted':submissionReady(x)?'submitted':'review';}
function submissionReady(x){return !x.deleted&&!returnedToSynthesis(x)&&(x.stage==='draft'&&x.ready||['submitted','reimbursed'].includes(x.stage));}
function reportEligible(x){return !!x&&!x.deleted&&!returnedToSynthesis(x)&&x.stage==='draft'&&x.ready&&x.verified;}
function expenseProjectReimbursed(x){return projectReimbursementStatus((state.projects||[]).find(p=>p.id===x.project_id))==='reimbursed';}
function pendingSubmission(x){return reportEligible(x)&&!expenseProjectReimbursed(x);}
function reportSelectionIds(ids=[...checked]){
 if(view!=='submitted')throw Error('请到“已核对”选择已核对费用制作报销包。');
 if(!ids.length)throw Error('请先勾选要制作的事项');
 if(ids.some(id=>!reportEligible(state.items.find(x=>x.id===id))))throw Error('勾选事项含未核对、缺项确认已失效或已有历史报销包的费用，请刷新并重新选择已核对费用。');
 return ids;
}
function matches(x,v){v=normalizeExpenseView(v);if(retiredExpense(x))return v==='deleted';if(v==='deleted'||returnedToSynthesis(x))return false;return v==='submitted'?submissionReady(x):v==='review'&&pendingReview(x);}
function renderSelection(){
  const visible=base().filter(x=>matches(x,view)), selected=visible.filter(x=>checked.has(x.id)).length;
  for(const input of $('list').querySelectorAll('[data-check]'))input.checked=checked.has(input.dataset.check);
  $('selectionTools').hidden=['inbox','deleted'].includes(view);
  $('selectVisible').disabled=!visible.length;
  $('selectVisible').checked=visible.length>0&&selected===visible.length;
  $('selectVisible').indeterminate=selected>0&&selected<visible.length;
  $('exportSelected').hidden=view!=='submitted';
  $('verifySelected').hidden=view!=='review';
  $('exportSelected').disabled=view!=='submitted'||!checked.size||[...checked].some(id=>!reportEligible(state.items.find(x=>x.id===id)));
  $('verifySelected').disabled=batchVerifyBusy||singleVerifyBusy||batchReturnBusy||!checked.size;
  $('verifySelected').textContent=batchVerifyBusy?'正在核对…':'核对完成';
  const assign=$('bulkAssignExpenses'),chosen=visible.filter(x=>checked.has(x.id));
  const returnButton=$('returnSelected');
  if(returnButton){returnButton.hidden=view!=='submitted';returnButton.disabled=batchReturnBusy||batchVerifyBusy||singleVerifyBusy||!currentInvoiceProjectId()||!chosen.length;returnButton.textContent=batchReturnBusy?'正在退回…':'退回待核对';returnButton.title=`只退回当前列表勾选的 ${chosen.length} 笔，隐藏的 ${checked.size-chosen.length} 笔不参与。`;}
  if(assign){
   const invalid=chosen.some(x=>!expensePersonAssignable(x));
   assign.hidden=view!=='review';assign.disabled=!currentInvoiceProjectId()||!chosen.length||invalid||!window.reimbursementPeopleUI?.bulkAssignExpenses;
   assign.textContent='修改归属'+(chosen.length?` · ${chosen.length} 笔`:'');
   assign.title=invalid?'当前勾选含不可修改归属的费用，整批不能修改；请取消这些费用的勾选。':`只修改当前列表勾选的 ${chosen.length} 笔，隐藏的 ${checked.size-chosen.length} 笔不参与。`;
  }
  window.updateReviewBatchTypeControls?.(chosen);
  if(view!=='inbox')$('listSummary').textContent=`${visible.length} 笔 · 已勾选 ${checked.size} 笔`+(checked.size>selected?`（其中 ${checked.size-selected} 笔不在当前列表）`:'');
}
function expensePersonAssignable(x){return !!x&&x.project_id===currentInvoiceProjectId()&&!!x.version&&x.stage==='draft'&&!x.verified&&!x.deleted&&!x.merged_into&&!x.legacy&&!x.history_imported&&!returnedToSynthesis(x)&&!['archived','deleted'].includes(x.synthesis_material_state);}
async function bulkAssignVisibleExpenses(){
 if(view!=='review'||!currentInvoiceProjectId())throw Error('请在项目的待核对列表选择费用，再批量修改归属。');
 const items=base().filter(x=>matches(x,view)&&checked.has(x.id));
 if(!items.length)throw Error('请勾选当前列表中要修改归属的费用；隐藏勾选不参与。');
 if(items.some(x=>!expensePersonAssignable(x)))throw Error('当前勾选含不可修改归属的费用，整批未修改；请取消这些费用的勾选。');
 if(!window.reimbursementPeopleUI?.bulkAssignExpenses)throw Error('人员归属入口尚未就绪，请刷新后重试。');
 await window.reimbursementPeopleUI.bulkAssignExpenses(items,{projectId:currentInvoiceProjectId(),onSaved:async()=>{
  for(const item of items)checked.delete(item.id);
  await load();
 }});
}
function materialSideCount(x,side){
 if(side==='trip'||side==='detail')return (x.attachments||[]).filter(a=>a.available&&a.role===side).length;
 const count=x.material_match?.[side+'_count'];if(Number.isSafeInteger(count)&&count>=0)return count;
 return (x.attachments||[]).filter(a=>a.available&&(side==='voucher'?['invoice','receipt'].includes(a.role):a.role==='payment')).length;
}
function taxiTripRequired(x){return x.category==='打车'&&!x.legacy&&x.material_basis!=='payment_only';}
function taxiProofsComplete(x){return taxiTripRequired(x)&&materialSideCount(x,'trip')>0&&(x.attachments||[]).some(a=>a.available&&a.role==='invoice');}
function courierWaybillRequired(x){return !x.legacy&&x.material_basis!=='payment_only'&&(x.courier_waybill_required===true||x.category==='顺丰'||x.report_section==='sf');}
function courierProofsComplete(x){return courierWaybillRequired(x)&&materialSideCount(x,'detail')>0&&(x.attachments||[]).some(a=>a.available&&a.role==='invoice');}
function materialAmountSides(x){return taxiTripRequired(x)?['voucher','trip']:courierWaybillRequired(x)?['voucher','detail']:['voucher','payment'];}
function knownMaterialAmount(value){return Number.isSafeInteger(value)&&value>=0;}
function materialAmountDiffers(x){
 const match=x.material_match;if(!match||x.amount_pending||!knownMaterialAmount(x.amount_cents))return false;
 return materialAmountSides(x).some(side=>materialSideCount(x,side)>0&&knownMaterialAmount(match[side+'_total_cents'])&&match[side+'_total_cents']!==x.amount_cents);
}
function materialAmountsAligned(x){
 const match=x.material_match,sides=materialAmountSides(x);return !!match&&!x.amount_pending&&knownMaterialAmount(x.amount_cents)&&
 (!taxiTripRequired(x)||taxiProofsComplete(x))&&
 (!courierWaybillRequired(x)||courierProofsComplete(x))&&
 sides.every(side=>materialSideCount(x,side)>0&&knownMaterialAmount(match[side+'_total_cents'])&&match[side+'_total_cents']===x.amount_cents)&&
 !['unknown','unconfirmed'].includes(match.alignment_status);
}
function badge(x){return x.deleted?'已删除':x.merged_into?'已合并':x.stage==='submitted'?'已生成报销包':x.stage==='reimbursed'?'已报销 · 历史记录':x.stage!=='draft'?state.stages[x.stage]:x.ready&&expenseProjectReimbursed(x)?'项目已报销 · 尚未生成报销包':x.ready&&x.missing_acknowledged?'待提交 · 已确认缺材料':!x.verified&&x.amount_pending?'金额待确认 · 待核对':!x.verified&&materialAmountDiffers(x)?'金额有差额 · 待核对':!x.complete?'待核对 · 仍缺材料':!x.verified?(materialAmountsAligned(x)?'金额已对齐 · 待核对':taxiProofsComplete(x)?'发票＋行程单齐全 · 待核对':courierProofsComplete(x)?'发票＋运单明细齐全 · 待核对':materialSideCount(x,'payment')===0&&receivedVoucher(x)?receivedVoucher(x)+' · 待核对':'材料齐全 · 待核对'):'待提交 · 已核对';}
function materialMatchNotice(x){
 const match=x.material_match;if(!match?.provisional||x.verified)return '';
 const voucherCount=materialSideCount(x,'voucher'),paymentCount=materialSideCount(x,'payment'),tripCount=materialSideCount(x,'trip'),detailCount=materialSideCount(x,'detail'),taxi=taxiTripRequired(x),courier=courierWaybillRequired(x),parts=[];
 if(paymentCount&&!taxi&&!courier)parts.push(`${knownMaterialAmount(match.payment_total_cents)?`付款合计 ¥ ${money(match.payment_total_cents)}`:'付款合计待确认'}，已暂配 ${paymentCount} 张`);
 if(voucherCount)parts.push(`已收 ${voucherCount} 份票据（${knownMaterialAmount(match.voucher_total_cents)?`票据合计 ¥ ${money(match.voucher_total_cents)}`:'票据合计待确认'}）`);
 if(taxi){
  parts.push(tripCount?`已收 ${tripCount} 份行程单`:'尚未选择行程单');
  parts.push(taxiProofsComplete(x)?'发票＋行程单已齐全，无需付款截图':'打车凭发票＋行程单报销，无需付款截图');
 }
 if(courier){
  parts.push(detailCount?`已收 ${detailCount} 份运单明细`:'尚未选择运单明细');
  parts.push(courierProofsComplete(x)?'发票＋运单明细已齐全，无需付款截图':'快递凭发票＋运单明细报销，无需付款截图');
  if(paymentCount)parts.push('付款截图保留为附件，不计入必需材料或默认打印页');
 }
 if(voucherCount&&!paymentCount&&!taxi&&!courier)parts.push(x.category==='高铁'?'高铁发票无需付款截图':'尚未选择付款截图');
 if(paymentCount&&!voucherCount)parts.push('尚未选择票据');
 const otherSupportCount=taxi||courier?Math.max(0,(match.support_count||0)-(taxi?tripCount:detailCount)):match.support_count;
 if(otherSupportCount)parts.push(`另有 ${otherSupportCount} 份补充材料，不计入金额`);
 const conclusion=materialAmountDiffers(x)?`当前报销金额 ¥ ${money(x.amount_cents)} 与原匹配材料合计有差额，请重新核对金额和材料。`:
 x.amount_pending?'报销金额待确认，请核对材料后填写。':materialAmountsAligned(x)?esc(match.reason||'金额相符，归属和内容仍需人工核对。'):'材料已登记，金额及对应关系仍需人工核对。';
 return `<div class="rule warn">${parts.length?parts.join('；')+'。':''}${conclusion}</div>`;
}
function receivedVoucher(x){
 const roles=new Set((x.attachments||[]).filter(a=>a.available).map(a=>a.role));
 return roles.has('invoice')?'发票已收到':roles.has('receipt')?'收据已收到':'';
}
function invoiceProgress(x){return receivedVoucher(x)||({'requested':'已申请，等待开票','not_requested':'尚未申请发票','unavailable':'无法开票'})[x.invoice_state];}
function expenseMoney(x){return x.amount_pending?'金额待确认':`¥ ${money(x.amount_cents)}`;}
function expenseRowActions(x){
 if(x.deleted)return `<div class="row-actions"><button type="button" data-restore-item="${esc(x.id)}" aria-label="恢复 ${esc(x.title)}">恢复</button></div>`;
 if(retiredExpense(x))return '<span class="muted">原费用记录 · 只读</span>';
 const canDelete=['draft','cancelled'].includes(x.stage)&&!x.merged_into;
 const canMove=x.stage==='draft'&&!x.merged_into;
 const reason=x.merged_into?'已合并的费用不能独立删除或迁移':x.stage==='cancelled'?'已取消的费用请先退回，再迁移':'历史费用请先退回待核对';
 return `<div class="row-actions"><button type="button" data-move-item="${esc(x.id)}" aria-label="迁移 ${esc(x.title)}" ${canMove?'':`disabled title="${esc(reason)}"`}>迁移</button><button type="button" class="row-delete" data-delete-item="${esc(x.id)}" aria-label="删除 ${esc(x.title)}" ${canDelete?'':`disabled title="${esc(reason)}"`}>删除</button></div>`;
}
async function expenseActionFailed(dialog,error){
 let extra='';try{await load();}catch(refreshError){extra='；刷新失败：'+refreshError.message;}
 if(dialog?.isConnected)dialog.querySelector('[data-confirm]').disabled=true;
 notify(error.message+extra+(dialog?' 请关闭窗口后重新选择。':''),true);
}
function expenseActionDialog(id,html){
 $(id)?.remove();const dialog=document.createElement('dialog');dialog.id=id;dialog.className='expense-action-dialog';dialog.innerHTML=html;
 const heading=dialog.querySelector('h2'),header=document.createElement('div');header.className='dialoghead';heading.replaceWith(header);header.append(heading);const dismissButton=document.createElement('button');dismissButton.type='button';dismissButton.className='dialog-close';dismissButton.dataset.dismiss='';dismissButton.setAttribute('aria-label','关闭');dismissButton.textContent='×';header.append(dismissButton);
 document.body.append(dialog);dialog.querySelectorAll('[data-close],[data-dismiss]').forEach(button=>button.onclick=()=>dialog.remove());
 dialog.oncancel=e=>{e.preventDefault();dialog.remove();};dialog.showModal();return dialog;
}
function openDeleteExpense(x){
 if(!x||x.deleted||!['draft','cancelled'].includes(x.stage)||x.merged_into)return;
 const dialog=expenseActionDialog('deleteExpenseDialog',`<h2>删除费用</h2><p class="expense-action-name">${esc(x.title)} · ${expenseMoney(x)}</p><p>删除后从正常列表移除；原材料保留，可在“已删除”中恢复。</p><div class="actions"><button type="button" data-close>取消</button><button type="button" data-confirm>确认删除</button></div>`);
 const button=dialog.querySelector('[data-confirm]');let busy=false;
 button.onclick=async()=>{
  if(busy)return;busy=true;button.disabled=true;
  try{await api('expenses/delete',{id:x.id,expected_version:x.version});dialog.remove();checked.delete(x.id);if(active===x.id)active=null;await load();notify('已删除费用，可从“已删除”恢复。');}
  catch(error){await expenseActionFailed(dialog,error);}
  finally{busy=false;}
 };
}
function openMoveExpense(x){
 if(!x||x.deleted||x.stage!=='draft'||x.merged_into)return;
 const projects=(state.projects||[]).filter(p=>p.id!==x.project_id&&(x.project_id||p.name!==x.project));
 const dialog=expenseActionDialog('moveExpenseDialog',`<h2>迁移费用</h2><p class="expense-action-name">${esc(x.title)} · ${expenseMoney(x)}</p><p>费用和关联材料一起转入目标项目，迁移后进入待核对。</p><label>目标项目<select name="project_id" required><option value="">选择目标项目</option>${projects.map(p=>`<option value="${esc(p.id)}">${esc(p.name)}</option>`).join('')}</select></label>${projects.length?'':'<p>请先在全部项目台账中新建目标项目。</p>'}<div class="actions"><button type="button" data-close>取消</button><button type="button" data-confirm disabled>确认迁移</button></div>`);
 const select=dialog.querySelector('select'),button=dialog.querySelector('[data-confirm]');let busy=false;
 select.onchange=()=>{button.disabled=busy||!select.value;};
 button.onclick=async()=>{
  if(busy||!select.value)return;busy=true;button.disabled=true;select.disabled=true;
  try{const target=projects.find(p=>p.id===select.value);await api('projects/move',{ids:[x.id],project_id:select.value,versions:{[x.id]:x.version}});dialog.remove();checked.delete(x.id);if(active===x.id)active=null;await load();notify('已迁移到'+target.name+'，请在目标项目核对。');}
  catch(error){await expenseActionFailed(dialog,error);}
  finally{busy=false;}
 };
}
async function restoreExpense(x,button){
 if(!x?.deleted)return;button.disabled=true;
 try{const restored=await api('expenses/restore',{id:x.id,expected_version:x.version});view=expenseViewFor(restored);active=returnedToSynthesis(restored)?null:x.id;if(returnedToSynthesis(restored))pendingMaterialDeskOpen=true;await load();notify('已恢复费用，材料仍保留。');}
 catch(error){await expenseActionFailed(null,error);}
 finally{if(button.isConnected)button.disabled=false;}
}
function invoiceProgressField(x){
 const received=receivedVoucher(x);
 if(received)return `<label>开票进度<input readonly value="${esc(received)}"><input type="hidden" name="invoice_state" value="${esc(x.invoice_state||'not_requested')}"></label>`;
 return `<label>开票进度<select name="invoice_state">${[['not_requested','尚未申请'],['requested','已申请，等待开票'],['unavailable','无法开票']].map(([v,t])=>`<option value="${v}" ${v===x.invoice_state?'selected':''}>${t}</option>`).join('')}</select></label>`;
}
function expenseFilterLabels(){
 const labels=[],query=$('search').value.trim();
 for(const [id,label] of [['personFilter','人员'],['categoryFilter','类别'],['issueFilter','问题']]){
  const selected=expenseFilterValues(id);if(selected.length)labels.push(label+'：'+[...$(id).options].filter(option=>selected.includes(option.value)).map(option=>option.textContent).join('、'));
 }
 if(query)labels.push('搜索：'+query);
 return labels;
}
function emptyExpenseView(){
 const filters=expenseFilterLabels();
 if(filters.length){
  const registered=state.items.filter(x=>!retiredExpense(x)&&!returnedToSynthesis(x));
  return `<div class="empty"><h2>当前筛选下暂无费用</h2><p>${esc(filters.join(' · '))}</p><p>${projectScope?'本项目':'台账'}共有 ${registered.length} 笔费用，合计 ¥ ${money(registered.reduce((total,x)=>total+x.amount_cents,0))}。请调整上方筛选或切换阶段查看。</p></div>`;
 }
 if(view==='submitted'){
  const pending=state.items.filter(x=>matches(x,'review'));
  return `<div class="empty"><h2>暂无已核对费用</h2>${pending.length?`<p>还有 ${pending.length} 笔费用，合计 ¥ ${money(pending.reduce((total,x)=>total+x.amount_cents,0))}，在“待核对”中。</p>`:''}<p>完成核对后，可在这里查看最终打印页并制作报销包。</p></div>`;
 }
 if(view==='review')return '<div class="empty"><h2>暂无待核对费用</h2><p>确认合成后进入这里核对；已经核对的费用请到“已核对”查看。</p></div>';
 return '<div class="empty"><h2>当前没有事项</h2><p>在材料合成台选择凭证，创建费用后可继续补材料。</p></div>';
}
function render(){view=normalizeExpenseView(view);const items=base(),filtered=expenseFilterLabels().length>0;$('inboxCount').textContent=state.inbox.length||'';$('metrics').innerHTML=[['review','待核对',''],['submitted','待提交','good']].map(([v,label,cls])=>{
 label+=filtered?' · 筛选结果':'';
 const a=items.filter(v==='submitted'?pendingSubmission:x=>matches(x,v));
 const historical=v==='submitted'?items.filter(x=>reportEligible(x)&&expenseProjectReimbursed(x)).length:0;
 return `<button class="metric ${cls}" data-view="${v}"><small>${label}</small><b>¥ ${money(a.reduce((t,x)=>t+x.amount_cents,0))}</b><em>${a.length} 笔事项${v==='submitted'?' · 查看最终打印页':''}</em>${historical?`<em>已报销项目中另有 ${historical} 笔已核对未生成报销包</em>`:''}</button>`;
}).join('');document.querySelectorAll('[data-view]').forEach(b=>b.classList.toggle('active',b.dataset.view===view));$('tabs').hidden=view==='inbox';$('exportSelected').hidden=view==='inbox';
if(view==='inbox'){$('listSummary').textContent=`${state.inbox.length} 份需处理邮件附件（打车材料已自动整理）`;$('list').innerHTML=state.inbox.length?state.inbox.map(f=>`<div class="mailrow"><a href="/inbox/${f.id}" target="_blank" rel="noopener">${esc(f.name)}</a><div class="actions"><select id="target-${f.id}" aria-label="关联事项"><option value="">选择所属事项</option>${state.items.filter(x=>!x.deleted&&x.stage==='draft').map(x=>`<option value="${x.id}">${esc(x.title)}</option>`).join('')}</select><select id="role-${f.id}" aria-label="材料用途">${Object.entries(state.roles).filter(([k])=>k!=="package").map(([k,v])=>`<option value="${k}" ${k==="other"?"selected":""}>${esc(v)}</option>`).join("")}</select><button data-assign="${f.id}">手动归档</button></div><small class="muted">${esc(f.reason||"非打车材料或无法确定归属，请核对后归档。")}</small></div>`).join(''):'<div class="empty">没有需处理的邮件附件。已配对的打车费用请到“事项台账”查看；补收邮件后会自动整理。</div>';}
else{const visible=items.filter(x=>matches(x,view));if(view==='submitted')visible.sort((a,b)=>Number(pendingSubmission(b))-Number(pendingSubmission(a)));$('listSummary').textContent=`${visible.length} 笔 · 已勾选 ${checked.size} 笔`;$('list').innerHTML=visible.length?visible.map(x=>`<div class="row ${active===x.id?'selected':''}" data-item="${x.id}">${x.deleted?'<span class="row-select-spacer" aria-hidden="true"></span>':`<input type="checkbox" aria-label="选择 ${esc(x.title)}" data-check="${x.id}" ${checked.has(x.id)?'checked':''}>`}<div><div class="title">${esc(x.title)}</div>${window.reimbursementPeopleUI?.markup(x)||''}<div class="sub">${esc(x.project)} · ${esc(x.category)}<br>${esc(x.expense_date)} · ${esc(x.merchant||'未填商家')}</div>${!x.complete?`<div class="missing">缺：${esc(x.missing.join('、'))}${x.overdue?' · 已到跟进日':''}</div>`:''}${x.alternative?'<div class="missing">无发票替代：扣费记录</div>':''}</div><div class="money">${expenseMoney(x)}<br><span class="badge ${x.ready?'good':x.stage!=='draft'?'neutral':''}">${badge(x)}</span>${expenseRowActions(x)}</div></div>`).join(''):emptyExpenseView();}
renderSelection();renderDetail();
for(const row of document.querySelectorAll('[data-item]')){
 const item=state.items.find(x=>x.id===row.dataset.item);
 if(retiredExpense(item)){const input=row.querySelector('[data-check]');if(input){const spacer=document.createElement('span');spacer.className='row-select-spacer';spacer.setAttribute('aria-hidden','true');input.replaceWith(spacer);}}
 if(!item.deleted&&item.stage==='draft'&&!item.complete){const note=document.createElement('div');note.className='sub';note.textContent=invoiceProgress(item)+` · 登记 ${item.waiting_days} 天`+(item.followup_date?' · 跟进 '+item.followup_date:'');row.querySelector('.title').parentElement.append(note);}
}}
function formFields(x={}){const field=(label,name,type='text',wide=false)=>`<label class="${wide?'wide':''}">${label}<input name="${name}" type="${type}" value="${esc(x[name]??'')}" ${name==='title'?'required maxlength="200"':''} ${name==='amount'?'required min="0" step="0.01"':''}></label>`;return `<div class="formgrid">${field('事项名称','title','text',true)}${field('报表显示简称','display_name')}${field('展示合并组（仅同项目汇总）','report_group')}<label>报表栏目<select name="report_section">${[['','按类别默认'],['materials','材料采购'],['dining','现场饮用水、夜宵'],['rail','高铁'],['taxi','打车'],['hotel','住宿'],['takeout','员工购餐'],['sf','顺丰同城'],['other','其他']].map(([v,t])=>`<option value="${v}" ${v===(x.report_section||'')?'selected':''}>${t}</option>`).join('')}</select></label>${projectScope?`<input type="hidden" name="project_id" value="${esc(projectScope)}"><label>项目<input name="project" readonly value="${esc(projectContext()?.name||'')}"></label>`:field('项目','project')}${field('商家／酒店','merchant')}<label>费用类别<select name="category">${state.categories.map(c=>`<option ${c===x.category?'selected':''}>${esc(c)}</option>`).join('')}</select></label>${field('支出金额（元）','amount','number')}${field('消费／预订日期','expense_date','date')}${field('订单号（可后补）','order_number')}<label>材料要求<select name="material_basis">${[['standard','按费用类别准备'],['invoice_payment','发票／收据＋付款截图'],['payment_only','仅支付凭证（已确认适用）']].map(([v,t])=>`<option value="${v}" ${v===(x.material_basis||'standard')?'selected':''}>${t}</option>`).join('')}</select></label>${invoiceProgressField(x)}${field('下次跟进日期','followup_date','date')}<label class="wide">用途／备注<textarea name="note">${esc(x.note||'')}</textarea></label></div>`;}
function progressActions(x){
  if(x.merged_into)return '<span class="muted">已合并到组合费用，请在组合费用中核对或撤回。</span>';
  const edit=reviewFieldsEditable(x)?`<button type="button" id="editReviewFields" ${typeof window.openReviewFieldsEditor==='function'?'':'disabled'}>修改报销信息</button>`:'';
  if(x.stage==='draft'&&x.ready)return view==='submitted'?'<button id="makeExpenseReport" class="primary">制作本笔报销包</button>':'<span class="muted">已核对，请到“已核对”制作报销包。</span>';
  const downgrade=x.stage==='draft'&&!x.verified&&x.material_match?.group_id?'<button id="undoExpenseMatch">撤回材料配对</button>':'';
  if(x.stage==='draft'&&!verificationBlockReason(x))return edit+`<button id="verifyNext" class="primary" ${singleVerifyBusy?'disabled':''}>${singleVerifyBusy?'正在核对…':'核对并下一笔'}</button><button data-action="verify" ${singleVerifyBusy?'disabled':''}>核对完成，进入已核对</button>`+downgrade;
  if(['submitted','reimbursed'].includes(x.stage))return '<span class="muted">历史费用保留，可查看最终打印页。</span><button data-action="draft">退回待核对</button>';
  if(x.stage==='cancelled')return '<button data-action="draft">退回待核对</button>';
  return edit+`<span class="muted">${esc(verificationBlockReason(x)||'请先确认材料要求，再进行核对。')}</span>`+downgrade+'<button data-action="cancelled">取消事项</button>';
}
function verificationBlockReason(x){
 if(x.amount_pending||(x.missing||[]).includes('金额待确认'))return '请先填写并确认报销金额，再进行核对。';
 if((x.missing||[]).some(text=>text.startsWith('确认此类别的材料要求')))return '请先选择具体费用类别，确认材料要求后再核对。';
 if(!x.complete&&x.missing_acknowledgement_allowed===false)return '请先确认报销金额和费用类别，再进行核对。';
 return '';
}
function missingVerificationText(items){
 return items.map(x=>`${x.title} · ${expenseMoney(x)}\n缺：${(x.missing||[]).join('、')||'材料要求待确认'}`).join('\n\n');
}
function verificationRequest(item){
 return {id:item.id,action:'verify',expected_version:item.version,...(!item.complete?{acknowledge_missing:true,expected_missing:[...item.missing]}:{})};
}
function reviewScopeKey(){return JSON.stringify(['categoryFilter','personFilter','issueFilter'].map(expenseFilterValues).concat($('search').value));}
function revealExpenseRow(id,scrollTop){
 const list=$('list');if(scrollTop!==undefined)list.scrollTop=scrollTop;
 const row=[...list.querySelectorAll('[data-item]')].find(node=>node.dataset.item===id);if(!row)return;
 const bounds=list.getBoundingClientRect(),rect=row.getBoundingClientRect();
 if(list.scrollHeight>list.clientHeight){if(rect.top<bounds.top)list.scrollTop+=rect.top-bounds.top;else if(rect.bottom>bounds.bottom)list.scrollTop+=rect.bottom-bounds.bottom;}
}
async function verifyExpense(id,{next=false}={}){
 if(singleVerifyBusy||batchVerifyBusy||batchReturnBusy)return;
 const item=state.items.find(x=>x.id===id);
  if(!item||!pendingReview(item)||item.ready)throw Error('请刷新并重新选择尚未核对的费用。');
  if(verificationBlockReason(item))throw Error(verificationBlockReason(item));
  const request=verificationRequest(item);
 const queue=base().filter(pendingReview).map(x=>x.id),index=queue.indexOf(id);
 const order=[...queue.slice(index+1),...queue.slice(0,Math.max(index,0))];
 const scope=reviewScopeKey(),originalView=view,listTop=$('list').scrollTop;
 const current=()=>active===id&&view===originalView&&reviewScopeKey()===scope;
 let committed=false,advanced=false,nextId=null;
 singleVerifyBusy=true;renderSelection();
 for(const button of $('detail').querySelectorAll('#verifyNext,[data-action="verify"]')){button.disabled=true;if(button.id==='verifyNext')button.textContent='正在核对…';}
 try{
   if(!item.complete){
    if(!await confirmVerification(`${missingVerificationText([item])}\n\n这笔费用仍缺材料，是否仍然核对完成？\n确认后进入“已核对”，缺项会继续显示，打印只使用已经收到的材料。`,{title:'材料不齐，仍然核对？',confirmText:'仍然核对完成'})){notify('已取消核对，费用保持原状。');return;}
    const currentItem=state.items.find(x=>x.id===id);
    if(!currentItem||currentItem.version!==request.expected_version||JSON.stringify(currentItem.missing)!==JSON.stringify(request.expected_missing)||!pendingReview(currentItem)||verificationBlockReason(currentItem))throw Error('费用或缺项已变化，请刷新后重新确认。');
   }
   const updated=await api('transition',request);committed=true;checked.delete(id);
  // Keep the acknowledged state even if the following read fails; never repeat the write to retry a refresh.
  state={...state,items:state.items.map(x=>x.id===id?{...x,...updated,verified:true,ready:true}:x)};
  await load({beforeRender:()=>{
   if(!current())return;
   if(next){
    const remaining=base().filter(pendingReview),remainingIds=new Set(remaining.map(x=>x.id));
    nextId=order.find(candidate=>remainingIds.has(candidate))||remaining[0]?.id||null;
    active=nextId;view='review';advanced=true;
   }else{view='submitted';active=null;}
  }});
  if(advanced){
   revealExpenseRow(nextId,listTop);$('detail').scrollTop=0;
   if(!nextId)$('detail').innerHTML='<div class="empty" role="status"><h2>当前筛选范围已核对完</h2><p>可切换人员、类别或问题筛选，继续处理其他费用；已核对费用在“已核对”中。</p></div>';
  }
  notify(next&&advanced?(nextId?'本笔核对完成，已打开当前筛选下的下一笔。':'本笔核对完成，当前筛选范围已全部核对。'):'本笔核对完成，可在“已核对”查看。');
 }catch(error){
  if(committed){render();throw Error('本笔已核对成功；刷新失败，请刷新页面继续，无需再次核对。'+error.message);}
  throw error;
 }finally{
  singleVerifyBusy=false;renderSelection();
  for(const button of $('detail').querySelectorAll('#verifyNext,[data-action="verify"]')){button.disabled=false;if(button.id==='verifyNext')button.textContent='核对并下一笔';}
  if(advanced&&nextId)$('verifyNext')?.focus({preventScroll:true});
 }
}
window.openReimbursementExpense=async(id,personId,{current=()=>true}={})=>{
 const projectId=currentInvoiceProjectId(),response=await fetch('/api/state');if(!response.ok)throw Error('无法读取费用，请重试。');
 const fresh=scopeData(await response.json());if(!current())return false;if(projectId!==currentInvoiceProjectId())throw Error('项目已变化，请重新打开人员明细。');
 state=fresh;expensePreviewRequests.clear();
 const item=state.items.find(x=>x.id===id);
 if(!item||item.project_id!==currentInvoiceProjectId()||(item.reimbursement_person_id||'')!==(personId||'')||retiredExpense(item)||returnedToSynthesis(item)||['archived','deleted'].includes(item.synthesis_material_state))throw Error('费用归属或状态已变化，请刷新人员明细后重试。');
 setExpenseFilterValues('categoryFilter',[]);$('search').value='';setExpenseFilterValues('issueFilter',[]);
 renderPersonFilter();const value=personId||'__unassigned__';
 if(![...$('personFilter').options].some(option=>option.value===value))throw Error('该人员已不在当前名单，请刷新人员明细。');
 setExpenseFilterValues('personFilter',[value]);active=id;view=expenseViewFor(item);render();revealExpenseRow(id,0);$('detail').scrollTop=0;return true;
};
const verifyDialog=document.createElement('dialog');
verifyDialog.id='verifyDialog';
verifyDialog.innerHTML='<div class="dialoghead"><h2 id="verifyTitle">批量核对确认</h2><button type="button" class="dialog-close" id="dismissVerify" aria-label="关闭">×</button></div><p id="verifyScope" style="white-space:pre-wrap"></p><div class="actions"><button id="cancelVerify" type="button">取消</button><button id="confirmVerify" type="button" class="primary">确认核对完成</button></div>';
verifyDialog.setAttribute('aria-labelledby','verifyTitle');
document.body.append(verifyDialog);
function confirmVerification(message,{title='批量核对确认',confirmText='确认核对完成'}={}){return new Promise(resolve=>{
  $('verifyTitle').textContent=title;$('confirmVerify').textContent=confirmText;
  $('verifyScope').textContent=message;
  let finished=false;
  const finish=result=>{if(finished)return;finished=true;verifyDialog.close();resolve(result);};
  $('cancelVerify').onclick=()=>finish(false);
  $('dismissVerify').onclick=()=>finish(false);
  $('confirmVerify').onclick=()=>finish(true);
  verifyDialog.oncancel=e=>{e.preventDefault();finish(false);};
  verifyDialog.showModal();
});}
const expensePreviewRequests=new Map();
let renderedExpenseDetail=null;
async function renderExpensePrintPreview(x){
  const pages=$('expensePrintPages'),status=$('expensePrintStatus'),link=$('expensePrintPdf');
  const current=()=>pages.isConnected&&active===x.id&&state.items.find(item=>item.id===x.id)?.version===x.version;
  const key=x.id+':'+x.version;
  try{
    let request=expensePreviewRequests.get(key);
    if(!request){request=api('expense-print-preview',{id:x.id,expected_version:x.version});expensePreviewRequests.set(key,request);}
    const result=await request;
    if(!current())return;
    if(result.id!==x.id||result.version!==x.version)throw Error('费用已变化，请刷新后查看打印页。');
    pages.replaceChildren();
    if(!result.preview_pages?.length){
      status.textContent='尚无可预览的原件，请上传已有材料。';
      const empty=document.createElement('p');empty.className='print-empty';empty.textContent='缺：'+(result.missing||x.missing).join('、');pages.append(empty);return;
    }
    status.textContent=`${result.page_count} 页 A4 · ${result.layout}${result.partial?'。还缺：'+(result.missing||x.missing).join('、'):'。打印选择 A4、每张纸 1 页。'}`;
    link.href='/export/'+encodeURIComponent(result.pdf);link.hidden=false;
    for(const [index,name] of result.preview_pages.entries()){
      const figure=document.createElement('figure'),image=document.createElement('img'),caption=document.createElement('figcaption');
      image.src='/export/'+encodeURIComponent(name);image.alt=`${x.title} · ${result.partial?'已收材料':'最终打印'}第 ${index+1} 页`;image.loading=index?'lazy':'eager';
      image.decoding='async';
      caption.textContent=`A4 · 第 ${index+1} / ${result.page_count} 页`;
      figure.append(image,caption);pages.append(figure);
    }
  }catch(error){
    expensePreviewRequests.delete(key);
    if(!current())return;
    status.textContent='无法生成最终打印页：'+error.message;pages.replaceChildren();
    const retry=document.createElement('button');retry.type='button';retry.id='retryExpensePrint';retry.textContent='重试打印预览';
    retry.onclick=()=>{status.textContent='正在生成实际打印页…';retry.remove();renderExpensePrintPreview(x);};pages.append(retry);
  }
}
function renderDetail(){
  const x=state.items.find(x=>x.id===active);
  if(!x||view==='inbox'||!base().some(item=>item.id===x.id&&matches(item,view))){$('detail').innerHTML='<div class="empty">选择一笔费用，查看最终 A4 打印页。</div>';return;}
  if(x.deleted){$('detail').innerHTML=`<div class="empty"><h2>${esc(x.title)}</h2><p>已删除 · 原材料保留，可使用列表右侧的恢复按钮。</p></div>`;return;}
  if(retiredExpense(x)){$('detail').innerHTML=`<h2>${esc(x.title)}</h2><span class="badge neutral">${badge(x)}</span><p class="muted">原费用记录只读保留，不参与待核对或待提交金额。原件仍可查看。</p><section>${x.attachments.map(a=>`<div class="attachment">${a.available?`<a href="/file/${esc(a.id)}" target="_blank" rel="noopener">${esc(a.name)}</a>`:`<span>文件缺失：${esc(a.name)}</span>`}</div>`).join('')||'<p class="muted">暂无凭证。</p>'}</section>`;return;}
  const personMarkup=window.reimbursementPeopleUI?.markup(x)||'';
  const detailKey=JSON.stringify([x,view,personMarkup,state.roles,state.stages,state.mail_search_ready,typeof window.openReviewFieldsEditor]);
  if(renderedExpenseDetail?.state===state&&renderedExpenseDetail.key===detailKey&&renderedExpenseDetail.pages.isConnected)return;
  $('detail').innerHTML=`<div class="print-heading"><div><div class="eyebrow">${x.complete?'最终打印预览':'已收材料预览'}</div><h2>${esc(x.title)}</h2></div><strong>${expenseMoney(x)}</strong></div>
  <span class="badge ${x.ready?'good':''}">${badge(x)}</span>
  ${window.reimbursementPeopleUI ? `<div class="reimbursement-detail-person">${personMarkup}${reviewFieldsEditable(x)?`<button type="button" data-edit-expense-person="${esc(x.id)}">修改费用归属人</button>`:''}</div>`:''}
  ${!x.complete?`<div class="rule warn">${x.missing_acknowledged?'已确认仍缺：':'还缺：'}${esc(x.missing.join('、'))}${x.missing_acknowledged?'。已人工确认核对通过，打印仅使用已收到的材料。':''}</div>`:''}
  ${materialMatchNotice(x)}
  ${window.invoiceCheckMarkup(x.material_checks)}
  ${expenseGoodsMarkup(x)}
  <div class="actions print-actions">${progressActions(x)}<a id="expensePrintPdf" target="_blank" rel="noopener" hidden>放大／下载打印 PDF</a></div>
  <p id="expensePrintStatus" class="muted" role="status">正在生成实际打印页…</p>
  <section id="expensePrintPages" class="print-pages" aria-label="费用最终打印页" aria-live="polite"></section>
  <details class="print-materials"><summary>材料夹 · ${x.attachments.length} 份${!x.complete?' · 补材料':''}</summary>
  ${x.stage==='draft'&&state.mail_search_ready?'<button type="button" id="findInvoice">查找对应发票</button>':''}
  <div>${x.attachments.map(a=>`<div class="attachment">${a.available?`<a href="/file/${a.id}" target="_blank" rel="noopener">↗ ${esc(a.name)}</a>`:`<span>文件缺失：${esc(a.name)}</span>`}${a.role==='package'?'<span class="muted">历史打印包</span>':`<select aria-label="${esc(a.name)} 的用途" data-role="${a.id}">${Object.entries(state.roles).filter(([r])=>r!=='package').map(([r,t])=>`<option value="${r}" ${a.role===r?'selected':''}>${t}</option>`).join('')}</select>`}</div>`).join('')||'<p class="muted">尚无材料。</p>'}</div>
  <div class="actions"><select id="uploadRole" aria-label="上传材料用途">${Object.entries(state.roles).filter(([r])=>r!=='package').map(([r,t])=>`<option value="${r}">${t}</option>`).join('')}</select><button type="button" id="uploadButton">＋ 上传材料</button><input type="file" id="uploadFiles" accept=".pdf,.png,.jpg,.jpeg,.webp" multiple hidden></div>
  <p class="muted">可多选，每份最多20MB。补材料后重新核对。</p></details>`;
  const make=$('makeExpenseReport');if(make)make.onclick=()=>{checked.clear();checked.add(x.id);renderSelection();$('exportSelected').click();};
  const edit=$('editReviewFields');if(edit)edit.onclick=()=>act(()=>window.openReviewFieldsEditor(x.id));
  const next=$('verifyNext');if(next)next.onclick=()=>act(()=>verifyExpense(x.id,{next:true}));
  const undo=$('undoExpenseMatch');if(undo)undo.onclick=()=>act(async()=>{undo.disabled=true;try{await api('material-matching/undo',{group_id:x.material_match.group_id,expected_version:x.version});checked.delete(x.id);active=null;view='review';pendingMaterialDeskOpen=true;await load();notify('已退回合成台，原费用与原件保留；请重新选择材料后确认合成。');}finally{if(undo.isConnected)undo.disabled=false;}});
  $('uploadButton').onclick=()=>$('uploadFiles').click();
  $('uploadFiles').onchange=e=>act(async()=>{const target=x.id,role=$('uploadRole').value;for(const f of e.target.files){if(f.size>20*1024*1024)throw Error('附件超过20MB：'+f.name);const data=await new Promise((resolve,reject)=>{const r=new FileReader();r.onload=()=>resolve(r.result.split(',')[1]);r.onerror=reject;r.readAsDataURL(f);});await api('upload',{id:target,name:f.name,data,role});}await load();notify('材料已保存，请核对用途。');});
  renderedExpenseDetail={state,key:detailKey,pages:$('expensePrintPages')};
  renderExpensePrintPreview(x);
}
function expenseGoodsMarkup(x){
 const goods=expenseGoods(x);if(!goods.length)return '';
 return `<details class="expense-goods"><summary>商品信息</summary><p class="muted">以下为材料中的商品识别记录；数量、规格及未识别内容请对照完整原件。</p>${goods.map(g=>{
  const name=typeof g.name==='string'?g.name:'',raw=typeof g.raw_name==='string'?g.raw_name:'',quote=typeof g.quote==='string'?g.quote:'';
  const original=typeof g.material_id==='string'&&g.material_id?`<a href="/intake-preview/${esc(encodeURIComponent(g.material_id))}" target="_blank" rel="noopener">查看完整原件</a>`:'';
  return `<div class="expense-good"><dl><dt>商品名称</dt><dd>${esc(name)||'未列商品名称'}</dd>${raw?`<dt>原始名称</dt><dd>${esc(raw)}</dd>`:''}<dt>识别原文</dt><dd>${quote?esc(quote):'未记录识别原文，请查看完整原件。'}</dd></dl>${original}</div>`;
 }).join('')}</details>`;
}
function setExpensePreview(id){
 active=id;
 for(const row of $('list').querySelectorAll('[data-item]'))row.classList.toggle('selected',row.dataset.item===active);
 renderDetail();
}
document.addEventListener('keydown',event=>{
 if(view!=='review'||event.defaultPrevented||event.repeat||event.isComposing||event.ctrlKey||event.metaKey||event.altKey||singleVerifyBusy||batchVerifyBusy||batchReturnBusy||document.querySelector('dialog[open]'))return;
 if(event.target instanceof Element&&event.target.closest('input,select,textarea,[contenteditable]:not([contenteditable="false"]),[role="textbox"],[role="combobox"],[role="menu"]'))return;
 const key=event.key.toLowerCase();
 if(['j','k','arrowdown','arrowup'].includes(key)){
  const queue=base().filter(pendingReview);if(!queue.length)return;
  event.preventDefault();const direction=['j','arrowdown'].includes(key)?1:-1,index=queue.findIndex(x=>x.id===active);
  const target=queue[index<0?(direction>0?0:queue.length-1):Math.max(0,Math.min(queue.length-1,index+direction))];
  setExpensePreview(target.id);revealExpenseRow(target.id);return;
 }
 if(key==='enter'||key==='e'){
  const button=$(key==='enter'?'verifyNext':'editReviewFields');
  if(button&&!button.disabled){event.preventDefault();button.click();}return;
 }
 if(key==='?'){
  event.preventDefault();const dialog=document.createElement('dialog');dialog.id='reviewShortcuts';
  dialog.innerHTML='<div class="dialoghead"><h2>核对快捷键</h2><button type="button" class="dialog-close" aria-label="关闭快捷键说明">×</button></div><p>J / ↓：下一笔；K / ↑：上一笔</p><p>Enter：核对并下一笔；E：修改报销信息</p><p>?：显示本说明。输入和对话框内不响应快捷键。</p>';
  dialog.querySelector('button').onclick=()=>dialog.remove();dialog.addEventListener('close',()=>dialog.remove());document.body.append(dialog);dialog.showModal();
 }
});
window.showCreatedMaterialExpenses=items=>{
 const first=Array.isArray(items)?items.find(item=>item&&typeof item.id==='string'&&item.id):null;
 if(!first)return false;
 checked.clear();view='review';active=first.id;
 const current=state.items.find(item=>item.id===first.id);
 if(current&&pendingReview(current)&&!base().some(item=>item.id===first.id)){
  for(const id of ['categoryFilter','personFilter','issueFilter'])setExpenseFilterValues(id,[]);
  $('search').value='';
  notify('费用已进入待核对；已清空筛选和搜索，显示刚创建的费用。');
 }
 render();revealExpenseRow(first.id,0);$('detail').scrollTop=0;
 return true;
};
function expensePreviewClick(event){
 if(view==='inbox'||document.querySelector('dialog[open]'))return;
 const target=event.target;
 if(!(target instanceof Element)||target.closest('button,a,input,select,textarea,label,summary,details,form,[contenteditable]:not([contenteditable="false"]),[role="button"],[role="combobox"],[role="option"],[role="menu"],[role="menuitem"],[role="listbox"]'))return;
 const row=target.closest('#list [data-item]');
 if(row){setExpensePreview(active===row.dataset.item?null:row.dataset.item);return;}
 // Only container whitespace dismisses the preview. Proof images, text and
 // attachment content keep it open; checkbox selection is a separate action.
 if(active&&target.matches('main,.workspace,.listpanel,#list,#detail,.listhead,.toolbar,.workbench-filters,.workbench-actions,.selection-tools,.tabs,#detail>.empty,#list>.empty'))setExpensePreview(null);
}
document.addEventListener('click',e=>{const b=e.target.closest('button');if(b?.dataset.projectStatus){projectReimbursementView=b.dataset.projectStatus;renderProjectNavigation();return;}if(b?.dataset.deleteItem){e.stopPropagation();openDeleteExpense(state.items.find(x=>x.id===b.dataset.deleteItem));return;}if(b?.dataset.moveItem){e.stopPropagation();openMoveExpense(state.items.find(x=>x.id===b.dataset.moveItem));return;}if(b?.dataset.restoreItem){e.stopPropagation();act(()=>restoreExpense(state.items.find(x=>x.id===b.dataset.restoreItem),b));return;}if(b?.dataset.view){view=b.dataset.view;render();}if(e.target.dataset.check)return;expensePreviewClick(e);if(b?.dataset.action)act(async()=>{const action=b.dataset.action;if(action==='verify'){await verifyExpense(active);return;}if(action==='draft'&&submissionReady(state.items.find(x=>x.id===active))){await returnReviewedExpenses([active]);return;}await api('transition',{id:active,action,expected_version:state.items.find(x=>x.id===active)?.version});await load();notify('状态已更新。');});if(b?.dataset.assign)act(async()=>{const id=$('target-'+b.dataset.assign).value;if(!id)throw Error('请先选择事项');await api('assign',{id,file_id:b.dataset.assign,role:$('role-'+b.dataset.assign).value});active=id;await load();notify('材料已归档，请核对金额和用途。');});});
document.addEventListener('change',e=>{if(e.target.dataset.check){if(e.target.checked)checked.add(e.target.dataset.check);else checked.delete(e.target.dataset.check);renderSelection();}if(e.target.dataset.role)act(async()=>{await api('role',{attachment_id:e.target.dataset.role,role:e.target.value});await load();});});
$('categoryFilter').onchange=render;$('personFilter').onchange=render;$('search').oninput=render;
$('issueFilter').onchange=()=>{if(!['review','submitted'].includes(view))view='review';render();};
$('bulkAssignExpenses').onclick=()=>act(bulkAssignVisibleExpenses);
$('returnSelected').onclick=()=>act(()=>returnReviewedExpenses(base().filter(x=>matches(x,'submitted')&&checked.has(x.id)).map(x=>x.id)));
async function returnReviewedExpenses(ids){
 if(batchReturnBusy||batchVerifyBusy||singleVerifyBusy)return;
 if(!currentInvoiceProjectId()||!ids.length)throw Error('请先勾选当前列表中要退回待核对的费用。');
 const projectId=currentInvoiceProjectId();let committed=false;
 batchReturnBusy=true;renderSelection();
 try{
  await load();
  const items=ids.map(id=>state.items.find(item=>item.id===id));
  if(items.some(item=>!item||item.project_id!==projectId||!submissionReady(item)||!item.version))throw Error('勾选费用的状态已变化，请刷新并重新选择已核对费用。');
  const versions=Object.fromEntries(items.map(item=>[item.id,item.version]));
  const hidden=[...checked].filter(id=>!ids.includes(id)).length;
  if(!await confirmVerification(`将这 ${items.length} 笔费用退回待核对：\n\n${items.slice(0,10).map(item=>`${item.title} · ${expenseMoney(item)}`).join('\n')}${items.length>10?'\n…':''}\n\n金额、费用归属、原件和历史报销文件保留。退回后可修改费用，需要重新核对；缺材料的费用也需要重新确认。${hidden?`\n其他 ${hidden} 笔勾选不参与本次退回。`:''}`,{title:'批量退回待核对',confirmText:'确认退回'})){notify('已取消退回，费用保持原状。');return;}
  const result=await api('review/return',{ids,versions,project_id:projectId});committed=true;
  for(const id of ids)checked.delete(id);
  if(ids.includes(active))active=null;
  const updated=new Map(result.items.map(item=>[item.id,item]));state={...state,items:state.items.map(item=>updated.get(item.id)||item)};expensePreviewRequests.clear();
  try{await load();notify(`已退回 ${result.changed} 笔费用，请到“待核对”继续修改和核对。`);}catch(error){render();throw Error('退回已成功；列表刷新失败，请刷新页面继续。'+error.message);}
 }catch(error){if(!committed)render();throw error;}finally{batchReturnBusy=false;renderSelection();}
}
$('selectVisible').onchange=e=>{for(const x of base().filter(x=>matches(x,view))){if(e.target.checked)checked.add(x.id);else checked.delete(x.id);}renderSelection();};
$('verifySelected').onclick=()=>act(async()=>{
  if(batchVerifyBusy||singleVerifyBusy||batchReturnBusy||!checked.size)return;
  const ids=[...checked], skipped=[], failed=[];let completed=0, refreshError='';
  batchVerifyBusy=true;renderSelection();
  try{
    // Refresh eligibility before confirmation; the server checks it again on each write.
    await load();
    const candidates=[];
    for(const id of ids){
      const x=state.items.find(item=>item.id===id);
      const reason=!x?'事项已不存在':x.deleted?'事项已删除':returnedToSynthesis(x)?'已退回合成台':x.stage!=='draft'?'不是待提交状态':x.ready?'已经核对完成':verificationBlockReason(x);
      if(reason)skipped.push(`${x?.title||id}：${reason}`);else candidates.push(x);
    }
    const visibleIds=new Set(base().filter(x=>matches(x,view)).map(x=>x.id));
    const hidden=candidates.filter(x=>!visibleIds.has(x.id)).length;
    const incomplete=candidates.filter(x=>!x.complete),requests=candidates.map(x=>verificationRequest(x));
    const missingText=incomplete.length?`\n\n其中 ${incomplete.length} 笔仍缺材料，确认后也将核对通过：\n${missingVerificationText(incomplete)}\n\n缺项会继续显示，打印仅使用已收到的材料。是否仍然核对完成？`:'';
    if(candidates.length&&!await confirmVerification(`将 ${candidates.length} 笔事项标记为核对完成${hidden?`（含不在当前列表中的 ${hidden} 笔）`:''}，跳过 ${skipped.length} 笔。\n请确认已核对这些凭证的内容和金额；完成后进入已核对，可查看最终打印页。\n\n${candidates.slice(0,10).map(x=>x.title).join('\n')}${candidates.length>10?'\n…':''}${missingText}`,incomplete.length?{title:'批量核对 · 包含缺材料费用',confirmText:'仍然核对完成'}:{})){notify('已取消批量核对，事项未改变。');return;}
    for(const x of candidates){
      notify(`批量核对中：${completed+failed.length+1}/${candidates.length}…`);
      try{
        const request=requests.find(request=>request.id===x.id),currentItem=state.items.find(item=>item.id===x.id);
        if(!currentItem||currentItem.version!==request.expected_version||!currentItem.complete&&JSON.stringify(currentItem.missing)!==JSON.stringify(request.expected_missing))throw Error('费用或缺项已变化，请重新确认。');
        const updated=await api('transition',request);
        state.items=state.items.map(item=>item.id===x.id?updated:item);checked.delete(x.id);if(active===x.id)active=null;completed++;
      }catch(error){failed.push(`${x.title}：${error.message}`);}
    }
    try{await load();}catch(error){render();refreshError='列表刷新失败，请刷新页面核实最新状态。';}
    notify(`批量核对完成：成功 ${completed} 笔，跳过 ${skipped.length} 笔，失败 ${failed.length} 笔。已核对的费用可在“已核对”查看最终打印页和制作报销包。${refreshError}`);
    if(skipped.length||failed.length){
      const detail=document.createElement('details'), title=document.createElement('summary'), list=document.createElement('ul');
      title.textContent='查看跳过与失败原因';detail.append(title,list);
      for(const text of [...skipped.map(x=>'跳过：'+x),...failed.map(x=>'失败：'+x)]){const li=document.createElement('li');li.textContent=text;list.append(li);}
      $('notice').append(detail);
    }
  }finally{batchVerifyBusy=false;renderSelection();}
});
window.addEventListener('beforeunload',e=>{if(batchVerifyBusy||singleVerifyBusy||batchReturnBusy){e.preventDefault();e.returnValue='';}});
$('importHistory').onclick=()=>act(async()=>{const result=await api('import');await load();notify(`同步完成：新增 ${result.added} 笔，缺少文件或字段 ${result.skipped} 笔。历史记录未自动标记已报销。`);});
$('exportSelected').onclick=()=>act(async()=>{const result=await api('export',{ids:reportSelectionIds()});notify('导出完成（未改变报销状态）。');const area=$('notice');for(const [key,label] of [['pdf','下载总 PDF'],['md','下载 MD 清单']]){const link=document.createElement('a');link.href='/export/'+encodeURIComponent(result[key]);link.textContent='　'+label;link.target='_blank';link.rel='noopener';area.append(link);} });
act(async()=>{await load();if(state.history_import?.skipped)notify(`历史记录有 ${state.history_import.skipped} 笔缺少文件或字段，未纳入台账，请核对原工作区。`,true);});
const projectActions=document.createElement('div');
projectActions.className='actions';
projectActions.hidden=!!projectScope;
projectActions.innerHTML='<button id="moveProject">移动勾选事项到项目</button>';
document.querySelector('.toolbar').append(projectActions);
let projectCreationBusy=false;
const createProjectButton=$('createProject')||document.createElement('button');
createProjectButton.id='createProject';createProjectButton.type='button';
createProjectButton.classList.add('project-create-button');
if(!createProjectButton.textContent)createProjectButton.textContent='＋ 新建项目';
const projectCreateMedia=matchMedia('(max-width:980px)');
function placeCreateProjectButton(){
 const parent=embeddedWorkbench?document.querySelector('.toolbar'):projectCreateMedia.matches?document.querySelector('header .actions'):document.querySelector('.rail');
 createProjectButton.classList.toggle('nav',!embeddedWorkbench&&!projectCreateMedia.matches);
 if(createProjectButton.parentElement!==parent){
  if(parent.matches('.rail'))parent.querySelector('.brand').after(createProjectButton);
  else parent.prepend(createProjectButton);
 }
}
placeCreateProjectButton();projectCreateMedia.addEventListener('change',placeCreateProjectButton);
createProjectButton.onclick=()=>{
 const existing=$('project-create');
 if(projectCreationBusy)return;
 if(existing){existing.querySelector('[name="name"]').focus();return;}
 const dialog=document.createElement('dialog');
 dialog.id='project-create';dialog.className='expense-action-dialog';
 dialog.setAttribute('aria-labelledby','project-create-title');
 dialog.innerHTML='<div class="dialoghead"><h2 id="project-create-title">新建项目</h2><button class="dialog-close" type="button" data-dismiss aria-label="关闭">×</button></div><form><label>项目名称<input name="name" required maxlength="120" autocomplete="off"></label><p data-create-error role="alert" hidden></p><div class="actions"><button type="button" data-cancel>取消</button><button type="submit" disabled>创建项目</button></div></form>';
 document.body.append(dialog);dialog.showModal();
 const input=dialog.querySelector('[name="name"]'),submit=dialog.querySelector('[type="submit"]'),cancel=dialog.querySelector('[data-cancel]'),errorArea=dialog.querySelector('[data-create-error]');
 let committed=false;
 input.oninput=()=>{submit.disabled=committed||projectCreationBusy||!input.value.trim();};
 const dismiss=()=>{if(!projectCreationBusy)dialog.remove();};cancel.onclick=dismiss;dialog.querySelector('[data-dismiss]').onclick=dismiss;
 dialog.oncancel=e=>{e.preventDefault();if(!projectCreationBusy)dialog.remove();};
 dialog.querySelector('form').onsubmit=async e=>{
  e.preventDefault();const name=input.value.trim();
  if(committed||projectCreationBusy||!name||!input.checkValidity())return;
  projectCreationBusy=true;createProjectButton.disabled=true;submit.disabled=true;cancel.disabled=true;dialog.querySelector('[data-dismiss]').disabled=true;input.disabled=true;
  submit.textContent='正在创建…';errorArea.hidden=true;errorArea.textContent='';
  try{
   const project=await api('projects',{name});
   committed=true;
   if(typeof project?.id!=='string'||!project.id.trim())throw Error('项目创建结果缺少编号，请关闭窗口并刷新项目列表核对。');
   const target=new URL('/',location.href);target.searchParams.set('project',project.id);
   if(embeddedWorkbench)target.searchParams.set('embedded','1');
   location.assign(target.pathname+target.search);
  }catch(error){
   errorArea.textContent=error.message;errorArea.hidden=false;
   if(committed){projectCreationBusy=false;createProjectButton.disabled=false;cancel.disabled=false;dialog.querySelector('[data-dismiss]').disabled=false;submit.textContent='已请求创建';}
  }
  finally{
   if(!committed){projectCreationBusy=false;createProjectButton.disabled=false;cancel.disabled=false;dialog.querySelector('[data-dismiss]').disabled=false;input.disabled=false;submit.disabled=!input.value.trim();submit.textContent='创建项目';input.focus();}
  }
 };
 input.focus();
};
$('moveProject').onclick=()=>act(async()=>{
 if(!checked.size)throw Error('请先勾选待提交事项');
 const projects=state.projects||[];if(!projects.length)throw Error('请先新建项目');
 const dialog=document.createElement('dialog');
 dialog.innerHTML='<div class="dialoghead"><h2>批量调整项目</h2><button class="dialog-close" type="button" data-dismiss aria-label="关闭">×</button></div><p>保留每笔金额及材料，移动后需重新核对。</p><select>'+projects.map(p=>`<option value="${esc(p.id)}">${esc(p.name)}</option>`).join('')+'</select><button data-confirm>确认移动</button><button data-cancel>取消</button>';
 document.body.append(dialog);dialog.showModal();
 dialog.querySelectorAll('[data-cancel],[data-dismiss]').forEach(button=>button.onclick=()=>dialog.remove());
 dialog.querySelector('[data-confirm]').onclick=()=>act(async()=>{
 const ids=[...checked], versions=Object.fromEntries(state.items.filter(x=>checked.has(x.id)).map(x=>[x.id,x.version]));
 const result=await api('projects/move',{ids,project_id:dialog.querySelector('select').value,versions});
 dialog.remove();await load();notify(`已移动 ${result.moved} 笔，金额和材料关联保持不变。`);
 });
});
