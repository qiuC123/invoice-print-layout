// Real workbench assets with intercepted synthetic records; no live server or writes.
const assert = require('node:assert/strict'), fs = require('node:fs/promises'), path = require('node:path');
async function run(pw) {
  const browser = await pw.chromium.launch({headless: true});
  const page = await browser.newPage({viewport: {width: 1440, height: 1000}});
  page.setDefaultTimeout(10000);
  const origin = 'http://127.0.0.1:18871', errors = [], writes = [], statePlans = [], pendingReleases = [];
  const people = [{id: 'a', label: '甲', version: 'a-v1'}, {id: 'b', label: '乙', version: 'b-v1'}, {id: 'empty', label: '空人员', version: 'e-v1'}];
  const types = [{code: 'other', label: '其他费用', editable_name: true, names: []}];
  const fee = (id, cents, person = 'a', extra = {}) => ({id, project_id: 'p1', project: '合成项目', title: '费用 ' + id,
    project_type: 'other', project_name: '费用 ' + id, category: '其他', amount: (cents / 100).toFixed(2), amount_cents: cents,
    stage: 'draft', verified: false, ready: false, complete: true, missing: [], attachments: [], events: [], version: id + '-v1',
    expense_date: '', reimbursement_person_id: person, reimbursement_person_label: people.find(value => value.id === person)?.label || '', ...extra});
  const items = [
    fee('a-pending', 1000), fee('a-zero', 0), fee('a-conflict', 1100, 'a', {reimbursement_person_conflict: true}),
    fee('a-revived', 9000, 'a', {deleted: true}), fee('a-revived', 400),
    fee('a-ready', 2000, 'a', {ready: true, verified: true}),
    fee('a-submitted', 3000, 'a', {stage: 'submitted', ready: true, verified: true}),
    fee('a-reimbursed', 4000, 'a', {stage: 'reimbursed', ready: true, verified: true}),
    fee('a-pending', 99999), fee('b-pending', 900, 'b'),
    fee('u-empty', 500, ''), fee('u-missing', 600, undefined, {reimbursement_person_id: undefined, ready: true}),
    fee('u-null', 700, null, {stage: 'submitted'}), fee('ghost', 800, 'retired-id', {reimbursement_person_label: '历史归属'}),
    ...[{deleted: true}, {merged_into: 'a-pending'}, {stage: 'cancelled'}, {returned_to_synthesis: true},
      {synthesis_material_state: 'archived'}, {synthesis_material_state: 'deleted'}].map((extra, index) => fee('excluded-' + index, 9000, 'a', extra)),
    fee('other-project', 99900, 'a', {project_id: 'p2', project: '另一项目'})
  ];
  const state = {app_mode: 'invoice', token: 'synthetic', projects: [{id: 'p1', name: '合成项目'}, {id: 'p2', name: '另一项目'}],
    items, inbox: [], categories: ['其他'], roles: {}, stages: {draft: '待核对'}, report_types: types, category_auto_ready: false};
  const summary = {project_id: 'p1', total_cents: 15000, expense_count: 12, rows: [
    {person_id: 'a', label: '甲', expense_count: 7, total_cents: 11500, pending_review_cents: 2500, ready_cents: 2000, packaged_cents: 7000},
    {person_id: 'b', label: '乙', expense_count: 1, total_cents: 900, pending_review_cents: 900, ready_cents: 0, packaged_cents: 0},
    {person_id: '', label: '未指定', expense_count: 3, total_cents: 1800, pending_review_cents: 500, ready_cents: 600, packaged_cents: 700},
    {person_id: 'retired-id', label: '历史归属', expense_count: 1, total_cents: 800, pending_review_cents: 800, ready_cents: 0, packaged_cents: 0}
  ]};
  function holdState(payload = structuredClone(state)) {
    let release, started;
    const gate = new Promise(resolve => {release = resolve;}), begun = new Promise(resolve => {started = resolve;});
    statePlans.push({gate, started, payload}); pendingReleases.push(release);
    return {release, started: begun};
  }
  page.on('pageerror', error => errors.push(error.message));
  await page.route(origin + '/**', async route => {
    const req = route.request(), url = new URL(req.url());
    const json = (body, status = 200) => route.fulfill({status, contentType: 'application/json', body: JSON.stringify(body)});
    if (req.method() !== 'GET') {writes.push({method: req.method(), url: url.pathname}); return json({error: 'No writes allowed'}, 405);}
    if (url.pathname === '/api/state') {
      const plan = statePlans.shift(); if (plan?.started) plan.started(); if (plan?.gate) await plan.gate;
      return json(plan?.payload || state, plan?.status || 200);
    }
    if (url.pathname === '/api/reimbursement-people') {
      assert.equal(url.searchParams.get('project'), 'p1');
      return json({project_id: 'p1', people, summary, retired_people: []});
    }
    if (url.pathname === '/api/material-matching') return json({vouchers: [], payments: [], other: [], archived: [], matches: [], suggestions: [], report_types: types});
    if (url.pathname.startsWith('/api/')) return json({queue: []});
    const file = url.pathname === '/' ? 'index.html' : url.pathname.slice(1);
    if (!/^[a-z-]+\.(html|js|css)$/.test(file)) return route.abort();
    return route.fulfill({contentType: file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html',
      body: await fs.readFile(path.resolve(__dirname, '../src/invoice_print_layout/web', file))});
  });
  const panel = page.locator('#reimbursementPeopleDialog');
  const number = (person, scope = 'all', count = false) => panel.locator(`[data-person-details="${person}"][data-allocation-scope="${scope}"][data-allocation-count="${count}"]`);
  const rows = () => panel.locator('[data-open-person-expense]');
  async function checkDetails(person, scope, expected, cents, count = false) {
    await number(person, scope, count).click(); await panel.locator('[data-content][aria-busy=false]').waitFor();
    assert.deepEqual(await rows().evaluateAll(buttons => buttons.map(button => button.dataset.openPersonExpense)), expected);
    assert.match(await panel.locator('.reimbursement-summary-total').innerText(), new RegExp(`合计 ¥ ${(cents / 100).toFixed(2).replace('.', '\\.')}\\s*${expected.length} 笔费用`));
    await panel.getByRole('button', {name: '← 返回人员汇总', exact: true}).click(); await number(person, scope, count).waitFor();
    assert.equal(await number(person, scope, count).evaluate(button => button === document.activeElement), true, 'return restores the originating number focus');
  }
  try {
    await page.goto(origin + '/?project=p1'); await page.locator('[data-item=a-zero]').waitFor();
    await page.evaluate(() => {
      window.detailOpens = [];
      window.openReimbursementExpense = async (id, person, options) => {
        if (window.holdExpenseOpen) await new Promise(resolve => {window.releaseExpenseOpen = resolve;});
        if (!options.current()) return false;
        if (window.failExpenseOpen) throw Error('费用已变化，请刷新后重试。');
        window.detailOpens.push({id, person}); return true;
      };
    });
    await page.locator('#reimbursementPeople').click(); await number('a', 'all', true).waitFor();
    assert.equal(await panel.locator('[data-inline-person] input[role=combobox]').count(), 3, 'name comboboxes remain available');
    assert.equal(await number('a', 'all', true).getAttribute('type'), 'button');
    await number('a', 'all', true).focus(); await page.keyboard.press('Tab');
    assert.equal(await number('a', 'all').evaluate(button => button === document.activeElement), true);
    for (const theme of ['light', 'dark']) {
      await page.evaluate(value => document.documentElement.dataset.theme = value, theme);
      const styles = await number('a', 'all').evaluate(button => {
        const style = getComputedStyle(button), token = getComputedStyle(document.documentElement).getPropertyValue('--wb-accent').trim();
        const probe = document.createElement('span'); probe.style.color = token; probe.style.background = getComputedStyle(document.documentElement).getPropertyValue('--wb-hover');
        document.body.append(probe); const accent = getComputedStyle(probe).color, hover = getComputedStyle(probe).backgroundColor; probe.remove();
        return {color: style.color, accent, hover, background: style.backgroundColor, decoration: style.textDecorationLine, outline: style.outlineStyle,
          minHeight: style.minHeight, radius: style.borderRadius};
      });
      assert.equal(styles.color, styles.accent); assert.ok(['rgba(0, 0, 0, 0)', styles.hover].includes(styles.background));
      assert.match(styles.decoration, /underline/); assert.notEqual(styles.outline, 'none');
      assert.equal(styles.minHeight, '32px'); assert.equal(styles.radius, '6px');
    }
    await checkDetails('a', 'all', ['a-pending', 'a-zero', 'a-conflict', 'a-revived', 'a-ready', 'a-submitted', 'a-reimbursed'], 11500, true);
    await checkDetails('a', 'all', ['a-pending', 'a-zero', 'a-conflict', 'a-revived', 'a-ready', 'a-submitted', 'a-reimbursed'], 11500);
    await checkDetails('a', 'pending', ['a-pending', 'a-zero', 'a-conflict', 'a-revived'], 2500);
    await checkDetails('a', 'ready', ['a-ready'], 2000);
    await checkDetails('a', 'packaged', ['a-submitted', 'a-reimbursed'], 7000);
    await checkDetails('', 'all', ['u-empty', 'u-missing', 'u-null'], 1800, true);
    await checkDetails('', 'pending', ['u-empty'], 500);
    await checkDetails('', 'ready', ['u-missing'], 600);
    await checkDetails('', 'packaged', ['u-null'], 700);
    await checkDetails('empty', 'all', [], 0, true);

    // Each opening reads current state instead of replaying the summary's older amounts.
    const fresh = structuredClone(state); fresh.items = [fee('fresh-zero', 0, 'b', {title: '<img data-detail-xss="yes">'})];
    statePlans.push({payload: fresh}); await number('b', 'all', true).click(); await rows().waitFor();
    assert.deepEqual(await rows().evaluateAll(buttons => buttons.map(button => button.dataset.openPersonExpense)), ['fresh-zero']);
    assert.equal(await page.locator('[data-detail-xss]').count(), 0); assert.match(await rows().innerText(), /<img/);
    await panel.getByRole('button', {name: '← 返回人员汇总', exact: true}).click(); await number('b', 'all', true).waitFor();
    statePlans.push({payload: {error: '明细暂时不可用'}, status: 503}); await number('a', 'all', true).click();
    await panel.getByRole('button', {name: '重试', exact: true}).waitFor(); assert.match(await panel.innerText(), /明细暂时不可用/);
    await panel.getByRole('button', {name: '重试', exact: true}).click(); await rows().first().waitFor();
    assert.equal(await rows().count(), 7);
    await panel.getByRole('button', {name: '← 返回人员汇总', exact: true}).click(); await number('a', 'all', true).waitFor();
    statePlans.push({payload: {...state, projects: [{id: 'p2', name: '另一项目'}]}}); await number('a', 'all', true).click();
    await panel.getByRole('button', {name: '重试', exact: true}).waitFor(); assert.equal(await rows().count(), 0);
    assert.match(await panel.innerText(), /项目或费用列表已变化/);
    await panel.getByRole('button', {name: '← 返回人员汇总', exact: true}).click(); await number('a', 'all', true).waitFor();

    // Returning or closing while a read is pending prevents its late result from replacing newer content.
    const firstRead = holdState(); await number('a', 'pending').click(); await firstRead.started;
    assert.match(await panel.innerText(), /正在读取最新费用明细/);
    assert.equal(await panel.getByRole('button', {name: '关闭', exact: true}).isEnabled(), true);
    await panel.getByRole('button', {name: '← 返回人员汇总', exact: true}).click(); await number('b', 'all', true).waitFor();
    await number('b', 'all', true).click(); await rows().waitFor(); firstRead.release();
    await page.waitForFunction(() => document.querySelector('[data-open-person-expense]')?.dataset.openPersonExpense === 'b-pending');
    assert.equal(await panel.locator('h2').innerText(), '乙 · 登记费用明细');
    await panel.getByRole('button', {name: '← 返回人员汇总', exact: true}).click(); await number('a', 'all', true).waitFor();
    const closedRead = holdState(); await number('a', 'all', true).click(); await closedRead.started;
    await panel.getByRole('button', {name: '关闭', exact: true}).click(); await panel.waitFor({state: 'detached'});
    await page.locator('#reimbursementPeople').click(); await number('b', 'all', true).waitFor(); closedRead.release();
    assert.equal(await panel.locator('h2').innerText(), '人员金额');

    // A failed single-expense handoff leaves the detail list available, including unassigned identity.
    await number('', 'all', true).click(); await rows().first().waitFor(); await page.evaluate(() => {window.failExpenseOpen = true;});
    await panel.locator('[data-open-person-expense=u-empty]').click();
    await panel.locator('[data-open-expense-error]').filter({hasText: '费用已变化'}).waitFor(); assert.equal(await rows().count(), 3);
    await page.evaluate(() => {window.failExpenseOpen = false;}); await panel.locator('[data-open-person-expense=u-empty]').click();
    await panel.waitFor({state: 'detached'}); assert.deepEqual(await page.evaluate(() => window.detailOpens), [{id: 'u-empty', person: ''}]);
    await page.locator('#reimbursementPeople').click(); await number('a', 'all', true).waitFor();
    await number('a', 'all', true).click(); await rows().first().waitFor(); await page.evaluate(() => {window.holdExpenseOpen = true;});
    await panel.locator('[data-open-person-expense=a-pending]').click(); await page.waitForFunction(() => !!window.releaseExpenseOpen);
    await panel.getByRole('button', {name: '关闭', exact: true}).click(); await page.locator('#reimbursementPeople').click(); await number('a', 'all', true).waitFor();
    await page.evaluate(() => {window.holdExpenseOpen = false; window.releaseExpenseOpen();});
    assert.equal(await panel.locator('h2').innerText(), '人员金额'); assert.equal(await page.evaluate(() => window.detailOpens.length), 1);
    await page.setViewportSize({width: 390, height: 844}); await number('a', 'all', true).click(); await rows().first().waitFor();
    assert.equal(await panel.evaluate(element => element.scrollWidth <= element.clientWidth), true);
    assert.deepEqual(writes, []); assert.deepEqual(errors, []);
    return {passed: true, scope: 'person/amount detail buckets; exact unassigned; exclusions and distinct IDs; latest project data; return focus; zero/empty/error/retry; safe late reads and handoffs; themes/keyboard/mobile; no POST'};
  } finally {for (const release of pendingReleases) release(); await browser.close();}
}
module.exports = run;
if (require.main === module) run(require('playwright')).then(console.log).catch(error => {console.error(error); process.exitCode = 1;});
