const assert = require('node:assert/strict'), fs = require('node:fs/promises'), path = require('node:path');
async function run(pw) {
  const browser = await pw.chromium.launch({headless: true}), page = await browser.newPage();
  const origin = 'http://127.0.0.1:18929', errors = [], writes = [];
  const people = [{id: 'first', label: '合成人员甲'}, {id: 'second', label: '合成人员乙'}].map(person => ({...person, project_id: 'p1', state: 'active', version: person.id + '-v1'}));
  let main = '', revision = 0, conflict = false;
  page.on('pageerror', error => errors.push(error.message));
  await page.route(origin + '/**', async route => {
    const request = route.request(), url = new URL(request.url());
    const json = (body, status = 200) => route.fulfill({status, contentType: 'application/json', body: JSON.stringify(body)});
    if (url.pathname === '/api/state') return json({app_mode: 'invoice', token: 'synthetic-token', projects: [{id: 'p1', name: '合成项目'}], items: [], inbox: [], categories: ['其他'], roles: {}, stages: {draft: '待核对'}});
    if (url.pathname === '/api/reimbursement-people') return json({project_id: 'p1', people, main_person_id: main, project_revision: revision, summary: {rows: [], total_cents: 0, expense_count: 0}});
    if (request.method() === 'POST') {
      assert.equal(url.pathname, '/api/reimbursement-people/main-person');
      assert.equal(request.headers()['x-workbench-token'], 'synthetic-token');
      const body = request.postDataJSON(); writes.push(body);
      assert.equal(body.project_id, 'p1'); assert.equal(body.expected_revision, revision);
      if (body.person_id) assert.equal(body.expected_version, people.find(person => person.id === body.person_id).version);
      else assert.equal(body.expected_version, undefined);
      if (conflict) {conflict = false; return json({error: '发票项目已变化，请刷新后重试'}, 409);}
      main = body.person_id; revision++;
      return json({project_id: 'p1', main_person_id: main, project_revision: revision, message: '主负责人已保存。'});
    }
    if (url.pathname.startsWith('/api/')) return json({queue: []});
    const file = url.pathname === '/' ? 'index.html' : url.pathname.slice(1);
    if (!/^[a-z-]+\.(html|js|css)$/.test(file)) return route.abort();
    return route.fulfill({contentType: file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html', body: await fs.readFile(path.resolve(__dirname, '../src/invoice_print_layout/web', file))});
  });
  const panel = () => page.locator('#reimbursementPeopleDialog'), row = id => panel().locator(`[data-person-row="${id}"]`);
  try {
    await page.goto(origin + '/?project=p1'); await page.locator('#reimbursementPeople').click();
    await row('first').getByRole('button', {name: '设为主负责人', exact: true}).click();
    await row('first').getByText('主负责人', {exact: true}).waitFor();
    assert.equal(main, 'first');
    await row('second').getByRole('button', {name: '设为主负责人', exact: true}).click();
    await row('second').getByText('主负责人', {exact: true}).waitFor();
    assert.equal(await row('first').getByText('主负责人', {exact: true}).count(), 0);
    await row('second').getByRole('button', {name: '取消主负责人', exact: true}).click();
    await row('second').getByRole('button', {name: '设为主负责人', exact: true}).waitFor();
    assert.equal(main, '');
    conflict = true;
    await row('first').getByRole('button', {name: '设为主负责人', exact: true}).click();
    await row('first').locator('[data-main-person-error]').filter({hasText: '已变化'}).waitFor();
    assert.equal(await row('first').locator('[data-main-person]').isDisabled(), true);
    assert.equal(writes.length, 4); assert.deepEqual(errors, []);
    return {passed: true, scope: 'main person set/change/clear and stale project conflict in real people UI', writes: writes.length};
  } finally {await browser.close();}
}
module.exports = run;
if (require.main === module) run(require('playwright')).then(console.log).catch(error => {console.error(error); process.exitCode = 1;});
