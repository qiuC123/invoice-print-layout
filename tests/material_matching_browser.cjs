// Synthetic HTTP contract against the real material matching UI. No live ledger.
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');

async function run(pw) {
  const browser = await pw.chromium.launch({headless: true});
  const page = await browser.newPage({viewport: {width: 1500, height: 1000}});
  page.setDefaultTimeout(10000);
  const origin = 'http://127.0.0.1:18775', errors = [], writes = [], received = [], prepared = [];
  const evidence = process.env.INVOICE_BROWSER_EVIDENCE ? path.resolve(process.env.INVOICE_BROWSER_EVIDENCE) :
    path.resolve(__dirname, '../workspace/invoice-operations/auto-match-existing-payment-20261012');
  const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j1ioAAAAASUVORK5CYII=', 'base64');
  const reportTypes = [
    {code: 'expansion', label: '拓展', names: ['场地费用(消费)', '施工押金(可退)'], editable_name: false},
    {code: 'transport', label: '车费', names: ['货车去程运费', '货车回程运费', '火车、飞机', '打的(出租车、滴滴等)', '公交(地铁、公交、大巴等)', '货拉拉(附行程单、报销凭证)'], editable_name: false},
    {code: 'living', label: '生活费', names: ['住宿费(附清单)', '员工统一购餐费(附清单)', '生活补助费用(附清单)', '现场饮用水、夜宵'], editable_name: false},
    {code: 'personnel', label: '人员', names: ['工厂搭建人工数', '工厂撤展人工数', '外请人员金额(附清单)', '保洁费(附清单)'], editable_name: false},
    {code: 'materials', label: '现场购材料', names: [], editable_name: true},
    {code: 'other', label: '其他', names: [], editable_name: true},
  ];
  const card = (id, role, amount, extra = {}) => {
    const result = {key: `q:${id}`, id, name: `合成 ${id}.png`, kind: role,
    role, amount_cents: amount, project_id: 'p1', revision: 1, thumbnail: `/synthetic-image/${id}.png`,
    url: `/intake-file/${id}`, issues: [], fields: {title: `合成 ${id}`, category: '其他'}, ...extra};
    result.report_choice = extra.report_choice || {project_type: extra.project_type ?? 'other', project_name: extra.project_name ?? `合成 ${id}`};
    delete result.project_type; delete result.project_name;
    return result;
  };
  const voucher = card('v1', 'invoice', 10000, {revision: 2, name: '合成发票.pdf'});
  const p70 = card('p70', 'payment', 7000, {revision: 3});
  const p30 = card('p30', 'payment', 3000, {revision: 4});
  const existing = {key: 'e:existing', id: 'existing', item_id: 'existing', name: '已有费用补付款', kind: 'expense',
    role: 'invoice', amount_cents: 5000, project_id: 'p1', report_choice: {project_type: 'other', project_name: '已有费用补付款'}, version: 'ev1', thumbnail: '/synthetic-image/existing.png', url: '/file/existing-invoice', issues: ['缺付款截图']};
  const unknown = card('unknown', 'other', null, {name: '普通未知材料.png', revision: 5, issues: ['金额不明']});
  const pendingQr = card('qr-initial', 'other', 9400, {kind: 'other', qr_status: 'manual', name: '待领票二维码.png', issues: ['待领票']});
  const nullInvoice = card('null-invoice', 'invoice', null, {name: '金额未识别的发票.pdf', issues: ['金额不明']});
  const supportDetail = card('support-detail', 'detail', null, {name: '未知金额运单.pdf', issues: ['金额不明']}),
    supportTrip = card('support-trip', 'trip_sheet', 50000, {name: '行程明细.pdf'}),
    supportExtra = card('support-extra', 'other', null, {name: '追加材料.png'}),
    supportOrder = card('support-order', 'order', null, {name: '订单明细.png'}),
    pendingPurchase = card('pending-purchase', 'purchase', 12300, {name: '购买凭证待确认.png'});
  const v60 = card('v60', 'invoice', 6000), v40 = card('v40', 'receipt', 4000);
  const manyLeft = {...existing, key: 'e:many-left', id: 'many-left', item_id: 'many-left', name: '已有费用甲', amount_cents: 2000, version: 'lv1'};
  const manyRight = {...existing, key: 'e:many-right', id: 'many-right', item_id: 'many-right', name: '已有费用乙', amount_cents: 3000, version: 'rv1'};
  const board = {vouchers: [voucher, existing, nullInvoice, v60, v40, card('v35', 'invoice', 3500), card('v45', 'invoice', 4500), manyLeft, manyRight],
    payments: [p70, p30, card('p40', 'payment', 4000), card('pm', 'payment', 5000), card('pn', 'payment', 1200),
      card('p100', 'payment', 10000), card('p80-30', 'payment', 3000), card('p80-50', 'payment', 5000), card('p-existing', 'payment', 5000), card('p-unknown', 'payment', null)],
    other: [unknown, pendingQr, supportDetail, supportTrip, supportExtra, supportOrder, pendingPurchase], matches: [], suggestions: [], report_types: reportTypes, supplement_candidates: []};
  const expense = (id, amount, version, fields = {}) => ({id, title: `合成费用 ${id}`, amount_cents: amount,
    amount: (amount / 100).toFixed(2), version, project_id: 'p1', project: '合成甲项目', stage: 'draft',
    complete: true, verified: false, ready: false, review_requested: true, category: '其他', missing: [],
    attachments: [], events: [], ...fields});
  const state = {app_mode: 'invoice', logistics_available: false, token: 'synthetic-token',
    projects: [{id: 'p1', name: '合成甲项目'}, {id: 'p2', name: '合成乙项目'}],
    items: [expense('existing', 5000, 'ev1', {complete: false, missing: ['付款截图']}),
      expense('many-left', 2000, 'lv1', {complete: false, missing: ['付款截图']}), expense('many-right', 3000, 'rv1', {complete: false, missing: ['付款截图']})], inbox: [],
    categories: ['材料采购', '餐饮', '酒店', '其他'], roles: {invoice: '发票', receipt: '收据', payment: '付款截图', other: '其他'},
    stages: {draft: '待核对', submitted: '已提交', reimbursed: '已报销'}, category_auto_ready: false,
    report_template_ready: true, report_person: '合成报销人'};
  let releaseFirstMatch, releaseMatchingScript;
  const firstMatchGate = new Promise(resolve => {releaseFirstMatch = resolve;});
  const matchingScriptGate = new Promise(resolve => {releaseMatchingScript = resolve;});
  let matchCount = 0, manyMatchCount = 0, failExistingVersion = true, autoCount = 0, autoLimit = '', supplementCount = 0, failSupplementVersion = true, failCandidateVersion = true;
  let boardReads = 0, stateReads = 0;
  const autoIssue = '自动检索每组最多4份票据和4份付款；更大组合请人工选择。商家明确对应优先；名称不同或存在竞争的组合仅展示建议，确认前不创建费用。';
  const merchantConflict = '相同商家与合计存在多个对应组合，等待人工选择';
  const ambiguousReason = '同一合计存在多个票据或付款组合，等待人工选择';
  const poolLimitReason = '材料池超过20份票据或32份付款，请分批人工匹配；未截断后自动猜测';
  const candidateLimitReason = '候选组合超过50000组，无法可靠判断唯一，请分批人工匹配';
  const remove = (list, predicate) => {for (let index = list.length - 1; index >= 0; index--) if (predicate(list[index])) list.splice(index, 1);};
  const addGroup = (id, item, reason, members) => board.matches.push({group_id: id, item_id: item.id,
    title: item.title, expense_version: item.version, reason, provisional: true, can_supplement: !item.verified && item.stage === 'draft',
    amount_cents: item.amount_cents, amount_pending: !!item.amount_pending, mode: reason.includes('人工确认创建') ? 'registration' : 'alignment',
    voucher_count: members.vouchers ? members.vouchers.length : members.voucher ? 1 : 0, voucher_total_cents: item.amount_cents, payment_count: members.payments.length,
    support_count: members.supports?.length || 0, members});
  const suggestion = (id, vouchers, payments, fields = {}) => ({id, project_id: 'p1',
    voucher_keys: vouchers.map(value => value.key), payment_ids: payments.map(value => value.id),
    amount_cents: vouchers.reduce((sum, value) => sum + value.amount_cents, 0),
    snapshot_token: `snapshot-${id}`, ambiguous: false, needs_name_review: false,
    reason: '商家明确对应且金额一致，等待确认合成', warnings: [], ...fields});
  const suggest = (suggestions, conflicts = []) => {
    board.suggestions = suggestions;
    return {engine: 'rules', matched: 0, suggested: suggestions.length, suggestions,
      ai: {requests: 0}, issues: [autoIssue], conflicts};
  };
  const businessBoard = () => JSON.stringify({vouchers: board.vouchers, payments: board.payments,
    other: board.other, matches: board.matches, supplement_candidates: board.supplement_candidates});
  page.on('pageerror', error => errors.push(error.message));
  await page.route(`${origin}/**`, async route => {
    const request = route.request(), url = new URL(request.url());
    const json = (body, status = 200) => route.fulfill({status, contentType: 'application/json', body: JSON.stringify(body)});
    if (request.method() === 'POST') {
      const body = request.postDataJSON(); writes.push({path: url.pathname, body});
      if (url.pathname === '/api/material-matching/create') {
        matchCount++;
        assert.equal(body.project_id, 'p1');
        assert.deepEqual(Object.keys(body.fields).sort(), ['amount', 'project_name', 'project_type']);
        assert.ok(Object.values(body.fields).every(value => typeof value === 'string'));
        const reportType = reportTypes.find(value => value.code === body.fields.project_type);
        assert.ok(reportType && body.fields.project_name && /^\d+(?:\.\d{1,2})?$/.test(body.fields.amount), 'all three creation fields must be confirmed');
        assert.ok(reportType.editable_name || reportType.names.includes(body.fields.project_name), 'closed report types only accept their own detail names');
        assert.ok(Array.isArray(body.voucher_keys), 'every manual grouping must send the complete voucher array');
        const selectedVouchers = body.voucher_keys.map(key => board.vouchers.find(value => value.key === key));
        assert.ok(selectedVouchers.every(Boolean));
        const selectedVoucher = selectedVouchers[0];
        const payments = body.payment_ids.map(id => board.payments.find(value => value.id === id));
        assert.ok(Array.isArray(body.support_ids), 'manual groups must explicitly send supplementary material IDs');
        const supports = body.support_ids.map(id => board.other.find(value => value.id === id));
        assert.ok(supports.every(value => value && value.qr_status !== 'manual' && ['other', 'order', 'trip', 'detail', 'trip_sheet', 'purchase'].includes(value.role)));
        assert.ok(selectedVouchers.length + payments.length + supports.length > 0);
        for (const support of supports) assert.equal(body.revisions[support.id], support.revision);
        assert.ok(payments.every(Boolean));
        for (const payment of payments) assert.equal(body.revisions[payment.id], payment.revision);
        for (const selected of selectedVouchers) {
          if (selected.key.startsWith('q:')) assert.equal(body.revisions[selected.id], selected.revision);
          else assert.equal(body.expected_versions[selected.item_id], selected.version);
        }
        if (selectedVouchers.length > 1) {
          manyMatchCount++;
          assert.equal(body.voucher_key, undefined, 'multiple vouchers must never be silently sent as only the first voucher');
          assert.equal(body.expected_version, undefined);
          const total = body.fields.amount ? Math.round(Number(body.fields.amount) * 100) : selectedVouchers.reduce((sum, value) => sum + (value.amount_cents || 0), 0);
          const item = expense(`many-${manyMatchCount}`, total, `many-v${manyMatchCount}`, {title: body.fields.project_name, ...body.fields}); state.items.push(item);
          remove(board.vouchers, value => body.voucher_keys.includes(value.key)); remove(board.payments, value => body.payment_ids.includes(value.id));
          remove(board.other, value => body.support_ids.includes(value.id));
          addGroup(`many-group-${manyMatchCount}`, item, '人工确认创建，待核对', {vouchers: selectedVouchers, payments, supports});
          return json({item, match: board.matches.at(-1)});
        }
        assert.equal(body.voucher_key, undefined);
        if (selectedVoucher?.key === 'q:v1') {
          await firstMatchGate;
          const item = expense('manual-new', 10000, 'mv1'); state.items.push(item);
          remove(board.vouchers, value => value.key === 'q:v1'); remove(board.payments, value => body.payment_ids.includes(value.id));
          remove(board.other, value => body.support_ids.includes(value.id));
          addGroup('g1', item, '人工确认创建，待核对', {voucher: selectedVoucher, payments, supports}); return json({item, match: board.matches.at(-1)});
        }
        if (selectedVoucher?.key === 'e:existing' && failExistingVersion) {
          failExistingVersion = false; existing.version = 'ev2'; state.items[0].version = 'ev2';
          return json({error: '费用版本已变化，请刷新'}, 409);
        }
        const basis = selectedVouchers.length ? selectedVouchers : payments.length ? payments : supports.length === 1 ? supports : [];
        const knownTotal = cards => cards.length && cards.every(value => Number.isSafeInteger(value.amount_cents)) ? cards.reduce((sum, value) => sum + value.amount_cents, 0) : null;
        const inferred = knownTotal(selectedVouchers) ?? knownTotal(payments) ?? (!selectedVouchers.length && !payments.length && supports.length === 1 ? knownTotal(supports) : null);
        const total = body.fields.amount ? Math.round(Number(body.fields.amount) * 100) : inferred;
        const missing = selectedVoucher?.key === 'e:existing' ? ['行程单'] : !selectedVouchers.length ? ['发票／收据'] : !payments.length ? ['付款截图'] : [];
        const id = selectedVoucher?.key === 'e:existing' ? 'existing' : `manual-only-${matchCount}`;
        const item = expense(id, total ?? 0, id === 'existing' ? 'ev3' : `only-v${matchCount}`, {complete: !missing.length, missing,
          title: body.fields.project_name, project_type: body.fields.project_type, project_name: body.fields.project_name,
          amount_pending: !body.fields.amount && inferred === null});
        if (id === 'existing') state.items[0] = item; else state.items.push(item);
        remove(board.vouchers, value => body.voucher_keys.includes(value.key));
        remove(board.payments, value => body.payment_ids.includes(value.id));
        remove(board.other, value => body.support_ids.includes(value.id));
        addGroup(id === 'existing' ? 'g2' : `only-group-${matchCount}`, item, '人工确认创建，待核对', {vouchers: selectedVouchers, payments, supports}); return json({item, match: board.matches.at(-1)});
      }
      if (url.pathname === '/api/material-matching/supplement') {
        supplementCount++;
        const group = board.matches.find(value => value.group_id === body.group_id); assert.ok(group?.can_supplement);
        const item = state.items.find(value => value.id === group.item_id); assert.ok(item);
        assert.equal(body.expected_version, group.expense_version);
        assert.ok(body.support_ids.length);
        const supports = body.support_ids.map(id => board.other.find(value => value.id === id));
        assert.ok(supports.every(value => value && value.qr_status !== 'manual' && ['other', 'order', 'trip', 'detail', 'trip_sheet'].includes(value.role)));
        for (const support of supports) assert.equal(body.revisions[support.id], support.revision);
        if (failSupplementVersion) {
          failSupplementVersion = false; item.version = 'mv2'; group.expense_version = item.version;
          return json({error: '费用版本已变化，请刷新'}, 409);
        }
        if (body.support_ids.includes('support-candidate') && failCandidateVersion) {
          failCandidateVersion = false; item.version = 'mv4'; group.expense_version = item.version;
          board.supplement_candidates[0].expected_version = item.version;
          return json({error: '费用版本已变化，请刷新'}, 409);
        }
        item.version = `mv${supplementCount + 1}`; group.expense_version = item.version;
        group.members.supports.push(...supports); group.support_count = group.members.supports.length;
        remove(board.other, value => body.support_ids.includes(value.id));
        return json({item, match: group});
      }
      if (url.pathname === '/api/material-matching/undo') {
        const group = board.matches.find(value => value.group_id === body.group_id); assert.ok(group);
        assert.equal(body.expected_version, group.expense_version);
        board.vouchers.push(...(group.members.vouchers || (group.members.voucher ? [group.members.voucher] : []))); board.payments.push(...group.members.payments);
        board.other.push(...(group.members.supports || []));
        remove(board.matches, value => value === group); remove(state.items, value => value.id === group.item_id);
        return json({undone: true});
      }
      if (url.pathname === '/api/expense-print-preview') {
        const item = state.items.find(value => value.id === body.id);
        assert.equal(typeof body.expected_version, 'string', 'preview must carry the displayed expense version');
        if (!item) return json({error: '费用已不存在，请刷新'}, 404);
        if (body.expected_version !== item.version) return json({error: '费用版本已变化，请刷新'}, 409);
        return json({id: item.id, version: item.version, complete: item.complete, missing: item.missing, pdf: `${item.id}.pdf`,
          preview_pages: [`${item.id}.png`], layout: 'A4', page_count: 1});
      }
      if (url.pathname === '/api/intake/classify') {
        const all = [...board.other, ...board.vouchers, ...board.payments], target = all.find(value => value.id === body.id); assert.ok(target);
        assert.equal(body.revision, target.revision); assert.equal(body.fields.project_id, 'p1');
        assert.equal(Object.hasOwn(body.fields, 'category'), false, 'material correction must not require a reimbursement category');
        assert.notEqual(target.qr_status, 'manual');
        remove(board.other, value => value === target); remove(board.vouchers, value => value === target); remove(board.payments, value => value === target);
        Object.assign(target, {role: body.role, kind: body.role, amount_cents: Math.round(Number(body.fields.amount) * 100), revision: target.revision + 1, fields: body.fields, issues: []});
        (target.role === 'payment' ? board.payments : ['invoice', 'receipt'].includes(target.role) ? board.vouchers : board.other).push(target);
        return json({entry: target});
      }
      if (url.pathname === '/api/intake/receive') {
        const id = `uploaded-${received.length + 1}`; received.push({id, ...body});
        assert.ok(body.data); assert.equal(body.project_id, 'p1'); return json({id, name: body.name, status: 'received'});
      }
      if (url.pathname === '/api/intake/prepare') {
        prepared.push(body.id);
        const original = received.find(value => value.id === body.id);
        if (!original) return json({entry: pendingQr, converted_to_pdf: false, message: '仍待领票，原件保留'});
        if (original.name === 'invoice.pdf') board.vouchers.push(card(original.id, 'invoice', 2000, {name: original.name}));
        if (original.name === 'payment.png') board.payments.push(card(original.id, 'payment', 2000, {name: original.name}));
        if (original.name === 'qr-ready.png') board.vouchers.push(card(original.id, 'invoice', 2500, {name: '已领取发票.pdf', qr_status: 'claimed'}));
        if (original.name === 'qr-pending.png') board.other.push(card(original.id, 'other', 6500, {kind: 'other', name: original.name, qr_status: 'manual', issues: ['待领票']}));
        if (original.name === 'failed.png') {board.other.push(card(original.id, 'other', null, {name: original.name, issues: ['识别失败']})); return json({error: '本地识别暂时失败'}, 422);}
        return json({entry: {id: original.id}, converted_to_pdf: original.name === 'qr-ready.png'});
      }
      if (url.pathname === '/api/material-matching/auto') {
        autoCount++; assert.equal(body.project_id, 'p1');
        assert.deepEqual(Object.keys(body), ['project_id'], 'automatic matching operates on the current saved project pool');
        if (autoLimit === 'existing-merchant') {
          const invoice = board.vouchers.find(value => value.id === 'merchant-invoice50');
          const payment = board.payments.find(value => value.id === 'old-merchant-payment50');
          assert.ok(invoice && payment); assert.equal(invoice.amount_cents, 5000); assert.equal(payment.amount_cents, 5000);
          assert.equal(invoice.fields.merchant, payment.fields.merchant);
          assert.ok(board.vouchers.some(value => value.id === 'railway25-a') && board.vouchers.some(value => value.id === 'railway25-b'));
          return json(suggest([suggestion('merchant-existing-candidate', [invoice], [payment])]));
        }
        if (autoLimit === 'merchant-conflict') {
          const payment = board.payments.find(value => value.id === 'merchant-ambiguous-pay');
          const suggestions = ['merchant-ambiguous-a', 'merchant-ambiguous-b'].map(id => suggestion(`competing-${id}`,
            [board.vouchers.find(value => value.id === id)], [payment], {ambiguous: true, reason: merchantConflict,
              warnings: ['同一付款存在多个候选，请检查后选择其中一组']}));
          return json(suggest(suggestions, [
            {amount_cents: 5000, reason: merchantConflict, voucher_keys: ['q:internal-auto-voucher'], payment_ids: ['internal-auto-payment']},
            {amount_cents: 5000, reason: merchantConflict}]));
        }
        if (autoLimit === 'merchant-version-conflict') return json({error: '商家配对材料版本已变化，请刷新后人工选择'}, 409);
        if (autoLimit) return json(suggest([], [{reason: autoLimit === 'pool' ? poolLimitReason : candidateLimitReason,
          voucher_keys: ['q:internal-auto-voucher'], payment_ids: ['internal-auto-payment']}]));
        const automaticVoucher = board.vouchers.find(value => value.name === 'invoice.pdf'), automaticPayment = board.payments.find(value => value.name === 'payment.png');
        if (!automaticVoucher || !automaticPayment) return json(suggest([]));
        assert.equal(automaticVoucher.amount_cents, automaticPayment.amount_cents, 'automatic matching remains strict even though manual creation is flexible');
        return json(suggest([suggestion('uploaded-candidate', [automaticVoucher], [automaticPayment])],
          [{reason: ambiguousReason, voucher_keys: ['q:internal-auto-voucher']}, {reason: ambiguousReason}]));
      }
      throw Error(`Unexpected ledger operation: ${url.pathname}`);
    }
    if (url.pathname === '/api/state') {stateReads++; return json(state);}
    if (url.pathname === '/api/material-matching') {
      boardReads++;
      const selectedProject = url.searchParams.get('project');
      return json(selectedProject === 'p2' ? {vouchers: [], payments: [], other: [], matches: [], report_types: reportTypes} : board);
    }
    if (url.pathname === '/api/intake') return json({queue: []});
    if (url.pathname === '/api/preferences') return json({rules: [], learned: [], sections: {}});
    if (url.pathname.startsWith('/synthetic-image/') || url.pathname.startsWith('/intake-file/') || url.pathname.startsWith('/file/') || url.pathname.startsWith('/export/')) return route.fulfill({contentType: 'image/png', body: png});
    const name = url.pathname === '/' ? 'index.html' : url.pathname.slice(1);
    if (!/^[a-z-]+\.(html|js|css)$/.test(name)) return route.abort();
    if (name === 'matching-desk.js') await matchingScriptGate;
    return route.fulfill({contentType: name.endsWith('.js') ? 'text/javascript' : name.endsWith('.css') ? 'text/css' : 'text/html',
      body: await fs.readFile(path.resolve(__dirname, '../src/invoice_print_layout/web', name))});
  });
  const selectVoucher = key => page.locator(`[data-voucher-key="${key}"]`).click();
  const selectPayment = async id => {const control = page.locator(`[data-payment-id="${id}"]`); if (await control.getAttribute('aria-pressed') !== 'true') await control.click();};
  const selectSupport = id => page.locator(`[data-support-id="${id}"]`).click();
  const classify = async (id, role, amount) => {
    await page.locator(`[data-classify-id="${id}"]`).click();
    const editor = page.locator('.matching-classify[open]');
    assert.equal(await editor.locator('[name="category"]').count(), 0);
    assert.equal(await editor.locator('[name="title"]').getAttribute('required'), null);
    await editor.locator('[name="title"]').fill('');
    await editor.locator('[name="role"]').selectOption(role); await editor.locator('[name="amount"]').fill(amount);
    await editor.getByRole('button', {name: '保存材料分类'}).click(); await editor.waitFor({state: 'detached'});
  };
  const createdInReview = async id => {
    await page.locator('#matchingDeskDialog[open]').waitFor({state: 'detached'});
    await page.waitForFunction(expected => document.querySelector('#expensePrintPdf')?.getAttribute('href')?.includes(expected), `${id}.pdf`);
    await page.waitForFunction(() => document.querySelector('#expensePrintPages img')?.naturalWidth > 0);
    assert.equal(await page.evaluate(() => view), 'review');
    assert.equal(await page.evaluate(() => active), id, 'Created expenses open their real print detail in review');
    assert.deepEqual(await page.evaluate(() => [...checked]), [], 'Creation cannot carry expense selections across stages');
    await page.locator('#materialMatching').click();
    await page.waitForFunction(() => document.getElementById('matchingDeskDialog').open && document.getElementById('matchingDeskDialog').getAttribute('aria-busy') === 'false');
  };
  try {
    await fs.mkdir(evidence, {recursive: true});
    await page.goto(origin+'/?project=p1', {waitUntil: 'commit'}); assert.equal(await page.locator('#projectFilter').count(),0);
    const openMatchingButton = page.locator('#materialMatching');
    assert.equal(await openMatchingButton.isDisabled(), true, 'material desk entry must stay disabled while its script is still loading');
    assert.equal(await page.evaluate(() => typeof window.openMaterialMatching), 'undefined');
    await openMatchingButton.evaluate(button => button.click());
    assert.equal(await page.locator('#matchingDeskDialog').count(), 0, 'early click must not open an uninitialized desk');
    assert.deepEqual(errors, [], 'early click must not raise an undefined-function error');
    releaseMatchingScript();
    await page.waitForFunction(() => typeof window.openMaterialMatching === 'function' && !document.getElementById('materialMatching').disabled);
    // This regression exercises explicit manual preparation and composition.
    // The default automatic path has its own upload/Agent integration coverage.
    await page.waitForFunction(() => !!document.querySelector('#manualAgent input[type="checkbox"]'));
    await page.locator('#manualAgent summary').click();
    await page.getByLabel('上传后自动整理', {exact: true}).uncheck();
    assert.equal(await page.getByRole('button', {name: '确认当前费用金额／分笔付款', exact: true}).count(), 0, 'old split-payment button must be removed');
    assert.equal(await page.locator('#newItem').count(), 0, 'the old separate manual-entry button should not duplicate this creation flow');
    await page.locator('#materialMatching').click(); await page.locator('#matchingDeskDialog[open]').waitFor();
    assert.equal(await page.locator('#matchingProject').count(),0);
    assert.equal(await page.evaluate(()=>currentInvoiceProjectId()),'p1');
    assert.equal(await page.getByRole('button', {name: '按金额自动配对', exact: true}).count(), 0);
    assert.equal(await page.getByRole('button', {name: '寻找合成组合', exact: true}).count(), 1);
    await page.locator('[data-voucher-key="q:v1"]').waitFor();
    assert.equal(await page.locator('#matchingSave').isDisabled(), true);
    await selectVoucher('q:v1'); await selectPayment('p70');
    assert.equal(await page.locator('#matchingSave').isDisabled(), false, 'manual creation may confirm unequal recognized totals');
    assert.equal(await page.locator('#matchingProjectName').inputValue(), '合成 v1');
    assert.equal(await page.locator('#matchingAmount').inputValue(), '100.00');
    assert.equal(await page.locator('#matchingProjectType').inputValue(), 'other');
    assert.equal(await page.locator('#matchingTitle,#matchingCategory').count(), 0);
    assert.equal(await page.locator('.matching-create-fields [required]').count(), 3);
    assert.match(await page.locator('#matchingPaymentTotal').innerText(), /70\.00/);
    await selectPayment('p30'); assert.equal(await page.locator('#matchingSave').isDisabled(), false);
    assert.match(await page.locator('#matchingPaymentTotal').innerText(), /100\.00/);
    assert.equal(await page.locator('.matching-payment-proofs figure').count(), 2);
    assert.equal(await page.locator('[data-support-id="qr-initial"]').count(), 0, 'unclaimed QR must not be selectable as supplementary proof');
    assert.equal(await page.locator('[data-support-id="pending-purchase"]').count(), 1, 'ordinary purchase evidence may participate in manual creation');
    assert.equal(await page.locator('[data-classify-id="pending-purchase"]').count(), 1, 'unconfirmed purchase evidence should retain its classification action');
    assert.equal(await page.locator('[data-support-id="support-order"]').count(), 1, 'ordinary order details may be selected without an amount');
    await selectSupport('support-detail'); await selectSupport('support-trip');
    assert.equal(await page.locator('#matchingSelectedSupportCount').innerText(), '2');
    assert.equal(await page.locator('.matching-support-proofs figure').count(), 2);
    assert.match(await page.locator('#matchingPreviewContent').innerText(), /不叠加金额/);
    assert.match(await page.locator('#matchingVoucherTotal').innerText(), /100\.00/);
    assert.match(await page.locator('#matchingPaymentTotal').innerText(), /100\.00/);
    assert.equal(await page.locator('#matchingSave').isDisabled(), false, 'unknown supplementary amount must not block a balanced group');
    for (const value of ['-1', '1.234', '1e2']) {
      await page.locator('#matchingAmount').fill(value); assert.equal(await page.locator('#matchingSave').isDisabled(), true);
    }
    await page.locator('#matchingAmount').fill(''); assert.equal(await page.locator('#matchingSave').isDisabled(), true);
    await page.locator('#matchingAmount').fill('100.00');
    await selectSupport('support-trip'); assert.equal(await page.locator('#matchingSelectedSupportCount').innerText(), '1');
    await selectSupport('support-trip'); supportTrip.revision++;
    await page.locator('#matchingRefresh').click(); await page.waitForFunction(() => !document.getElementById('matchingRefresh').disabled);
    assert.equal(await page.locator('[data-support-id="support-detail"]').getAttribute('aria-pressed'), 'true');
    assert.equal(await page.locator('[data-support-id="support-trip"]').getAttribute('aria-pressed'), 'false', 'refresh should drop only the changed supplementary card');
    assert.equal(await page.locator('[data-voucher-key="q:v1"]').getAttribute('aria-pressed'), 'true');
    assert.equal(await page.locator('[data-payment-id="p70"]').getAttribute('aria-pressed'), 'true');
    await selectSupport('support-trip');
    await page.locator('#matchingSave').click();
    await page.waitForFunction(() => document.getElementById('matchingSave').disabled);
    assert.equal(matchCount, 1); await page.locator('#matchingSave').evaluate(button => button.click()); assert.equal(matchCount, 1);
    releaseFirstMatch();
    await createdInReview('manual-new');
    assert.equal(state.items.find(value => value.id === 'manual-new').verified, false);
    assert.equal(state.items.find(value => value.id === 'manual-new').stage, 'draft');
    assert.ok((await page.locator('#expensePrintPdf').getAttribute('href')).includes('manual-new.pdf'));
    const supportedMatchRequest = writes.find(value => value.path === '/api/material-matching/create').body;
    assert.deepEqual(supportedMatchRequest.support_ids, ['support-detail', 'support-trip']);
    assert.equal(supportedMatchRequest.revisions['support-trip'], 2);
    assert.equal(state.items.find(value => value.id === 'manual-new').amount_cents, 10000, 'supplementary originals are not another expense');
    assert.match(await page.locator('#matchingGroups').textContent(), /2 份补充附件/);
    const firstGroupText = await page.locator('#matchingGroups').textContent();
    assert.ok(!firstGroupText.includes('g1') && !firstGroupText.includes('mv1'), 'group ID and expense version must remain request data rather than visible content');
    await page.locator('.matching-groups summary').click();
    assert.equal(await page.locator('[data-supplement-group="g1"]').isDisabled(), true, 'supplement requires an explicit ordinary material selection');
    await selectSupport('support-extra');
    assert.equal(await page.locator('#matchingSave').isDisabled(), true, 'unknown amount must be entered before creation, while supplementing remains available');
    assert.equal(await page.locator('[data-supplement-group="g1"]').isDisabled(), false);
    await selectSupport('pending-purchase');
    assert.equal(await page.locator('[data-supplement-group="g1"]').isDisabled(), true, 'purchase is allowed for creation but retains the existing supplement role restriction');
    assert.match(await page.locator('[data-supplement-group="g1"]').getAttribute('title'), /确认材料用途/);
    await selectSupport('pending-purchase');
    const firstGroup = board.matches.find(value => value.group_id === 'g1');
    firstGroup.can_supplement = false; state.items.find(value => value.id === 'manual-new').verified = true;
    await page.locator('#matchingRefresh').click(); await page.waitForFunction(() => !document.getElementById('matchingRefresh').disabled);
    assert.equal(await page.locator('[data-supplement-group="g1"]').isDisabled(), true, 'verified groups must not allow supplementary mutations');
    firstGroup.can_supplement = true; state.items.find(value => value.id === 'manual-new').verified = false;
    await page.locator('#matchingRefresh').click(); await page.waitForFunction(() => !document.getElementById('matchingRefresh').disabled);
    await page.locator('[data-supplement-group="g1"]').click();
    await page.waitForFunction(() => document.getElementById('matchingMessage').textContent.includes('版本已变化'));
    await page.waitForFunction(() => !document.querySelector('[data-supplement-group="g1"]').disabled);
    assert.equal(supplementCount, 1, 'version conflict must not silently retry supplementary attachment');
    assert.equal(await page.locator('[data-support-id="support-extra"]').getAttribute('aria-pressed'), 'true');
    await page.locator('[data-supplement-group="g1"]').click();
    await page.waitForFunction(() => document.getElementById('matchingMessage').textContent.includes('已补入 1 份材料'));
    await page.waitForFunction(() => document.querySelector('#matchingPrintPages img')?.naturalWidth > 0);
    assert.equal(supplementCount, 2);
    assert.equal(state.items.find(value => value.id === 'manual-new').amount_cents, 10000);
    assert.match(await page.locator('#matchingGroups').innerText(), /3 份补充附件/);
    assert.ok(writes.some(value => value.path === '/api/expense-print-preview' && value.body.id === 'manual-new' && value.body.expected_version === 'mv3'), 'supplemented group must request the updated actual print preview');
    const candidateCard = card('support-candidate', 'detail', null, {name: '同票号运单.pdf'});
    board.other.push(candidateCard);
    const candidate = {key: candidateCard.key, support_id: candidateCard.id, support_ids: [candidateCard.id], revision: 1,
      revisions: {[candidateCard.id]: 1}, group_id: 'g1', item_id: 'manual-new', title: '合成顺丰费用',
      expected_version: 'mv3', expense_version: 'mv3', can_supplement: true, reason: '发票号码一致；补充材料不计金额'};
    board.supplement_candidates = [candidate, {...candidate, key: pendingQr.key, support_id: pendingQr.id, revision: pendingQr.revision}];
    await page.locator('#matchingRefresh').click(); await page.waitForFunction(() => !document.getElementById('matchingRefresh').disabled);
    assert.match(await page.locator('#matchingOther').innerText(), /对应已有费用：合成顺丰费用/);
    assert.equal(await page.locator('[data-supplement-candidate="qr-initial"]').count(), 0, 'QR must never become a supplementary candidate');
    board.supplement_candidates.push({...candidate, group_id: 'ambiguous-group'});
    await page.locator('#matchingRefresh').click(); await page.waitForFunction(() => !document.getElementById('matchingRefresh').disabled);
    assert.equal(await page.locator('[data-supplement-candidate="support-candidate"]').count(), 0, 'ambiguous candidates must not offer direct attachment');
    board.supplement_candidates.pop();
    await page.locator('#matchingRefresh').click(); await page.waitForFunction(() => !document.getElementById('matchingRefresh').disabled);
    const createsBeforeCandidate = matchCount;
    await page.locator('[data-supplement-candidate="support-candidate"]').click();
    await page.waitForFunction(() => document.getElementById('matchingMessage').textContent.includes('版本已变化'));
    await page.waitForFunction(() => !document.querySelector('[data-supplement-candidate="support-candidate"]').disabled);
    assert.equal(supplementCount, 3, 'direct candidate conflicts must wait for explicit retry');
    assert.equal(matchCount, createsBeforeCandidate);
    await page.locator('[data-supplement-candidate="support-candidate"]').click();
    await page.waitForFunction(() => document.getElementById('matchingMessage').textContent.includes('材料已补入对应已有费用'));
    await page.waitForFunction(() => document.querySelector('#matchingPrintPages img')?.naturalWidth > 0);
    assert.equal(supplementCount, 4); assert.equal(matchCount, createsBeforeCandidate, 'attaching an identified supplement must not create another expense');
    assert.equal(state.items.find(value => value.id === 'manual-new').amount_cents, 10000);
    assert.match(await page.locator('#matchingGroups').innerText(), /4 份补充附件/);
    assert.equal(await page.locator('[data-supplement-candidate="support-candidate"]').count(), 0);
    await page.locator('[data-undo-group="g1"]').click();
    await page.locator('[data-voucher-key="q:v1"]').waitFor(); assert.equal(await page.locator('#matchingPrintPages img').count(), 0);
    assert.equal(board.payments.filter(value => ['p70', 'p30'].includes(value.id)).length, 2);
    assert.equal(board.other.filter(value => ['support-detail', 'support-trip', 'support-extra'].includes(value.id)).length, 3, 'undo returns supplementary originals to the pool');

    // Two vouchers may be paid together in one transfer; neither voucher may disappear from the payload.
    await selectVoucher('q:v60'); await selectVoucher('q:v40'); await selectPayment('p100');
    assert.equal(await page.locator('#matchingSelectedVoucherCount').innerText(), '2');
    assert.equal(await page.locator('#matchingSelectedPaymentCount').innerText(), '1');
    assert.equal(await page.locator('.matching-voucher-proofs figure').count(), 2);
    assert.match(await page.locator('#matchingVoucherTotal').innerText(), /100\.00/);
    assert.match(await page.locator('#matchingDifference').innerText(), /0\.00/);
    assert.equal(await page.locator('#matchingSave').isDisabled(), false);
    await selectVoucher('q:null-invoice');
    assert.equal(await page.locator('#matchingSave').isDisabled(), false, 'unknown recognized invoice amount may be confirmed manually');
    assert.equal(await page.locator('#matchingVoucherTotal').innerText(), '金额待确认');
    assert.equal(await page.locator('.matching-voucher-proofs figure').count(), 3);
    await selectVoucher('q:null-invoice');
    await selectPayment('p-unknown');
    assert.equal(await page.locator('#matchingSave').isDisabled(), false, 'unknown recognized payment amount does not prevent manual creation');
    assert.equal(await page.locator('#matchingPaymentTotal').innerText(), '金额待确认');
    await page.locator('[data-payment-id="p-unknown"]').click();
    assert.equal(await page.locator('#matchingSave').isDisabled(), false);
    await selectVoucher('q:v40');
    assert.equal(await page.locator('[data-voucher-key="q:v40"]').getAttribute('aria-pressed'), 'false');
    assert.equal(await page.locator('.matching-voucher-proofs figure').count(), 1);
    assert.equal(await page.locator('#matchingSave').isDisabled(), false);
    assert.match(await page.locator('#matchingDifference').innerText(), /-40\.00/);
    await selectVoucher('q:v40'); await selectPayment('p40');
    assert.equal(await page.locator('#matchingSave').isDisabled(), false);
    assert.equal(matchCount, 1, 'changing a selection must not automatically create a group');
    await page.locator('[data-payment-id="p40"]').click();
    // One card changed in the background. Refresh should keep every other valid selection.
    v40.revision++;
    await page.locator('#matchingRefresh').click();
    await page.waitForFunction(() => !document.getElementById('matchingRefresh').disabled);
    assert.equal(await page.locator('[data-voucher-key="q:v60"]').getAttribute('aria-pressed'), 'true');
    assert.equal(await page.locator('[data-voucher-key="q:v40"]').getAttribute('aria-pressed'), 'false');
    assert.equal(await page.locator('[data-payment-id="p100"]').getAttribute('aria-pressed'), 'true');
    await selectVoucher('q:v40'); await page.locator('#matchingSave').click();
    await createdInReview('many-1');
    assert.equal(state.items.find(value => value.id === 'many-1').amount_cents, 10000);
    assert.match(await page.locator('#matchingGroups').innerText(), /2 张票据 · 1 张付款/);
    const firstManyRequest = writes.filter(value => value.path === '/api/material-matching/create')[1].body;
    assert.deepEqual(firstManyRequest.voucher_keys, ['q:v60', 'q:v40']);
    assert.deepEqual(firstManyRequest.payment_ids, ['p100']);
    assert.equal(firstManyRequest.revisions.v40, 2);

    // Two vouchers with two split payments are still one accounting group.
    await selectVoucher('q:v35'); await selectVoucher('q:v45'); await selectPayment('p80-30');
    assert.equal(await page.locator('#matchingSave').isDisabled(), false);
    await selectPayment('p80-50');
    assert.equal(await page.locator('#matchingSave').isDisabled(), false);
    assert.equal(await page.locator('.matching-voucher-proofs figure').count(), 2);
    assert.equal(await page.locator('.matching-payment-proofs figure').count(), 2);
    board.payments.find(value => value.id === 'p80-30').revision++;
    await page.locator('#matchingRefresh').click(); await page.waitForFunction(() => !document.getElementById('matchingRefresh').disabled);
    assert.equal(await page.locator('[data-voucher-key="q:v35"]').getAttribute('aria-pressed'), 'true');
    assert.equal(await page.locator('[data-voucher-key="q:v45"]').getAttribute('aria-pressed'), 'true');
    assert.equal(await page.locator('[data-payment-id="p80-50"]').getAttribute('aria-pressed'), 'true');
    assert.equal(await page.locator('[data-payment-id="p80-30"]').getAttribute('aria-pressed'), 'false');
    assert.equal(await page.locator('#matchingSave').isDisabled(), false);
    await selectPayment('p80-30');
    await page.locator('#matchingSave').click(); await createdInReview('many-2');
    assert.equal(state.items.find(value => value.id === 'many-2').amount_cents, 8000);
    assert.match(await page.locator('#matchingGroups').innerText(), /2 张票据 · 2 张付款/);

    // Existing expense versions all travel with a grouping, rather than only the first one.
    await selectVoucher('e:many-left'); await selectVoucher('e:many-right'); await selectPayment('p-existing');
    assert.match(await page.locator('#matchingExistingNotice').innerText(), /合并为一笔.*不会.*重复/);
    manyRight.version = 'rv2'; state.items.find(value => value.id === 'many-right').version = 'rv2';
    await page.locator('#matchingRefresh').click(); await page.waitForFunction(() => !document.getElementById('matchingRefresh').disabled);
    assert.equal(await page.locator('[data-voucher-key="e:many-left"]').getAttribute('aria-pressed'), 'true');
    assert.equal(await page.locator('[data-voucher-key="e:many-right"]').getAttribute('aria-pressed'), 'false');
    assert.equal(await page.locator('[data-payment-id="p-existing"]').getAttribute('aria-pressed'), 'true');
    assert.equal(await page.locator('#matchingSave').isDisabled(), false);
    await selectVoucher('e:many-right');
    await page.locator('#matchingSave').click(); await createdInReview('many-3');
    const existingManyRequest = writes.filter(value => value.path === '/api/material-matching/create')[3].body;
    assert.deepEqual(existingManyRequest.expected_versions, {'many-left': 'lv1', 'many-right': 'rv2'});
    assert.deepEqual(existingManyRequest.voucher_keys, ['e:many-left', 'e:many-right']);
    assert.deepEqual(existingManyRequest.payment_ids, ['p-existing']);

    await selectVoucher('e:existing'); await selectPayment('pm'); await page.locator('#matchingSave').click();
    await page.waitForFunction(() => document.getElementById('matchingMessage').textContent.includes('版本已变化'));
    assert.equal(matchCount, 5); assert.equal(await page.locator('#matchingSave').isDisabled(), true);
    assert.equal(await page.locator('[data-voucher-key="e:existing"]').getAttribute('aria-pressed'), 'false');
    assert.equal(writes.filter(value => value.path === '/api/material-matching/create').length, 5, 'version conflict must wait for another explicit selection and save');
    await selectVoucher('e:existing'); await selectPayment('pm'); await page.locator('#matchingSave').click();
    await createdInReview('existing');
    assert.match(await page.locator('#detail').innerText(), /行程单/, 'The review detail keeps the remaining material requirement visible');
    assert.equal(matchCount, 6); assert.equal(state.items.find(value => value.id === 'existing').amount_cents, 5000);

    await selectVoucher('q:null-invoice'); await selectPayment('pn'); assert.equal(await page.locator('#matchingSave').isDisabled(), false);
    assert.equal(await page.locator('#matchingAmount').inputValue(), '12.00', 'unknown invoice amount should fall back to a fully recognized payment total');
    await classify('null-invoice', 'invoice', '12.00');
    await selectVoucher('q:null-invoice'); assert.equal(await page.locator('#matchingSave').isDisabled(), false);
    await classify('unknown', 'payment', '15.00'); await page.locator('[data-payment-id="unknown"]').waitFor();
    assert.equal(await page.locator('[data-classify-id="qr-initial"]').count(), 0, 'QR screenshot must not be editable into an invoice');
    assert.equal(await page.locator('[data-voucher-key="q:qr-initial"]').count(), 0);
    assert.match(await page.locator('#matchingOther').innerText(), /待领票/);

    const ledgerBeforeUpload = JSON.stringify(state.items), groupsBeforeUpload = JSON.stringify(board.matches);
    await page.locator('#matchingFiles').setInputFiles([
      {name: 'invoice.pdf', mimeType: 'application/pdf', buffer: Buffer.from('%PDF-1.4 synthetic')},
      ...['payment.png', 'qr-ready.png', 'qr-pending.png', 'failed.png'].map(name => ({name, mimeType: 'image/png', buffer: png})),
    ]);
    await page.waitForFunction(() => document.getElementById('matchingMessage').textContent.includes('本批 5 份'));
    assert.equal(received.length, 5); assert.equal(prepared.filter(id => id.startsWith('uploaded-')).length, 5);
    assert.equal(autoCount, 1, 'batch must trigger read-only suggestion discovery once after all preparation');
    const batchMessage = await page.locator('#matchingMessage').innerText();
    assert.match(batchMessage, /失败 1 项.*转为发票 PDF 1 份.*建议组合 1 组/);
    assert.ok(batchMessage.includes(autoIssue), 'batch completion must show the automatic search size restriction');
    assert.ok(batchMessage.includes(ambiguousReason), 'batch completion must explain ambiguous combinations');
    assert.equal(batchMessage.split(ambiguousReason).length - 1, 1, 'duplicate conflict explanations should be concise');
    assert.ok(!batchMessage.includes('internal-auto-voucher'), 'batch feedback must not expose internal material keys');
    assert.ok(received.some(value => value.name === 'failed.png' && value.data), 'failed preparation must keep the received original');
    assert.ok(board.other.some(value => value.name === 'qr-pending.png' && value.qr_status === 'manual'));
    assert.equal(JSON.stringify(state.items), ledgerBeforeUpload, 'upload suggestions never create or verify expenses');
    assert.equal(JSON.stringify(board.matches), groupsBeforeUpload, 'discovery does not consume materials into saved matches');
    assert.equal(board.suggestions.length, 1);
    assert.deepEqual(board.suggestions[0].voucher_keys, ['q:uploaded-1']);
    assert.deepEqual(board.suggestions[0].payment_ids, ['uploaded-2']);
    assert.equal(await page.locator('[data-voucher-key="q:uploaded-1"]').count(), 1, 'suggested invoice stays available before confirmation');
    assert.equal(await page.locator('[data-payment-id="uploaded-2"]').count(), 1, 'suggested payment stays available before confirmation');
    assert.ok(!writes.some(value => ['/api/intake/analyze', '/api/transition', '/api/report'].includes(value.path)));

    const itemCountBeforeLimits = state.items.length;
    for (const [limit, reason] of [['pool', poolLimitReason], ['candidates', candidateLimitReason]]) {
      autoLimit = limit;
      await page.getByRole('button', {name: '寻找合成组合', exact: true}).click();
      await page.waitForFunction(expected => document.getElementById('matchingMessage').textContent.includes(expected), reason);
      const feedback = await page.locator('#matchingMessage').innerText();
      assert.ok(feedback.includes(autoIssue), 'manual automatic matching must show the four-material search restriction');
      assert.ok(feedback.includes(reason), 'manual automatic matching must show the actual backend limit reason');
      assert.ok(!feedback.includes('internal-auto-voucher') && !feedback.includes('internal-auto-payment'), 'feedback must display only explanations, not internal keys');
      assert.equal(state.items.length, itemCountBeforeLimits, 'search limits must not invent matched expenses');
    }
    assert.equal(autoCount, 3);
    assert.ok(!writes.some(value => ['/api/transition', '/api/report'].includes(value.path)));

    // A newly saved invoice can pair with an older saved payment even with an unrelated 25+25 total.
    const merchantText = '合成青禾测试 & <img data-merchant-injection="true" src=x>';
    const freshInvoice50 = card('merchant-invoice50', 'invoice', 5000, {name: '新50元合成打印发票.pdf', revision: 6,
      fields: {merchant: merchantText, title: '合成打印费', invoice_number: 'synthetic-invoice50'}});
    const oldPayment50 = card('old-merchant-payment50', 'payment', 5000, {name: '旧50元合成付款截图.png', revision: 4,
      fields: {merchant: merchantText, title: '合成打印费', payment_date: '2026-09-30'}});
    const railwayA = card('railway25-a', 'invoice', 2500, {fields: {merchant: '中国铁路（合成）'}});
    const railwayB = card('railway25-b', 'invoice', 2500, {fields: {merchant: '中国铁路（合成）'}});
    board.vouchers.push(freshInvoice50, railwayA, railwayB); board.payments.push(oldPayment50);
    await page.locator('#matchingRefresh').click(); await page.waitForFunction(() => !document.getElementById('matchingRefresh').disabled);
    assert.match(await page.locator('[data-voucher-key="q:merchant-invoice50"]').innerText(), /销售方：合成青禾测试/);
    assert.ok((await page.locator('[data-payment-id="old-merchant-payment50"]').innerText()).includes(merchantText));
    assert.equal(await page.locator('[data-merchant-injection]').count(), 0, 'merchant strings must remain escaped text');
    const autoBeforeMerchant = autoCount, beforeMerchantReads = {board: boardReads, state: stateReads}, receivedBeforeMerchant = received.length;
    const beforeMerchantLedger = JSON.stringify(state.items), beforeMerchantPool = businessBoard();
    autoLimit = 'existing-merchant';
    await page.getByRole('button', {name: '寻找合成组合', exact: true}).click();
    await page.waitForFunction(() => !document.getElementById('matchingAuto').disabled && document.getElementById('matchingMessage').textContent.includes('1 组建议组合'));
    assert.equal(autoCount, autoBeforeMerchant + 1, 'a successful click issues only one automatic matching write');
    assert.ok(boardReads > beforeMerchantReads.board, 'read-only discovery refreshes the saved material pool');
    assert.equal(received.length, receivedBeforeMerchant, 'an older saved payment must not be reuploaded to match a new invoice');
    assert.equal(await page.locator('[data-voucher-key="q:merchant-invoice50"]').count(), 1, 'candidate invoice is not consumed before confirmation');
    assert.equal(await page.locator('[data-payment-id="old-merchant-payment50"]').count(), 1, 'older payment stays available before confirmation');
    assert.equal(await page.locator('[data-voucher-key="q:railway25-a"]').count(), 1);
    assert.equal(await page.locator('[data-voucher-key="q:railway25-b"]').count(), 1, 'unrelated equal-sum invoices remain in the material pool');
    assert.equal(await page.locator('[data-undo-group="merchant-existing-group"]').count(), 0, 'a suggestion is not a saved group');
    assert.ok((await page.locator('#matchingMessage').innerText()).includes(autoIssue));
    assert.equal(JSON.stringify(state.items), beforeMerchantLedger);
    assert.equal(businessBoard(), beforeMerchantPool, 'read-only discovery preserves all saved material associations');
    assert.deepEqual(board.suggestions[0].voucher_keys, ['q:merchant-invoice50']);
    assert.deepEqual(board.suggestions[0].payment_ids, ['old-merchant-payment50']);
    assert.equal(board.suggestions[0].amount_cents, 5000);
    await page.locator('#matchingClose').click();
    await page.goto(origin+'/?project=p1');
    await page.waitForFunction(()=>currentInvoiceProjectId()==='p1');
    await page.locator('#materialMatching').click();
    const merchantCandidate = page.locator('[data-synthesis-id="merchant-existing-candidate"]');
    await merchantCandidate.waitFor();
    assert.match(await merchantCandidate.innerText(), /建议合成/);
    assert.ok((await merchantCandidate.innerText()).includes(merchantText), 'escaped merchant name is preserved in the candidate');
    assert.equal(await page.locator('[data-merchant-injection]').count(), 0);
    await merchantCandidate.locator('.synthesis-row-preview').click();
    assert.equal(await page.locator('#matchingPreviewContent .matching-voucher-proofs figure,#matchingPreviewContent .matching-payment-proofs figure').count(), 2);
    assert.equal(JSON.stringify(state.items), beforeMerchantLedger, 'viewing candidate previews never creates a bill');
    await merchantCandidate.screenshot({path: path.join(evidence, 'synthetic-merchant-candidate-pending-synthesis.png')});
    await page.locator('.matching-groups summary').click();

    const ambiguousA = card('merchant-ambiguous-a', 'invoice', 5000, {fields: {merchant: merchantText}});
    const ambiguousB = card('merchant-ambiguous-b', 'invoice', 5000, {fields: {merchant: merchantText}});
    const ambiguousPayment = card('merchant-ambiguous-pay', 'payment', 5000, {fields: {merchant: merchantText}});
    board.vouchers.push(ambiguousA, ambiguousB); board.payments.push(ambiguousPayment);
    await page.locator('#matchingRefresh').click(); await page.waitForFunction(() => !document.getElementById('matchingRefresh').disabled);
    const conflictItems = JSON.stringify(state.items), conflictPool = businessBoard(), autoBeforeConflict = autoCount;
    autoLimit = 'merchant-conflict';
    await page.getByRole('button', {name: '寻找合成组合', exact: true}).click();
    await page.waitForFunction(expected => !document.getElementById('matchingAuto').disabled && document.getElementById('matchingMessage').textContent.includes(expected), merchantConflict);
    const merchantFeedback = await page.locator('#matchingMessage').innerText();
    assert.match(merchantFeedback, /¥\s*50\.00/);
    assert.ok(merchantFeedback.includes(autoIssue), 'merchant-aware matching still displays the maximum four invoices/four payments rule');
    assert.equal(merchantFeedback.split(merchantConflict).length - 1, 1);
    assert.ok(!merchantFeedback.includes('internal-auto-voucher') && !merchantFeedback.includes('internal-auto-payment'));
    assert.equal(autoCount, autoBeforeConflict + 1, 'an ambiguous result must not trigger another write');
    assert.equal(JSON.stringify(state.items), conflictItems); assert.equal(businessBoard(), conflictPool);
    assert.equal(board.suggestions.length, 2, 'both competing combinations remain explicit recommendations');
    assert.ok(board.suggestions.every(value => value.ambiguous && value.payment_ids[0] === 'merchant-ambiguous-pay'));
    for (const id of ['merchant-ambiguous-a', 'merchant-ambiguous-b']) assert.equal(await page.locator(`[data-voucher-key="q:${id}"]`).count(), 1);
    assert.equal(await page.locator('[data-payment-id="merchant-ambiguous-pay"]').count(), 1);
    await page.locator('#matchingMessage').screenshot({path: path.join(evidence, 'synthetic-merchant-conflict-feedback.png')});

    autoLimit = 'merchant-version-conflict';
    const autoBeforeVersionConflict = autoCount;
    await page.getByRole('button', {name: '寻找合成组合', exact: true}).click();
    await page.waitForFunction(() => !document.getElementById('matchingAuto').disabled && document.getElementById('matchingMessage').textContent.includes('商家配对材料版本已变化'));
    assert.equal(autoCount, autoBeforeVersionConflict + 1, 'a 409 automatic matching failure waits for another explicit action');
    assert.equal(JSON.stringify(state.items), conflictItems); assert.equal(businessBoard(), conflictPool);
    assert.equal(await page.locator('[data-payment-id="merchant-ambiguous-pay"]').count(), 1);
    assert.ok(!writes.some(value => ['/api/transition', '/api/report', '/api/report/confirm'].includes(value.path)));
    await page.locator('#matchingClose').click();
    await page.locator('#materialMatching').click();
    await page.locator('[data-synthesis-id="competing-merchant-ambiguous-a"]').waitFor();
    assert.equal(await page.locator('.synthesis-row').count(), 2);
    assert.ok((await page.locator('.synthesis-row .badge').allTextContents()).every(value => value === '存在竞争组合'));
    await page.locator('[data-synthesis-id="competing-merchant-ambiguous-a"] .synthesis-row-preview').click();
    assert.match(await page.locator('#matchingSuggestionReason').innerText(), /同一付款存在多个候选/);
    assert.equal(JSON.stringify(state.items), conflictItems);
    assert.equal(businessBoard(), conflictPool);
    autoLimit = '';

    // Every lane can create by itself. Recognized information is a starting point for confirmation.
    while (await page.locator('.matching-card[aria-pressed="true"]').count()) await page.locator('.matching-card[aria-pressed="true"]').first().click();
    const manualInvoice = card('manual-invoice', 'invoice', 4200, {project_type: 'living', project_name: '员工统一购餐费(附清单)', fields: {title: '识别餐费', category: '餐饮', amount: '42.00'}});
    const manualHotel = card('manual-hotel', 'invoice', 12000, {report_choice: {project_type: 'living', project_name: '住宿费(附清单)'}, fields: {title: '合成酒店住宿', category: '酒店'}});
    manualHotel.project_type = 'other'; manualHotel.project_name = '过期顶层字段';
    const manualPayment = card('manual-payment', 'payment', 1850, {project_type: 'transport', project_name: '打的(出租车、滴滴等)', fields: {title: '识别车费', category: '打车', amount: '18.50'}});
    const manualOther = card('manual-other', 'purchase', 700, {project_type: 'materials', project_name: '识别采购', fields: {title: '识别采购', category: '材料采购', amount: '7.00'}});
    const manualOtherTwo = card('manual-other-two', 'other', 900);
    const manualBlank = card('manual-blank', 'other', null, {project_type: '', project_name: '', fields: {category: '餐饮'}});
    board.vouchers.push(manualInvoice, manualHotel, card('manual-mismatch', 'invoice', 6000), card('manual-zero', 'invoice', 400));
    board.payments.push(manualPayment, card('manual-mismatch-pay', 'payment', 2500));
    board.other.push(manualOther, manualOtherTwo, manualBlank);
    await page.locator('#matchingRefresh').click(); await page.waitForFunction(() => !document.getElementById('matchingRefresh').disabled);
    const saveOnly = async () => {
      const id = `manual-only-${matchCount + 1}`;
      await page.locator('#matchingSave').click();
      await createdInReview(id);
      return state.items.find(value => value.id === id);
    };
    await selectVoucher('q:manual-hotel');
    assert.equal(await page.locator('#matchingProjectType').inputValue(), 'living', 'real nested report_choice must take precedence over stale top-level fields');
    assert.equal(await page.locator('#matchingProjectName').inputValue(), '住宿费(附清单)', 'nested hotel recognition must select the corresponding report row');
    await selectVoucher('q:manual-hotel');
    await selectVoucher('q:manual-invoice');
    assert.equal(await page.locator('#matchingAmount').inputValue(), '42.00');
    assert.equal(await page.locator('#matchingProjectType').inputValue(), 'living');
    assert.equal(await page.locator('#matchingProjectName').inputValue(), '员工统一购餐费(附清单)');
    assert.equal(await page.locator('#matchingProjectName').evaluate(node => node.tagName), 'SELECT');
    await selectVoucher('q:manual-mismatch');
    assert.equal(await page.locator('#matchingProjectType').inputValue(), '', 'conflicting mapped report types require a user choice');
    assert.equal(await page.locator('#matchingProjectName').inputValue(), '');
    assert.equal(await page.locator('#matchingSave').isDisabled(), true);
    await selectVoucher('q:manual-mismatch');
    assert.equal(await page.locator('.matching-voucher-proofs a[href="/intake-file/manual-invoice"]').count(), 1);
    const invoiceOnly = await saveOnly();
    assert.equal(invoiceOnly.amount_cents, 4200); assert.equal(invoiceOnly.verified, false);
    assert.match(await page.locator('#detail').innerText(), /付款截图/);

    await selectPayment('manual-payment');
    assert.equal(await page.locator('#matchingAmount').inputValue(), '18.50');
    assert.equal(await page.locator('#matchingProjectType').inputValue(), 'transport');
    assert.equal(await page.locator('#matchingProjectName').inputValue(), '打的(出租车、滴滴等)');
    for (const type of reportTypes.filter(value => !value.editable_name)) {
      await page.locator('#matchingProjectType').selectOption(type.code);
      assert.equal(await page.locator('#matchingProjectName').inputValue(), '', 'changing type must clear the prior detail and never select the first row');
      assert.equal(await page.locator('#matchingSave').isDisabled(), true);
      assert.deepEqual(await page.locator('#matchingProjectName option').evaluateAll(options => options.map(option => option.value).filter(Boolean)), type.names);
      await page.locator('#matchingProjectName').selectOption(type.names[0]);
      assert.equal(await page.locator('#matchingSave').isDisabled(), false);
    }
    await page.locator('#matchingProjectType').selectOption('transport');
    await page.locator('#matchingProjectName').evaluate(select => {
      select.add(new Option('住宿费(附清单)', '住宿费(附清单)')); select.value = '住宿费(附清单)';
      select.dispatchEvent(new Event('change', {bubbles: true}));
    });
    assert.equal(await page.locator('#matchingSave').isDisabled(), true, 'a stale or injected detail from another type must be rejected');
    assert.match(await page.locator('#matchingFieldsError').innerText(), /当前类型对应的细则/);
    await page.locator('#matchingProjectType').selectOption('other');
    assert.equal(await page.locator('#matchingProjectName').evaluate(node => node.tagName), 'INPUT');
    assert.ok((await page.locator('#matchingNameSuggestions option').evaluateAll(options => options.map(option => option.value))).includes('打的(出租车、滴滴等)'));
    await page.locator('#matchingProjectName').fill('人工修改的设备费用');
    await page.locator('#matchingAmount').fill('19.25');
    pendingQr.revision++;
    await page.locator('#matchingRefresh').click(); await page.waitForFunction(() => !document.getElementById('matchingRefresh').disabled);
    assert.equal(await page.locator('#matchingProjectName').inputValue(), '人工修改的设备费用', 'unrelated material refresh must preserve the edited name');
    assert.equal(await page.locator('#matchingAmount').inputValue(), '19.25', 'unrelated refresh must preserve the edited amount');
    assert.equal(await page.locator('#matchingProjectType').inputValue(), 'other', 'edited report type must survive unrelated refresh');
    const paymentOnly = await saveOnly();
    assert.equal(paymentOnly.amount_cents, 1925); assert.equal(paymentOnly.title, '人工修改的设备费用');
    assert.equal(paymentOnly.project_type, 'other'); assert.equal(paymentOnly.project_name, '人工修改的设备费用');
    assert.match(await page.locator('#detail').innerText(), /发票／收据/);
    const paymentGroupText = await page.locator(`[data-undo-group="only-group-${matchCount}"]`).locator('xpath=../..').innerText();
    assert.match(paymentGroupText, /0 张票据.*费用金额/);
    assert.ok(!paymentGroupText.includes('票据合计'), 'payment-only groups must not label the expense as an invoice total');

    await selectSupport('manual-other');
    assert.equal(await page.locator('#matchingAmount').inputValue(), '7.00');
    assert.equal(await page.locator('#matchingProjectType').inputValue(), 'materials');
    assert.equal(await page.locator('#matchingProjectName').inputValue(), '识别采购');
    assert.equal(await page.locator('#matchingProjectName').evaluate(node => node.tagName), 'INPUT');
    await selectSupport('manual-other-two');
    assert.equal(await page.locator('#matchingAmount').inputValue(), '', 'multiple ordinary materials must not silently add their prices');
    assert.equal(await page.locator('#matchingSave').isDisabled(), true);
    await selectSupport('manual-other-two');
    assert.equal(await page.locator('#matchingAmount').inputValue(), '7.00', 'selection changes should restore the new recognized defaults');
    const otherOnly = await saveOnly(); assert.equal(otherOnly.amount_cents, 700);

    await selectSupport('manual-blank');
    assert.equal(await page.locator('#matchingAmount').inputValue(), '');
    assert.equal(await page.locator('#matchingProjectType').inputValue(), '', 'raw category text must not override missing backend report mapping');
    assert.equal(await page.locator('#matchingProjectName').inputValue(), '');
    assert.equal(await page.locator('#matchingProjectName').isDisabled(), true);
    assert.equal(await page.locator('#matchingSave').isDisabled(), true, 'all three creation fields are mandatory');
    const writesBeforeBlank = writes.length;
    await page.locator('#matchingSave').evaluate(button => button.click());
    assert.equal(writes.length, writesBeforeBlank);
    await page.locator('#matchingProjectType').selectOption('living');
    await page.locator('#matchingProjectName').selectOption('住宿费(附清单)');
    assert.equal(await page.locator('#matchingSave').isDisabled(), true, 'amount remains required even with a valid report choice');
    await page.locator('#matchingAmount').fill('5.00');
    await page.locator('#matchingProjectName').selectOption('');
    assert.equal(await page.locator('#matchingSave').isDisabled(), true, 'detail remains required even with type and amount');
    await page.locator('#matchingProjectName').selectOption('住宿费(附清单)');
    const blankOnly = await saveOnly(); assert.equal(blankOnly.amount_cents, 500);
    const blankRequest = writes.filter(value => value.path === '/api/material-matching/create').at(-1).body;
    assert.deepEqual(blankRequest.fields, {project_type: 'living', amount: '5.00', project_name: '住宿费(附清单)'});
    board.matches.push({group_id: 'historical-pending', title: '历史金额待确认', amount_pending: true, amount_cents: 0, mode: 'registration', voucher_count: 0, payment_count: 0, support_count: 1, can_supplement: false});
    await page.locator('#matchingRefresh').click(); await page.waitForFunction(() => !document.getElementById('matchingRefresh').disabled);
    const pendingGroupText = await page.locator('[data-undo-group="historical-pending"]').locator('xpath=../..').innerText();
    assert.match(pendingGroupText, /金额待确认/);
    assert.ok(!pendingGroupText.includes('¥ 0.00'), 'legacy pending amount must not look like confirmed zero');

    await selectVoucher('q:manual-mismatch'); await selectPayment('manual-mismatch-pay');
    assert.match(await page.locator('#matchingDifference').innerText(), /35\.00/);
    assert.equal(await page.locator('#matchingAmount').inputValue(), '60.00', 'invoice and payment totals must never be added together');
    await page.locator('#matchingAmount').fill('61.00');
    const mismatchItem = await saveOnly(); assert.equal(mismatchItem.amount_cents, 6100);
    await selectVoucher('q:manual-zero'); await page.locator('#matchingAmount').fill('0');
    const zeroItem = await saveOnly(); assert.equal(zeroItem.amount_cents, 0, 'explicit zero is a valid nonnegative confirmation');
    assert.ok(!writes.some(value => value.path === '/api/material-matching/match'), 'manual confirmation must exclusively use create, leaving match strict');
    assert.ok(!writes.some(value => ['/api/transition', '/api/report'].includes(value.path)));

    await page.goto(origin+'/?project=p1');
    await page.locator('#materialMatching').click();
    assert.equal(await page.locator('#matchingProject').count(),0);
    await page.locator('[data-voucher-key="q:v1"]').waitFor();
    await selectVoucher('q:v1'); await selectPayment('p70'); await selectSupport('support-extra');
    await page.goto(origin+'/?project=p2'); await page.locator('#materialMatching').click();
    await page.waitForFunction(() => document.getElementById('matchingVouchers').textContent.includes('还没有待配对票据'));
    assert.equal(await page.locator('#matchingSave').isDisabled(), true);
    assert.equal(await page.locator('.matching-card[aria-pressed="true"]').count(), 0);
    await page.goto(origin+'/?project=p1'); await page.locator('#materialMatching').click(); await page.locator('[data-voucher-key="q:v1"]').waitFor();
    assert.equal(await page.locator('.matching-card[aria-pressed="true"]').count(), 0);
    await page.setViewportSize({width: 390, height: 844});
    await selectSupport('support-extra');
    assert.equal(await page.locator('.matching-support-proofs figure').count(), 1);
    assert.equal(await page.locator('#matchingDeskDialog').evaluate(node => node.scrollWidth <= node.clientWidth), true);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    assert.deepEqual(errors, []);
    const result = {passed: true, scope: 'synthetic HTTP frontend only', manualCreates: matchCount - 1, manyToManyGroups: manyMatchCount,
      twoVouchersOnePayment: true, twoVouchersTwoPayments: true, validSelectionPreservedOnRefresh: true, versionConflictBlocked: true,
      uploadedOriginals: received.length, convertedQrInvoices: 1, autoCalls: autoCount,
      automaticLimitsVisible: true, batchConflictFeedbackVisible: true, internalKeysHidden: true,
      newInvoiceSuggestsOlderSavedPayment: true, merchantDiscoveryRefreshesPoolAndPreservesLedger: true,
      unrelatedEqualSumInvoicesPreserved: true, merchantNamesEscaped: true, conflictAmountVisible: true,
      ambiguousMaterialsPreserved: true, automaticConflictNoWriteRetry: true, suggestionsNeverCreateExpenses: true,
      candidatePreviewReadOnly: true, competitionBadgesVisible: true,
      unknownSupportAmountAllowed: true, supplementaryAmountsExcluded: true, supplementCalls: supplementCount,
      existingGroupSupplementAndUndo: true, supportRevisionRefresh: true, qrSupportBlocked: true,
      purchaseCreationAllowed: true, purchaseSupplementStillGuarded: true, oldSplitPaymentButtonRemoved: true, delayedInitializationSafe: true,
      invoiceOnlyCreated: true, paymentOnlyCreated: true, otherOnlyCreated: true, allBlankBlocked: true,
      allThreeFieldsRequired: true, reportTypeDetailsLinked: true, crossTypeDetailRejected: true, openNamesRestrictedToTwoTypes: true,
      mappedRecognitionDefaults: true, nestedHotelAndTaxiDefaults: true, directSupplementCandidateWithoutNewExpense: true, candidateConflictExplicitRetry: true,
      mismatchManuallyConfirmed: true, editedFieldsSurviveRefresh: true,
      recognizedDefaultsChangeWithSelection: true, unknownInvoiceUsesKnownPaymentTotal: true,
      unknownGroupAmountVisible: true, noInvoiceGroupUsesExpenseAmount: true,
      amountValidationAndZeroPassed: true, groupIdentifiersHidden: true, liveLedgerWrites: 0};
    await fs.writeFile(path.join(evidence, 'material-matching-browser.json'), JSON.stringify(result, null, 2));
    await fs.writeFile(path.join(evidence, 'material-matching-browser-writes.json'), JSON.stringify(writes, null, 2));
    return result;
  } finally {releaseMatchingScript(); releaseFirstMatch(); await browser.close();}
}
module.exports = run;
if (require.main === module) run(require('playwright')).then(console.log).catch(error => {console.error(error); process.exitCode = 1;});
