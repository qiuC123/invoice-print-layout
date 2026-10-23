// Material selection survives an atomic owner change; intake assignment never creates a fee.
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
  let failWrite = false, failRead = false, assignments = 0;
  page.on('pageerror', error => errors.push(error.message));
  await page.route(origin + '/**', async route => {
    const req = route.request(), url = new URL(req.url()), json = (value, status = 200) => route.fulfill({status, contentType: 'application/json', body: JSON.stringify(value)});
    if (url.pathname === '/api/state') return json(state);
    if (url.pathname === '/api/reimbursement-people') return json({project_id: 'p1', people, retired_people: [], summary: {rows: [{person_id: '', label: '未指定', expense_count: 1, total_cents: 500}], expense_count: 1, total_cents: 500}});
    if (url.pathname === '/api/material-matching') {if (failRead) {failRead = false; return json({error: '材料刷新暂时失败'}, 503);} return json(board);}
    if (req.method() === 'POST') {
      const body = req.postDataJSON(); writes.push({path: url.pathname, body});
      if (url.pathname === '/api/expense-print-preview') return json({id: item.id, version: item.version, preview_pages: [], missing: item.missing});
      assert.equal(url.pathname, '/api/reimbursement-people/assign-materials', 'owner edits must not create, merge or verify expenses');
      assert.equal(req.headers()['x-workbench-token'], state.token); assert.equal(body.project_id, 'p1');
      assert.deepEqual(body.ids.sort(), ['invoice', 'payment', 'support']);
      assert.deepEqual(body.revisions, {invoice: 2, payment: 2, support: 2}); assert.equal(body.person_id, 'alpha'); assert.equal(body.target_version, 'alpha-v1');
      if (failWrite) {failWrite = false; return json({error: '归属服务暂时不可用'}, 503);}
      assignments++; assert.equal(assignments, 1);
      for (const material of [invoice, payment, support]) {material.revision++; material.reimbursement_person_id = 'alpha'; material.reimbursement_person_label = people[0].label;}
      failRead = true;
      return json({entries: [invoice, payment, support], person: people[0], changed: 3, message: '三份材料归属已保存'});
    }
    if (url.pathname.startsWith('/api/')) return json({queue: []});
    const name = url.pathname === '/' ? 'index.html' : url.pathname.slice(1);
    if (!/^[a-z-]+\.(html|js|css)$/.test(name)) return route.abort();
    return route.fulfill({contentType: name.endsWith('.js') ? 'text/javascript' : name.endsWith('.css') ? 'text/css' : 'text/html', body: await fs.readFile(path.resolve(__dirname, '../src/invoice_print_layout/web', name))});
  });
  try {
    await page.goto(origin + '/?project=p1'); await page.locator('#materialMatching').click();
    const entry = page.locator('#matchingAssignPeople'); assert.equal(await entry.isDisabled(), true);
    await page.locator('[data-voucher-key="q:invoice"]').click(); await page.locator('[data-payment-id="payment"]').click(); await page.locator('[data-support-id="support"]').click();
    await page.locator('#matchingProjectType').selectOption('other'); await page.locator('#matchingProjectName').fill('用户尚未保存的名称'); await page.locator('#matchingAmount').fill('23.45');
    assert.equal(await entry.isEnabled(), true); assert.match(await entry.innerText(), /3份/);
    await page.locator('[data-voucher-key="e:existing"]').click(); assert.equal(await entry.isDisabled(), true); assert.match(await entry.getAttribute('title'), /待核对列表/);
    await page.locator('[data-voucher-key="e:existing"]').click();
    await page.locator('#matchingProjectType').selectOption('other'); await page.locator('#matchingProjectName').fill('用户尚未保存的名称'); await page.locator('#matchingAmount').fill('23.45');
    await entry.click(); const bulk = page.locator('#reimbursementPeopleBulk'); await bulk.waitFor();
    assert.match(await bulk.innerText(), /不创建费用/); await bulk.locator('[name=person_id]').selectOption('alpha');
    failWrite = true; await bulk.locator('[type=submit]').click(); await bulk.locator('[data-error]').filter({hasText: '暂时不可用'}).waitFor();
    assert.equal(await bulk.locator('[name=person_id]').inputValue(), 'alpha'); assert.equal(await bulk.locator('[type=submit]').isEnabled(), true); assert.equal(invoice.revision, 2);
    await bulk.locator('[type=submit]').click(); await bulk.locator('[data-refresh-confirmed]').waitFor();
    assert.match(await bulk.locator('[data-error]').innerText(), /归属已保存/); assert.equal(await bulk.locator('[type=submit]').isDisabled(), true);
    await bulk.locator('[data-refresh-confirmed]').click(); await bulk.waitFor({state: 'detached'});
    assert.equal(assignments, 1); assert.equal(writes.filter(write => write.path.endsWith('/assign-materials')).length, 2);
    assert.equal(await page.locator('#matchingVouchers .matching-card.selected').count(), 1);
    assert.equal(await page.locator('#matchingPayments .matching-card.selected').count(), 1);
    assert.equal(await page.locator('#matchingOther .matching-card.selected').count(), 1);
    assert.equal(await page.locator('#matchingProjectName').inputValue(), '用户尚未保存的名称'); assert.equal(await page.locator('#matchingAmount').inputValue(), '23.45');
    assert.equal(await page.locator('#matchingReimbursementPerson').inputValue(), 'alpha'); assert.equal(await entry.isEnabled(), true);
    assert.equal(item.amount_cents, 500); assert.equal(state.items.length, 1); assert.equal(item.verified, false);
    await entry.click(); await page.locator('#reimbursementPeopleBulk [data-close]').first().click(); assert.equal(assignments, 1);
    assert.equal(await page.locator('#matchingProjectName').inputValue(), '用户尚未保存的名称'); assert.deepEqual(errors, []);
    return {passed: true, scope: 'unlinked invoice/payment/support only; linked expense disabled; 503 retains choices; committed read retry never reposts; revisions advance without losing selected materials or entered fields; no fee creation'};
  } finally {await browser.close();}
}
module.exports = run;
if (require.main === module) run(require('playwright')).then(console.log).catch(error => {console.error(error); process.exitCode = 1;});
