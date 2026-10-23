// Explicit missing-material acknowledgement; real frontend and synthetic ledger only.
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');

async function run(pw) {
  const browser = await pw.chromium.launch({headless: true});
  const page = await browser.newPage({viewport: {width: 1440, height: 1100}});
  page.setDefaultTimeout(10000);
  const origin = 'http://127.0.0.1:18863';
  const errors = [], transitions = [], previewReads = [], unexpectedWrites = [];
  const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j1ioAAAAASUVORK5CYII=', 'base64');
  const defer = () => {let resolve; const promise = new Promise(done => {resolve = done;}); return {promise, resolve};};
  const fee = (id, extra = {}) => ({id, title: 'Synthetic ' + id, project_id: 'p1', project: 'Synthetic project',
    category: '打车', amount_cents: 1800, amount: '18.00', stage: 'draft', complete: false, verified: false, ready: false,
    missing: ['发票', '行程单'], version: id + '-v1', material_basis: 'invoice_payment',
    merchant: 'Synthetic merchant', expense_date: '2026-10-15', order_number: '', note: '', followup_date: '',
    invoice_state: 'not_requested', events: [], attachments: [{id: 'proof-' + id, name: id + '.pdf', role: 'payment', available: true}], ...extra});
  const baseline = () => [fee('taxi-one'), fee('taxi-two'),
    fee('dining', {category: '餐饮', amount_cents: 1450, amount: '14.50', missing: ['发票或收据']}),
    fee('complete', {title: 'Complete synthetic expense', category: '高铁', amount_cents: 1000, amount: '10.00', complete: true, missing: [],
      material_basis: 'standard', attachments: [{id: 'proof-complete', name: 'complete.pdf', role: 'invoice', available: true}]})];
  let items = [], failId = '', heldId = '', started = null, gate = null;
  const state = () => ({app_mode: 'invoice', token: 'synthetic', items, inbox: [], projects: [{id: 'p1', name: 'Synthetic project'}],
    categories: ['打车', '餐饮', '高铁', '酒店', '材料采购', '外卖', '其他', '顺丰'],
    roles: {invoice: '发票', receipt: '收据', trip: '行程单', payment: '付款截图'},
    stages: {draft: '待核对', submitted: '已提交'}, category_auto_ready: false, report_template_ready: true});
  page.on('pageerror', error => errors.push(error.message));
  await page.route(origin + '/**', async route => {
    const request = route.request(), url = new URL(request.url());
    const json = (value, status = 200) => route.fulfill({status, contentType: 'application/json', body: JSON.stringify(value)});
    if (url.pathname === '/api/state') return json(state());
    if (url.pathname === '/api/reimbursement-people') return json({project_id: 'p1', people: [], summary: {rows: [], total_cents: 0, expense_count: 0}});
    if (url.pathname === '/api/expense-print-preview') {
      const body = request.postDataJSON(), item = items.find(value => value.id === body.id);
      previewReads.push(body); assert.ok(item); assert.equal(body.expected_version, item.version);
      return json({id: item.id, version: item.version, complete: item.complete, partial: !item.complete, missing: item.missing,
        pdf: item.id + '.pdf', preview_pages: [item.id + '.png'], page_count: 1,
        layout: item.complete ? 'Synthetic A4' : '仅已收材料预览（非正式报销包）'});
    }
    if (url.pathname === '/api/transition') {
      const body = request.postDataJSON(); transitions.push(body);
      if (body.id === heldId && gate) {started.resolve(); await gate.promise; gate = null;}
      const item = items.find(value => value.id === body.id);
      assert.ok(item); assert.equal(body.action, 'verify');
      if (body.expected_version !== item.version || body.acknowledge_missing && JSON.stringify(body.expected_missing) !== JSON.stringify(item.missing)) {
        return json({error: '费用或缺项已变化，请重新确认'}, 409);
      }
      if (failId === item.id) {failId = ''; return json({error: 'Synthetic temporary failure'}, 503);}
      assert.equal(item.verified, false, 'One acknowledgement must not verify an expense twice');
      if (!item.complete) {
        assert.equal(body.acknowledge_missing, true, 'Missing material must never be silently accepted');
        assert.deepEqual(body.expected_missing, item.missing);
      } else {
        assert.equal(Object.hasOwn(body, 'acknowledge_missing'), false);
        assert.equal(Object.hasOwn(body, 'expected_missing'), false);
      }
      item.verified = true; item.ready = true; item.version += '-reviewed';
      if (!item.complete) item.missing_acknowledged = true;
      return json(item);
    }
    if (request.method() === 'POST') {
      unexpectedWrites.push({path: url.pathname, body: request.postDataJSON()});
      return json({error: 'Unexpected business write'}, 400);
    }
    if (url.pathname.startsWith('/api/')) return json({queue: [], vouchers: [], payments: [], other: [], matches: [], suggestions: []});
    if (url.pathname.startsWith('/export/')) return route.fulfill({contentType: 'image/png', body: png});
    const name = url.pathname === '/' ? 'index.html' : url.pathname.slice(1);
    if (!/^[a-z-]+\.(html|js|css)$/.test(name)) return route.abort();
    return route.fulfill({contentType: name.endsWith('.js') ? 'text/javascript' : name.endsWith('.css') ? 'text/css' : 'text/html',
      body: await fs.readFile(path.resolve(__dirname, '../src/invoice_print_layout/web', name))});
  });
  const row = id => page.locator(`#list [data-item="${id}"]`);
  const active = () => page.evaluate(() => active);
  const settled = () => page.waitForFunction(() => !singleVerifyBusy && !batchVerifyBusy);
  const reset = async (values = baseline()) => {
    items = values; failId = ''; heldId = ''; gate = null;
    await page.goto(origin + '/?project=p1'); await row(items[0].id).waitFor();
  };
  const show = async id => {if (await active() !== id) await row(id).locator('.title').click(); await page.locator('#detail h2').waitFor();};
  const confirmMissing = async (id, next = false) => {
    await show(id); await page.locator(next ? '#verifyNext' : '#detail [data-action="verify"]').click();
    await page.locator('#verifyDialog[open]').waitFor();
    assert.match(await page.locator('#confirmVerify').innerText(), /仍然核对完成/);
  };
  const expectedBody = item => ({id: item.id, action: 'verify', expected_version: item.id + '-v1',
    acknowledge_missing: true, expected_missing: item.missing});
  try {
    // Each of the three real missing-material shapes has an explicit review path.
    await reset();
    for (const id of ['taxi-one', 'taxi-two', 'dining']) {
      await show(id);
      assert.equal(await page.locator('#verifyNext').count(), 1);
      assert.equal(await page.locator('#detail [data-action="verify"]').count(), 1);
    }
    for (const cancel of ['button', 'escape', 'close']) {
      await confirmMissing('taxi-one', true);
      const message = await page.locator('#verifyScope').innerText();
      assert.match(message, /Synthetic taxi-one/); assert.match(message, /18\.00/);
      assert.match(message, /发票/); assert.match(message, /行程单/);
      await page.evaluate(() => document.querySelector('#verifyNext').onclick());
      assert.equal(await page.locator('#verifyDialog[open]').count(), 1);
      if (cancel === 'button') await page.locator('#cancelVerify').click();
      else if (cancel === 'escape') await page.keyboard.press('Escape');
      else await page.locator('#verifyDialog').getByRole('button', {name: '关闭', exact: true}).click();
      await settled(); assert.equal(await active(), 'taxi-one'); assert.deepEqual(transitions, []);
    }

    // Display source strings literally, including malicious-looking missing labels.
    const unsafe = '<img data-missing-injection src=x onerror="window.missingInjected=true">';
    await reset([fee('unsafe', {title: unsafe, category: '餐饮', missing: ['发票或收据', unsafe]})]);
    await confirmMissing('unsafe');
    assert.ok((await page.locator('#verifyScope').innerText()).includes(unsafe));
    assert.equal(await page.locator('#verifyDialog [data-missing-injection]').count(), 0);
    assert.equal(await page.evaluate(() => window.missingInjected), undefined);
    await page.locator('#cancelVerify').click(); await settled(); assert.deepEqual(transitions, []);

    // Missing approval is versioned and explicit; a double event cannot write twice.
    await reset(); const firstMissing = [...items[0].missing];
    await confirmMissing('taxi-one'); heldId = 'taxi-one'; started = defer(); gate = defer();
    await page.locator('#confirmVerify').click(); await started.promise;
    assert.deepEqual(transitions.at(-1), expectedBody(items[0]));
    const singleCount = transitions.length;
    await page.evaluate(() => {document.querySelector('#verifyNext').onclick(); document.querySelector('[data-action="verify"]').click();});
    assert.equal(transitions.length, singleCount); gate.resolve(); await settled();
    assert.equal(await page.locator('#tabs [data-view=submitted]').getAttribute('class'), 'active');
    assert.equal(await active(), null, 'A completed fee enters its next stage without retaining preview focus');
    assert.equal(await row('taxi-one').locator('[data-check]').isChecked(), false);
    assert.equal(items[0].complete, false); assert.equal(items[0].missing_acknowledged, true);
    assert.deepEqual(items[0].missing, firstMissing);
    assert.match(await row('taxi-one').locator('.badge').innerText(), /已核对.*已确认缺材料/);
    await show('taxi-one');
    assert.match(await page.locator('#detail .eyebrow').innerText(), /已收材料/);
    await page.waitForFunction(() => document.querySelector('#expensePrintStatus')?.textContent.includes('仅已收材料'));
    assert.match(await page.locator('#expensePrintStatus').innerText(), /仅已收材料.*发票.*行程单/);
    assert.equal(await page.locator('#makeExpenseReport').isEnabled(), true);

    // A complete expense keeps its original API body and requires no acknowledgement dialog.
    await reset(); await show('complete'); const completeCount = transitions.length;
    await page.locator('#detail [data-action="verify"]').click(); await settled();
    assert.equal(await page.locator('#verifyDialog[open]').count(), 0);
    assert.deepEqual(transitions[completeCount], {id: 'complete', action: 'verify', expected_version: 'complete-v1'});

    // Continuous review acknowledges only the current expense and opens its next neighbour.
    await reset(); await confirmMissing('taxi-two', true);
    await page.locator('#confirmVerify').click(); await settled();
    assert.equal(await active(), 'dining');
    assert.equal(items.find(value => value.id === 'dining').verified, false);
    assert.equal(items.find(value => value.id === 'taxi-one').verified, false);

    // Transient API failure retains the current expense and requires a new explicit approval on retry.
    await reset(); await confirmMissing('taxi-one', true); failId = 'taxi-one';
    await page.locator('#confirmVerify').click(); await settled();
    assert.equal(await active(), 'taxi-one'); assert.equal(items[0].verified, false);
    assert.match(await page.locator('#error').innerText(), /Synthetic temporary failure/);
    const retryCount = transitions.length;
    await page.locator('#verifyNext').click(); await page.locator('#verifyDialog[open]').waitFor();
    assert.equal(transitions.length, retryCount);
    await page.locator('#confirmVerify').click(); await settled(); assert.equal(await active(), 'taxi-two');

    // A stale confirmation cannot reuse an older version or older missing-material list.
    await reset(); await confirmMissing('taxi-one'); const staleCount = transitions.length;
    items[0].version = 'taxi-one-v2'; items[0].missing = ['行程单'];
    await page.evaluate(() => load()); await page.locator('#confirmVerify').click(); await settled();
    assert.equal(items[0].verified, false);
    assert.equal(transitions.length, staleCount, 'Refresh while a dialog is open invalidates its old approval');
    assert.match(await page.locator('#error').innerText(), /变化|重新/);

    // The existing single batch confirmation lists every missing expense, including hidden checks.
    await reset([...baseline(), fee('pending', {amount_cents: 0, amount: '0.00', amount_pending: true, missing: ['金额待确认']}),
      fee('unknown-category', {category: '未知类别', missing: ['确认此类别的材料要求（请选择具体类别）'],
        missing_acknowledgement_allowed: false})]);
    await page.locator('#selectVisible').check(); await page.locator('#search').fill('Complete');
    await page.locator('#verifySelected').click(); await page.locator('#verifyDialog[open]').waitFor();
    const batchMessage = await page.locator('#verifyScope').innerText();
    for (const token of ['Synthetic taxi-one', 'Synthetic taxi-two', 'Synthetic dining', '发票', '行程单', '发票或收据', '14.50']) assert.ok(batchMessage.includes(token));
    assert.match(batchMessage, /不在当前列表中的 3 笔/);
    assert.match(batchMessage, /跳过 2 笔/);
    const batchCount = transitions.length;
    await page.locator('#confirmVerify').click(); await settled();
    const batchWrites = transitions.slice(batchCount);
    assert.equal(batchWrites.length, 4);
    assert.deepEqual(batchWrites.map(body => body.id), ['taxi-one', 'taxi-two', 'dining', 'complete']);
    assert.ok(items.slice(0, 4).every(value => value.verified && value.ready));
    assert.ok(items.slice(4).every(value => !value.verified));
    assert.ok(items.slice(0, 3).every(value => !value.complete && value.missing_acknowledged));
    assert.match(await page.locator('#notice').innerText(), /成功 4 笔，跳过 2 笔，失败 0 笔/);

    // A delayed acknowledged write must not steal back manually cleared preview focus.
    await reset(); await confirmMissing('taxi-one', true); heldId = 'taxi-one'; started = defer(); gate = defer();
    await page.locator('#confirmVerify').click(); await started.promise;
    await row('taxi-one').locator('.title').click(); assert.equal(await active(), null);
    gate.resolve(); await settled(); assert.equal(await active(), null);
    assert.match(await page.locator('#detail').innerText(), /选择一笔费用/);
    assert.equal(items[1].verified, false);
    assert.deepEqual(unexpectedWrites, []); assert.deepEqual(errors, []); assert.ok(previewReads.length > 0);
    return {passed: true, transitions: transitions.length,
      scope: 'three missing-material expenses; explicit versioned ack, cancellation/Escape/x, XSS text, duplicate lock, partial preview, complete request compatibility, next/failure/stale/batch/hidden scope/manual deselection'};
  } finally {
    gate?.resolve(); await browser.close();
  }
}

module.exports = run;
if (require.main === module) run(require('playwright')).then(console.log).catch(error => {console.error(error); process.exitCode = 1;});
