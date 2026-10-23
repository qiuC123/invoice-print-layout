'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const escape = value => String(value ?? '').replace(/[&<>"']/g, character => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[character]));
  const money = value => Number.isSafeInteger(value) ? (value / 100).toFixed(2) : '待确认';
  const pathKey = value => encodeURIComponent(String(value));
  const getStored = key => {try {return localStorage.getItem(key) || '';} catch (_) {return '';}};
  const store = (key, value) => {try {localStorage.setItem(key, value);} catch (_) { /* Private browsing can disable storage. */ }};
  let state = {projects: [], items: [], report_types: []}, queue = [], board = {}, people = [];
  let project = getStored('phone-project'), tab = 'materials', busy = false, expired = false, loaded = false;
  let selected = new Set();
  const roles = {invoice: '发票', receipt: '收据／购买凭证', payment: '付款截图', purchase: '购买记录', order: '订单记录', trip: '行程单', detail: '明细', other: '其他材料'};
  const alive = item => !item.deleted && !item.merged_into && !item.returned_to_synthesis && item.stage !== 'cancelled';
  const pending = item => alive(item) && item.stage === 'draft' && (!item.verified || !item.ready);
  const ready = item => alive(item) && (item.stage === 'draft' && item.ready || ['submitted', 'reimbursed'].includes(item.stage));
  const items = () => state.items.filter(item => item.project_id === project);
  const cards = () => [...(board.vouchers || []), ...(board.payments || []), ...(board.other || [])];
  const materialId = card => card.id || String(card.key || '').replace(/^q:/, '');
  function notice(message, error = false) {
    $(error ? 'error' : 'notice').textContent = message;
    $(error ? 'error' : 'notice').hidden = false;
    $(error ? 'notice' : 'error').hidden = true;
  }
  function lock(value) {
    busy = value; $('content').setAttribute('aria-busy', String(value));
    for (const control of document.querySelectorAll('button,input,select,textarea')) {
      if (control.id === 'theme') continue;
      control.disabled = value || expired || !loaded;
    }
    $('refresh').disabled = value || expired;
    if (!value && !expired && loaded) {
      $('files').disabled = $('photos').disabled = !project;
      for (const control of document.querySelectorAll('[data-needs-project]')) control.disabled = !project;
      for (const control of document.querySelectorAll('[data-locked]')) control.disabled = true;
    }
  }
  function disconnect() {
    expired = true; loaded = false;
    $('connection').textContent = '连接已关闭或过期，请在电脑上重新开启手机连接并扫码。';
    notice('连接已关闭或过期。请重新扫描电脑上的二维码；已保存在电脑上的材料会保留。', true);
    lock(false);
  }
  async function request(url, body) {
    if (expired) throw Error('请重新扫描电脑上的二维码连接。');
    let response;
    try {
      response = await fetch(url, {method: body === undefined ? 'GET' : 'POST', credentials: 'same-origin', cache: 'no-store',
        ...(body === undefined ? {} : {headers: {'Content-Type': 'application/json', ...(url === '/api/phone/pair' ? {} : {'X-Workbench-Token': state.token})}, body: JSON.stringify(body)})});
    } catch (_) {
      const error = Error('无法连接电脑。请确认 Wi-Fi 或 Tailscale 已连接，且电脑上的助手仍在运行。操作结果可能已保存，请先刷新查看，勿直接重复提交。');
      error.uncertain = body !== undefined; throw error;
    }
    let value;
    try {value = await response.json();} catch (_) {
      const error = Error('电脑返回内容无法确认，请刷新查看结果，勿重复提交。'); error.uncertain = body !== undefined; throw error;
    }
    if (response.status === 403) {disconnect(); const error = Error('连接已关闭或过期，请重新扫码。'); error.status = 403; throw error;}
    if (!response.ok) {const error = Error(value.error || '操作未完成，请刷新后重试。'); error.status = response.status; throw error;}
    return value;
  }
  const post = (name, body) => request('/api/' + name, body);
  async function act(work) {
    if (busy || expired) return;
    lock(true);
    try {await work();} catch (error) {if (!expired) notice(error.message, true);} finally {lock(false);}
  }
  function updateProjects() {
    const projects = state.projects.filter(value => !value.deleted && !value.permanently_deleted && !value.reimbursed);
    if (!projects.some(value => value.id === project)) project = projects[0]?.id || '';
    store('phone-project', project);
    $('project').innerHTML = projects.length ? projects.map(value => `<option value="${escape(value.id)}" ${value.id === project ? 'selected' : ''}>${escape(value.name)}</option>`).join('') : '<option value="">请先在电脑上新建项目</option>';
  }
  async function refresh() {
    loaded = false;
    try {
    const latest = await request('/api/state');
    state = latest; updateProjects();
    if (project) {
      const query = '?project=' + encodeURIComponent(project);
      const values = await Promise.all([request('/api/intake'), request('/api/material-matching' + query), request('/api/reimbursement-people' + query)]);
      queue = (values[0].queue || []).filter(value => value.project_id === project);
      board = values[1]; people = values[2].people || [];
    } else {queue = []; board = {}; people = [];}
    selected.clear(); loaded = true;
    $('connection').textContent = '已连接电脑 · 材料与费用保存在电脑账本 · ' + new Date().toLocaleTimeString('zh-CN', {hour: '2-digit', minute: '2-digit'});
    render();
    } finally {
      if (!loaded) $('content').innerHTML = '<p class="empty">未能读取最新账本。请检查电脑与 Wi-Fi 或 Tailscale 连接后点击刷新；请勿重复提交刚才的操作。</p>';
    }
  }
  function materialName(card) {return card.fields?.shop_name || card.fields?.merchant || card.fields?.title || card.name || '材料';}
  const sourceButton = (key, title = '查看原件') => `<button type="button" data-source="${escape(key)}">${title}</button>`;
  function materialCard(entry) {
    const card = cards().find(value => value.key === 'q:' + entry.id) || {...entry, key: 'q:' + entry.id, amount_cents: null};
    const active = !entry.item_id && ['received', 'review'].includes(entry.status);
    const selectable = active && cards().some(value => value.key === card.key) && card.qr_status !== 'manual';
    return `<article class="card"><div class="card-head"><div class="grow"><h3>${escape(materialName(card))}</h3><p class="file-name">${escape(entry.name)}</p></div><span class="money">${Number.isSafeInteger(card.amount_cents) ? '¥ ' + money(card.amount_cents) : '金额待确认'}</span></div><p><span class="tag">${escape(roles[card.role] || '待分类')}</span> <span class="muted">${escape(entry.item_id ? '已关联费用' : entry.result || '等待识别')}</span></p>${card.issues?.length ? `<p class="warning">${escape(card.issues.join('、'))}</p>` : ''}<div class="actions">${sourceButton('q:' + entry.id)}${active ? `<button type="button" data-analyze="${escape(entry.id)}">本地识别</button><button type="button" data-classify="${escape(entry.id)}">修正识别结果</button>` : ''}</div>${selectable ? `<label class="select-material"><input type="checkbox" data-select="${escape(card.key)}" ${selected.has(card.key) ? 'checked' : ''}>选择加入手动组合</label>` : ''}</article>`;
  }
  function renderMaterials() {
    const visible = queue.filter(value => !['deleted', 'archived', 'unused', 'duplicate', 'converted'].includes(value.status));
    const suggestions = board.suggestions || [];
    return `<section><div class="section-heading"><h2>上传与整理</h2><button type="button" id="upload" class="primary" data-needs-project>＋ 上传材料</button></div><p class="muted">选择 PDF、照片或截图，可一次上传多份。电脑本地识别；建议组合须由你确认，不会自动核对。</p><div class="subsection"><div class="section-heading"><h2>建议组合 <span class="tag">${suggestions.length}</span></h2></div>${suggestions.map((suggestion, index) => `<article class="card"><div class="card-head"><div class="grow"><h3>${escape(suggestion.title || '材料组合')}</h3><p class="muted">${suggestion.voucher_count || suggestion.voucher_keys.length} 份票据／购买凭证 · ${suggestion.payment_count || suggestion.payment_ids.length} 份付款</p></div><span class="money">¥ ${money(suggestion.amount_cents)}</span></div><p class="muted">${escape(suggestion.reason || '')}</p>${suggestion.ambiguous || suggestion.needs_name_review || suggestion.warnings?.length ? `<p class="warning">${escape([...(suggestion.ambiguous ? ['存在多个可能组合，请对照原件选择。'] : []), ...(suggestion.needs_name_review ? ['商家名称待核对。'] : []), ...(suggestion.warnings || [])].join(' '))}</p>` : ''}<button type="button" data-suggestion="${index}" class="primary">查看并确认组合</button></article>`).join('') || '<p class="empty">暂无建议组合。可修正识别结果后刷新，或勾选对应材料手动组合。</p>'}${(board.suggestion_conflicts || []).length ? `<p class="warning">${escape(board.suggestion_conflicts.map(value => value.reason).join(' '))}</p>` : ''}</div><div class="subsection"><h2>项目材料</h2><div class="manual-tools"><button type="button" id="manualCreate" data-needs-project>手动组合（${selected.size}）</button><p class="muted">选择同一笔费用的材料；发票与付款不会重复计费。</p></div>${visible.map(materialCard).join('') || '<p class="empty">本项目还没有材料，点击“上传材料”开始。</p>'}</div></section>`;
  }
  function renderExpenses(isReady) {
    const list = items().filter(isReady ? ready : pending);
    return `<section><h2>${isReady ? '可提交' : '待核对'} <span class="tag">${list.length}</span></h2><p class="muted">${isReady ? '核对结果与电脑版共用。报销表和打印包请在电脑上生成；可提交不代表已报销到账。' : '对照原件，确认金额、报销项目和费用归属人，再完成核对。'}</p>${list.map(item => `<article class="card"><div class="card-head"><div class="grow"><h3>${escape(item.project_name || item.title)}</h3><p class="muted">${escape(item.reimbursement_person_label || people.find(person => person.id === item.reimbursement_person_id)?.label || '未指定人员')} · ${escape(item.category || '类别待确认')}</p></div><span class="money">${item.amount_pending ? '待确认' : '¥ ' + money(item.amount_cents)}</span></div>${item.missing?.length ? `<p class="warning">${item.missing_acknowledged ? '已确认仍缺：' : '还缺：'}${escape(item.missing.join('、'))}</p>` : '<p class="muted">材料齐全</p>'}<button type="button" data-expense="${escape(item.id)}">${isReady ? '查看费用与原件' : '查看与核对'}</button></article>`).join('') || `<p class="empty">暂无${isReady ? '可提交' : '待核对'}费用。</p>`}</section>`;
  }
  function render() {
    $('materialCount').textContent = queue.filter(value => !value.item_id && ['received', 'review'].includes(value.status)).length;
    $('reviewCount').textContent = items().filter(pending).length; $('readyCount').textContent = items().filter(ready).length;
    document.querySelectorAll('nav [data-tab]').forEach(button => {button.classList.toggle('active', button.dataset.tab === tab); if (button.dataset.tab === tab) button.setAttribute('aria-current', 'page'); else button.removeAttribute('aria-current');});
    $('content').innerHTML = !project ? '<p class="empty">电脑上还没有可用项目，请先在电脑版新建项目，再点击刷新。</p>' : tab === 'materials' ? renderMaterials() : renderExpenses(tab === 'ready');
    if ($('upload')) $('upload').onclick = () => {if (!busy) openUpload();};
    if ($('manualCreate')) $('manualCreate').onclick = () => {if (!busy) openCreate(null);};
    bindSources($('content'));
    for (const button of document.querySelectorAll('[data-analyze]')) button.onclick = () => act(async () => {notice('电脑正在本地识别，请稍候…'); await post('intake/analyze', {id: button.dataset.analyze}); await refresh(); notice('本地识别已完成，请对照原件检查识别结果。');});
    for (const button of document.querySelectorAll('[data-classify]')) button.onclick = () => openClassify(button.dataset.classify);
    for (const input of document.querySelectorAll('[data-select]')) input.onchange = () => {input.checked ? selected.add(input.dataset.select) : selected.delete(input.dataset.select); $('manualCreate').textContent = `手动组合（${selected.size}）`;};
    for (const button of document.querySelectorAll('[data-suggestion]')) button.onclick = () => openCreate(board.suggestions[Number(button.dataset.suggestion)]);
    for (const button of document.querySelectorAll('[data-expense]')) button.onclick = () => openExpense(button.dataset.expense);
  }
  function closeDialog() {const dialog = $('phoneDialog'); if (dialog && !busy) dialog.remove();}
  function dialog(title, body) {
    $('phoneDialog')?.remove();
    const element = document.createElement('dialog'); element.id = 'phoneDialog';
    element.innerHTML = `<div class="dialog-head"><h2>${escape(title)}</h2><button type="button" data-close aria-label="关闭">×</button></div><div class="dialog-body">${body}<p class="dialog-error" data-dialog-error role="alert" hidden></p></div>`;
    document.body.append(element); element.querySelector('[data-close]').onclick = closeDialog;
    element.oncancel = event => {event.preventDefault(); closeDialog();}; element.showModal(); bindSources(element); return element;
  }
  function bindSources(root) {for (const button of root.querySelectorAll('[data-source]')) button.onclick = () => openSource(button.dataset.source);}
  function openUpload() {
    const element = dialog('上传材料', '<p>选择照片或文件，可一次上传多份。</p><div class="actions"><button type="button" data-picker="photos" class="primary">从相册多选</button><button type="button" data-picker="files">选择文件（PDF／图片）</button></div><p class="muted">相册中勾选多张后确认。如果相册仍只能选一张，请用“选择文件”，长按第一份文件后再勾选其他文件。</p><p class="muted">支持 PDF、JPG、PNG、WebP；每批最多 50 份，每份不超过 20MB。选定后开始上传，原件保存在电脑。</p>');
    for (const button of element.querySelectorAll('[data-picker]')) button.onclick = () => {
      if (busy || expired || !loaded || !project) return;
      closeDialog(); $(button.dataset.picker).click();
    };
  }
  function openSource(key) {
    if (busy) return;
    const isQueue = key.startsWith('q:'), id = key.slice(2);
    const entry = isQueue ? queue.find(value => value.id === id) : state.items.flatMap(item => item.attachments || []).find(value => value.id === id);
    if (!entry) {notice('材料已变化，请刷新后查看。', true); return;}
    const pdf = /\.pdf$/i.test(entry.name), original = (isQueue ? '/intake-file/' : '/file/') + pathKey(id);
    const preview = pdf ? `<iframe class="preview-frame" title="原件预览" src="/${isQueue ? 'intake-preview' : 'attachment-preview'}/${pathKey(id)}"></iframe>` : `<img class="preview" alt="原件" src="${original}">`;
    dialog('材料原件', `<p class="file-name">${escape(entry.name)}</p>${preview}<div class="actions"><a href="${original}" target="_blank" rel="noopener noreferrer">打开／保存原始文件</a><button type="button" id="sourceBack">返回${tab === 'materials' ? '材料' : '费用列表'}</button></div>`);
    $('sourceBack').onclick = closeDialog;
  }
  function formError(element, failure, committed = false) {
    const error = element.querySelector('[data-dialog-error]');
    error.hidden = false; error.textContent = committed ? '操作已保存，但页面刷新未完成。请关闭此窗口并刷新查看，勿重复提交。' : failure.message;
    if (committed || failure.uncertain || failure.status === 409 || failure.status === 403 || /版本|变化|冲突|已关联|不是草稿/.test(failure.message)) {
      error.textContent += ' 请关闭窗口并刷新后重新确认。';
      element.querySelectorAll('[type=submit],[data-commit]').forEach(control => {control.dataset.locked = 'true'; control.disabled = true;});
    }
  }
  function submitForm(element, work, done) {
    const form = element.querySelector('form'); let committed = false, submitting = false;
    form.onsubmit = async event => {
      event.preventDefault(); if (busy || expired || submitting || form.querySelector('[type=submit]')?.dataset.locked) return;
      submitting = true; lock(true); element.querySelector('[data-dialog-error]').hidden = true;
      try {await work(form); committed = true; await refresh(); element.remove(); lock(false); if (done) done();}
      catch (failure) {if (!expired) formError(element, failure, committed);}
      finally {submitting = false; lock(false);}
    };
  }
  function openClassify(id) {
    if (busy) return;
    const entry = queue.find(value => value.id === id), card = cards().find(value => value.key === 'q:' + id);
    if (!entry || entry.item_id || !['received', 'review'].includes(entry.status)) {notice('材料已变化，请刷新后编辑。', true); return;}
    const fields = entry.fields || {}, chosenRole = ['invoice', 'receipt', 'payment', 'other'].includes(card?.role || entry.role) ? card?.role || entry.role : ['order', 'purchase'].includes(entry.role) ? 'receipt' : 'other';
    const names = {title: '材料名称', amount: '实际金额（元）', merchant: '商家／销售方', shop_name: '实际店铺', shopping_platform: '购物平台', payment_channel: '支付渠道', expense_date: '费用日期', payment_time: '支付时间', order_number: '订单号', invoice_number: '发票号码', note: '备注'};
    const element = dialog('修正识别结果', `<p class="file-name">${escape(entry.name)}</p><p class="muted">只修改本份材料的信息。保存后仍需确认组合和核对。</p><form><label>材料用途<select name="role">${['invoice', 'receipt', 'payment', 'other'].map(role => `<option value="${role}" ${chosenRole === role ? 'selected' : ''}>${roles[role]}</option>`).join('')}</select></label>${Object.entries(names).map(([name, label]) => `<label>${label}<input name="${name}" ${name === 'amount' ? 'inputmode="decimal"' : ''} maxlength="${name === 'note' ? 2000 : 200}" value="${escape(fields[name] ?? (name === 'amount' && Number.isSafeInteger(card?.amount_cents) ? money(card.amount_cents) : name === 'title' ? entry.name : ''))}"></label>`).join('')}<label>付款状态<select name="payment_status"><option value="">未确认</option>${[['succeeded', '支付成功'], ['unpaid', '未支付'], ['failed', '支付失败'], ['refunded', '已退款']].map(([value, text]) => `<option value="${value}" ${fields.payment_status === value ? 'selected' : ''}>${text}</option>`).join('')}</select></label><details><summary>查看本地识别文字</summary><pre class="recognition-text">${escape(entry.text || '尚未识别出文字')}</pre></details><div class="actions"><button type="submit" class="primary">保存识别结果</button></div></form>`);
    submitForm(element, async form => {const values = Object.fromEntries([...form.elements].filter(control => control.name).map(control => [control.name, control.value])); const role = values.role; delete values.role; await post('intake/classify', {id, revision: entry.revision, role, fields: {...values, project_id: project}});}, () => notice('识别结果已保存，请检查建议组合。'));
  }
  const personOptions = id => '<option value="">未指定人员</option>' + people.map(person => `<option value="${escape(person.id)}" ${person.id === id ? 'selected' : ''}>${escape(person.label)}</option>`).join('');
  function reportFields(choice, amount) {
    return `<label>项目类型<select name="project_type" required><option value="">请选择项目类型</option>${(state.report_types || []).map(type => `<option value="${escape(type.code)}" ${type.code === choice.project_type ? 'selected' : ''}>${escape(type.label)}</option>`).join('')}</select></label><label>报销项目名称<span data-name-slot></span></label><label>报销金额（元）<input name="amount" inputmode="decimal" required maxlength="20" value="${escape(amount)}"></label>`;
  }
  function bindReportFields(element, initialName) {
    const typeSelect = element.querySelector('[name=project_type]');
    const updateName = name => {
      const type = state.report_types.find(value => value.code === typeSelect.value);
      element.querySelector('[data-name-slot]').innerHTML = type?.editable_name ? `<input name="project_name" required maxlength="200" value="${escape(name || '')}" placeholder="填写报销项目名称">` : `<select name="project_name" required ${type ? '' : 'disabled'}><option value="">${type ? '请选择具体项目' : '请先选择项目类型'}</option>${(type?.names || []).map(value => `<option value="${escape(value)}" ${value === name ? 'selected' : ''}>${escape(value)}</option>`).join('')}</select>`;
    };
    updateName(initialName); typeSelect.onchange = () => updateName('');
  }
  function reportValues(form) {
    const values = {project_type: form.elements.project_type.value, project_name: form.elements.project_name.value, amount: form.elements.amount.value}, amount = values.amount.trim();
    if (!/^\d+(?:\.\d{1,2})?$/.test(amount) || !Number.isFinite(Number(amount)) || Number(amount) > 999999999) throw Error('金额请填非负数字，最多两位小数。');
    if (!values.project_type || !values.project_name?.trim()) throw Error('请选择项目类型并选择或填写报销项目名称。');
    return {project_type: values.project_type, project_name: values.project_name.trim(), amount};
  }
  function openCreate(suggestion) {
    if (busy) return;
    const keys = suggestion ? [...suggestion.voucher_keys, ...suggestion.payment_ids.map(id => 'q:' + id)] : [...selected];
    const chosen = keys.map(key => cards().find(card => card.key === key));
    if (!keys.length || chosen.some(card => !card)) {notice('请先选择属于同一笔费用的材料。', true); return;}
    const vouchers = chosen.filter(card => (board.vouchers || []).some(value => value.key === card.key));
    const payments = chosen.filter(card => (board.payments || []).some(value => value.key === card.key));
    const supports = chosen.filter(card => (board.other || []).some(value => value.key === card.key));
    const choice = chosen.find(card => card.report_choice?.project_type)?.report_choice || {};
    // These totals only prefill the form; the desktop service validates the selected sources.
    const counted = vouchers.length ? vouchers : payments.length ? payments : supports;
    const amount = suggestion?.amount_cents ?? (counted.every(card => Number.isSafeInteger(card.amount_cents)) ? counted.reduce((sum, card) => sum + card.amount_cents, 0) : null);
    const element = dialog('确认材料组合', `<p>共 ${chosen.length} 份材料。请确认均属于同一笔费用；确认后只创建待核对费用。</p><ul class="source-list">${chosen.map(card => `<li><strong>${escape(materialName(card))}</strong><p class="muted">${escape(roles[card.role] || '材料')} · ${Number.isSafeInteger(card.amount_cents) ? '¥ ' + money(card.amount_cents) : '金额待确认'}</p><p class="file-name">${escape(card.name)}</p></li>`).join('')}</ul>${suggestion?.warnings?.length || suggestion?.ambiguous || suggestion?.needs_name_review ? `<p class="warning">${escape([...(suggestion.warnings || []), ...(suggestion.ambiguous ? ['存在多个可能组合，必须人工确认对应关系。'] : []), ...(suggestion.needs_name_review ? ['商家名称待核对。'] : [])].join(' '))}</p>` : ''}<form>${reportFields(choice, Number.isSafeInteger(amount) ? money(amount) : '')}<label>费用归属人<select name="person_id">${personOptions(suggestion?.reimbursement_person_id || '')}</select></label><p class="muted">人员名单由电脑版管理。暂不指定也可创建，再在核对时修改。</p><label class="check-line"><input name="confirmed" type="checkbox" required><span>我已对照材料，确认这些材料属于同一笔费用，并确认报销金额。</span></label><div class="actions"><button type="submit" class="primary">确认创建待核对费用</button></div></form>`);
    bindReportFields(element, choice.project_name);
    submitForm(element, async form => {
      const fields = reportValues(form), person = form.elements.person_id.value;
      const revisions = Object.fromEntries(chosen.filter(card => card.key.startsWith('q:')).map(card => [materialId(card), card.revision]));
      const versions = Object.fromEntries(vouchers.filter(card => card.key.startsWith('e:')).map(card => [card.item_id || card.id || card.key.slice(2), card.version]));
      await post('material-matching/create', {project_id: project, voucher_keys: vouchers.map(card => card.key), payment_ids: payments.map(materialId), support_ids: supports.map(materialId), revisions, expected_versions: versions, fields: {...fields, ...(person ? {reimbursement_person_id: person} : {})}, ...(suggestion ? {suggestion_id: suggestion.id, suggestion_token: suggestion.snapshot_token} : {})});
    }, () => {tab = 'review'; render(); notice('已创建 1 笔待核对费用，电脑账本同步可见。请继续核对。');});
  }
  function openExpense(id) {
    if (busy) return;
    const item = state.items.find(value => value.id === id && value.project_id === project);
    if (!item) {notice('费用已变化，请刷新后查看。', true); return;}
    const editable = pending(item);
    const name = people.find(person => person.id === item.reimbursement_person_id)?.label || item.reimbursement_person_label || '未指定人员';
    const element = dialog('费用与核对', `<h3>${escape(item.project_name || item.title)}</h3><p class="money">${item.amount_pending ? '金额待确认' : '¥ ' + money(item.amount_cents)}</p><p>报销分类：${escape(state.report_types.find(type => type.code === item.project_type)?.label || item.category || '待确认')}</p><p>费用归属人：${escape(name)}</p>${item.missing?.length ? `<p class="warning">${item.missing_acknowledged ? '已确认仍缺：' : '还缺：'}${escape(item.missing.join('、'))}</p>` : '<p class="muted">材料齐全，请对照原件核对。</p>'}<h3 class="subsection">原始材料</h3><ul class="source-list">${(item.attachments || []).map(attachment => `<li><p>${escape(attachment.name)}</p><p class="muted">${escape(roles[attachment.role] || attachment.role)}</p>${sourceButton('a:' + attachment.id)}</li>`).join('') || '<li class="muted">暂无附件。</li>'}</ul>${editable ? `<div class="actions"><button type="button" id="editExpense">修改金额与报销项目</button>${!item.verified ? '<button type="button" id="editPerson">修改归属人</button>' : ''}</div><div class="actions"><button type="button" id="verifyExpense" class="primary">核对完成，进入可提交</button></div>` : '<p class="muted">本笔费用已核对或已有历史记录。报销表和打印包请在电脑版生成。</p>'}`);
    if (editable) {$('editExpense').onclick = () => openEditExpense(item); if ($('editPerson')) $('editPerson').onclick = () => openPerson(item); $('verifyExpense').onclick = () => openVerify(item);}
    return element;
  }
  function openEditExpense(item) {
    const choice = {project_type: item.project_type || item.report_choice?.project_type || '', project_name: item.project_name || item.report_choice?.project_name || ''};
    const element = dialog('修改金额与报销项目', `<p class="muted">保存后仍待核对。</p><form>${reportFields(choice, item.amount_pending ? '' : money(item.amount_cents))}<div class="actions"><button type="submit" class="primary">保存报销信息</button></div></form>`);
    bindReportFields(element, choice.project_name);
    submitForm(element, async form => {await post('review/edit-fields', {id: item.id, expected_version: item.version, ...reportValues(form)});}, () => {notice('报销信息已保存，仍待核对。'); openExpense(item.id);});
  }
  function openPerson(item) {
    const element = dialog('修改费用归属人', `<p>${escape(item.title)}</p><form><label>费用归属人<select name="person_id">${personOptions(item.reimbursement_person_id || '')}</select></label><p class="muted">新增人员请在电脑版的人员管理中操作，再回到手机刷新。</p><div class="actions"><button type="submit" class="primary">保存归属人</button></div></form>`);
    submitForm(element, async form => {await post('reimbursement-people/assign-expense', {id: item.id, expected_version: item.version, person_id: form.elements.person_id.value});}, () => {notice('费用归属人已保存。'); openExpense(item.id);});
  }
  function openVerify(item) {
    const blocked = item.amount_pending || (item.missing || []).some(gap => gap === '金额待确认' || gap.startsWith('确认此类别的材料要求')) || !item.complete && item.missing_acknowledgement_allowed === false;
    if (blocked) {const target = $('phoneDialog').querySelector('[data-dialog-error]'); target.hidden = false; target.textContent = '请先确认报销金额、具体费用类别和材料要求，再完成核对。'; return;}
    const gaps = item.missing || [];
    const element = dialog('确认核对完成', `<p><strong>${escape(item.project_name || item.title)} · ¥ ${money(item.amount_cents)}</strong></p>${!item.complete ? `<p class="warning">本笔仍缺：${escape(gaps.join('、'))}。确认后缺项会继续显示，打印只使用已经收到的材料。</p>` : '<p>请确认已对照原件检查金额、报销项目与人员。</p>'}<form><label class="check-line"><input name="confirmed" type="checkbox" required><span>${item.complete ? '我已核对材料和费用信息。' : '我已确认当前缺项，仍将此费用核对完成。'}</span></label><div class="actions"><button type="submit" class="primary">${item.complete ? '确认核对完成' : '确认缺项并核对完成'}</button></div></form>`);
    submitForm(element, async () => {await post('transition', {id: item.id, action: 'verify', expected_version: item.version, ...(!item.complete ? {acknowledge_missing: true, expected_missing: [...gaps]} : {})});}, () => {tab = 'ready'; render(); notice('核对完成，已进入可提交。电脑版可继续制作报销表和打印包。');});
  }
  function uploadFiles(input) {return act(async () => {
    const files = [...input.files]; input.value = ''; if (!project || !files.length) return;
    if (files.length > 50) throw Error('每批最多上传 50 份材料，请分批选择。');
    const unsupported = files.find(file => !/\.(pdf|png|jpe?g|webp)$/i.test(file.name));
    if (unsupported) throw Error(`${unsupported.name}：仅支持 PDF、JPG、PNG、WebP。尚未上传本批材料，请重新选择。`);
    const oversized = files.find(file => !file.size || file.size > 20 * 1024 * 1024);
    if (oversized) throw Error(`${oversized.name}：请使用非空且不超过 20MB 的文件。尚未上传本批材料，请重新选择。`);
    const batch = globalThis.crypto?.randomUUID?.() || `phone-${Date.now()}-${Math.random().toString(16).slice(2)}`;
    let received = 0, duplicate = 0, phase = '保存';
    try {
      for (const [index, file] of files.entries()) {
        phase = '保存'; notice(`正在上传 ${index + 1}/${files.length}：${file.name}…`);
        const data = await new Promise((resolve, reject) => {const reader = new FileReader(); reader.onload = () => resolve(reader.result.split(',')[1]); reader.onerror = () => reject(Error('无法读取所选文件，请重新选择。')); reader.readAsDataURL(file);});
        const entry = await post('intake/receive', {name: file.name, data, project_id: project, batch}); received++;
        if (entry.duplicate || entry.status === 'duplicate') {duplicate++; continue;}
        phase = '识别'; notice(`已保存 ${index + 1}/${files.length}，电脑正在本地识别：${file.name}…`);
        await post('intake/analyze', {id: entry.id});
      }
      await refresh(); notice(`本批已保存 ${received} 份，重复 ${duplicate} 份。请查看识别结果与建议组合。`);
    } catch (error) {
      if (!expired) {
        try {await refresh();} catch (_) { /* Do not retry any upload or recognition mutation. */ }
        notice(`本批已确认保存 ${received} 份，${phase}未能全部完成。${error.message} 已保存的原件保留，可在材料卡片点“本地识别”。请先刷新核实，勿重复上传整批。`, true);
      }
    }
  });}
  for (const id of ['photos', 'files']) $(id).onchange = () => uploadFiles($(id));
  $('project').onchange = () => act(async () => {project = $('project').value; selected.clear(); tab = 'materials'; closeDialog(); await refresh(); notice('已切换项目。');});
  $('refresh').onclick = () => act(async () => {await refresh(); notice('已从电脑刷新，金额和核对结果以当前账本为准。');});
  for (const button of document.querySelectorAll('nav [data-tab]')) button.onclick = () => {if (!busy && loaded && !expired) {tab = button.dataset.tab; render(); window.scrollTo(0, 0);}};
  function setTheme(theme) {document.documentElement.dataset.theme = theme; store('phone-theme', theme); $('theme').setAttribute('aria-label', theme === 'dark' ? '切换浅色主题' : '切换深色主题');}
  setTheme(getStored('phone-theme') || (matchMedia('(prefers-color-scheme:light)').matches ? 'light' : 'dark'));
  $('theme').onclick = () => setTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark');
  window.addEventListener('offline', () => notice('手机网络已断开。已保存的材料仍在电脑上，恢复 Wi-Fi 或 Tailscale 连接后请点击刷新。', true));
  // A QR secret lives only in the fragment, then in this one pairing request.
  const code = location.hash.slice(1); history.replaceState(null, '', location.pathname + location.search);
  (async () => {
    lock(true);
    try {if (code) await post('phone/pair', {code}); await refresh(); notice('与电脑版共用一份账本。可上传材料、查看原件并核对费用。');}
    catch (error) {if (!expired) {notice(error.message, true); $('connection').textContent = '连接未完成，请确认电脑仍在运行，或重新扫码。';}}
    finally {lock(false);}
  })();
})();
