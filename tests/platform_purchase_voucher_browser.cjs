// Paid platform purchase receipts in lane 1, using synthetic HTTP fixtures only.
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');

async function run(pw) {
  const browser = await pw.chromium.launch({headless: true});
  const page = await browser.newPage({viewport: {width: 1500, height: 1000}});
  page.setDefaultTimeout(10000);
  const origin = 'http://127.0.0.1:18873', errors = [], writes = [];
  const evidence = process.env.INVOICE_BROWSER_EVIDENCE ? path.resolve(process.env.INVOICE_BROWSER_EVIDENCE) :
    path.resolve(__dirname, '../workspace/invoice-operations/platform-purchase-voucher-20261016');
  const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j1ioAAAAASUVORK5CYII=', 'base64');
  const card = (id, role, extra = {}) => ({id, key: 'q:' + id, project_id: 'p1', role, kind: role, revision: 2,
    status: 'review', name: 'Synthetic ' + id + '.png', amount_cents: 1700,
    fields: {title: 'Synthetic ' + id, category: '其他', merchant: 'Synthetic platform shop', payment_status: 'succeeded'},
    report_choice: {project_type: 'other', project_name: 'Synthetic purchase'},
    thumbnail: '/intake-file/' + id, url: '/intake-file/' + id, issues: [], ...extra});
  const order = card('order-receipt', 'receipt', {source_role: 'order', issues: ['AI与原件字段冲突：merchant']}),
    purchase = card('purchase-receipt', 'receipt', {source_role: 'purchase'}),
    invoice = card('formal-invoice', 'invoice'), regular = card('plain-receipt', 'receipt'),
    payment = card('payment', 'payment'), ordinary = card('ordinary', 'other');
  const state = {app_mode: 'invoice', token: 'synthetic-token', projects: [{id: 'p1', name: 'Synthetic project'}],
    items: [], inbox: [], categories: ['其他'], roles: {invoice: '发票', receipt: '收据', payment: '付款截图', other: '其他'},
    stages: {draft: '待核对'}, category_auto_ready: false};
  let saved = false;
  const board = () => ({vouchers: saved ? [purchase, invoice, regular] : [order, purchase, invoice, regular],
    payments: saved ? [] : [payment], other: [ordinary], matches: [], suggestions: [], supplement_candidates: [],
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
        assert.deepEqual(body.voucher_keys, ['q:order-receipt']); assert.deepEqual(body.payment_ids, ['payment']);
        assert.deepEqual(body.support_ids, []); assert.equal(body.project_id, 'p1');
        assert.deepEqual(body.revisions, {'order-receipt': 2, payment: 2});
        assert.deepEqual(body.fields, {project_type: 'other', project_name: 'Synthetic purchase', amount: '17.00'});
        saved = true;
        const item = {id: 'created', title: 'Synthetic purchase', category: '其他', amount: '17.00', amount_cents: 1700,
          version: 'created-v1', project_id: 'p1', project: 'Synthetic project', stage: 'draft', verified: false,
          ready: false, complete: true, missing: [], review_requested: true, attachments: [], events: []};
        state.items.push(item); return json({item});
      }
      if (url.pathname === '/api/expense-print-preview') return json({id: body.id, version: body.expected_version,
        complete: true, missing: [], pdf: 'created.pdf', preview_pages: ['created.png'], page_count: 1});
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
    const selected = page.locator('[data-voucher-key="q:order-receipt"]');
    await selected.waitFor();
    assert.equal(await page.locator('#matchingVoucherCount').innerText(), '4 份');
    assert.equal(await page.locator('#matchingOtherCount').innerText(), '1 份');
    assert.equal(await page.locator('#matchingVouchers [data-voucher-key]').count(), 4);
    assert.equal(await page.locator('#matchingOther [data-support-id="order-receipt"],#matchingOther [data-support-id="purchase-receipt"]').count(), 0);
    assert.equal(await page.locator('#matchingOther [data-support-id="ordinary"]').count(), 1);
    for (const theme of ['light', 'dark']) {
      await page.locator('#themeSelect').selectOption(theme, {force: true});
      assert.doesNotMatch((await selected.locator('small').allTextContents()).join('；'), /收据 · 平台购买记录/);
      assert.equal(await page.locator('#matchingVouchers [data-voucher-key="q:purchase-receipt"]').count(), 1, 'platform purchases stay in the voucher section');
      assert.doesNotMatch(await page.locator('[data-voucher-key="q:formal-invoice"]').innerText(), /平台购买记录/);
      assert.doesNotMatch(await page.locator('[data-voucher-key="q:plain-receipt"]').innerText(), /平台购买记录/);
      assert.match(await selected.innerText(), /AI与本地识别的商家名称不同/, 'Classification must retain the Chinese review warning');
      await selected.click(); assert.equal(await selected.getAttribute('aria-pressed'), 'true');
      assert.equal(await page.locator('#matchingAmount').inputValue(), '17.00');
      assert.equal(await page.locator('#matchingPreviewContent .matching-proof[href="/intake-file/order-receipt"]').count(), 1);
      await selected.click(); assert.equal(await selected.getAttribute('aria-pressed'), 'false');
      assert.equal(await page.locator('#matchingSave').isDisabled(), true);
      await page.screenshot({path: path.join(evidence, 'synthetic-platform-receipt-' + theme + '.png')});
    }
    assert.equal(writes.length, 0, 'Preview and toggling must not classify or create expenses');
    await selected.click(); await page.locator('[data-payment-id="payment"]').click();
    assert.equal(await page.locator('#matchingAmount').inputValue(), '17.00', 'Receipt 17 + payment 17 remains one 17 expense');
    assert.equal(await page.locator('#matchingPreviewContent .matching-proof[href]').count(), 2);
    assert.equal(await page.locator('#matchingSave').isEnabled(), true);
    await page.locator('#matchingSave').click();
    await page.waitForFunction(() => !document.getElementById('matchingDeskDialog').open);
    assert.equal(writes.filter(row => row.path === '/api/material-matching/create').length, 1);
    assert.equal(state.items.length, 1); assert.equal(state.items[0].amount_cents, 1700);
    assert.equal(state.items[0].verified, false); assert.equal(state.items[0].ready, false);
    assert.equal(writes.filter(row => /classify|transition|report$/.test(row.path)).length, 0);
    assert.deepEqual(errors, []);
    const result = {passed: true, projectedPurchaseLaneOne: true, ordinaryLaneThree: true,
      originalReceiptAndInvoiceLabelsPreserved: true, warningsPreserved: true,
      toggleAndOriginalPreview: true, themes: ['light', 'dark'], amountCountedOnce: 1700,
      singleExplicitCreation: true, noVerificationSubmissionOrClassification: true, pageErrors: errors};
    await fs.writeFile(path.join(evidence, 'platform-purchase-voucher-browser.json'), JSON.stringify(result, null, 2));
    return result;
  } finally {await browser.close();}
}
module.exports = run;
if (require.main === module) run(require('playwright')).then(console.log).catch(error => {console.error(error); process.exitCode = 1;});
