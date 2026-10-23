// Real UI against synthetic intercepted batches; production records are never edited.
const assert = require('node:assert/strict'), fs = require('node:fs/promises'), path = require('node:path');
async function run(pw) {
  const browser = await pw.chromium.launch({headless: true}), page = await browser.newPage({viewport: {width: 1440, height: 1000}});
  page.setDefaultTimeout(10000);
  const origin = 'http://127.0.0.1:18841', errors = [], writes = [];
  const report_types = [{code: 'materials', label: '现场购材料', editable_name: true, names: []},
    {code: 'other', label: '其他', editable_name: true, names: []}, {code: 'living', label: '生活费', editable_name: false, names: ['住宿费(附清单)', '现场饮用水、夜宵']}];
  const fee = (id, title, cents, extra = {}) => ({id, title, project_id: 'p1', project: '批量类型测试', category: '其他',
    project_type: 'other', project_name: title, amount: (cents / 100).toFixed(2), amount_cents: cents,
    stage: 'draft', verified: false, ready: false, complete: false, missing: ['人工核对'], version: id + '-v1', expense_date: '2026-10-14',
    reimbursement_person_id: '', attachments: [{id: id + '-source', name: 'synthetic.pdf', digest: id + '-sha', role: 'invoice', available: true}], events: [], ...extra});
  const items = [fee('usb1', 'u盘甲', 1000, {project_name: '   '}), fee('usb2', 'u盘乙', 2000), fee('hidden', '隔离隐藏费用', 900),
    fee('pending', '金额待确认材料', 0, {amount_pending: true, amount: ''}), fee('verified', '已核对草稿', 700, {verified: true}),
    fee('legacy', '历史兼容费用', 800, {legacy: true}), fee('history', '历史导入费用', 800, {history_imported: true})];
  const protectedFields = Object.fromEntries(items.map(item => [item.id, JSON.stringify({title: item.title, amount_cents: item.amount_cents,
    amount_pending: item.amount_pending, verified: item.verified, stage: item.stage, expense_date: item.expense_date,
    reimbursement_person_id: item.reimbursement_person_id, attachments: item.attachments})]));
  const state = {app_mode: 'invoice', token: 'synthetic', projects: [{id: 'p1', name: '批量类型测试'}], items, inbox: [], categories: ['其他', '酒店', '材料采购'], roles: {invoice: '发票'}, stages: {draft: '待核对'}, report_types};
  let failure = '', failState = false, hold = false, release;
  page.on('pageerror', error => errors.push(error.message));
  await page.route(origin + '/**', async route => {
    const req = route.request(), url = new URL(req.url()), json = (value, status = 200) => route.fulfill({status, contentType: 'application/json', body: JSON.stringify(value)});
    if (url.pathname === '/api/state') {if (failState) {failState = false; return json({error: '列表读取失败'}, 503);} return json(state);}
    if (url.pathname === '/api/reimbursement-people') return json({project_id: 'p1', people: [], retired_people: [], summary: {rows: [], expense_count: 7, total_cents: 6200}});
    if (req.method() === 'POST') {
      const body = req.postDataJSON();
      if (url.pathname === '/api/expense-print-preview') return json({id: body.id, version: body.expected_version, preview_pages: []});
      assert.equal(url.pathname, '/api/review/batch-project-type'); writes.push(body); assert.equal(req.headers()['x-workbench-token'], state.token);
      assert.equal(body.project_id, 'p1'); assert.equal(body.ids.length, new Set(body.ids).size);
      const selected = items.filter(item => body.ids.includes(item.id)); assert.equal(selected.length, body.ids.length);
      assert.deepEqual(Object.keys(body.versions).sort(), [...body.ids].sort()); assert.ok(selected.every(item => body.versions[item.id] === item.version));
      const type = report_types.find(type => type.code === body.project_type); assert.ok(type);
      assert.deepEqual(Object.keys(body).sort(), (type.editable_name ? ['ids', 'project_id', 'project_type', 'versions'] : ['ids', 'project_id', 'project_type', 'versions', 'project_name']).sort());
      if (hold) {hold = false; await new Promise(resolve => release = resolve);}
      if (failure) {const error = failure; failure = ''; return json({error: error === '400' ? '费用版本已变化' : '保存暂时不可用'}, Number(error));}
      for (const item of selected) {
        assert.equal(item.verified, false); assert.ok(!item.legacy && !item.history_imported);
        item.project_type = body.project_type; item.project_name = type.editable_name ? typeof item.project_name === 'string' && item.project_name.trim() ? item.project_name : item.title : body.project_name;
        item.display_name = item.project_name; item.category = body.project_type === 'materials' ? '材料采购' : body.project_type === 'living' ? '酒店' : '其他';
        item.version += '-saved';
      }
      return json({items: selected, changed: selected.length, message: '项目类型已保存'});
    }
    if (url.pathname.startsWith('/api/')) return json({queue: [], suggestions: []});
    const name = url.pathname === '/' ? 'index.html' : url.pathname.slice(1);
    if (!/^[a-z-]+\.(html|js|css)$/.test(name)) return route.abort();
    return route.fulfill({contentType: name.endsWith('.js') ? 'text/javascript' : name.endsWith('.css') ? 'text/css' : 'text/html', body: await fs.readFile(path.resolve(__dirname, '../src/invoice_print_layout/web', name))});
  });
  const dialog = () => page.locator('#reviewBatchTypeDialog'), save = () => dialog().locator('[type=submit]');
  const uncheckVisible = async () => {for (const id of await page.locator('#list [data-check]:checked').evaluateAll(nodes => nodes.map(node => node.dataset.check))) await page.locator(`[data-check="${id}"]`).uncheck();};
  const sameProtected = () => {
    for (const item of items) assert.equal(JSON.stringify({title: item.title, amount_cents: item.amount_cents, amount_pending: item.amount_pending,
      verified: item.verified, stage: item.stage, expense_date: item.expense_date, reimbursement_person_id: item.reimbursement_person_id, attachments: item.attachments}), protectedFields[item.id]);
  };
  try {
    await page.goto(origin + '/?project=p1'); assert.equal(await page.locator('#batchProjectType').isDisabled(), true);
    await page.getByRole('checkbox', {name: '选择 隔离隐藏费用', exact: true}).check();
    await page.locator('#search').fill('u盘'); await page.locator('#selectVisible').check();
    await page.locator('#batchProjectType').click(); assert.match(await dialog().innerText(), /当前勾选的 2 笔/); assert.match(await dialog().innerText(), /30\.00/);
    await dialog().locator('[name=project_type]').selectOption('materials'); assert.equal(await dialog().locator('[data-fixed-detail]').isVisible(), false);
    assert.match(await dialog().locator('[data-target]').innerText(), /每笔保留上表各自名称/); assert.equal(await save().isEnabled(), true);
    failure = '503'; await save().click(); await dialog().locator('[data-error]').filter({hasText: '暂时不可用'}).waitFor();
    assert.equal(await dialog().locator('[name=project_type]').inputValue(), 'materials'); assert.equal(items[0].category, '其他');
    hold = true; await save().click(); await dialog().locator('form').evaluate(form => form.requestSubmit());
    assert.equal(writes.length, 2); failState = true; release(); await dialog().locator('[data-refresh-confirmed]').waitFor();
    assert.equal(await save().isDisabled(), true); const posted = writes.length;
    await dialog().locator('[data-refresh-confirmed]').click(); await dialog().waitFor({state: 'detached'}); assert.equal(writes.length, posted);
    assert.deepEqual(writes[0].ids.sort(), ['usb1', 'usb2']); assert.equal(items[2].project_type, 'other'); assert.equal(items[0].project_name, 'u盘甲');
    assert.equal(await page.locator('#list input:checked').count(), 0); await page.locator('#search').fill('隔离隐藏'); assert.equal(await page.locator('#list input:checked').count(), 1);
    await uncheckVisible(); sameProtected();
    await page.locator('#search').fill('金额待确认'); await page.locator('#selectVisible').check(); await page.locator('#batchProjectType').click();
    assert.match(await dialog().innerText(), /另有 1 笔金额待确认/); await dialog().locator('[name=project_type]').selectOption('living');
    assert.equal(await save().isDisabled(), true); assert.equal(await dialog().locator('[data-fixed-detail]').isVisible(), true);
    await dialog().locator('[name=project_name]').selectOption('住宿费(附清单)'); await save().click(); await dialog().waitFor({state: 'detached'});
    assert.equal(items[3].title, '金额待确认材料'); assert.equal(items[3].project_name, '住宿费(附清单)'); assert.equal(items[3].amount_pending, true); sameProtected();
    await page.locator('#search').fill('u盘'); await page.locator('#selectVisible').check(); await page.locator('#batchProjectType').click();
    await dialog().locator('[name=project_type]').selectOption('other'); failure = '400'; await save().click();
    await dialog().locator('[data-error]').filter({hasText: '版本已变化'}).waitFor(); assert.equal(await save().isDisabled(), true);
    assert.equal(await dialog().locator('[name=project_type]').inputValue(), 'other'); const staleCount = writes.length;
    await dialog().locator('form').evaluate(form => form.requestSubmit()); assert.equal(writes.length, staleCount);
    await dialog().getByRole('button', {name: '取消', exact: true}).click(); await uncheckVisible();
    for (const query of ['已核对草稿', '历史兼容费用', '历史导入费用']) {
      await page.locator('#search').fill(query); await page.locator('#selectVisible').check(); assert.equal(await page.locator('#batchProjectType').isDisabled(), true); await uncheckVisible();
    }
    await page.setViewportSize({width: 390, height: 844}); await page.locator('#search').fill('u盘'); await page.locator('#selectVisible').check();
    await page.locator('#batchProjectType').click(); await dialog().locator('[name=project_type]').selectOption('materials');
    assert.equal(await dialog().evaluate(node => node.scrollWidth <= node.clientWidth), true); await page.keyboard.press('Escape'); await dialog().waitFor({state: 'detached'});
    await page.locator('#tabs [data-view=submitted]').click(); assert.equal(await page.locator('#batchProjectType').isVisible(), false);
    sameProtected(); assert.deepEqual(errors, []);
    return {passed: true, scope: 'visible checked only with hidden checked retained; materials preserve individual names/amounts/people/originals; fixed detail explicit; pending amount retained; whole invalid refusal; 503/busy/stale400 retention; committed GET retry never reposts; keyboard/narrow screen', posts: writes.length};
  } finally {await browser.close();}
}
module.exports = run;
if (require.main === module) run(require('playwright')).then(console.log).catch(error => {console.error(error); process.exitCode = 1;});
