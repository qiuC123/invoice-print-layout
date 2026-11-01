'use strict';
const receiptRender=render,receiptThumbs=new Map();
const emptyInbox=()=>({rows:[],paired:false,catalog:{projects:[],categories},needs_pairing:false});
render=function(){
 $('receiptPage').hidden=tab!=='receipts';
 if(tab!=='receipts'){receiptRender();return;}
 document.body.dataset.screen='receipts';$('settingsPage').hidden=true;
 document.querySelectorAll('nav button').forEach(b=>b.classList.toggle('active',b.dataset.tab==='receipts'));
 const inbox=state.receipt_inbox||emptyInbox(),rows=inbox.rows;
 const status=r=>r.status==='received'?'已到电脑':r.status==='reselect'?'需要重新选择':r.status==='failed'?'上传失败':'待上传';
 $('receiptPage').innerHTML=`<div class="receipt-header"><span class="mark">票</span><div><h1 id="receiptTitle">收票箱</h1><p>纸质票拍下来，确认后安心存手机</p></div></div><button id="receiptCamera" class="receipt-camera">＋ 拍照收票</button><p class="fine">手机本地识别 · 没网也能收 · 连上电脑自动上传</p><div class="receipt-connection"><span>${escape(inbox.needs_pairing?'配对失效，请重新连接':inbox.paired?inbox.server_name:'还未连接电脑')}</span><button id="receiptConnect" class="subtle">连接电脑</button></div>${inbox.connection_error?`<p class="fine">${escape(inbox.connection_error)}</p>`:''}<div class="section-head"><h2>最近30天</h2><button id="receiptRetryAll" class="subtle">重试上传</button></div><div id="receiptRows">${rows.map(r=>`<article class="card" data-receipt="${escape(r.client_id)}"><div class="card-top"><img class="receipt-thumb" data-receipt-thumb="${escape(r.client_id)}" alt="拍摄的票据"><div class="receipt-copy"><h3>${r.amount_cents===null?'金额待确认':'¥'+money(r.amount_cents)}</h3><span class="tag">${escape(r.category)}</span><p>${escape(inbox.catalog.projects.find(p=>p.id===r.project_id)?.name||r.project_name||(!r.project_id?'项目待选择':'原项目已不在目录'))}</p><span class="receipt-status ${r.status==='received'?'received':''}">${status(r)}</span></div></div><p>${escape(r.captured_at.replace('T',' '))}</p>${r.error?`<p class="warning">${escape(r.error)}</p>`:''}${r.status!=='received'?`<div class="card-controls"><button class="secondary" data-receipt-edit="${escape(r.client_id)}">${r.status==='reselect'?'重新选择':'修改'}</button><button class="secondary" data-receipt-retry="${escape(r.client_id)}">重试</button><button class="secondary" data-receipt-delete="${escape(r.client_id)}">删除</button></div>`:''}</article>`).join('')||empty('还没有收票','拍一张发票或收据，确认金额、类别、项目和所属人。')}</div>`;
 $('receiptCamera').onclick=()=>run(async()=>{const capture=await api('receiptCamera');if(capture)receiptConfirm(capture);});
 $('receiptConnect').onclick=()=>{setTab('settings');receiptPairDialog();};
 const retry=()=>run(async()=>{await api('receiptSync');toast('已尝试连接电脑');});$('receiptRetryAll').onclick=retry;
 document.querySelectorAll('[data-receipt-retry]').forEach(b=>b.onclick=()=>{const row=rows.find(r=>r.client_id===b.dataset.receiptRetry);row.status==='reselect'?receiptEdit(row):retry();});
 document.querySelectorAll('[data-receipt-edit]').forEach(b=>b.onclick=()=>receiptEdit(rows.find(r=>r.client_id===b.dataset.receiptEdit)));
 document.querySelectorAll('[data-receipt-delete]').forEach(b=>b.onclick=()=>dialog('删除未上传票据','<p>从手机收票箱删除这张票据和照片？</p>','删除',()=>api('receiptDelete',{id:b.dataset.receiptDelete})));
 document.querySelectorAll('[data-receipt-thumb]').forEach(async img=>{const id=img.dataset.receiptThumb;try{const src=receiptThumbs.get(id)||await api('receiptThumbnail',{id});receiptThumbs.set(id,src);if(img.isConnected)img.src=src;}catch(e){img.alt='照片暂无法预览';}});
};
function receiptFields(defaults,values={}){
 const c=defaults.catalog,projectId=values.project_id??defaults.project_id;
 return `${field('amount','金额（元，可留空）',values.amount||'')}<p class="fine" id="receiptOcrStatus">${values.edit?'请对照照片确认':'正在手机本地识别，也可先手工填写'}</p>${selectField('category','类别',c.categories.map(v=>[v,v]),values.category||defaults.category)}${selectField('project_id','项目',c.projects.length?c.projects.map(p=>[p.id,p.name]):[['','先连接电脑']],projectId)}${c.projects.length?'':'<p class="warning">先连接电脑获取项目；现在仍可保存，之后再选项目。</p>'}<div id="receiptPeople"></div><label class="field">备注（选填）<input name="note" aria-label="备注（选填）" maxlength="200" value="${escape(values.note||'')}"></label>`;
}
function receiptPeople(defaults,initial){
 const form=$('dialogForm');
 const update=(person)=>{const p=defaults.catalog.projects.find(p=>p.id===form.elements.project_id.value);$('receiptPeople').innerHTML=selectField('person_id','所属人',[['','暂不填写'],...(p?.people||[]).map(v=>[v.id,v.label])],person??defaults.people_defaults[p?.id]??'');};
 form.elements.project_id.onchange=()=>update();update(initial);
}
function receiptConfirm(capture){
 let closed=false,saved=false,amountTouched=false,categoryTouched=false;
 dialog('确认收票',`<img class="receipt-confirm-photo" src="${escape(capture.thumbnail||'')}" alt="本次拍摄的票据">${receiptFields(capture.defaults)}`,'存到收票箱',async form=>{await api('receiptSave',{...Object.fromEntries(form),token:capture.token,captured_at:capture.captured_at});saved=true;toast('已存到手机，连上电脑后自动上传');});
 const form=$('dialogForm');receiptPeople(capture.defaults);
 form.elements.amount.addEventListener('input',()=>amountTouched=true);form.elements.category.addEventListener('change',()=>categoryTouched=true);
 const poll=setInterval(async()=>{try{const result=await api('receiptOcr',{token:capture.token});if(closed||!result.done||!$('dialog').open)return;if(!amountTouched&&result.amount)form.elements.amount.value=result.amount;if(!categoryTouched&&result.category&&result.category!=='其他'&&capture.defaults.catalog.categories.includes(result.category))form.elements.category.value=result.category;$('receiptOcrStatus').textContent=result.warning||'本地识别完成，请确认后保存';clearInterval(poll);}catch(e){clearInterval(poll);}},600);
 $('dialog').addEventListener('close',()=>{closed=true;clearInterval(poll);if(!saved)api('receiptCancel',{token:capture.token}).catch(e=>toast(e.message));},{once:true});
}
async function receiptEdit(row){try{const defaults=await api('receiptDefaults',{captured_at:row.captured_at});const p=defaults.catalog.projects.find(p=>p.id===row.project_id);const person=p?.people.some(v=>v.id===row.person_id)?row.person_id:undefined;dialog('重新确认收票',receiptFields(defaults,{...row,edit:true,project_id:p?row.project_id:defaults.project_id,amount:row.amount_cents===null?'':money(row.amount_cents)}),'保存并重传',form=>api('receiptEdit',{id:row.client_id,...Object.fromEntries(form)}));receiptPeople(defaults,person);}catch(e){showError(e.message);}}
function receiptPairDialog(){
 const inbox=state.receipt_inbox||emptyInbox();
 dialog('连接电脑收票',`<p>在电脑「手机连接 → 收票 App」生成配对码。</p>${inbox.paired?`<div class="card"><h3>${escape(inbox.server_name)}</h3><p>最近连上：${escape(inbox.last_connected||'尚未连接')}</p>${inbox.needs_pairing?'<p class="warning">配对已失效，请重新配对。</p>':''}<div class="small-actions"><button type="button" class="secondary" id="receiptUnpair">解除配对</button><button type="button" class="secondary" id="receiptCatalogRefresh">刷新目录</button></div></div>`:''}<button type="button" id="receiptScan" class="receipt-scan">拍电脑配对二维码</button><p class="fine">也可以手动填写电脑显示的地址和8位码。</p><label class="field">电脑地址<input name="url" aria-label="电脑地址" placeholder="http://192.168.1.5:8769" autocomplete="off"></label><label class="field">8位配对码<input name="code" aria-label="8位配对码" inputmode="numeric" maxlength="8" autocomplete="off"></label><p class="fine" id="receiptPairProgress">配对后自动缓存项目和人员，离线也能选择。</p>`,'配对电脑',async form=>{if(!/^\d{8}$/.test(form.get('code')))throw Error('请输入8位配对码');$('receiptPairProgress').textContent='正在连接电脑…';await api('receiptPair',Object.fromEntries(form));toast('电脑已配对');});
 $('receiptScan').onclick=async()=>{try{const qr=await api('receiptQrCamera');if(!qr)return;$('receiptPairProgress').textContent='二维码已识别，正在配对…';await api('receiptPair',{qr});$('dialog').close();await refresh();toast('电脑已配对');}catch(e){showError(e.message);}};
 if(inbox.paired){$('receiptUnpair').onclick=()=>run(async()=>{await api('receiptUnpair');$('dialog').close();toast('已解除配对，收票记录仍保留在手机');});$('receiptCatalogRefresh').onclick=()=>run(async()=>{await api('receiptSync');$('dialog').close();toast('已尝试刷新电脑目录');});}
}
setInterval(()=>{if(tab==='receipts'&&!document.hidden&&!$('dialog').open&&!busy)refresh().catch(e=>toast(e.message));},5000);
