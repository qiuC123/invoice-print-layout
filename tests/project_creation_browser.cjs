// Actual frontend with synthetic HTTP only. No live projects or ledger writes.
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');

async function run(pw) {
  const browser = await pw.chromium.launch({headless: true});
  const page = await browser.newPage({viewport: {width: 1500, height: 1000}});
  page.setDefaultTimeout(10000);
  const origin = 'http://127.0.0.1:18788';
  const evidence = path.resolve(__dirname, '../workspace/invoice-operations/project-create-entry-20261013');
  const clone = value => JSON.parse(JSON.stringify(value));
  const projects = [
    {id: 'old-unpaid', name: '合成未报销甲', reimbursement_status: 'unreimbursed', reimbursement_revision: 4},
    {id: 'old-paid', name: '合成已报销乙', reimbursement_status: 'reimbursed', reimbursement_revision: 7},
  ];
  const item = (id, project) => ({id, title: id, project_id: project.id, project: project.name,
    category: '其他', amount_cents: 1000, amount: '10.00', stage: 'draft', deleted: false,
    verified: false, ready: false, complete: true, missing: [], attachments: [], events: [],
    version: id + '-v1', expense_date: '', merchant: '', note: '', invoice_state: 'received'});
  const items = projects.map((project, index) => item('synthetic-fee-' + index, project));
  const existingProjects = clone(projects), existingItems = clone(items), writes = [], errors = [];
  const state = {app_mode: 'invoice', logistics_available: false, projects, items,
    categories: ['其他'], inbox: [], token: 'synthetic-create-token', roles: {invoice: '发票'},
    stages: {draft: '待核对'}, category_auto_ready: false, report_template_ready: true};
  let failNext = false, holdNext = false, missingIdNext = false, releasePending;
  page.on('pageerror', error => errors.push(error.message));
  await page.route(`${origin}/**`, async route => {
    const req = route.request(), url = new URL(req.url());
    const json = (data, status = 200) => route.fulfill({status, contentType: 'application/json', body: JSON.stringify(data)});
    if (url.pathname === '/api/expense-print-preview') return json({id: req.postDataJSON().id, version: 'synthetic-v1', complete: false, preview_pages: [], missing: []});
    if (req.method() === 'POST') {
      const data = req.postDataJSON(); writes.push({path: url.pathname, data: clone(data)});
      assert.equal(url.pathname, '/api/projects', 'creating a project never calls expense, intake, or reimbursement APIs');
      assert.equal(req.headers()['x-workbench-token'], state.token);
      assert.deepEqual(Object.keys(data), ['name'], 'current project scope is never copied into the new-project payload');
      assert.ok(data.name.trim());
      if (failNext) {failNext = false; return json({error: '合成创建暂时失败，请重试'}, 503);}
      if (holdNext) {holdNext = false; await new Promise(resolve => {releasePending = resolve;});}
      const created = {id: `server-generated-${projects.length + 10}`, name: data.name, status: 'draft'};
      projects.push(created);
      if (missingIdNext) {missingIdNext = false; return json({name: created.name});}
      return json(created);
    }
    if (url.pathname === '/api/state') return json(state);
    if (url.pathname === '/api/intake') return json({queue: []});
    if (url.pathname === '/api/material-matching') return json({vouchers: [], payments: [], matches: [], other: [], report_types: []});
    if (url.pathname === '/api/material-matching/candidates') return json({candidates: [], engine: 'rules', requests: 0});
    if (url.pathname === '/api/report-snapshot') return json({rows: []});
    if (url.pathname.startsWith('/api/')) return json({});
    const name = url.pathname === '/' ? 'index.html' : url.pathname.slice(1);
    if (!/^[a-z-]+\.(html|js|css)$/.test(name)) return route.abort();
    return route.fulfill({contentType: name.endsWith('.js') ? 'text/javascript' : name.endsWith('.css') ? 'text/css' : 'text/html',
      body: await fs.readFile(path.resolve(__dirname, '../src/invoice_print_layout/web', name))});
  });
  const dialog = () => page.locator('#project-create');
  const input = () => dialog().locator('[name="name"]');
  const submit = () => dialog().getByRole('button', {name: '创建项目', exact: true});
  const open = async () => {await page.locator('#createProject').click(); await dialog().waitFor({state: 'visible'});};
  const loaded = async id => {
    await page.waitForFunction(id => state.projects.some(p => p.id === id) && document.querySelector('h1').textContent.includes(state.projects.find(p => p.id === id).name), id);
  };
  const unchanged = () => {assert.deepEqual(projects.slice(0, 2), existingProjects); assert.deepEqual(items, existingItems);};
  try {
    await fs.mkdir(evidence, {recursive: true});
    // Every existing project and the unscoped page expose the same single sidebar entry.
    for (const scope of ['', 'old-unpaid', 'old-paid']) {
      await page.goto(origin + '/' + (scope ? '?project=' + scope : ''));
      if (scope) await loaded(scope); else await page.locator('[data-item]').first().waitFor();
      assert.equal(await page.locator('.rail #createProject').isVisible(), true);
      assert.equal(await page.locator('#createProject').count(), 1);
      const before = writes.length;
      await open(); assert.equal(await submit().isDisabled(), true);
      await input().fill('   '); assert.equal(await submit().isDisabled(), true);
      await dialog().locator('form').dispatchEvent('submit');
      assert.equal(writes.length, before, 'empty or whitespace names never send a request');
      await input().fill('取消的合成项目');
      await dialog().getByRole('button', {name: '取消', exact: true}).click();
      assert.equal(await dialog().count(), 0); assert.equal(writes.length, before);
      await open(); await page.keyboard.press('Escape');
      assert.equal(await dialog().count(), 0); assert.equal(writes.length, before);
      unchanged();
    }
    // The project marked reimbursed may create another project without changing its label.
    await open(); await input().fill('合成新项目 <img data-created-project="yes" src=x> & "丙"');
    failNext = true; await submit().click();
    await dialog().getByRole('alert').filter({hasText: '合成创建暂时失败'}).waitFor();
    assert.equal(await submit().isEnabled(), true, 'failed POST leaves the dialog ready to retry');
    assert.equal(projects.length, 2); unchanged();
    holdNext = true; await submit().click();
    await page.waitForFunction(() => projectCreationBusy);
    assert.equal(writes.length, 2);
    assert.equal(await dialog().locator('[name="name"]').isDisabled(), true);
    assert.equal(await dialog().getByRole('button', {name: '取消', exact: true}).isDisabled(), true);
    assert.equal(await page.locator('#createProject').isDisabled(), true);
    await dialog().locator('form').dispatchEvent('submit');
    await page.keyboard.press('Escape');
    assert.equal(await dialog().isVisible(), true); assert.equal(writes.length, 2, 'double submit cannot create twice');
    assert.ok(releasePending); releasePending();
    await page.waitForURL(origin + '/?project=server-generated-12');
    await loaded('server-generated-12');
    assert.equal(projects.length, 3); assert.equal(await page.locator('[data-item]').count(), 0);
    assert.equal(await page.locator('#projectReimbursementStatus').count(), 0);
    assert.equal(await page.evaluate(()=>projectContext().reimbursement_status||'unreimbursed'),'unreimbursed');
    assert.equal(await page.locator('[data-created-project]').count(), 0, 'new project name remains escaped text');
    assert.equal(await page.locator('#projectLinks a[aria-current="page"]').innerText(), projects[2].name + ' · 发票报销');
    unchanged();
    await page.screenshot({path: path.join(evidence, 'synthetic-created-project.png'), fullPage: true});
    // Root creation navigates using the actual POST response ID, without retaining old query filters.
    await page.goto(origin + '/?matter=synthetic-fee-0'); await page.locator('[data-item]').first().waitFor();
    await open(); await input().fill('合成全部项目中新建'); await submit().click();
    await page.waitForURL(origin + '/?project=server-generated-13'); await loaded('server-generated-13');
    assert.equal(projects.length, 4); unchanged();
    // At mobile width the very same entry moves to the visible header; desktop restores it.
    await page.setViewportSize({width: 390, height: 844});
    await page.locator('header #createProject').waitFor({state: 'visible'});
    assert.equal(await page.locator('header #createProject').isVisible(), true);
    assert.equal(await page.locator('#createProject').count(), 1);
    await open(); await input().fill('合成移动端新项目');
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    assert.equal(await dialog().evaluate(el => el.getBoundingClientRect().right <= innerWidth), true);
    await submit().click(); await page.waitForURL(origin + '/?project=server-generated-14'); await loaded('server-generated-14');
    assert.equal(projects.length, 5); assert.equal(await page.locator('header #createProject').isVisible(), true);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await page.screenshot({path: path.join(evidence, 'synthetic-mobile-created-project.png'), fullPage: true});
    await page.setViewportSize({width: 1500, height: 1000});
    await page.locator('.rail #createProject').waitFor({state: 'visible'});
    assert.equal(await page.locator('.rail #createProject').isVisible(), true);
    assert.equal(await page.locator('#createProject').count(), 1);
    // A successful response without an ID may already have created a project: never POST it twice.
    await open(); await input().fill('合成创建成功但响应缺编号'); missingIdNext = true;
    const beforeMissingId = writes.length; await submit().click();
    await dialog().locator('[data-create-error]').filter({hasText: '项目创建结果缺少编号'}).waitFor();
    assert.equal(writes.length, beforeMissingId + 1); assert.equal(projects.length, 6);
    assert.equal(await dialog().locator('[type="submit"]').isDisabled(), true);
    assert.equal(await dialog().getByRole('button', {name: '取消', exact: true}).isEnabled(), true);
    await dialog().locator('form').dispatchEvent('submit');
    assert.equal(writes.length, beforeMissingId + 1, 'an acknowledged create response cannot be retried automatically or by another submit');
    assert.equal(new URL(page.url()).searchParams.get('project'), 'server-generated-14');
    await dialog().getByRole('button', {name: '取消', exact: true}).click();
    assert.equal(await dialog().count(), 0); assert.equal(await page.locator('#createProject').isEnabled(), true);
    await page.reload(); await loaded('server-generated-14');
    assert.equal(await page.locator('#projectLinks a[href="/?project=server-generated-15"]').count(), 1, 'refresh recovers the acknowledged project from authoritative state');
    assert.deepEqual(errors, []); unchanged();
    assert.equal(writes.length, 5, 'one failed POST, three successful navigations, and one acknowledged response without ID');
    const result = {passed: true, scope: 'root and both reimbursement views, empty/cancel/escape without write, scoped POST isolation, visible in-dialog failure retry, pending duplicate suppression, acknowledged missing ID without retry and close/reload recovery, actual response ID navigation, name escaping, mobile single entry, existing expenses and labels unchanged', writes, no_live_data: true};
    await fs.writeFile(path.join(evidence, 'browser-verification.json'), JSON.stringify(result, null, 2));
    return result;
  } catch (error) {
    await fs.mkdir(evidence, {recursive: true});
    await fs.writeFile(path.join(evidence, 'browser-failure.json'), JSON.stringify({error: error.message, stack: error.stack, errors, writes, url: page.url(), body: await page.locator('body').innerText()}, null, 2));
    await page.screenshot({path: path.join(evidence, 'synthetic-failure.png'), fullPage: true});
    throw error;
  } finally {await browser.close();}
}
module.exports = run;
if (require.main === module) run(require('playwright')).then(result => console.log(JSON.stringify(result))).catch(error => {console.error(error); process.exitCode = 1;});
