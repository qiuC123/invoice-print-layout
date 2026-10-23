// Shop identity and amount matching without comparing order/payment timestamps.
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');

async function run(pw) {
  const browser = await pw.chromium.launch({headless: true});
  const page = await browser.newPage({viewport: {width: 1500, height: 1000}});
  page.setDefaultTimeout(10000);
  const origin = 'http://127.0.0.1:18879', writes = [], errors = [];
  const evidence = process.env.INVOICE_BROWSER_EVIDENCE ? path.resolve(process.env.INVOICE_BROWSER_EVIDENCE) :
    path.resolve(__dirname, '../workspace/invoice-operations/platform-shop-matching-20261016');
  const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j1ioAAAAASUVORK5CYII=', 'base64');
  const shop = '京东便利店（合成湖店）', branch = '京东便利店（合成西门店）';
  const card = (id, role, actualShop = shop) => ({id, key: 'q:' + id, project_id: 'p1',
    role: role === 'payment' ? role : 'receipt', source_role: role, status: 'review', revision: 3,
    name: 'Synthetic ' + id + '.png', amount_cents: 1700,
    fields: {title: 'Synthetic platform purchase', category: '其他', merchant: role === 'payment' ? '淘宝闪购' : actualShop,
      shopping_platform: '淘宝闪购', shop_name: actualShop, payment_channel: '支付宝', payment_status: 'succeeded'},
    transaction_evidence: {shopping_platform: '淘宝闪购', shop_name: '店铺名称：' + actualShop, payment_channel: '支付方式：支付宝'},
    transaction_identifiers: [{kind: role === 'payment' ? 'payment_order' : 'shopping_order',
      namespace: role === 'payment' ? '支付宝' : '淘宝闪购', value: role === 'payment' ?
        '2026010523001100000000000001' : '4006123456789012345', label: '订单号', quote: '合成来源订单号'}],
    report_choice: {project_type: 'other', project_name: 'Synthetic platform purchase'},
    thumbnail: '/intake-file/' + id, url: '/intake-file/' + id, issues: []});
  const order = card('order', 'order');
  // The server resolved the historical bare marker using matching local/AI
  // source quotes. The original records remain untouched in backend tests.
  order.local_evidence = {merchant: shop}; order.ai_evidence = {merchant: shop};
  const genuine = card('genuine-conflict', 'order'); genuine.amount_cents = 2300;
  genuine.issues = ['AI与本地识别的商家名称不同，请对照原件确认。'];
  genuine.field_conflicts = [{field: 'merchant', label: '商家名称', local_value: shop,
    ai_value: branch + ' <img data-shop-injected="yes">', local_quote: shop,
    ai_quote: branch, local_source: 'local_ocr'}];
  const payments = [card('same-shop', 'payment', '京东 便利店 ( 合成湖店 )'), card('other-branch', 'payment', branch)];
  const suggestion = (id, paid, matched) => ({id, project_id: 'p1', snapshot_token: 'token-' + id,
    voucher_keys: ['q:order'], payment_ids: [paid], revisions: {order: 3, [paid]: 3}, expected_versions: {},
    amount_cents: 1700, needs_name_review: !matched, ambiguous: true, competing_count: 1,
    matching_context: {platform: 'matched', shop: matched ? 'matched' : 'different', order_numbers: 'different',
      checks: ['金额合计一致', matched ? '店铺或原文商品说明对应' : '店铺分店不同，请对照原件确认',
        '购物平台一致：淘宝闪购', '不同编号体系的订单号不要求一致，编号用于区分与防重复']},
    reason: '金额合计一致；' + (matched ? '店铺或原文商品说明对应' : '店铺分店不同，请对照原件确认') + '；购物平台一致：淘宝闪购',
    warnings: [...(matched ? [] : ['店铺分店不同，请对照原件确认']), '这些材料存在2种金额相符组合，请人工选择；不是唯一配对'],
    evidence: [{key: 'q:order', role: 'receipt', amount_cents: 1700, merchant: shop,
      shopping_platform: '淘宝闪购', shop_name: shop, payment_channel: '支付宝'},
    {key: 'q:' + paid, role: 'payment', amount_cents: 1700, merchant: '',
      descriptions: [matched ? shop : branch], shopping_platform: '淘宝闪购', shop_name: matched ? shop : branch,
      payment_channel: '支付宝'}]});
  const candidates = [suggestion('shop-matched', 'same-shop', true), suggestion('branch-different', 'other-branch', false)];
  const state = {app_mode: 'invoice', token: 'synthetic-shop-token',
    projects: [{id: 'p1', name: 'Synthetic shop project'}], items: [], inbox: [], categories: ['其他'],
    roles: {invoice: '发票', receipt: '收据', payment: '付款截图'}, stages: {draft: '待核对'}, category_auto_ready: false};
  const board = {vouchers: [order, genuine], payments, other: [], archived: [], deleted: [], matches: [],
    suggestions: candidates, supplement_candidates: [], report_types: [{code: 'other', label: '其他', editable_name: true, names: []}]};
  page.on('pageerror', error => errors.push(error.message));
  await page.route(origin + '/**', async route => {
    const request = route.request(), url = new URL(request.url());
    const json = value => route.fulfill({contentType: 'application/json', body: JSON.stringify(value)});
    if (url.pathname === '/api/state') return json(state);
    if (url.pathname === '/api/material-matching') return json(board);
    if (url.pathname === '/api/intake') return json({queue: []});
    if (url.pathname === '/api/reimbursement-people') return json({people: [], summary: {rows: []}});
    if (request.method() === 'POST') {
      writes.push(url.pathname); assert.fail('Reading shop evidence must remain read-only: ' + url.pathname);
    }
    if (url.pathname.startsWith('/intake-file/')) return route.fulfill({contentType: 'image/png', body: png});
    const file = url.pathname === '/' ? 'index.html' : url.pathname.slice(1);
    if (!/^[a-z-]+\.(html|js|css)$/.test(file)) return route.abort();
    return route.fulfill({contentType: file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html',
      body: await fs.readFile(path.resolve(__dirname, '../src/invoice_print_layout/web', file))});
  });
  try {
    await fs.mkdir(evidence, {recursive: true}); await page.goto(origin + '/?project=p1');
    await page.waitForFunction(() => typeof window.openMaterialMatching === 'function' && !!state.token);
    await page.locator('#materialMatching').click();
    await page.locator('#matchingSuggestions [data-synthesis-id="shop-matched"]').waitFor();
    assert.equal(await page.locator('#matchingSuggestions [data-synthesis-id]').count(), 2);
    const geometries = [];
    for (const theme of ['light', 'dark']) for (const width of [1500, 390]) {
      await page.locator('#themeSelect').selectOption(theme, {force: true});
      await page.setViewportSize({width, height: 1000});
      const same = page.locator('[data-voucher-key="q:order"]');
      assert.equal(await same.locator('.matching-card-issues,.matching-field-conflict').count(), 0);
      assert.doesNotMatch(await same.innerText(), /识别不一致|名称不同|历史记录未保存AI/);
      const actualConflict = await page.locator('[data-voucher-key="q:genuine-conflict"]').innerText();
      assert.ok(actualConflict.includes(shop) && actualConflict.includes(branch));
      assert.match(actualConflict, /商家名称.*本地识别.*AI补读/s);
      assert.equal(await page.locator('[data-shop-injected]').count(), 0);
      await page.locator('[data-synthesis-id="shop-matched"] .synthesis-row-preview').click();
      const reason = await page.locator('#matchingSuggestionReason').innerText();
      assert.doesNotMatch(reason, /金额合计一致|店铺或原文商品说明对应|购物平台一致|编号体系/);
      assert.doesNotMatch(reason, /时间|间隔|相差.*秒|精确到分钟|名称.*不同|识别不一致/);
      assert.match(reason, /2种.*人工选择/s);
      assert.equal(await page.locator('#matchingAIStatus').count(), 0);
      assert.equal(await page.locator('#matchingPreviewContent .matching-proof[href]').count(), 2);
      await page.locator('[data-synthesis-id="branch-different"] .synthesis-row-preview').click();
      const different = await page.locator('#matchingSuggestionReason').innerText();
      assert.match(different, /店铺.*不同.*原件/s);
      assert.doesNotMatch(different, /时间|间隔|相差.*秒|精确到分钟/);
      const bounds = await page.locator('#matchingDeskDialog').evaluate(node => ({scroll: node.scrollWidth,
        client: node.clientWidth, page: document.documentElement.scrollWidth, width: innerWidth}));
      assert.ok(bounds.scroll <= bounds.client + 1 && bounds.page <= bounds.width + 1, `${theme}/${width}: content fits`);
      assert.deepEqual(writes, []);
      geometries.push({theme, width});
    }
    // A clear suggestion has no explanatory rule panel; financial checks and
    // original previews remain available for the user's confirmation.
    candidates[0].ambiguous = false; candidates[0].warnings = [];
    board.suggestions = [candidates[0]];
    await page.locator('#matchingRefresh').click();
    await page.locator('[data-synthesis-id="shop-matched"] .synthesis-row-preview').click();
    assert.equal(await page.locator('#matchingSuggestionReason,.synthesis-row-reason,.matching-recognition-note,#matchingAIStatus').count(), 0);
    assert.equal(await page.locator('#matchingAmount').inputValue(), '17.00');
    assert.equal(await page.locator('#matchingDifference').innerText(), '¥ 0.00');
    assert.equal(await page.locator('#matchingPreviewContent .matching-proof[href]').count(), 2);
    assert.match(await page.locator('[data-voucher-key="q:order"]').innerText(), /4006123456789012345/);
    assert.equal(await page.locator('#matchingSave').isEnabled(), true);
    assert.deepEqual(writes, []);
    await page.screenshot({path: path.join(evidence, 'synthetic-shop-dark-mobile.png')});
    assert.deepEqual(errors, []);
    const result = {passed: true, routineMatchingReasonsHidden: true, noTimeComparisonsOrWarnings: true,
      sameShopNoHistoricalFalseConflict: true, distinctBranchStillNeedsReview: true,
      genuineLocalAiConflictRetainedAndEscaped: true, competingChoices: 2, geometries,
      businessWrites: writes.length, pageErrors: errors};
    await fs.writeFile(path.join(evidence, 'platform-shop-matching-browser.json'), JSON.stringify(result, null, 2));
    return result;
  } finally {await browser.close();}
}
module.exports = run;
if (require.main === module) run(require('playwright')).then(console.log).catch(error => {console.error(error); process.exitCode = 1;});
