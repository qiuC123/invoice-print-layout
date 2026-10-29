'use strict';
// Suggestions are read-only; creating an expense still requires explicit confirmation.
(() => {
  let cache = {projectId: null, board: null, loaded: false, error: ''};
  let selected = '', request = 0, loading = false, selectionNotice = '', batch = null, deskBusy = false, batchOpenRevision = 0;
  const checkedCandidates = new Map();
  const project = () => projectScope || window.currentInvoiceProjectId?.() || '';
  const current = () => cache.projectId === project();
  const cards = () => [...(cache.board?.vouchers || []), ...(cache.board?.payments || []), ...(cache.board?.other || [])];
  const safeUrl = value => typeof value === 'string' && value.startsWith('/') && !value.startsWith('//') ? value : '';
  const members = suggestion => ({
    vouchers: (cache.board?.vouchers || []).filter(card => suggestion.voucher_keys.includes(card.key)),
    payments: (cache.board?.payments || []).filter(card => suggestion.payment_ids.includes(card.id)),
    supports: (cache.board?.other || []).filter(card => (suggestion.support_ids || []).includes(card.id)),
  });
  const title = suggestion => {
    const vouchers = members(suggestion).vouchers;
    return [...new Set(vouchers.map(card => card.fields?.merchant).filter(Boolean))].join('、') || suggestion.title || '建议材料组合';
  };
  // The desk shows its current project's material pool, independently of the
  // expense filters behind the modal.
  const filtered = () => current() ? (cache.board?.suggestions || []).filter(value => value.project_id === project()) : [];
  window.readSynthesisCandidates = filtered;
  window.synthesisSuggestionsLoaded = () => current() && cache.loaded;
  function adoptBoard(board, projectId) {
    let removed = 0;
    for (const [id, token] of checkedCandidates) {
      if (projectId !== cache.projectId || !(board?.suggestions || []).some(value => value.id === id && value.snapshot_token === token)) {
        checkedCandidates.delete(id); removed++;
      }
    }
    if (removed) selectionNotice = `${removed} 组勾选的材料或版本已变化，已取消勾选；请检查当前候选后重新选择。`;
    cache = {projectId, board, loaded: true, error: ''};
    if (!filtered().some(suggestion => suggestion.id === selected)) selected = '';
    if (batch && !batch.committed && (projectId !== batch.projectId || batch.groups.some(group =>
      !(board?.suggestions || []).some(value => value.id === group.suggestion_id && value.snapshot_token === group.snapshot_token)))) {
      batch.expired = true; showBatchStatus('候选材料或项目已变化，原表单保留供你对照。请关闭后重新勾选当前候选。', true); updateBatchSubmit();
    }
  }
  window.setSynthesisBoard = (board, projectId = project()) => {
    if (projectId !== project()) return;
    adoptBoard(board, projectId);
    renderSuggestions();
  };
  async function refresh() {
    const id = project(), serial = ++request;
    loading = true; renderSuggestions();
    try {
      if ($('matchingDeskDialog')?.open && window.matchingSuggestionActions) {await window.matchingSuggestionActions.refresh(); return;}
      const response = await fetch('/api/material-matching?' + new URLSearchParams(id ? {project: id} : {}));
      const board = await response.json();
      if (!response.ok) throw Error(board.error || '无法读取建议组合');
      if (serial !== request || id !== project()) return;
      adoptBoard(board, id);
    } catch (error) {
      if (serial !== request || id !== project()) return;
      cache = {projectId: id, board: null, loaded: true, error: error.message};
    } finally {
      if (serial === request) {loading = false; renderSuggestions();}
    }
  }
  window.refreshSynthesisCandidates = refresh;
  window.showSynthesisSuggestions = () => window.openMaterialMatching();
  window.resetSynthesisSelection = () => {batchOpenRevision++; checkedCandidates.clear(); selected = ''; selectionNotice = ''; renderSuggestions();};
  const proof = card => {
    const url = safeUrl(card.url), image = safeUrl(card.thumbnail);
    return `<figure class="synthesis-proof">${url ? `<a href="${esc(url)}" target="_blank" rel="noopener">` : ''}${image ? `<img src="${esc(image)}" alt="${esc(window.materialMatchingTitle(card))}" loading="lazy">` : '<span>查看原件</span>'}${url ? '<span class="synthesis-open-original">放大原件 ↗</span></a>' : ''}<figcaption>${esc(window.materialMatchingTitle(card))}<br><strong>¥ ${money(card.amount_cents)}</strong></figcaption></figure>`;
  };
  const selectedCandidates = () => filtered().filter(value => checkedCandidates.get(value.id) === value.snapshot_token);
  function overlapping(groups) {
    const used = new Set();
    for (const group of groups) for (const key of [...group.voucher_keys, ...[...group.payment_ids, ...(group.support_ids || [])].map(id => 'q:' + id)]) {
      if (used.has(key)) return '勾选的组合共用了同一份材料，请取消其中一组，再确认合成。';
      used.add(key);
    }
    return '';
  }
  function batchChoice(suggestion) {
    const group = members(suggestion), primary = group.vouchers.length ? group.vouchers : group.payments;
    const choices = primary.map(card => card.report_choice || {}), codes = choices.map(value => value.project_type || '');
    const type = codes.length && new Set(codes).size === 1 ? (cache.board?.report_types || []).find(value => value.code === codes[0]) : null;
    const names = choices.map(value => value.project_name || '');
    const name = type?.editable_name ? [...new Set(names.filter(Boolean))].join('、').slice(0, 200) :
      type && new Set(names).size === 1 && type.names.includes(names[0]) ? names[0] : '';
    return {project_type: type && (type.editable_name || name) ? type.code : '', project_name: name,
      amount: Number.isSafeInteger(suggestion.amount_cents) ? (suggestion.amount_cents / 100).toFixed(2) : '',
      reimbursement_person_id: window.reimbursementPeopleUI?.choice([...group.vouchers, ...group.payments, ...group.supports]).personId || ''};
  }
  function batchNameControl(index, fields) {
    const type = batch?.types.find(value => value.code === fields.project_type);
    return type?.editable_name ? `<input data-batch-name="${index}" value="${esc(fields.project_name)}" maxlength="200" required placeholder="填写项目名称">` :
      `<select data-batch-name="${index}" required ${type ? '' : 'disabled'}><option value="">${type ? '选择对应细则' : '先选择项目类型'}</option>${(type?.names || []).map(name => `<option value="${esc(name)}" ${name === fields.project_name ? 'selected' : ''}>${esc(name)}</option>`).join('')}</select>`;
  }
  function batchFields(index) {
    const dialog = $('synthesisBatchDialog');
    const person = dialog.querySelector(`[data-batch-person="${index}"]`)?.value || '';
    return {project_type: dialog.querySelector(`[data-batch-type="${index}"]`).value,
      amount: dialog.querySelector(`[data-batch-amount="${index}"]`).value.trim(),
      project_name: dialog.querySelector(`[data-batch-name="${index}"]`).value.trim(), ...(person ? {reimbursement_person_id: person} : {})};
  }
  function batchFieldError(fields, index = null) {
    if (batch.ownerConflicts?.[index] && !fields.reimbursement_person_id) return '请选择费用归属人，所选材料将按确认的费用归属人记录。';
    const type = batch.types.find(value => value.code === fields.project_type);
    if (!type) return '请选择项目类型。';
    if (!fields.amount || !/^\d+(?:\.\d{1,2})?$/.test(fields.amount) || !Number.isFinite(Number(fields.amount)) || Number(fields.amount) * 100 > Number.MAX_SAFE_INTEGER) return '请确认金额，填写非负数字，最多两位小数。';
    if (!fields.project_name) return '请选择或填写项目名称。';
    if (fields.project_name.length > 200) return '项目名称最多200字。';
    if (!type.editable_name && !type.names.includes(fields.project_name)) return '项目名称须选择当前项目类型下的细则。';
    return '';
  }
  function showBatchStatus(text, error = false) {
    const status = $('synthesisBatchStatus');
    if (status) {status.textContent = String(text).replaceAll('待合成候选', '建议组合').replaceAll('待合成材料池', '材料池'); status.hidden = !text; status.classList.toggle('is-error', error);}
  }
  function updateBatchSubmit() {
    if (!batch || !$('confirmSynthesisBatch')) return;
    let invalid = batch.projectId !== project() || batch.expired;
    for (const [index] of batch.groups.entries()) {
      const error = batchFieldError(batchFields(index), index), target = $('synthesisBatchDialog').querySelector(`[data-batch-error="${index}"]`);
      target.textContent = error; target.hidden = !error; invalid ||= !!error;
    }
    $('confirmSynthesisBatch').disabled = batch.busy || batch.committed || invalid;
    $('confirmSynthesisBatch').textContent = batch.busy ? '正在确认合成…' : '确认合成并进入待核对';
  }
  function batchLock(value) {
    batch.busy = value;
    for (const control of $('synthesisBatchDialog').querySelectorAll('button,input,select')) control.disabled = value || batch.committed;
    for (const control of $('synthesisBatchDialog').querySelectorAll('[data-batch-close]')) control.disabled = value;
    for (const control of $('synthesisBatchDialog').querySelectorAll('[data-batch-name]')) {
      if (control.tagName === 'SELECT') control.disabled = value || batch.committed || !batch.types.some(type => type.code === batchFields(Number(control.dataset.batchName)).project_type);
    }
    updateBatchSubmit();
  }
  async function openBatch() {
    if (batch || deskBusy || !$('matchingDeskDialog')?.open || !project()) {if (!project()) {selectionNotice = '先进入一个项目，再确认组合。'; renderSuggestions();} return;}
    const suggestions = selectedCandidates(), conflict = overlapping(suggestions);
    if (!suggestions.length || conflict) {selectionNotice = conflict || '先勾选要合成的候选。'; renderSuggestions(); return;}
    const openingProject = project(), openingBoard = cache.board, serial = ++batchOpenRevision;
    const openingSelection = JSON.stringify(suggestions.map(value => [value.id, value.snapshot_token]));
    try {await window.reimbursementPeopleUI?.refresh(openingProject);} catch (_) { /* Unassigned legacy groups remain usable; person conflicts still require a selection. */ }
    if (serial !== batchOpenRevision || batch || deskBusy || !$('matchingDeskDialog')?.open || project() !== openingProject || cache.board !== openingBoard ||
      JSON.stringify(selectedCandidates().map(value => [value.id, value.snapshot_token])) !== openingSelection) return;
    batch = {projectId: project(), types: cache.board?.report_types || [], busy: false, committed: false, expired: false,
      ownerConflicts: suggestions.map(value => {const group = members(value); return window.reimbursementPeopleUI?.choice([...group.vouchers, ...group.payments, ...group.supports]).conflict || false;}),
      idempotencyKey: crypto.randomUUID(), fingerprint: '', groups: suggestions.map(value => {
        const group = members(value);
        return {suggestion_id: value.id, snapshot_token: value.snapshot_token, voucher_keys: [...value.voucher_keys],
          payment_ids: [...value.payment_ids], ...(value.support_ids?.length ? {support_ids: [...value.support_ids]} : {}), revisions: value.revisions || Object.fromEntries([...group.vouchers, ...group.payments, ...group.supports]
            .filter(card => card.key?.startsWith('q:')).map(card => [card.id, card.revision])),
          expected_versions: value.expected_versions || Object.fromEntries(group.vouchers.filter(card => card.key?.startsWith('e:')).map(card => [card.item_id || card.id, card.version]))};
      })};
    const dialog = document.createElement('dialog'); dialog.id = 'synthesisBatchDialog'; dialog.className = 'synthesis-batch-dialog';
    dialog.innerHTML = `<div class="dialoghead"><h2>逐组确认报销信息 · ${suggestions.length} 组</h2><button type="button" data-batch-close aria-label="关闭合成" class="dialog-close">×</button></div><p>每组确认项目类型、金额和项目名称。点击下方确认后，整批创建并进入待核对。</p><p id="synthesisBatchStatus" role="status" hidden></p><form id="synthesisBatchForm" novalidate>${suggestions.map((suggestion, index) => {
      const group = members(suggestion), fields = batchChoice(suggestion);
      const reviewNotes = window.synthesisReviewNotes(suggestion);
      return `<section class="synthesis-batch-group"><h3>第 ${index + 1} 组 · ${esc(title(suggestion))}</h3>${window.reimbursementPeopleUI?.groupMarkup([...group.vouchers,...group.payments,...group.supports])||''}${reviewNotes.length ? `<div class="rule warn">${esc(reviewNotes.join('；'))}</div>` : ''}${window.reimbursementPeopleUI?`<div class="reimbursement-person-control"><label>费用归属人<select data-batch-person="${index}">${window.reimbursementPeopleUI.options(fields.reimbursement_person_id)}</select></label><button type="button" data-new-batch-person="${index}">新增费用归属人</button></div>${batch.ownerConflicts[index]?'<p class="reimbursement-person-message">所选材料包含不同或未指定的费用归属人，请选择费用归属人；材料将按确认的费用归属人记录。</p>':''}`:''}<div class="synthesis-batch-fields"><label>项目类型<select data-batch-type="${index}" required><option value="">选择项目类型</option>${batch.types.map(type => `<option value="${esc(type.code)}" ${type.code === fields.project_type ? 'selected' : ''}>${esc(type.label)}</option>`).join('')}</select></label><label>金额（元）<input data-batch-amount="${index}" required inputmode="decimal" value="${esc(fields.amount)}" maxlength="20"></label><label class="wide">项目名称<span data-batch-name-control="${index}">${batchNameControl(index, fields)}</span></label><p data-batch-error="${index}" role="alert" hidden></p></div><details class="synthesis-batch-materials" open><summary>材料预览 · ${group.vouchers.length} 份票据 ＋ ${group.payments.length} 份付款${group.supports.length ? ` ＋ ${group.supports.length} 份行程／明细` : ''}</summary><h4>发票／收据</h4><div class="synthesis-proofs">${group.vouchers.map(proof).join('')}</div><h4>付款截图</h4><div class="synthesis-proofs synthesis-payment-proofs">${group.payments.map(proof).join('')}</div>${group.supports.length ? `<h4>行程／明细</h4><div class="synthesis-proofs">${group.supports.map(proof).join('')}</div>` : ''}</details></section>`;
    }).join('')}<div class="actions synthesis-batch-actions"><button id="confirmSynthesisBatch" type="submit" class="primary" disabled>确认合成并进入待核对</button><button type="button" data-batch-close>取消</button></div></form>`;
    document.body.append(dialog);
    const close = () => {if (batch && !batch.busy) {const committed = batch.committed; dialog.close(); dialog.remove(); batch = null; if (committed) window.matchingSuggestionActions?.closeAfterCommit();}};
    dialog.querySelectorAll('[data-batch-close]').forEach(button => button.onclick = close);
    dialog.querySelectorAll('[data-new-batch-person]').forEach(button => button.onclick = () => {
      const index = Number(button.dataset.newBatchPerson), projectId = batch.projectId;
      window.reimbursementPeopleUI?.newPerson({projectId, onCreated: person => {
        if (!batch || batch.projectId !== projectId || !dialog.isConnected) return;
        for (const select of dialog.querySelectorAll('[data-batch-person]')) select.innerHTML = window.reimbursementPeopleUI.options(Number(select.dataset.batchPerson) === index ? person.id : select.value);
        updateBatchSubmit();
      }});
    });
    dialog.addEventListener('cancel', event => {event.preventDefault(); close();});
    dialog.addEventListener('change', event => {
      if (event.target.dataset.batchType !== undefined) {
        const index = Number(event.target.dataset.batchType), fields = batchFields(index); fields.project_name = '';
        dialog.querySelector(`[data-batch-name-control="${index}"]`).innerHTML = batchNameControl(index, fields);
      }
      updateBatchSubmit();
    });
    dialog.addEventListener('input', updateBatchSubmit);
    dialog.querySelector('form').onsubmit = async event => {
      event.preventDefault(); if (!batch || batch.busy || batch.committed) return;
      if (batch.expired || batch.projectId !== project()) {showBatchStatus('候选或项目已变化，请关闭后重新勾选当前候选。', true); updateBatchSubmit(); return;}
      const groups = batch.groups.map((group, index) => ({...group, fields: batchFields(index)}));
      const invalid = groups.map((group, index) => batchFieldError(group.fields, index)).find(Boolean);
      if (invalid) {showBatchStatus(invalid, true); updateBatchSubmit(); return;}
      const fingerprint = JSON.stringify(groups);
      if (batch.fingerprint && batch.fingerprint !== fingerprint) batch.idempotencyKey = crypto.randomUUID();
      batch.fingerprint = fingerprint; batchLock(true); showBatchStatus('正在确认整批材料…');
      try {
        const result = await api('material-matching/create-batch', {project_id: batch.projectId, idempotency_key: batch.idempotencyKey, groups});
        batch.committed = true;
        if (!Array.isArray(result.items) || result.items.length !== groups.length || result.created !== groups.length) {
          showBatchStatus('服务已返回成功，但结果数量未能核对。请关闭并查看待核对列表，勿重复提交。', true); return;
        }
        checkedCandidates.clear(); selected = ''; selectionNotice = ''; cache.loaded = false;
        view = 'review'; active = result.items[0].id; checked.clear();
        showBatchStatus(`${groups.length} 组已合成并进入待核对。`);
        try {await load(); notify(`${groups.length} 组已合成并进入待核对。`); window.showCreatedMaterialExpenses(result.items); dialog.close(); dialog.remove(); batch = null; window.matchingSuggestionActions.closeAfterCommit();}
        catch (error) {showBatchStatus(`已完成 ${groups.length} 组，页面刷新未完成：${error.message}。关闭后查看待核对，勿重复提交。`, true);}
      } catch (error) {
        showBatchStatus(`本次未能确认整批：${error.message}。表单已保留，可检查后重试。`, true);
        if (/版本|过期|snapshot|变化|材料池|项目/.test(error.message)) {
          batch.expired = true;
          try {await refresh();} catch (_) { /* Keep the original fields for manual comparison. */ }
          showBatchStatus(`材料版本或项目已变化：${error.message}。原表单保留供对照，请关闭后重新勾选。`, true);
        }
      } finally {if (batch) batchLock(false);}
    };
    dialog.showModal(); batchLock(false);
  }
  function renderSuggestions() {
    const list = $('matchingSuggestionsList'); if (!list) return;
    const suggestions = filtered(), chosen = selectedCandidates(), conflict = overlapping(chosen);
    $('matchingSuggestionSummary').textContent = loading ? '正在读取…' : `${suggestions.length} 组 · 已勾选 ${chosen.length} 组`;
    const truncated = current() && cache.board?.suggestions_truncated ? '<p class="muted">候选较多，仅展示前100组；其余材料仍在下方。</p>' : '';
    $('matchingSuggestionStatus').innerHTML = `${selectionNotice || conflict ? `<p class="synthesis-selection-status">${esc(conflict || selectionNotice)}</p>` : ''}${cache.error ? `<p class="rule warn">${esc(cache.error)}</p>` : ''}${truncated}`;
    $('matchingSuggestionTools').innerHTML = `<div class="synthesis-tools"><label class="synthesis-select-all"><input id="selectSynthesisVisible" type="checkbox" ${suggestions.length && chosen.length === suggestions.length ? 'checked' : ''}>全选当前组合</label><button id="confirmSelectedSynthesis" type="button" class="primary">确认选中组合</button></div>`;
    list.innerHTML = suggestions.length ? suggestions.map(suggestion => {
      const group = members(suggestion);
      const reviewNotes = window.synthesisReviewNotes(suggestion);
      return `<article class="synthesis-row synthesis-selectable-row ${selected === suggestion.id ? 'selected' : ''}" data-synthesis-id="${esc(suggestion.id)}"><input type="checkbox" data-synthesis-check="${esc(suggestion.id)}" aria-label="勾选 ${esc(title(suggestion))}" ${checkedCandidates.get(suggestion.id) === suggestion.snapshot_token ? 'checked' : ''}><button type="button" class="synthesis-row-preview"><span class="title">${esc(title(suggestion))}</span>${window.reimbursementPeopleUI?.groupMarkup([...group.vouchers,...group.payments,...group.supports])||''}<span class="sub">${group.vouchers.length} 份票据 ＋ ${group.payments.length} 份付款${group.supports.length ? ` ＋ ${group.supports.length} 份行程／明细` : ''}</span></button><div class="money">¥ ${money(suggestion.amount_cents)}<br><span class="badge">${suggestion.ambiguous ? '存在竞争组合' : suggestion.needs_name_review ? '名称待核对' : '建议合成'}</span></div>${reviewNotes.length ? `<div class="synthesis-row-reason sub">${esc(reviewNotes.join('；'))}</div>` : ''}</article>`;
    }).join('') : `<p class="matching-empty">${loading ? '正在读取建议组合…' : cache.error ? '建议暂不可用，请刷新材料重试。' : '暂未找到建议组合，可点击“寻找合成组合”，或从下方手工选择材料。'}</p>`;
    const all = $('selectSynthesisVisible'); all.indeterminate = chosen.length > 0 && chosen.length < suggestions.length;
    all.onchange = event => {selectionNotice = ''; for (const suggestion of suggestions) {
      if (event.target.checked) checkedCandidates.set(suggestion.id, suggestion.snapshot_token); else checkedCandidates.delete(suggestion.id);
    } renderSuggestions();};
    $('confirmSelectedSynthesis').onclick = openBatch;
    lockSuggestions(deskBusy);
  }
  function lockSuggestions(value) {
    deskBusy = value;
    const container = $('matchingSuggestions'); if (!container) return;
    for (const control of container.querySelectorAll('button,input')) control.disabled = value || loading;
    if ($('selectSynthesisVisible')) $('selectSynthesisVisible').disabled = value || loading || !filtered().length;
    if ($('confirmSelectedSynthesis')) $('confirmSelectedSynthesis').disabled = value || loading || !selectedCandidates().length || !!overlapping(selectedCandidates());
  }
  window.lockSynthesisSuggestions = lockSuggestions;
  window.renderSynthesisSuggestions = renderSuggestions;
  document.addEventListener('change', event => {
    if (!event.target.matches('#matchingSuggestions [data-synthesis-check]') || deskBusy) return;
    const suggestion = filtered().find(value => value.id === event.target.dataset.synthesisCheck); if (!suggestion) return;
    if (event.target.checked) checkedCandidates.set(suggestion.id, suggestion.snapshot_token); else checkedCandidates.delete(suggestion.id);
    selectionNotice = ''; renderSuggestions();
    [...$('matchingSuggestionsList').querySelectorAll('[data-synthesis-check]')].find(input => input.dataset.synthesisCheck === suggestion.id)?.focus();
  });
  document.addEventListener('click', event => {
    const row = event.target.closest('#matchingSuggestions [data-synthesis-id]');
    if (!row || deskBusy || event.target.matches('[data-synthesis-check]')) return;
    const suggestion = filtered().find(value => value.id === row.dataset.synthesisId);
    if (suggestion && window.matchingSuggestionActions?.select(suggestion)) {selected = suggestion.id; renderSuggestions();}
  });
  renderSuggestions();
})();
