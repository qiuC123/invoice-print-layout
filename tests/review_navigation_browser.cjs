// Synthetic ledger only: issue filters, report scope, and explicitly initiated continuous review.
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');

async function run(pw) {
  const browser = await pw.chromium.launch({headless: true});
  const page = await browser.newPage({viewport: {width: 1440, height: 1000}});
  page.setDefaultTimeout(10000);
  const origin = 'http://127.0.0.1:18843';
  const errors = [], transitions = [], previewReads = [], reportScopes = [], unexpectedWrites = [];
  const people = [{id: 'alice', label: '测试甲', project_id: 'p1'}, {id: 'bob', label: '测试乙', project_id: 'p1'}];
  const invoice = id => ({id: id + '-invoice', role: 'invoice', name: id + '.pdf', available: true});
  const fee = (id, extra = {}) => ({
    id, title: '测试 ' + id, project_id: 'p1', project: '合成测试项目', category: '高铁',
    reimbursement_person_id: 'alice', reimbursement_person_label: '测试甲',
    stage: 'draft', verified: false, ready: false, complete: true, missing: [],
    amount_cents: 1000, amount: '10.00', version: id + '-v1', expense_date: '2026-10-01',
    merchant: '测试商家', order_number: '', attachments: [invoice(id)], events: [], ...extra,
  });
  let items = [], failTransition = false, failState = false, heldTransition = null, transitionGate = null;
  let holdNextState = false, heldState = null, stateGate = null;
  const defer = () => {let resolve; const promise = new Promise(done => {resolve = done;}); return {promise, resolve};};
  const state = () => ({
    app_mode: 'invoice', token: 'synthetic-review', report_types:[{code:'materials',label:'材料采购',editable_name:true,names:[]}], projects: [{id: 'p1', name: '合成测试项目'}, {id: 'p2', name: '另一测试项目'}],
    items, inbox: [], categories: ['高铁', '打车', '材料采购', '其他'], roles: {invoice: '发票', trip: '行程单', payment: '付款截图'},
    stages: {draft: '待核对', submitted: '已制包'}, category_auto_ready: false, report_template_ready: true,
  });
  page.on('pageerror', error => errors.push(error.message));
  await page.route(origin + '/**', async route => {
    const request = route.request(), url = new URL(request.url());
    const json = (data, status = 200) => route.fulfill({status, contentType: 'application/json', body: JSON.stringify(data)});
    if (url.pathname === '/api/state') {
      if (holdNextState) {holdNextState = false; heldState.resolve(); await stateGate.promise;}
      if (failState) {failState = false; return json({error: 'synthetic state read failed'}, 503);}
      return json(state());
    }
    if (url.pathname === '/api/reimbursement-people') return json({project_id: 'p1', people, summary: {rows: [], total_cents: 0, expense_count: 0}});
    if (url.pathname === '/api/report-preview') {reportScopes.push(request.postDataJSON().ids); return json({combined_pdf:'synthetic-combined.pdf',combined_preview_pages:['synthetic-table.svg'],combined_page_sizes:[{width:842,height:595}]});}
    if (url.pathname.startsWith('/export/')) return route.fulfill({contentType:'image/svg+xml',body:'<svg xmlns="http://www.w3.org/2000/svg" width="842" height="595"><rect width="842" height="595" fill="white"/></svg>'});
    if (url.pathname === '/api/expense-print-preview') {
      const body = request.postDataJSON(), item = items.find(x => x.id === body.id);
      previewReads.push(body); assert.ok(item); assert.equal(body.expected_version, item.version);
      return json({id: item.id, version: item.version, complete: item.complete, missing: item.missing, preview_pages: [], page_count: 0});
    }
    if (url.pathname === '/api/transition') {
      const body = request.postDataJSON(); transitions.push(body);
      if (transitionGate) {heldTransition.resolve(); await transitionGate.promise; transitionGate = null;}
      if (failTransition) {failTransition = false; return json({error: 'synthetic verification failed'}, 409);}
      const item = items.find(x => x.id === body.id);
      assert.ok(item); assert.equal(body.action, 'verify'); assert.equal(body.expected_version, item.version);
      assert.equal(item.verified, false, 'a committed expense must not be verified a second time');
      item.verified = true; item.ready = true; item.version += '-reviewed';
      return json(item);
    }
    if (request.method() === 'POST') {unexpectedWrites.push({path: url.pathname, body: request.postDataJSON()}); return json({error: 'unexpected write'}, 400);}
    if (url.pathname.startsWith('/api/')) return json({queue: [], vouchers: [], payments: [], suggestions: [], matches: [], groups: [], archived: []});
    const file = url.pathname === '/' ? 'index.html' : url.pathname.slice(1);
    if (!/^[a-z-]+\.(html|js|css)$/.test(file)) return route.abort();
    return route.fulfill({contentType: file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html', body: await fs.readFile(path.resolve(__dirname, '../src/invoice_print_layout/web', file))});
  });
  const visibleIds = () => page.locator('#list [data-item]').evaluateAll(nodes => nodes.map(node => node.dataset.item));
  const activeId = () => page.locator('#list [data-item].selected').getAttribute('data-item');
  const show = async id => {await page.locator(`[data-item="${id}"] .title`).click(); await page.locator('#detail h2').filter({hasText: '测试 ' + id}).waitFor();};
  const settled = () => page.waitForFunction(() => !singleVerifyBusy);
  const reset = async values => {
    items = values; failTransition = false; failState = false; transitionGate = null; holdNextState = false;
    await page.goto(origin + '/?project=p1'); await page.locator('#issueFilter').waitFor({state:'attached'});
    await page.waitForFunction(() => document.querySelector('#personFilter option[value="alice"]'));
    await page.locator('#tabs [data-view="review"]').click();
  };
  const issue = value => page.locator('#issueFilter').selectOption(value,{force:true});
  const assertIds = async ids => assert.deepEqual(await visibleIds(), ids);
  const beginHeldVerify = async () => {
    heldTransition = defer(); transitionGate = defer();
    await page.locator('#verifyNext').click(); await heldTransition.promise;
  };
  try {
    const balanced = {voucher_count: 1, voucher_total_cents: 1000, payment_count: 1, payment_total_cents: 1000, alignment_status: 'balanced'};
    await reset([
      fee('missing', {complete: false, missing: ['发票'], attachments: []}),
      fee('difference', {material_match: {...balanced, payment_total_cents: 900, alignment_status: 'mismatch'}}),
      fee('balanced', {material_match: balanced}),
      fee('unknown', {amount_cents: 0, amount: '0.00', amount_pending: true, complete: false, missing: ['金额待确认'], material_match: {...balanced, voucher_total_cents: null, payment_total_cents: null, alignment_status: 'unknown'}}),
      fee('unknown-missing', {amount_pending: true, complete: false, missing: ['金额待确认', '发票'], attachments: [], material_match: {...balanced, voucher_total_cents: null, alignment_status: 'unknown'}}),
      fee('unknown-total', {material_match: {...balanced, voucher_total_cents: null, payment_total_cents: null, alignment_status: 'unconfirmed'}}),
      fee('taxi', {category: '打车', material_basis: 'invoice_payment', attachments: [invoice('taxi'), {id: 'taxi-trip', role: 'trip', name: '另一平台行程单.pdf', available: true}], material_match: {voucher_count: 1, voucher_total_cents: 1000, payment_count: 0, payment_total_cents: 0, alignment_status: 'partial'}}),
      fee('taxi-optional-payment', {category: '打车', material_basis: 'invoice_payment', attachments: [invoice('taxi-optional-payment'), {id: 'optional-trip', role: 'trip', name: 'trip.pdf', available: true}, {id: 'optional-pay', role: 'payment', name: 'payment.png', available: true}], material_match: {...balanced, payment_total_cents: 900}}),
      fee('bob-missing', {reimbursement_person_id: 'bob', reimbursement_person_label: '测试乙', complete: false, missing: ['发票']}),
      fee('material-missing', {category: '材料采购', complete: false, missing: ['付款截图']}),
      fee('other-project', {project_id: 'p2', project: '另一测试项目', complete: false, missing: ['发票']}),
    ]);
    const filterBaseline = JSON.stringify(items);
    await issue('missing'); await assertIds(['missing', 'unknown-missing', 'bob-missing', 'material-missing']);
    await page.locator('#personFilter').selectOption('alice',{force:true}); await page.locator('#categoryFilter').selectOption('高铁',{force:true});
    await assertIds(['missing', 'unknown-missing']);
    await page.locator('#search').fill('unknown'); await assertIds(['unknown-missing']);
    await page.locator('#search').fill(''); await issue('amount'); await assertIds(['difference']);
    await issue('complete'); await assertIds(['difference', 'balanced', 'unknown', 'unknown-total']);
    await page.locator('#categoryFilter').selectOption('打车',{force:true}); await assertIds(['taxi', 'taxi-optional-payment']);
    await show('taxi'); assert.match(await page.locator('#detail').innerText(), /发票＋行程单齐全/);
    assert.doesNotMatch(await page.locator('#detail').innerText(), /还缺：付款截图/);
    await issue('missing'); await assertIds([]); await issue('amount'); await assertIds([]);
    await page.locator('#categoryFilter').selectOption('高铁',{force:true}); await assertIds(['difference']);
    await page.locator('#previewReport').click(); await page.frameLocator('#previewFrame').locator('.print-page img').waitFor();
    assert.deepEqual(reportScopes.at(-1), ['difference'], 'unselected report preview must honor the issue/person/category intersection');
    await page.locator('#closePreview').click();
    await issue('complete'); await page.locator('[data-check="balanced"]').check(); await issue('missing');
    await page.locator('#previewReport').click(); await page.frameLocator('#previewFrame').locator('.print-page img').waitFor();
    assert.deepEqual(reportScopes.at(-1), ['balanced'], 'an explicitly checked hidden expense retains the existing report selection contract');
    await page.locator('#closePreview').click();
    assert.equal(await page.locator('#tabs [data-view="synthesis"]').count(), 0, 'material combinations are no longer a main expense stage');
    await page.locator('#tabs [data-view="submitted"]').click(); await issue('amount');
    assert.equal(await page.locator('#tabs [data-view="submitted"]').getAttribute('class'), 'active', 'issue filters retain the current expense stage'); await assertIds([]);
    await page.locator('#tabs [data-view="deleted"]').click(); await issue('missing');
    assert.equal(await page.locator('#tabs [data-view="review"]').getAttribute('class'), 'active');
    for (const theme of ['light', 'dark']) {
      await page.locator('#themeSelect').selectOption(theme);
      assert.equal(await page.locator('html').getAttribute('data-theme'), theme);
      await page.setViewportSize({width: 390, height: 844});
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true, `${theme}: issue filter must not overflow a narrow screen`);
      await page.setViewportSize({width: 1440, height: 1000});
    }
    assert.equal(JSON.stringify(items), filterBaseline); assert.equal(transitions.length, 0);

    // Keep the same filtered queue and list position, including an incomplete next expense.
    const queue = Array.from({length: 35}, (_, index) => fee('queue-' + String(index).padStart(2, '0')));
    queue[16].complete = false; queue[16].missing = ['行程单'];
    await reset([fee('excluded-person', {reimbursement_person_id: 'bob'}), ...queue, fee('excluded-category', {category: '材料采购'})]);
    await page.locator('#personFilter').selectOption('alice',{force:true}); await page.locator('#categoryFilter').selectOption('高铁',{force:true});
    await page.locator('#search').fill('queue'); await show('queue-15');
    await page.locator('[data-check="queue-15"]').check();
    const beforeScroll = await page.locator('#list').evaluate(node => node.scrollTop);
    assert.ok(beforeScroll > 0, 'fixture must exercise a scrolled list');
    await beginHeldVerify();
    assert.equal(await page.locator('[data-action="verify"]').isDisabled(), true);
    assert.equal(await page.locator('#verifySelected').isDisabled(), true);
    // A second event from either entry cannot add a second write while the first is pending.
    await page.evaluate(() => {document.querySelector('#verifyNext').onclick(); document.querySelector('[data-action="verify"]').click();});
    assert.equal(transitions.length, 1); transitionGate.resolve(); await settled();
    assert.equal(await activeId(), 'queue-16'); assert.equal(await page.locator('#verifyNext').count(), 1);
    assert.match(await page.locator('#detail').innerText(), /还缺：行程单/);
    assert.equal(await page.locator('#tabs [data-view="review"]').getAttribute('class'), 'active');
    assert.equal(await page.locator('#personFilter').inputValue(), 'alice'); assert.equal(await page.locator('#categoryFilter').inputValue(), '高铁');
    assert.equal(await page.locator('#search').inputValue(), 'queue');
    const afterScroll = await page.locator('#list').evaluate(node => node.scrollTop);
    assert.ok(Math.abs(afterScroll - beforeScroll) <= 2, 'review should not reset the list to its top');
    const visibility = await page.locator('[data-item="queue-16"]').evaluate(row => {const r = row.getBoundingClientRect(), list = row.closest('#list').getBoundingClientRect(); return {top: r.top, bottom: r.bottom, start: list.top, end: list.bottom};});
    assert.ok(visibility.top >= visibility.start - 1 && visibility.bottom <= visibility.end + 1, 'the next expense should remain visible');
    assert.deepEqual(transitions[0], {id: 'queue-15', action: 'verify', expected_version: 'queue-15-v1'});
    assert.equal(items.find(x => x.id === 'queue-16').verified, false); assert.equal(items.filter(x => x.verified).length, 1);

    // A rejected verification keeps the current expense and its input scope; retry writes it once.
    await reset([fee('failed'), fee('after-failed')]); await show('failed'); failTransition = true;
    let count = transitions.length; await page.locator('#verifyNext').click(); await settled();
    assert.equal(transitions.length, count + 1); assert.equal(await activeId(), 'failed');
    assert.match(await page.locator('#error').innerText(), /synthetic verification failed/);
    assert.equal(items[0].verified, false); assert.equal(await page.locator('#verifyNext').isEnabled(), true);
    await page.locator('#verifyNext').click(); await settled(); assert.equal(await activeId(), 'after-failed');
    assert.equal(items.filter(x => x.verified).length, 1);

    // Once the server acknowledged the write, a failed refresh must not offer to repeat it.
    await reset([fee('committed'), fee('after-commit')]); await show('committed'); count = transitions.length; failState = true;
    await page.locator('#verifyNext').click(); await settled();
    assert.equal(transitions.length, count + 1); assert.equal(items[0].verified, true);
    assert.match(await page.locator('#error').innerText(), /已核对成功.*刷新失败.*无需再次核对/s);
    assert.equal(await page.locator('#verifyNext,[data-action="verify"]').count(), 0);
    await page.locator('[data-item="after-commit"] .title').click();
    assert.equal(transitions.length, count + 1, 'opening the remaining expense only reads its print preview');

    // A late success must not steal a manual row selection or a changed filter/stage.
    await reset([fee('slow'), fee('manual'), fee('next')]); await show('slow'); await beginHeldVerify();
    await show('manual'); transitionGate.resolve(); await settled(); assert.equal(await activeId(), 'manual');
    await reset([fee('slow-filter'), fee('bob-target', {reimbursement_person_id: 'bob'})]); await show('slow-filter'); await beginHeldVerify();
    await page.locator('#personFilter').selectOption('bob',{force:true}); await show('bob-target'); transitionGate.resolve(); await settled();
    assert.equal(await page.locator('#personFilter').inputValue(), 'bob'); assert.equal(await activeId(), 'bob-target');
    await reset([fee('slow-stage'), fee('ready', {verified: true, ready: true})]); await show('slow-stage'); await beginHeldVerify();
    await page.locator('#tabs [data-view="submitted"]').click(); await show('ready'); transitionGate.resolve(); await settled();
    assert.equal(await page.locator('#tabs [data-view="submitted"]').getAttribute('class'), 'active'); assert.equal(await activeId(), 'ready');

    // Also protect changes made after the write succeeded while its follow-up state read is pending.
    await reset([fee('read-delay'), fee('read-manual')]); await show('read-delay');
    heldState = defer(); stateGate = defer(); holdNextState = true;
    await page.locator('#verifyNext').click(); await heldState.promise; await show('read-manual');
    stateGate.resolve(); await settled(); assert.equal(await activeId(), 'read-manual');

    // The final item completes only this scope and leaves other people's expenses untouched.
    await reset([fee('last'), fee('outside', {reimbursement_person_id: 'bob'})]);
    await page.locator('#personFilter').selectOption('alice',{force:true}); await issue('complete'); await show('last');
    count = transitions.length; await page.locator('#verifyNext').click(); await settled();
    assert.equal(transitions.length, count + 1); assert.equal(items[1].verified, false);
    assert.equal(await page.locator('#personFilter').inputValue(), 'alice'); assert.equal(await page.locator('#issueFilter').inputValue(), 'complete');
    assert.match(await page.locator('#detail').innerText(), /当前筛选范围已核对完/);
    assert.equal(await page.locator('#tabs [data-view="review"]').getAttribute('class'), 'active'); await assertIds([]);

    // Keyboard actions use the same bounded review queue and confirmation path.
    await reset([fee('key-a'),fee('key-b',{complete:false,missing:['行程单']}),fee('key-c')]);
    const press=async key=>{await page.locator('#list').focus();await page.keyboard.press(key);};
    await press('j');assert.equal(await activeId(),'key-a');
    await press('ArrowDown');assert.equal(await activeId(),'key-b');
    await press('k');assert.equal(await activeId(),'key-a');
    await press('j');await press('ArrowUp');assert.equal(await activeId(),'key-a');
    count=transitions.length;
    await page.locator('#search').focus();await page.keyboard.press('j');assert.equal(transitions.length,count);
    await page.locator('#search').fill('');assert.equal(await activeId(),'key-a');
    await page.evaluate(()=>document.getElementById('personFilter').dispatchEvent(new KeyboardEvent('keydown',{key:'j',bubbles:true})));assert.equal(await activeId(),'key-a');
    await press('?');await page.locator('#reviewShortcuts[open]').waitFor();
    await page.keyboard.press('j');assert.equal(await activeId(),'key-a');await page.locator('#reviewShortcuts button').click();
    await press('e');await page.locator('#reviewEditDialog[open]').waitFor();
    await page.evaluate(()=>{const text=document.createElement('textarea');document.querySelector('#reviewEditDialog').append(text);text.focus();});await page.keyboard.press('j');assert.equal(transitions.length,count);
    await page.locator('#reviewEditDialog [data-close]').click();
    await press('Enter');await settled();assert.equal(await activeId(),'key-b');assert.equal(transitions.length,count+1);
    await press('Enter');await page.locator('#verifyDialog[open]').waitFor();assert.equal(transitions.length,count+1);
    await page.keyboard.press('j');assert.equal(await activeId(),'key-b');
    await page.locator('#cancelVerify').click();await settled();assert.equal(items[1].verified,false);
    await press('Enter');await page.locator('#confirmVerify').click();await settled();
    assert.equal(await activeId(),'key-c');assert.equal(transitions.at(-1).acknowledge_missing,true);
    await page.locator('#tabs [data-view="submitted"]').click();count=transitions.length;
    await press('Enter');assert.equal(transitions.length,count,'shortcuts only run in review');

    // The original explicit destination remains available without automatic next-item verification.
    await reset([fee('original-action'), fee('original-next')]); await show('original-action'); count = transitions.length;
    await page.locator('[data-action="verify"]').click(); await settled();
    assert.equal(transitions.length, count + 1); assert.equal(items[1].verified, false);
    assert.equal(await page.locator('#tabs [data-view="submitted"]').getAttribute('class'), 'active');
    assert.equal(await page.locator('#list [data-item].selected').count(), 0, 'The reviewed fee does not retain preview selection in its new stage');
    assert.equal(await page.locator('[data-check="original-action"]').isChecked(), false);
    assert.deepEqual(unexpectedWrites, []); assert.deepEqual(errors, []); assert.ok(previewReads.length > 0);
    return {passed: true, transitions: transitions.length, scope: 'problem/person/category/search intersection; taxi and unknown amounts; read-only preview and hidden checks; explicit versioned single review, duplicate lock, failure/retry, committed refresh failure, incomplete next, scoped completion, scroll preservation, late-response isolation, old destination, light/dark narrow layout'};
  } finally {
    transitionGate?.resolve(); stateGate?.resolve(); await browser.close();
  }
}
module.exports = run;
if (require.main === module) run(require('playwright')).then(console.log).catch(error => {console.error(error); process.exitCode = 1;});
