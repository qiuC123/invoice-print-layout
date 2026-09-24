'use strict';
// Sequential local requests keep OCR memory bounded. No write until explicit Save.
const receiptDialog = document.createElement('dialog');
receiptDialog.id = 'receiptDialog';
receiptDialog.className = 'receipt-dialog';
receiptDialog.innerHTML = `<div class="dialoghead"><h2>批量上传凭证并识别</h2><button id="closeReceipt" aria-label="关闭凭证识别">×</button></div>
<p class="muted">本地 OCR · 可多选 PNG/JPG/WebP，每张最多20MB，每批最多50张、合计200MB。逐张识别，核对后再保存；关闭页面不会保存尚未入账的图片。</p>
<label>选择购买凭证截图（可多选、可追加）<input id="receiptFile" type="file" accept=".png,.jpg,.jpeg,.webp" multiple></label>
<p id="receiptProgress" role="status"></p><div id="receiptError" role="alert" hidden></div>
<div id="receiptQueue" class="receipt-queue" aria-label="凭证队列"></div>
<div class="actions"><button id="saveAllReceipts" class="primary" disabled>保存全部待保存凭证</button></div>
<p class="muted">每张图片登记一笔事项；同一订单的多张图会查重，不自动合并。补充发票或扣费记录请进入已有事项的材料夹。</p>
<div class="receipt-grid"><div id="receiptImagePanel" hidden><a id="receiptOriginal" target="_blank" rel="noopener"><img id="receiptPreview" alt="待识别的购买凭证原图"></a><p class="muted">点击原图可放大查看，原件不会裁剪或改写。</p></div><div id="receiptResult"></div></div>`;
document.body.append(receiptDialog);
let receiptQueue = [], receiptIndex = -1, receiptObjectUrl = null, receiptBusy = false;
const receiptLabels = {waiting:'等待识别', recognizing:'识别中', ready:'待保存', saved:'已保存', duplicate:'重复，未新增', error:'识别失败', 'save-error':'保存失败／需补填', skipped:'已跳过'};
const receiptPending = x => ['ready','save-error'].includes(x.status);
const receiptUnsaved = () => receiptQueue.some(x=>['waiting','recognizing','ready','error','save-error'].includes(x.status));
function receiptError(error) { $('receiptError').textContent=error.message; $('receiptError').hidden=false; }
function receiptButtons(busy) {
  receiptBusy=busy;
  receiptDialog.querySelectorAll('button,input,select,textarea').forEach(el=>el.disabled=busy);
  $('saveAllReceipts').disabled=busy||!receiptQueue.some(receiptPending);
}
function receiptSummary() {
  const counts = Object.entries(receiptLabels).map(([s,label])=>{
    const n=receiptQueue.filter(x=>x.status===s).length; return n?`${label} ${n}`:'';
  }).filter(Boolean);
  $('receiptProgress').textContent=receiptQueue.length?`共 ${receiptQueue.length} 张 · ${counts.join(' · ')}`:'';
  $('receiptQueue').innerHTML=receiptQueue.map((x,i)=>`<button type="button" data-receipt-index="${i}" class="${i===receiptIndex?'selected':''}" aria-pressed="${i===receiptIndex}"><strong>${i+1}. ${esc(x.file.name)}</strong><span>${esc(receiptLabels[x.status])}${x.fields?` · ${esc(x.fields.title)} · ¥${esc(x.fields.amount||'待填')}`:''}</span></button>`).join('');
  receiptButtons(receiptBusy);
}
function clearReceipt() {
  if(receiptObjectUrl) URL.revokeObjectURL(receiptObjectUrl);
  receiptQueue=[]; receiptIndex=-1; receiptObjectUrl=null;
  $('receiptFile').value=''; $('receiptPreview').removeAttribute('src');
  $('receiptOriginal').removeAttribute('href'); $('receiptImagePanel').hidden=true;
  $('receiptResult').replaceChildren(); $('receiptError').hidden=true;
  receiptSummary();
}
function closeReceipt() {
  if(receiptBusy)return;
  if(receiptUnsaved()&&!confirm('尚有未保存的凭证，关闭后需要重新选择。确定关闭吗？'))return;
  receiptDialog.close();
}
$('recognizeReceipt').onclick=async()=>{
  try { if(!state.token) await load(); clearReceipt(); receiptDialog.showModal(); }
  catch(e) { notify(e.message,true); }
};
$('closeReceipt').onclick=closeReceipt;
receiptDialog.addEventListener('cancel',e=>{e.preventDefault();closeReceipt();});
receiptDialog.addEventListener('close',clearReceipt);
window.addEventListener('beforeunload',e=>{if(receiptBusy||receiptUnsaved()){e.preventDefault();e.returnValue='';}});
function duplicateMarkup(items) {
  return `<div class="rule warn">发现已有事项，不重复登记。可打开材料夹查看或补充材料。</div>`+items.map(x=>`<div class="attachment"><strong>${esc(x.title)}</strong><p class="muted">${esc(x.reason)} · ${esc(state.stages[x.stage]||x.stage)}</p><button type="button" data-open-receipt="${esc(x.id)}">打开已有事项（新标签页）</button></div>`).join('');
}
function captureReceipt() {
  const form=$('receiptForm'), x=receiptQueue[receiptIndex];
  if(!form||!x||receiptBusy)return;
  const {role,...fields}=Object.fromEntries(new FormData(form));
  x.role=role; x.fields=fields;
}
function showReceipt(index) {
  receiptIndex=index; const x=receiptQueue[index]; if(!x)return;
  if(receiptObjectUrl)URL.revokeObjectURL(receiptObjectUrl);
  receiptObjectUrl=URL.createObjectURL(x.file);
  $('receiptPreview').src=receiptObjectUrl; $('receiptOriginal').href=receiptObjectUrl;
  $('receiptImagePanel').hidden=false; $('receiptError').hidden=true;
  let html=`<h2>${esc(x.file.name)}</h2>`;
  if(x.error)html+=`<div class="rule warn">${esc(x.error)}</div>`;
  if(x.status==='duplicate')html+=duplicateMarkup(x.duplicates);
  else if(x.status==='saved')html+=`<p>已保存，尚未核对。不会重复入账。</p><button data-open-receipt="${esc(x.itemId)}">打开已有事项（新标签页）</button>`;
  else if(x.status==='error')html+='<button id="retryReceipt">重试识别</button>';
  else if(x.status==='skipped')html+='<p>本张不入账。</p><button id="restoreReceipt">恢复此张</button>';
  else if(receiptPending(x))html+=`<div class="rule warn">${x.result.warnings.map(esc).join('<br>')||'请核对识别结果。'}<br>保存后仍是待提交、未核对状态，不会自动报销。</div>
    <form id="receiptForm">${formFields(x.fields)}<label class="receipt-role">这张图片的用途<select name="role">${Object.entries(state.roles).filter(([r])=>r!=='package').map(([r,t])=>`<option value="${r}" ${r===x.role?'selected':''}>${esc(t)}</option>`).join('')}</select></label>
    <div class="actions"><button type="submit" class="primary">仅保存此张</button></div></form>
    <details><summary>查看识别依据与原文</summary><pre>${esc(Object.entries(x.result.evidence).map(([k,v])=>k+': '+v).join('\n'))}\n\n${esc(x.result.text)}</pre></details>`;
  if(['ready','save-error','error'].includes(x.status))html+='<div class="actions"><button id="skipReceipt">跳过此张</button></div>';
  $('receiptResult').innerHTML=html;
  if($('receiptForm')){
    $('receiptForm').oninput=()=>{captureReceipt();receiptSummary();};
    // Do not rebuild queue buttons on blur: that would consume the navigation click.
    $('receiptForm').onchange=captureReceipt;
    $('receiptForm').onsubmit=e=>{e.preventDefault();saveReceipts([index]);};
  }
  if($('retryReceipt'))$('retryReceipt').onclick=()=>recognizeReceipts([index]);
  if($('skipReceipt'))$('skipReceipt').onclick=()=>{captureReceipt();x.previousStatus=x.status;x.status='skipped';showReceipt(index);};
  if($('restoreReceipt'))$('restoreReceipt').onclick=()=>{x.status=x.previousStatus;showReceipt(index);};
  receiptSummary();
}
receiptDialog.addEventListener('click',e=>{
  if(receiptBusy)return;
  const row=e.target.closest('[data-receipt-index]');
  if(row){captureReceipt();showReceipt(Number(row.dataset.receiptIndex));}
  const b=e.target.closest('[data-open-receipt]');
  if(b)window.open('/?matter='+encodeURIComponent(b.dataset.openReceipt),'_blank','noopener');
});
function readReceiptFile(file) {
  return new Promise((resolve,reject)=>{const r=new FileReader();r.onload=()=>resolve({name:file.name,data:r.result.split(',')[1]});r.onerror=()=>reject(Error('图片读取失败'));r.readAsDataURL(file);});
}
async function recognizeReceipts(indices) {
  if(receiptBusy)return;
  captureReceipt(); receiptButtons(true);
  try {
    for(const index of indices){
      const x=receiptQueue[index]; x.status='recognizing'; x.error=''; receiptSummary();
      try {
        if(x.file.size>20*1024*1024)throw Error('图片超过20MB，请选择较小的原图');
        if(!/\.(png|jpe?g|webp)$/i.test(x.file.name))throw Error('仅支持 PNG/JPG/WebP 图片');
        x.result=await api('recognize',await readReceiptFile(x.file));
        x.duplicates=x.result.duplicates;
        x.status=x.duplicates.length?'duplicate':'ready';
        x.fields={...x.result.fields,project:$('projectFilter').value||''}; x.role=x.result.role;
      } catch(error){x.status='error';x.error=error.message;}
      receiptSummary();
    }
  } finally {
    receiptButtons(false);
    showReceipt(indices[0]);
  }
}
$('receiptFile').onchange=async e=>{
  const files=Array.from(e.target.files); e.target.value=''; if(!files.length||receiptBusy)return;
  $('receiptError').hidden=true;
  if(receiptQueue.length+files.length>50){receiptError(Error('每批最多50张，请先完成当前批次'));return;}
  if([...receiptQueue.map(x=>x.file),...files].reduce((n,f)=>n+f.size,0)>200*1024*1024){receiptError(Error('每批图片合计最多200MB，请分批选择'));return;}
  const start=receiptQueue.length;
  receiptQueue.push(...files.map(file=>({file,status:'waiting'})));
  await recognizeReceipts(files.map((_,i)=>start+i));
};
async function saveReceipts(indices) {
  if(receiptBusy)return;
  captureReceipt(); receiptButtons(true);
  let savedCount=0, refreshError='';
  try {
    for(const index of indices){
      const x=receiptQueue[index]; if(!receiptPending(x))continue;
      showReceipt(index);
      // Disabled controls are exempt from browser validation; enable only to check.
      receiptButtons(false);
      const valid=$('receiptForm').checkValidity();
      captureReceipt(); receiptButtons(true);
      if(!valid){x.status='save-error';x.error='请补齐事项名称、有效金额等必填信息，再重试保存。';receiptSummary();continue;}
      try {
        const result=await api('receipt',{...await readReceiptFile(x.file),fields:x.fields,role:x.role});
        x.error='';
        if(result.duplicates.length){x.status='duplicate';x.duplicates=result.duplicates;}
        else{x.status='saved';x.itemId=result.item.id;active=result.item.id;savedCount++;}
      } catch(error){x.status='save-error';x.error=error.message;}
      receiptSummary();
    }
    if(savedCount){view='all';$('projectFilter').value='';$('categoryFilter').value='';$('search').value='';
      try{await load();}catch(error){refreshError='凭证已保存，但列表刷新失败，请稍后刷新工作台。';}}
  } finally {
    receiptButtons(false);
    const remaining=receiptQueue.findIndex(x=>['save-error','error','ready'].includes(x.status));
    showReceipt(remaining>=0?remaining:indices[0]);
    if(refreshError)receiptError(Error(refreshError));
  }
}
$('saveAllReceipts').onclick=()=>saveReceipts(receiptQueue.map((_,i)=>i).filter(i=>receiptPending(receiptQueue[i])));
