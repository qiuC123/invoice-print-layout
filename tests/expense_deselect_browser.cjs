// Real frontend, synthetic HTTP responses only; no live service or business writes.
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');

async function run(pw) {
  const browser = await pw.chromium.launch({headless: true});
  const context = await browser.newContext({viewport: {width: 1440, height: 1200}});
  const page = await context.newPage();
  page.setDefaultTimeout(10000);
  const origin = 'http://127.0.0.1:18862';
  const errors = [], writes = [], previewReads = [], requests = [];
  const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j1ioAAAAASUVORK5CYII=', 'base64');
  const item = (id, fields = {}) => ({id, title: `Synthetic ${id}`, project: 'Synthetic project', project_id: 'p1',
    category: '酒店', amount: '1.23', amount_cents: 123, expense_date: '2026-10-15',
    merchant: 'Synthetic merchant', order_number: '', note: '', followup_date: '', invoice_state: 'received',
    stage: 'draft', verified: false, ready: false, complete: true, missing: [], events: [],
    reimbursement_person_id: 'personA', reimbursement_person_label: 'Person A', version: id + '-v1',
    attachments: [{id: 'proof-' + id, name: id + '.pdf', role: 'invoice', available: true}], ...fields});
  const state = {app_mode: 'invoice', token: 'synthetic', items: [item('A'), item('B'), item('SLOW')], inbox: [],
    projects: [{id: 'p1', name: 'Synthetic project'}, {id: 'p2', name: 'Other project'}],
    categories: ['酒店', '餐饮'], roles: {invoice: '发票', payment: '付款截图', other: '待分类'},
    stages: {draft: '待核对', submitted: '已提交'}, category_auto_ready: false, report_template_ready: true,
    report_types: [{code: 'hotel', label: '住宿', names: ['住宿费'], editable_name: false}]};
  let releaseSlow;
  const slowGate = new Promise(resolve => {releaseSlow = resolve;});
  page.on('pageerror', error => errors.push(error.message));
  await context.route(origin + '/**', async route => {
    const request = route.request(), url = new URL(request.url());
    requests.push(url.pathname);
    const json = (value, status = 200) => route.fulfill({status, contentType: 'application/json', body: JSON.stringify(value)});
    if (request.method() === 'POST') {
      const body = request.postDataJSON();
      if (url.pathname !== '/api/expense-print-preview') {
        writes.push({path: url.pathname, body});
        return json({error: 'This read-only interaction test forbids business writes'}, 400);
      }
      previewReads.push(body);
      const expense = state.items.find(value => value.id === body.id);
      assert.ok(expense); assert.equal(body.expected_version, expense.version);
      if (expense.id === 'SLOW') await slowGate;
      return json({id: expense.id, version: expense.version, complete: true, partial: false, missing: [],
        pdf: expense.id + '.pdf', preview_pages: [expense.id + '.png'], page_count: 1, layout: 'Synthetic A4'});
    }
    if (url.pathname === '/api/state') return json(state);
    if (url.pathname === '/api/reimbursement-people') return json({project_id: 'p1',
      people: [{id: 'personA', label: 'Person A', state: 'active', project_id: 'p1', version: 'person-v1'}],
      summary: {rows: [], total_cents: 369, expense_count: 3}});
    if (url.pathname === '/api/material-matching') return json({vouchers: [], payments: [], other: [], matches: [], suggestions: []});
    if (url.pathname.startsWith('/api/')) return json({queue: [], rules: [], learned: [], sections: {}});
    if (url.pathname.startsWith('/export/') || url.pathname.startsWith('/file/')) return route.fulfill({contentType: 'image/png', body: png});
    const name = url.pathname === '/' ? 'index.html' : url.pathname.slice(1);
    if (!/^[a-z-]+\.(html|js|css)$/.test(name)) return route.abort();
    return route.fulfill({contentType: name.endsWith('.js') ? 'text/javascript' : name.endsWith('.css') ? 'text/css' : 'text/html',
      body: await fs.readFile(path.resolve(__dirname, '../src/invoice_print_layout/web', name))});
  });
  const row = id => page.locator(`#list [data-item="${id}"]`);
  const active = () => page.evaluate(() => active);
  const checked = () => page.evaluate(() => [...checked].sort());
  const select = async id => {
    await row(id).locator('.title').click();
    await page.waitForFunction(expected => active === expected, id);
    if (id !== 'SLOW') await page.locator('#expensePrintPages img').waitFor();
  };
  const empty = async () => {
    assert.equal(await active(), null);
    assert.equal(await page.locator('#list [data-item].selected').count(), 0);
    assert.equal(await page.locator('#expensePrintPages').count(), 0);
    assert.match(await page.locator('#detail').innerText(), /选择一笔费用/);
  };
  const ownBackground = async selector => {
    const point = await page.locator(selector).evaluate(node => {
      const rect = node.getBoundingClientRect();
      const xs = [rect.left + 3, rect.right - 3, rect.left + rect.width / 2];
      const ys = [rect.top + 3, rect.bottom - 3, rect.top + rect.height / 2];
      for (let x = Math.max(0, rect.left) + 4; x < Math.min(innerWidth, rect.right) - 2; x += 13) xs.push(x);
      for (let y = Math.max(0, rect.top) + 4; y < Math.min(innerHeight, rect.bottom) - 2; y += 13) ys.push(y);
      for (const y of ys) for (const x of xs) {
        if (x > 0 && x < innerWidth && y > 0 && y < innerHeight && document.elementFromPoint(x, y) === node) return {x, y};
      }
      return null;
    });
    assert.ok(point, 'Expected an actual empty background point in ' + selector);
    await page.mouse.click(point.x, point.y);
  };
  try {
    await page.goto(origin + '/?project=p1');
    await row('A').waitFor();
    await page.locator('#personFilter option[value="personA"]').waitFor({state: 'attached'});
    await select('A');
    await page.locator('[data-check="A"]').check();
    await page.locator('[data-check="B"]').check();
    assert.equal(await active(), 'A'); assert.deepEqual(await checked(), ['A', 'B']);
    const originalReads = previewReads.length;
    await row('A').locator('.title').click(); await empty();
    assert.deepEqual(await checked(), ['A', 'B']); assert.equal(previewReads.length, originalReads);
    await select('A');
    assert.equal(previewReads.length, originalReads, 'Reselecting an unchanged expense reuses its request');
    await select('B'); assert.equal(await active(), 'B');
    await row('B').locator('.money').click({position: {x: 10, y: 10}}); await empty();
    await select('A');

    // Neither checkbox nor its select-all label changes the focused preview.
    const retained = await page.locator('#expensePrintPages img').elementHandle();
    await page.locator('[data-check="A"]').uncheck();
    await page.locator('#selectionTools label').click();
    await page.locator('#selectVisible').uncheck();
    assert.equal(await active(), 'A');
    assert.equal(await retained.evaluate(node => node === document.querySelector('#expensePrintPages img')), true);
    await page.locator('[data-check="B"]').check();
    for (const mode of ['dark', 'light']) {
      await page.locator('#themeSelect').selectOption(mode);
      assert.equal(await active(), 'A');
      for (const selector of ['main', '.workspace', '#list', '#detail', '.listhead', '.toolbar']) {
        const before = requests.length;
        await ownBackground(selector); await empty();
        assert.deepEqual(await checked(), ['B']);
        assert.equal(requests.length, before, 'A background click is purely local UI state');
        await select('A');
      }
    }

    // Text, pages, materials and navigation controls are not empty background.
    await page.locator('#detail h2').click(); assert.equal(await active(), 'A');
    await page.locator('#expensePrintPages img').click(); assert.equal(await active(), 'A');
    await page.locator('#detail .print-materials summary').click(); assert.equal(await active(), 'A');
    await page.locator('#uploadRole').click(); await page.keyboard.press('Escape'); assert.equal(await active(), 'A');
    const popupPromise = page.waitForEvent('popup');
    await page.locator('#detail a[href="/file/proof-A"]').click();
    const popup = await popupPromise; await popup.close(); assert.equal(await active(), 'A');
    await page.locator('#search').fill('Synthetic'); assert.equal(await active(), 'A');
    await page.locator('#categoryFilter').selectOption('酒店',{force:true}); assert.equal(await active(), 'A');
    await page.locator('#personFilter').selectOption('personA',{force:true}); assert.equal(await active(), 'A');
    await page.locator('#issueFilter').selectOption('complete',{force:true}); assert.equal(await active(), 'A');

    await page.locator('#verifySelected').click();
    await page.locator('#verifyDialog[open] #cancelVerify').click();
    await page.waitForFunction(() => !batchVerifyBusy);
    assert.equal(await active(), 'A'); assert.deepEqual(await checked(), ['B']);

    await row('A').locator('[data-move-item]').click();
    const move = page.locator('#moveExpenseDialog[open]'); await move.waitFor();
    assert.equal(await active(), 'A'); await move.locator('select').selectOption('p2');
    await move.locator('p').first().click(); assert.equal(await active(), 'A');
    await page.mouse.click(5, 5); assert.equal(await active(), 'A');
    await move.locator('[data-close]').click(); assert.equal(await active(), 'A');
    await row('A').locator('[data-delete-item]').click();
    await page.locator('#deleteExpenseDialog[open] [data-close]').click(); assert.equal(await active(), 'A');
    await page.locator('#editReviewFields').click();
    const edit = page.locator('#reviewEditDialog[open]'); await edit.waitFor();
    await edit.locator('[name=amount]').fill('1.24'); assert.equal(await active(), 'A');
    await edit.locator('[data-close]').click(); assert.equal(await active(), 'A');
    assert.deepEqual(await checked(), ['B']);
    const filters = await page.locator('#categoryFilter,#personFilter,#issueFilter,#search').evaluateAll(nodes => nodes.map(node => node.value));
    await ownBackground('main'); await empty();
    assert.deepEqual(await page.locator('#categoryFilter,#personFilter,#issueFilter,#search').evaluateAll(nodes => nodes.map(node => node.value)), filters);
    await select('A');

    // A response that finishes after deselection must not put the page back.
    const started = page.waitForRequest(request => request.url().endsWith('/api/expense-print-preview') && request.postDataJSON().id === 'SLOW');
    await select('SLOW'); await started;
    await row('SLOW').locator('.title').click(); await empty();
    const finished = page.waitForResponse(response => response.url().endsWith('/api/expense-print-preview') && response.request().postDataJSON().id === 'SLOW');
    releaseSlow(); await finished; await page.waitForTimeout(30); await empty();
    await select('SLOW'); await page.locator('#expensePrintPages img').waitFor();
    assert.equal(previewReads.filter(value => value.id === 'SLOW').length, 1);

    // Clearing focus does not rebuild the rows or move the independently scrolled list.
    state.items.push(...Array.from({length: 16}, (_, index) => item('FILLER-' + index)));
    await page.evaluate(() => load()); await page.locator('#expensePrintPages img').waitFor();
    await select('A');
    const preservedRow = await row('B').elementHandle();
    const listScroll = await page.locator('#list').evaluate(node => {node.scrollTop = 160; return node.scrollTop;});
    assert.ok(listScroll > 0);
    await ownBackground('main'); await empty();
    assert.equal(await page.locator('#list').evaluate(node => node.scrollTop), listScroll);
    assert.equal(await preservedRow.evaluate(node => node === document.querySelector('[data-item="B"]')), true);
    assert.deepEqual(await checked(), ['B']);

    // The same row and explicit background behavior also works in a narrow layout.
    await page.setViewportSize({width: 390, height: 844});
    await select('SLOW');
    await row('SLOW').locator('.title').click(); await empty();
    await select('A');
    await ownBackground('main'); await empty();
    assert.deepEqual(await checked(), ['B']);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    assert.deepEqual(writes, []); assert.deepEqual(errors, []);
    return 'passed: expense focus toggles and safe blank deselection; independent checkboxes, unchanged filters, guarded controls/dialogs, delayed preview, cached reselection, light/dark and 390px';
  } finally {
    releaseSlow(); await context.close(); await browser.close();
  }
}

module.exports = run;
if (require.main === module) run(require('playwright')).then(console.log).catch(error => {console.error(error); process.exitCode = 1;});
