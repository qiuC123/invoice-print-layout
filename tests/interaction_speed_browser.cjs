// Request overlap and clear filtered totals, using only synthetic HTTP data.
const assert = require('node:assert/strict'), fs = require('node:fs/promises'), path = require('node:path');
async function run(pw) {
  const browser = await pw.chromium.launch({headless: true});
  const page = await browser.newPage({viewport: {width: 1400, height: 1000}});
  const origin = 'http://127.0.0.1:18859', errors = [], writes = [];
  const items = ['f1', 'f2'].map((id, index) => ({id, title: 'Synthetic ' + id, project_id: 'p1', project: 'Synthetic project',
    category: index ? '材料采购' : '餐饮', amount_cents: (index + 1) * 1000, amount: index ? '20.00' : '10.00',
    stage: 'draft', verified: false, ready: false, complete: true, missing: [], attachments: [], events: [],
    version: id + '-v1', reimbursement_person_id: 'person1', reimbursement_person_label: 'Synthetic person'}));
  const state = {app_mode: 'invoice', token: 'synthetic', projects: [{id: 'p1', name: 'Synthetic project'}], items,
    inbox: [], categories: ['餐饮', '材料采购'], roles: {}, stages: {draft: '待核对'}, category_auto_ready: false};
  const people = {project_id: 'p1', people: [{id: 'person1', label: 'Synthetic person', state: 'active', project_id: 'p1', version: 'pv1'}],
    summary: {rows: [{person_id: 'person1', label: 'Synthetic person', expense_count: 2, total_cents: 3000}], expense_count: 2, total_cents: 3000}};
  const board = {vouchers: [], payments: [], other: [], matches: [], suggestions: [], archived: [], deleted: [],
    report_types: [{code: 'other', label: '其他', names: [], editable_name: true}]};
  let hold = false, releaseBoard, releasePeople, peopleReads = 0, boardReads = 0;
  const boardGate = new Promise(resolve => {releaseBoard = resolve;}), peopleGate = new Promise(resolve => {releasePeople = resolve;});
  page.on('pageerror', error => errors.push(error.message));
  await page.route(origin + '/**', async route => {
    const req = route.request(), url = new URL(req.url()), json = value => route.fulfill({contentType: 'application/json', body: JSON.stringify(value)});
    if (req.method() === 'POST') {writes.push(url.pathname); throw Error('No interaction in this test may write business records');}
    if (url.pathname === '/api/state') return json(state);
    if (url.pathname === '/api/reimbursement-people') {peopleReads++; if (hold) await peopleGate; return json(people);}
    if (url.pathname === '/api/material-matching') {boardReads++; if (hold) await boardGate; return json(board);}
    if (url.pathname.startsWith('/api/')) return json({queue: []});
    const name = url.pathname === '/' ? 'index.html' : url.pathname.slice(1);
    if (!/^[a-z-]+\.(html|js|css)$/.test(name)) return route.abort();
    return route.fulfill({contentType: name.endsWith('.js') ? 'text/javascript' : name.endsWith('.css') ? 'text/css' : 'text/html',
      body: await fs.readFile(path.resolve(__dirname, '../src/invoice_print_layout/web', name))});
  });
  try {
    await page.goto(origin + '/?project=p1'); await page.locator('#personFilter option[value=person1]').waitFor({state: 'attached'});
    await page.locator('[data-item=f1]').waitFor();
    hold = true;
    const peopleStarted = page.waitForRequest(req => new URL(req.url()).pathname === '/api/reimbursement-people', {timeout: 2000});
    const boardStarted = page.waitForRequest(req => new URL(req.url()).pathname === '/api/material-matching', {timeout: 2000});
    await page.locator('#materialMatching').click(); await Promise.all([peopleStarted, boardStarted]);
    assert.equal(await page.locator('#matchingClose').isDisabled(), false, 'A read-only load can be dismissed while both responses remain pending');
    assert.equal(await page.locator('#matchingUpload').isDisabled(), true);
    const peopleFinished = page.waitForResponse(res => new URL(res.url()).pathname === '/api/reimbursement-people');
    releasePeople(); await peopleFinished;
    assert.equal(await page.locator('#matchingUpload').isDisabled(), true, 'Material loading remains busy until the board also arrives');
    releaseBoard(); await page.waitForFunction(() => !document.querySelector('#matchingUpload').disabled);
    await page.locator('#matchingClose').click(); hold = false;
    const readsBeforeReopen = {people: peopleReads, board: boardReads};
    await page.locator('#materialMatching').click(); await page.waitForFunction(() => !document.querySelector('#matchingUpload').disabled);
    assert.equal(peopleReads, readsBeforeReopen.people + 1); assert.equal(boardReads, readsBeforeReopen.board + 1, 'A later open rechecks current material records');
    await page.locator('#matchingClose').click();

    await page.locator('#personFilter').selectOption('__unassigned__',{force:true});
    assert.equal(await page.locator('#list [data-item]').count(), 0);
    assert.match(await page.locator('.metric[data-view=review] small').innerText(), /筛选结果/);
    assert.match(await page.locator('#list .empty').innerText(), /人员：未指定/);
    assert.match(await page.locator('#list .empty').innerText(), /本项目共有 2 笔费用，合计 ¥ 30\.00/);
    await page.locator('#search').fill('<img src=x onerror=alert(1)>');
    assert.equal(await page.locator('#list .empty img').count(), 0);
    assert.match(await page.locator('#list .empty').innerText(), /<img src=x/);
    await page.locator('#search').fill(''); await page.locator('#personFilter').selectOption('',{force:true});
    assert.equal(await page.locator('#list [data-item]').count(), 2);
    assert.doesNotMatch(await page.locator('.metric[data-view=review] small').innerText(), /筛选结果/);
    await page.locator('#tabs [data-view=submitted]').click();
    assert.match(await page.locator('#list .empty').innerText(), /还有 2 笔费用，合计 ¥ 30\.00，在“待核对”中/);
    await page.locator('#tabs [data-view=review]').click();
    await page.locator('[data-check=f1]').check(); await page.locator('[data-check=f1]').uncheck();
    await page.locator('#categoryFilter').selectOption('餐饮',{force:true}); assert.equal(await page.locator('#list [data-item]').count(), 1);
    await page.locator('#categoryFilter').selectOption('',{force:true}); assert.equal(await page.locator('#list [data-item]').count(), 2);
    assert.deepEqual(errors, []); assert.deepEqual(writes, []);
    return {passed: true, readsOverlap: true, laterOpenReadsFreshData: true, filteredScopeExplained: true,
      pendingExpensesExplained: true, filterTextEscaped: true, normalSelectionWorks: true, businessWrites: 0};
  } finally {releaseBoard(); releasePeople(); await browser.close();}
}
module.exports = run;
if (require.main === module) run(require('playwright')).then(result => console.log(JSON.stringify(result))).catch(error => {console.error(error); process.exitCode = 1;});
