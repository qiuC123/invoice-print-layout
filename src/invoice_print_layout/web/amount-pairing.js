'use strict';
(() => {
  let opening = false;
  window.openAmountPairing = async ({projectId, types = [], cards = [], setBusy, onCommitted}) => {
    if (opening || document.getElementById('amountPairingDialog')) return;
    if (!projectId || projectId !== currentInvoiceProjectId()) throw Error('请先从侧栏选择一个项目。');
    opening = true;
    let preview;
    try {preview = await api('material-matching/amount-preview', {project_id: projectId});}
    finally {opening = false;}
    if (preview.project_id !== projectId || !preview.snapshot_token || !Array.isArray(preview.groups) || preview.groups.some(group => !Number.isSafeInteger(group.amount_cents) || group.amount_cents < 0 || !group.report_choice?.project_type || !group.report_choice?.project_name || !Array.isArray(group.voucher_keys) || !Array.isArray(group.payment_ids))) throw Error('配对预览不完整，请重新预览。');
    const dialog = document.createElement('dialog'); dialog.id = 'amountPairingDialog'; dialog.className = 'amount-pairing-dialog';
    const typeLabel = code => types.find(type => type.code === code)?.label || code || '待核对';
    const cardFor = key => cards.find(card => card.key === key || card.id === key);
    const cardName = key => cardFor(key)?.title || cardFor(key)?.name || '材料原件';
    const materialList = keys => keys.map(key => {const card = cardFor(key), url = card?.url; return typeof url === 'string' && url.startsWith('/') && !url.startsWith('//') ? `<a href="${esc(url)}" target="_blank" rel="noopener">${esc(cardName(key))} ↗</a>` : esc(cardName(key));}).join('、');
    dialog.innerHTML = `<div class="dialoghead"><h2>确认按金额配对</h2><button type="button" data-close class="dialog-close" aria-label="关闭">×</button></div><p>本项目可唯一配对 ${preview.groups.length} 组。本台未归属材料随本项目确认归属。确认后统一进入待核对，尚未核验或制作报销包。</p><p>金额相同不代表用途或归属相同；同金额存在多种组合的材料将跳过，留在合成台手工选择。</p><div class="amount-pairing-groups">${preview.groups.map((group, index) => `<section><h3>第 ${index + 1} 组 · ${esc(group.title || group.report_choice?.project_name || '报销费用')}</h3>${window.reimbursementPeopleUI?.markup(group)||''}<p>将采用：${esc(typeLabel(group.report_choice?.project_type))} ／ ${esc(group.report_choice?.project_name || '待核对')} ／ ¥ ${money(group.amount_cents)}</p><p>票据：${materialList(group.voucher_keys)}</p><p>付款：${materialList(group.payment_ids)}</p></section>`).join('') || '<p>没有可唯一配对的组合，请手工选择材料。</p>'}</div>${preview.skipped?.length ? `<details open><summary>跳过 ${preview.skipped.length} 项</summary><ul>${preview.skipped.map(value => `<li>${materialList(value.keys || [])}${value.keys?.length ? '：' : ''}${esc(value.reason || '需要手工核对')}</li>`).join('')}</ul></details>` : ''}<form><label class="amount-pairing-ack"><input id="amountPairingAcknowledged" type="checkbox">我已逐份查看票据与付款，同意仅按金额确认以上唯一组合。</label><p id="amountPairingError" role="alert" hidden></p><div class="actions"><button type="button" data-close>取消</button><button id="amountPairingConfirm" type="submit" class="primary" disabled>确认配对 ${preview.groups.length} 组，进入待核对</button></div></form>`;
    document.body.append(dialog); dialog.showModal();
    const acknowledgement = dialog.querySelector('#amountPairingAcknowledged'), submit = dialog.querySelector('#amountPairingConfirm'), errorTarget = dialog.querySelector('#amountPairingError');
    const idempotencyKey = crypto.randomUUID();
    let busy = false, expired = false, committed = false;
    const update = () => {submit.disabled = busy || expired || committed || !preview.groups.length || !acknowledgement.checked;};
    acknowledgement.onchange = update;
    const close = () => {if (!busy) dialog.remove();};
    dialog.querySelectorAll('[data-close]').forEach(button => button.onclick = close);
    dialog.oncancel = event => {event.preventDefault(); close();};
    dialog.querySelector('form').onsubmit = async event => {
      event.preventDefault(); if (busy || expired || committed || !preview.groups.length || !acknowledgement.checked) return;
      if (projectId !== currentInvoiceProjectId()) {expired = true; errorTarget.textContent = '项目已变化，请关闭后重新预览。'; errorTarget.hidden = false; update(); return;}
      busy = true; setBusy?.(true); dialog.querySelectorAll('button,input').forEach(control => control.disabled = true);
      try {
        const result = await api('material-matching/amount-confirm', {project_id: projectId, snapshot_token: preview.snapshot_token, idempotency_key: idempotencyKey, amount_only_confirmed: true});
        if (!Array.isArray(result.items) || !Array.isArray(result.matches)) {expired = true; throw Error('确认结果无法核实，请关闭并刷新材料，不要重复提交。');}
        committed = true;
        await onCommitted?.(result); dialog.remove();
      } catch (error) {
        expired = expired || error.status === 409 || error.status === 403 || /版本|变化|revision|过期|重新预览/.test(error.message);
        errorTarget.textContent = committed ? `配对已确认；材料刷新失败：${error.message}。请关闭后刷新，不能再次确认。` : error.message + (expired ? '；请关闭后重新预览。' : '；预览和勾选已保留，可稍后重试。'); errorTarget.hidden = false;
      } finally {
        busy = false; setBusy?.(false); if (dialog.isConnected) {dialog.querySelectorAll('button,input').forEach(control => control.disabled = false); update();}
      }
    };
  };
})();
