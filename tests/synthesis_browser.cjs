// Actual UI with synthetic responses; confirmation is the only expense write.
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
async function run(pw) {
  const browser = await pw.chromium.launch({headless: true});
  const page = await browser.newPage({viewport: {width: 1500, height: 1000}});
  page.setDefaultTimeout(10000);
  const origin = 'http://127.0.0.1:18793', errors = [], writes = [];
  const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j1ioAAAAASUVORK5CYII=', 'base64');
  const state = {app_mode: 'invoice', token: 'synthetic', projects: [{id: 'p1', name: '合成测试项目'}], items: [], inbox: [],
    categories: ['酒店', '其他'], roles: {invoice: '发票', payment: '付款截图'}, stages: {draft: '待核对', submitted: '已提交'}, category_auto_ready: false};
  const card = (id, cents, role) => ({id, key: 'q:' + id, project_id: 'p1', name: id + '.png', revision: 1, status: 'review',
    role, amount_cents: cents, fields: {merchant: role === 'invoice' ? '测试区合成宾馆' : '合成旅行平台', goods: '合成宾馆', category: '酒店'},
    report_choice: {project_type: 'life', project_name: '住宿费（附清单）'}, thumbnail: '/intake-file/' + id, url: '/intake-file/' + id, issues: []});
  const voucher = card('v1', 135300, 'invoice'), payments = [92100, 12400, 15400, 15400].map((amount, index) => card('p' + index, amount, 'payment'));
  const suggestion = {id: 's:hotel', project_id: 'p1', voucher_keys: ['q:v1'], payment_ids: payments.map(value => value.id),
    amount_cents: 135300, reason: '金额合计一致；商家名称需人工核对', needs_name_review: true, ambiguous: false,
    warnings: ['请确认平台展示名和开票名称对应'], snapshot_token: 'snapshot-1'};
  const board = {vouchers: [voucher], payments, other: [], matches: [], supplement_candidates: [], suggestions: [suggestion],
    report_types: [{code: 'life', label: '生活费', editable_name: false, names: ['住宿费（附清单）']}], ai: {available: false}};
  let stale = false;
  page.on('pageerror', error => errors.push(error.message));
  await page.route(origin + '/**', async route => {
    const req = route.request(), url = new URL(req.url());
    const json = (body, status = 200) => route.fulfill({status, contentType: 'application/json', body: JSON.stringify(body)});
    if (url.pathname === '/api/state') return json(state);
    if (url.pathname === '/api/intake') return json({queue: []});
    if (url.pathname === '/api/material-matching') return json(stale ? {...board, suggestions: [{...suggestion, snapshot_token: 'snapshot-2'}]} : board);
    if (req.method() === 'POST') {
      const body = req.postDataJSON(); writes.push({route: url.pathname, body});
      if (url.pathname === '/api/material-matching/create') {
        assert.equal(body.suggestion_id, suggestion.id); assert.equal(body.suggestion_token, 'snapshot-2');
        assert.deepEqual(body.payment_ids, payments.map(value => value.id)); assert.deepEqual(body.voucher_keys, ['q:v1']);
        assert.equal(body.fields.amount, '1353.00'); assert.equal(body.fields.project_name, '住宿费（附清单）');
        const item = {id: 'created', title: '合成住宿', project_id: 'p1', project: '合成测试项目', category: '酒店', merchant: '合成宾馆',
          amount_cents: 135300, stage: 'draft', verified: false, ready: false, complete: true, attachments: [], events: [], missing: [], version: 'created-v1'};
        state.items.push(item); board.suggestions = []; board.vouchers = []; board.payments = []; stale = false;
        return json({item});
      }
      if (url.pathname === '/api/expense-print-preview') {
        const item = state.items[0]; return json({id: item.id, version: item.version, complete: true, missing: [], partial: false,
          pdf: 'synthetic.pdf', preview_pages: ['synthetic.png'], page_count: 1, layout: 'A4'});
      }
      if (url.pathname === '/api/transition') {
        assert.equal(body.action, 'verify'); Object.assign(state.items[0], {verified: true, ready: true, version: 'created-v2'});
        return json(state.items[0]);
      }
      throw Error('Unexpected write ' + url.pathname);
    }
    if (url.pathname.startsWith('/intake-file/') || url.pathname.startsWith('/export/')) return route.fulfill({contentType: 'image/png', body: png});
    const name = url.pathname === '/' ? 'index.html' : url.pathname.slice(1);
    if (!/^[a-z-]+\.(html|js|css)$/.test(name)) return route.abort();
    return route.fulfill({contentType: name.endsWith('.js') ? 'text/javascript' : name.endsWith('.css') ? 'text/css' : 'text/html',
      body: await fs.readFile(path.resolve(__dirname, '../src/invoice_print_layout/web', name))});
  });
  try {
    await page.goto(origin + '/?project=p1'); await page.waitForFunction(() => !!state.token);
    assert.equal(await page.locator('#tabs [data-view="synthesis"],#metrics [data-view="synthesis"]').count(), 0, 'The material desk is the only suggestion workspace');
    await page.locator('#materialMatching').click();
    await page.locator('#matchingSuggestions [data-synthesis-id]').waitFor();
    assert.match(await page.locator('[data-synthesis-id]').innerText(), /名称待核对/);
    await page.locator('[data-synthesis-id] .synthesis-row-preview').click();
    assert.equal(await page.locator('#matchingPreviewContent .matching-voucher-proofs figure,#matchingPreviewContent .matching-payment-proofs figure').count(), 5);
    assert.ok(await page.locator('#matchingPreviewContent .matching-voucher-proof img').first().evaluate(node => node.getBoundingClientRect().width > 400), 'preview fills the material review pane');
    assert.match(await page.locator('#matchingSuggestionReason').innerText(), /商家名称待核对.*请确认平台展示名和开票名称对应/s);
    assert.equal(await page.locator('#detail .synthesis-proof,[data-open-synthesis]').count(), 0, 'Candidate inspection stays inside the desk');
    assert.equal(writes.length, 0, 'viewing candidates cannot write expenses');
    await page.setViewportSize({width: 390, height: 844});
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), 'candidate preview must fit a narrow screen');
    await page.setViewportSize({width: 1500, height: 1000});
    await page.locator('#matchingClose').click();
    stale = true;
    await page.evaluate(suggestion => window.openMaterialMatching({suggestion}), suggestion);
    await page.waitForFunction(() => /版本.*已变化/.test(document.getElementById('matchingMessage').textContent));
    assert.equal(await page.locator('.matching-card.selected').count(), 0, 'stale choice must not silently select refreshed materials');
    assert.equal(writes.length, 0);
    assert.equal(await page.locator('#refreshSynthesis,#openSynthesisDesk').count(), 0);
    await page.locator('[data-synthesis-id] .synthesis-row-preview').click();
    await page.waitForFunction(() => document.querySelectorAll('.matching-card.selected').length === 5);
    assert.equal(await page.locator('#matchingAmount').inputValue(), '1353.00');
    await page.locator('#matchingSave').click();
    await page.locator('#matchingDeskDialog[open]').waitFor({state: 'detached'});
    assert.equal(writes.filter(value => value.route.endsWith('/create')).length, 1);
    assert.equal(await page.locator('#tabs [data-view="review"]').getAttribute('class'), 'active');
    assert.equal(await page.evaluate(() => active), 'created');
    assert.deepEqual(await page.evaluate(() => [...checked]), []);
    await page.locator('[data-action="verify"]').click();
    await page.waitForFunction(() => document.querySelector('#tabs [data-view="submitted"]').classList.contains('active'));
    assert.equal(await page.evaluate(() => active), null, 'Successful verification clears the old detail selection');
    assert.deepEqual(await page.evaluate(() => [...checked]), []);
    await page.locator('[data-item="created"]').click();
    await page.locator('#expensePrintPages img').waitFor();
    assert.equal(await page.locator('#tabs [data-view="reimbursed"]').count(), 0);
    assert.deepEqual(errors, []);
    return {candidateReadOnly: true, oneToFourPreselection: true, narrowScreenFits: true, staleCandidateRejected: true, confirmationToReview: true, verificationToSubmissionPreview: true};
  } finally {await browser.close();}
}
module.exports = run;
if (require.main === module) run(require('playwright')).then(console.log).catch(error => {console.error(error); process.exitCode = 1;});
