// Railway payment proofs are visible as archived originals, never selectable.
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
async function run(pw) {
  const browser = await pw.chromium.launch({headless: true});
  const page = await browser.newPage({viewport: {width: 1450, height: 1000}});
  page.setDefaultTimeout(10000);
  const origin = 'http://127.0.0.1:18792', errors = [], writes = [];
  const reason = '高铁报销只需12306发票，支付截图无需使用；原件已保留。';
  const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j1ioAAAAASUVORK5CYII=', 'base64');
  const unused = [35700, 40300, 2500, 2500].map((amount, index) => ({id: 'rail-' + index, key: 'q:rail-' + index,
    project_id: 'p1', name: `synthetic-rail-${index}.png`, role: 'payment', status: 'unused', revision: 2,
    amount_cents: amount, unused_reason: reason, result: reason, source: 'upload', issues: ['商品不明'],
    fields: {amount: (amount / 100).toFixed(2), goods: '12306消费', category: '其他'},
    thumbnail: '/intake-file/rail-' + index, url: '/intake-file/rail-' + index}));
  const retail = {id: 'retail', key: 'q:retail', project_id: 'p1', name: 'synthetic-logistics.png',
    role: 'payment', status: 'review', revision: 1, amount_cents: 2500, issues: [], source: 'upload', result: '识别完成',
    fields: {merchant: '中铁物流', category: '其他', goods: '货运物流'}, thumbnail: '/intake-file/retail', url: '/intake-file/retail'};
  const state = {app_mode: 'invoice', token: 'synthetic', projects: [{id: 'p1', name: '合成铁路项目'}], items: [], inbox: [],
    categories: ['高铁', '其他'], roles: {invoice: '发票', payment: '付款截图'}, stages: {}, category_auto_ready: false};
  const board = {vouchers: [], payments: [retail], unused, other: [], matches: [], suggestions: [], report_types: [], supplement_candidates: [], ai: {available: true}};
  page.on('pageerror', error => errors.push(error.message));
  await page.route(origin + '/**', async route => {
    const req = route.request(), url = new URL(req.url());
    if (req.method() === 'POST') {writes.push(url.pathname); throw Error('Unexpected write from archived-proof view');}
    const json = body => route.fulfill({contentType: 'application/json', body: JSON.stringify(body)});
    if (url.pathname === '/api/state') return json(state);
    if (url.pathname === '/api/material-matching') return json(board);
    if (url.pathname === '/api/intake') return json({queue: [...unused, retail]});
    if (url.pathname.startsWith('/intake-file/')) return route.fulfill({contentType: 'image/png', body: png});
    const name = url.pathname === '/' ? 'index.html' : url.pathname.slice(1);
    if (!/^[a-z-]+\.(html|js|css)$/.test(name)) return route.abort();
    return route.fulfill({contentType: name.endsWith('.js') ? 'text/javascript' : name.endsWith('.css') ? 'text/css' : 'text/html',
      body: await fs.readFile(path.resolve(__dirname, '../src/invoice_print_layout/web', name))});
  });
  try {
    await page.goto(origin + '/?project=p1');
    assert.equal(await page.locator('#recognizeReceipt,#checkMail').count(), 0,
      'Archived originals must not restore duplicate main-page intake entries');
    await page.locator('#materialMatching').click();
    await page.locator('#matchingPaymentCount').filter({hasText: '1 份'}).waitFor();
    assert.equal(await page.locator('[data-payment-id]').count(), 1, 'unrelated logistics payment remains available');
    assert.equal(await page.locator('#matchingUnusedCount').innerText(), '4 份');
    await page.locator('#matchingUnusedSection > summary').click();
    assert.equal(await page.locator('#matchingUnused a').count(), 4);
    assert.equal(await page.locator('#matchingUnused [data-material-action="delete"]').count(), 4, 'archived railway proofs can be deleted');
    assert.equal(await page.locator('#matchingUnused [data-material-action="restore"]').count(), 0, 'railway proofs cannot return to active pool');
    assert.equal(await page.locator('#matchingUnused [data-prepare-id],#matchingUnused [data-classify-id],#matchingUnused [data-payment-id],[data-assist-id]').count(), 0, 'archived railway proofs have no select/edit/AI actions');
    await page.locator('#matchingClose').click();
    // The internal archive filter is retained for older callers; the visible collection entry is the matching desk.
    await page.evaluate(() => window.openUnifiedIntake());
    await page.locator('#intakeFilter').selectOption('归档');
    assert.equal(await page.locator('#intakeRows .mailrow').count(), 4);
    assert.equal(await page.locator('#intakeRows button').count(), 0);
    assert.match(await page.locator('#intakeSummary').innerText(), /归档 4.*待处理 1/);
    assert.deepEqual(writes, []); assert.deepEqual(errors, []);
    return {unusedOriginalsReadable: 4, notSelectable: true, unrelatedPaymentRetained: true, unusedExcludedFromPending: true, expenseWrites: 0};
  } finally {await browser.close();}
}
module.exports = run;
if (require.main === module) run(require('playwright')).then(console.log).catch(error => {console.error(error); process.exitCode = 1;});
