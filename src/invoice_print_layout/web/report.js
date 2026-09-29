'use strict';
// Read-only HTML review is generated from a fresh snapshot in the browser.
// It deliberately does not call the report endpoint, which submits expenses.
function buildReportPreviewHtml(items, meta={}) {
  if(new Set(items.map(x=>x.id)).size!==items.length)throw Error('预览包含重复事项');
  if(items.some(x=>!Number.isSafeInteger(x.amount_cents)||x.amount_cents<0))throw Error('预览金额无效');
  const total=items.reduce((n,x)=>n+x.amount_cents,0);
  if(!Number.isSafeInteger(total))throw Error('预览合计超出范围');
  const projects=[...new Set(items.map(x=>x.project||'待分配项目'))];
  const dates=items.map(x=>x.expense_date).filter(Boolean).sort();
  const period=dates.length?dates[0]+' 至 '+dates[dates.length-1]+(dates.length<items.length?'（部分日期待补）':''):'消费日期待填写';
  const itemName=x=>{
    if(x.display_name)return x.display_name;
    const parts=x.title.split(' · ');let name=parts.length>1?parts.slice(1).join(' · '):x.title;
    if(/^雨衣(?:采购)?(?:[（(].*[）)])?$/.test(name))name='雨衣采购';
    if(/^口罩(?:采购)?(?:[（(].*[）)])?$/.test(name))name='口罩采购';
    return name;
  };
  const rainKey=x=>x.category==='材料采购'&&itemName(x)==='雨衣采购'?(x.project||'待分配项目'):null;
  const categories=new Map(),buckets=new Map(),rainRows=new Map();let materials=0,others=0;
  for(const x of items){
    categories.set(x.category,(categories.get(x.category)||0)+x.amount_cents);
    let row;
    if(x.category==='材料采购'){
      const key=rainKey(x);
      if(key!==null&&rainRows.has(key))row=rainRows.get(key);
      else{row=24+Math.min(materials++,4);if(key!==null)rainRows.set(key,row);}
    }
    else if(x.category==='其他')row=30+Math.min(others++,4);
    else row={'高铁':12,'打车':13,'酒店':16,'外卖':17,'餐饮':19,'顺丰':29}[x.category]||34;
    if(x.category==='外卖'&&/咖啡|奶茶|饮品|饮用水|蜜雪冰城/.test(x.title+' '+x.merchant))row=19;
    if(!buckets.has(row))buckets.set(row,[]);
    buckets.get(row).push(x);
  }
  if(meta.rows){buckets.clear();for(const r of meta.rows)buckets.set(r.row,r.items.map(v=>items.find(x=>x.id===v.id)).filter(Boolean));}
  const rowLabels={8:'场地费用（消费）',9:'施工押金（可退）',10:'货车去程运费',11:'货车回程运费',12:'火车、飞机',13:'打的（出租车、滴滴等）',14:'公交（地铁、公交、大巴等）',15:'货拉拉（附行程单、报销凭证）',16:'住宿费（附清单）',17:'员工统一购餐费（附清单）',18:'生活补助费用（附清单）',19:'现场饮用水、夜宵',20:'工厂搭建人工数',21:'工厂撤展人工数',22:'外请人员金额（附清单）',23:'保洁费（附清单）'};
  const sections={8:['拓展',2],10:['车费',6],16:['生活费',4],20:['人员',4],24:['现场购材料',5],29:['其他',6]};
  let rows='';
  for(let r=8;r<=34;r++){
    const group=buckets.get(r)||[],amount=group.reduce((n,x)=>n+x.amount_cents,0);
    const seenRain=new Set();
    const names=meta.rows&&r>=24?(meta.rows.find(row=>row.row===r)?.name?`<a href="#expenses">${esc(meta.rows.find(row=>row.row===r).name)}</a>`:''):r>=24?(group.length&&group.every(x=>x.category==='顺丰')?'<a href="#expenses">顺丰同城</a>':group.filter(x=>{
      const key=rainKey(x);if(key===null)return true;
      if(seenRain.has(key))return false;seenRain.add(key);return true;
    }).map(x=>{
      const name=itemName(x);
      return `<a href="#expense-${items.indexOf(x)}" title="${esc(x.title)}">${esc(name)}</a>`;
    }).join('<br>')):'';
    rows+=`<tr>${sections[r]?`<th rowspan="${sections[r][1]}">${sections[r][0]}</th>`:''}<th data-cell="B${r}">${esc(rowLabels[r]||(r>=29?(r-28)+'、':(r-23)+'、'))}${names}</th><td></td><td></td><td></td><td></td><td></td><td class="amount ${group.length?'filled':''}" data-cell="H${r}">${group.length?`<a href="#expenses" title="${esc(group.map(x=>x.title).join('；'))}">${money(amount)}</a>`:''}</td><td></td><td></td><td></td><td></td><td></td></tr>`;
  }
  const stage=x=>x.stage==='draft'?(!x.complete?'待补材料':!x.verified?'待核对':'可提交'):({'submitted':'已提交','reimbursed':'已报销','cancelled':'已取消'}[x.stage]||x.stage);
  const fileBase=new URL('/file/',location.origin).href;
  const details=items.map((x,i)=>`<article class="expense" id="expense-${i}"><div class="expense-head"><div><small>${i+1} / ${esc(x.category)} · ${esc(x.expense_date||'日期待补')} · ${esc(x.project||'待分配项目')}</small><h3>${esc(x.title)}</h3></div><strong>¥ ${money(x.amount_cents)}</strong></div><p class="status">${esc(stage(x))}${x.missing?.length?' · 缺：'+esc(x.missing.join('、')):''}</p><p>${esc(x.merchant||'商家未填写')}</p>${x.order_number?`<p class="muted">交易／订单号：${esc(x.order_number)}</p>`:''}<p class="note">${esc(x.note||'')}</p><details><summary>查看 ${x.attachments.length} 份凭证</summary><div class="attachments">${x.attachments.map(a=>{
      const url=fileBase+encodeURIComponent(a.id),label=(meta.roles||{})[a.role]||a.role;
      return `<div class="attachment"><p>${esc(label)}</p>${a.available?`<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(a.name)}</a>${/\.(jpg|jpeg|png|webp)$/i.test(a.name)?`<a href="${esc(url)}" target="_blank" rel="noopener noreferrer"><img loading="lazy" src="${esc(url)}" alt="${esc(a.name)}"></a>`:'<p class="muted">点击文件名查看 PDF 原件</p>'}`:`<p>文件缺失：${esc(a.name)}</p>`}</div>`;
    }).join('')||'<p class="muted">暂无附件</p>'}</div></details></article>`).join('');
  return `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>报销审阅预览 · ${money(total)}元</title><style>
*{box-sizing:border-box}body{margin:0;background:#f1f5f4;color:#183a3b;font:14px/1.6 system-ui,"Microsoft YaHei",sans-serif}main{max-width:1280px;margin:auto;padding:28px}h1{font-size:30px;letter-spacing:-1px;margin:5px 0}h2{font-size:20px;margin:0}h3{margin:3px 0;font-size:17px}.muted,small{color:#627976}.intro{display:flex;justify-content:space-between;gap:20px;align-items:center}.intro strong{font-size:32px;font-variant-numeric:tabular-nums}.pills{display:flex;gap:12px;flex-wrap:wrap;margin:20px 0}.pill{background:white;border:1px solid #d5e3de;padding:12px 18px;border-radius:9px}.pill b{margin-left:15px}.notice{background:#fff8e6;color:#82652e;padding:13px 17px;border-radius:8px;margin:16px 0}nav{display:flex;gap:20px;margin:12px 0 20px}a{color:#136f64;text-underline-offset:3px}.paper{background:white;padding:26px;border:1px solid #d5e3de;border-radius:10px}.table-wrap{overflow:auto;margin-top:18px}.template{width:100%;min-width:1080px;border-collapse:collapse;color:#222;font-size:11px;table-layout:fixed}.template th,.template td{border:1px solid #657b73;padding:5px;white-space:pre-line;overflow-wrap:anywhere;height:29px;font-weight:400}.template thead th{background:#f2f5f3;font-weight:600}.template .form-title{font-size:23px;text-align:center;background:white;padding:14px}.amount{text-align:right;font-variant-numeric:tabular-nums}.filled{background:#edf7f2;font-weight:600!important}.template tfoot td{height:38px}.template .total{font-weight:bold;background:#e8f2ed}.section{margin-top:34px}.expense{border:1px solid #d5e3de;background:#fff;border-radius:10px;margin:14px 0;padding:22px;break-inside:avoid}.expense-head{display:flex;justify-content:space-between;gap:20px}.expense-head strong{white-space:nowrap;font-size:23px}.expense p{margin:7px 0;overflow-wrap:anywhere}.note{white-space:pre-wrap;color:#556f69}.status{color:#86632f;font-size:13px}summary{cursor:pointer;color:#136f64;padding:13px 0;border-top:1px solid #e5ece9;margin-top:14px}.attachments{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:16px}.attachment{min-width:0;background:#f6f8f7;padding:12px;border-radius:6px;overflow-wrap:anywhere}.attachment img{max-width:100%;max-height:450px;object-fit:contain;display:block;margin:14px auto 0}footer{margin:22px 0;color:#627976;font-size:12px}.table-wrap::-webkit-scrollbar{height:8px}.table-wrap::-webkit-scrollbar-thumb{background:#b7cec3;border-radius:4px}@media(max-width:600px){main{padding:16px}.intro,.expense-head{align-items:start;flex-direction:column;gap:6px}h1{font-size:25px}.paper,.expense{padding:16px}.pill{padding:9px 12px}.attachments{grid-template-columns:1fr}}@media print{@page{size:A4 landscape;margin:10mm}body{background:white}main{padding:0;max-width:none}nav,details{display:none}.paper,.expense{border-color:#999;box-shadow:none}.paper{padding:10px}.template{min-width:0;font-size:9px}.template th,.template td{padding:3px;height:23px}.table-wrap{overflow:visible}.section{break-before:page}.notice{background:white;border:1px solid #aaa}.expense{break-inside:avoid}a{color:inherit;text-decoration:none}}
</style></head><body><main><header class="intro"><div><small>后勤工作台 / 审阅草稿</small><h1>报销审阅预览</h1><p class="muted">${esc(meta.scope||'所选事项')} · ${items.length} 笔 · ${esc(period)}</p></div><div><small>本次预览合计</small><br><strong id="previewTotal">¥ ${money(total)}</strong></div></header><div class="pills">${[...categories].map(([c,n])=>`<div class="pill">${esc(c)}<b>¥ ${money(n)}</b></div>`).join('')}</div><p class="notice">这是审阅草稿，包含尚未核对或材料未齐的费用；预览不会提交报销。${projects.includes('待分配项目')?' 本次包含待分配项目的费用。':''}</p><nav><a href="#form">模板汇总</a><a href="#expenses">逐笔费用与凭证</a></nav><section class="paper" id="form"><h2>公司模板汇总</h2><p class="muted">按已核对的《活动运营费用统计表》栏目排版，费用区显示购买内容／用途、“现场现金支付（需报销）”及合计。餐饮归“现场饮用水、夜宵”，锁具相关及罚款保留在“其他”；HTML、Excel和材料清单使用相同汇总规则。</p><div class="table-wrap" tabindex="0" aria-label="模板表格，可横向滚动"><table class="template"><colgroup>${[7,16,9,8,7,7,6,10,3,3,3,7,7].map(n=>`<col style="width:${n}%">`).join('')}</colgroup><thead><tr><th colspan="13" class="form-title">活动运营费用统计表</th></tr><tr><th colspan="3">项目编号：</th><th colspan="3">项目名称：${esc(projects.length===1?projects[0]:'多个项目（详见明细）')}</th><th colspan="7">项目日期（活动日期）：</th></tr><tr><th colspan="13">执行：　班组长：　工厂搭建人数：　工厂撤展人数：</th></tr><tr><th colspan="13">此表格报销区间日期：${esc(period)}　报销人：${esc(meta.person||'待填写')}</th></tr><tr><th rowspan="2">项目</th><th rowspan="2">名称</th><th rowspan="2">细节描述</th><th rowspan="2">实际消费金额</th><th rowspan="2">公司账户付</th><th rowspan="2">公司现金付</th><th rowspan="2">场馆费</th><th rowspan="2">现场现金支付<br>（需报销）</th><th colspan="3">发票</th><th rowspan="2">备注</th><th rowspan="2">相关人员核实签字</th></tr><tr><th>有</th><th>无</th><th>暂时未到</th></tr></thead><tbody>${rows}</tbody><tfoot><tr class="total"><td></td><th colspan="2">费用合计：</th><td></td><td></td><td></td><td></td><td class="amount" data-cell="H35">${money(total)}</td><td></td><td></td><td></td><td></td><td></td></tr><tr><td colspan="3">审核人签字：</td><td colspan="3">财务核对签字：</td><td colspan="3">项目主管签字：</td><td colspan="4">报销人签字：</td></tr></tfoot></table></div><p class="muted">材料超过5项时汇入最后一行；其他费用超过空行时同样汇总。全部逐笔明细保留在下方，附件不另计金额。</p></section><section id="expenses" class="section"><h2>逐笔费用与凭证 · ${items.length} 笔</h2>${details||'<p>当前筛选下没有事项。</p>'}</section><footer>生成时间：${esc(meta.generated||'')}。数据为生成时快照；原始凭证链接需要本机工作台在线。原 Excel 模板未修改。</footer></main></body></html>`;
}

