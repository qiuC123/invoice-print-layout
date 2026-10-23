// Actual frontend files with synthetic HTTP responses only; never accesses the live ledger.
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');

async function run(pw) {
  const browser = await pw.chromium.launch({headless: true});
  const page = await browser.newPage({viewport: {width: 1500, height: 1000}});
  page.setDefaultTimeout(10000);
  const origin = 'http://127.0.0.1:18779';
  const evidence = path.resolve(__dirname, '../workspace/invoice-operations/dialog-close-neutral-dark-20261014/row-actions');
  const errors = [], writes = [], previews = [], stateReads = [];
  const clone = value => JSON.parse(JSON.stringify(value));
  const projects = [{id: 'p1', name: '合成项目甲'}, {id: 'p2', name: '合成项目乙 & <测试>'}, {id: 'p3', name: '合成项目丙'}];
  const attachment = (id, role) => ({id, role, name: `合成${role}.pdf`, available: true, sha256: `synthetic-${id}`});
  const item = (id, fields = {}) => ({
    id, title: `合成费用 ${id}`, project_id: 'p1', project: projects[0].name,
    category: '其他', amount: '40.50', amount_cents: 4050, stage: 'draft',
    deleted: false, verified: false, complete: true, ready: false,
    attachments: [attachment(`${id}-invoice`, 'invoice'), attachment(`${id}-payment`, 'payment')],
    events: [], missing: [], version: `${id}-v1`, expense_date: '2026-10-10',
    note: '', merchant: '合成商家', order_number: '', followup_date: '',
    invoice_state: 'received', waiting_days: 0, ...fields,
  });
  const items = [
    item('delete-me', {title: '<img data-synthetic-injection="true" src=x> & "费用"'}),
    item('move-me', {verified: true, ready: true, amount_cents: 79000, amount: '790.00',
      attachments: [attachment('move-invoice', 'invoice'), attachment('move-payment', 'payment'), attachment('move-detail', 'detail')]}),
    item('cancelled-me', {stage: 'cancelled', complete: false, missing: ['付款截图']}),
    item('submitted-me', {stage: 'submitted', verified: true, ready: true}),
    item('paid-me', {stage: 'reimbursed', verified: true, ready: true}),
    item('merged-me', {stage: 'cancelled', merged_into: 'move-me'}),
    item('stale-delete'), item('stale-move'),
    item('already-deleted', {stage: 'cancelled', deleted: true}),
    item('other-project', {project_id: 'p2', project: projects[1].name}),
  ];
  const baseline = clone(items);
  const state = {
    app_mode: 'invoice', logistics_available: false, items, projects,
    inbox: [], categories: ['其他'], roles: {invoice: '发票', receipt: '收据', payment: '付款截图', detail: '明细'},
    stages: {draft: '待核对', cancelled: '已取消', submitted: '已提交', reimbursed: '已报销'},
    token: 'synthetic-row-actions-token', report_template_ready: true, category_auto_ready: false,
  };
  page.on('pageerror', error => errors.push(error.message));
  await page.route(`${origin}/**`, async route => {
    const req = route.request(), url = new URL(req.url());
    const json = (data, status = 200) => route.fulfill({status, contentType: 'application/json', body: JSON.stringify(data)});
    if (req.method() === 'POST') {
      const data = req.postDataJSON();
      if (url.pathname === '/api/expense-print-preview') {
        previews.push(clone(data));
        const x = items.find(value => value.id === data.id);
        assert.ok(x); assert.equal(data.expected_version, x.version);
        return json({id: x.id, version: x.version, complete: x.complete, missing: x.missing,
          preview_pages: [], page_count: 0, layout: 'A4'});
      }
      writes.push({path: url.pathname, data: clone(data)});
      assert.equal(req.headers()['x-workbench-token'], state.token);
      if (['/api/expenses/delete', '/api/expenses/restore'].includes(url.pathname)) {
        const x = items.find(value => value.id === data.id);
        assert.ok(x); assert.deepEqual(Object.keys(data).sort(), ['expected_version', 'id']);
        if (data.id === 'stale-delete') {
          assert.equal(data.expected_version, 'stale-delete-v1');
          x.version = 'stale-delete-v2';
          return json({error: '费用已变化，请刷新后重试（合成删除冲突）'}, 409);
        }
        assert.equal(data.expected_version, x.version);
        assert.ok(!['submitted', 'reimbursed'].includes(x.stage));
        x.deleted = url.pathname.endsWith('/delete');
        x.version = `${x.id}-v${Number(x.version.split('-v').at(-1)) + 1}`;
        return json(x);
      }
      if (url.pathname === '/api/projects/move') {
        assert.equal(data.ids.length, 1);
        const x = items.find(value => value.id === data.ids[0]);
        const target = projects.find(value => value.id === data.project_id);
        assert.ok(x && target); assert.notEqual(target.id, x.project_id);
        assert.deepEqual(Object.keys(data).sort(), ['ids', 'project_id', 'versions']);
        if (x.id === 'stale-move') {
          assert.deepEqual(data.versions, {'stale-move': 'stale-move-v1'});
          x.version = 'stale-move-v2';
          return json({error: '费用已变化，请刷新后重试（合成迁移冲突）'}, 409);
        }
        assert.deepEqual(data.versions, {[x.id]: x.version});
        assert.equal(x.deleted, false); assert.ok(!['submitted', 'reimbursed'].includes(x.stage));
        Object.assign(x, {project_id: target.id, project: target.name, verified: false, ready: false, version: `${x.id}-v2`});
        return json({moved: 1, items: [x]});
      }
      throw Error(`Unexpected write from row controls: ${url.pathname}`);
    }
    if (url.pathname === '/api/state') {stateReads.push(url.href); return json(state);}
    if (url.pathname === '/api/reimbursement-people') return json({project_id: url.searchParams.get('project') || 'p1', people: [], summary: {rows: []}});
    if (url.pathname === '/api/intake') return json({queue: []});
    if (url.pathname === '/api/preferences') return json({rules: [], learned: [], sections: {}});
    if (url.pathname === '/api/material-matching') return json({vouchers: [], payments: [], matches: [], other: [], report_types: []});
    if (url.pathname === '/api/report-snapshot') return json({rows: []});
    const name = url.pathname === '/' ? 'index.html' : url.pathname.slice(1);
    assert.match(name, /^[a-z-]+\.(html|js|css)$/);
    return route.fulfill({contentType: name.endsWith('.js') ? 'text/javascript' : name.endsWith('.css') ? 'text/css' : 'text/html',
      body: await fs.readFile(path.resolve(__dirname, '../src/invoice_print_layout/web', name))});
  });
  const row = id => page.locator(`[data-item="${id}"]`);
  const control = (action, id) => page.locator(`[data-${action}-item="${id}"]`);
  const openDialog = name => page.locator(`#${name}[open]`);
  const inactive = async (beforePreviews, beforeWrites) => {
    assert.equal(await page.evaluate(() => active), null, 'row button must not select the expense');
    assert.equal(previews.length, beforePreviews, 'row button must not generate a print preview');
    assert.equal(writes.length, beforeWrites, 'opening a row action must not write');
  };
  const noOverflow = async () => {
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true,
      'viewport must have no horizontal overflow');
  };
  try {
    await fs.mkdir(evidence, {recursive: true});
    await page.goto(`${origin}/?project=p1`);
    await control('delete', 'delete-me').waitFor();
    await page.evaluate(() => {window.confirm = () => {throw Error('Native confirm must not be used');};});
    assert.equal(await row('already-deleted').count(), 0, 'deleted expenses are hidden from the ordinary view');
    assert.equal(await row('other-project').count(), 0, 'project scope excludes another project');
    assert.equal(await row('delete-me').locator('.title').innerText(), baseline[0].title);
    assert.equal(await page.locator('[data-synthetic-injection]').count(), 0, 'HTML titles must remain text');
    for (const id of ['submitted-me', 'paid-me', 'merged-me']) {
      for (const action of ['delete', 'move']) {
        const button = control(action, id);
        assert.ok(await button.count() === 0 || !await button.isVisible() || await button.isDisabled(),
          `${id} must not allow ${action}`);
      }
    }
    await noOverflow();
    await page.screenshot({path: path.join(evidence, 'synthetic-desktop.png'), fullPage: true});

    // Opening and cancelling are read-only, even when a row already has a checkbox selected.
    await page.locator('[data-check="delete-me"]').check();
    const previewBefore = previews.length, writeBefore = writes.length;
    await control('delete', 'delete-me').click();
    await openDialog('deleteExpenseDialog').waitFor();
    await inactive(previewBefore, writeBefore);
    assert.match(await openDialog('deleteExpenseDialog').innerText(), /费用/);
    assert.equal(await openDialog('deleteExpenseDialog').locator('[data-synthetic-injection]').count(), 0);
    await openDialog('deleteExpenseDialog').locator('[data-close]').click();
    await inactive(previewBefore, writeBefore);
    assert.equal(items[0].deleted, false);

    await control('delete', 'delete-me').click();
    await openDialog('deleteExpenseDialog').locator('[data-confirm]').click();
    await row('delete-me').waitFor({state: 'detached'});
    assert.deepEqual(writes.at(-1), {path: '/api/expenses/delete', data: {id: 'delete-me', expected_version: 'delete-me-v1'}});
    assert.equal(await page.evaluate(() => checked.has('delete-me')), false, 'deleted item leaves the selection');
    assert.equal(await page.evaluate(() => active), null);
    assert.equal(previews.length, previewBefore);
    await page.locator('#tabs [data-view="review"]').click();
    assert.equal(await row('delete-me').count(), 0);
    await page.locator('#tabs [data-view="deleted"]').click();
    await control('restore', 'delete-me').waitFor();
    assert.deepEqual((await page.locator('[data-item]').evaluateAll(xs => xs.map(x => x.dataset.item))).sort(), ['already-deleted', 'cancelled-me', 'delete-me', 'merged-me']);
    assert.equal(await control('move', 'delete-me').count(), 0);
    const beforeRestore = previews.length;
    await control('restore', 'delete-me').click();
    await page.waitForFunction(() => !state.items.find(x => x.id === 'delete-me').deleted && view === 'review');
    assert.deepEqual(writes.at(-1), {path: '/api/expenses/restore', data: {id: 'delete-me', expected_version: 'delete-me-v2'}});
    await page.waitForFunction(() => document.getElementById('expensePrintStatus')?.textContent.includes('尚无可预览的原件'));
    assert.equal(previews.length, beforeRestore + 1, 'restored expense opens its read-only preview in the correct stage');
    await page.locator('#tabs [data-view="review"]').click();
    await row('delete-me').waitFor();
    assert.equal(items[0].stage, 'draft');
    assert.equal(items[0].amount_cents, 4050);
    assert.deepEqual(items[0].attachments, baseline[0].attachments);
    await page.goto(`${origin}/?project=p1`);
    await row('delete-me').waitFor();

    // Legacy cancellation is shown as a read-only retired record and cannot become a new draft.
    await page.locator('#tabs [data-view="deleted"]').click();
    assert.equal(await row('cancelled-me').count(), 1);
    assert.match(await row('cancelled-me').innerText(), /只读/);
    assert.equal(await row('cancelled-me').locator('[data-check]').count(), 0);
    for (const action of ['delete', 'move', 'restore']) assert.equal(await control(action, 'cancelled-me').count(), 0);
    assert.deepEqual(items.find(x => x.id === 'cancelled-me'), baseline.find(x => x.id === 'cancelled-me'));
    await page.locator('#tabs [data-view="review"]').click();
    assert.equal(await row('cancelled-me').count(), 0);
    assert.equal(items.find(x => x.id === 'cancelled-me').stage, 'cancelled');

    // A stale confirmation is surfaced once, then the displayed state is refreshed.
    const beforeDeleteConflictReads = stateReads.length, beforeDeleteConflictWrites = writes.length;
    await control('delete', 'stale-delete').click();
    await openDialog('deleteExpenseDialog').locator('[data-confirm]').click();
    await page.getByText(/合成删除冲突/).first().waitFor({state: 'visible'});
    await page.waitForFunction(() => state.items.find(x => x.id === 'stale-delete')?.version === 'stale-delete-v2');
    assert.ok(stateReads.length > beforeDeleteConflictReads);
    assert.equal(writes.length, beforeDeleteConflictWrites + 1, 'conflict does not retry a deletion automatically');
    assert.equal(items.find(x => x.id === 'stale-delete').deleted, false);
    if (await openDialog('deleteExpenseDialog').count()) await openDialog('deleteExpenseDialog').locator('[data-close]').click();

    const beforeMoveConflictReads = stateReads.length, beforeMoveConflictWrites = writes.length;
    await control('move', 'stale-move').click();
    await openDialog('moveExpenseDialog').locator('select[name="project_id"]').selectOption('p2');
    await openDialog('moveExpenseDialog').locator('[data-confirm]').click();
    await page.getByText(/合成迁移冲突/).first().waitFor({state: 'visible'});
    await page.waitForFunction(() => state.items.find(x => x.id === 'stale-move')?.version === 'stale-move-v2');
    assert.ok(stateReads.length > beforeMoveConflictReads);
    assert.equal(writes.length, beforeMoveConflictWrites + 1, 'conflict does not retry a migration automatically');
    assert.equal(items.find(x => x.id === 'stale-move').project_id, 'p1');
    if (await openDialog('moveExpenseDialog').count()) await openDialog('moveExpenseDialog').locator('[data-close]').click();

    // The mobile row still offers both actions without overflowing the viewport.
    await page.setViewportSize({width: 390, height: 844});
    await page.locator('#tabs [data-view="submitted"]').click();
    await noOverflow();
    const moveBaseline = clone(items.find(x => x.id === 'move-me'));
    const beforeMobilePreviews = previews.length, beforeMobileWrites = writes.length;
    await control('delete', 'move-me').click();
    await openDialog('deleteExpenseDialog').waitFor();
    await noOverflow();
    await openDialog('deleteExpenseDialog').locator('[data-close]').click();
    await inactive(beforeMobilePreviews, beforeMobileWrites);
    await control('move', 'move-me').click();
    await openDialog('moveExpenseDialog').waitFor();
    await inactive(beforeMobilePreviews, beforeMobileWrites);
    const target = openDialog('moveExpenseDialog').locator('select[name="project_id"]');
    assert.deepEqual(await target.locator('option').evaluateAll(xs => xs.filter(x => x.value).map(x => x.value).sort()), ['p2', 'p3']);
    assert.equal(await target.locator('option[value="p2"]').innerText(), projects[1].name, 'project names must be escaped');
    await noOverflow();
    await page.screenshot({path: path.join(evidence, 'synthetic-mobile-move.png'), fullPage: true});
    await openDialog('moveExpenseDialog').locator('[data-close]').click();
    await inactive(beforeMobilePreviews, beforeMobileWrites);
    await control('move', 'move-me').click();
    await openDialog('moveExpenseDialog').locator('select[name="project_id"]').selectOption('p2');
    await openDialog('moveExpenseDialog').locator('[data-confirm]').click();
    await row('move-me').waitFor({state: 'detached'});
    assert.deepEqual(writes.at(-1), {path: '/api/projects/move', data: {ids: ['move-me'], project_id: 'p2', versions: {'move-me': 'move-me-v1'}}});
    assert.equal(await page.evaluate(() => active), null);
    assert.equal(previews.length, beforeMobilePreviews);
    const moved = items.find(x => x.id === 'move-me');
    assert.equal(moved.amount_cents, moveBaseline.amount_cents);
    assert.deepEqual(moved.attachments, moveBaseline.attachments);
    assert.equal(moved.stage, 'draft'); assert.equal(moved.verified, false); assert.equal(moved.ready, false);
    await page.goto(`${origin}/?project=p2`);
    await row('move-me').waitFor();
    assert.equal(await row('move-me').locator('.money').innerText().then(x => x.includes('790.00')), true);
    assert.deepEqual(await page.evaluate(() => state.items.find(x => x.id === 'move-me').attachments), moveBaseline.attachments);
    await noOverflow();
    await page.screenshot({path: path.join(evidence, 'synthetic-mobile-target.png'), fullPage: true});

    await page.goto(origin + '/');
    await row('move-me').waitFor();
    assert.equal(await row('move-me').count(), 1, 'migration counts the expense once in the all-project ledger');
    assert.equal(await row('already-deleted').count(), 0);
    assert.deepEqual(errors, []);
    assert.deepEqual(previews, [{id: 'delete-me', expected_version: 'delete-me-v3'}], 'only the explicitly restored record opens a read-only preview');
    assert.equal(writes.length, 5);
    assert.ok(writes.every(x => ['/api/expenses/delete', '/api/expenses/restore', '/api/projects/move'].includes(x.path)));
    for (const id of ['submitted-me', 'paid-me', 'merged-me', 'other-project']) {
      assert.deepEqual(items.find(x => x.id === id), baseline.find(x => x.id === id), `${id} was not altered`);
    }
    const result = {passed: true, scope: 'synthetic row controls; required versions, cancel, delete/restore, legacy cancellation, stale conflicts, project migration, HTML escaping, desktop and 390px mobile',
      writes, previews, state_read_count: stateReads.length, no_live_data: true};
    await fs.writeFile(path.join(evidence, 'browser-verification.json'), JSON.stringify(result, null, 2));
    await fs.writeFile(path.join(evidence, 'browser.log'), 'PASS ' + result.scope + '\n');
    return result;
  } catch (error) {
    await fs.mkdir(evidence, {recursive: true});
    await fs.writeFile(path.join(evidence, 'browser-failure.json'), JSON.stringify({error: error.message, stack: error.stack, errors, writes, previews,
      url: page.url(), body: await page.locator('body').innerText()}, null, 2));
    throw error;
  } finally {await browser.close();}
}
module.exports = run;
if (require.main === module) run(require('playwright')).then(result => console.log(JSON.stringify(result))).catch(error => {console.error(error); process.exitCode = 1;});
