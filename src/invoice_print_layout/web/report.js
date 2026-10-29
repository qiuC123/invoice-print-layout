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
  const rowLabels=meta.row_labels||{8:'场地费用(消费)',9:'施工押金(可退)',10:'货车去程运费',11:'货车回程运费',12:'火车、飞机',13:'打的(出租车、滴滴等)',14:'公交(地铁、公交、大巴等)',15:'货拉拉(附行程单、报销凭证)',16:'住宿费(附清单)',17:'员工统一购餐费(附清单)',18:'生活补助费用(附清单)',19:'现场饮用水、夜宵',20:'工厂搭建人工数',21:'工厂撤展人工数',22:'外请人员金额(附清单)',23:'保洁费(附清单)'};
  const sections=meta.report_types?Object.fromEntries(meta.report_types.map(type=>[type.rows[0],[type.label,type.rows.length]])):{8:['拓展',2],10:['车费',6],16:['生活费',4],20:['人员',4],24:['现场购材料',5],29:['其他',6]};
  let rows='';
  for(let r=8;r<=34;r++){
    const group=buckets.get(r)||[],amount=group.reduce((n,x)=>n+x.amount_cents,0);
    const seenRain=new Set();
    const names=meta.rows&&r>=24?(meta.rows.find(row=>row.row===r)?.name?esc(meta.rows.find(row=>row.row===r).name):''):r>=24?(group.length&&group.every(x=>x.category==='顺丰')?'顺丰同城':group.filter(x=>{
      const key=rainKey(x);if(key===null)return true;
      if(seenRain.has(key))return false;seenRain.add(key);return true;
    }).map(x=>{
      const name=itemName(x);
      return `<span title="${esc(x.title)}">${esc(name)}</span>`;
    }).join('<br>')):'';
    rows+=`<tr>${sections[r]?`<th rowspan="${sections[r][1]}">${sections[r][0]}</th>`:''}<th data-cell="B${r}">${esc(rowLabels[r]||(r>=29?(r-28)+'、':(r-23)+'、'))}${names}</th><td></td><td></td><td></td><td></td><td></td><td class="amount ${group.length?'filled':''}" data-cell="H${r}">${group.length?`<span title="${esc(group.map(x=>x.title).join('；'))}">${money(amount)}</span>`:''}</td><td></td><td></td><td></td><td></td><td></td></tr>`;
  }
  const stage=x=>x.stage==='draft'?(x.ready&&x.missing_acknowledged?'已核对 · 已确认缺材料':!x.complete?'待核对 · 仍缺材料':!x.verified?'待核对':'已核对'):({'submitted':'已核对 · 已生成报销包','reimbursed':'已核对 · 历史记录','cancelled':'已取消'}[x.stage]||x.stage);
  const fileBase=new URL('/file/',location.origin).href;
  // Use the same template order as the PDF and Excel without changing the frozen
  // selection or its request-cache key. Older snapshots keep their input order.
  const itemOrder=new Map((meta.ordered_item_ids||[]).map((id,index)=>[id,index]));
  const detailItems=[...items].sort((a,b)=>(itemOrder.get(a.id)??items.length)-(itemOrder.get(b.id)??items.length));
  const details=detailItems.map((x,i)=>`<article class="expense" id="expense-${i}"><div class="expense-head"><div><small>${i+1} / ${esc(x.category)} · ${esc(x.expense_date||'日期待补')} · ${esc(x.project||'待分配项目')}</small><h3>${esc(x.title)}</h3></div><strong>¥ ${money(x.amount_cents)}</strong></div><p class="status">${esc(stage(x))}${x.missing?.length?' · 缺：'+esc(x.missing.join('、')):''}</p><p>${esc(x.merchant||'商家未填写')}</p>${x.order_number?`<p class="muted">交易／订单号：${esc(x.order_number)}</p>`:''}<p class="note">${esc(x.note||'')}</p><details><summary>查看 ${x.attachments.length} 份凭证</summary><div class="attachments">${x.attachments.map(a=>{
      const url=fileBase+encodeURIComponent(a.id),label=(meta.roles||{})[a.role]||a.role;
      return `<div class="attachment"><p>${esc(label)}</p>${a.available?`<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(a.name)}</a>${/\.(jpg|jpeg|png|webp)$/i.test(a.name)?`<a href="${esc(url)}" target="_blank" rel="noopener noreferrer"><img loading="lazy" decoding="async" ${meta.defer_images?'data-preview-src':'src'}="${esc(url)}" alt="${esc(a.name)}"></a>`:'<p class="muted">点击文件名查看 PDF 原件</p>'}`:`<p>文件缺失：${esc(a.name)}</p>`}</div>`;
    }).join('')||'<p class="muted">暂无附件</p>'}</div></details></article>`).join('');
  return `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>报销审阅预览 · ${money(total)}元</title><style>
*{box-sizing:border-box}body{margin:0;background:#f1f5f4;color:#183a3b;font:14px/1.6 system-ui,"Microsoft YaHei",sans-serif}main{max-width:1280px;margin:auto;padding:28px}h1{font-size:30px;letter-spacing:-1px;margin:5px 0}h2{font-size:20px;margin:0}h3{margin:3px 0;font-size:17px}.muted,small{color:#627976}.intro{display:flex;justify-content:space-between;gap:20px;align-items:center}.intro strong{font-size:32px;font-variant-numeric:tabular-nums}.pills{display:flex;gap:12px;flex-wrap:wrap;margin:20px 0}.pill{background:white;border:1px solid #d5e3de;padding:12px 18px;border-radius:9px}.pill b{margin-left:15px}.notice{background:#fff8e6;color:#82652e;padding:13px 17px;border-radius:8px;margin:16px 0}nav{display:flex;gap:20px;margin:12px 0 20px}a{color:#136f64;text-underline-offset:3px}.paper{background:white;padding:26px;border:1px solid #d5e3de;border-radius:10px}.table-wrap{overflow:auto;margin-top:18px}.template{width:100%;min-width:1080px;border-collapse:collapse;color:#222;font-size:11px;table-layout:fixed}.template th,.template td{border:1px solid #657b73;padding:5px;white-space:pre-line;overflow-wrap:anywhere;height:29px;font-weight:400}.template thead th{background:#f2f5f3;font-weight:600}.template .form-title{font-size:23px;text-align:center;background:white;padding:14px}.amount{text-align:right;font-variant-numeric:tabular-nums}.filled{background:#edf7f2;font-weight:600!important}.template tfoot td{height:38px}.template .total{font-weight:bold;background:#e8f2ed}.section{margin-top:34px}.expense{border:1px solid #d5e3de;background:#fff;border-radius:10px;margin:14px 0;padding:22px;break-inside:avoid}.expense-head{display:flex;justify-content:space-between;gap:20px}.expense-head strong{white-space:nowrap;font-size:23px}.expense p{margin:7px 0;overflow-wrap:anywhere}.note{white-space:pre-wrap;color:#556f69}.status{color:#86632f;font-size:13px}summary{cursor:pointer;color:#136f64;padding:13px 0;border-top:1px solid #e5ece9;margin-top:14px}.attachments{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:16px}.attachment{min-width:0;background:#f6f8f7;padding:12px;border-radius:6px;overflow-wrap:anywhere}.attachment img{max-width:100%;max-height:450px;object-fit:contain;display:block;margin:14px auto 0}footer{margin:22px 0;color:#627976;font-size:12px}.table-wrap::-webkit-scrollbar{height:8px}.table-wrap::-webkit-scrollbar-thumb{background:#b7cec3;border-radius:4px}@media(max-width:600px){main{padding:16px}.intro,.expense-head{align-items:start;flex-direction:column;gap:6px}h1{font-size:25px}.paper,.expense{padding:16px}.pill{padding:9px 12px}.attachments{grid-template-columns:1fr}}@media print{@page{size:A4 landscape;margin:10mm}body{background:white}main{padding:0;max-width:none}nav,details{display:none}.paper,.expense{border-color:#999;box-shadow:none}.paper{padding:10px}.template{min-width:0;font-size:9px}.template th,.template td{padding:3px;height:23px}.table-wrap{overflow:visible}.section{break-before:page}.notice{background:white;border:1px solid #aaa}.expense{break-inside:avoid}a{color:inherit;text-decoration:none}}
</style></head><body><main><header class="intro"><div><small>后勤工作台 / 审阅草稿</small><h1>报销审阅预览</h1><p class="muted">${esc(meta.scope||'所选事项')} · ${items.length} 笔 · ${esc(period)}</p></div><div><small>本次预览合计</small><br><strong id="previewTotal">¥ ${money(total)}</strong></div></header><div class="pills">${[...categories].map(([c,n])=>`<div class="pill">${esc(c)}<b>¥ ${money(n)}</b></div>`).join('')}</div><p class="notice">这是审阅草稿，包含尚未核对或材料未齐的费用；预览不会提交报销。${projects.includes('待分配项目')?' 本次包含待分配项目的费用。':''}</p><nav aria-label="预览内容"><span>模板汇总</span><span>逐笔费用与凭证</span></nav><section class="paper" id="form"><h2>公司模板汇总</h2><p class="muted">按已核对的《活动运营费用统计表》栏目排版，费用区显示购买内容／用途、“现场现金支付（需报销）”及合计。餐饮归“现场饮用水、夜宵”，锁具相关及罚款保留在“其他”；HTML、Excel和材料清单使用相同汇总规则。</p><div class="table-wrap" tabindex="0" aria-label="模板表格，可横向滚动"><table class="template"><colgroup>${[7,16,9,8,7,7,6,10,3,3,3,7,7].map(n=>`<col style="width:${n}%">`).join('')}</colgroup><thead><tr><th colspan="13" class="form-title">活动运营费用统计表</th></tr><tr><th colspan="3">项目编号：</th><th colspan="3">项目名称：${esc(projects.length===1?projects[0]:'多个项目（详见明细）')}</th><th colspan="7">项目日期（活动日期）：</th></tr><tr><th colspan="13">执行：　班组长：　工厂搭建人数：　工厂撤展人数：</th></tr><tr><th colspan="13">此表格报销区间日期：${esc(period)}　报销人：${esc(meta.person||'待填写')}</th></tr><tr><th rowspan="2">项目</th><th rowspan="2">名称</th><th rowspan="2">细节描述</th><th rowspan="2">实际消费金额</th><th rowspan="2">公司账户付</th><th rowspan="2">公司现金付</th><th rowspan="2">场馆费</th><th rowspan="2">现场现金支付<br>（需报销）</th><th colspan="3">发票</th><th rowspan="2">备注</th><th rowspan="2">相关人员核实签字</th></tr><tr><th>有</th><th>无</th><th>暂时未到</th></tr></thead><tbody>${rows}</tbody><tfoot><tr class="total"><td></td><th colspan="2">费用合计：</th><td></td><td></td><td></td><td></td><td class="amount" data-cell="H35">${money(total)}</td><td></td><td></td><td></td><td></td><td></td></tr><tr><td colspan="3">审核人签字：</td><td colspan="3">财务核对签字：</td><td colspan="3">项目主管签字：</td><td colspan="4">报销人签字：</td></tr></tfoot></table></div><p class="muted">材料超过5项时汇入最后一行；其他费用超过空行时同样汇总。全部逐笔明细保留在下方，附件不另计金额。</p></section><section id="expenses" class="section"><h2>逐笔费用与凭证 · ${items.length} 笔</h2>${details||'<p>当前筛选下没有事项。</p>'}</section><footer>生成时间：${esc(meta.generated||'')}。数据为生成时快照；原始凭证链接需要本机工作台在线。原 Excel 模板未修改。</footer></main></body></html>`;
}

