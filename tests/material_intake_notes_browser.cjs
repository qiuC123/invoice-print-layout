// Synthetic receipt/mail intake comments use ordinary card text and never reveal sources.
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');

async function run(pw) {
  const browser = await pw.chromium.launch({headless: true});
  const page = await browser.newPage({viewport: {width: 1500, height: 1000}});
  page.setDefaultTimeout(10000);
  const origin = 'http://127.0.0.1:18891', errors = [], writes = [];
  const evidence = process.env.INVOICE_BROWSER_EVIDENCE || path.resolve(__dirname, '../.tmp-notes-browser');
  const photoNote = '和合成甲方吃饭 <img data-note-injected="yes"> & 核对', mailNote = '合成邮件主题备注';
  const card = (id, role, intake_note = '', extra = {}) => ({id, key: 'q:' + id, project_id: 'p1',
    role, status: 'review', revision: 1, name: 'synthetic-' + id + '.pdf', amount_cents: 1400,
    fields: {category: '餐饮', amount: '14.00'}, report_choice: {project_type: 'other', project_name: '合成用途'},
    url: '/intake-file/' + id, thumbnail: '', intake_note, issues: [], ...extra});
  const board = {vouchers: [card('photo', 'receipt', photoNote), card('mail', 'invoice', mailNote),
    card('empty', 'receipt', '', {fields: {category: '餐饮', note: '费用备注不得充当收件备注'}})],
    payments: [card('payment', 'payment', '合成付款收件备注')],
    other: [card('detail', 'detail', '合成明细备注'), card('qr', 'other', '合成待领票备注', {qr_status: 'manual'})],
    archived: [card('archived', 'receipt', '合成归档备注', {status: 'archived'})],
    deleted: [card('deleted', 'receipt', '合成已删除备注', {status: 'deleted'})],
    matches: [], suggestions: [], supplement_candidates: [],
    report_types: [{code: 'other', label: '其他', editable_name: true, names: []}]};
  const state = {app_mode: 'invoice', token: 'synthetic-notes-token',
    projects: [{id: 'p1', name: '合成收件备注项目'}], items: [], inbox: [], categories: ['餐饮', '其他'],
    roles: {invoice: '发票', receipt: '收据', payment: '付款截图'}, stages: {draft: '待核对'}, category_auto_ready: false};
  page.on('pageerror', error => errors.push(error.message));
  await page.route(origin + '/**', async route => {
    const request = route.request(), url = new URL(request.url());
    if (!['GET', 'HEAD'].includes(request.method())) {
      writes.push(url.pathname);
      return route.fulfill({status: 403, contentType: 'application/json', body: '{"error":"read-only fixture"}'});
    }
    const json = value => route.fulfill({contentType: 'application/json', body: JSON.stringify(value)});
    if (url.pathname === '/api/state') return json(state);
    if (url.pathname === '/api/material-matching') return json(board);
    if (url.pathname === '/api/intake') return json({queue: []});
    if (url.pathname === '/api/reimbursement-people') return json({project_id: 'p1', people: [], summary: {rows: []}});
    if (url.pathname.startsWith('/intake-page/')) return route.fulfill({status: 204, body: ''});
    const file = url.pathname === '/' ? 'index.html' : url.pathname.slice(1);
    if (!/^[a-z-]+\.(html|js|css)$/.test(file)) return route.abort();
    return route.fulfill({contentType: file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html',
      body: await fs.readFile(path.resolve(__dirname, '../src/invoice_print_layout/web', file))});
  });
  try {
    await fs.mkdir(evidence, {recursive: true});
    await page.goto(origin + '/?project=p1');
    await page.waitForFunction(() => typeof window.openMaterialMatching === 'function' && !!state.token);
    await page.locator('#materialMatching').click();
    const photo = page.locator('[data-voucher-key="q:photo"]');
    await photo.waitFor();
    assert.equal(await photo.locator('.matching-intake-note').innerText(), '备注：' + photoNote);
    assert.equal(await photo.locator('.matching-intake-note').getAttribute('title'), photoNote);
    assert.equal(await photo.locator('.matching-intake-note').evaluate(node => getComputedStyle(node).whiteSpace), 'nowrap');
    assert.equal(await page.locator('[data-voucher-key="q:mail"] .matching-intake-note').innerText(), '备注：' + mailNote);
    assert.equal(await page.locator('[data-voucher-key="q:empty"] .matching-intake-note').count(), 0);
    assert.equal(await page.locator('[data-note-injected]').count(), 0);
    assert.equal(await page.locator('[data-payment-id="payment"] .matching-intake-note').innerText(), '备注：合成付款收件备注');
    assert.equal(await page.locator('[data-support-id="detail"] .matching-intake-note').innerText(), '备注：合成明细备注');
    assert.equal(await page.locator('#matchingOther .matching-other-card .matching-intake-note').innerText(), '备注：合成待领票备注');
    assert.equal(await page.locator('[data-archived-key="q:archived"] .matching-intake-note').textContent(), '备注：合成归档备注');
    assert.equal(await page.locator('[data-archived-key="q:deleted"] .matching-intake-note').textContent(), '备注：合成已删除备注');
    assert.doesNotMatch(await page.locator('#matchingDeskDialog').innerText(), /来自手机|来自邮箱|收票 App|来源：|费用备注不得充当收件备注/);
    assert.match(await photo.innerText(), /14\.00/);
    await photo.click();
    assert.equal(await photo.getAttribute('aria-pressed'), 'true');
    assert.equal(await photo.locator('.matching-intake-note').innerText(), '备注：' + photoNote);
    for (const theme of ['light', 'dark']) {
      await page.locator('#themeSelect').selectOption(theme, {force: true});
      await page.screenshot({path: path.join(evidence, 'material-intake-notes-' + theme + '.png')});
    }
    assert.deepEqual(writes, []); assert.deepEqual(errors, []);
    const result = {passed: true, photoAndMailNotes: true, emptyNoteHidden: true, escapedMarkup: true,
      paymentSupportQrAndArchivedNotes: true, noSourceLabels: true, expenseNoteSeparate: true};
    await fs.writeFile(path.join(evidence, 'material-intake-notes-browser.json'), JSON.stringify(result, null, 2));
    return result;
  } finally {await browser.close();}
}
module.exports = run;
if (require.main === module) run(require('playwright')).then(console.log).catch(error => {console.error(error); process.exitCode = 1;});
