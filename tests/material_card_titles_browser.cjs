// Synthetic-only material titles: filenames remain transport data, not visible labels.
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');

async function run(pw) {
  const browser = await pw.chromium.launch({headless: true});
  const page = await browser.newPage({viewport: {width: 1500, height: 1000}});
  page.setDefaultTimeout(10000);
  const origin = 'http://127.0.0.1:18883', writes = [], errors = [];
  const evidence = process.env.INVOICE_BROWSER_EVIDENCE ? path.resolve(process.env.INVOICE_BROWSER_EVIDENCE) :
    path.resolve(__dirname, '../workspace/invoice-operations/material-card-dedup-20261016');
  const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j1ioAAAAASUVORK5CYII=', 'base64');
  const shop = '合成便利店（东门店）', owner = '合成人员甲';
  const filename = id => 'synthetic_' + id + '_0123456789abcdef0123456789abcdef0123456789abcdef.png';
  const material = (id, role, fields = {}, extra = {}) => ({id, key: 'q:' + id, project_id: 'p1',
    role, status: 'review', revision: 3, name: filename(id), amount_cents: 1700,
    fields: {title: '合成购买用途', category: '其他', ...fields},
    report_choice: {project_type: 'other', project_name: '合成用途'},
    reimbursement_person_id: 'person1', reimbursement_person_label: owner,
    thumbnail: '/intake-file/' + id + '/thumbnail.png', url: '/intake-file/' + id + '/' + filename(id),
    issues: [], ...extra});
  const actorFields = {shopping_platform: '淘宝闪购', payment_channel: '支付宝'};
  const first = material('same-a', 'invoice', {...actorFields, merchant: '淘宝闪购', shop_name: shop, goods: shop});
  const second = material('same-b', 'receipt', {...actorFields, merchant: shop, shop_name: shop});
  const escapedTitle = '<img data-title-injected="yes"> 合成 & "便利店"';
  const vouchers = [first, second,
    material('legacy', 'receipt', {merchant: '合成旧店铺'}),
    material('invoice', 'invoice', {...actorFields, merchant: '淘宝闪购'}),
    material('receipt', 'receipt', {...actorFields, merchant: '支付宝'}),
    material('order', 'receipt', {...actorFields, merchant: '淘宝闪购'}, {source_role: 'order'}),
    material('purchase', 'receipt', {merchant: '微信支付', payment_channel: '微信支付'}, {source_role: 'purchase'}),
    material('escaped', 'invoice', {shop_name: escapedTitle})];
  const normalizedShop = '合成 便利店 ( 东门店 )', otherBranch = '合成便利店（西门店）';
  const distinctPurpose = '矿泉水和工作餐', shopWithSuffix = shop + '自营超市';
  const payments = [material('payment', 'payment', {...actorFields, merchant: '支付宝'}),
    material('payment-shop', 'payment', {...actorFields, merchant: '淘宝闪购', shop_name: shop, goods: shop}),
    material('payment-normalized', 'payment', {...actorFields, merchant: '淘宝闪购', shop_name: shop, goods: normalizedShop}),
    material('payment-purpose', 'payment', {...actorFields, merchant: '淘宝闪购', shop_name: shop, goods: distinctPurpose}),
    material('payment-branch', 'payment', {...actorFields, merchant: '淘宝闪购', shop_name: shop, goods: otherBranch},
      {issues: ['店铺分店不同，请核对原件。']}),
    material('payment-suffix', 'payment', {...actorFields, merchant: '淘宝闪购', shop_name: shop, goods: shopWithSuffix})];
  const other = ['trip', 'trip_sheet', 'detail', 'other'].map(role => material(role, role));
  other.push(material('qr', 'other', {}, {qr_status: 'manual', kind: 'invoice_qr'}));
  const archived = [material('archived', 'receipt', {merchant: '合成归档店'}, {status: 'archived', title: filename('archived')})];
  const deleted = [material('deleted', 'invoice', {shop_name: '合成删除店'}, {status: 'deleted', title: filename('deleted')})];
  const board = {vouchers, payments, other, archived, deleted, matches: [],
    suggestions: [{id: 'title-suggestion', project_id: 'p1', snapshot_token: 'synthetic-title-snapshot',
      voucher_keys: [first.key], payment_ids: ['payment-shop'], revisions: {'same-a': 3, 'payment-shop': 3}, expected_versions: {},
      amount_cents: 1700, ambiguous: false, needs_name_review: false, warnings: [], reason: ''}],
    supplement_candidates: [], report_types: [{code: 'other', label: '其他', editable_name: true, names: []}]};
  const state = {app_mode: 'invoice', token: 'synthetic-title-token', projects: [{id: 'p1', name: '合成标题项目'}],
    items: [], inbox: [], categories: ['其他'], roles: {invoice: '发票', receipt: '收据', payment: '付款截图'},
    stages: {draft: '待核对'}, category_auto_ready: false};
  const people = {project_id: 'p1', people: [{id: 'person1', project_id: 'p1', label: owner, state: 'active'}], summary: {rows: []}};
  page.on('pageerror', error => errors.push(error.message));
  await page.route(origin + '/**', async route => {
    const request = route.request(), url = new URL(request.url());
    // Any accidental business mutation fails the test before it can be served.
    if (!['GET', 'HEAD'].includes(request.method())) {
      writes.push({method: request.method(), path: url.pathname});
      await route.fulfill({status: 403, contentType: 'application/json', body: '{"error":"read-only fixture"}'});
      return;
    }
    const json = value => route.fulfill({contentType: 'application/json', body: JSON.stringify(value)});
    if (url.pathname === '/api/state') return json(state);
    if (url.pathname === '/api/material-matching') return json(board);
    if (url.pathname === '/api/intake') return json({queue: []});
    if (url.pathname === '/api/reimbursement-people') return json(people);
    if (url.pathname.startsWith('/intake-file/')) return route.fulfill({contentType: 'image/png', body: png});
    const file = url.pathname === '/' ? 'index.html' : url.pathname.slice(1);
    if (!/^[a-z-]+\.(html|js|css)$/.test(file)) return route.abort();
    return route.fulfill({contentType: file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html',
      body: await fs.readFile(path.resolve(__dirname, '../src/invoice_print_layout/web', file))});
  });
  const card = id => page.locator(`[data-voucher-key="q:${id}"], [data-payment-id="${id}"], [data-support-id="${id}"]`);
  const expectCard = async (id, title) => {
    const target = card(id);
    assert.equal(await target.locator('strong').first().innerText(), title, id + ' title');
    assert.equal(await target.locator('img').getAttribute('alt'), '', id + ' thumbnail defers to the adjacent title');
    assert.match(await target.innerText(), /¥ 17\.00/);
    assert.match(await target.innerText(), new RegExp('费用归属人：' + owner));
  };
  try {
    await fs.mkdir(evidence, {recursive: true});
    await page.goto(origin + '/?project=p1');
    await page.waitForFunction(() => typeof window.openMaterialMatching === 'function' && !!state.token);
    // Keep a reference to the exact read-only board used by both renderers, so
    // hiding a label cannot accidentally erase fields needed by later actions.
    await page.evaluate(() => {
      const original = window.setSynthesisBoard;
      window.setSynthesisBoard = (value, ...args) => {
        window.__materialTitleFixtureBoard = value;
        window.__materialTitleFixtureBefore = JSON.stringify(value);
        return original(value, ...args);
      };
    });
    await page.locator('#materialMatching').click();
    await page.locator('[data-voucher-key="q:same-a"]').waitFor();
    assert.equal(await page.evaluate(() => typeof window.materialMatchingTitle), 'function');
    await expectCard('same-a', shop); await expectCard('same-b', shop);
    await expectCard('legacy', '合成旧店铺');
    await expectCard('invoice', '发票'); await expectCard('receipt', '收据');
    await expectCard('order', '购买记录'); await expectCard('purchase', '购买记录');
    await expectCard('escaped', escapedTitle);
    await expectCard('payment', '付款截图'); await expectCard('payment-shop', shop);
    for (const id of ['payment-normalized', 'payment-purpose', 'payment-branch', 'payment-suffix']) await expectCard(id, shop);
    for (const id of ['same-a', 'same-b', 'payment-shop', 'payment-normalized', 'payment-purpose', 'payment-branch', 'payment-suffix']) {
      assert.doesNotMatch(await card(id).innerText(), /实际店铺：/, id + ' has no second copy of its shop heading');
    }
    for (const id of ['payment-shop', 'payment-normalized']) {
      const text = await card(id).innerText();
      assert.doesNotMatch(text, /用途：/, id + ' does not repeat its shop as a payment purpose');
      assert.match(text, /购物平台：淘宝闪购/); assert.match(text, /支付渠道：支付宝/);
    }
    assert.ok((await card('payment-purpose').innerText()).includes('用途：' + distinctPurpose));
    assert.ok((await card('payment-branch').innerText()).includes('用途：' + otherBranch));
    assert.ok((await card('payment-suffix').innerText()).includes('用途：' + shopWithSuffix));
    assert.match(await card('payment-branch').innerText(), /店铺分店不同，请核对原件/);
    assert.ok((await card('same-a').innerText()).includes('用途：' + shop), 'voucher purpose is not a payment-only duplicate');
    for (const [id, title] of [['trip', '行程单'], ['trip_sheet', '行程单'], ['detail', '明细'], ['other', '其他材料']]) {
      await expectCard(id, title);
    }
    assert.equal(await page.locator('[data-title-injected]').count(), 0, 'shop names are text, never executable markup');
    const qr = page.locator('#matchingOther .matching-other-card').filter({has: page.locator('[data-prepare-id="qr"]')});
    assert.equal(await qr.locator('strong').innerText(), '领票二维码');
    assert.equal(await qr.locator('img').getAttribute('alt'), '领票二维码');
    assert.match(await qr.textContent(), /¥ 17\.00/);
    assert.match(await qr.textContent(), new RegExp('费用归属人：' + owner));
    for (const [id, title] of [['archived', '合成归档店'], ['deleted', '合成删除店']]) {
      const target = page.locator(`[data-archived-key="q:${id}"]`);
      assert.equal(await target.locator('strong').textContent(), title);
      assert.equal(await target.locator('img').getAttribute('alt'), title);
      assert.match(await target.textContent(), /¥ 17\.00/);
      assert.match(await target.textContent(), new RegExp('费用归属人：' + owner));
      assert.equal(await target.locator('.matching-proof').getAttribute('href'),
        '/intake-file/' + id + '/' + filename(id), 'archived original path preserved');
    }
    const geometries = [];
    for (const theme of ['light', 'dark']) for (const width of [1500, 390]) {
      await page.locator('#themeSelect').selectOption(theme, {force: true});
      await page.setViewportSize({width, height: 1000});
      // Identical visible titles and amounts must never become selection keys.
      await card('same-a').click();
      assert.equal(await card('same-a').getAttribute('aria-pressed'), 'true');
      assert.equal(await card('same-b').getAttribute('aria-pressed'), 'false');
      await card('same-b').click();
      assert.equal(await card('same-a').getAttribute('aria-pressed'), 'true');
      assert.equal(await card('same-b').getAttribute('aria-pressed'), 'true');
      assert.equal(await page.locator('#matchingSelectedVoucherCount').innerText(), '2');
      assert.match(await page.locator('#matchingVoucherTotal').innerText(), /34\.00/);
      const figures = page.locator('#matchingPreviewContent .matching-voucher-proofs figure');
      assert.equal(await figures.count(), 2);
      for (const [index, value] of [first, second].entries()) {
        assert.match(await figures.nth(index).locator('figcaption').innerText(), new RegExp(shop.replace(/[（）]/g, '\\$&')));
        assert.equal(await figures.nth(index).locator('img').getAttribute('alt'), shop);
        assert.equal(await figures.nth(index).locator('a').getAttribute('href'), value.url);
      }
      await card('same-a').click();
      assert.equal(await card('same-a').getAttribute('aria-pressed'), 'false');
      assert.equal(await card('same-b').getAttribute('aria-pressed'), 'true');
      assert.equal(await page.locator('#matchingSelectedVoucherCount').innerText(), '1');
      await card('same-b').click();
      assert.equal(await card('same-b').getAttribute('aria-pressed'), 'false');
      await card('payment').click();
      assert.match(await page.locator('.matching-payment-proofs figcaption').innerText(), /付款截图.*17\.00/);
      assert.equal(await page.locator('.matching-payment-proofs a').getAttribute('href'), payments[0].url);
      await card('payment').click();
      for (const value of payments.slice(1)) {
        await card(value.id).click();
        const summary = await page.locator('#matchingPreviewContent .matching-transaction-summary').innerText();
        assert.ok(summary.includes('实际店铺：' + shop), 'original preview keeps the actual shop field');
        assert.match(summary, /购物平台：淘宝闪购/); assert.match(summary, /支付渠道：支付宝/);
        assert.equal(await page.locator('.matching-payment-proofs a').getAttribute('href'), value.url);
        assert.ok((await page.locator('.matching-payment-proofs figcaption').innerText()).startsWith(shop));
        assert.equal(await card(value.id).getAttribute('aria-pressed'), 'true');
        await card(value.id).click();
        assert.equal(await card(value.id).getAttribute('aria-pressed'), 'false');
      }
      await card('trip_sheet').click();
      assert.equal(await page.locator('.matching-support-proofs figcaption').innerText(), '行程单');
      assert.equal(await page.locator('.matching-support-proofs a').getAttribute('href'), other[1].url);
      await card('trip_sheet').click();
      const fullText = await page.locator('#matchingDeskDialog').textContent();
      assert.doesNotMatch(fullText, /synthetic_[a-z_-]+_0123456789abcdef/);
      for (const alt of await page.locator('#matchingDeskDialog img').evaluateAll(nodes => nodes.map(node => node.alt))) {
        assert.doesNotMatch(alt, /synthetic_|0123456789abcdef/);
      }
      const bounds = await page.locator('#matchingDeskDialog').evaluate(node => ({scroll: node.scrollWidth,
        client: node.clientWidth, page: document.documentElement.scrollWidth, width: innerWidth}));
      assert.ok(bounds.scroll <= bounds.client + 1 && bounds.page <= bounds.width + 1, `${theme}/${width}: content fits`);
      assert.deepEqual(writes, []);
      assert.equal(await page.evaluate(() => JSON.stringify(window.__materialTitleFixtureBoard) ===
        window.__materialTitleFixtureBefore), true, 'rendering and selecting preserve every original board field');
      geometries.push({theme, width, ...bounds});
    }
    // The same semantic title is used in the read-only, per-group confirmation preview.
    await page.locator('[data-synthesis-check="title-suggestion"]').check();
    await page.locator('#confirmSelectedSynthesis').click();
    await page.locator('#synthesisBatchDialog').waitFor();
    const batchProofs = page.locator('#synthesisBatchDialog .synthesis-proof');
    assert.equal(await batchProofs.count(), 2);
    for (const [index, value] of [first, payments[1]].entries()) {
      assert.ok((await batchProofs.nth(index).locator('figcaption').innerText()).startsWith(shop));
      assert.equal(await batchProofs.nth(index).locator('img').getAttribute('alt'), shop);
      assert.equal(await batchProofs.nth(index).locator('a').getAttribute('href'), value.url);
    }
    assert.doesNotMatch(await page.locator('#synthesisBatchDialog').textContent(), /synthetic_[a-z_-]+_0123456789abcdef/);
    assert.deepEqual(writes, []); assert.deepEqual(errors, []);
    await page.locator('#synthesisBatchDialog [data-batch-close]').first().click();
    await page.screenshot({path: path.join(evidence, 'synthetic-material-titles-dark-mobile.png')});
    const result = {passed: true, shopNameBeforePlatform: true, legacyMerchantFallback: true,
      typedFallbacks: ['发票', '收据', '购买记录', '付款截图', '行程单', '明细', '其他材料', '领票二维码'],
      filenamesHiddenFromCardsAltsAndCaptions: true, originalUrlsPreserved: true, amountAndOwnerPreserved: true,
      distinctSameTitleSelections: true, escapedShopMarkup: true, archivedAndDeletedCovered: true,
      batchPreviewCovered: true, repeatedShopLabelsHidden: true, matchingPaymentShopPurposeHidden: true,
      normalizedBracketsAndWhitespaceCovered: true, differentPurposeBranchAndSuffixPreserved: true,
      voucherPurposePreserved: true, previewActualShopPreserved: true, sourceFieldsUnchanged: true,
      geometries, businessWrites: writes.length, pageErrors: errors};
    await fs.writeFile(path.join(evidence, 'material-card-titles-browser.json'), JSON.stringify(result, null, 2));
    return result;
  } finally {await browser.close();}
}
module.exports = run;
if (require.main === module) run(require('playwright')).then(console.log).catch(error => {console.error(error); process.exitCode = 1;});