const previewButton=document.createElement('button');
previewButton.id='previewReport';previewButton.type='button';previewButton.textContent='报销预览';
$('checkMail').before(previewButton);
const previewDialog=document.createElement('dialog');previewDialog.id='reportPreviewDialog';
previewDialog.innerHTML=`<div class="dialoghead"><h2>报销审阅预览</h2><button id="closePreview" aria-label="关闭报销预览">×</button></div><div class="actions preview-tools"><label>项目<select id="previewProject"></select></label><label>类别<select id="previewCategory"></select></label><button id="downloadPreview">下载 HTML</button><button id="printPreview">打印预览</button></div><p id="previewScope" class="muted"></p><iframe id="previewFrame" title="报销审阅草稿" sandbox="allow-same-origin allow-popups allow-popups-to-escape-sandbox allow-modals"></iframe>`;
document.body.append(previewDialog);
let previewSnapshot=[],previewMeta={},previewHtml='';
let previewRevision=0;
async function renderReportPreview(){
  previewRevision++;document.getElementById('actualPdf')?.remove();
  const project=$('previewProject').value,category=$('previewCategory').value;
  const items=previewSnapshot.filter(x=>(!project||x.project===project)&&(!category||x.category===category));
  if(items.length){const response=await fetch('/api/report-snapshot?'+new URLSearchParams(items.map(x=>['id',x.id])));if(!response.ok)throw Error('无法读取共用报销快照');const snapshot=await response.json();previewMeta.rows=snapshot.rows;}else previewMeta.rows=[];
  previewHtml=buildReportPreviewHtml(items,previewMeta);
  $('previewFrame').srcdoc=previewHtml;
  $('previewScope').textContent=`${previewMeta.scope}；筛选后 ${items.length} 笔 / ¥ ${money(items.reduce((n,x)=>n+x.amount_cents,0))}。只读快照，修改台账后请重新打开。`;
}
previewButton.onclick=()=>act(async()=>{
  // Fetch directly: load() schedules automatic classification and is not read-only.
  const response=await fetch('/api/state');if(!response.ok)throw Error('无法读取预览数据');
  const snapshot=scopeData(await response.json()),ids=new Set(checked),q=$('search').value.trim().toLowerCase();
  if(ids.size&&[...ids].some(id=>!snapshot.items.some(x=>x.id===id)))throw Error('勾选事项已变化，请刷新列表后重试');
  previewSnapshot=snapshot.items.filter(x=>ids.size?ids.has(x.id):x.stage==='draft'&&(!$('projectFilter').value||x.project===$('projectFilter').value)&&(!$('categoryFilter').value||x.category===$('categoryFilter').value)&&(!q||[x.title,x.merchant,x.order_number,x.id].join(' ').toLowerCase().includes(q)));
  if(!previewSnapshot.length)throw Error('当前范围没有待提交事项；也可先勾选需要审阅的事项。');
  if(!snapshot.report_template_ready)throw Error('工作台尚未保存公司模板，请先设置模板。');
  previewMeta={person:snapshot.report_person||'',roles:snapshot.roles,generated:new Date().toLocaleString('zh-CN'),scope:ids.size?`勾选的 ${ids.size} 笔事项（含隐藏勾选）`:'当前项目、类别及搜索范围内的全部待提交事项'};
  for(const [id,key,label] of [['previewProject','project','所有项目'],['previewCategory','category','所有类别']])$(id).innerHTML=`<option value="">${label}</option>`+[...new Set(previewSnapshot.map(x=>x[key]))].sort().map(v=>`<option value="${esc(v)}">${esc(v)}</option>`).join('');
  await renderReportPreview();previewDialog.showModal();
});
$('previewProject').onchange=()=>act(renderReportPreview);$('previewCategory').onchange=()=>act(renderReportPreview);
$('closePreview').onclick=()=>previewDialog.close();
$('downloadPreview').onclick=()=>{
  const url=URL.createObjectURL(new Blob([previewHtml],{type:'text/html;charset=utf-8'}));
  const a=document.createElement('a');a.href=url;a.download='报销审阅预览.html';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
};
$('printPreview').onclick=()=>{$('previewFrame').contentWindow.focus();$('previewFrame').contentWindow.print();};
if(new URLSearchParams(location.search).get('preview')==='1')queueMicrotask(()=>previewButton.click());
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

