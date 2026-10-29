'use strict';
// Material links remain provisional; verification and submission use the ledger flow.
(() => {
  const dialog = document.createElement('dialog');
  dialog.id = 'matchingDeskDialog';
  dialog.className = 'matching-desk';
  dialog.innerHTML = `
    <div class="matching-header"><div><small>材料合成台</small><h2>选择材料，确认创建报销项目</h2></div><button id="matchingClose" type="button" aria-label="关闭材料合成台" class="dialog-close">×</button></div>
    <div class="matching-toolbar">
      <label>本批归属人<select id="matchingBatchPerson"><option value="">不指定</option></select></label>
      <button id="matchingUpload" type="button" class="primary">＋ 收材料</button>
      <input id="matchingFiles" type="file" accept=".pdf,.png,.jpg,.jpeg,.webp" multiple hidden>
      <button id="matchingClaim" type="button">二维码／链接领票</button>
      <button id="matchingMail" type="button">补收邮件</button>
      <button id="matchingAuto" type="button">寻找合成组合</button>
      <button id="matchingAmountPair" type="button">按金额一键配对</button>
      <button id="matchingAssignPeople" type="button" disabled>指定材料归属</button>
      <button id="matchingRefresh" type="button">刷新材料</button>
    </div>
    <p id="matchingMessage" class="matching-message" role="status"></p>
    <div id="matchingMailFeedback" class="matching-message is-error" hidden></div>
    <div id="matchingUploadProgress" class="matching-progress" hidden><progress id="matchingProgressBar"></progress><span id="matchingProgressText"></span></div>
    <div id="matchingBackgroundStatus" class="matching-progress" hidden></div>
    <div id="matchingLoadState" hidden></div>
    <div class="matching-layout">
      <div class="matching-pools" tabindex="0" role="region" aria-label="材料列表，可独立滚动">
        <section class="matching-lane" id="matchingSuggestions"><div class="matching-lane-head"><h3>建议组合</h3><span id="matchingSuggestionSummary"></span></div><p class="muted">本项目材料的建议组合；点击一组，在右侧预览并确认报销信息。</p><div id="matchingSuggestionTools"></div><div id="matchingSuggestionStatus" role="status"></div><div id="matchingSuggestionsList"></div></section>
        <section class="matching-lane"><div class="matching-lane-head"><h3>① 发票／收据</h3><span id="matchingVoucherCount"></span></div><p class="muted">含平台购买记录，可选多张票据；缺付款的已有费用也在这里。再次点击可取消。</p><div id="matchingVouchers" class="matching-card-grid"></div></section>
        <section class="matching-lane"><div class="matching-lane-head"><h3>② 付款截图</h3><span id="matchingPaymentCount"></span></div><p class="muted">可选多张，对应票据合计；同一笔付款只计一次。</p><div id="matchingPayments" class="matching-card-grid matching-payments"></div></section>
        <details class="matching-other" open><summary>③ 待领票与其他材料 <span id="matchingOtherCount"></span></summary><p class="muted">普通材料可以直接创建，也可作为补充附件；有票据或付款时不叠加金额。二维码需先领取正式发票。</p><div id="matchingOther" class="matching-card-grid"></div></details>
        <details class="matching-other matching-archive" id="matchingUnusedSection" hidden><summary>归档 <span id="matchingUnusedCount"></span></summary><p class="muted">归档材料不参与合成，也不计入费用。可恢复普通材料或删除；已删除材料仍可恢复，原件保留。</p><div id="matchingUnused" class="matching-card-grid"></div><details id="matchingDeletedSection" hidden><summary>已删除 <span id="matchingDeletedCount"></span></summary><div id="matchingDeleted" class="matching-card-grid"></div></details></details>
        <details class="matching-groups"><summary>已配对 · 可撤回 <span id="matchingGroupCount"></span></summary><div id="matchingGroups"></div></details>
      </div>
      <section class="matching-preview" tabindex="0" aria-label="票据与付款合成预览，可独立滚动">
        <div id="matchingPreviewContent"></div>
        <div class="matching-footer"><p id="matchingAmountStatus" role="status"></p><button id="matchingSave" type="button" class="primary" disabled>确认合成并进入待核对</button></div>
      </section>
    </div>`;
  document.body.append(dialog);
  const el = id => document.getElementById(id);
  const escape = value => esc(value);
  const currency = cents => Number.isSafeInteger(cents) ? `¥ ${money(cents)}` : '金额待确认';
  let board = {vouchers: [], payments: [], other: [], unused: [], archived: [], deleted: [], matches: [], report_types: [], supplement_candidates: []};
  let projectId = '', voucherKeys = new Set(), paymentIds = new Set(), supportIds = new Set(), busy = false, fetchRevision = 0;
  let savedItem = null, fieldSelection = '', needsSelectionConfirmation = false, sourceSuggestion = null;
  let opening = false, openRevision = 0;
  const printRequests = new Map();
  const currentVouchers = () => board.vouchers.filter(card => voucherKeys.has(card.key));
  const currentPayments = () => board.payments.filter(card => paymentIds.has(card.id));
  const currentSupports = () => board.other.filter(card => supportIds.has(queueId(card)) && isCreateSupport(card));
  const totalAmount = cards => cards.some(card => !Number.isSafeInteger(card.amount_cents) || card.amount_cents < 0) ? null :
    cards.reduce((total, card) => total + card.amount_cents, 0);
  const signature = card => card ? `${card.key || card.id}:${card.version ?? ''}:${card.revision ?? ''}` : '';
  const queueId = card => card.id || (card.key || '').replace(/^q:/, '');
  const isQr = card => card.qr_status === 'manual' || /^(qr|qr_claim|claim|invoice_qr)$/.test(card.kind || '') ||
    (card.issues || []).some(issue => /待领票|领票二维码|尚未领取|未领取发票/.test(issue));
  const isSupplementCard = card => !isQr(card) && ['other', 'order', 'trip', 'detail', 'trip_sheet'].includes(card.role);
  const isCreateSupport = card => isSupplementCard(card) || (!isQr(card) && card.role === 'purchase');
  const reportChoice = card => card.report_choice || {project_type: card.project_type || '', project_name: card.project_name || ''};
  const materialTitle = card => {
    const fields = card.fields || {}, text = value => typeof value === 'string' ? value.trim() : '';
    const shop = text(fields.shop_name);
    if (shop) return shop;
    const merchant = text(fields.merchant), identity = value => text(value).normalize('NFKC').replace(/\s+/g, '');
    const platforms = [fields.shopping_platform, fields.payment_channel, '淘宝闪购', '淘宝', '天猫', '京东', '美团', '美团外卖', '饿了么', '支付宝', '微信', '微信支付', '银联', '云闪付'];
    if (merchant && !/^(?:未填商家|商家未填写|未指定|未知|未识别|未填写|无|-)$/.test(merchant) &&
        !platforms.some(value => identity(value) && identity(value) === identity(merchant))) return merchant;
    if (isQr(card)) return '领票二维码';
    const role = card.role === 'receipt' && ['order', 'purchase'].includes(card.source_role) ? card.source_role : card.role || card.kind;
    return {invoice: '发票', receipt: '收据', payment: '付款截图', order: '购买记录', purchase: '购买记录',
      trip: '行程单', trip_sheet: '行程单', detail: '明细', other: '其他材料'}[role] || '其他材料';
  };
  window.materialMatchingTitle = materialTitle;
  const sameDisplayName = (left, right) => {
    const identity = value => typeof value === 'string' ? value.normalize('NFKC').replace(/\s+/g, '') : '';
    return !!identity(left) && identity(left) === identity(right);
  };
  const safeUrl = url => typeof url === 'string' && url.startsWith('/') && !url.startsWith('//') ? url : '';
  const originalUrl = card => safeUrl(card.url);
  const thumbnailUrl = card => safeUrl(card.thumbnail) || (!/\.pdf$/i.test(card.name || '') ? originalUrl(card) : '');
  const fieldLabels = {merchant: '商家名称', shop_name: '实际店铺', amount: '金额', invoice_number: '发票号码', payment_status: '付款状态'};
  const readableIssue = value => String(value).replace(/AI与原件字段冲突[:：]\s*(merchant|shop_name|amount|invoice_number|payment_status)/g,
    (_, field) => `AI与本地识别的${fieldLabels[field]}不同，请对照原件确认。`);
  const optionalOrderIssue = value => /订单号|订单编号|交易单号|交易号|流水号|支付编号/.test(value) &&
    /缺|未显示|未提供|未见|未识别|未能识别|无法识别|不同|不一致|不相同|不符|无法建立|无法对应|唯一对应|不能建立/.test(value) &&
    !/多笔|多个|多份|多张|两笔|两个订单|混有|歧义|无法区分|不能区分|退款|付款失败|未付款|未支付|付款状态|支付状态|发票号码|收款身份|材料用途|凭证用途/.test(value) &&
    !/(?:金额|商家|店铺|销售方).{0,8}(?:冲突|不一致|不同|不符)/.test(value);
  function recognitionMarkup(card) {
    const issues = (card.issues || []).map(String).filter(value => !['商品不明', '归属不明', '归属未定'].includes(value));
    const warnings = issues.filter(value => !optionalOrderIssue(value)).map(readableIssue);
    const conflicts = (Array.isArray(card.field_conflicts) ? card.field_conflicts : []).filter(value => fieldLabels[value.field]);
    const values = (window.invoiceCheckMarkup?.([card]) || '') + conflicts.map(value => `<small class="matching-field-conflict">${escape(fieldLabels[value.field])}：${value.observed_source === 'labelled_payment' ? '原有识别' : '本地识别'}“${escape(value.local_value || '未保存')}”；${value.observed_source === 'labelled_payment' ? '付款原文' : 'AI补读'}“${escape(value.ai_value || '未保存')}”。请对照原件确认。</small>`).join('');
    const legacyMerchant = warnings.some(value => /AI.*商家名称.*不同/.test(value)) && !conflicts.some(value => value.field === 'merchant');
    return `${warnings.length ? `<small class="matching-card-issues">${escape((card.key?.startsWith('e:') ? warnings.map(value => /^\s*(?:待配对|缺)/.test(value) ? value : `待配对：${value}`) : warnings).join('、'))}</small>` : ''}${values}${legacyMerchant ? '<small class="matching-field-conflict">历史记录未保存AI候选商家名称，请对照原件确认。</small>' : ''}`;
  }
  // Keep actionable review notes separate from routine matching explanations.
  const suggestionReviewNotes = suggestion => [...new Set([
    ...(suggestion.needs_name_review ? ['商家名称待核对，请对照原件确认。'] : []),
    ...(suggestion.warnings || []),
    ...(suggestion.ambiguous && !suggestion.warnings?.length ? ['存在多个可能组合，请选择对应材料。'] : []),
  ])];
  window.synthesisReviewNotes = suggestionReviewNotes;
  function transactionMarkup(card, pool, title = '') {
    const fields = card.fields || {}, labels = {shopping_platform: '购物平台', shop_name: '实际店铺', payment_channel: '支付渠道'};
    const lines = Object.entries(labels).filter(([key]) => fields[key] &&
      !(key === 'shop_name' && sameDisplayName(fields[key], title))).map(([key, label]) =>
      `<small>${label}：${escape(fields[key])}</small>`);
    const merchant = fields.merchant || '';
    const actorIdentity = value => String(value || '').normalize('NFKC').replace(/\s+/g, '').replace(/^闪购[:：·]?/, '');
    if (merchant && (card.role === 'invoice' || ![fields.shop_name, fields.shopping_platform].some(value =>
      value && actorIdentity(value) === actorIdentity(merchant)))) {
      lines.push(`<small>${card.role === 'invoice' ? '销售方' : pool === 'payment' ? '收款方' : '商家'}：${escape(merchant)}</small>`);
    }
    const kinds = {shopping_order: '购物订单号', payment_order: '支付订单号', merchant_order: '商家订单号',
      payment_transaction: '支付交易号', untyped_order: '订单号（体系未明）'};
    for (const identifier of Array.isArray(card.transaction_identifiers) ? card.transaction_identifiers : []) {
      if (kinds[identifier.kind] && identifier.value) lines.push(`<small class="muted">${kinds[identifier.kind]}${identifier.namespace ? ` · ${escape(identifier.namespace)}` : ''}：${escape(identifier.value)}</small>`);
    }
    return lines.join('');
  }
  const groupVersion = group => group.version || group.expense_version || group.item_version || group.item?.version || '';
  const canSupplement = group => {
    if (typeof group.can_supplement === 'boolean') return group.can_supplement;
    const item = group.item || state.items.find(value => value.id === group.item_id);
    return group.provisional === true && item?.stage === 'draft' && item.verified === false && !item.ready;
  };

  function message(text, error = false) {
    el('matchingMessage').textContent = String(text).replaceAll('待合成候选', '建议组合').replaceAll('待合成材料池', '材料池');
    el('matchingMessage').classList.toggle('is-error', error);
  }
  const publicClaimText = value => String(value || '').replace(/https?:\/\/[^\s<>"']+/gi, '领票链接');
  const count = value => Number.isSafeInteger(value) && value >= 0 ? value : 0;
  function clearMailFeedback() {
    const feedback = el('matchingMailFeedback');
    feedback.replaceChildren(); feedback.hidden = true;
  }
  function mailFeedback(result) {
    const intake = result.intake || {}, linkFailures = Array.isArray(result.link_failures) ? result.link_failures : [];
    const importFailures = Array.isArray(intake.import_failures) ? intake.import_failures : [];
    const preparationFailures = Array.isArray(intake.preparation_failures) ? intake.preparation_failures : [];
    const failures = [...linkFailures.map(value => ({...value, type: 'link'})),
      ...importFailures.map(value => ({...value, type: 'import'})), ...preparationFailures.map(value => ({...value, type: 'prepare'}))];
    const detailed = ['link_downloaded', 'image_downloaded'].some(key => Number.isSafeInteger(result[key])) ||
      ['converted_to_pdf', 'claim_pending', 'prepared'].some(key => Number.isSafeInteger(intake[key]));
    const received = count(result.downloaded), linked = count(result.link_downloaded), images = count(result.image_downloaded);
    const converted = count(intake.converted_to_pdf), pending = count(intake.claim_pending), prepared = count(intake.prepared);
    const summary = [result.message || '补收完成，材料已放入合成台；确认创建后进入待核对。'];
    if (detailed) {
      summary.push(`链接材料已下载 ${linked} 份 · 图片材料已保存 ${images} 份 · 已整理 ${prepared} 份 · 二维码转为正式发票 PDF ${converted} 份 · 待人工领票 ${pending} 份 · 链接下载失败 ${linkFailures.length} 项。`);
      if (!received && !linked && !images && !converted && !prepared) summary.push('本次没有新增材料；已保存的材料保留，可继续合成。');
      if (pending) summary.push('待领票图片和原因在第三栏，可打开原件或重试领票。');
    }
    if (failures.length) summary.push(`失败共 ${failures.length} 项。`);
    if (linkFailures.length) summary.push('部分领票链接未收齐，再次点击“补收邮件”可重试；已保存的材料保留。');
    if (importFailures.length) summary.push('部分文件未能导入，请更换原文件后重新上传。');
    if (preparationFailures.length) summary.push('部分材料整理失败，原件已保存，可在材料区重试识别。');
    message(summary.join(' '), failures.length > 0 || pending > 0);
    clearMailFeedback();
    if (failures.length) {
      const feedback = el('matchingMailFeedback'), heading = document.createElement('p'), list = document.createElement('ul');
      heading.textContent = '需要处理的邮件材料：';
      for (const failure of failures) {
        const row = document.createElement('li');
        const parts = failure.type === 'link' ? ['链接下载失败', failure.subject || '未命名邮件', failure.message_uid ? `邮件 ${failure.message_uid}` : '',
          failure.order_number ? `订单 ${failure.order_number}` : '', failure.reason || '链接下载失败', '再次补收邮件可重试'] :
          failure.type === 'import' ? ['导入失败', failure.name || '未命名文件', failure.reason || '文件校验失败', '请更换原文件后重新上传'] :
          ['整理失败', failure.id ? `材料 ${failure.id}` : '已保存材料', failure.reason || '识别未完成', '可在材料区重试识别'];
        row.textContent = publicClaimText(parts.filter(Boolean).join(' · ')); list.append(row);
      }
      feedback.append(heading, list); feedback.hidden = false;
    }
  }
  function lock(value) {
    busy = value;
    for (const control of dialog.querySelectorAll('button,input,select')) control.disabled = value;
    updateSaveState();
    for (const id of ['matchingAuto', 'matchingAmountPair', 'matchingClaim', 'matchingMail', 'matchingUpload', 'matchingRefresh']) el(id).disabled = value || !projectId;
    for (const button of dialog.querySelectorAll('[data-undo-group]')) button.disabled = value || !button.dataset.groupVersion;
    if (!value) queueMicrotask(showBackgroundResult);
    window.lockSynthesisSuggestions?.(value);
    updateSupplementButtons();
    updatePeopleButton();
    el('matchingClose').disabled = value && !opening;
  }
  function peopleSelectionError(cards) {
    if (!projectId) return '先从侧栏选择项目。';
    if (!cards.length) return '先选择属于同一个人的材料，再批量指定归属。';
    if (cards.length > 200) return '每次最多指定200份材料，请分批选择。';
    if (cards.some(card => !card.key?.startsWith('q:') || card.item_id)) return '已有费用请在待核对列表勾选后批量修改归属。';
    if (cards.some(card => isQr(card) || !['received', 'review'].includes(card.status) || !Number.isSafeInteger(card.revision) || card.revision < 0)) return '所选材料已变化，请刷新后重新选择。';
    if (cards.some(card => card.project_id && card.project_id !== projectId)) return '所选材料属于另一项目，请重新选择。';
    return '';
  }
  function updatePeopleButton() {
    const cards = [...currentVouchers(), ...currentPayments(), ...currentSupports()], button = el('matchingAssignPeople');
    const error = peopleSelectionError(cards);
    button.disabled = busy || !!error || !window.reimbursementPeopleUI?.bulkAssignMaterials;
    button.title = error || '只修改选中材料的人员归属，不创建或合并费用。';
    button.textContent = '指定材料归属' + (cards.length ? `（${cards.length}份）` : '');
  }
  function supportError() {
    if (!projectId) return '先选择一个项目，再补充材料。';
    if (currentSupports().some(card => card.project_id && card.project_id !== projectId)) return '补充材料属于另一项目，请重新选择。';
    return '';
  }
  function updateSupplementButtons() {
    const unsupported = currentSupports().some(card => !isSupplementCard(card));
    for (const button of dialog.querySelectorAll('[data-supplement-group]')) {
      const group = board.matches.find(value => (value.group_id || value.id) === button.dataset.supplementGroup);
      button.disabled = busy || !group || !canSupplement(group) || !groupVersion(group) || !currentSupports().length || !!supportError() || unsupported;
      button.title = unsupported ? '购买凭证可用于创建；补到已有配对前，请先确认材料用途' : !group || !canSupplement(group) ? '仅可补到尚未核对的暂配费用' : '把第三栏选中的普通材料补到这组费用，不计金额';
    }
    for (const button of dialog.querySelectorAll('[data-supplement-candidate]')) {
      const card = board.other.find(value => queueId(value) === button.dataset.supplementCandidate);
      button.disabled = busy || !card || !candidateFor(card);
    }
  }
  function selectionError() {
    const vouchers = currentVouchers(), payments = currentPayments(), supports = currentSupports();
    if (!projectId) return '先选择一个项目，再创建报销项目。';
    if (supportError()) return supportError();
    if (!vouchers.length && !payments.length && !supports.length) return '选择任意一份普通材料，再确认创建。';
    if (needsSelectionConfirmation) return '材料或费用版本已变化，请重新点选材料并确认后保存。';
    if (vouchers.some(card => card.project_id && card.project_id !== projectId)) return '所选票据属于另一项目，请切换项目后重新选择。';
    if (payments.some(card => card.project_id && card.project_id !== projectId)) return '付款截图属于另一项目，请重新选择。';
    return '';
  }
  function readFields() {
    const person = el('matchingReimbursementPerson')?.value || '';
    return {project_type: el('matchingProjectType')?.value || '', amount: el('matchingAmount')?.value.trim() || '', project_name: el('matchingProjectName')?.value.trim() || '', ...(person ? {reimbursement_person_id: person} : {})};
  }
  function amountError(value = readFields().amount) {
    if (!value) return '请确认金额；未识别的金额需手动填写。';
    return /^\d+(?:\.\d{1,2})?$/.test(value) && Number.isFinite(Number(value)) && Number(value) * 100 <= Number.MAX_SAFE_INTEGER ? '' : '金额请填非负数字，最多两位小数。';
  }
  function fieldsError(fields = readFields()) {
    if (window.reimbursementPeopleUI?.choice([...currentVouchers(), ...currentPayments(), ...currentSupports()]).conflict && !fields.reimbursement_person_id) return '请选择费用归属人，所选材料将按确认的费用归属人记录。';
    const type = board.report_types.find(value => value.code === fields.project_type);
    if (!type) return '请选择项目类型。';
    if (amountError(fields.amount)) return amountError(fields.amount);
    if (!fields.project_name) return '请选择或填写项目名称。';
    if (fields.project_name.length > 200) return '项目名称最多200字。';
    if (!type.editable_name && !type.names.includes(fields.project_name)) return '项目名称须选择当前类型对应的细则。';
    return '';
  }
  function updateSaveState() {
    const invalid = selectionError() || fieldsError();
    el('matchingSave').disabled = busy || !!invalid;
    const target = el('matchingFieldsError');
    if (target) {target.textContent = fieldsError(); target.hidden = !target.textContent;}
    if (el('matchingProjectName')) el('matchingProjectName').disabled = busy || !board.report_types.some(type => type.code === readFields().project_type);
  }
  function recognizedFields(vouchers, payments, supports) {
    const primary = vouchers.length ? vouchers : payments.length ? payments : supports;
    const voucherCents = vouchers.length ? totalAmount(vouchers) : null, paymentCents = payments.length ? totalAmount(payments) : null;
    const supportCents = !vouchers.length && !payments.length && supports.length === 1 ? totalAmount(supports) : null;
    const cents = Number.isSafeInteger(voucherCents) ? voucherCents : Number.isSafeInteger(paymentCents) ? paymentCents : supportCents;
    const codes = primary.map(card => reportChoice(card).project_type || ''), names = primary.map(card => String(reportChoice(card).project_name || '').trim());
    const type = codes.length && new Set(codes).size === 1 ? board.report_types.find(value => value.code === codes[0]) : null;
    let name = '';
    if (type?.editable_name) name = [...new Set(names.filter(Boolean))].join('、').slice(0, 200);
    else if (type && new Set(names).size === 1 && type.names.includes(names[0])) name = names[0];
    const person = window.reimbursementPeopleUI?.choice([...vouchers, ...payments, ...supports]).personId;
    return {project_type: type && (type.editable_name || name) ? type.code : '',
      amount: Number.isSafeInteger(cents) ? (cents / 100).toFixed(2) : '', project_name: name, ...(person ? {reimbursement_person_id: person} : {})};
  }
  function nameField(fields) {
    const type = board.report_types.find(value => value.code === fields.project_type);
    const suggestions = [...new Set([...currentVouchers(), ...currentPayments(), ...currentSupports()].map(card => reportChoice(card).project_name).filter(Boolean))];
    return type?.editable_name ? `<input id="matchingProjectName" required list="matchingNameSuggestions" value="${escape(fields.project_name)}" maxlength="200" placeholder="填写项目名称"><datalist id="matchingNameSuggestions">${suggestions.map(name => `<option value="${escape(name)}"></option>`).join('')}</datalist>` :
      `<select id="matchingProjectName" required ${type ? '' : 'disabled'}><option value="">${type ? '选择对应细则' : '先选择项目类型'}</option>${(type?.names || []).map(name => `<option value="${escape(name)}" ${name === fields.project_name ? 'selected' : ''}>${escape(name)}</option>`).join('')}</select>`;
  }
  function candidateFor(card) {
    if (!projectId || !isSupplementCard(card) || card.project_id !== projectId) return null;
    const candidates = board.supplement_candidates.filter(value => value.support_id === queueId(card) || value.key === card.key);
    if (candidates.length !== 1) return null;
    const candidate = candidates[0], group = board.matches.find(value => (value.group_id || value.id) === candidate.group_id);
    return candidate.can_supplement === true && candidate.expected_version && candidate.revision === card.revision && (!group || canSupplement(group)) ? candidate : null;
  }
  function proof(card, className = '') {
    const thumbnail = thumbnailUrl(card), url = originalUrl(card);
    const content = thumbnail ? `<img src="${escape(thumbnail)}" alt="${escape(materialTitle(card))}" loading="lazy">` : '<span class="matching-pdf-placeholder">PDF 原件</span>';
    return url ? `<a class="matching-proof ${className}" href="${escape(url)}" target="_blank" rel="noopener">${content}<span>放大原件 ↗</span></a>` : `<div class="matching-proof ${className}">${content}</div>`;
  }
  function materialButton(card, action, label, allowed) {
    if (!allowed) return '';
    if (card.key?.startsWith('e:')) return `<button type="button" class="matching-edit-card ${action === 'delete' ? 'matching-delete' : ''}" data-expense-action="${action}" data-expense-key="${escape(card.key)}">${label}</button>`;
    if (!card.key?.startsWith('q:') || card.item_id) return '';
    return `<button type="button" class="matching-edit-card ${action === 'delete' ? 'matching-delete' : ''}" data-material-action="${action}" data-material-id="${escape(queueId(card))}">${label}</button>`;
  }
  const archiveButton = card => materialButton(card, 'archive', '归档', card.key?.startsWith('e:') ? card.can_archive === true : card.can_archive ??
    (!['unused', 'archived', 'deleted', 'linked', 'duplicate', 'converted'].includes(card.status)));
  function archivedMarkup(card, deleted = false) {
    const expense = card.key?.startsWith('e:');
    const restore = deleted ? materialButton(card, 'restore-deleted', '恢复到归档', expense ? card.can_restore_deleted === true : card.can_restore_deleted ?? true) :
      materialButton(card, 'restore', '恢复到合成台', expense ? card.can_restore === true : card.can_restore ?? card.status === 'archived');
    const remove = deleted ? '' : materialButton(card, 'delete', '删除', expense ? card.can_delete === true : card.can_delete ?? ['unused', 'archived'].includes(card.status));
    return `<article class="matching-other-card" data-archived-key="${escape(card.key || '')}">${proof(card)}<strong>${escape(materialTitle(card))}</strong><b>${currency(card.amount_cents)}</b>
      ${window.reimbursementPeopleUI?.markup(card) || ''}
      ${card.fields?.merchant ? `<p>商家：${escape(card.fields.merchant)}</p>` : ''}
      ${card.status === 'unused' || card.unused_reason ? `<p>${escape(card.unused_reason || '高铁支付截图无需参与合成。')}</p>` : ''}
      <div class="matching-material-actions">${restore}${remove}</div></article>`;
  }
  function cardMarkup(card, pool) {
    const selected = pool === 'voucher' ? voucherKeys.has(card.key) : pool === 'payment' ? paymentIds.has(card.id) : supportIds.has(queueId(card));
    const attribute = pool === 'voucher' ? `data-voucher-key="${escape(card.key)}"` : pool === 'payment' ? `data-payment-id="${escape(card.id)}"` : `data-support-id="${escape(queueId(card))}"`;
    const thumbnail = thumbnailUrl(card), candidate = pool === 'support' ? candidateFor(card) : null, title = materialTitle(card);
    const goods = card.fields?.goods;
    const duplicatePaymentPurpose = pool === 'payment' && sameDisplayName(goods, title) &&
      [card.fields?.shop_name, card.fields?.merchant].some(value => sameDisplayName(value, title));
    return `<article class="matching-card-shell"><button type="button" class="matching-card ${selected ? 'selected' : ''}" ${attribute} aria-pressed="${selected}">
      <span class="matching-card-image">${thumbnail ? `<img src="${escape(thumbnail)}" alt="" loading="lazy">` : '<span>PDF</span>'}</span>
      <strong>${escape(title)}</strong><b>${currency(card.amount_cents)}</b>
      ${window.reimbursementPeopleUI?.markup(card) || ''}
      ${transactionMarkup(card, pool, title)}
      ${goods && !duplicatePaymentPurpose ? `<small>用途：${escape(goods)}</small>` : ''}
      ${pool === 'voucher' && card.key?.startsWith('e:') ? '<small>已有费用 · 补入／合并</small>' : ''}
      ${card.payment_not_required ? '<small>高铁发票无需支付截图，可直接确认创建。</small>' : ''}
      ${recognitionMarkup(card)}
      ${card.agent_review_reason ? `<small class="matching-card-issues">自动整理未能确定组合：${escape(card.agent_review_reason)}</small>` : ''}
      ${card.ai_analysis?.status === 'failed' && card.ai_analysis?.message ? `<small class="matching-card-issues">${escape(card.ai_analysis.message)}</small>` : ''}</button>${candidate ? `<div class="matching-candidate"><p>对应已有费用：${escape(candidate.title || '已有费用')}</p><small>${escape(candidate.reason || '补充材料不计金额')}</small><button type="button" data-supplement-candidate="${escape(queueId(card))}">补入已有组合</button></div>` : ''}${card.key?.startsWith('q:') && !isQr(card) ? `<button type="button" class="matching-edit-card" data-classify-id="${escape(queueId(card))}">${pool === 'support' ? '确认用途' : '确认金额／用途'}</button>${card.ai_analysis?.status === 'failed' ? `<button type="button" class="matching-edit-card" data-prepare-id="${escape(queueId(card))}">重新识别</button>` : ''}` : ''}${card.key?.startsWith('e:') && card.can_edit === true ? `<button type="button" class="matching-edit-card" data-edit-expense="${escape(card.key)}">确认金额／用途</button>` : ''}${archiveButton(card)}</article>`;
  }
  function drawPools() {
    el('matchingVoucherCount').textContent = `${board.vouchers.length} 份`;
    el('matchingPaymentCount').textContent = `${board.payments.length} 份`;
    el('matchingOtherCount').textContent = `${board.other.length} 份`;
    el('matchingUnusedCount').textContent = `${board.archived.length} 份`;
    el('matchingUnusedSection').hidden = !board.archived.length && !board.deleted.length;
    el('matchingUnused').innerHTML = board.archived.map(card => archivedMarkup(card)).join('') || '<p class="matching-empty">暂无归档材料。</p>';
    el('matchingDeletedCount').textContent = `${board.deleted.length} 份`;
    el('matchingDeletedSection').hidden = !board.deleted.length;
    el('matchingDeleted').innerHTML = board.deleted.map(card => archivedMarkup(card, true)).join('');
    el('matchingGroupCount').textContent = `${board.matches.length} 组`;
    el('matchingVouchers').innerHTML = board.vouchers.map(card => cardMarkup(card, 'voucher')).join('') || '<p class="matching-empty">还没有待配对票据。上传发票 PDF 或收据图片即可。</p>';
    el('matchingPayments').innerHTML = board.payments.map(card => cardMarkup(card, 'payment')).join('') || '<p class="matching-empty">还没有待配对付款截图。</p>';
    el('matchingOther').innerHTML = board.other.map(card => isCreateSupport(card) ? cardMarkup(card, 'support') : `<article class="matching-other-card">
      ${proof(card)}<strong>${escape(materialTitle(card))}</strong><b>${currency(card.amount_cents)}</b>
      ${window.reimbursementPeopleUI?.markup(card) || ''}
      <p>${isQr(card) ? '待领票：这份二维码截图还不是发票 PDF。原件已保留，可重试领取。' : '购买凭证用途尚未确认，请先确认用途，再作为票据或补充材料选择。'}</p>
      ${isQr(card) && card.claim_message ? `<p class="muted">${escape(publicClaimText(card.claim_message))}</p>` : ''}
      ${(card.issues || []).length ? `<p class="muted">${escape(card.issues.join('、'))}</p>` : ''}
      <button type="button" ${isQr(card) ? 'data-prepare-id' : 'data-classify-id'}="${escape(queueId(card))}">${isQr(card) ? '重试领票' : '确认用途'}</button>${archiveButton(card)}</article>`).join('') || '<p class="matching-empty">没有其他待处理材料。</p>';
    el('matchingGroups').innerHTML = board.matches.map(group => {
      const id = group.group_id || group.id, version = groupVersion(group);
      const voucherCount = group.voucher_count ?? group.voucher_keys?.length ?? 1;
      const paymentCount = group.payment_count ?? group.payment_ids?.length;
      const supportCount = group.support_count ?? group.support_ids?.length ?? 0;
      const amountText = group.amount_pending ? ' · 金额待确认' : group.mode === 'registration' || !voucherCount ?
        Number.isSafeInteger(group.amount_cents) ? ' · 费用金额 ' + currency(group.amount_cents) : '' :
        Number.isSafeInteger(group.voucher_total_cents) ? ' · 票据合计 ' + currency(group.voucher_total_cents) : '';
      return `<article class="matching-group"><div><strong>${escape(group.title || group.item?.title || '已配对费用')}</strong><p>${voucherCount} 张票据 · ${paymentCount === undefined ? '付款张数未记录' : paymentCount + ' 张付款'} · ${supportCount} 份补充附件${amountText}</p><p>${escape(group.reason || '已暂关联，等待核对')}</p></div><div class="matching-group-actions"><button type="button" data-supplement-group="${escape(id)}" disabled>补充选中材料</button><button type="button" data-undo-group="${escape(id)}" data-group-version="${escape(version)}" ${version ? '' : 'disabled'}>撤回配对</button></div></article>`;
    }).join('') || '<p class="matching-empty">配对后可在这里撤回。</p>';
  }
  function updatePoolSelection() {
    // Selection changes no material data: keep thumbnails, focus and scroll in place.
    for (const button of dialog.querySelectorAll('.matching-card')) {
      const selected = button.dataset.voucherKey ? voucherKeys.has(button.dataset.voucherKey) :
        button.dataset.paymentId ? paymentIds.has(button.dataset.paymentId) : supportIds.has(button.dataset.supportId);
      button.classList.toggle('selected', selected);
      button.setAttribute('aria-pressed', String(selected));
    }
  }
  function renderSelection(preservedFields = null) {
    const vouchers = currentVouchers(), payments = currentPayments(), supports = currentSupports(), error = selectionError();
    const voucherTotal = totalAmount(vouchers), paymentTotal = totalAmount(payments);
    const difference = Number.isSafeInteger(voucherTotal) && Number.isSafeInteger(paymentTotal) ? voucherTotal - paymentTotal : null;
    el('matchingAmountStatus').textContent = error || `已选 ${vouchers.length + payments.length + supports.length} 份材料`;
    el('matchingAmountStatus').classList.toggle('matching-balanced', !error && difference === 0);
    const target = el('matchingPreviewContent'); target.classList.toggle('matching-suggestion-preview', !!sourceSuggestion);
    dialog.querySelector('.matching-footer').classList.toggle('matching-review-actions', !!(vouchers.length + payments.length + supports.length));
    updateSupplementButtons();
    updatePeopleButton();
    if (!vouchers.length && !payments.length && !supports.length) {
      fieldSelection = '';
      if (savedItem) {
        target.innerHTML = `<h3>${escape(savedItem.title || '已配对费用')} · 最终打印预览</h3><p>材料已进入待核对。</p><p id="matchingPrintStatus" role="status">正在生成实际打印页…</p><a id="matchingPrintPdf" target="_blank" rel="noopener" hidden>放大／下载打印 PDF ↗</a><div id="matchingPrintPages"></div>`;
        renderPrint(savedItem);
      } else target.innerHTML = '<div class="matching-preview-empty"><span>① ／ ② ／ ③</span><h3>在左侧选择任意材料</h3><p>只有票据、只有付款或只有普通材料，都可以确认创建。</p></div>';
      updateSaveState();
      return;
    }
    const key = JSON.stringify([vouchers.map(card => card.key), payments.map(queueId), supports.map(queueId)]);
    const fields = preservedFields || (key === fieldSelection && el('matchingProjectType') ? readFields() : recognizedFields(vouchers, payments, supports));
    fieldSelection = key;
    const existing = vouchers.filter(card => card.key.startsWith('e:'));
    const reviewNotes = sourceSuggestion ? suggestionReviewNotes(sourceSuggestion) : [];
    target.innerHTML = `<h3 class="matching-preview-heading" id="matchingReviewInfo" tabindex="-1">确认报销信息 · 原件预览</h3>
      <nav class="matching-preview-nav" aria-label="核对材料跳转"><button type="button" data-matching-jump="matchingReviewInfo">报销信息</button><button type="button" data-matching-jump="matchingVoucherSection" ${vouchers.length ? '' : 'disabled'}>票据 ${vouchers.length}</button><button type="button" data-matching-jump="matchingPaymentSection" ${payments.length ? '' : 'disabled'}>付款 ${payments.length}</button><button type="button" data-matching-jump="matchingSupportSection" ${supports.length ? '' : 'disabled'}>附件 ${supports.length}</button></nav>
      ${reviewNotes.length ? `<div id="matchingSuggestionReason" class="rule warn">${escape(reviewNotes.join('；'))}</div>` : ''}
      <div class="matching-transaction-summary">${[...vouchers, ...payments].map(card => `<div>${transactionMarkup(card, card.role === 'payment' ? 'payment' : 'voucher')}</div>`).join('')}</div>
      ${window.reimbursementPeopleUI?.choiceControl([...vouchers, ...payments, ...supports], fields.reimbursement_person_id || '', 'id="matchingReimbursementPerson"') || ''}
      <div class="matching-create-fields"><label>项目类型<select id="matchingProjectType" required><option value="">选择项目类型</option>${board.report_types.map(type => `<option value="${escape(type.code)}" ${type.code === fields.project_type ? 'selected' : ''}>${escape(type.label)}</option>`).join('')}</select></label><label>金额（元）<input id="matchingAmount" required inputmode="decimal" value="${escape(fields.amount)}" maxlength="20" placeholder="请确认金额"></label><label class="matching-name-field">项目名称<span id="matchingNameControl">${nameField(fields)}</span></label><p id="matchingFieldsError" role="alert" hidden></p></div>
      ${existing.length ? `<p id="matchingExistingNotice">${existing.length === 1 ? '材料将补入所选已有费用，确认信息会更新该费用，不会重复新增。' : '所选已有费用和材料将合并为一笔报销费用，不会各自重复计费。'}</p>` : ''}
      <div class="matching-totals" aria-label="所选材料金额核对"><div><span>票据 <b id="matchingSelectedVoucherCount">${vouchers.length}</b> 张</span><strong id="matchingVoucherTotal">${currency(voucherTotal)}</strong></div><div><span>付款 <b id="matchingSelectedPaymentCount">${payments.length}</b> 张</span><strong id="matchingPaymentTotal">${currency(paymentTotal)}</strong></div><div><span>差额（票据－付款）</span><strong id="matchingDifference" class="${difference === 0 ? 'matching-balanced' : ''}">${currency(difference)}</strong></div></div>
      <div class="matching-payment-heading" id="matchingVoucherSection" tabindex="-1"><h4>所选发票／收据 · ${vouchers.length} 张</h4></div>
      <div class="matching-voucher-proofs ${vouchers.length === 1 ? 'matching-single-voucher' : ''}">${vouchers.map(card => `<figure>${proof(card, 'matching-voucher-proof')}<figcaption>${escape(materialTitle(card))} · ${currency(card.amount_cents)}</figcaption></figure>`).join('') || '<p class="matching-empty">从左上方选择对应票据，可选多张。</p>'}</div>
      <div class="matching-payment-heading" id="matchingPaymentSection" tabindex="-1"><h4>对应付款 · ${payments.length} 张</h4></div>
      <div class="matching-payment-proofs">${payments.map(card => `<figure>${proof(card)}<figcaption>${escape(materialTitle(card))} · ${currency(card.amount_cents)}</figcaption></figure>`).join('') || '<p class="matching-empty">从左下方选择付款截图。</p>'}</div>
      <div class="matching-payment-heading" id="matchingSupportSection" tabindex="-1"><h4>其他／补充材料 · <span id="matchingSelectedSupportCount">${supports.length}</span> 份</h4></div><p class="muted">有票据或付款时，这些材料作为补充附件，不叠加金额。</p>
      <div class="matching-support-proofs">${supports.map(card => `<figure>${proof(card)}<figcaption>${escape(materialTitle(card))}</figcaption></figure>`).join('') || '<p class="matching-empty">第三栏可选择运单、明细等材料作为补充附件，不计金额。</p>'}</div>`;
    updateSaveState();
  }
  async function renderPrint(item) {
    const pages = el('matchingPrintPages'), status = el('matchingPrintStatus'), link = el('matchingPrintPdf');
    const current = () => pages.isConnected && savedItem?.id === item.id && savedItem?.version === item.version && !voucherKeys.size && !paymentIds.size && !supportIds.size;
    const key = `${item.id}:${item.version}`;
    try {
      if (!printRequests.has(key)) printRequests.set(key, api('expense-print-preview', {id: item.id, expected_version: item.version}));
      const result = await printRequests.get(key);
      if (!current()) return;
      if (result.id !== item.id || result.version !== item.version) throw Error('费用版本已变化，请刷新后查看。');
      if (!result.preview_pages?.length) {
        status.textContent = `仍需补材料：${(result.missing || item.missing || []).join('、') || '请在待核对列表查看'}。`;
        return;
      }
      status.textContent = `${result.complete ? '' : '仍缺：' + (result.missing || item.missing || []).join('、') + '；以下为已有材料。'}${result.page_count} 页 A4 · 打印选择 A4、每张纸 1 页。`;
      link.href = '/export/' + encodeURIComponent(result.pdf); link.hidden = false;
      pages.innerHTML = result.preview_pages.map((name, index) => `<figure><img src="/export/${encodeURIComponent(name)}" alt="实际打印第 ${index + 1} 页"><figcaption>A4 · 第 ${index + 1} 页</figcaption></figure>`).join('');
    } catch (error) {
      printRequests.delete(key);
      if (!current()) return;
      status.textContent = `实际打印页暂不可用：${error.message}`;
      const retry = document.createElement('button'); retry.type = 'button'; retry.textContent = '重试打印预览';
      retry.onclick = () => {retry.remove(); status.textContent = '正在生成实际打印页…'; renderPrint(item);}; pages.append(retry);
    }
  }
  async function refreshBoard(preserve = true, preservedFields = null) {
    const revision = ++fetchRevision, old = board;
    if (!projectId) {board = {vouchers: [], payments: [], other: [], archived: [], deleted: [], matches: [], report_types: [], supplement_candidates: [], suggestions: []}; drawPools(); renderSelection(); return;}
    const [res] = await Promise.all([
      fetch('/api/material-matching?' + new URLSearchParams({project: projectId})),
      window.reimbursementPeopleUI?.refresh(projectId).catch(() => { /* Personnel have their own retry. */ }),
    ]);
    const result = await res.json();
    if (!res.ok) throw Error(result.error || '无法读取材料合成台');
    if (revision !== fetchRevision) return;
    board = {vouchers: result.vouchers || [], payments: result.payments || [], other: result.other || [], unused: result.unused || [],
      archived: result.archived || result.unused || [], deleted: result.deleted || [], matches: result.matches || [], report_types: result.report_types || [], supplement_candidates: result.supplement_candidates || [], suggestions: result.suggestions || [], ai: result.ai};
    window.setSynthesisBoard?.(result, projectId);
    if (sourceSuggestion && !board.suggestions.some(value => value.id === sourceSuggestion.id && value.snapshot_token === sourceSuggestion.snapshot_token)) {
      sourceSuggestion = null; needsSelectionConfirmation = true;
    }
    el('matchingAuto').textContent = '寻找合成组合';
    voucherKeys = new Set([...voucherKeys].filter(key => preserve &&
      board.vouchers.some(card => card.key === key) &&
      signature(board.vouchers.find(card => card.key === key)) === signature(old.vouchers.find(card => card.key === key))));
    paymentIds = new Set([...paymentIds].filter(id => preserve && signature(board.payments.find(card => card.id === id)) === signature(old.payments.find(card => card.id === id)) && board.payments.some(card => card.id === id)));
    supportIds = new Set([...supportIds].filter(id => preserve && board.other.some(card => queueId(card) === id && isCreateSupport(card)) &&
      signature(board.other.find(card => queueId(card) === id)) === signature(old.other.find(card => queueId(card) === id))));
    drawPools(); renderSelection(preservedFields); lock(busy);
  }
  async function refreshLedger() {await load();}
  async function mutation(action, preservedFields = null) {
    if (busy) return;
    if (!projectId || projectId !== currentInvoiceProjectId()) {message('请先从侧栏选择一个项目，再打开材料合成台。', true); return;}
    lock(true);
    try {await action();}
    catch (error) {
      if (/版本|revision|过期/.test(error.message)) needsSelectionConfirmation = true;
      message(`${error.message}。材料原件保留；请核对刷新后的材料，再操作。`, true);
      try {await refreshBoard(true, preservedFields);} catch (refreshError) {message(`${error.message}；刷新失败：${refreshError.message}`, true);}
    } finally {lock(false);}
  }
  function changeMaterial(id, action) {
    const card = [...board.vouchers, ...board.payments, ...board.other, ...board.archived, ...board.deleted]
      .find(value => value.key?.startsWith('q:') && queueId(value) === id);
    if (!card) return;
    const fields = el('matchingProjectType') ? readFields() : null;
    mutation(async () => {
      if (card.project_id && !projectId) throw Error('先选择材料所属项目，再操作归档。');
      const result = await api('intake/' + action, {id, revision: card.revision, project_id: projectId});
      await refreshBoard(true, fields);
      message(result.message || ({archive: '材料已归档，不再参与合成。', restore: '材料已恢复到合成台。', delete: '材料已删除，可在归档的已删除列表恢复。', 'restore-deleted': '材料已恢复到归档。'}[action]));
    }, fields);
  }
  function changeExpense(key, action) {
    const card = [...board.vouchers, ...board.archived, ...board.deleted].find(value => value.key === key);
    const capability = 'can_' + action.replace('-', '_');
    if (!card?.key?.startsWith('e:') || card[capability] !== true || !card.version) return;
    const fields = el('matchingProjectType') ? readFields() : null;
    mutation(async () => {
      const result = await api('synthesis-expense/' + action, {id: card.item_id || key.slice(2), expected_version: card.version, project_id: projectId});
      await refreshBoard(true, fields); await refreshLedger();
      message(result.message || '费用材料状态已更新，原件保留。');
    }, fields);
  }
  function editExpense(key) {
    const card = board.vouchers.find(value => value.key === key);
    if (!card?.key?.startsWith('e:') || card.can_edit !== true || !card.version || !projectId) return;
    const selectedFields = el('matchingProjectType') ? readFields() : null;
    const editor = document.createElement('dialog'); editor.id = 'synthesisExpenseEditor'; editor.className = 'matching-classify';
    editor.innerHTML = `<div class="dialoghead"><h2>确认金额／用途</h2><button type="button" data-close class="dialog-close" aria-label="关闭">×</button></div><p>${escape(card.title || card.name)} · 只修改费用材料的信息，原件保留；配对后仍需核对。</p><form novalidate><label>金额（元）<input name="amount" inputmode="decimal" maxlength="20" value="${Number.isSafeInteger(card.amount_cents) ? (card.amount_cents / 100).toFixed(2) : ''}" required></label><label>名称／用途<input name="title" maxlength="200" value="${escape(card.title || card.name || '')}" required></label><p data-error role="alert" hidden></p><div class="actions"><button type="submit" class="primary">保存金额／用途</button></div></form>`;
    document.body.append(editor); editor.showModal();
    const form = editor.querySelector('form'), submit = form.querySelector('[type=submit]'), errorTarget = editor.querySelector('[data-error]');
    let saving = false, expired = false;
    const validate = () => amountError(form.elements.amount.value.trim()) || (!form.elements.title.value.trim() ? '请填写名称／用途。' : form.elements.title.value.trim().length > 200 ? '名称／用途最多200字。' : '');
    const update = () => {submit.disabled = saving || expired || !!validate();};
    form.oninput = update; update();
    editor.querySelector('[data-close]').onclick = () => {if (!saving) editor.remove();};
    editor.oncancel = event => {event.preventDefault(); if (!saving) editor.remove();};
    form.onsubmit = async event => {
      event.preventDefault(); if (saving || expired || validate()) return;
      if (projectId !== currentInvoiceProjectId()) {expired = true; errorTarget.textContent = '项目已变化，请关闭后重新打开。'; errorTarget.hidden = false; update(); return;}
      saving = true; lock(true); editor.querySelectorAll('button,input').forEach(control => control.disabled = true);
      try {
        const result = await api('synthesis-expense/edit-fields', {id: card.item_id || key.slice(2), expected_version: card.version, project_id: projectId, amount: form.elements.amount.value.trim(), title: form.elements.title.value.trim()});
        if (result.item?.id !== (card.item_id || key.slice(2)) || !result.item?.version) {expired = true; throw Error('保存结果无法确认，请关闭并刷新材料。');}
        editor.remove(); await refreshBoard(true, selectedFields); await refreshLedger(); message(result.message || '金额／用途已保存，请选择材料后继续配对。');
      } catch (error) {
        if (!editor.isConnected) {message('金额／用途已保存；材料刷新失败：' + error.message + '。请刷新后继续，勿重复保存。', true); return;}
        expired = expired || error.status === 409 || error.status === 403 || /版本|变化|revision|过期|已关联|已归档|已删除/.test(error.message);
        errorTarget.textContent = error.message + (expired ? '；请关闭后重新打开当前材料。' : '；输入已保留，可稍后重试。'); errorTarget.hidden = false;
        if (expired) try {await refreshBoard(true, selectedFields);} catch (_) { /* Retain the entered values and original error. */ }
      } finally {
        saving = false; lock(false); if (editor.isConnected) {editor.querySelectorAll('button,input').forEach(control => control.disabled = false); update();}
      }
    };
  }
  async function autoMatch() {
    const result = await api('material-matching/auto', {project_id: projectId});
    const explanations = [...(result.issues || []), ...(result.conflicts || []).map(conflict =>
      Number.isSafeInteger(conflict.amount_cents) ? `${currency(conflict.amount_cents)}：${conflict.reason}` : conflict.reason)]
      .filter(value => typeof value === 'string' && value.trim() && !/^系统候选进入待合成[；;]/.test(value.trim())).map(value => value.trim());
    return {suggested: result.suggested ?? result.suggestions?.length ?? 0,
      feedback: [...new Set(explanations)].join(' ')};
  }
  async function uploadFiles(files) {
    if (busy || !files.length) return;
    if (files.length > 50) {message('每批最多50份，请分批上传。', true); return;}
    if (files.some(file => file.size > 20 * 1024 * 1024) || files.reduce((sum, file) => sum + file.size, 0) > 200 * 1024 * 1024) {message('每份最多20MB，每批合计最多200MB，请分批上传。', true); return;}
    const batchPerson = el('matchingBatchPerson').value;
    const uploadProject = projectId, agent = window.manualInvoiceAgent;
    const organizer = uploadProject && typeof agent?.shouldAutoOrganize === 'function' && typeof agent?.startReceived === 'function' && agent.shouldAutoOrganize(uploadProject) ? agent : null;
    const background = !organizer && window.invoiceIntakeProgress?.available();
    lock(true);
    el('matchingUploadProgress').hidden = false;
    const batch = crypto.randomUUID(), failures = [], entryIds = new Set();
    let received = 0, duplicates = 0, converted = 0, unused = 0, suggested = 0, recognitionReviews = 0, autoFeedback = '';
    el('matchingProgressBar').max = files.length; el('matchingProgressBar').value = 0;
    try {
      for (const [index, file] of files.entries()) {
        el('matchingProgressText').textContent = `${index + 1} / ${files.length} · ${file.name}`;
        try {
          const data = await new Promise((resolve, reject) => {const reader = new FileReader(); reader.onload = () => resolve(reader.result.split(',')[1]); reader.onerror = reject; reader.readAsDataURL(file);});
          const entry = await api('intake/receive', {name: file.name, data, project_id: uploadProject, batch, reimbursement_person_id: batchPerson}); received++;
          if (entry.duplicate || entry.status === 'duplicate') duplicates++;
          else if (organizer || background) {
            if (typeof entry.id === 'string' && entry.id && entry.project_id === uploadProject && !entry.item_id && ['received', 'review'].includes(entry.status)) entryIds.add(entry.id);
          } else {const prepared = await api('intake/prepare', {id: entry.id}); if (prepared.converted_to_pdf) converted++;
            if (prepared.entry?.status === 'unused') unused++;
            else if (['failed', 'review'].includes(prepared.entry?.ai_analysis?.status)) recognitionReviews++;}
        } catch (error) {failures.push(`${file.name}：${error.message}`);}
        el('matchingProgressBar').value = index + 1;
      }
      if (organizer) {
        // Receiving is already committed. Neither a failed start nor a later read
        // may send the files again or start another task for this batch.
        el('matchingFiles').value = '';
        let started = false, startError = '';
        if (entryIds.size) {
          try {await organizer.startReceived({projectId: uploadProject, entryIds: [...entryIds], batch}); started = true;}
          catch (error) {startError = `自动整理未能启动：${error.message}。已收到的原件保留，请在自动整理助手中查看或重试。`;}
        }
        const summary = `本批 ${files.length} 份 · 已保存 ${received} 份 · 重复 ${duplicates} 份 · 上传失败 ${failures.length} 份。` +
          (started ? `已将本批 ${entryIds.size} 份新材料交给自动整理；缺少依据时会在当前工作台询问。` : startError || '本批没有可交给自动整理的新材料；原件及已有费用关联保持。');
        el('matchingProgressText').textContent = failures.join('\n') || startError || '收件完成。';
        if (started) {
          closeMatchingAfterCommit(); notify(summary, !!failures.length);
          try {await organizer.open?.();} catch (error) {notify(summary + ` 助手显示未完成：${error.message}；任务已启动，请刷新查看，无需重新上传。`, true);}
        }
        else {
          try {await refreshBoard();} catch (error) {failures.push(`材料刷新未完成：${error.message}`);}
          message(summary + (failures.length ? ' ' + failures.join('；') : ''), !!failures.length || !!startError);
        }
        return;
      }
      if (background) {
        el('matchingFiles').value = '';
        try {
          if (entryIds.size) await window.invoiceIntakeProgress.enqueue(batch, [...entryIds], uploadProject);
          message(`本批已保存 ${received} 份 · 重复 ${duplicates} 份 · 上传失败 ${failures.length} 份。${entryIds.size ? '已交给后台逐份识别，可以关闭窗口；完成后会刷新建议组合。' : '没有新的待识别材料。'}`, !!failures.length);
        } catch (error) {
          message(`原件已保存，后台识别未能启动：${error.message}。可刷新后点击继续识别。`, true);
          void window.invoiceIntakeProgress.refresh(uploadProject);
        }
        el('matchingProgressText').textContent = '文件已保存，可以关闭窗口。';
        void refreshBoard().catch(error => message(error.message, true)); return;
      }
      try {const automatic = await autoMatch(); suggested = automatic.suggested; autoFeedback = automatic.feedback;}
      catch (error) {failures.push(`自动配对：${error.message}`);}
      await refreshBoard(); await refreshLedger();
      message(`本批 ${files.length} 份 · 已保存 ${received} 份 · 重复 ${duplicates} 份 · 失败 ${failures.length} 项 · 转为发票 PDF ${converted} 份 · 建议组合 ${suggested} 组 · 无需使用 ${unused} 份。${recognitionReviews ? ` ${recognitionReviews}份识别结果需人工处理，原件留在材料区。` : ''}${failures.length ? '已收到的原件保留，可在材料区重试。' : '建议组合已显示在左侧，可直接预览并确认进入待核对。'}${autoFeedback ? ' ' + autoFeedback : ''}`, !!failures.length || recognitionReviews > 0);
      el('matchingProgressText').textContent = failures.length ? failures.join('\n') :
        recognitionReviews ? '收件完成，存疑材料留待人工处理。' : '收件与配对完成。';
    } catch (error) {message(`${error.message}；已收到的原件保留。`, true);}
    finally {el('matchingFiles').value = ''; lock(false);}
  }
  function classify(card) {
    const editor = document.createElement('dialog'); editor.className = 'matching-classify';
    editor.innerHTML = `<div class="dialoghead"><h2>确认材料用途与金额</h2><button type="button" data-close class="dialog-close" aria-label="关闭">×</button></div><p>${escape(card.name)} · 只整理本份材料，保存后仍需核对。</p>
      <form><label>材料用途<select name="role"><option value="invoice">发票</option><option value="receipt">收据</option><option value="payment">付款截图</option><option value="other">其他材料</option></select></label>
      <label>材料名称（可选）<input name="title" value="${escape(card.fields?.title || card.name)}" maxlength="200"></label>
      <label>金额（元）<input name="amount" type="number" min="0" step="0.01" value="${Number.isSafeInteger(card.amount_cents) ? (card.amount_cents / 100).toFixed(2) : ''}" required></label>
      <p class="muted">报销分类在确认创建时选择；商品说明和付款备注缺失不影响配对。</p>
      <p data-error role="alert" hidden></p><div class="actions"><button type="submit" class="primary">保存材料分类</button></div></form>`;
    document.body.append(editor); editor.showModal();
    editor.querySelector('select[name="role"]').value = ['invoice', 'receipt', 'payment', 'other'].includes(card.role) ? card.role : 'other';
    let saving = false;
    editor.querySelector('[data-close]').onclick = () => {if (!saving) editor.remove();};
    editor.addEventListener('cancel', event => {if (saving) event.preventDefault(); else editor.remove();});
    editor.querySelector('form').onsubmit = async event => {
      event.preventDefault(); if (saving) return; saving = true;
      editor.querySelectorAll('button,input,select').forEach(control => control.disabled = true);
      const form = event.target;
      const fields = {title: form.elements.title.value, amount: form.elements.amount.value, project_id: projectId};
      try {
        await api('intake/classify', {id: queueId(card), revision: card.revision, role: form.elements.role.value, fields});
        editor.remove(); await refreshBoard(); message('材料分类已保存，请选择对应票据与付款。');
      } catch (error) {
        const target = editor.querySelector('[data-error]'); target.hidden = false; target.textContent = `${error.message}；请刷新材料后重新确认。`;
        try {await refreshBoard();} catch (_) { /* Keep the original error visible. */ }
        if (/版本|变化|revision|过期/.test(error.message)) {editor.remove(); message(`${error.message}；材料已刷新，请重新打开这份材料确认。`, true);}
      } finally {saving = false; if (editor.isConnected) editor.querySelectorAll('button,input,select').forEach(control => control.disabled = false);}
    };
  }
  function closeMatchingAfterCommit() {
    ++openRevision; ++fetchRevision; opening = false;
    dialog.setAttribute('aria-busy', 'false'); lock(false); dialog.close();
  }
  function closeMatching() {if (busy && !opening) return; closeMatchingAfterCommit();}
  function applySuggestion(suggestion) {
    const fresh = board.suggestions?.find(value => value.id === suggestion.id && value.snapshot_token === suggestion.snapshot_token);
    if (!dialog.open || projectId !== currentInvoiceProjectId() || suggestion.project_id !== projectId || !fresh) {
      message('这组建议、材料版本或项目已变化，请刷新材料后重新选择。', true); return false;
    }
    if (fresh.voucher_keys.some(key => !board.vouchers.some(card => card.key === key)) || fresh.payment_ids.some(id => !board.payments.some(card => card.id === id))) {
      message('这组建议的材料已变化，请刷新后重新选择。', true); return false;
    }
    sourceSuggestion = fresh; voucherKeys = new Set(fresh.voucher_keys); paymentIds = new Set(fresh.payment_ids); supportIds = new Set(fresh.support_ids || []);
    savedItem = null; needsSelectionConfirmation = false;
    updatePoolSelection(); renderSelection();
    dialog.querySelector('.matching-preview').scrollTop = 0;
    message('已预选建议组合，请在右侧核对原件、项目类型、金额和项目名称，再确认合成。');
    return true;
  }
  window.matchingSuggestionActions = {select: suggestion => !busy && applySuggestion(suggestion), refresh: () => refreshBoard(),
    isBusy: () => busy, closeAfterCommit: closeMatchingAfterCommit};
  el('matchingClose').onclick = closeMatching;
  dialog.addEventListener('cancel', event => {event.preventDefault(); closeMatching();});
  el('matchingRefresh').onclick = () => mutation(async () => {await refreshBoard(); void window.invoiceIntakeProgress?.refresh(projectId); message('材料已刷新；失效的选择已清除。');});
  el('matchingAssignPeople').onclick = async () => {
    if (busy) return;
    const cards = [...currentVouchers(), ...currentPayments(), ...currentSupports()], error = peopleSelectionError(cards);
    if (error) {message(error, true); return;}
    const selectedProject = projectId, fields = el('matchingProjectType') ? readFields() : null;
    const selectedVouchers = [...voucherKeys], selectedPayments = [...paymentIds], selectedSupports = [...supportIds];
    try {
      await window.reimbursementPeopleUI.bulkAssignMaterials(cards.map(card => ({...card, id: queueId(card)})), {
        projectId: selectedProject,
        onSaved: async result => {
          if (projectId !== selectedProject || projectId !== currentInvoiceProjectId()) throw Error('归属已保存，请在材料所属项目刷新查看。');
          lock(true);
          try {
            const preserved = {...fields, reimbursement_person_id: result.person?.id || ''};
            await refreshBoard(true, preserved);
            // Assignment changes revisions without changing the selected originals.
            const available = card => card && !peopleSelectionError([card]);
            voucherKeys = new Set(selectedVouchers.filter(key => available(board.vouchers.find(card => card.key === key))));
            paymentIds = new Set(selectedPayments.filter(id => available(board.payments.find(card => queueId(card) === id))));
            supportIds = new Set(selectedSupports.filter(id => {
              const card = board.other.find(card => queueId(card) === id);
              return available(card) && isCreateSupport(card);
            }));
            sourceSuggestion = null; needsSelectionConfirmation = false; savedItem = null;
            drawPools(); renderSelection(preserved);
            await refreshLedger();
            message(result.message || `已更新 ${cards.length} 份材料归属；金额、用途和原件保留，尚未创建费用。`);
          } finally {lock(false);}
        }
      });
    } catch (failure) {message(failure.message || '无法打开材料归属选择，请重试。', true);}
  };
  el('matchingUpload').onclick = () => {if (!busy) el('matchingFiles').click();};
  el('matchingFiles').onchange = () => uploadFiles([...el('matchingFiles').files]);
  el('matchingClaim').onclick = () => {if (!busy) window.openInvoiceClaim({project_id: projectId,
    onComplete: async result => {await refreshBoard(); await refreshLedger(); message(result.message || '原始PDF已放入材料区，请选择材料后确认创建。');}});};
  el('matchingMail').onclick = () => mutation(async () => {
    clearMailFeedback();
    message('正在补收邮件并整理材料…');
    let result;
    try {result = await api('mail', {project_id: projectId, material_only: true});}
    catch (error) {throw Error(`${error.message}；补收未完成，可再次点击“补收邮件”重试`);}
    await refreshBoard(); await refreshLedger();
    mailFeedback(result);
  });
  el('matchingAuto').onclick = () => mutation(async () => {
    message('正在寻找票据与付款的建议组合…'); const automatic = await autoMatch(); await refreshBoard();
    if (!voucherKeys.size && !paymentIds.size && !supportIds.size && board.suggestions.length === 1) applySuggestion(board.suggestions[0]);
    message((automatic.suggested ? `找到 ${automatic.suggested} 组建议组合，已显示在左侧；可直接选择并在右侧预览、确认进入待核对。` : '找到 0 组建议组合，可在下方手工选择材料，右侧预览并确认后进入待核对。') + (automatic.feedback ? ' ' + automatic.feedback : ''));
  });
  el('matchingAmountPair').onclick = () => mutation(async () => {
    const fields = el('matchingProjectType') ? readFields() : null;
    await window.openAmountPairing({projectId, types: board.report_types, cards: [...board.vouchers, ...board.payments, ...board.other, ...board.archived, ...board.deleted], setBusy: lock,
      onCommitted: async result => {savedItem = null; sourceSuggestion = null; await refreshBoard(true, fields); await refreshLedger(); message(result.message || '金额配对已确认，费用进入待核对；尚未核验或制作报销包。');}});
  });
  el('matchingSave').onclick = () => mutation(async () => {
    const invalid = selectionError() || fieldsError(); if (invalid) throw Error(invalid);
    const vouchers = currentVouchers(), payments = currentPayments(), supports = currentSupports();
    const revisions = Object.fromEntries([...payments, ...supports, ...vouchers.filter(card => card.key.startsWith('q:'))].map(card => [queueId(card), card.revision]));
    const expectedVersions = Object.fromEntries(vouchers.filter(card => card.key.startsWith('e:')).map(card => [card.item_id || card.id || card.key.slice(2), card.version]));
    const payload = {voucher_keys: vouchers.map(card => card.key), payment_ids: payments.map(queueId), support_ids: supports.map(queueId), project_id: projectId,
      expected_versions: expectedVersions, revisions, fields: readFields(),
      ...(sourceSuggestion ? {suggestion_id: sourceSuggestion.id, suggestion_token: sourceSuggestion.snapshot_token} : {})};
    const result = await api('material-matching/create', payload);
    savedItem = result.item; voucherKeys.clear(); paymentIds.clear(); supportIds.clear(); sourceSuggestion = null;
    view = 'review'; active = result.item.id; checked.clear();
    try {
      await refreshLedger(); notify('报销项目已创建并进入待核对，请检查打印页与缺项。');
      window.showCreatedMaterialExpenses([result.item]); closeMatchingAfterCommit();
    } catch (error) {
      renderSelection();
      message(`报销项目已创建，页面刷新未完成：${error.message}。请关闭合成台后刷新待核对，勿重复创建。`, true);
    }
  });
  dialog.addEventListener('change', event => {
    if (event.target.id === 'matchingProjectType') {
      const fields = readFields(); fields.project_name = '';
      el('matchingNameControl').innerHTML = nameField(fields); lock(busy);
    }
    if (['matchingProjectName', 'matchingReimbursementPerson'].includes(event.target.id)) updateSaveState();
  });
  dialog.addEventListener('input', event => {if (['matchingAmount', 'matchingProjectName'].includes(event.target.id)) updateSaveState();});
  dialog.addEventListener('click', event => {
    const button = event.target.closest('button'); if (!button || busy) return;
    if (button.dataset.matchingJump) {
      const target = el(button.dataset.matchingJump), pane = dialog.querySelector('.matching-preview');
      if (!target) return;
      const desktop = getComputedStyle(pane).overflowY === 'auto', scroller = desktop ? pane : dialog;
      const inset = desktop ? (dialog.querySelector('.matching-preview-nav')?.offsetHeight || 0) + 12 : 12;
      scroller.scrollTop += target.getBoundingClientRect().top - scroller.getBoundingClientRect().top - inset;
      target.focus({preventScroll: true});
      return;
    }
    if (button.hasAttribute('data-new-reimbursement-person')) {
      const fields = readFields();
      window.reimbursementPeopleUI?.newPerson({projectId, onCreated: person => {if (projectId === currentInvoiceProjectId()) {drawPools(); renderSelection({...fields, reimbursement_person_id: person.id});}}});
    }
    if (button.dataset.voucherKey) {sourceSuggestion = null; needsSelectionConfirmation = false; const key = button.dataset.voucherKey; if (voucherKeys.has(key)) voucherKeys.delete(key); else voucherKeys.add(key); savedItem = null; updatePoolSelection(); renderSelection();}
    if (button.dataset.paymentId) {sourceSuggestion = null; needsSelectionConfirmation = false; const id = button.dataset.paymentId; if (paymentIds.has(id)) paymentIds.delete(id); else paymentIds.add(id); savedItem = null; updatePoolSelection(); renderSelection();}
    if (button.dataset.supportId) {
      const id = button.dataset.supportId, card = board.other.find(value => queueId(value) === id);
      if (card && isCreateSupport(card)) {sourceSuggestion = null; needsSelectionConfirmation = false; if (supportIds.has(id)) supportIds.delete(id); else supportIds.add(id); savedItem = null; updatePoolSelection(); renderSelection();}
    }
    if (button.dataset.classifyId) {const card = [...board.other, ...board.vouchers, ...board.payments].find(value => value.key?.startsWith('q:') && queueId(value) === button.dataset.classifyId); if (card && !isQr(card)) classify(card);}
    if (button.dataset.materialAction) changeMaterial(button.dataset.materialId, button.dataset.materialAction);
    if (button.dataset.expenseAction) changeExpense(button.dataset.expenseKey, button.dataset.expenseAction);
    if (button.dataset.editExpense) editExpense(button.dataset.editExpense);
    if (button.dataset.prepareId) mutation(async () => {
      const result = await api('intake/prepare', {id: button.dataset.prepareId});
      await refreshBoard(); await refreshLedger();
      message(result.message || (result.converted_to_pdf ? '发票 PDF 已收到，可以选择付款配对。' : '原件已保留，请查看材料提示。'));
    });
    if (button.dataset.supplementCandidate) mutation(async () => {
      const card = board.other.find(value => queueId(value) === button.dataset.supplementCandidate), candidate = card && candidateFor(card);
      if (!candidate) throw Error('对应费用或材料已变化，请刷新后确认。');
      const result = await api('material-matching/supplement', {group_id: candidate.group_id, support_ids: [queueId(card)],
        expected_version: candidate.expected_version, revisions: {[queueId(card)]: card.revision}});
      savedItem = result.item; voucherKeys.clear(); paymentIds.clear(); supportIds.clear(); printRequests.clear();
      await refreshBoard(false); await refreshLedger(); message('材料已补入对应已有费用，费用金额不变；右侧显示更新后的实际打印页。');
    });
    if (button.dataset.supplementGroup) mutation(async () => {
      const group = board.matches.find(value => (value.group_id || value.id) === button.dataset.supplementGroup), supports = currentSupports();
      if (!group || !canSupplement(group) || !groupVersion(group)) throw Error('这组费用已变化或已核对，请刷新后确认。');
      if (!supports.length) throw Error('先在第三栏选择要补入的普通材料。');
      if (supportError()) throw Error(supportError());
      if (supports.some(card => !isSupplementCard(card))) throw Error('购买凭证补到已有配对前，请先确认材料用途。');
      const result = await api('material-matching/supplement', {group_id: group.group_id || group.id,
        support_ids: supports.map(queueId), expected_version: groupVersion(group),
        revisions: Object.fromEntries(supports.map(card => [queueId(card), card.revision]))});
      savedItem = result.item; voucherKeys.clear(); paymentIds.clear(); supportIds.clear(); printRequests.clear();
      await refreshBoard(false); await refreshLedger(); message(`已补入 ${supports.length} 份材料，费用金额不变；右侧显示更新后的实际打印页。`);
    });
    if (button.dataset.undoGroup) mutation(async () => {
      const group = board.matches.find(value => (value.group_id || value.id) === button.dataset.undoGroup);
      if (!group) throw Error('配对组已变化，请刷新材料。');
      await api('material-matching/undo', {group_id: group.group_id || group.id, expected_version: group.version || group.expense_version || group.item_version || group.item?.version});
      savedItem = null; voucherKeys.clear(); paymentIds.clear(); supportIds.clear(); printRequests.clear(); await refreshBoard(false); await refreshLedger(); message('已撤回配对，材料返回待处理；原件保留。');
    });
  });
  window.openMaterialMatching = async ({suggestion} = {}) => {
    if(dialog.open){if(suggestion&&!busy)applySuggestion(suggestion);return;}
    const serial = ++openRevision, current = () => serial === openRevision && dialog.open;
    async function read() {
      if (!current()) return;
      opening = true; projectId = currentInvoiceProjectId();
      voucherKeys.clear(); paymentIds.clear(); supportIds.clear(); savedItem = null; sourceSuggestion = null; needsSelectionConfirmation = false;
      window.resetSynthesisSelection?.();
      clearMailFeedback(); el('matchingUploadProgress').hidden = true; message('');
      dialog.querySelector('.matching-layout').hidden = true;
      const loading = el('matchingLoadState'); loading.hidden = false; loading.innerHTML = dialogLoadingMarkup('正在读取材料…');
      dialog.setAttribute('aria-busy', 'true'); lock(true);
      try {
        if (!state.token) await load();
        if (!current()) return;
        projectId = currentInvoiceProjectId();
        window.reimbursementPeopleUI?.batchChoice(el('matchingBatchPerson'), projectId);
        if (suggestion && suggestion.project_id !== projectId) throw Error('请先进入这组建议所属的项目。');
        await refreshBoard(false);
        if (!current()) return;
        if (!projectId) message('请先从侧栏选择一个项目，再打开材料合成台。');
        else if (suggestion) applySuggestion(suggestion);
        else message('在左侧选择建议组合或任意材料，右侧预览并确认后进入待核对；缺材料可以后补。');
        opening = false; loading.hidden = true; dialog.querySelector('.matching-layout').hidden = false; lock(false);
        void window.invoiceIntakeProgress?.refresh(projectId);
      } catch (error) {
        if (!current()) return;
        loading.innerHTML = dialogLoadErrorMarkup(error.message);
        loading.querySelector('[data-dialog-retry]').onclick = read;
      } finally {if (current()) dialog.setAttribute('aria-busy', 'false');}
    }
    dialog.showModal();
    await read();
  };
  const openButton = el('materialMatching');
  openButton.onclick = () => {if(!currentInvoiceProjectId()&&!new URLSearchParams(location.search).get('project')){notify('请先从侧栏选择一个项目，再打开材料合成台。',true);document.querySelector('#projectLinks a')?.focus();return;}return window.openMaterialMatching();};
  openButton.disabled = false;
  let backgroundDirty = false;
  async function showBackgroundResult() {
    if (!backgroundDirty || busy || !dialog.open || dialog.querySelector('input:focus,select:focus,textarea:focus') ||
        [...document.querySelectorAll('dialog[open]')].at(-1) !== dialog) return;
    backgroundDirty = false;
    const fields = el('matchingProjectType') ? readFields() : null;
    try {await refreshBoard(true, fields); message('后台识别完成，建议组合已刷新；请对照原件确认。');}
    catch (error) {message(error.message, true);}
  }
  document.addEventListener('intake-progress', event => {
    if (event.detail.projectId !== projectId) return;
    window.invoiceIntakeProgress.render(el('matchingBackgroundStatus'), event.detail);
    if (busy) el('matchingBackgroundStatus').querySelectorAll('button').forEach(button => button.disabled = true);
    if (event.detail.finished.length) {backgroundDirty = true; void showBackgroundResult();}
  });
  dialog.addEventListener('focusout', () => setTimeout(showBackgroundResult, 0));
  document.addEventListener('intake-progress-error', event => {
    if (!dialog.open || event.detail.projectId !== projectId) return;
    const status = el('matchingBackgroundStatus'); status.hidden = false;
    status.textContent = event.detail.message + '；可点刷新材料查看进度。';
  });
})();
