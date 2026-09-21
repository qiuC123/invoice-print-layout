'use strict';
// Preview first. Only explicit Save writes the image and a draft matter.
const receiptDialog = document.createElement('dialog');
receiptDialog.id = 'receiptDialog';
receiptDialog.className = 'receipt-dialog';
receiptDialog.innerHTML = `<div class="dialoghead"><h2>上传凭证并识别</h2><button id="closeReceipt" aria-label="关闭凭证识别">×</button></div>
<p class="muted">本地 OCR · 不调用大模型 · 保存前请对照原图核对。每次一张，PNG/JPG/WebP，最多20MB。</p>
<label>选择购买凭证截图<input id="receiptFile" type="file" accept=".png,.jpg,.jpeg,.webp"></label>
<p id="receiptProgress" role="status"></p><div id="receiptError" role="alert" hidden></div>
<div class="receipt-grid"><div id="receiptImagePanel" hidden><a id="receiptOriginal" target="_blank" rel="noopener"><img id="receiptPreview" alt="待识别的购买凭证原图"></a><p class="muted">点击原图可放大查看，原件不会裁剪或改写。</p></div><div id="receiptResult"></div></div>`;
document.body.append(receiptDialog);
$('receiptPreview').onerror=()=>{$('receiptImagePanel').hidden=true;};
let receiptUpload = null, receiptObjectUrl = null, receiptBusy = false;
function receiptError(error) { $('receiptError').textContent=error.message; $('receiptError').hidden=false; }
function receiptButtons(busy) {
  receiptBusy=busy;
  $('receiptFile').disabled=busy;
  $('closeReceipt').disabled=busy;
  receiptDialog.querySelectorAll('button[type=submit]').forEach(b=>b.disabled=busy);
}
function clearReceipt() {
  if(receiptObjectUrl) URL.revokeObjectURL(receiptObjectUrl);
  receiptUpload=null; receiptObjectUrl=null;
  $('receiptFile').value=''; $('receiptPreview').removeAttribute('src');
  $('receiptOriginal').removeAttribute('href'); $('receiptImagePanel').hidden=true;
  $('receiptResult').replaceChildren(); $('receiptProgress').textContent=''; $('receiptError').hidden=true;
}
$('recognizeReceipt').onclick=async()=>{
  try { if(!state.token) await load(); clearReceipt(); receiptDialog.showModal(); }
  catch(e) { notify(e.message,true); }
};
$('closeReceipt').onclick=()=>receiptDialog.close();
receiptDialog.addEventListener('cancel',e=>{if(receiptBusy)e.preventDefault();});
receiptDialog.addEventListener('close',clearReceipt);
function duplicateMarkup(items) {
  return `<div class="rule warn">发现已有事项，不重复登记。可打开材料夹查看或补充材料。</div>`+items.map(x=>`<div class="attachment"><strong>${esc(x.title)}</strong><p class="muted">${esc(x.reason)} · ${esc(state.stages[x.stage]||x.stage)}</p><button type="button" data-open-receipt="${esc(x.id)}">打开已有事项</button></div>`).join('');
}
receiptDialog.addEventListener('click',e=>{
  const b=e.target.closest('[data-open-receipt]'); if(!b)return;
  act(async()=>{active=b.dataset.openReceipt;view='all';$('projectFilter').value='';$('categoryFilter').value='';$('search').value='';await load();receiptDialog.close();$('detail').scrollIntoView({block:'nearest'});});
});
$('receiptFile').onchange=async e=>{
  const file=e.target.files[0]; if(!file)return;
  $('receiptError').hidden=true; $('receiptResult').replaceChildren(); receiptUpload=null;
  if(receiptObjectUrl)URL.revokeObjectURL(receiptObjectUrl);
  $('receiptImagePanel').hidden=true;
  if(file.size>20*1024*1024){receiptError(Error('图片超过20MB，请选择较小的原图'));return;}
  receiptObjectUrl=URL.createObjectURL(file); $('receiptPreview').src=receiptObjectUrl;
  $('receiptOriginal').href=receiptObjectUrl; $('receiptImagePanel').hidden=false;
  receiptButtons(true); $('receiptProgress').textContent='正在本机识别并查重…首次加载可能稍慢，请稍候。';
  try {
    const data=await new Promise((resolve,reject)=>{const r=new FileReader();r.onload=()=>resolve(r.result.split(',')[1]);r.onerror=()=>reject(Error('图片读取失败'));r.readAsDataURL(file);});
    receiptUpload={name:file.name,data};
    const result=await api('recognize',receiptUpload);
    $('receiptProgress').textContent='识别完成；尚未写入台账。';
    if(result.duplicates.length){$('receiptResult').innerHTML=duplicateMarkup(result.duplicates);return;}
    const x={...result.fields,project:$('projectFilter').value||''};
    $('receiptResult').innerHTML=`<div class="rule warn">${result.warnings.map(esc).join('<br>')||'请核对识别结果。'}<br>保存后仍是待提交、未核对状态，不会自动报销。</div>
      <form id="receiptForm">${formFields(x)}<label class="receipt-role">这张图片的用途<select name="role">${Object.entries(state.roles).filter(([r])=>r!=='package').map(([r,t])=>`<option value="${r}" ${r===result.role?'selected':''}>${esc(t)}</option>`).join('')}</select></label>
      <div class="actions"><button type="submit" class="primary">保存凭证与事项</button></div></form>
      <details><summary>查看识别依据与原文</summary><pre>${esc(Object.entries(result.evidence).map(([k,v])=>k+': '+v).join('\n'))}\n\n${esc(result.text)}</pre></details>`;
    $('receiptForm').onsubmit=async event=>{
      event.preventDefault(); if(receiptBusy||!receiptUpload)return;
      const {role,...fields}=Object.fromEntries(new FormData(event.target));
      receiptButtons(true); $('receiptError').hidden=true;
      try {
        const saved=await api('receipt',{...receiptUpload,fields,role});
        if(saved.duplicates.length){$('receiptResult').innerHTML=duplicateMarkup(saved.duplicates);return;}
        active=saved.item.id;view='all';$('projectFilter').value='';$('categoryFilter').value='';$('search').value='';
        receiptDialog.close();await load();notify('凭证与事项已保存，尚未核对。可在右侧补发票或扣费记录。');
      } catch(error){receiptError(error);} finally{receiptButtons(false);}
    };
  } catch(error){$('receiptProgress').textContent='未完成识别，没有写入台账。可重新选择图片重试。';receiptError(error);}
  finally{receiptButtons(false);}
};