// The screen and print action use the same composed PDF. Original-file links and
// ledger descriptions are not substitutes for the final A4 pages.
function buildFinalReportPreviewHtml(pages,meta={}){
  const theme=meta.theme==='dark'?'dark':'light';
  const figures=pages.map((source,index)=>{
    const size=meta.page_sizes?.[index]||{},landscape=Number(size.width)>Number(size.height);
    const width=Number.isFinite(size.width)&&size.width>0?size.width:(landscape?842:595);
    const height=Number.isFinite(size.height)&&size.height>0?size.height:(landscape?595:842);
    const sourceAttribute=meta.defer_pages&&index>1?'data-page-src':'src';
    return `<figure class="print-page ${landscape?'landscape':'portrait'}" data-page="${index+1}"><img width="${Math.round(width)}" height="${Math.round(height)}" style="aspect-ratio:${width}/${height}" ${index?'loading="lazy" ':''}decoding="async" ${sourceAttribute}="${esc(source)}" alt="最终打印页 第${index+1}页"><figcaption>第 ${index+1} / ${pages.length} 页</figcaption></figure>`;
  }).join('');
  return `<!doctype html><html lang="zh-CN" data-theme="${theme}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>报销打印页</title><style>
*{box-sizing:border-box}html{color-scheme:light;--surface:#f7f8fa;--muted:#5f6672;--line:#dee4ed}html[data-theme=dark]{color-scheme:dark;--surface:#292929;--muted:#b8b8b8;--line:#404040}body{margin:0;background:var(--surface);font:12px/20px "Segoe UI","Microsoft YaHei",sans-serif;color:var(--muted)}main{max-width:1100px;margin:0 auto;padding:20px}.print-page{max-width:794px;margin:0 auto 20px}.print-page.landscape{max-width:1100px}.print-page img{display:block;width:100%;height:auto;background:#fff;color-scheme:light;box-shadow:0 2px 8px #00000012}.print-page figcaption{text-align:center;margin-top:8px}a{color:inherit}@media(max-width:600px){main{padding:12px}.print-page{margin-bottom:16px}}@media print{@page portrait{size:A4 portrait;margin:0}@page landscape{size:A4 landscape;margin:0}html,body{background:#fff;color:#000}main{max-width:none;padding:0}.print-page{margin:0;max-width:none;break-after:page;break-inside:avoid}.print-page:last-child{break-after:auto}.print-page.portrait{page:portrait;width:210mm;height:297mm}.print-page.landscape{page:landscape;width:297mm;height:210mm}.print-page img{width:100%;height:100%;object-fit:contain;box-shadow:none}.print-page figcaption{display:none}}
</style></head><body><main id="actualPdf" aria-label="最终PDF打印页">${figures}</main></body></html>`;
}
const previewButton=document.createElement('button');
previewButton.id='previewReport';previewButton.type='button';previewButton.textContent='报销预览';
const previewActions=document.getElementById('workbenchActions'),previewAnchor=document.getElementById('themeSelect');
previewActions.insertBefore(previewButton,previewAnchor?.parentElement===previewActions?previewAnchor:null);
const previewDialog=document.createElement('dialog');previewDialog.id='reportPreviewDialog';
previewDialog.innerHTML=`<div class="dialoghead"><h2>报销打印预览</h2><button id="closePreview" aria-label="关闭报销预览" class="dialog-close" type="button">×</button></div><div class="preview-tools"><div class="preview-filter"><span class="preview-filter-label">类别</span><select id="previewCategory" aria-label="报销预览类别筛选"></select></div><div class="preview-tool-actions"><button id="downloadPreview" type="button">下载 HTML</button><button id="previewTrial" type="button">刷新打印页</button><button id="printPreview" type="button" class="primary">打印预览</button></div></div><p id="previewScope" class="muted"></p><div id="previewLoadState" hidden></div><iframe id="previewFrame" title="报销最终打印页" sandbox="allow-same-origin"></iframe>`;
document.body.append(previewDialog);
window.invoiceMultiFilters.attach('previewCategory');
let previewSnapshot=[],previewMeta={},previewHtml='',previewResult=null;
let previewRevision=0,previewSnapshotRequests=new Map();
let previewSession=0,previewLoading=false,previewRendering=false,previewDownloadBusy=false;
let previewPageObserver=null;
function clearPreviewPageLoader(){
  previewPageObserver?.disconnect();previewPageObserver=null;$('previewFrame').onload=null;
}
function previewReadControls(disabled){
  for(const control of previewDialog.querySelectorAll('.preview-tools select'))if(control.disabled!==disabled){control.disabled=disabled;window.invoiceMultiFilters.refresh(control.id);}
  for(const id of ['downloadPreview','printPreview'])$(id).disabled=disabled||previewRendering||!previewResult||previewDownloadBusy;
  $('previewTrial').disabled=disabled||previewRendering;
}
function selectedPreviewItems(){
  const categories=window.invoiceMultiFilters.values('previewCategory');
  return previewSnapshot.filter(x=>!categories.length||categories.includes(x.category));
}
function previewPageUrl(name){return new URL('/export/'+encodeURIComponent(name),location.origin).href;}
async function requestReportPrintPreview(ids){
  // Opening a preview is allowed while the main list is still loading. Use the
  // token from this dialog's fresh read rather than the main list's older state.
  if(!previewMeta.project_id||previewMeta.project_id!==projectScope)throw Error('项目已变化，请重新打开当前项目的报销预览。');
  const response=await fetch('/api/report-preview',{method:'POST',headers:{'Content-Type':'application/json','X-Workbench-Token':previewMeta.token},body:JSON.stringify({ids,options:{person:previewMeta.person}})});
  const result=await response.json();if(!response.ok)throw Error(result.error||'无法生成最终打印页');return result;
}
function renderFinalPreviewFrame(){
  if(!previewResult)return;
  clearPreviewPageLoader();
  const result=previewResult,serial=previewSession,revision=previewRevision,frame=$('previewFrame');
  previewHtml=buildFinalReportPreviewHtml(previewResult.combined_preview_pages.map(previewPageUrl),{
    theme:document.documentElement.dataset.theme,page_sizes:previewResult.combined_page_sizes,defer_pages:true});
  // The sandboxed document has no scripts. Load nearby pages from the parent
  // rather than relying on native lazy loading inside that document.
  frame.onload=()=>{
    if(serial!==previewSession||revision!==previewRevision||result!==previewResult||!previewDialog.open)return;
    const doc=frame.contentDocument,images=[...doc.querySelectorAll('img[data-page-src]')];
    const load=image=>{image.src=image.dataset.pageSrc;delete image.dataset.pageSrc;};
    if(typeof IntersectionObserver!=='function'){images.forEach(load);return;}
    previewPageObserver=new IntersectionObserver(entries=>{
      if(serial!==previewSession||revision!==previewRevision||result!==previewResult||!previewDialog.open)return;
      for(const entry of entries)if(entry.isIntersecting){load(entry.target);previewPageObserver?.unobserve(entry.target);}
    },{root:doc,rootMargin:'1000px 0px'});
    images.forEach(image=>previewPageObserver.observe(image));
  };
  frame.srcdoc=previewHtml;
}
new MutationObserver(()=>{
  const root=$('previewFrame').contentDocument?.documentElement;
  if(previewDialog.open&&root)root.dataset.theme=document.documentElement.dataset.theme==='dark'?'dark':'light';
}).observe(document.documentElement,{attributes:true,attributeFilter:['data-theme']});
async function renderReportPreview({refresh=false}={}){
  const serial=previewSession,revision=++previewRevision;
  const current=()=>serial===previewSession&&revision===previewRevision&&previewDialog.open;
  const items=selectedPreviewItems(),requests=previewSnapshotRequests,key=JSON.stringify(items.map(x=>x.id));
  previewResult=null;previewHtml='';previewRendering=true;
  clearPreviewPageLoader();
  $('previewFrame').hidden=true;$('previewFrame').srcdoc='';
  const loading=$('previewLoadState');loading.hidden=false;loading.innerHTML=dialogLoadingMarkup('正在生成最终报销表和材料打印页…');
  $('previewScope').textContent=`${previewMeta.scope}；当前 ${items.length} 笔 / ¥ ${money(items.reduce((n,x)=>n+x.amount_cents,0))}。`;
  previewDialog.setAttribute('aria-busy','true');previewReadControls(previewLoading);
  let request;
  try{
    if(!items.length)throw Error('当前筛选没有费用，请选择其他类别。');
    if(refresh)requests.delete(key);
    request=requests.get(key);
    if(!request){request=requestReportPrintPreview(items.map(x=>x.id));requests.set(key,request);}
    const result=await request;
    if(!current())return false;
    if(!result.combined_pdf||!Array.isArray(result.combined_preview_pages)||!result.combined_preview_pages.length)throw Error('最终打印文件尚未生成，请刷新打印页重试。');
    previewResult=result;renderFinalPreviewFrame();
    loading.hidden=true;$('previewFrame').hidden=false;
    $('previewScope').textContent+=` 共 ${result.combined_preview_pages.length} 页；表格及材料顺序与 PDF 一致。`;
    return true;
  }catch(error){
    if(requests.get(key)===request)requests.delete(key);
    if(!current())return false;
    loading.innerHTML=dialogLoadErrorMarkup(error.message);
    loading.querySelector('[data-dialog-retry]').onclick=()=>renderReportPreview({refresh:true});
    return false;
  }finally{
    if(current()){previewRendering=false;previewDialog.setAttribute('aria-busy','false');previewReadControls(previewLoading);}
  }
}
async function readReportPreview(serial,explicitIds=null){
  const current=()=>serial===previewSession&&previewDialog.open;
  if(!current()||previewLoading)return;
  previewLoading=true;previewRevision++;previewSnapshotRequests=new Map();
  previewSnapshot=[];previewMeta={};previewHtml='';previewResult=null;
  previewDialog.querySelectorAll('.dialog-message').forEach(node=>node.remove());
  $('previewFrame').hidden=true;$('previewFrame').srcdoc='';$('previewScope').textContent='';
  $('previewCategory').innerHTML='<option value="">所有类别</option>';
  window.invoiceMultiFilters.set('previewCategory',[]);
  const loading=$('previewLoadState');loading.hidden=false;loading.innerHTML=dialogLoadingMarkup('正在读取报销预览…');
  previewDialog.setAttribute('aria-busy','true');previewReadControls(true);
  try{
    const projectId=projectScope;
    if(!projectId)throw Error('请先从侧栏选择一个项目，再打开报销预览。');
    // Fetch directly: load() schedules automatic classification and is not read-only.
    const response=await fetch('/api/state');if(!response.ok)throw Error('无法读取预览数据');
    const source=await response.json();if(!current())return;
    const snapshot=scopeData(source),ids=new Set(explicitIds||checked);
    if(ids.size&&[...ids].some(id=>!source.items.some(x=>x.id===id)))throw Error('勾选事项已变化，请刷新列表后重试');
    if(ids.size&&[...ids].some(id=>!snapshot.items.some(x=>x.id===id&&x.project_id===projectId)))throw Error('报销预览只包含当前项目，请取消其他项目的勾选后重试。');
    previewSnapshot=snapshot.items.filter(x=>x.project_id===projectId&&!x.deleted&&(ids.size?ids.has(x.id):x.stage==='draft'&&matchesExpenseFilters(x)));
    if(!previewSnapshot.length)throw Error('当前范围没有待提交事项；也可先勾选需要预览的事项。');
    if(!snapshot.report_template_ready)throw Error('工作台尚未保存公司模板，请先设置模板。');
    const projectName=snapshot.projects.find(project=>project.id===projectId).name;
    previewMeta={project_id:projectId,person:snapshot.report_person||'',token:snapshot.token,scope:`${projectName} · `+(explicitIds?`本次智能整理的 ${ids.size} 笔事项`:ids.size?`勾选的 ${ids.size} 笔事项`:'当前项目、人员、类别、问题及搜索范围内的全部待提交事项')};
    $('previewCategory').innerHTML='<option value="">所有类别</option>'+[...new Set(previewSnapshot.map(x=>x.category))].sort().map(v=>`<option value="${esc(v)}">${esc(v)}</option>`).join('');
    window.invoiceMultiFilters.set('previewCategory',[]);
    previewLoading=false;await renderReportPreview();
  }catch(error){
    if(!current())return;
    loading.innerHTML=dialogLoadErrorMarkup(error.message);
    loading.querySelector('[data-dialog-retry]').onclick=()=>readReportPreview(serial,explicitIds);
  }finally{if(current()){previewLoading=false;previewDialog.setAttribute('aria-busy',String(previewRendering));previewReadControls(false);}}
}
previewButton.onclick=()=>{
  if(previewDialog.open)return;
  const serial=++previewSession;window.invoiceMultiFilters.closeAll();previewDialog.showModal();return readReportPreview(serial);
};
window.openAgentReportPreview=({projectId,itemIds})=>{
  if(!projectId||projectId!==projectScope)throw Error('项目已变化，请重新打开当前项目的整理结果。');
  if(!Array.isArray(itemIds)||!itemIds.length||itemIds.some(id=>typeof id!=='string'||!id))throw Error('本次整理没有可预览的费用。');
  if(previewDialog.open)throw Error('请先关闭当前报销预览，再查看本次整理结果。');
  const serial=++previewSession;window.invoiceMultiFilters.closeAll();previewDialog.showModal();
  return readReportPreview(serial,[...new Set(itemIds)]);
};
$('previewCategory').onchange=()=>renderReportPreview();
function dismissReportPreview(){previewSession++;previewRevision++;previewLoading=false;previewRendering=false;previewResult=null;previewSnapshotRequests=new Map();clearPreviewPageLoader();previewDialog.setAttribute('aria-busy','false');window.invoiceMultiFilters.closeAll();previewDialog.close();}
$('closePreview').onclick=dismissReportPreview;
previewDialog.addEventListener('cancel',event=>{event.preventDefault();dismissReportPreview();});
previewDialog.addEventListener('close',()=>{if(!previewDialog.open){previewSession++;previewRevision++;previewLoading=false;previewRendering=false;previewResult=null;previewSnapshotRequests=new Map();clearPreviewPageLoader();window.invoiceMultiFilters.closeAll();}});
function previewImageDataUrl(blob){return new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(reader.result);reader.onerror=()=>reject(Error('无法读取打印页图片'));reader.readAsDataURL(blob);});}
$('downloadPreview').onclick=()=>act(async()=>{
  if(previewLoading||previewRendering||!previewResult||previewDownloadBusy)return;
  const serial=previewSession,revision=previewRevision,result=previewResult;previewDownloadBusy=true;previewReadControls(false);
  try{
    // Downloads remain useful offline; they embed the same page images as the PDF preview.
    const sources=await Promise.all(result.combined_preview_pages.map(async name=>{
      const response=await fetch(previewPageUrl(name));if(!response.ok)throw Error('无法下载打印页，请重试');
      return previewImageDataUrl(await response.blob());
    }));
    if(serial!==previewSession||revision!==previewRevision||!previewDialog.open)return;
    const html=buildFinalReportPreviewHtml(sources,{theme:'light',page_sizes:result.combined_page_sizes});
    const url=URL.createObjectURL(new Blob([html],{type:'text/html;charset=utf-8'}));
    const a=document.createElement('a');a.href=url;a.download='报销打印预览.html';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
  }finally{previewDownloadBusy=false;if(serial===previewSession)previewReadControls(previewLoading);}
});
$('printPreview').onclick=()=>{
  if(previewLoading||previewRendering||!previewResult)return;
  window.open(previewPageUrl(previewResult.combined_pdf),'_blank','noopener');
};
$('previewTrial').onclick=()=>renderReportPreview({refresh:true});
if(new URLSearchParams(location.search).get('preview')==='1')queueMicrotask(()=>previewButton.click());
const reportDialog=document.createElement('dialog');
reportDialog.id='reportDialog';
reportDialog.innerHTML=`<div class="dialoghead"><h2>制作报销包</h2><button id="closeReport" aria-label="关闭报销制作" class="dialog-close" type="button">×</button></div>
<p class="muted">生成报销表与材料打印文件。打印请选择 A4、1×1。</p>
<p id="reportScope"></p><p id="reportTemplateState" class="muted"></p>
<label>设置／更换公司模板（XLSX，最多5MB）<input id="reportTemplateFile" type="file" accept=".xlsx"></label>
<form id="reportForm"><div class="formgrid"><label>报销人<input name="person" maxlength="80"></label><label>报销期间（不填写则按所选事项日期）<input name="period" maxlength="120"></label>
</div>
<p class="muted">主表费用区仅填写“现场现金支付（需报销）”列及合计，其他费用栏留空。活动日期、审核签字等未知内容留空。制作成功后，报销包保留在“已核对”供查看与下载；失败保持原状态。</p>
<div class="actions"><button type="submit" id="createReport" class="primary">开始制作 PDF＋MD＋Excel</button></div></form>
<p id="reportProgress" role="status"></p><p id="reportError" role="alert" hidden></p><div id="reportDownloads" class="actions"></div>`;
document.body.append(reportDialog);
let reportIds=[],reportBusy=false;
$('exportSelected').textContent='制作报销包';
function reportLock(busy){reportBusy=busy;reportDialog.querySelectorAll('button,input,select').forEach(x=>x.disabled=busy);$('createReport').disabled=busy||!state.report_template_ready;}
function reportFail(error){$('reportError').textContent=error.message;$('reportError').hidden=false;}
$('exportSelected').onclick=()=>act(async()=>{
  reportIds=reportSelectionIds();await load();reportSelectionIds(reportIds);
  const visible=new Set(base().filter(x=>matches(x,view)).map(x=>x.id));
  const hidden=reportIds.filter(id=>!visible.has(id)).length;
  $('reportScope').textContent=`已选择 ${reportIds.length} 笔${hidden?`（其中 ${hidden} 笔不在当前列表）`:''}；合计 ¥${money(reportIds.reduce((s,id)=>s+state.items.find(x=>x.id===id).amount_cents,0))}。`;
  const confirmedMissing=reportIds.map(id=>state.items.find(x=>x.id===id)).filter(x=>x.missing_acknowledged);
  if(confirmedMissing.length)$('reportScope').textContent+=` 其中 ${confirmedMissing.length} 笔已确认仍缺材料，打印仅包含已收到的原件，缺项保留在清单和报销明细中。`;
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
  $('reportProgress').textContent='正在制作报销包…';
  try{
    reportSelectionIds(reportIds);
    const result=await api('report',{ids:reportIds,options});
    for(const [key,label] of [['pdf','下载总 PDF'],['md','下载 MD 清单'],['xlsx','下载 Excel 报销表']]){
      const a=document.createElement('a');a.href='/export/'+encodeURIComponent(result[key]);a.textContent=label;a.target='_blank';a.rel='noopener';$('reportDownloads').append(a);
    }
    $('reportProgress').textContent='三份文件已生成，报销包已保留在“已核对”。PDF请选择A4、1×1打印。';
    $('notice').replaceChildren();notify('报销包已生成（PDF＋MD＋Excel），可在“已核对”查看最终打印页。');
    for(const a of $('reportDownloads').children)$('notice').append(a.cloneNode(true));
    for(const id of reportIds)checked.delete(id);
    try{await load();}catch(error){$('reportProgress').textContent+=' 列表刷新失败，请刷新页面查看“已核对”中的报销包；文件已生成，无需重复制作。';}
  }catch(error){$('reportProgress').textContent='制作未完成，请按提示检查；原件与事项状态保留。';reportFail(error);}
  finally{reportLock(false);}
};
