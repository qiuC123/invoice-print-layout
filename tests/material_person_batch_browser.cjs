// Owners are chosen for each expense; material selection never assigns the original materials.
const assert = require('node:assert/strict'), fs = require('node:fs/promises'), path = require('node:path');
async function run(pw) {
  const browser = await pw.chromium.launch({headless: true}), page = await browser.newPage({viewport: {width: 1450, height: 1050}});
  page.setDefaultTimeout(10000);
  const origin = 'http://127.0.0.1:18830', errors = [], writes = [];
  const people = [{id: 'alpha', label: '测试人员甲', project_id: 'p1', state: 'active', version: 'alpha-v1'},
    {id: 'beta', label: '测试人员乙', project_id: 'p1', state: 'active', version: 'beta-v1'}];
  const item = {id: 'existing', title: '已有待核对费用', project_id: 'p1', project: '批量材料测试', category: '其他', amount_cents: 500,
    amount: '5.00', stage: 'draft', verified: false, ready: false, complete: false, version: 'existing-v1', missing: ['付款截图'], attachments: [], events: []};
  const state = {app_mode: 'invoice', token: 'synthetic', projects: [{id: 'p1', name: '批量材料测试'}], items: [item], inbox: [], categories: ['其他'], roles: {invoice: '发票'}, stages: {draft: '待核对'}};
  const card = (id, role) => ({id, key: 'q:' + id, name: '隔离材料' + id + '.pdf', project_id: 'p1', revision: 2, status: 'review', item_id: '', role,
    amount_cents: 1000, report_choice: {project_type: 'other', project_name: '识别名称'}, issues: []});
  const invoice = card('invoice', 'invoice'), payment = card('payment', 'payment'), support = card('support', 'other');
  const existing = {...card('existing', 'invoice'), key: 'e:existing', version: item.version, revision: undefined};
  const board = {vouchers: [invoice, existing], payments: [payment], other: [support], archived: [], deleted: [], matches: [], suggestions: [], report_types: [{code: 'other', label: '其他', editable_name: true, names: []}]};
  let failWrite = true, creations = 0;
  page.on('pageerror', error => errors.push(error.message));
  await page.route(origin + '/**', async route => {
    const req = route.request(), url = new URL(req.url()), json = (value, status = 200) => route.fulfill({status, contentType: 'application/json', body: JSON.stringify(value)});
    if (url.pathname === '/api/state') return json(state);
    if (url.pathname === '/api/reimbursement-people') return json({project_id: 'p1', people, retired_people: [], summary: {rows: [{person_id: '', label: '未指定', expense_count: 1, total_cents: 500}], expense_count: 1, total_cents: 500}});
    if (url.pathname === '/api/material-matching') return json(board);
    if (req.method() === 'POST') {
      const body = req.postDataJSON(); writes.push({path: url.pathname, body});
      if (url.pathname === '/api/expense-print-preview') return json({id: item.id, version: item.version, preview_pages: [], missing: item.missing});
      assert.equal(url.pathname, '/api/material-matching/create', 'owner choice is part of creating an expense');
      assert.equal(req.headers()['x-workbench-token'], state.token); assert.equal(body.project_id, 'p1');
      assert.deepEqual(body.voucher_keys, ['q:invoice']); assert.deepEqual(body.payment_ids, ['payment']); assert.deepEqual(body.support_ids, ['support']);
      assert.deepEqual(body.revisions, {invoice: 2, payment: 2, support: 2}); assert.equal(body.fields.reimbursement_person_id, 'beta');
      assert.equal(body.fields.amount, '23.45'); assert.equal(body.fields.project_name, '用户尚未保存的名称');
      if (failWrite) {failWrite = false; return json({error: '合成服务暂时不可用'}, 503);}
      creations++; assert.equal(creations, 1);
      const created = {...item, id: 'created', title: body.fields.project_name, amount_cents: 2345, amount: '23.45', reimbursement_person_id: 'beta'};
      state.items.push(created); board.vouchers = [existing]; board.payments = []; board.other = [];
      return json({item: created});
    }
    if (url.pathname.startsWith('/api/')) return json({queue: []});
    const name = url.pathname === '/' ? 'index.html' : url.pathname.slice(1);
    if (!/^[a-z-]+\.(html|js|css)$/.test(name)) return route.abort();
    return route.fulfill({contentType: name.endsWith('.js') ? 'text/javascript' : name.endsWith('.css') ? 'text/css' : 'text/html', body: await fs.readFile(path.resolve(__dirname, '../src/invoice_print_layout/web', name))});
  });
  try {
    await page.goto(origin + '/?project=p1'); await page.locator('#materialMatching').click();
    assert.equal(await page.locator('#matchingBatchPerson, #matchingAssignPeople').count(), 0);
    assert.equal(await page.evaluate(() => typeof window.reimbursementPeopleUI.bulkAssignMaterials), 'undefined');
    await page.locator('[data-voucher-key="q:invoice"]').click(); await page.locator('[data-payment-id="payment"]').click(); await page.locator('[data-support-id="support"]').click();
    await page.locator('#matchingProjectType').selectOption('other'); await page.locator('#matchingProjectName').fill('用户尚未保存的名称'); await page.locator('#matchingAmount').fill('23.45');
    const person = page.locator('#matchingReimbursementPerson');
    assert.equal(await person.inputValue(), '');
    await person.selectOption('alpha'); await person.selectOption('beta');
    assert.equal(writes.filter(write => !write.path.endsWith('/expense-print-preview')).length, 0, 'selection does not mutate materials');
    assert.equal(await page.locator('#matchingProjectName').inputValue(), '用户尚未保存的名称'); assert.equal(await page.locator('#matchingAmount').inputValue(), '23.45');
    await page.locator('#matchingSave').click(); await page.locator('#matchingMessage').filter({hasText: '暂时不可用'}).waitFor();
    assert.equal(creations, 0); assert.equal(await person.inputValue(), 'beta');
    assert.equal(await page.locator('#matchingVouchers .matching-card.selected').count(), 1);
    assert.equal(await page.locator('#matchingPayments .matching-card.selected').count(), 1);
    assert.equal(await page.locator('#matchingOther .matching-card.selected').count(), 1);
    assert.equal(await page.locator('#matchingProjectName').inputValue(), '用户尚未保存的名称'); assert.equal(await page.locator('#matchingAmount').inputValue(), '23.45');
    await page.locator('#matchingSave').click(); await page.locator('#matchingDeskDialog').waitFor({state: 'hidden'});
    assert.equal(creations, 1); assert.equal(writes.filter(write => write.path.endsWith('/create')).length, 2);
    assert.ok(!writes.some(write => write.path.endsWith('/assign-materials')));
    assert.ok([invoice, payment, support].every(material => material.revision === 2 && !material.reimbursement_person_id));
    assert.equal(item.amount_cents, 500); assert.equal(item.verified, false); assert.equal(state.items.length, 2);
    assert.deepEqual(errors, []);
    return {passed: true, scope: 'removed batch/material assignment controls; per-expense owner choice preserves selections and fields; failed creation keeps input; explicit retry creates once; source material owners unchanged'};
  } finally {await browser.close();}
}
module.exports = run;
if (require.main === module) run(require('playwright')).then(console.log).catch(error => {console.error(error); process.exitCode = 1;});
