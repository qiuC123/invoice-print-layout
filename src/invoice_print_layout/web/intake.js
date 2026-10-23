'use strict';
(() => {
 const dialog=document.createElement('dialog');dialog.id='intakeDialog';dialog.style.cssText='width: min(1200px,95vw);max-width:95vw';
 dialog.innerHTML='<div class="dialoghead"><h2>统一收件与待处理</h2><button data-close class="dialog-close" type="button" aria-label="关闭">×</button></div><p>材料已保存在本机。补充材料不增加金额，自动整理不会核对或提交。</p><div class="actions"><input id="intakeFiles" type="file" multiple accept=".pdf,.png,.jpg,.jpeg,.webp"><button id="claimInvoice">二维码／链接领票</button><button id="intakeMail">纳入已补收邮件</button><select id="intakeFilter"><option value="">全部收件</option><option>金额冲突</option><option>疑似重复</option><option>手写难辨</option></select><button id="intakeRefresh">刷新</button></div><p id="intakeSummary" role="status"></p><div id="intakeRows"></div>';
 document.body.append(dialog);
 $('intakeFilter').append(new Option('归档','归档'),new Option('已删除','已删除'));
 let entries=[];
 async function refresh(){const res=await fetch('/api/intake');if(!res.ok)throw Error('无法读取收件');entries=(await res.json()).queue.filter(q=>!projectScope||q.project_id===projectScope).map(q=>({...q,issues:(q.issues||[]).filter(issue=>!['商品不明','归属不明','归属未定'].includes(issue))}));draw();}
 function draw(){
  const counts={};for(const q of entries)counts[q.result]=(counts[q.result]||0)+1;
  const archived=q=>['unused','archived'].includes(q.status),inactive=q=>archived(q)||q.status==='deleted';
  $('intakeSummary').textContent=`已保存 ${entries.length} 份 · 新增费用 ${counts['新增费用']||0} · 补充材料 ${counts['补充材料']||0} · 重复文件 ${entries.filter(q=>q.status==='duplicate').length} · 归档 ${entries.filter(archived).length} · 已删除 ${entries.filter(q=>q.status==='deleted').length} · 待处理 ${entries.filter(q=>!['linked','duplicate','unused','archived','deleted','converted'].includes(q.status)).length}`;
  $('intakeRows').innerHTML=entries.filter(q=>!$('intakeFilter').value||($('intakeFilter').value==='归档'?archived(q):$('intakeFilter').value==='已删除'?q.status==='deleted':!inactive(q)&&q.issues.includes($('intakeFilter').value))).map(q=>`<article class="mailrow"><a target="_blank" rel="noopener" href="/intake-file/${esc(q.id)}">${esc(q.name)}</a><p>${esc(q.source)} · ${esc(q.status==='unused'?q.unused_reason||q.result:q.result)}${inactive(q)?'':' · '+esc(q.issues.join('、'))}</p>${inactive(q)?`<span class="badge neutral">${q.status==='deleted'?'已删除 · 可恢复':'归档 · 原件保留'}</span>`:q.item_id?`<a href="/?matter=${encodeURIComponent(q.item_id)}${projectScope?'&project='+encodeURIComponent(projectScope):''}" target="_blank">查看所属费用</a>`:`<button data-analyze="${esc(q.id)}">识别／重试</button><button data-claim="${esc(q.id)}">识别领票二维码</button><button data-resolve="${esc(q.id)}">对照原件处理</button>`}</article>`).join('');
 }
 async function open(){await refresh();$('intakeMail').hidden=!!projectScope;dialog.showModal();}
 window.openUnifiedIntake=open;
 if($('recognizeReceipt')){$('recognizeReceipt').onclick=()=>act(open);$('recognizeReceipt').textContent='＋ 收图片／PDF并整理';}
 dialog.querySelector('[data-close]').onclick=()=>dialog.close();$('intakeRefresh').onclick=()=>act(refresh);$('intakeFilter').onchange=draw;
 let intakeUploadBusy=false;
 $('intakeFiles').onchange=()=>act(async()=>{
  if(intakeUploadBusy)return;
  const files=[...$('intakeFiles').files];if(files.length>50)throw Error('每批最多50份');
  if(!files.length)return;
  const batch=crypto.randomUUID(),project=projectContext(),uploadProject=project?.id||projectScope||'',agent=window.manualInvoiceAgent;
  const organizer=uploadProject&&typeof agent?.shouldAutoOrganize==='function'&&typeof agent?.startReceived==='function'&&agent.shouldAutoOrganize(uploadProject)?agent:null;
  const entryIds=new Set();let received=0;
  let duplicates=0,failed=0,converted=0,unused=0,matched=0,recognitionReviews=0,autoError='',autoFeedback='';
  intakeUploadBusy=true;dialog.querySelectorAll('button,input,select').forEach(control=>control.disabled=true);
  try{
   for(const [index,file] of files.entries()){
    $('intakeSummary').textContent=`正在收件 ${index+1}/${files.length}：${file.name}`;
    try{
     const data=await new Promise((resolve,reject)=>{const r=new FileReader();r.onload=()=>resolve(r.result.split(',')[1]);r.onerror=reject;r.readAsDataURL(file);});
     const q=await api('intake/receive',{name:file.name,data,project_id:uploadProject,batch});received++;
     if(q.duplicate||q.status==='duplicate')duplicates++;
     else if(organizer){if(typeof q.id==='string'&&q.id&&q.project_id===uploadProject&&!q.item_id&&['received','review'].includes(q.status))entryIds.add(q.id);}
     else{const prepared=await api('intake/prepare',{id:q.id});if(prepared.converted_to_pdf)converted++;if(prepared.entry?.status==='unused')unused++;else if(['failed','review'].includes(prepared.entry?.ai_analysis?.status))recognitionReviews++;}
    }catch(error){failed++;notify(file.name+'：'+error.message,true);}
   }
   if(organizer){
    // Files have been saved. Read failures after handing off must not re-send
    // the batch or repeat the Agent start request.
    $('intakeFiles').value='';let started=false,startError='';
    if(entryIds.size){try{await organizer.startReceived({projectId:uploadProject,entryIds:[...entryIds],batch});started=true;}catch(error){startError=`自动整理未能启动：${error.message}。原件已保存，请在自动整理助手中查看或重试。`;}}
    const summary=`本批 ${files.length} 份，已保存 ${received} 份，重复 ${duplicates} 份，上传失败 ${failed} 份。`+(started?`已将本批 ${entryIds.size} 份新材料交给自动整理；缺少依据时会在当前工作台询问。`:startError||'本批没有可交给自动整理的新材料；原件及已有费用关联保持。');
    if(started){dialog.close();notify(summary,failed>0);try{await organizer.open?.();}catch(error){notify(summary+` 助手显示未完成：${error.message}；任务已启动，请刷新查看，无需重新上传。`,true);}}else{let refreshError='';try{await refresh();}catch(error){refreshError=' 材料刷新未完成：'+error.message;}notify(summary+refreshError,!!startError||failed>0||!!refreshError);}
    return;
   }
   try{const result=await api('material-matching/auto',{project_id:project?.id||projectScope||''});matched=result.suggested||0;autoFeedback=[...new Set((result.conflicts||[]).map(value=>value.reason).filter(Boolean))].join(' ');}catch(error){autoError=' 建议组合扫描未完成：'+error.message;}
   $('intakeFiles').value='';await refresh();await load();if(window.synthesisSuggestionsLoaded?.())await window.refreshSynthesisCandidates?.();notify(`本批 ${files.length} 份，重复 ${duplicates} 份，失败 ${failed} 份，转为发票PDF ${converted} 份，无需使用 ${unused} 份，建议组合 ${matched} 组。请在材料合成台查看建议组合，确认创建后进入待核对。${recognitionReviews?` ${recognitionReviews}份识别需人工处理。`:''}已收到的材料可在收件区重试。${agent&&!uploadProject?' 自动整理请先从侧栏选择项目。':''}${autoError}${autoFeedback}`,!!autoError||recognitionReviews>0);
  }finally{
   intakeUploadBusy=false;dialog.querySelectorAll('button,input,select').forEach(control=>control.disabled=false);
  }
 });
 // Use a page-owned form: embedded browsers do not support native prompt().
 window.openInvoiceClaim=({project_id='',onComplete}={})=>{
  if(document.getElementById('invoiceClaimDialog'))return;
  const claimDialog=document.createElement('dialog');claimDialog.id='invoiceClaimDialog';claimDialog.className='invoice-claim-dialog';
  claimDialog.innerHTML='<div class="dialoghead"><h2>二维码／链接领票</h2><button type="button" data-claim-close aria-label="关闭领票" class="dialog-close">×</button></div><p>粘贴二维码对应的网址。领取到的原始PDF将放入当前项目的材料合成台。</p><form id="invoiceClaimForm" novalidate><label>领票链接<input id="invoiceClaimUrl" name="url" type="url" autocomplete="off" placeholder="https://…" required></label><p id="invoiceClaimStatus" role="status" hidden></p><div id="invoiceClaimLink"></div><div class="actions"><button id="invoiceClaimSubmit" type="submit" class="primary">领取发票</button><button type="button" data-claim-close>取消</button></div></form>';
  document.body.append(claimDialog);
  const form=claimDialog.querySelector('form'),input=$('invoiceClaimUrl'),submit=$('invoiceClaimSubmit'),status=$('invoiceClaimStatus');
  let saving=false,finished=false,received=null;
  const show=(text,error=false)=>{status.hidden=false;status.textContent=text;status.classList.toggle('claim-error',error);};
  const close=()=>{if(!saving){claimDialog.close();claimDialog.remove();}};
  claimDialog.querySelectorAll('[data-claim-close]').forEach(button=>button.onclick=close);
  claimDialog.addEventListener('cancel',event=>{event.preventDefault();close();});
  form.onsubmit=async event=>{
   event.preventDefault();if(saving||finished)return;
   const url=input.value.trim();
   if(!received){
    try{const parsed=new URL(url);if(!['http:','https:'].includes(parsed.protocol)||!parsed.hostname||parsed.username||parsed.password)throw Error();}
    catch(_){show('请粘贴有效的领票网页链接。',true);input.focus();return;}
   }
   saving=true;claimDialog.querySelectorAll('button,input').forEach(control=>control.disabled=true);show(received?'正在整理已领取的PDF…':'正在领取原始发票…');
   try{
    const result=received||await api('intake/claim',{url,project_id});
    if(result.entry){
     received=result;
     if(!result.entry.duplicate&&result.entry.status!=='duplicate')await api('intake/prepare',{id:result.entry.id});
    }
    // Claim is committed. A failed UI refresh must never re-send it.
    finished=true;show(result.message||'领票处理完成。');
    if(result.url){
     try{const parsed=new URL(result.url);if(['http:','https:'].includes(parsed.protocol)&&!parsed.username&&!parsed.password){
      const link=document.createElement('a');link.href=parsed.href;link.target='_blank';link.rel='noopener noreferrer';link.textContent='打开领取入口';$('invoiceClaimLink').append(link);
     }}catch(_){ /* Do not render an invalid returned URL. */ }
    }
    if(result.entry&&onComplete){try{await onComplete(result);}catch(error){show('原始PDF已保存，页面刷新未完成：'+error.message+'。关闭后可刷新材料。',true);}}
   }catch(error){show((received?'原始PDF已保存，整理未完成：':'领取未完成：')+error.message+'。可以重试。',true);submit.textContent=received?'重试整理':'重试领取';}
   finally{
    saving=false;claimDialog.querySelectorAll('[data-claim-close]').forEach(control=>control.disabled=false);input.disabled=finished||!!received;submit.disabled=finished;
   }
  };
  claimDialog.showModal();input.focus();
 };
 $('claimInvoice').onclick=()=>{
  const project=projectContext();
  window.openInvoiceClaim({project_id:project?.id||projectScope||'',onComplete:async()=>{await refresh();await load();}});
 };
 $('intakeMail').onclick=()=>act(async()=>{await api('intake/mail');await refresh();for(const q of entries.filter(x=>x.status==='received'))await api('intake/analyze',{id:q.id});await refresh();await load();});
 $('intakeRows').onclick=e=>act(async()=>{
  if(e.target.dataset.claim){const result=await api('intake/claim',{id:e.target.dataset.claim});$('intakeSummary').textContent=result.message;if(result.url){const a=document.createElement('a');a.href=result.url;a.target='_blank';a.rel='noopener noreferrer';a.textContent='打开领取入口';$('intakeSummary').append(a);}if(result.entry){await api('intake/analyze',{id:result.entry.id});await refresh();}return;}
  const key=e.target.dataset.analyze;if(key){await api('intake/analyze',{id:key});await refresh();await load();return;}
  const q=entries.find(x=>x.id===e.target.dataset.resolve);if(q)edit(q);
 });
 function edit(q){
  const d=document.createElement('dialog');d.style.cssText='width:min(1200px,95vw);max-width:95vw';
  const fields={...q.fields,project_id:q.project_id,project:(state.projects||[]).find(p=>p.id===q.project_id)?.name||q.fields.project||''};
  d.innerHTML=`<div class="dialoghead"><h2>确认材料关系</h2><button data-close class="dialog-close" type="button" aria-label="关闭">×</button></div><div style="display:grid;grid-template-columns:1fr 1fr;gap:16px"><iframe title="原件" src="/${q.name.toLowerCase().endsWith('.pdf')?'intake-preview':'intake-file'}/${q.id}" style="width:100%;height:65vh"></iframe><form><label>所属费用<select name="item_id"><option value="">新增一笔未核对费用</option>${state.items.filter(x=>!x.deleted&&x.stage==='draft').map(x=>`<option value="${x.id}" ${(q.candidates||[]).includes(x.id)?'selected':''}>${esc(x.title)} · ${money(x.amount_cents)}元</option>`).join('')}</select></label><p>补到已有费用时只增加材料；多笔付款可都补到同一笔费用。</p><label>材料用途<select name="role">${Object.entries(state.roles).filter(([k])=>k!=='package').map(([k,v])=>`<option value="${k}" ${k===q.role?'selected':''}>${esc(v)}</option>`).join('')}</select></label>${formFields(fields)}<button class="primary">保存关系</button><pre style="white-space:pre-wrap">${esc(q.text)}</pre></form></div>`;
  document.body.append(d);d.showModal();d.querySelector('[data-close]').onclick=()=>d.remove();
  d.querySelector('form').onsubmit=e=>{e.preventDefault();act(async()=>{const f=Object.fromEntries(new FormData(e.target)),item=state.items.find(x=>x.id===f.item_id);await api('intake/resolve',{id:q.id,revision:q.revision,fields:f,role:f.role,item_id:f.item_id,expected_version:item?.version||''});d.remove();await refresh();await load();});};
 }
})();
