'use strict';
// Expenses keep their person attribution while sharing one project/company form.
(() => {
  let cache = {projectId: '', people: [], retiredPeople: [], summary: null}, request = 0, bulkOpening = false, closeNamePopup = null;
  const project = () => window.currentInvoiceProjectId?.() || '';
  const label = item => (cache.projectId === project() ? cache.people.find(person => person.id === item?.reimbursement_person_id)?.label : '') || item?.reimbursement_person_label || '';
  const options = selected => `<option value="">未指定</option>${cache.people.map(person => `<option value="${esc(person.id)}" ${person.id === selected ? 'selected' : ''}>${esc(person.label)}</option>`).join('')}`;
  const activePeople = (projectId = project()) => cache.projectId === projectId ? cache.people : [];
  const filterOptions = (selected = '') => `<option value="">全部人员</option><option value="__unassigned__" ${selected === '__unassigned__' ? 'selected' : ''}>未指定</option>${activePeople().map(person => `<option value="${esc(person.id)}" ${person.id === selected ? 'selected' : ''}>${esc(person.label)}</option>`).join('')}`;
  const matchesPerson = (record, value) => {
    const id = !record?.reimbursement_person_conflict && !record?.reimbursement_person_pending && activePeople().some(person => person.id === record?.reimbursement_person_id) ? record.reimbursement_person_id : '';
    return !value || (value === '__unassigned__' ? !id : id === value);
  };
  const markup = item => {
    const text = label(item);
    return text || cache.projectId === project() && cache.people.length ? `<small class="reimbursement-person-label">费用归属人：${esc(text || '未指定')}</small>` : '';
  };
  function choice(cards) {
    const ids = new Set(cards.map(card => card.reimbursement_person_id || ''));
    return {personId: ids.size === 1 ? [...ids][0] : '', conflict: ids.size > 1};
  }
  function choiceControl(cards, selected, attribute) {
    const source = choice(cards), text = source.conflict ? '所选材料包含不同或未指定的费用归属人，请选择费用归属人；材料将按确认的费用归属人记录。' : '按费用归属人记录；公司报销表仍统一制作。';
    return `<div class="reimbursement-person-control"><label>费用归属人<select ${attribute}>${options(selected || source.personId)}</select></label><button type="button" data-new-reimbursement-person>新增费用归属人</button></div><p class="reimbursement-person-message">${text}</p>`;
  }
  function groupMarkup(cards) {
    const source = choice(cards);
    return source.conflict ? '<small class="reimbursement-person-label">费用归属人：需确认</small>' : markup(cards.find(card => card.reimbursement_person_id === source.personId) || {});
  }
  async function refresh(projectId = project()) {
    const serial = ++request;
    if (!projectId) {cache = {projectId: '', people: [], retiredPeople: [], summary: null}; return cache;}
    const response = await fetch('/api/reimbursement-people?' + new URLSearchParams({project: projectId}));
    const result = await response.json();
    if (!response.ok) throw Error(result.error || '无法读取人员金额');
    if (serial !== request || projectId !== project()) return cache;
    if (result.project_id && result.project_id !== projectId) throw Error('人员名单属于另一项目，请重新打开。');
    const seen = new Set();
    const people = (result.people || []).filter(person => person.id && person.label && person.label !== '未指定' && (!person.state || person.state === 'active') && (!person.project_id || person.project_id === projectId) && !seen.has(person.id) && seen.add(person.id));
    const retiredPeople = (result.retired_people || []).filter(person => person.id && person.state === 'retired' && (!person.project_id || person.project_id === projectId));
    cache = {projectId, people, retiredPeople, summary: result.summary || null};
    document.dispatchEvent(new CustomEvent('reimbursement-people-changed', {detail: {projectId, people}}));
    const button = $('reimbursementPeople');
    if (button) {button.hidden = false; button.disabled = false;}
    return cache;
  }
  function newPerson({projectId = project(), onCreated} = {}) {
    if (!projectId || projectId !== project()) return;
    const dialog = document.createElement('dialog'); dialog.id = 'reimbursementPersonCreate'; dialog.className = 'reimbursement-people-dialog';
    dialog.innerHTML = `<div class="dialoghead"><h2>选择或新增费用归属人</h2><button type="button" data-close class="dialog-close" aria-label="关闭">×</button></div><section class="reimbursement-existing-person"><label>已有人员<select name="existing_person"><option value="">选择已有人员</option>${cache.people.map(person => `<option value="${esc(person.id)}">${esc(person.label)}</option>`).join('')}</select></label><button type="button" data-use-existing disabled>使用选中人员</button><p class="muted">同一个人已有其他昵称时，可直接选择名单中的人员；不同昵称的已有记录可在人员金额表归并。</p></section><form novalidate><h3>独立新增人员</h3><label>姓名／昵称<input name="label" maxlength="100" required placeholder="用于记账和回款后分发"></label><p data-error role="alert" hidden></p><div class="actions"><button type="submit" class="primary" disabled>新增</button><button type="button" data-close>取消</button></div></form>`;
    document.body.append(dialog); dialog.showModal();
    const form = dialog.querySelector('form'), save = form.querySelector('[type=submit]'), existing = dialog.querySelector('[name=existing_person]'), useExisting = dialog.querySelector('[data-use-existing]'); let busy = false, created = null;
    const validName = () => form.elements.label.value.trim() && form.elements.label.value.trim() !== '未指定' && form.elements.label.value.trim().length <= 100;
    const update = () => {save.disabled = busy || !validName(); useExisting.disabled = busy || !!created || !cache.people.some(person => person.id === existing.value); existing.disabled = busy || !!created;};
    form.oninput = update;
    existing.onchange = update;
    useExisting.onclick = async () => {
      if (busy || created || projectId !== project()) return;
      const selected = cache.people.find(person => person.id === existing.value); if (!selected) return;
      busy = true; update();
      try {dialog.remove(); await onCreated?.(selected);} catch (error) {notify('已选择已有人员；页面更新失败：' + error.message, true);} finally {busy = false;}
    };
    const close = () => {if (!busy) dialog.remove();};
    dialog.querySelectorAll('[data-close]').forEach(button => button.onclick = close);
    dialog.oncancel = event => {event.preventDefault(); close();};
    form.onsubmit = async event => {
      event.preventDefault(); if (busy || !validName()) return;
      busy = true; dialog.querySelectorAll('button,input,select').forEach(control => control.disabled = true);
      try {
        if (projectId !== project()) throw Error('项目已变化，请关闭后重新打开。');
        if (!created) {const result = await api('reimbursement-people/create', {project_id: projectId, label: form.elements.label.value.trim()}); created = result.person; if (!created?.id) throw Error('新增结果无法确认，请刷新人员列表。');}
        await refresh(projectId); dialog.remove(); await onCreated?.(created);
      } catch (error) {
        if (!dialog.isConnected) {notify('人员已新增；页面刷新失败：' + error.message, true); return;}
        const target = dialog.querySelector('[data-error]'); target.textContent = error.message + (created ? '；人员已经新增，可重试刷新。' : '；输入已保留。'); target.hidden = false;
      } finally {busy = false; if (dialog.isConnected) {dialog.querySelectorAll('button,input,select').forEach(control => control.disabled = false); update();}}
    };
  }
  function personAction(person, action, onChanged, {targetId = ''} = {}) {
    const projectId = project();
    if (!projectId || !person?.id || !person.version || document.getElementById('reimbursementPersonAction')) return;
    const targets = cache.people.filter(target => target.id !== person.id && target.version);
    if (action === 'merge' && !targets.length || action === 'retire' && person.can_retire !== true || action === 'restore' && person.can_restore !== true) return;
    const title = {rename: '修改姓名', merge: '归并到已有人员', retire: '移除空人员', restore: '恢复人员'}[action];
    const rowFor = id => cache.summary?.rows?.find(row => row.person_id === id) || {expense_count: 0, total_cents: 0};
    const sourceRow = rowFor(person.id);
    const dialog = document.createElement('dialog'); dialog.id = 'reimbursementPersonAction'; dialog.className = 'reimbursement-people-dialog';
    dialog.innerHTML = `<div class="dialoghead"><h2>${title}</h2><button type="button" data-close class="dialog-close" aria-label="关闭">×</button></div><p>当前人员：<strong>${esc(person.label)}</strong></p><form novalidate>${action === 'rename' ? `<label>姓名／昵称<input name="label" maxlength="100" required value="${esc(person.label)}"></label><p class="muted">修改后，已有费用与材料的姓名同步更新；费用金额和原件保持。</p>` : action === 'merge' ? `<label>归并到已有人员<select name="target_person_id"><option value="">选择目标人员</option>${targets.map(target => `<option value="${esc(target.id)}" ${target.id === targetId ? 'selected' : ''}>${esc(target.label)}</option>`).join('')}</select></label><p class="muted">将此昵称的费用、材料归属统一到目标人员；原昵称退出有效名单，费用金额和原件保持。</p>` : `<p>${action === 'retire' ? '此人员没有费用或材料引用。移除后退出有效名单，可在“已移除人员”恢复。' : '恢复后重新加入本项目的有效名单，已有费用金额保持。'}</p>`}<div data-person-impact class="reimbursement-person-impact" role="status"></div><p data-error role="alert" hidden></p><div class="actions"><button type="submit" class="primary" disabled>${action === 'merge' ? '确认归并' : action === 'rename' ? '保存姓名' : action === 'retire' ? '确认移除' : '确认恢复'}</button><button type="button" data-refresh-confirmed hidden>刷新人员金额</button><button type="button" data-close>取消</button></div></form>`;
    document.body.append(dialog); dialog.showModal();
    const form = dialog.querySelector('form'), save = form.querySelector('[type=submit]'), impact = dialog.querySelector('[data-person-impact]'), errorTarget = form.querySelector('[data-error]'), refreshConfirmed = form.querySelector('[data-refresh-confirmed]');
    let busy = false, expired = false, committed = false;
    const targetPerson = () => targets.find(target => target.id === form.elements.target_person_id?.value);
    const update = () => {
      const target = targetPerson(), targetRow = target ? rowFor(target.id) : null;
      impact.innerHTML = `<p>${esc(person.label)}：当前登记 ${sourceRow.expense_count || 0} 笔，¥ ${money(sourceRow.total_cents || 0)}；全部费用引用 ${person.expense_count || 0} 笔、材料引用 ${person.material_count || 0} 份。</p>${action === 'merge' && target ? `<p>选中目标：<strong>${esc(target.label)}</strong>，当前登记 ${targetRow.expense_count || 0} 笔，¥ ${money(targetRow.total_cents || 0)}。</p><p>归并后 ${esc(target.label)}：登记 ${(sourceRow.expense_count || 0) + (targetRow.expense_count || 0)} 笔，¥ ${money((sourceRow.total_cents || 0) + (targetRow.total_cents || 0))}；本项目总额保持。</p>` : ''}`;
      const valid = action === 'merge' ? !!target : action === 'rename' ? form.elements.label.value.trim() && form.elements.label.value.trim() !== '未指定' && form.elements.label.value.trim().length <= 100 : true;
      save.disabled = busy || expired || committed || !valid;
    };
    form.oninput = update; form.onchange = update; update();
    const close = () => {if (!busy) dialog.remove();}; dialog.querySelectorAll('[data-close]').forEach(button => button.onclick = close); dialog.oncancel = event => {event.preventDefault(); close();};
    const finish = async () => {await refresh(projectId); await load(); await onChanged?.(); dialog.remove(); notify(title + '已完成，费用金额和原件保持。');};
    refreshConfirmed.onclick = async () => {
      if (busy || !committed) return; busy = true; refreshConfirmed.disabled = true;
      try {await finish();} catch (error) {errorTarget.textContent = '操作已完成；刷新失败：' + error.message + '。可再刷新，无需重复保存。'; errorTarget.hidden = false;}
      finally {busy = false; if (dialog.isConnected) {refreshConfirmed.disabled = false; update();}}
    };
    form.onsubmit = async event => {
      event.preventDefault(); update(); if (save.disabled) return;
      const target = targetPerson();
      busy = true; dialog.querySelectorAll('button,input,select').forEach(control => control.disabled = true);
      try {
        if (projectId !== project()) throw Error('项目已变化，请关闭后重新打开。');
        const body = {project_id: projectId, person_id: person.id, expected_version: person.version,
          ...(action === 'rename' ? {label: form.elements.label.value.trim()} : action === 'merge' ? {target_person_id: target.id, target_expected_version: target.version} : {})};
        const result = await api('reimbursement-people/' + action, body); committed = true;
        if (result.person?.id !== (action === 'merge' ? target.id : person.id)) throw Error('操作结果无法确认，请刷新人员金额。');
        await finish();
      } catch (error) {
        expired ||= error.status === 409 || error.status === 403 || /版本|变化|项目|已归并|已移除/.test(error.message);
        errorTarget.textContent = committed ? '操作已完成；' + error.message + '。请刷新人员金额，勿重复保存。' : error.message + (expired ? '；输入保留供对照，请关闭后重新打开当前人员。' : '；输入和选择已保留，可稍后重试。'); errorTarget.hidden = false;
        refreshConfirmed.hidden = !committed;
      } finally {
        busy = false; if (dialog.isConnected) {dialog.querySelectorAll('button,input,select').forEach(control => control.disabled = false); update();}
      }
    };
  }
  function inlineName(person, form, onChanged) {
    const projectId = project(), input = form.elements.label, combo = form.querySelector('.reimbursement-name-combo'), toggle = form.querySelector('[data-name-toggle]'), list = form.querySelector('[role=listbox]'), hint = form.querySelector('[data-name-hint]'), save = form.querySelector('[type=submit]'), target = form.querySelector('[data-error]');
    const candidates = cache.people.filter(value => value.version).map(value => ({...value}));
    let busy = false, expired = false, committed = false, visible = [], highlighted = -1, showingAll = false;
    const closeOptions = () => {list.hidden = true; input.setAttribute('aria-expanded', 'false'); input.removeAttribute('aria-activedescendant'); highlighted = -1; showingAll = false; document.removeEventListener('pointerdown', dismissOptions, true); window.removeEventListener('resize', positionOptions); window.removeEventListener('scroll', positionOptions, true); if (closeNamePopup === closeOptions) closeNamePopup = null;};
    function dismissOptions(event) {if (!combo.contains(event.target)) closeOptions();}
    function positionOptions() {
      if (list.hidden || !form.isConnected) {closeOptions(); return;}
      const anchor = input.closest('.reimbursement-name-combo').getBoundingClientRect(), panel = form.closest('dialog').getBoundingClientRect();
      const leftEdge = Math.max(8, panel.left + 8), rightEdge = Math.min(innerWidth - 8, panel.right - 8), topEdge = Math.max(8, panel.top + 8), bottomEdge = Math.min(innerHeight - 8, panel.bottom - 8);
      if (anchor.right <= leftEdge || anchor.left >= rightEdge || anchor.bottom <= topEdge || anchor.top >= bottomEdge) {closeOptions(); return;}
      list.style.width = Math.min(anchor.width, rightEdge - leftEdge) + 'px';
      list.style.left = Math.max(leftEdge, Math.min(anchor.left, rightEdge - parseFloat(list.style.width))) + 'px'; list.style.right = 'auto';
      const below = Math.max(0, bottomEdge - anchor.bottom - 3), above = Math.max(0, anchor.top - topEdge - 3), desired = Math.min(190, list.scrollHeight + 2), upwards = below < desired && above > below;
      const height = Math.min(desired, upwards ? above : below);
      list.style.maxHeight = height + 'px'; list.style.top = (upwards ? anchor.top - height - 3 : anchor.bottom + 3) + 'px';
    }
    const update = () => {
      const name = input.value.trim(), dirty = name !== person.label, existing = candidates.some(value => value.id !== person.id && value.label === name);
      hint.textContent = existing ? '此姓名已存在，请从下拉名单选择该人员并确认归并。' : ''; hint.hidden = !existing;
      save.hidden = !dirty && !committed; save.textContent = committed ? '刷新名单' : '保存'; save.disabled = busy || expired || !person.version || !committed && (!dirty || !name || name === '未指定' || name.length > 100 || existing);
      toggle.disabled = busy || expired || committed || !person.version;
    };
    const highlight = index => {
      highlighted = index;
      list.querySelectorAll('[role=option]').forEach((option, position) => option.setAttribute('aria-selected', String(position === index)));
      const option = list.querySelectorAll('[role=option]')[index];
      if (option) {input.setAttribute('aria-activedescendant', option.id); if (option.offsetTop < list.scrollTop) list.scrollTop = option.offsetTop; else if (option.offsetTop + option.offsetHeight > list.scrollTop + list.clientHeight) list.scrollTop = option.offsetTop + option.offsetHeight - list.clientHeight;} else input.removeAttribute('aria-activedescendant');
    };
    const openOptions = (all = false) => {
      if (busy || expired || committed || !person.version) return;
      const text = input.value.trim(); visible = candidates.filter(value => all || text === person.label || value.label.includes(text));
      showingAll = all || text === person.label;
      list.innerHTML = visible.map((value, index) => `<li role="option" id="${esc(list.id + '-' + index)}" aria-selected="false" data-person-option="${esc(value.id)}">${esc(value.label)}${value.id === person.id ? '<small>当前人员</small>' : ''}</li>`).join('') || '<li class="reimbursement-name-empty">没有匹配人员，可填写新姓名后保存。</li>';
      if (closeNamePopup && closeNamePopup !== closeOptions) closeNamePopup(); closeNamePopup = closeOptions;
      list.hidden = false; list.scrollTop = 0; input.setAttribute('aria-expanded', 'true'); highlighted = -1; input.removeAttribute('aria-activedescendant'); positionOptions(); if (!list.hidden) document.addEventListener('pointerdown', dismissOptions, true); window.addEventListener('resize', positionOptions); window.addEventListener('scroll', positionOptions, true);
    };
    const choosePerson = id => {
      if (busy || expired || committed) return;
      const selected = visible.find(value => value.id === id); if (!selected) return;
      closeOptions();
      if (selected.id === person.id) {input.value = person.label; update(); input.focus(); return;}
      personAction(person, 'merge', onChanged, {targetId: selected.id});
    };
    input.oninput = () => {update(); openOptions();};
    input.onfocus = () => openOptions();
    input.onclick = () => {if (list.hidden) openOptions();};
    input.onkeydown = event => {
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {event.preventDefault(); if (list.hidden || event.altKey) openOptions(true); if (visible.length) highlight(event.key === 'ArrowDown' ? (highlighted + 1) % visible.length : (highlighted <= 0 ? visible.length : highlighted) - 1);}
      else if (event.key === 'Enter' && !list.hidden && highlighted >= 0) {event.preventDefault(); choosePerson(visible[highlighted].id);}
      else if (event.key === 'Escape') {event.preventDefault(); event.stopPropagation(); if (!list.hidden) closeOptions(); else if (!busy && !committed) {input.value = person.label; target.hidden = true; update();}}
      else if (event.key === 'Tab') closeOptions();
    };
    toggle.onpointerdown = event => event.preventDefault(); toggle.onclick = () => {const shouldClose = !list.hidden && showingAll; input.focus(); if (shouldClose) closeOptions(); else openOptions(true);};
    list.onpointerdown = event => event.preventDefault(); list.onclick = event => {const option = event.target.closest('[data-person-option]'); if (option) choosePerson(option.dataset.personOption);};
    form.onfocusout = event => {if (!form.contains(event.relatedTarget)) closeOptions();};
    update();
    form.onsubmit = async event => {
      event.preventDefault(); update(); if (save.disabled) return;
      busy = true; closeOptions(); input.disabled = true; toggle.disabled = true; save.disabled = true;
      try {
        if (projectId !== project()) throw Error('项目已变化，请重新打开人员金额。');
        if (!committed) {const result = await api('reimbursement-people/rename', {project_id: projectId, person_id: person.id, expected_version: person.version, label: input.value.trim()}); committed = true; if (result.person?.id !== person.id) throw Error('改名结果无法确认，请刷新名单。');}
        await refresh(projectId); await load(); await onChanged(); notify('姓名已保存，费用金额和原件保持。');
      } catch (error) {
        expired ||= !committed && (error.status === 409 || error.status === 403 || /版本|变化|项目|已归并|已移除/.test(error.message));
        target.textContent = committed ? '姓名已保存；' + error.message + '。请刷新名单，勿重复保存。' : error.message + (expired ? '；输入保留供对照，请刷新人员金额后重新编辑。' : '；输入已保留，可稍后重试。'); target.hidden = false;
      } finally {busy = false; if (form.isConnected) {input.disabled = committed; update();}}
    };
  }
  const allocationStage = item => ['submitted', 'reimbursed'].includes(item.stage) ? 'packaged' : item.ready ? 'ready' : 'pending';
  const allocationLabels = {all: '登记费用', pending: '待核对', ready: '可提交', packaged: '已制包'};
  function allocationDetails(data, projectId, personId, scope) {
    if (!Array.isArray(data.items) || !(data.projects || []).some(value => value.id === projectId)) throw Error('项目或费用列表已变化，请关闭后重新打开人员金额。');
    const seen = new Set();
    // Keep the same exclusion, first-ID and stage precedence as person_allocation.
    return data.items.filter(item => {
      if (item.project_id !== projectId || seen.has(item.id) || item.deleted || item.merged_into || item.stage === 'cancelled' || item.returned_to_synthesis || ['archived', 'deleted'].includes(item.synthesis_material_state)) return false;
      seen.add(item.id);
      return String(item.reimbursement_person_id || '') === personId && (scope === 'all' || allocationStage(item) === scope);
    });
  }
  const summaryNumber = (row, scope, count = false) => {
    const personId = String(row.person_id || ''), name = row.label || '未指定';
    const value = count ? String(row.expense_count || 0) : '¥ ' + money(row[{all: 'total_cents', pending: 'pending_review_cents', ready: 'ready_cents', packaged: 'packaged_cents'}[scope]] || 0);
    return `<button type="button" class="reimbursement-amount-link" data-person-details="${esc(personId)}" data-allocation-scope="${scope}" data-allocation-count="${count}" aria-label="查看${esc(name)}的${allocationLabels[scope]}明细，${value}${count ? '笔' : ''}">${value}</button>`;
  };
  async function openSummary() {
    const projectId = project(); if (!projectId) return;
    if ($('reimbursementPeopleDialog')?.open) return;
    const dialog = document.createElement('dialog'); dialog.id = 'reimbursementPeopleDialog'; dialog.className = 'reimbursement-people-dialog reimbursement-summary';
    dialog.innerHTML = '<div class="dialoghead"><h2>人员金额</h2><button type="button" data-close class="dialog-close" aria-label="关闭">×</button></div><div data-content></div>';
    let revision = 0;
    const close = () => {revision++; closeNamePopup?.(); dialog.remove();};
    document.body.append(dialog); dialog.showModal(); dialog.querySelector('[data-close]').onclick = close;
    dialog.oncancel = event => {event.preventDefault(); close();};
    async function drawDetails(personId, personLabel, scope, returnTo) {
      const serial = ++revision, content = dialog.querySelector('[data-content]');
      const current = () => serial === revision && dialog.isConnected && projectId === project();
      closeNamePopup?.(); dialog.querySelector('h2').textContent = `${personLabel} · ${allocationLabels[scope]}明细`;
      content.setAttribute('aria-busy', 'true');
      content.innerHTML = `<div class="reimbursement-details-navigation"><button type="button" data-back-summary>← 返回人员汇总</button></div><div data-detail-content aria-live="polite">${dialogLoadingMarkup('正在读取最新费用明细…')}</div>`;
      const back = content.querySelector('[data-back-summary]');
      back.onclick = () => draw(returnTo); back.focus();
      const target = content.querySelector('[data-detail-content]');
      try {
        const response = await fetch('/api/state'), data = await response.json();
        if (!current()) return;
        if (!response.ok) throw Error(data.error || '无法读取费用明细');
        const items = allocationDetails(data, projectId, personId, scope), total = items.reduce((sum, item) => sum + item.amount_cents, 0);
        const types = data.report_types || [];
        target.innerHTML = `<div class="reimbursement-summary-total"><strong>合计 ¥ ${money(total)}</strong><span>${items.length} 笔费用</span></div><p class="muted">按最新登记费用计算；点击费用名称可查看材料和打印页。</p>${items.length ? `<div class="reimbursement-summary-table"><table class="reimbursement-details-table"><thead><tr><th scope="col">费用名称</th><th scope="col">项目类型</th><th scope="col">状态</th><th scope="col">金额</th></tr></thead><tbody>${items.map(item => {
          const type = types.find(value => value.code === (item.project_type || item.report_choice?.project_type));
          return `<tr><td><button type="button" class="reimbursement-expense-link" data-open-person-expense="${esc(item.id)}">${esc(item.title || item.project_name || item.id)}</button></td><td>${esc(type?.label || item.category || '待确认')}</td><td>${allocationLabels[allocationStage(item)]}${item.amount_pending ? ' · 金额待确认' : ''}</td><td>¥ ${money(item.amount_cents)}</td></tr>`;
        }).join('')}</tbody></table></div>` : '<div class="empty reimbursement-details-empty"><p>当前口径下暂无费用。</p></div>'}<p data-open-expense-error role="alert" hidden></p>`;
        let opening = false;
        for (const button of target.querySelectorAll('[data-open-person-expense]')) button.onclick = async () => {
          if (opening || !current()) return;
          const error = target.querySelector('[data-open-expense-error]'); error.hidden = true;
          opening = true; button.disabled = true;
          try {
            if (!window.openReimbursementExpense) throw Error('费用详情入口尚未就绪，请稍后重试。');
            const opened = await window.openReimbursementExpense(button.dataset.openPersonExpense, personId, {current});
            if (opened && current()) close();
          } catch (failure) {if (current()) {error.textContent = failure.message; error.hidden = false;}}
          finally {opening = false; if (button.isConnected) button.disabled = false;}
        };
      } catch (error) {if (current()) {
        target.innerHTML = dialogLoadErrorMarkup(error.message);
        target.querySelector('[data-dialog-retry]').onclick = () => drawDetails(personId, personLabel, scope, returnTo);
      }} finally {if (current()) content.setAttribute('aria-busy', 'false');}
    }
    async function draw(returnTo = null) {
      const serial = ++revision;
      const content = dialog.querySelector('[data-content]');
      closeNamePopup?.();
      dialog.querySelector('h2').textContent = '人员金额';
      content.setAttribute('aria-busy', 'true');
      content.innerHTML = dialogLoadingMarkup('正在读取人员金额…');
      try {
        await refresh(projectId); if (serial !== revision || !dialog.isConnected || projectId !== project()) return;
        const summary = cache.summary || {rows: [], total_cents: 0, expense_count: 0};
        const rows = [...(summary.rows || [])];
        for (const person of cache.people) if (!rows.some(row => row.person_id === person.id)) rows.push({person_id: person.id, label: person.label});
        closeNamePopup?.();
        content.innerHTML = `<p>本项目统一使用同一张公司报销表。这里按费用归属人记录金额，供统一结算时核对。点击姓名直接编辑并保存，或从姓名下拉选择已有人员归并；点击笔数或金额查看对应费用明细。</p><div class="reimbursement-summary-total"><strong>登记合计 ¥ ${money(summary.total_cents || 0)}</strong><span>${summary.expense_count || 0} 笔费用</span></div><div class="reimbursement-summary-table"><table><thead><tr><th>费用归属人</th><th>笔数</th><th>登记金额</th><th>待核对</th><th>可提交</th><th>已制包</th></tr></thead><tbody>${rows.map(row => {
          const person = cache.people.find(value => value.id === row.person_id);
          return `<tr data-person-row="${esc(row.person_id || '')}"><th>${person ? `<form data-inline-person="${esc(person.id)}" class="reimbursement-person-name" novalidate><div class="reimbursement-name-combo"><input name="label" role="combobox" aria-label="修改或选择 ${esc(person.label)} 姓名" aria-autocomplete="list" aria-haspopup="listbox" aria-expanded="false" aria-controls="person-name-list-${esc(person.id)}" value="${esc(person.label)}" maxlength="100" autocomplete="off" ${person.version ? '' : 'disabled'}><button type="button" data-name-toggle tabindex="-1" aria-label="显示全部人员" ${person.version ? '' : 'disabled'}><svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true" focusable="false"><path d="m3 4.5 3 3 3-3" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg></button><ul id="person-name-list-${esc(person.id)}" role="listbox" aria-label="本项目已有人员" hidden></ul></div><p data-name-hint class="reimbursement-person-message" hidden></p><button type="submit" hidden>保存</button><p data-error role="alert" hidden></p></form>${person.can_retire === true ? `<details class="reimbursement-name-menu"><summary aria-label="${esc(person.label)} 姓名菜单">⋯</summary><button type="button" data-retire-person="${esc(person.id)}">移除空人员</button></details>` : ''}` : esc(row.label || '未指定')}</th><td>${summaryNumber(row, 'all', true)}</td><td>${summaryNumber(row, 'all')}</td><td>${summaryNumber(row, 'pending')}</td><td>${summaryNumber(row, 'ready')}</td><td>${summaryNumber(row, 'packaged')}</td></tr>`;
        }).join('') || '<tr><td colspan="6">尚未登记费用。</td></tr>'}</tbody></table></div><p class="muted">同一笔费用只计算一次，发票与付款截图不会重复计费。未创建的材料不计入金额；已制包表示报销文件已生成，尚不代表公司款项到账。</p>${cache.retiredPeople.length ? `<details class="reimbursement-retired"><summary>已移除人员 · ${cache.retiredPeople.length} 人</summary>${cache.retiredPeople.map(person => `<div><span>${esc(person.label)}</span>${person.can_restore && person.version ? `<button type="button" data-restore-person="${esc(person.id)}">恢复到名单</button>` : ''}</div>`).join('')}</details>` : ''}<div class="actions"><button type="button" data-new-person>选择或新增人员</button><button type="button" data-refresh-people>刷新金额</button></div>`;
        content.querySelector('[data-new-person]').onclick = () => newPerson({projectId, onCreated: draw});
        content.querySelector('[data-refresh-people]').onclick = draw;
        for (const button of content.querySelectorAll('[data-person-details]')) button.onclick = () => {
          const personId = button.dataset.personDetails, scope = button.dataset.allocationScope;
          const row = rows.find(value => String(value.person_id || '') === personId);
          drawDetails(personId, row?.label || '未指定', scope, {personId, scope, count: button.dataset.allocationCount});
        };
        if (returnTo && typeof returnTo.personId === 'string') [...content.querySelectorAll('[data-person-details]')].find(button => button.dataset.personDetails === returnTo.personId && button.dataset.allocationScope === returnTo.scope && button.dataset.allocationCount === returnTo.count)?.focus();
        for (const form of content.querySelectorAll('[data-inline-person]')) inlineName(cache.people.find(person => person.id === form.dataset.inlinePerson), form, draw);
        for (const action of ['retire', 'restore']) for (const button of content.querySelectorAll(`[data-${action}-person]`)) button.onclick = () => {
          const id = button.getAttribute(`data-${action}-person`), person = [...cache.people, ...cache.retiredPeople].find(value => value.id === id);
          personAction(person, action, draw);
        };
      } catch (error) {if (serial === revision && dialog.isConnected && projectId === project()) {
        content.innerHTML = dialogLoadErrorMarkup(error.message); content.querySelector('[data-dialog-retry]').onclick = draw;
      }} finally {if (serial === revision && dialog.isConnected) content.setAttribute('aria-busy', 'false');}
    }
    await draw();
  }
  async function editExpense(item) {
    if (!item || !pendingReview(item) || !item.version) return;
    const projectId = project(), expectedVersion = item.version;
    try {await refresh(projectId);} catch (error) {notify(error.message, true); return;}
    if (projectId !== project()) return;
    const dialog = document.createElement('dialog'); dialog.id = 'expensePersonDialog'; dialog.className = 'reimbursement-people-dialog';
    dialog.innerHTML = `<div class="dialoghead"><h2>修改费用归属人</h2><button type="button" data-close class="dialog-close" aria-label="关闭">×</button></div><p>${esc(item.title)} · ${expenseMoney(item)}</p><form novalidate><label>费用归属人<select name="person_id">${options(item.reimbursement_person_id || '')}</select></label><button type="button" data-new-person>新增费用归属人</button><p class="muted">只修改人员归属，金额、项目类型和名称保持。</p><p data-error role="alert" hidden></p><div class="actions"><button type="submit" class="primary">保存费用归属人</button><button type="button" data-close>取消</button></div></form>`;
    document.body.append(dialog); dialog.showModal(); let busy = false, expired = false, committed = false;
    const close = () => {if (!busy) dialog.remove();}; dialog.querySelectorAll('[data-close]').forEach(button => button.onclick = close);
    dialog.oncancel = event => {event.preventDefault(); close();};
    const form = dialog.querySelector('form'), save = form.querySelector('[type=submit]');
    dialog.querySelector('[data-new-person]').onclick = () => newPerson({projectId, onCreated: person => {form.elements.person_id.innerHTML = options(person.id);}});
    form.onsubmit = async event => {
      event.preventDefault(); if (busy || expired || committed) return;
      busy = true; dialog.querySelectorAll('button,select').forEach(control => control.disabled = true);
      try {
        if (projectId !== project()) throw Error('项目已变化，请关闭后重新打开。');
        await api('reimbursement-people/assign-expense', {id: item.id, expected_version: expectedVersion, person_id: form.elements.person_id.value}); committed = true;
        await load(); dialog.remove(); notify('费用归属人已保存，费用金额和报销分类保持。');
      } catch (error) {
        expired ||= error.status === 409 || error.status === 403 || /版本|变化|项目/.test(error.message);
        const target = dialog.querySelector('[data-error]'); target.textContent = (committed ? '费用归属人已保存；刷新失败，请关闭后刷新页面。' : error.message + (expired ? '；请关闭后重新选择当前费用。' : '；选择已保留，可重试。')); target.hidden = false;
      } finally {busy = false; if (dialog.isConnected) {dialog.querySelectorAll('button,select').forEach(control => control.disabled = false); save.disabled = expired || committed;}}
    };
  }
  async function bulkAssign(records, {projectId = project(), onSaved} = {}, materials = false) {
    if (!projectId || projectId !== project()) throw Error('请先进入当前项目。');
    if (bulkOpening || document.getElementById('reimbursementPeopleBulk')) return;
    if (!Array.isArray(records) || !records.length || records.length > 200) throw Error('每次请选择1至200份不重复的' + (materials ? '材料' : '费用') + '。');
    const selected = records.map(record => ({...record})), ids = selected.map(record => record.id);
    if (ids.some(id => typeof id !== 'string' || !id) || new Set(ids).size !== ids.length) throw Error('勾选列表含无效或重复记录，请重新选择。');
    bulkOpening = true;
    try {await refresh(projectId);} finally {bulkOpening = false;}
    if (projectId !== project()) throw Error('项目已变化，请重新选择。');
    const targets = activePeople(projectId).map(person => ({...person}));
    const invalid = selected.filter(record => materials ? record.key?.startsWith('e:') || record.item_id || !['received', 'review'].includes(record.status) || !Number.isSafeInteger(record.revision) || record.revision < 0 || record.project_id && record.project_id !== projectId : record.project_id !== projectId || typeof record.version !== 'string' || !record.version || record.stage !== 'draft' || record.verified || record.deleted || record.returned_to_synthesis || record.merged_into || record.legacy || record.history_imported || ['archived', 'deleted'].includes(record.synthesis_material_state));
    const known = selected.filter(record => !record.amount_pending && Number.isSafeInteger(record.amount_cents)), total = known.reduce((sum, record) => sum + record.amount_cents, 0);
    const owners = new Map();
    for (const record of selected) {const id = record.reimbursement_person_id || '', owner = owners.get(id) || {label: label(record) || '未指定', count: 0}; owner.count++; owners.set(id, owner);}
    const title = materials ? '批量修改材料归属人' : '批量修改费用归属人';
    const dialog = document.createElement('dialog'); dialog.id = 'reimbursementPeopleBulk'; dialog.className = 'reimbursement-people-dialog';
    dialog.innerHTML = `<div class="dialoghead"><h2>${title}</h2><button type="button" data-close class="dialog-close" aria-label="关闭">×</button></div><p>仅修改本次勾选的 ${selected.length} ${materials ? '份材料' : '笔费用'}；${materials ? '识别金额合计（非费用合计）' : '费用合计'} ¥ ${money(total)}${known.length < selected.length ? `，另有 ${selected.length - known.length} 份金额待确认` : ''}。</p><p>当前归属：${[...owners.values()].map(owner => `${esc(owner.label)} ${owner.count} ${materials ? '份' : '笔'}`).join('；')}。已指定 ${[...owners.keys()].filter(Boolean).length} 人${owners.has('') ? `，未指定 ${owners.get('').count} ${materials ? '份' : '笔'}` : ''}。</p><details class="reimbursement-bulk-records"><summary>查看本次勾选列表</summary><ul>${selected.map(record => `<li>${esc(record.title || record.name || '未命名记录')} · ${esc(label(record) || '未指定')} · ${Number.isSafeInteger(record.amount_cents) && !record.amount_pending ? '¥ ' + money(record.amount_cents) : '金额待确认'}</li>`).join('')}</ul></details><p class="muted">${materials ? '只指定材料归属，不改用途或识别金额，不创建费用；票据与付款识别金额不作为重复记账。' : '只改这些费用的人员归属，金额、报销分类、核对状态和原件保持；仅未核对草稿可指定归属，已退回合成台的费用请在材料中处理。'}</p>${invalid.length ? `<p class="reimbursement-bulk-invalid">${invalid.length} 份记录不符合当前条件，整组不能保存。请刷新并重新选择，未跳过任何记录。</p>` : ''}<form novalidate><label>统一指定费用归属人<select name="person_id"><option value="">未指定</option>${targets.map(person => `<option value="${esc(person.id)}">${esc(person.label)}</option>`).join('')}</select></label><p data-bulk-target role="status">将上述选中记录设为未指定，金额保持。</p><p data-error role="alert" hidden></p><div class="actions"><button type="submit" class="primary" ${invalid.length ? 'disabled' : ''}>确认保存 ${selected.length} ${materials ? '份材料' : '笔费用'}归属</button><button type="button" data-refresh-confirmed hidden>刷新页面</button><button type="button" data-close>取消</button></div></form>`;
    document.body.append(dialog); dialog.showModal();
    const form = dialog.querySelector('form'), select = form.elements.person_id, save = form.querySelector('[type=submit]'), errorTarget = form.querySelector('[data-error]'), retryRead = form.querySelector('[data-refresh-confirmed]');
    let busy = false, expired = false, committed = false, result = null;
    const targetPerson = () => targets.find(person => person.id === select.value);
    const update = () => {const target = targetPerson(); save.disabled = busy || expired || committed || !!invalid.length || !!select.value && !target?.version; form.querySelector('[data-bulk-target]').textContent = `将上述 ${selected.length} ${materials ? '份材料' : '笔费用'}统一设为${target?.label || '未指定'}，金额保持。`;};
    select.onchange = update; update();
    const close = () => {if (!busy) dialog.remove();}; dialog.querySelectorAll('[data-close]').forEach(button => button.onclick = close); dialog.oncancel = event => {event.preventDefault(); close();};
    const finish = async () => {await refresh(projectId); if (onSaved) await onSaved(result); else await load(); dialog.remove(); notify(result.message || '勾选记录的人员归属已保存，金额和原件保持。');};
    retryRead.onclick = async () => {
      if (busy || !committed) return; busy = true; retryRead.disabled = true;
      try {await finish();} catch (error) {errorTarget.textContent = '归属已保存；刷新失败：' + error.message + '。无需重复保存。'; errorTarget.hidden = false;}
      finally {busy = false; if (dialog.isConnected) {retryRead.disabled = false; update();}}
    };
    form.onsubmit = async event => {
      event.preventDefault(); update(); if (save.disabled) return;
      const target = targetPerson(); busy = true; dialog.querySelectorAll('button,select').forEach(control => control.disabled = true);
      try {
        if (projectId !== project()) throw Error('项目已变化，请关闭后重新选择。');
        const versions = Object.fromEntries(selected.map(record => [record.id, materials ? record.revision : record.version]));
        result = await api('reimbursement-people/' + (materials ? 'assign-materials' : 'assign-expenses'), {project_id: projectId, ids, ...(materials ? {revisions: versions} : {versions}), person_id: select.value, ...(target ? {target_version: target.version} : {})}); committed = true;
        if (result.person?.id !== select.value) throw Error('人员归属结果无法核对，请刷新页面。');
        const returned = materials ? result.entries : result.items;
        if (!Array.isArray(returned) || returned.length !== selected.length || returned.some(record => !ids.includes(record.id)) || new Set(returned.map(record => record.id)).size !== ids.length) throw Error('批量结果无法核对，请刷新页面。');
        await finish();
      } catch (error) {
        expired ||= error.status === 409 || error.status === 403 || /版本|变化|项目|过期|已归并|已移除/.test(error.message);
        errorTarget.textContent = committed ? '归属已保存；' + error.message + '。请刷新页面，勿重复保存。' : error.message + (expired ? '；选择已保留，请关闭后刷新并重新勾选。' : '；选择已保留，可稍后重试。'); errorTarget.hidden = false; retryRead.hidden = !committed;
      } finally {busy = false; if (dialog.isConnected) {dialog.querySelectorAll('button,select').forEach(control => control.disabled = false); select.disabled = committed; update();}}
    };
  }
  const bulkAssignExpenses = (items, config) => bulkAssign(items, config);
  const bulkAssignMaterials = (entries, config) => bulkAssign(entries, config, true);
  function batchChoice(select, projectId) {
    const people = activePeople(projectId), key = 'invoice-batch-person:' + projectId;
    let selected = '';
    try { selected = localStorage.getItem(key) || ''; } catch (_) { /* Optional preference. */ }
    select.innerHTML = '<option value="">不指定</option>' + people.map(p => `<option value="${esc(p.id)}">${esc(p.label)}</option>`).join('');
    select.value = people.some(p => p.id === selected) ? selected : '';
    select.disabled = !projectId;
    select.onchange = () => { try { localStorage.setItem(key, select.value); } catch (_) { /* Current batch still works. */ } };
  }
  window.reimbursementPeopleUI = {batchChoice, refresh, options, markup, label, choice, choiceControl, groupMarkup, newPerson, editExpense, activePeople, filterOptions, matchesPerson, bulkAssignExpenses, bulkAssignMaterials,
    people: () => cache.projectId === project() ? cache.people : [],
    hasPeople: () => cache.projectId === project() && cache.people.length > 0};
  const button = $('reimbursementPeople'); if (button) button.onclick = openSummary;
  document.addEventListener('pointerdown', event => {
    for (const menu of document.querySelectorAll('.reimbursement-name-menu[open]')) if (!menu.contains(event.target)) menu.open = false;
  }, true);
  document.addEventListener('keydown', event => {
    const menu = event.key === 'Escape' && event.target.closest('.reimbursement-name-menu[open]');
    if (menu) {event.preventDefault(); event.stopPropagation(); menu.open = false; menu.querySelector('summary').focus();}
  }, true);
  document.addEventListener('click', event => {
    const button = event.target.closest('[data-edit-expense-person]');
    if (button) {event.stopPropagation(); editExpense(state.items.find(item => item.id === button.dataset.editExpensePerson));}
  });
  if (project()) refresh().then(() => {if (typeof render === 'function') render();}).catch(() => { /* The amount panel offers an explicit retry; expense loading stays available. */ });
})();
