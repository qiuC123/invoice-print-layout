// Real frontend with synthetic responses; never touches the live workbench.
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');

async function run(playwright) {
  const browser = await playwright.chromium.launch({headless: true});
  const page = await browser.newPage({viewport: {width: 1500, height: 1000}});
  page.setDefaultTimeout(10000);
  const origin = 'http://127.0.0.1:18789';
  const errors = [], writes = [], previews = [];
  const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j1ioAAAAASUVORK5CYII=', 'base64');
  const item = (id, fields = {}) => ({
    id, title: `合成费用 ${id}`, project: '合成测试项目', project_id: 'p1',
    category: '酒店', amount_cents: 10000, stage: 'draft', complete: true,
    verified: false, ready: false, attachments: [], missing: [], events: [],
    version: `${id}-v1`, expense_date: '2026-10-13', merchant: '合成宾馆',
    invoice_state: 'received', waiting_days: 0, ...fields,
  });
  const state = {
    app_mode: 'invoice', logistics_available: false, items: [
      item('review'), item('requested', {complete: false, review_requested: true, missing: ['付款截图']}),
      item('missing', {complete: false, missing: ['发票']}),
      item('ready', {verified: true, ready: true}),
      item('submitted', {stage: 'submitted', verified: true, ready: true}),
      item('reimbursed', {stage: 'reimbursed', verified: true, ready: true}),
      item('deleted', {deleted: true, verified: true, ready: true}),
      item('cancelled', {stage:'cancelled'}),
      item('merged', {stage:'cancelled',merged_into:'ready',attachments:[{id:'old-proof',name:'original.pdf',role:'invoice',available:true}]}),
    ], inbox: [], projects: [
      {id: 'p1', name: '合成测试项目', reimbursement_status: 'unreimbursed'},
      {id: 'p2', name: '合成历史项目', reimbursement_status: 'reimbursed'},
    ], categories: ['酒店', '其他'], roles: {invoice: '发票', payment: '付款截图'},
    stages: {draft: '待核对', submitted: '已提交', reimbursed: '已报销',cancelled:'已取消'},
    token: 'synthetic', report_template_ready: true, category_auto_ready: false,
  };
  page.on('pageerror', error => errors.push(error.message));
  await page.route(`${origin}/**`, async route => {
    const request = route.request(), url = new URL(request.url());
    const json = body => route.fulfill({contentType: 'application/json', body: JSON.stringify(body)});
    if (request.method() === 'POST') {
      const body = request.postDataJSON();
      if (url.pathname === '/api/expense-print-preview') {
        previews.push(body.id);
        const x = state.items.find(value => value.id === body.id);
        assert.equal(body.expected_version, x.version);
        return json({id: x.id, version: x.version, complete: x.complete, missing: x.missing,
          partial: !x.complete, pdf: `${x.id}.pdf`, preview_pages: [`${x.id}.png`],
          page_count: 1, layout: '发票／收据＋付款截图'});
      }
      if (url.pathname === '/api/transition') {
        writes.push(body);
        assert.equal(body.action, 'verify');
        const x = state.items.find(value => value.id === body.id);
        assert.equal(body.expected_version, x.version);
        Object.assign(x, {verified: true, ready: true, version: `${x.id}-v2`});
        return json(x);
      }
      throw Error(`Unexpected ledger write: ${url.pathname}`);
    }
    if (url.pathname === '/api/state') return json(state);
    if (url.pathname === '/api/intake') return json({queue: []});
    if (url.pathname === '/api/material-matching') return json({vouchers: [], payments: [], other: [], matches: [], suggestions: [], report_types: []});
    if (url.pathname === '/api/preferences') return json({rules: [], learned: [], sections: {}});
    if (url.pathname === '/api/report-snapshot') return json({rows: []});
    if (url.pathname.startsWith('/export/')) return route.fulfill({contentType: 'image/png', body: png});
    const name = url.pathname === '/' ? 'index.html' : url.pathname.slice(1);
    if (!/^[a-z-]+\.(html|js|css)$/.test(name)) return route.abort();
    return route.fulfill({contentType: name.endsWith('.js') ? 'text/javascript' : name.endsWith('.css') ? 'text/css' : 'text/html',
      body: await fs.readFile(path.resolve(__dirname, '../src/invoice_print_layout/web', name))});
  });
  const tab = value => page.locator(`#tabs [data-view="${value}"]`);
  const ids = () => page.locator('#list [data-item]').evaluateAll(rows => rows.map(row => row.dataset.item).sort());
  const select = id => page.locator(`[data-item="${id}"] .title`).click();
  const image = id => page.waitForFunction(value => document.querySelector('#expensePrintPages img')?.getAttribute('src') === `/export/${value}.png`, id);
  try {
    await page.goto(`${origin}/?project=p1`);
    await page.locator('[data-item="review"]').waitFor();
    assert.equal(await tab('submitted').innerText(), '已核对');
    assert.equal(await tab('reimbursed').count(), 0, 'only the expense reimbursed tab is removed');
    assert.equal(await page.locator('[data-project-status="reimbursed"]').innerText(), '已报销 1', 'project classification survives');
    assert.equal(await page.locator('#projectReimbursementStatus,#requestReviewSelected').count(), 0);
    assert.equal(await tab('all').count(),0);assert.equal(await tab('missing').count(),0);
    assert.equal(await page.locator('[data-view="synthesis"],#synthesisSelectionTools').count(), 0);
    assert.equal(await page.locator('.metric').count(), 2);
    const metricWidths = await page.locator('.metric').evaluateAll(nodes => nodes.map(node => node.getBoundingClientRect().width));
    assert.ok(Math.abs(metricWidths[0] - metricWidths[1]) < 1, 'Both financial stage cards fill equal columns');

    await tab('review').click();
    assert.deepEqual(await ids(), ['missing', 'requested', 'review'], 'every incomplete or unverified draft remains in review');
    await select('missing');await image('missing');
    await page.locator('#detail .print-materials summary').click();
    assert.equal(await page.locator('#uploadButton').isVisible(),true,'missing materials are supplemented inside the existing detail');
    await tab('deleted').click();assert.deepEqual(await ids(),['cancelled','deleted','merged']);
    await select('merged');assert.equal(await page.locator('#detail a[href="/file/old-proof"]').count(),1);
    assert.equal(await page.locator('[data-restore-item="cancelled"],[data-restore-item="merged"],#detail [data-action],#detail #uploadButton').count(),0,'cancelled and merged sources stay readonly without a false restore action');
    await tab('submitted').click();
    assert.deepEqual(await ids(), ['ready', 'reimbursed', 'submitted'], 'ready drafts and old historical bills are retained');
    await page.locator('#search').fill('没有这样的费用');
    assert.match(await page.locator('#list').innerText(), /当前筛选下暂无费用.*搜索：没有这样的费用.*本项目共有 6 笔费用/s);
    await page.locator('#search').fill('');
    assert.equal(await page.locator('#verifySelected').isVisible(), false);
    assert.equal(await page.locator('#exportSelected').isVisible(), true);
    await select('ready');
    await image('ready');
    assert.equal(await page.locator('#expensePrintPdf').getAttribute('href'), '/export/ready.pdf');
    assert.match(await page.locator('#detail').innerText(), /最终打印预览/);
    await page.locator('#makeExpenseReport').click();
    await page.locator('#reportDialog[open]').waitFor();
    assert.match(await page.locator('#reportScope').innerText(), /已选择 1 笔/);
    await page.locator('#closeReport').click();
    for (const id of ['submitted', 'reimbursed']) {
      await select(id);await image(id);
      assert.equal(await page.locator('#detail [data-action="reimbursed"]').count(), 0, 'no expense mark-reimbursed action');
      assert.equal(await page.locator('#makeExpenseReport').count(), 0, 'old package cannot be accidentally produced again');
      assert.match(await page.locator('#detail').innerText(), /历史费用保留/);
    }
    assert.deepEqual(writes, [], 'viewing final previews and export dialog is read-only');

    await page.locator('#materialMatching').click();
    await page.locator('#matchingDeskDialog[open]').waitFor();
    assert.equal(await tab('submitted').getAttribute('class'), 'active', 'Opening materials does not replace the ledger stage');
    await page.locator('#matchingClose').click();
    assert.deepEqual(writes, [], 'Opening the material desk does not create or verify a bill');

    await tab('review').click();await select('review');await image('review');
    await page.locator('#detail [data-action="verify"]').click();
    await page.waitForFunction(() => document.querySelector('#tabs [data-view="submitted"]')?.classList.contains('active'));
    assert.equal(await page.locator('#list [data-item].selected').count(), 0, 'Completed fees do not retain preview focus in the submission view');
    assert.equal(await page.locator('[data-check="review"]').isChecked(), false);
    await select('review');
    await image('review');
    assert.deepEqual(await ids(), ['ready', 'reimbursed', 'review', 'submitted']);
    assert.match(await page.locator('#notice').innerText(), /核对完成.*已核对/);
    assert.equal(state.items.find(x => x.id === 'review').stage, 'draft', 'UI view does not rewrite stored stage');
    await tab('review').click();assert.deepEqual(await ids(), ['missing','requested']);
    await tab('submitted').click();
    await page.setViewportSize({width: 390, height: 844});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    assert.equal(writes.length, 1, 'only the explicitly clicked verify made a write');
    state.items.push(item('created', {category: '其他', title: '新创建的费用'}));
    await page.evaluate(async () => {await load();setExpenseFilterValues('categoryFilter', ['酒店']);setExpenseFilterValues('issueFilter', ['missing']);document.getElementById('search').value='挡住新费用';render();});
    await page.evaluate(() => window.showCreatedMaterialExpenses([{id: 'created'}]));
    assert.equal(await tab('review').getAttribute('class'), 'active');
    assert.equal(await page.locator('[data-item=created]').getAttribute('class'), 'row selected');
    assert.equal(await page.locator('[data-check]:checked').count(), 0);
    assert.deepEqual(await page.evaluate(() => ['categoryFilter','personFilter','issueFilter'].map(expenseFilterValues)), [[],[],[]]);
    assert.equal(await page.locator('#search').inputValue(), '');
    assert.match(await page.locator('#notice').innerText(), /清空筛选和搜索/);
    await page.evaluate(() => {setExpenseFilterValues('categoryFilter', ['其他']);document.getElementById('search').value='新创建';render();window.showCreatedMaterialExpenses([{id:'created'}]);});
    assert.deepEqual(await page.evaluate(() => expenseFilterValues('categoryFilter')), ['其他']);
    assert.equal(await page.locator('#search').inputValue(), '新创建', 'A compatible existing filter is retained');
    await page.goto(`${origin}/?project=p1&view=synthesis`);
    await page.locator('#matchingDeskDialog[open]').waitFor();
    assert.equal(await tab('review').getAttribute('class'), 'active', 'Legacy synthesis URL resolves to the material desk over review');
    await page.locator('#matchingClose').click();
    assert.equal(writes.length, 1, 'Stage compatibility and focus helpers do not write ledger data');
    state.items.push(item('held-returned', {returned_to_synthesis: true, amount_cents: 999999}));
    await page.goto(`${origin}/?project=p1&matter=held-returned`);
    await page.locator('#matchingDeskDialog[open]').waitFor();
    assert.equal(await page.locator('[data-item=held-returned]').count(), 0, 'A legacy returned-matter link opens materials without re-registering that source');
    assert.doesNotMatch(await page.locator('#metrics').innerText(), /9,999\.99/);
    await page.locator('#matchingClose').click();
    assert.equal(writes.length, 1);
    state.items.push(item('historical-project', {project_id:'p2', verified:true, ready:true}));
    await page.goto(origin + '/');
    await page.locator('.metric[data-view="submitted"]').waitFor();
    const metric = page.locator('.metric[data-view="submitted"]');
    assert.equal(await metric.locator('b').innerText(), '¥ 200.00', 'only two reviewed unexported drafts in a pending project count');
    assert.match(await metric.innerText(), /已报销项目中另有 1 笔已核对未生成报销包/);
    await metric.click();
    assert.deepEqual(await page.locator('#list [data-item]').evaluateAll(rows=>rows.slice(0,2).map(x=>x.dataset.item)), ['review','ready']);
    assert.match(await page.locator('[data-item="historical-project"]').innerText(), /项目已报销/);
    assert.equal(await page.evaluate(()=>reportEligible(state.items.find(x=>x.id==='historical-project'))),true,'package eligibility remains unchanged');
    assert.deepEqual(errors, []);
    return {passed: true, scope: 'synthetic browser only', reviewSplit: true, preservedHistoricalBills: true,
      finalPrintPreview: true, materialDeskEntry: true, createdExpenseFocus: true, legacySynthesisRedirect: true,
      twoEqualMetricCards: true, projectReimbursementLabels: true, previewIds: previews};
  } finally {await browser.close();}
}
module.exports = run;
if (require.main === module) run(require('playwright')).then(console.log).catch(error => {console.error(error);process.exitCode = 1;});
