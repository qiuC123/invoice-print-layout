/* The selected matter is frozen for the lifetime of this dialog. No automatic association. */
(() => {
  const dialog = document.createElement('dialog');
  dialog.id = 'mailSearchDialog';
  dialog.className = 'mail-search-dialog';
  dialog.innerHTML = `<div class="dialoghead"><h2>查找对应发票</h2><button type="button" id="mailSearchClose" aria-label="关闭发票查找">×</button></div>
    <p id="mailSearchMatter"></p><p class="muted">按邮件收件日期查找附件和领票链接。匹配提示仅作线索，预览核对后再关联。</p>
    <form id="mailSearchForm"><div class="actions"><label>开始日期 <input id="mailSearchStart" type="date" required></label><label>结束日期 <input id="mailSearchEnd" type="date" required></label><button class="primary" id="mailSearchRun">查找邮箱</button></div></form>
    <p id="mailSearchStatus" role="status"></p><div id="mailSearchResults"></div>`;
  document.body.append(dialog);
  let target = null, result = null, busy = false;
  const el = id => document.getElementById(id);
  const day = d => `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
  const message = text => { el('mailSearchStatus').textContent = text; };
  const close = () => { if(!busy) dialog.close(); };
  el('mailSearchClose').onclick = close;
  dialog.addEventListener('cancel', e => { if(busy) e.preventDefault(); });
  function setBusy(value) {
    busy = value;
    dialog.querySelectorAll('button,input').forEach(node => node.disabled = value);
  }
  document.addEventListener('click', e => {
    if(!e.target.closest('#findInvoice')) return;
    const item = state.items.find(x => x.id === active);
    if(!item || item.stage !== 'draft') return;
    target = {id:item.id,title:item.title}; result = null;
    el('mailSearchMatter').textContent = `${item.project} · ${item.title} · ¥${item.amount}`;
    const today = new Date(), first = new Date(); first.setDate(first.getDate()-30);
    el('mailSearchStart').value = day(first); el('mailSearchEnd').value = day(today);
    el('mailSearchStart').max = el('mailSearchEnd').max = day(today);
    el('mailSearchResults').replaceChildren();
    message('默认查最近31天；更早的发票可以修改日期。只读查询，不标记邮件已读。');
    dialog.showModal();
  });
  const node = (tag, text, cls) => { const n=document.createElement(tag); if(text) n.textContent=text; if(cls)n.className=cls; return n; };
  function link(label,url) {
    const a=node('a',label);a.href=url;a.target='_blank';a.rel='noopener noreferrer';return a;
  }
  function render() {
    const box=el('mailSearchResults');box.replaceChildren();
    if(!result.candidates.length) { box.append(node('p','这个日期范围未取得候选发票。可扩大日期范围，或在原邮件领取后上传。'));return; }
    result.candidates.forEach(c => {
      const card=node('article',null,'mail-candidate');
      card.append(node('strong',c.name),node('p',c.reasons.length?c.reasons.join(' · '):'未发现明确匹配线索，请核对是否属于本事项','muted'));
      card.append(node('p',`${c.source.subject}\n${c.source.sender}\n${c.source.date}`,'mail-source'));
      const actions=node('div',null,'actions');
      if(c.preview)actions.append(link(c.status==='needs_action'?'查看二维码':'预览文件',c.preview));
      if(c.status==='downloaded') {
        const role=node('select');role.setAttribute('aria-label',`${c.name} 的材料用途`);
        [['invoice','发票'],['trip','行程单'],['detail','费用明细'],['other','其他材料']].forEach(([value,label])=>{const option=node('option',label);option.value=value;role.append(option);});
        role.value=c.suggested_role||'invoice';actions.append(role);
        const button=node('button','关联到此事项');button.type='button';actions.append(button);
        const confirmation=node('div',null,'mail-confirm');confirmation.hidden=true;
        const explanation=node('p');confirmation.append(explanation);
        const confirm=node('button','确认关联','primary'),cancel=node('button','取消');
        confirm.type=cancel.type='button';confirmation.append(confirm,cancel);
        button.onclick=()=>{explanation.textContent=`确认将这份文件作为${role.selectedOptions[0].textContent}加入“${target.title}”？关联后仍需核对，不会提交报销。`;confirmation.hidden=false;button.hidden=true;role.disabled=true;};
        cancel.onclick=()=>{confirmation.hidden=true;button.hidden=false;role.disabled=false;};
        confirm.onclick=async()=>{
          setBusy(true);
          try {
            const response=await api('mail/associate',{id:target.id,search_id:result.search_id,candidate_id:c.id,role:role.value});
            c.status='associated'; await load(); render();
            message(response.duplicate?'该文件已经关联，无需重复添加。':'已关联到选定事项，请核对附件用途与金额。');
          } catch(e) { message(e.message); }
          finally { setBusy(false); }
        };
        card.append(actions,confirmation);
      } else {
        card.append(node('p',c.status==='associated'?'已关联到此事项':c.detail));
        if(c.link && /^https?:\/\//i.test(c.link)) actions.append(link('打开领票页面',c.link));
        card.append(actions);
        if(c.status==='needs_action' && c.preview) { const img=node('img');img.src=c.preview;img.alt='邮件中的领票二维码';img.className='mail-qr';card.append(img); }
        if(c.status==='needs_action')card.append(node('p','领取后关闭此窗口，在事项材料夹选择“发票”并上传文件。','muted'));
      }
      box.append(card);
    });
  }
  el('mailSearchForm').onsubmit=async e=>{
    e.preventDefault();if(busy||!target)return;
    setBusy(true);result=null;el('mailSearchResults').replaceChildren();message('正在只读查询邮箱并提取发票文件，请稍候…');
    try {
      result=await api('mail/search',{id:target.id,start:el('mailSearchStart').value,end:el('mailSearchEnd').value});
      render();message(`已检查${result.scanned}封邮件，其中${result.invoice_mails}封票据邮件，得到${result.candidates.length}个候选。${result.warnings.join('；')}`);
    } catch(e) {message(e.message);}
    finally {setBusy(false);}
  };
})();
