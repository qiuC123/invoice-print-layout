// Actor evidence and explicit matching choice, using synthetic intercepted HTTP.
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');

async function run(pw) {
  const browser = await pw.chromium.launch({headless: true});
  const page = await browser.newPage({viewport: {width: 1500, height: 1000}});
  page.setDefaultTimeout(10000);
  const origin = 'http://127.0.0.1:18878', writes = [], errors = [];
  const evidence = process.env.INVOICE_BROWSER_EVIDENCE ? path.resolve(process.env.INVOICE_BROWSER_EVIDENCE) :
    path.resolve(__dirname, '../workspace/invoice-operations/platform-transaction-matching-20261016');
  const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j1ioAAAAASUVORK5CYII=', 'base64');
  const shop = '合成便利店（展馆店）';
  const identifiers = (role, channel = '支付宝') => [{kind: role === 'payment' ? 'payment_order' : 'shopping_order',
    namespace: role === 'payment' ? channel : '淘宝闪购', value: role === 'payment' ? '2026010523001100000000000001' : '4006123456789012345',
    label: '订单号', quote: '合成来源订单号'}];
  const card = (id, role, platform = '淘宝闪购', channel = '支付宝', actualShop = shop) => ({
    id, key: 'q:' + id, project_id: 'p1', role: role === 'payment' ? role : 'receipt', source_role: role,
    status: 'review', revision: 3, name: 'Synthetic ' + id + '.png', amount_cents: 1700,
    fields: {title: 'Synthetic platform purchase', category: '其他', merchant: role === 'payment' ? platform : actualShop,
      shopping_platform: platform, shop_name: actualShop, payment_channel: channel, payment_status: 'succeeded'},
    transaction_evidence: {shopping_platform: platform ? '合成来源平台：' + platform : '',
      shop_name: '合成来源店铺：' + actualShop, payment_channel: channel ? '合成来源支付方式：' + channel : ''},
    transaction_identifiers: identifiers(role, channel),
    report_choice: {project_type: 'other', project_name: 'Synthetic platform purchase'},
    thumbnail: '/intake-file/' + id, url: '/intake-file/' + id, issues: []});
  const order = card('order', 'order');
  const escaped = card('escaped', 'order', '淘宝闪购', '', '合成店铺 <img data-platform-injected="yes">');
  escaped.amount_cents = 2300;
  const payments = [card('pay-one', 'payment'), card('pay-two', 'payment', '美团外卖', '微信支付'),
    card('pay-unknown-channel', 'payment', '淘宝闪购', '')];
  payments[2].amount_cents = 1800;
  const suggestion = (id, paid, platform) => ({id, project_id: 'p1', snapshot_token: 'token-' + id,
    voucher_keys: ['q:order'], payment_ids: [paid], revisions: {order: 3, [paid]: 3}, expected_versions: {},
    amount_cents: 1700, needs_name_review: false, ambiguous: true, competing_count: 1,
    matching_context: {platform, shop: 'matched',
      order_numbers: 'different', checks: ['金额合计一致',
        platform === 'matched' ? '购物平台一致：淘宝闪购' : '购物平台不同，请对照原件确认',
        '店铺或原文商品说明对应',
        '不同编号体系的订单号不要求一致，编号用于区分与防重复']},
    reason: '金额合计一致；' + (platform === 'matched' ? '购物平台一致' : '购物平台不同，请核对') +
      '；店铺或商品说明对应',
    warnings: ['这些材料存在2种金额相符组合，请人工选择；不是唯一配对'],
    evidence: [{key: 'q:order', role: 'receipt', amount_cents: 1700, merchant: shop,
      shopping_platform: '淘宝闪购', shop_name: shop, payment_channel: '支付宝'},
    {key: 'q:' + paid, role: 'payment', amount_cents: 1700, merchant: '', descriptions: [shop],
      shopping_platform: platform === 'matched' ? '淘宝闪购' : '美团外卖', shop_name: shop,
      payment_channel: platform === 'matched' ? '支付宝' : '微信支付'}]});
  const candidates = [suggestion('platform-one', 'pay-one', 'matched'), suggestion('platform-two', 'pay-two', 'different')];
  const state = {app_mode: 'invoice', token: 'synthetic-platform-token',
    projects: [{id: 'p1', name: 'Synthetic platform project'}], items: [], inbox: [], categories: ['其他'],
    roles: {invoice: '发票', receipt: '收据', payment: '付款截图'}, stages: {draft: '待核对'}, category_auto_ready: false};
  let consumed = false;
  const board = () => ({vouchers: consumed ? [escaped] : [order, escaped],
    payments: consumed ? [payments[0], payments[2]] : payments, other: [], archived: [], deleted: [], matches: [],
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
        assert.equal(consumed, false); assert.equal(body.project_id, 'p1');
        assert.equal(body.suggestion_id, 'platform-two'); assert.equal(body.suggestion_token, 'token-platform-two');
        assert.deepEqual(body.voucher_keys, ['q:order']); assert.deepEqual(body.payment_ids, ['pay-two']);
        assert.deepEqual(body.fields, {project_type: 'other', project_name: 'Synthetic platform purchase', amount: '17.00'});
        consumed = true;
        const item = {id: 'created-platform', project_id: 'p1', project: 'Synthetic platform project',
          title: 'Synthetic platform purchase', category: '其他', amount: '17.00', amount_cents: 1700,
          stage: 'draft', version: 'platform-v1', verified: false, ready: false, complete: true,
          missing: [], review_requested: true, attachments: [], events: []};
        state.items.push(item); return json({item});
      }
      if (url.pathname === '/api/expense-print-preview') return json({id: body.id, version: body.expected_version,
        complete: true, missing: [], pdf: 'platform.pdf', preview_pages: ['platform.png'], page_count: 1});
      assert.fail('Unexpected business write: ' + url.pathname);
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
    await page.locator('#matchingSuggestions [data-synthesis-id="platform-one"]').waitFor();
    assert.equal(await page.locator('#matchingSuggestions [data-synthesis-id]').count(), 2);
    assert.equal(writes.length, 0, 'Opening platform choices cannot create or verify');
    const geometries = [];
    for (const theme of ['light', 'dark']) for (const width of [1500, 390]) {
      await page.locator('#themeSelect').selectOption(theme, {force: true});
      await page.setViewportSize({width, height: 1000});
      const paidText = await page.locator('[data-payment-id="pay-one"]').innerText();
      assert.match(paidText, /购物平台[：:]\s*淘宝闪购/);
      assert.match(await page.locator('[data-payment-id="pay-one"] > strong').innerText(), /合成便利店/);
      assert.doesNotMatch(paidText, /实际店铺[：:]/);
      assert.match(paidText, /支付渠道[：:]\s*支付宝/);
      assert.doesNotMatch(paidText, /商家[：:]\s*淘宝闪购/, 'A shopping platform cannot be presented as the actual shop');
      const unknownText = await page.locator('[data-payment-id="pay-unknown-channel"]').innerText();
      assert.match(unknownText, /购物平台[：:]\s*淘宝闪购/);
      assert.doesNotMatch(unknownText, /支付渠道[：:]\s*(支付宝|微信支付)/, 'Unknown channels stay unknown');
      assert.ok((await page.locator('[data-voucher-key="q:escaped"]').innerText()).includes('<img data-platform-injected="yes">'));
      assert.equal(await page.locator('[data-platform-injected]').count(), 0, 'Actor fields must be escaped');
      await page.locator('[data-synthesis-id="platform-two"] .synthesis-row-preview').click();
      const reason = await page.locator('#matchingSuggestionReason').innerText();
      assert.doesNotMatch(reason, /金额合计一致|购物平台一致|店铺或原文商品说明对应|编号体系/);
      assert.doesNotMatch(reason, /时间|分钟|秒/, 'Matching reasons must not compare transaction timestamps');
      assert.match(reason, /2种.*人工选择/s);
      assert.equal(await page.locator('#matchingPreviewContent .matching-proof[href]').count(), 2);
      assert.equal(await page.locator('[data-payment-id="pay-two"]').getAttribute('aria-pressed'), 'true');
      assert.equal(await page.locator('[data-payment-id="pay-one"]').getAttribute('aria-pressed'), 'false');
      const bounds = await page.locator('#matchingDeskDialog').evaluate(node => ({scroll: node.scrollWidth,
        client: node.clientWidth, page: document.documentElement.scrollWidth, width: innerWidth}));
      assert.ok(bounds.scroll <= bounds.client + 1 && bounds.page <= bounds.width + 1, `${theme}/${width}: content fits`);
      assert.equal(writes.length, 0, 'Evidence and original preview remain read-only');
      geometries.push({theme, width});
    }
    await page.screenshot({path: path.join(evidence, 'synthetic-platform-dark-mobile.png')});
    await page.setViewportSize({width: 1500, height: 1000});
    assert.equal(await page.locator('#matchingSave').isEnabled(), true);
    await page.locator('#matchingSave').click();
    await page.waitForFunction(() => !document.getElementById('matchingDeskDialog').open);
    assert.equal(writes.filter(row => row.path === '/api/material-matching/create').length, 1);
    assert.equal(state.items.length, 1); assert.equal(state.items[0].amount_cents, 1700);
    assert.equal(state.items[0].verified, false); assert.equal(state.items[0].ready, false);
    assert.equal(writes.filter(row => /classify|transition|\/report$|create-batch/.test(row.path)).length, 0);
    assert.deepEqual(errors, []);
    const result = {passed: true, separatedPlatformShopAndChannel: true, unknownChannelNotInvented: true,
      escapedActorFields: true, choiceCount: 2, reasonExplainsPlatformShopAmountOnly: true, geometries,
      scanAndPreviewReadOnly: true, oneExplicitUnverifiedExpenseCents: 1700, chosenPayment: 'pay-two', pageErrors: errors};
    await fs.writeFile(path.join(evidence, 'platform-transaction-matching-browser.json'), JSON.stringify(result, null, 2));
    return result;
  } finally {await browser.close();}
}
module.exports = run;
if (require.main === module) run(require('playwright')).then(console.log).catch(error => {console.error(error); process.exitCode = 1;});
