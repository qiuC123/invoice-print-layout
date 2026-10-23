// Actual frontend files and synthetic HTTP only. Never reads or writes the live ledger.
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');

async function run(pw) {
  const browser = await pw.chromium.launch({headless: true});
  const page = await browser.newPage({viewport: {width: 1500, height: 1000}});
  page.setDefaultTimeout(10000);
  const origin = 'http://127.0.0.1:18780';
  const evidence = path.resolve(__dirname, '../workspace/invoice-operations/project-reimbursement-status-20261010');
  const errors = [], writes = [], reads = [];
  const clone = value => JSON.parse(JSON.stringify(value));
  const projects = [
    {id: 'p1', name: '合成未报销 <img data-synthetic-project="true" src=x> & "甲"', reimbursement_status: 'unreimbursed', reimbursement_revision: 0},
    {id: 'p2', name: '合成已报销乙', reimbursement_status: 'reimbursed', reimbursement_revision: 7},
    {id: 'p3', name: '合成旧项目丙'}, // Legacy metadata defaults to unreimbursed/revision zero.
    {id: 'p4', name: '合成空账单项目丁', reimbursement_status: 'reimbursed', reimbursement_revision: 3},
  ];
  const item = (id, project, fields = {}) => ({
    id, title: `合成费用 ${id}`, project_id: project.id, project: project.name,
    category: '其他', amount_cents: 1000, amount: '10.00', stage: 'draft',
    deleted: false, verified: false, ready: false, complete: true, missing: [],
    attachments: [{id: id + '-invoice', name: '合成发票.pdf', role: 'invoice', available: true, sha256: 'synthetic-invoice-sha'}],
    events: [], version: id + '-v1', expense_date: '2026-10-10', merchant: '合成商家',
    order_number: '', note: '', invoice_state: 'received', waiting_days: 0, ...fields,
  });
  const items = [
    item('paid-fee', projects[0], {stage: 'reimbursed', verified: true, ready: true}),
    item('draft-fee', projects[1]),
    {...item('unassigned-fee', projects[0]), project_id: ''},
  ];
  const itemBaseline = clone(items);
  const state = {
    app_mode: 'invoice', logistics_available: false, projects, items,
    categories: ['其他'], inbox: [], token: 'synthetic-project-reimbursement-token',
    roles: {invoice: '发票', payment: '付款截图', detail: '明细'},
    stages: {draft: '待核对', submitted: '已提交', reimbursed: '已报销', cancelled: '已取消'},
    category_auto_ready: false, report_template_ready: true,
  };
  const queue = projects.slice(0, 2).map(p => ({id: `q-${p.id}`, project_id: p.id,
    name: `材料-${p.id}.png`, status: 'received', revision: 0, issues: [], source: 'upload', fields: {}, result: '已保存'}));
  let failure = null, failReadsAfterNextSave = false, remainingReadFailures = 0, failedStateReads = 0;
  page.on('pageerror', error => errors.push(error.message));
  await page.route(`${origin}/**`, async route => {
    const req = route.request(), url = new URL(req.url());
    const json = (data, status = 200) => route.fulfill({status, contentType: 'application/json', body: JSON.stringify(data)});
    if(url.pathname==='/api/expense-print-preview')return json({complete:false,preview_pages:[],missing:[]});
    if (req.method() === 'POST') {
      const data = req.postDataJSON();
      writes.push({path: url.pathname, data: clone(data)});
      assert.equal(url.pathname, '/api/projects/reimbursement', 'project status changes must not write any expense, material or submission API');
      assert.equal(req.headers()['x-workbench-token'], state.token);
      assert.deepEqual(Object.keys(data).sort(), ['expected_revision', 'id', 'status']);
      const project = projects.find(p => p.id === data.id);
      assert.ok(project); assert.ok(['unreimbursed', 'reimbursed'].includes(data.status));
      assert.equal(data.expected_revision, project.reimbursement_revision || 0);
      if (failure) {
        const current = failure; failure = null;
        if (current === 'conflict') project.reimbursement_revision += 1;
        return json({error: current === 'conflict' ? '合成项目状态版本冲突，请刷新后重试' : '合成项目状态保存暂时失败'}, current === 'conflict' ? 409 : 503);
      }
      project.reimbursement_status = data.status;
      project.reimbursement_revision = (project.reimbursement_revision || 0) + 1;
      if (failReadsAfterNextSave) {failReadsAfterNextSave = false; remainingReadFailures = 1;}
      return json(project);
    }
    if (url.pathname === '/api/state') {
      reads.push(url.href);
      if (remainingReadFailures > 0) {remainingReadFailures--; failedStateReads++; return json({error: '合成状态读取暂时失败'}, 503);}
      return json(state);
    }
    if (url.pathname === '/api/intake') return json({queue});
    if (url.pathname === '/api/preferences') return json({rules: [], learned: [], sections: {}});
    if (url.pathname === '/api/logistics') return json({projects, tasks: [], outbox: [], token: state.token});
    if (url.pathname === '/api/logistics/inbox') return json({groups: [], messages: [], receiver: {status: 'disconnected'}});
    if (url.pathname === '/api/material-matching') {
      const scoped = url.searchParams.get('project');
      return json({vouchers: [], payments: [], matches: [], other: queue.filter(q => !scoped || q.project_id === scoped).map(q => ({...q,
        key: 'q:' + q.id, role: 'detail', amount_cents: 1000,
        report_choice: {project_type: 'other', project_name: '合成默认名称'}, fields: {title: '合成默认名称', amount: '10.00', category: '其他'}})),
        report_types: [{code: 'other', label: '其他', names: [], editable_name: true}]});
    }
    if (url.pathname === '/api/report-snapshot') return json({rows: []});
    const name = url.pathname === '/' ? 'index.html' : url.pathname === '/logistics' ? 'logistics.html' : url.pathname.slice(1);
    if (!/^[a-z-]+\.(html|js|css)$/.test(name)) return route.abort();
    return route.fulfill({contentType: name.endsWith('.js') ? 'text/javascript' : name.endsWith('.css') ? 'text/css' : 'text/html',
      body: await fs.readFile(path.resolve(__dirname, '../src/invoice_print_layout/web', name))});
  });
  const runtimeFor = context => context===page?page:page.frames().find(f=>f.parentFrame()&&new URL(f.url()).pathname==='/');
  const selector = context => ({
    count:()=>context.locator('#projectReimbursementStatus').count(),
    isVisible:async()=>false,
    inputValue:()=>runtimeFor(context).evaluate(()=>projectReimbursementStatus(projectContext())),
    selectOption:async status=>{const runtime=runtimeFor(context),id=await runtime.evaluate(()=>projectContext().id);await context.locator(`[data-project-menu-id="${id}"]:visible`).click({button:'right'});await context.locator('#projectMenu').getByRole('menuitem',{name:status==='reimbursed'?'移到已报销':'移回未报销',exact:true}).click();}
  });
  const linkIds = context => context.locator('#projectLinks a').evaluateAll(xs => xs.map(x => new URL(x.href).searchParams.get('project')).sort());
  const categoryButton = (context, status) => context.locator(`#projectWorkbenches [data-project-status="${status}"]`);
  const unchangedExpenses = () => assert.deepEqual(items, itemBaseline, 'project reimbursement metadata never changes an expense field or attachment');
  const obsoleteSidebarGone = async context => {
    assert.equal(await context.locator('.rail > p,.rail .railnote,.rail [data-view="all"]').count(), 0);
    assert.equal(await context.locator('.rail').getByRole('link', {name: '全部项目台账', exact: true}).count(), 0);
    assert.equal(await context.locator('#tabs [data-view="all"],#tabs [data-view="missing"],#projectReimbursementStatus').count(),0,'obsolete stages and toolbar status are removed');
    assert.equal(await context.locator('[data-synthetic-project]').count(), 0, 'HTML project names remain text');
  };
  const counts = async (context, unreimbursed, reimbursed) => {
    assert.match(await categoryButton(context, 'unreimbursed').innerText(), new RegExp(`未报销.*${unreimbursed}`));
    assert.match(await categoryButton(context, 'reimbursed').innerText(), new RegExp(`已报销.*${reimbursed}`));
  };
  const waitProject = async (context, id, status, revision) => {

    const runtime = context === page ? page : page.frames().find(f => f.parentFrame() && new URL(f.url()).searchParams.get('project') === id);
    assert.ok(runtime, 'expected project frame exists');
    await runtime.waitForFunction(({id, status, revision}) => {
      const project = state.projects.find(p => p.id === id);
      return project?.reimbursement_status === status && project?.reimbursement_revision === revision && document.getElementById('projectMenu').getAttribute('aria-busy')!=='true';
    }, {id, status, revision});
    assert.equal(await selector(context).inputValue(), status);
  };
  try {
    await fs.mkdir(evidence, {recursive: true});
    await page.goto(`${origin}/?project=p1&matter=paid-fee`);
    await page.locator('[data-item="paid-fee"]').waitFor();
    await obsoleteSidebarGone(page);
    assert.equal(await selector(page).count(),0,'project status lives only in the right-click menu');
    assert.equal(await selector(page).inputValue(), 'unreimbursed', 'an all-paid expense ledger does not automatically mark the project reimbursed');
    assert.deepEqual(await linkIds(page), ['p1', 'p3']);
    await counts(page, 2, 2);
    assert.equal(await page.locator('#projectLinks a[href="/?project=p1"]').innerText(), projects[0].name + ' · 发票报销');
    const beforeCategoryClick = writes.length;
    await categoryButton(page, 'reimbursed').click();
    assert.deepEqual(await linkIds(page), ['p2', 'p4']);
    assert.equal(writes.length, beforeCategoryClick, 'browsing project categories does not save a status');
    assert.equal(await page.locator('[data-item="paid-fee"]').count(), 1, 'project navigation filtering does not filter the current expense list');
    await page.locator('#projectLinks a[href="/?project=p2"]').click();
    await page.locator('[data-item="draft-fee"]').waitFor();
    assert.equal(await selector(page).inputValue(), 'reimbursed', 'a draft expense does not automatically undo a reimbursed project status');
    assert.deepEqual(await linkIds(page), ['p2', 'p4']);
    await selector(page).selectOption('unreimbursed');
    await waitProject(page, 'p2', 'unreimbursed', 8);
    assert.deepEqual(writes.at(-1), {path: '/api/projects/reimbursement', data: {id: 'p2', status: 'unreimbursed', expected_revision: 7}});
    assert.deepEqual(await linkIds(page), ['p1', 'p2', 'p3']);
    await counts(page, 3, 1); unchangedExpenses();
    await page.reload(); await page.locator('[data-item="draft-fee"]').waitFor();
    assert.equal(await selector(page).inputValue(), 'unreimbursed', 'project status survives reload');
    await selector(page).selectOption('reimbursed');
    await waitProject(page, 'p2', 'reimbursed', 9);
    assert.deepEqual(writes.at(-1).data, {id: 'p2', status: 'reimbursed', expected_revision: 8});
    await counts(page, 2, 2); unchangedExpenses();

    await page.goto(`${origin}/?project=p1&matter=paid-fee`); await page.locator('[data-item="paid-fee"]').waitFor();
    await selector(page).selectOption('reimbursed');
    await waitProject(page, 'p1', 'reimbursed', 1);
    assert.deepEqual(writes.at(-1).data, {id: 'p1', status: 'reimbursed', expected_revision: 0});
    await counts(page, 1, 3); unchangedExpenses();
    const beforeFailureWrites = writes.length, beforeFailureReads = reads.length;
    failure = 'unavailable';
    await selector(page).selectOption('unreimbursed');
    await page.locator('#error').filter({hasText: '合成项目状态保存暂时失败'}).waitFor();
    await waitProject(page, 'p1', 'reimbursed', 1);
    assert.equal(writes.length, beforeFailureWrites + 1);
    assert.ok(reads.length > beforeFailureReads, 'failed save reloads the authoritative project state');
    unchangedExpenses();
    const beforeConflictWrites = writes.length, beforeConflictReads = reads.length;
    failure = 'conflict';
    await selector(page).selectOption('unreimbursed');
    await page.locator('#error').filter({hasText: '合成项目状态版本冲突'}).waitFor();
    await waitProject(page, 'p1', 'reimbursed', 2);
    assert.equal(writes.length, beforeConflictWrites + 1, '409 must not automatically retry');
    assert.ok(reads.length > beforeConflictReads);
    assert.equal(await selector(page).inputValue(), 'reimbursed');
    unchangedExpenses();

    // A committed POST remains displayed when its follow-up GET fails.
    const beforeReadFailureWrites = writes.length;
    failReadsAfterNextSave = true;
    await selector(page).selectOption('unreimbursed');
    await page.locator('#notice').filter({hasText: '列表刷新失败'}).waitFor();
    await waitProject(page, 'p1', 'unreimbursed', 3);
    assert.equal(writes.length, beforeReadFailureWrites + 1, 'a read failure cannot retry a committed POST');
    assert.equal(failedStateReads, 1);
    assert.deepEqual(writes.at(-1).data, {id: 'p1', status: 'unreimbursed', expected_revision: 2});
    assert.deepEqual(await linkIds(page), ['p1', 'p3'], 'committed status also redraws navigation when reloads fail');
    await counts(page, 2, 2);
    unchangedExpenses();
    await page.reload(); await page.locator('[data-item="paid-fee"]').waitFor();
    assert.equal(await selector(page).inputValue(), 'unreimbursed');
    assert.deepEqual(await linkIds(page), ['p1', 'p3']);
    await counts(page, 2, 2);

    await page.goto(origin + '/'); await page.locator('[data-item="draft-fee"]').waitFor();
    assert.ok(await selector(page).count() === 0 || !await selector(page).isVisible(), 'root page cannot change an unspecified project status');
    await obsoleteSidebarGone(page);
    assert.deepEqual(await linkIds(page), ['p1', 'p3']);
    await counts(page, 2, 2);
    await categoryButton(page, 'reimbursed').click();
    assert.deepEqual(await linkIds(page), ['p2', 'p4']);
    assert.equal(await page.evaluate(()=>state.items.length),3,'unscoped state retains every project and stage');
    assert.equal(await page.locator('[data-item]').count(),2,'unscoped review retains every unfinished draft');
    await page.screenshot({path: path.join(evidence, 'synthetic-desktop-project-groups.png'), fullPage: true});

    // The same scoped invoice UI remains usable in the workbench frame.
    await page.goto(`${origin}/?project=p2`);await page.locator('[data-item="draft-fee"]').waitFor();
    await selector(page).selectOption('unreimbursed');await waitProject(page,'p2','unreimbursed',10);
    state.app_mode = 'workbench'; state.logistics_available = true;
    await page.goto(`${origin}/logistics?project=p2&tab=invoices`);
    const panel = page.frameLocator('#invoiceFrame');
    await panel.locator('[data-item="draft-fee"]').waitFor();
    await obsoleteSidebarGone(panel);
    assert.equal(await panel.locator('.rail').isVisible(), false);
    assert.equal(await panel.locator('[data-item="paid-fee"]').count(), 0);
    assert.equal(await selector(panel).inputValue(), 'unreimbursed');
    assert.equal(await selector(panel).count(),0,'embedded toolbar also removes the duplicate status control');
    assert.deepEqual(writes.at(-1).data, {id: 'p2', status: 'unreimbursed', expected_revision: 9});
    unchangedExpenses();
    assert.equal(await panel.locator('#recognizeReceipt,#checkMail').count(), 0, 'collection remains inside the matching desk');
    await panel.locator('#materialMatching').click();
    await panel.locator('#matchingDeskDialog').waitFor({state: 'visible'});
    assert.equal(await panel.locator('#matchingUpload').isVisible(), true);
    assert.equal(await panel.locator('#matchingProject').count(), 0);
    assert.equal(await panel.locator('body').evaluate(node=>node.ownerDocument.defaultView.currentInvoiceProjectId()), 'p2');
    assert.equal(await panel.locator('[data-support-id="q-p1"]').count(), 0);
    assert.equal(await panel.locator('[data-support-id="q-p2"]').count(), 1);
    await panel.locator('#matchingClose').click();
    await page.locator('[data-project-id="p1"]').click();
    await panel.locator('#tabs [data-view="submitted"]').click();await panel.locator('[data-item="paid-fee"]').waitFor();
    assert.equal(await panel.locator('[data-item="draft-fee"]').count(), 0);
    assert.equal(await selector(panel).inputValue(), 'unreimbursed');
    await page.setViewportSize({width: 390, height: 844});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    assert.equal(await panel.locator('html').evaluate(x => x.scrollWidth <= x.clientWidth), true);
    await page.screenshot({path: path.join(evidence, 'synthetic-mobile-embedded.png'), fullPage: true});

    state.app_mode = 'invoice'; state.logistics_available = false;
    await page.goto(`${origin}/?project=p2`); await page.locator('[data-item="draft-fee"]').waitFor();
    assert.equal(await selector(page).count(),0);
    assert.equal(await selector(page).inputValue(), 'unreimbursed');
    await selector(page).selectOption('reimbursed');
    await waitProject(page, 'p2', 'reimbursed', 11);
    assert.deepEqual(writes.at(-1).data, {id: 'p2', status: 'reimbursed', expected_revision: 10});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await page.screenshot({path: path.join(evidence, 'synthetic-mobile-project-status.png'), fullPage: true});
    assert.deepEqual(errors, []); unchangedExpenses();
    assert.equal(writes.length, 8);
    assert.ok(writes.every(x => x.path === '/api/projects/reimbursement'));
    const result = {passed: true, scope: 'project status independent from expense stage; navigation cleanup, category counts, revisions, reload, 503/409 restore without retry, committed status/navigation after a GET failure, project isolation, invoice/embedded workbench, HTML escaping and 390px mobile',
      writes, state_read_count: reads.length, failed_state_read_count: failedStateReads, expenses_unchanged: true, no_live_data: true};
    await fs.writeFile(path.join(evidence, 'browser-verification.json'), JSON.stringify(result, null, 2));
    await fs.writeFile(path.join(evidence, 'browser.log'), 'PASS ' + result.scope + '\n');
    return result;
  } catch (error) {
    await fs.mkdir(evidence, {recursive: true});
    await fs.writeFile(path.join(evidence, 'browser-failure.json'), JSON.stringify({error: error.message, stack: error.stack, errors, writes, url: page.url(),
      frames: await Promise.all(page.frames().map(async f => ({url: f.url(), body: await f.locator('body').innerText()}))),
      overflow: await page.evaluate(() => ({width: innerWidth, scrollWidth: document.documentElement.scrollWidth,
        elements: [...document.querySelectorAll('body *')].map(el => ({tag: el.tagName, id: el.id, className: String(el.className),
          left: el.getBoundingClientRect().left, right: el.getBoundingClientRect().right, width: el.getBoundingClientRect().width}))
          .filter(x => x.right > innerWidth + 1 && x.width > 0).slice(0, 25)}))}, null, 2));
    await page.screenshot({path: path.join(evidence, 'synthetic-failure.png'), fullPage: true});
    throw error;
  } finally {await browser.close();}
}
module.exports = run;
if (require.main === module) run(require('playwright')).then(result => console.log(JSON.stringify(result))).catch(error => {console.error(error); process.exitCode = 1;});
