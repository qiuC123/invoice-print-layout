'use strict';
const reportDialog=document.createElement('dialog');
reportDialog.id='reportDialog';
reportDialog.innerHTML=`<div class="dialoghead"><h2>制作报销包</h2><button id="closeReport" aria-label="关闭报销制作">×</button></div>
<p class="muted">打车、外卖、顺丰复用打印助手排版；其他材料完整保留。输出总 PDF＋MD＋Excel，最终按 A4、1×1 打印。</p>
<p id="reportScope"></p><p id="reportTemplateState" class="muted"></p>
<label>设置／更换公司模板（XLSX，最多5MB）<input id="reportTemplateFile" type="file" accept=".xlsx"></label>
<form id="reportForm"><div class="formgrid"><label>报销人<input name="person" maxlength="80"></label><label>报销期间（不填写则按所选事项日期）<input name="period" maxlength="120"></label>
</div>
<p class="muted">主表费用区仅填写“现场现金支付（需报销）”列及合计，其他费用栏留空。活动日期、审核签字等未知内容留空。制作成功后，所选事项自动进入“已提交”；失败保持原状态。</p>
<div class="actions"><button type="submit" id="createReport" class="primary">开始制作 PDF＋MD＋Excel</button></div></form>
<p id="reportProgress" role="status"></p><p id="reportError" role="alert" hidden></p><div id="reportDownloads" class="actions"></div>`;
document.body.append(reportDialog);
let reportIds=[],reportBusy=false;
$('exportSelected').textContent='制作报销包';
function reportLock(busy){reportBusy=busy;reportDialog.querySelectorAll('button,input,select').forEach(x=>x.disabled=busy);$('createReport').disabled=busy||!state.report_template_ready;}
function reportFail(error){$('reportError').textContent=error.message;$('reportError').hidden=false;}
$('exportSelected').onclick=()=>act(async()=>{
  if(!checked.size)throw Error('请先勾选要制作的事项');
  reportIds=[...checked];await load();
  const invalid=reportIds.map(id=>state.items.find(x=>x.id===id)).filter(x=>!x||!x.ready||x.stage!=='draft');
  if(invalid.length)throw Error(`所选事项中有 ${invalid.length} 笔尚未核对、材料不齐或已提交／已报销。请筛选“可提交”后重新勾选。`);
  const visible=new Set(base().filter(x=>matches(x,view)).map(x=>x.id));
  const hidden=reportIds.filter(id=>!visible.has(id)).length;
  $('reportScope').textContent=`已选择 ${reportIds.length} 笔${hidden?`（其中 ${hidden} 笔不在当前列表）`:''}；合计 ¥${money(reportIds.reduce((s,id)=>s+state.items.find(x=>x.id===id).amount_cents,0))}。`;
  $('reportTemplateState').textContent=state.report_template_ready?'公司模板已设置；无需再次上传。':'尚未设置公司模板，请先上传。';
  $('reportForm').reset();$('reportForm').elements.person.value=state.report_person||'';
  $('reportProgress').textContent='';$('reportError').hidden=true;$('reportDownloads').replaceChildren();
  reportLock(false);reportDialog.showModal();
});
$('closeReport').onclick=()=>{if(!reportBusy)reportDialog.close();};
reportDialog.addEventListener('cancel',e=>{if(reportBusy)e.preventDefault();});
window.addEventListener('beforeunload',e=>{if(reportBusy){e.preventDefault();e.returnValue='';}});
$('reportTemplateFile').onchange=async e=>{
  const file=e.target.files[0];e.target.value='';if(!file||reportBusy)return;
  $('reportError').hidden=true;reportLock(true);
  try{
    if(file.size>5*1024*1024||!file.name.toLowerCase().endsWith('.xlsx'))throw Error('请上传5MB以内的XLSX模板');
    await api('report-template',await readReceiptFile(file));
    state.report_template_ready=true;$('reportTemplateState').textContent='公司模板已设置。';
  }catch(error){reportFail(error);}finally{reportLock(false);}
};
$('reportForm').onsubmit=async e=>{
  e.preventDefault();if(reportBusy)return;
  const options=Object.fromEntries(new FormData(e.target));
  reportLock(true);$('reportError').hidden=true;$('reportDownloads').replaceChildren();
  $('reportProgress').textContent='正在逐事项排版并填写 Excel；图片可能需要本地识别，请稍候…';
  try{
    const result=await api('report',{ids:reportIds,options});
    for(const [key,label] of [['pdf','下载总 PDF'],['md','下载 MD 清单'],['xlsx','下载 Excel 报销表']]){
      const a=document.createElement('a');a.href='/export/'+encodeURIComponent(result[key]);a.textContent=label;a.target='_blank';a.rel='noopener';$('reportDownloads').append(a);
    }
    $('reportProgress').textContent='三份文件已生成，所选事项已自动进入“已提交”。PDF请选择A4、1×1打印。';
    $('notice').replaceChildren();notify('报销包已生成（PDF＋MD＋Excel），所选事项已提交。');
    for(const a of $('reportDownloads').children)$('notice').append(a.cloneNode(true));
    for(const id of reportIds)checked.delete(id);
    try{await load();}catch(error){$('reportProgress').textContent+=' 列表刷新失败，请刷新页面查看已提交事项；文件已生成，无需重复制作。';}
  }catch(error){$('reportProgress').textContent='制作未完成，请按提示检查；原件与事项状态保留。';reportFail(error);}
  finally{reportLock(false);}
};
