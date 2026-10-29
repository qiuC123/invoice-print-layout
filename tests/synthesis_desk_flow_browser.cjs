// Desk-only suggestion workflow against intercepted synthetic HTTP; no real ledger or service.
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
async function run(pw) {
  const browser = await pw.chromium.launch({headless: true});
  const page = await browser.newPage({viewport: {width: 1500, height: 1000}});
  page.setDefaultTimeout(10000);
  const origin = 'http://127.0.0.1:18869', errors = [], writes = [], boardPlans = [], releases = [];
  const evidence = process.env.INVOICE_BROWSER_EVIDENCE ? path.resolve(process.env.INVOICE_BROWSER_EVIDENCE) :
    path.resolve(__dirname, '../workspace/invoice-operations/synthesis-desk-flow-20261016');
  const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j1ioAAAAASUVORK5CYII=', 'base64');
  const expense = (id, project = 'p1') => ({id, project_id: project, project: 'Synthetic ' + project, title: 'Synthetic ' + id,
    category: '酒店', amount: '10.00', amount_cents: 1000, complete: true, missing: [], stage: 'draft',
    verified: true, ready: true, version: 'version-' + id, attachments: [], events: []});
  const state = {app_mode: 'invoice', token: 'synthetic', projects: [{id: 'p1', name: 'Synthetic p1'}, {id: 'p2', name: 'Synthetic p2'}],
    items: [expense('existing')], inbox: [], categories: ['酒店', '餐饮'], roles: {invoice: '发票', payment: '付款截图'},
    stages: {draft: '待核对'}, category_auto_ready: false};
  const card = (id, role, category, project = 'p1') => ({id, key: 'q:' + id, project_id: project, role, status: 'review', revision: 1,
    name: 'Synthetic ' + id + '.png', amount_cents: 1000, fields: {category, merchant: 'Synthetic merchant ' + id},
    report_choice: {project_type: 'living', project_name: category === '酒店' ? '住宿费(附清单)' : '员工统一购餐费(附清单)'},
    thumbnail: '/intake-file/' + id, url: '/intake-file/' + id, issues: []});
  const vouchers = [card('va', 'invoice', '酒店'), card('vb', 'invoice', '餐饮'), card('vf', 'invoice', '酒店', 'p2')];
  const payments = [card('pa', 'payment', '酒店'), card('pb', 'payment', '餐饮'), card('pf', 'payment', '酒店', 'p2')];
  const suggestion = (id, suffix, project = 'p1') => ({id, project_id: project, snapshot_token: 'token-' + id,
    voucher_keys: ['q:v' + suffix], payment_ids: ['p' + suffix], revisions: {['v' + suffix]: 1, ['p' + suffix]: 1},
    expected_versions: {}, amount_cents: 1000, needs_name_review: suffix === 'a', ambiguous: suffix === 'b',
    reason: suffix === 'a' ? '金额相符，名称仍需核对' : '存在另一组竞争材料', warnings: ['请检查原件对应关系']});
  let candidates = [suggestion('current-a', 'a'), suggestion('current-b', 'b'), suggestion('foreign', 'f', 'p2')];
  const oldNavigation = '系统候选进入待合成；选择材料并确认创建后进入待核对。';
  const importantWarning = '请核对发票金额与付款截图中的实际支付金额。';
  let consumed = false, failedState = false, zeroSuggestions = false, createCalls = 0, autoCalls = 0, releaseAuto;
  const board = () => ({vouchers: consumed ? [] : vouchers, payments: consumed ? [] : payments, other: [], matches: [],
    suggestions: consumed || zeroSuggestions ? [] : candidates, supplement_candidates: [], ai: {available: false},
    report_types: [{code: 'living', label: '生活费', editable_name: false, names: ['住宿费(附清单)', '员工统一购餐费(附清单)']}]});
  function holdBoard(payload, status = 200) {
    let release, start; const gate = new Promise(resolve => {release = resolve;}), started = new Promise(resolve => {start = resolve;});
    boardPlans.push({payload, status, gate, start}); releases.push(release); return {release, started};
  }
  page.on('pageerror', error => errors.push(error.message));
  await page.route(origin + '/**', async route => {
    const request = route.request(), url = new URL(request.url()), body = request.method() === 'POST' ? request.postDataJSON() : null;
    const json = (value, status = 200) => route.fulfill({status, contentType: 'application/json', body: JSON.stringify(value)});
    if (url.pathname === '/api/runtime') return json({started_at:'synthetic-start',build_id:'synthetic-build'});
    if (url.pathname === '/api/state') {
      if (failedState) {failedState = false; return json({error: 'Synthetic ledger refresh unavailable'}, 503);}
      return json(state);
    }
    if (url.pathname === '/api/material-matching') {
      assert.equal(url.searchParams.get('project'), 'p1');
      const plan = boardPlans.shift(); if (plan) {plan.start(); await plan.gate; return json(plan.payload, plan.status);}
      return json(board());
    }
    if (url.pathname === '/api/intake') return json({queue: []});
    if (url.pathname === '/api/reimbursement-people') return json({people: [{id: 'person-a', label: 'Synthetic person A', project_id: 'p1', state: 'active'}], summary: {rows: []}});
    if (body) {
      writes.push({path: url.pathname, body});
      if (url.pathname === '/api/material-matching/auto') {
        autoCalls++; if (!zeroSuggestions) await new Promise(resolve => {releaseAuto = resolve; releases.push(resolve);});
        return json({suggested: zeroSuggestions ? 0 : 2, suggestions: zeroSuggestions ? [] : candidates,
          issues: [oldNavigation, importantWarning], conflicts: []});
      }
      if (url.pathname === '/api/material-matching/create') {
        createCalls++; assert.equal(body.project_id, 'p1'); assert.equal(body.suggestion_id, 'current-a');
        assert.equal(body.suggestion_token, 'token-current-a'); assert.deepEqual(body.voucher_keys, ['q:va']); assert.deepEqual(body.payment_ids, ['pa']);
        assert.deepEqual(body.fields, {project_type: 'living', amount: '10.00', project_name: '住宿费(附清单)'});
        const item = {...expense('created'), verified: false, ready: false, review_requested: true}; state.items.push(item); consumed = true;
        failedState = true; return json({item});
      }
      if (url.pathname === '/api/expense-print-preview') return json({id: body.id, version: body.expected_version,
        complete: true, missing: [], pdf: body.id + '.pdf', preview_pages: [body.id + '.png'], page_count: 1});
      assert.fail('Unexpected business write ' + url.pathname);
    }
    if (url.pathname.startsWith('/intake-file/') || url.pathname.startsWith('/export/')) return route.fulfill({contentType: 'image/png', body: png});
    const file = url.pathname === '/' ? 'index.html' : url.pathname.slice(1);
    if (!/^[a-z-]+\.(html|js|css)$/.test(file)) return route.abort();
    return route.fulfill({contentType: file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html',
      body: await fs.readFile(path.resolve(__dirname, '../src/invoice_print_layout/web', file))});
  });
  const filters = async () => page.evaluate(() => ({category: window.invoiceMultiFilters.values('categoryFilter'),
    issue: window.invoiceMultiFilters.values('issueFilter'), person: window.invoiceMultiFilters.values('personFilter'), search: $('search').value}));
  const expectedFilters = {category: ['酒店'], issue: ['missing'], person: ['person-a'], search: 'Synthetic no matching expense'};
  const ready = () => page.waitForFunction(() => document.getElementById('matchingDeskDialog').open && document.getElementById('matchingDeskDialog').getAttribute('aria-busy') === 'false' && !document.querySelector('.matching-layout').hidden);
  try {
    await fs.mkdir(evidence, {recursive: true}); await page.goto(origin + '/?project=p1');
    await page.waitForFunction(() => !!state.token && typeof window.openMaterialMatching === 'function');
    assert.equal(await page.locator('#tabs [data-view="synthesis"],#metrics [data-view="synthesis"]').count(), 0);
    await page.evaluate(() => {view = 'submitted'; active = 'existing'; checked = new Set(['existing']);
      window.invoiceMultiFilters.set('categoryFilter', ['酒店']); window.invoiceMultiFilters.set('issueFilter', ['missing']);
      window.invoiceMultiFilters.set('personFilter', ['person-a']);
      $('search').value = 'Synthetic no matching expense'; render();});
    const old = holdBoard({...board(), suggestions: [suggestion('late-old', 'a')]});
    await page.locator('#materialMatching').click(); await old.started;
    assert.equal(await page.locator('#matchingDeskDialog').isVisible(), true);
    assert.equal(await page.locator('#matchingLoadState progress').isVisible(), true);
    assert.equal(await page.locator('#matchingClose').isEnabled(), true);
    await page.locator('#matchingClose').click();
    const fresh = holdBoard(board()); await page.locator('#materialMatching').click(); await fresh.started;
    fresh.release(); await ready(); old.release(); await page.waitForTimeout(40);
    assert.equal(await page.locator('#matchingSuggestions [data-synthesis-id]').count(), 2);
    assert.equal(await page.locator('[data-synthesis-id="foreign"],[data-synthesis-id="late-old"]').count(), 0, 'Closed reads and foreign projects cannot enter current suggestions');
    assert.deepEqual(await filters(), expectedFilters); assert.deepEqual(await page.evaluate(() => [...checked]), ['existing']);
    assert.match(await page.locator('#matchingSuggestions').innerText(), /名称待核对.*存在竞争组合/s);
    await page.locator('#matchingClose').click();
    const failure = holdBoard({error: 'Synthetic material read failed'}, 503); await page.locator('#materialMatching').click(); await failure.started; failure.release();
    await page.locator('#matchingLoadState').filter({hasText: 'Synthetic material read failed'}).waitFor();
    await page.locator('#matchingLoadState [data-dialog-retry]').click(); await ready();
    await page.locator('#matchingAuto').click();
    await page.waitForFunction(() => document.getElementById('matchingAuto').disabled);
    assert.equal(autoCalls, 1); await page.locator('#matchingAuto').dispatchEvent('click'); assert.equal(autoCalls, 1, 'Busy discovery cannot issue a duplicate request');
    releaseAuto(); await page.waitForFunction(() => !document.getElementById('matchingAuto').disabled);
    assert.equal(await page.locator('#matchingDeskDialog').evaluate(node => node.open), true);
    assert.equal(await page.locator('#matchingSuggestions [data-synthesis-id]').count(), 2);
    assert.equal(await page.locator('[data-synthesis-check]:checked').count(), 0, 'Discovery never confirms a suggested association');
    assert.equal(createCalls, 0); assert.equal(state.items.length, 1);
    assert.doesNotMatch(await page.locator('#matchingMessage').innerText(), /待合成/, 'Legacy navigation wording must not send users back to the removed stage');
    assert.ok((await page.locator('#matchingMessage').innerText()).includes(importantWarning), 'Filtering redundant navigation must preserve material-review warnings');
    zeroSuggestions = true; await page.locator('#matchingAuto').click();
    await page.locator('#matchingMessage').filter({hasText: '找到 0 组建议组合'}).waitFor();
    assert.match(await page.locator('#matchingMessage').innerText(), /下方手工选择材料.*右侧预览并确认后进入待核对/s);
    assert.doesNotMatch(await page.locator('#matchingMessage').innerText(), /待合成/);
    assert.ok((await page.locator('#matchingMessage').innerText()).includes(importantWarning));
    assert.equal(await page.locator('#matchingSuggestions [data-synthesis-id]').count(), 0);
    assert.equal(await page.locator('[data-voucher-key="q:va"]').count(), 1, 'No suggestions still leaves original materials available for manual grouping');
    assert.equal(createCalls, 0); assert.equal(autoCalls, 2); assert.equal(state.items.length, 1);
    zeroSuggestions = false; await page.locator('#matchingRefresh').click();
    await page.waitForFunction(() => !document.getElementById('matchingRefresh').disabled && document.querySelectorAll('#matchingSuggestions [data-synthesis-id]').length === 2);
    const geometries = [];
    for (const theme of ['light', 'dark']) for (const width of [1500, 390]) {
      await page.locator('#themeSelect').selectOption(theme, {force: true}); await page.setViewportSize({width, height: 1000});
      await page.locator('[data-synthesis-id="current-a"] .synthesis-row-preview').click();
      const bounds = await page.locator('#matchingDeskDialog').evaluate(node => ({scroll: node.scrollWidth, client: node.clientWidth,
        page: document.documentElement.scrollWidth, width: innerWidth}));
      assert.ok(bounds.scroll <= bounds.client + 1 && bounds.page <= bounds.width + 1, `${theme}/${width}: desk and candidate controls must fit`);
      assert.equal(await page.locator('#matchingPreviewContent .matching-proof[href]').count(), 2);
      assert.match(await page.locator('#matchingSuggestionReason').innerText(), /商家名称待核对.*请检查原件对应关系/s);
      assert.deepEqual(await filters(), expectedFilters); geometries.push({theme, width});
    }
    await page.screenshot({path: path.join(evidence, 'synthetic-desk-mobile-dark.png')});
    await page.setViewportSize({width: 1500, height: 1000});
    assert.equal(await page.locator('#matchingSave').innerText(), '确认合成并进入待核对');
    await page.locator('#matchingSave').click();
    await page.locator('#matchingMessage').filter({hasText: '页面刷新未完成'}).waitFor();
    assert.match(await page.locator('#matchingMessage').innerText(), /已创建.*无法读取工作台.*勿重复创建/s);
    assert.equal(createCalls, 1); assert.equal(state.items.length, 2, 'A read failure after commit cannot undo or duplicate the successful creation');
    assert.equal(await page.locator('#matchingDeskDialog').evaluate(node => node.open), true);
    assert.equal(await page.locator('#matchingSave').isDisabled(), true, 'Committed creation stays locked after ledger refresh failure');
    await page.locator('#matchingSave').dispatchEvent('click'); await page.waitForTimeout(30); assert.equal(createCalls, 1);
    assert.ok(state.items.filter(item => item.id === 'created').every(item => !item.verified && !item.ready && item.stage === 'draft'));
    assert.deepEqual(await page.evaluate(() => [...checked]), [], 'Creation clears cross-stage expense checks even when the refresh fails');
    await page.locator('#matchingClose').click(); await page.evaluate(() => load());
    assert.equal(await page.evaluate(() => view), 'review');
    assert.equal(await page.evaluate(() => state.items.filter(item => item.id === 'created').length), 1, 'An explicit recovery read finds the single committed expense');
    assert.equal(writes.filter(value => /\/transition$|\/report$|\/create-batch$/.test(value.path)).length, 0);
    assert.deepEqual(errors, []);
    const result = {passed: true, deskOnlySuggestionFlow: true, mainFiltersDoNotHideProjectCandidates: true,
      earlyLoadingAndClose: true, lateClosedResponseIsolated: true, projectBoundary: true, readFailureRetry: true,
      discoveryDoubleClickBlocked: true, previewReasonsOriginalsAndWarnings: true, geometries,
      oldNavigationFilteredImportantWarningPreserved: true, zeroSuggestionsExplainManualFallback: true,
      postCommitRefreshFailureCannotResubmit: true, expenseChecksCleared: true, noVerificationOrSubmission: true, pageErrors: errors};
    await fs.writeFile(path.join(evidence, 'synthesis-desk-flow-browser.json'), JSON.stringify(result, null, 2)); return result;
  } finally {for (const release of releases) release(); await browser.close();}
}
module.exports = run;
if (require.main === module) run(require('playwright')).then(console.log).catch(error => {console.error(error); process.exitCode = 1;});
