// Exercise versioned material archive HTTP contracts against the real browser UI.
// All documents, projects and responses are synthetic; no production writes.
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');

async function run(pw) {
  const browser = await pw.chromium.launch({headless: true});
  const page = await browser.newPage({viewport: {width: 1450, height: 1000}});
  page.setDefaultTimeout(10000);
  const origin = 'http://127.0.0.1:18797', errors = [], writes = [];
  const evidence = process.env.INVOICE_BROWSER_EVIDENCE ? path.resolve(process.env.INVOICE_BROWSER_EVIDENCE) :
    path.resolve(__dirname, '../workspace/invoice-operations/material-archive-20261013');
  const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j1ioAAAAASUVORK5CYII=', 'base64');
  const card = (id, role, extra = {}) => ({id, key: 'q:' + id, name: '合成材料 ' + id + '.png',
    project_id: 'p1', role, status: 'review', revision: 1, amount_cents: 1000, issues: [],
    thumbnail: '/intake-file/' + id, url: '/intake-file/' + id, fields: {merchant: '合成商家'},
    report_choice: {project_type: 'other', project_name: '合成材料 ' + id}, can_archive: true,
    can_restore: false, can_delete: false, can_restore_deleted: false, ...extra});
  const cards = [card('v1', 'invoice'), card('v2', 'receipt'), card('payment', 'payment'), card('ordinary', 'other'),
    card('qr', 'other', {qr_status: 'manual', issues: ['待领票']}),
    card('rail', 'payment', {status: 'unused', can_archive: false, can_delete: true,
      unused_reason: '高铁报销只需12306发票，支付截图无需参与合成。'}),
    card('unsafe', 'invoice', {name: '<img src=x onerror="window.archiveXss=1">.pdf',
      fields: {merchant: '<script>window.archiveXss=2</script>'}, thumbnail: 'javascript:alert(1)', url: '//attacker.invalid/file'})];
  const existing = {key: 'e:expense', id: 'expense', name: '已有费用', role: 'invoice', amount_cents: 5000,
    project_id: 'p1', version: 'ev1', report_choice: {project_type: 'other', project_name: '已有费用'}};
  const state = {app_mode: 'invoice', token: 'synthetic', projects: [{id: 'p1', name: '合成归档项目'}],
    items: [], inbox: [], categories: ['其他'], roles: {invoice: '发票', payment: '付款截图'}, stages: {}, category_auto_ready: false};
  let legacyBoard = false, failure = '', delayed = false, releaseWrite;
  const board = () => {
    const active = cards.filter(value => !['archived', 'deleted', 'unused'].includes(value.status));
    const result = {vouchers: [...active.filter(value => ['invoice', 'receipt'].includes(value.role)), existing],
      payments: active.filter(value => value.role === 'payment'), other: active.filter(value => value.role === 'other'),
      unused: cards.filter(value => value.status === 'unused'), matches: [], suggestions: [], supplement_candidates: [],
      report_types: [{code: 'other', label: '其他', names: [], editable_name: true}], ai: {available: true}};
    if (!legacyBoard) {
      result.archived = cards.filter(value => ['archived', 'unused'].includes(value.status));
      result.deleted = cards.filter(value => value.status === 'deleted');
    }
    return result;
  };
  page.on('pageerror', error => errors.push(error.message));
  await page.route(origin + '/**', async route => {
    const req = route.request(), url = new URL(req.url());
    const json = (body, status = 200) => route.fulfill({status, contentType: 'application/json', body: JSON.stringify(body)});
    if (url.pathname.startsWith('/api/')) {
      if (req.method() === 'POST') {
        const body = req.postDataJSON(); writes.push({path: url.pathname, body});
        assert.match(url.pathname, /^\/api\/intake\/(archive|restore|delete|restore-deleted)$/,
          'archival actions must never prepare, match, create, verify or submit');
        assert.deepEqual(Object.keys(body).sort(), ['id', 'project_id', 'revision']);
        assert.equal(body.project_id, 'p1');
        const entry = cards.find(value => value.id === body.id); assert.ok(entry);
        if (delayed) {delayed = false; await new Promise(resolve => {releaseWrite = resolve;});}
        if (failure === 'unavailable') {failure = ''; return json({error: '归档服务暂时不可用，请稍后重试'}, 503);}
        if (failure === 'conflict') {failure = ''; entry.revision++; return json({error: '材料版本已变化，请刷新后重新操作'}, 409);}
        if (entry.revision !== body.revision) return json({error: '材料版本已变化'}, 409);
        const action = url.pathname.split('/').at(-1);
        if (action === 'archive') {assert.equal(entry.can_archive, true); entry.previous_status = entry.status; entry.status = 'archived';}
        if (action === 'restore') {assert.equal(entry.can_restore, true); entry.status = entry.previous_status;}
        if (action === 'delete') {assert.equal(entry.can_delete, true); entry.deleted_status = entry.status; entry.status = 'deleted';}
        if (action === 'restore-deleted') {assert.equal(entry.can_restore_deleted, true); entry.status = entry.deleted_status;}
        entry.revision++; entry.can_archive = entry.status === 'review'; entry.can_restore = entry.status === 'archived';
        entry.can_delete = ['archived', 'unused'].includes(entry.status); entry.can_restore_deleted = entry.status === 'deleted';
        return json({entry, message: ({archive: '材料已归档', restore: '材料已恢复到合成台', delete: '材料已删除，可恢复', 'restore-deleted': '材料已恢复到归档'})[action]});
      }
      if (url.pathname === '/api/state') return json(state);
      if (url.pathname === '/api/material-matching') return json(board());
      if (url.pathname === '/api/intake') return json({queue: cards});
      return json({});
    }
    if (url.pathname.startsWith('/intake-file/')) return route.fulfill({contentType: 'image/png', body: png});
    const name = url.pathname === '/' ? 'index.html' : url.pathname.slice(1);
    if (!/^[a-z-]+\.(html|js|css)$/.test(name)) return route.abort();
    return route.fulfill({contentType: name.endsWith('.js') ? 'text/javascript' : name.endsWith('.css') ? 'text/css' : 'text/html',
      body: await fs.readFile(path.resolve(__dirname, '../src/invoice_print_layout/web', name))});
  });
  const action = (id, name) => page.locator(`[data-material-id="${id}"][data-material-action="${name}"]`);
  const perform = async (id, name) => {
    await Promise.all([page.waitForResponse(response => response.url() === origin + '/api/intake/' + name &&
      response.request().postDataJSON()?.id === id), action(id, name).click()]);
    await page.waitForFunction(() => !document.getElementById('matchingRefresh').disabled);
  };
  const fields = async () => ({type: await page.locator('#matchingProjectType').inputValue(),
    amount: await page.locator('#matchingAmount').inputValue(), name: await page.locator('#matchingProjectName').inputValue()});
  try {
    await fs.mkdir(evidence, {recursive: true});
    await page.goto(origin + '/?project=p1');
    await page.locator('#materialMatching').click();
    await action('v1', 'archive').waitFor();
    assert.equal(await page.locator('[data-material-action="archive"]').count(), 6);
    assert.equal(await page.locator('[data-material-id="expense"]').count(), 0, 'existing expenses have no archive actions');
    assert.equal(await page.locator('[data-assist-id]').count(), 0);
    assert.doesNotMatch(await page.locator('#matchingDeskDialog').innerText(), /AI辅助识别/);
    assert.equal(await page.locator('#matchingAIStatus').count(), 0);
    assert.equal(await page.locator('#matchingUnusedCount').innerText(), '1 份');
    await page.locator('#matchingUnusedSection > summary').click();
    assert.equal(await action('rail', 'restore').count(), 0);
    assert.equal(await action('rail', 'delete').count(), 1);
    await page.locator('[data-voucher-key="q:v1"]').click();
    await page.locator('[data-voucher-key="q:v2"]').click();
    await page.locator('[data-payment-id="payment"]').click();
    await page.locator('#matchingProjectType').selectOption('other');
    await page.locator('#matchingAmount').fill('27.35');
    await page.locator('#matchingProjectName').fill('手写名称');
    const draft = await fields();
    delayed = true;
    await action('v1', 'archive').click();
    await page.waitForFunction(() => document.querySelector('[data-material-id="v1"][data-material-action="archive"]')?.disabled);
    await page.locator('[data-material-id="v1"][data-material-action="archive"]').dispatchEvent('click');
    assert.equal(writes.length, 1, 'double-click while busy cannot resubmit');
    releaseWrite();
    await page.locator('#matchingMessage').filter({hasText: '材料已归档'}).waitFor();
    assert.equal(await page.locator('[data-voucher-key="q:v1"]').count(), 0);
    assert.equal(await page.locator('.matching-card.selected').count(), 2, 'only archived selection is removed');
    assert.deepEqual(await fields(), draft, 'remaining material selections keep all three unsaved fields');
    assert.deepEqual(writes[0].body, {id: 'v1', revision: 1, project_id: 'p1'});
    await perform('v1', 'delete');
    await page.locator('#matchingMessage').filter({hasText: '材料已删除'}).waitFor();
    assert.equal(await page.locator('#matchingDeletedSection').getAttribute('open'), null, 'deleted list starts collapsed');
    await page.locator('#matchingDeletedSection > summary').click();
    assert.equal(await page.locator('#matchingDeleted a[href="/intake-file/v1"]').count(), 1, 'original remains viewable');
    await perform('v1', 'restore-deleted');
    await page.locator('#matchingMessage').filter({hasText: '材料已恢复到归档'}).waitFor();
    assert.equal(await page.locator('[data-voucher-key="q:v1"]').count(), 0, 'deletion restore returns to archive first');
    await perform('v1', 'restore');
    await page.locator('#matchingMessage').filter({hasText: '材料已恢复到合成台'}).waitFor();
    assert.equal(await page.locator('[data-voucher-key="q:v1"]').getAttribute('aria-pressed'), 'false');
    assert.deepEqual(await fields(), draft);
    await perform('rail', 'delete');
    await page.locator('#matchingMessage').filter({hasText: '材料已删除'}).waitFor();
    await perform('rail', 'restore-deleted');
    await page.locator('#matchingMessage').filter({hasText: '材料已恢复到归档'}).waitFor();
    assert.equal(await action('rail', 'restore').count(), 0);
    assert.equal(await page.locator('[data-payment-id="rail"]').count(), 0);
    failure = 'unavailable'; const before503 = writes.length;
    await perform('ordinary', 'archive');
    await page.locator('#matchingMessage').filter({hasText: '归档服务暂时不可用'}).waitFor();
    assert.equal(writes.length, before503 + 1, '503 does not retry automatically');
    assert.equal(await action('ordinary', 'archive').isEnabled(), true);
    assert.deepEqual(await fields(), draft);
    failure = 'conflict'; const beforeConflict = writes.length;
    await perform('qr', 'archive');
    await page.locator('#matchingMessage').filter({hasText: '材料版本已变化'}).waitFor();
    assert.equal(writes.length, beforeConflict + 1, 'conflict refresh requires another user click');
    await perform('qr', 'archive');
    await page.locator('#matchingMessage').filter({hasText: '材料已归档'}).waitFor();
    assert.equal(writes.at(-1).body.revision, 2, 'explicit retry uses refreshed revision');
    assert.deepEqual(await fields(), draft);
    await perform('ordinary', 'archive');
    await page.locator('#matchingMessage').filter({hasText: '材料已归档'}).waitFor();
    assert.equal(await page.locator('[data-support-id="ordinary"]').count(), 0);
    await perform('payment', 'archive');
    await page.locator('#matchingMessage').filter({hasText: '材料已归档'}).waitFor();
    assert.equal(await page.locator('[data-payment-id="payment"]').count(), 0);
    assert.deepEqual(await fields(), draft);
    await perform('v2', 'archive');
    await page.locator('#matchingMessage').filter({hasText: '材料已归档'}).waitFor();
    assert.equal(await page.locator('#matchingProjectType').count(), 0, 'last archived selection returns to empty preview');
    await page.locator('[data-voucher-key="q:v1"]').click();
    assert.equal(await page.locator('#matchingAmount').inputValue(), '10.00', 'unrelated next selection gets its own amount');
    assert.notEqual(await page.locator('#matchingProjectName').inputValue(), '手写名称');
    await perform('unsafe', 'archive');
    await page.locator('#matchingMessage').filter({hasText: '材料已归档'}).waitFor();
    assert.equal(await page.evaluate(() => window.archiveXss), undefined);
    assert.equal(await page.locator('#matchingUnused a[href^="javascript:"],#matchingUnused a[href^="//"],#matchingUnused script').count(), 0);
    await page.setViewportSize({width: 390, height: 844});
    assert.equal(await page.locator('#matchingDeskDialog').evaluate(node => node.scrollWidth <= node.clientWidth + 1), true, 'archive cards fit narrow screens');
    await page.screenshot({path: path.join(evidence, 'synthetic-archive-mobile.png')});
    legacyBoard = true;
    await page.locator('#matchingRefresh').click();
    await page.locator('#matchingUnusedCount').filter({hasText: '1 份'}).waitFor();
    assert.equal(await action('rail', 'delete').count(), 1, 'legacy board.unused remains archive-compatible');
    await page.locator('#matchingClose').click();
    await page.evaluate(() => window.openUnifiedIntake());
    await page.locator('#intakeFilter').selectOption('归档');
    assert.equal(await page.locator('#intakeRows button').count(), 0, 'legacy intake cannot re-read archived material');
    assert.match(await page.locator('#intakeSummary').innerText(), /归档 6.*已删除 0.*待处理 1/);
    assert.deepEqual(errors, []);
    const result = {passed: true, archiveAllMaterialRoles: true, existingExpensesExcluded: true, restoreToArchiveFirst: true,
      railwayNeverReactivated: true, remainingSelectionsAndDraftPreserved: true, lastSelectionDraftNotReused: true,
      busyPreventsDuplicates: true, noAutomaticRetryOn503OrConflict: true, explicitRetryUsesNewRevision: true,
      deletedOriginalsReadable: true, legacyUnusedCompatible: true, manualAIRemoved: true, xssEscaped: true, narrowScreenFits: true,
      falseExpenseOrRecognitionWrites: 0, archiveWrites: writes.length, pageErrors: errors};
    await fs.writeFile(path.join(evidence, 'archive-browser.json'), JSON.stringify(result, null, 2));
    return result;
  } finally {await browser.close();}
}
module.exports = run;
if (require.main === module) run(require('playwright')).then(console.log).catch(error => {console.error(error); process.exitCode = 1;});