const trial=document.createElement('button');trial.textContent='试生成并查看实际打印页';$('previewScope').before(trial);
trial.onclick=()=>act(async()=>{
 const project=$('previewProject').value,category=$('previewCategory').value;
 const ids=previewSnapshot.filter(x=>(!project||x.project===project)&&(!category||x.category===category)).map(x=>x.id);
 const revision=previewRevision;
 trial.disabled=true;
 document.querySelectorAll('#reportPreviewDialog .dialog-message').forEach(x=>x.remove());$('error').hidden=true;
 try{const result=await api('report-preview',{ids,options:{person:previewMeta.person}});
 if(revision!==previewRevision)return;
 let pdf=document.getElementById('actualPdf');if(!pdf){pdf=document.createElement('section');pdf.id='actualPdf';pdf.setAttribute('aria-label','实际PDF打印页');pdf.style.cssText='max-height:70vh;overflow:auto';previewDialog.append(pdf);}
 pdf.replaceChildren();
 const link=document.createElement('a');link.href='/export/'+encodeURIComponent(result.pdf);link.textContent='打开／下载原始PDF打印';link.target='_blank';link.rel='noopener';pdf.append(link);
 for(const [index,name] of (result.preview_pages||[]).entries()){const image=document.createElement('img');image.src='/export/'+encodeURIComponent(name);image.alt=`实际打印页 第${index+1}页`;image.style.cssText='display:block;width:100%;height:auto;margin-top:12px';pdf.append(image);}
 pdf.scrollIntoView();
 }finally{trial.disabled=false;}
});
