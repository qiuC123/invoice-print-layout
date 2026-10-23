// Contextual choices and readable field conflicts; only synthetic intercepted HTTP.
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');

async function run(pw) {
  const browser = await pw.chromium.launch({headless: true});
  const page = await browser.newPage({viewport: {width: 1500, height: 1000}});
  page.setDefaultTimeout(10000);
  const origin = 'http://127.0.0.1:18874', errors = [], writes = [];
  const evidence = process.env.INVOICE_BROWSER_EVIDENCE ? path.resolve(process.env.INVOICE_BROWSER_EVIDENCE) :
    path.resolve(__dirname, '../workspace/invoice-operations/contextual-material-matching-20261016');
  const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j1ioAAAAASUVORK5CYII=', 'base64');
  const shop = '京东便利店（合成湖店）', otherShop = '合成阳光便利店';
  const card = (id, role, extra = {}) => ({id, key: 'q:' + id, project_id: 'p1', role,
    status: 'review', revision: 3, name: 'Synthetic ' + id + '.png', amount_cents: 1700,
    fields: {title: 'Synthetic purchase', category: '其他', merchant: role === 'payment' ? '淘宝闪购' : shop,
      payment_status: 'succeeded'},
    report_choice: {project_type: 'other', project_name: 'Synthetic contextual purchase'},
    thumbnail: '/intake-file/' + id, url: '/intake-file/' + id, issues: [], ...extra});
  const order = card('order', 'receipt', {source_role: 'order',
    issues: ['页面未显示订单号或交易单号，无法建立本笔交易与平台的唯一对应关系'],
    recognition_notes: ['订单号用于区分记录和避免重复；不同体系的编号无需一致，配对结合平台、店铺和金额。']});
  const conflict = card('conflict', 'receipt', {source_role: 'order',
    issues: ['AI与原件字段冲突：merchant'],
    field_conflicts: [{field: 'merchant', label: '商家名称', local_value: shop,
      ai_value: otherShop + ' <img data-context-injected="yes">', local_quote: '商家名称：' + shop,
      ai_quote: otherShop, local_source: 'local_ocr'}]});
  const legacy = card('legacy-conflict', 'receipt', {source_role: 'purchase', issues: ['AI与原件字段冲突：merchant']});
  const mixedWarnings = ['交易单号不同，付款状态待确认', '订单号缺失，材料用途待确认', '订单号不同，发票号码冲突'];
  const mixed = card('mixed-warning', 'receipt', {source_role: 'purchase', issues: mixedWarnings});
  const paid = [card('pay-one', 'payment'), card('pay-two', 'payment')];
  const suggestion = (id, paymentId) => ({id, project_id: 'p1', snapshot_token: 'token-' + id,
    voucher_keys: ['q:order'], payment_ids: [paymentId], revisions: {order: 3, [paymentId]: 3},
    expected_versions: {}, amount_cents: 1700, needs_name_review: false, ambiguous: true, competing_count: 1,
    matching_context: {platform: 'unknown', shop: 'matched', order_numbers: 'different',
      checks: ['金额合计一致', '店铺与付款商品说明一致', '购物平台信息未明，结合店铺与金额核对',
        '平台业务订单号与支付订单号可能属于不同编号体系，不要求相同']},
    reason: '金额合计一致；店铺与商品说明对应；购物平台信息未明，等待确认合成',
    warnings: ['这些材料存在2种金额相符组合，请人工选择；不是唯一配对'],
    evidence: [{key: 'q:order', role: 'receipt', amount_cents: 1700, merchant: shop, descriptions: []},
    {key: 'q:' + paymentId, role: 'payment', amount_cents: 1700, merchant: '', descriptions: [shop]}]});
  const candidates = [suggestion('context-one', 'pay-one'), suggestion('context-two', 'pay-two')];
  const state = {app_mode: 'invoice', token: 'synthetic-context-token',
    projects: [{id: 'p1', name: 'Synthetic contextual project'}], items: [], inbox: [], categories: ['其他'],
    roles: {invoice: '发票', receipt: '收据', payment: '付款截图'}, stages: {draft: '待核对'}, category_auto_ready: false};
  let consumed = false;
  const board = () => ({vouchers: consumed ? [conflict, legacy, mixed] : [order, conflict, legacy, mixed],
    payments: consumed ? [paid[0]] : paid, other: [], archived: [], deleted: [], matches: [],
    suggestions: consumed ? [] : candidates, supplement_candidates: [],
    report_types: [{code: 'other', label: '其他', editable_name: true, names: []}]});
  page.on('pageerror', error => errors.push(error.message));
  await page.route(origin + '/**', async route => {
    const request = route.request(), url = new URL(request.url());
    const json = value => route.fulfill({contentType: 'application/json', body: JSON.stringify(value)});
    if (url.pathname === '/api/state') return json(state);
    if (url.pathname === '/api/material-matching') return json(board());
    if (url.pathname === '/api/intake') return json({queue: []});
    if (url.pathname === '/api/reimbursement-people') return json({people: [], summary: {rows: []}});
    if (request.method() === 'POST') {
      const body = request.postDataJSON(); writes.push({path: url.pathname, body});
      if (url.pathname === '/api/material-matching/create') {
        assert.equal(body.project_id, 'p1'); assert.equal(body.suggestion_id, 'context-two');
        assert.equal(body.suggestion_token, 'token-context-two');
        assert.deepEqual(body.voucher_keys, ['q:order']); assert.deepEqual(body.payment_ids, ['pay-two']);
        assert.deepEqual(body.revisions, {order: 3, 'pay-two': 3});
        assert.deepEqual(body.fields, {project_type: 'other', project_name: 'Synthetic contextual purchase', amount: '17.00'});
        assert.equal(consumed, false, 'Explicit confirmation cannot duplicate a creation'); consumed = true;
        const item = {id: 'created-context', project_id: 'p1', project: 'Synthetic contextual project',
          title: 'Synthetic contextual purchase', category: '其他', amount: '17.00', amount_cents: 1700,
          version: 'created-v1', stage: 'draft', verified: false, ready: false, complete: true,
          missing: [], review_requested: true, attachments: [], events: []};
        state.items.push(item); return json({item});
      }
      if (url.pathname === '/api/expense-print-preview') return json({id: body.id, version: body.expected_version,
        complete: true, missing: [], pdf: 'context.pdf', preview_pages: ['context.png'], page_count: 1});
      assert.fail('Unexpected business write ' + url.pathname);
    }
    if (url.pathname.startsWith('/intake-file/') || url.pathname.startsWith('/export/')) {
      return route.fulfill({contentType: 'image/png', body: png});
    }
    const file = url.pathname === '/' ? 'index.html' : url.pathname.slice(1);
    if (!/^[a-z-]+\.(html|js|css)$/.test(file)) return route.abort();
    return route.fulfill({contentType: file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html',
      body: await fs.readFile(path.resolve(__dirname, '../src/invoice_print_layout/web', file))});
  });
  try {
    await fs.mkdir(evidence, {recursive: true}); await page.goto(origin + '/?project=p1');
    await page.waitForFunction(() => typeof window.openMaterialMatching === 'function' && !!state.token);
    await page.locator('#materialMatching').click();
    await page.locator('#matchingSuggestions [data-synthesis-id="context-one"]').waitFor();
    assert.equal(await page.locator('#matchingSuggestions [data-synthesis-id]').count(), 2);
    assert.equal(await page.locator('[data-synthesis-check]:checked').count(), 0);
    assert.equal(writes.length, 0, 'Loading choices cannot classify, create, verify, or submit');
    const geometries = [];
    for (const theme of ['light', 'dark']) for (const width of [1500, 390]) {
      await page.locator('#themeSelect').selectOption(theme, {force: true});
      await page.setViewportSize({width, height: 1000});
      const conflictText = await page.locator('[data-voucher-key="q:conflict"]').innerText();
      assert.match(conflictText, /商家名称/); assert.ok(conflictText.includes(shop)); assert.ok(conflictText.includes(otherShop));
      assert.doesNotMatch(conflictText, /merchant/, 'A user must see the Chinese field name');
      const legacyText = await page.locator('[data-voucher-key="q:legacy-conflict"]').innerText();
      assert.match(legacyText, /历史.*未保存.*AI.*名称/s); assert.doesNotMatch(legacyText, /merchant/);
      const orderWarnings = await page.locator('[data-voucher-key="q:order"] .matching-card-issues').allTextContents();
      assert.ok(orderWarnings.every(value => !/订单号|交易单号|唯一对应关系/.test(value)),
        'A historical missing business-order number must not stay an error-looking identity warning');
      const mixedCard = page.locator('[data-voucher-key="q:mixed-warning"]');
      const retainedWarnings = (await mixedCard.locator('.matching-card-issues').allTextContents()).join('；');
      for (const warning of mixedWarnings) assert.ok(retainedWarnings.includes(warning),
        `A business-order note mixed with a core review warning must retain the warning: ${warning}`);
      assert.equal(await mixedCard.locator('.matching-recognition-note').count(), 0,
        'Mixed payment-state, material-role, and invoice-number warnings cannot be demoted to an optional order note');
      assert.equal(await page.locator('[data-context-injected]').count(), 0, 'Conflict values are escaped as text');
      await page.locator('[data-synthesis-id="context-two"] .synthesis-row-preview').click();
      const reason = await page.locator('#matchingSuggestionReason').innerText();
      assert.doesNotMatch(reason, /金额合计一致|店铺与商品说明对应|购物平台信息未明|编号体系/);
      assert.equal(await page.locator('.matching-recognition-note,#matchingAIStatus').count(), 0);
      assert.doesNotMatch(await page.locator('[data-voucher-key="q:order"]').innerText(), /订单号用于区分|防重|无需一致/);
      assert.doesNotMatch(reason, /时间|分钟|秒/, 'Matching reasons must not compare transaction timestamps');
      assert.match(reason, /2种.*人工选择/s);
      assert.equal(await page.locator('#matchingPreviewContent .matching-proof[href]').count(), 2);
      assert.equal(await page.locator('[data-payment-id="pay-two"]').getAttribute('aria-pressed'), 'true');
      assert.equal(await page.locator('[data-payment-id="pay-one"]').getAttribute('aria-pressed'), 'false');
      assert.equal(await page.locator('#matchingAmount').inputValue(), '17.00');
      const bounds = await page.locator('#matchingDeskDialog').evaluate(node => ({scroll: node.scrollWidth, client: node.clientWidth,
        page: document.documentElement.scrollWidth, width: innerWidth}));
      assert.ok(bounds.scroll <= bounds.client + 1 && bounds.page <= bounds.width + 1, `${theme}/${width}: readable content must fit`);
      assert.equal(writes.length, 0, 'Reading competing originals and reasons cannot choose on the user behalf');
      geometries.push({theme, width});
    }
    await page.screenshot({path: path.join(evidence, 'synthetic-context-dark-mobile.png')});
    await page.setViewportSize({width: 1500, height: 1000});
    assert.equal(await page.locator('#matchingSave').isEnabled(), true);
    await page.locator('#matchingSave').click();
    await page.waitForFunction(() => !document.getElementById('matchingDeskDialog').open);
    assert.equal(writes.filter(row => row.path === '/api/material-matching/create').length, 1);
    assert.equal(state.items.length, 1); assert.equal(state.items[0].amount_cents, 1700);
    assert.equal(state.items[0].verified, false); assert.equal(state.items[0].ready, false);
    assert.equal(writes.filter(row => /classify|transition|\/report$|create-batch/.test(row.path)).length, 0);
    assert.deepEqual(errors, []);
    const result = {passed: true, choiceCount: 2, contextualReasonsAndOriginals: true,
      explicitAlternativeChosen: 'pay-two', chineseFieldConflictWithBothValues: true,
      legacyConflictDoesNotInventAiValue: true, escapedConflictValues: true, geometries,
      mixedOrderWarningsRetainPaymentStateRoleAndInvoiceNumber: true,
      scanAndPreviewReadOnly: true, oneExplicitUnverifiedExpenseCents: 1700, pageErrors: errors};
    await fs.writeFile(path.join(evidence, 'contextual-material-matching-browser.json'), JSON.stringify(result, null, 2));
    return result;
  } finally {await browser.close();}
}
module.exports = run;
if (require.main === module) run(require('playwright')).then(console.log).catch(error => {console.error(error); process.exitCode = 1;});
