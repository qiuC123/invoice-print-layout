'use strict';
(() => {
  const eligible = item => !!item && item.project_id === currentInvoiceProjectId() && typeof item.version === 'string' && !!item.version &&
    item.stage === 'draft' && !item.verified && !item.deleted && !item.merged_into && !item.returned_to_synthesis &&
    !item.legacy && !item.history_imported && !['archived', 'deleted'].includes(item.synthesis_material_state);
  const visibleSelected = () => base().filter(item => matches(item, view) && checked.has(item.id));
  const originalName = item => {
    let name = item.project_name;
    if (name == null || typeof name === 'string' && !name.trim()) name = item.title;
    return typeof name === 'string' ? name.trim() : '';
  };
  const knownAmount = item => !item.amount_pending && Number.isSafeInteger(item.amount_cents) && item.amount_cents >= 0;
  window.updateReviewBatchTypeControls = (items = visibleSelected()) => {
    const button = $('batchProjectType');
    if (!button) return;
    const invalid = items.some(item => !eligible(item));
    button.hidden = view !== 'review';
    button.disabled = view !== 'review' || !currentInvoiceProjectId() || !items.length || items.length > 200 || invalid;
    button.textContent = '修改项目类型' + (items.length ? ` · ${items.length} 笔` : '');
    button.title = invalid ? '当前勾选含不可编辑费用，整批不能修改；请取消这些费用的勾选。' : items.length > 200 ? '每次最多修改200笔，请分批选择。' : `仅修改当前可见勾选的 ${items.length} 笔，隐藏勾选不参与。`;
  };
  window.openBatchProjectTypeEditor = () => {
    const projectId = currentInvoiceProjectId(), records = visibleSelected().map(item => ({...item}));
    if (view !== 'review' || !projectId) throw Error('请在项目待核对列表勾选费用。');
    if (!records.length || records.length > 200) throw Error('每次请选择1至200笔当前可见费用。');
    if (records.some(item => !eligible(item))) throw Error('勾选含不可编辑费用，整批未修改。请刷新并重新选择。');
    const existing = $('reviewBatchTypeDialog');
    if (existing) {existing.querySelector('select')?.focus(); return;}
    const types = (state.report_types || []).map(type => ({...type, names: [...(type.names || [])]}));
    if (!types.length) throw Error('项目类型尚未加载，请刷新页面。');
    const ids = records.map(item => item.id), versions = Object.fromEntries(records.map(item => [item.id, item.version]));
    const known = records.filter(knownAmount), total = known.reduce((sum, item) => sum + item.amount_cents, 0);
    const typeLabel = item => types.find(type => type.code === (item.project_type || item.report_choice?.project_type))?.label || '待确认';
    const dialog = document.createElement('dialog'); dialog.id = 'reviewBatchTypeDialog'; dialog.className = 'review-batch-dialog';
    dialog.innerHTML = `<div class="dialoghead"><h2>批量修改项目类型</h2><button type="button" data-close class="dialog-close" aria-label="关闭">×</button></div><p>仅修改当前勾选的 ${records.length} 笔费用；已知金额合计 ¥ ${money(total)}${known.length < records.length ? `，另有 ${records.length - known.length} 笔金额待确认` : ''}。</p><div class="review-batch-table"><table><thead><tr><th>费用名称</th><th>原项目类型</th><th>金额</th></tr></thead><tbody>${records.map(item => `<tr><td>${esc(originalName(item))}</td><td>${esc(typeLabel(item))}</td><td>${knownAmount(item) ? '¥ ' + money(item.amount_cents) : '金额待确认'}</td></tr>`).join('')}</tbody></table></div><form novalidate><label>统一修改为项目类型<select name="project_type"><option value="">选择项目类型</option>${types.map(type => `<option value="${esc(type.code)}">${esc(type.label)}</option>`).join('')}</select></label><label data-fixed-detail hidden>对应细则（本批统一）<select name="project_name"><option value="">选择对应细则</option></select></label><p data-target role="status">选择目标类型后，查看本批名称处理方式。</p><p class="muted">每笔费用仍独立保存；金额、消费日期、费用归属人和原件保持，保存后仍待核对。</p><p data-error role="alert" hidden></p><div class="actions"><button type="submit" class="primary" disabled>确认修改 ${records.length} 笔项目类型</button><button type="button" data-refresh-confirmed hidden>刷新费用列表</button><button type="button" data-close>取消</button></div></form>`;
    document.body.append(dialog); dialog.showModal();
    const form = dialog.querySelector('form'), typeControl = form.elements.project_type, nameControl = form.elements.project_name;
    const save = form.querySelector('[type=submit]'), error = form.querySelector('[data-error]'), retryRead = form.querySelector('[data-refresh-confirmed]');
    let busy = false, expired = false, committed = false, result = null;
    const target = () => types.find(type => type.code === typeControl.value);
    const invalid = () => {
      const type = target();
      if (!type) return '请选择目标项目类型。';
      if (type.editable_name && records.some(item => !originalName(item) || originalName(item).length > 200)) return '部分费用没有有效名称，请先逐笔补齐名称。';
      if (!type.editable_name && !type.names.includes(nameControl.value)) return '请选择该类型对应的固定细则。';
      return '';
    };
    const update = () => {
      save.disabled = busy || expired || committed || !!invalid();
      const type = target();
      form.querySelector('[data-target]').textContent = !type ? '选择目标类型后，查看本批名称处理方式。' : type.editable_name ? `将本批统一改为“${type.label}”，每笔保留上表各自名称。` : `将本批统一改为“${type.label}”${nameControl.value ? `，报销表细则为“${nameControl.value}”` : '，请先选择对应细则'}，原费用标题保留。`;
    };
    typeControl.onchange = () => {
      const type = target(); form.querySelector('[data-fixed-detail]').hidden = !type || !!type.editable_name;
      nameControl.innerHTML = '<option value="">选择对应细则</option>' + (type && !type.editable_name ? type.names.map(name => `<option value="${esc(name)}">${esc(name)}</option>`).join('') : '');
      error.hidden = true; update();
    };
    nameControl.onchange = update;
    const close = () => {if (!busy) {dialog.remove(); $('batchProjectType')?.focus();}};
    dialog.querySelectorAll('[data-close]').forEach(button => button.onclick = close);
    dialog.oncancel = event => {event.preventDefault(); close();};
    const finish = async () => {
      await load();
      for (const id of ids) checked.delete(id);
      render(); dialog.remove(); notify(result?.message || '勾选费用的项目类型已保存，金额和原件保持。');
    };
    retryRead.onclick = async () => {
      if (busy || !committed) return; busy = true; retryRead.disabled = true;
      try {await finish();} catch (failure) {error.textContent = '项目类型已保存；刷新失败：' + failure.message + '。无需重复保存。'; error.hidden = false;}
      finally {busy = false; if (dialog.isConnected) {retryRead.disabled = false; update();}}
    };
    form.onsubmit = async event => {
      event.preventDefault(); update(); if (save.disabled) return;
      const current = ids.map(id => state.items.find(item => item.id === id));
      if (projectId !== currentInvoiceProjectId() || current.some(item => !eligible(item) || item.version !== versions[item.id])) {
        expired = true; error.textContent = '费用或项目已变化，选择已保留；请关闭后刷新并重新勾选。'; error.hidden = false; update(); return;
      }
      const type = target(); busy = true; dialog.querySelectorAll('button,select').forEach(control => control.disabled = true);
      try {
        result = await api('review/batch-project-type', {project_id: projectId, ids, versions, project_type: type.code, ...(!type.editable_name ? {project_name: nameControl.value} : {})});
        committed = true;
        if (!Array.isArray(result.items) || result.items.length !== ids.length || result.items.some(item => !ids.includes(item.id)) || new Set(result.items.map(item => item.id)).size !== ids.length) throw Error('保存结果无法核对，请刷新费用列表。');
        await finish();
      } catch (failure) {
        expired ||= failure.status === 400 || failure.status === 409 || failure.status === 403 || /已变化|版本|已核对|已删除|已合并|撤回/.test(failure.message);
        error.textContent = committed ? '项目类型已保存；' + failure.message + '。请刷新费用列表，勿重复保存。' : failure.message + (expired ? '；选择已保留，请关闭后刷新并重新勾选。' : '；选择已保留，可重试。');
        error.hidden = false; retryRead.hidden = !committed;
      } finally {
        busy = false;
        if (dialog.isConnected) {dialog.querySelectorAll('button,select').forEach(control => control.disabled = false); typeControl.disabled = nameControl.disabled = committed; update();}
      }
    };
    update(); typeControl.focus();
  };
  $('batchProjectType').onclick = () => act(window.openBatchProjectTypeEditor);
  window.updateReviewBatchTypeControls();
})();
