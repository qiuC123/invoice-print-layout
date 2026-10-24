// Hold synthetic reads to prove that the real dialogs open before data arrives.
const assert = require('node:assert/strict'), fs = require('node:fs/promises'), path = require('node:path');
async function run(pw) {
  const browser = await pw.chromium.launch({headless: true});
  const page = await browser.newPage({viewport: {width: 1272, height: 1000}});
  page.setDefaultTimeout(10000);
  const origin = 'http://127.0.0.1:18861', plans = new Map(), releases = [], errors = [], writes = [];
  let item = {id: 'f1', title: 'Synthetic fee', project_id: 'p1', project: 'Synthetic project', category: '酒店',
    amount_cents: 1000, amount: '10.00', stage: 'draft', verified: false, complete: true, ready: false,
    attachments: [], events: [], missing: [], version: 'v1', expense_date: '', merchant: '', order_number: ''};
  const state = () => ({app_mode: 'invoice', token: 'synthetic', items: [item], projects: [{id: 'p1', name: 'Synthetic project'}],
    inbox: [], roles: {}, categories: ['酒店'], stages: {draft: '待核对'}, category_auto_ready: false, report_template_ready: true});
  const people = () => ({project_id: 'p1', people: [], summary: {rows: [], expense_count: 1, total_cents: item.amount_cents}});
  const board = {vouchers: [], payments: [], other: [], archived: [], matches: [], suggestions: [], report_types: []};
  const snapshot = () => ({combined_pdf: item.version + '.pdf', combined_preview_pages: [item.version + '-table.svg', item.version + '-materials.svg'], combined_page_sizes: [{width: 842, height: 595}, {width: 595, height: 842}]});
  function hold(endpoint, payload, status = 200) {
    let release, markStarted;
    const gate = new Promise(resolve => {release = resolve;}), started = new Promise(resolve => {markStarted = resolve;});
    const plan = {gate, markStarted, payload, status};
    if (!plans.has(endpoint)) plans.set(endpoint, []);
    plans.get(endpoint).push(plan); releases.push(release);
    return {started, release};
  }
  page.on('pageerror', error => errors.push(error.message));
  await page.route(origin + '/**', async route => {
    const request = route.request(), url = new URL(request.url());
    const json = (payload, status = 200) => route.fulfill({status, contentType: 'application/json', body: JSON.stringify(payload)});
    if (request.method() === 'POST' && url.pathname !== '/api/report-preview') {writes.push(url.pathname); throw Error('Only read-only dialog actions are allowed');}
    const plan = plans.get(url.pathname)?.shift();
    if (plan) {plan.markStarted(); await plan.gate; return json(plan.payload, plan.status);}
    if (url.pathname === '/api/state') return json(state());
    if (url.pathname === '/api/reimbursement-people') return json(people());
    if (url.pathname === '/api/material-matching') return json(board);
    if (url.pathname === '/api/report-preview') return json(snapshot());
    if (url.pathname.startsWith('/export/')) return route.fulfill({contentType: 'image/svg+xml', body: '<svg xmlns="http://www.w3.org/2000/svg" width="842" height="595"><rect width="842" height="595" fill="white"/></svg>'});
    if (url.pathname.startsWith('/api/')) return json({queue: []});
    const name = url.pathname === '/' ? 'index.html' : url.pathname.slice(1);
    if (!/^[a-z-]+\.(html|js|css)$/.test(name)) return route.abort();
    return route.fulfill({contentType: name.endsWith('.js') ? 'text/javascript' : name.endsWith('.css') ? 'text/css' : 'text/html',
      body: await fs.readFile(path.resolve(__dirname, '../src/invoice_print_layout/web', name))});
  });
  const loading = async id => {
    const dialog = page.locator('#' + id);
    assert.equal(await dialog.isVisible(), true, id + ' opens before its held read resolves');
    assert.equal(await dialog.locator('.dialog-loading').isVisible(), true);
    assert.equal(await dialog.locator('.dialog-loading progress').getAttribute('value'), null, 'Loading progress is indeterminate');
    assert.equal(await dialog.locator('.dialog-close').isEnabled(), true, 'Read-only loading remains dismissible');
    return dialog;
  };
  const reportReady = async total => {
    await page.frameLocator('#previewFrame').locator('.print-page img').first().waitFor();
    await page.locator('#previewScope').filter({hasText: total}).waitFor();
    assert.equal(await page.locator('#reportPreviewDialog').getAttribute('aria-busy'), 'false');
    assert.equal(await page.locator('#previewFrame').isVisible(), true);
    for (const id of ['downloadPreview', 'printPreview', 'previewTrial']) assert.equal(await page.locator('#' + id).isEnabled(), true);
  };
  try {
    await page.goto(origin + '/?project=p1'); await page.locator('[data-item=f1]').waitFor();
    await page.waitForFunction(() => !document.querySelector('#personFilter').disabled);
    const stalePeople = hold('/api/reimbursement-people', {error: 'OLD people error'}, 500);
    await page.locator('#reimbursementPeople').click(); await stalePeople.started; await loading('reimbursementPeopleDialog');
    await page.evaluate(() => document.querySelector('#reimbursementPeople').click());
    assert.equal(await page.locator('#reimbursementPeopleDialog').count(), 1);
    await page.locator('#reimbursementPeopleDialog .dialog-close').click();
    const freshPeople = hold('/api/reimbursement-people', people());
    await page.locator('#reimbursementPeople').click(); await freshPeople.started;
    stalePeople.release(); await page.waitForTimeout(30); await loading('reimbursementPeopleDialog');
    assert.equal(await page.locator('#reimbursementPeopleDialog .dialog-load-error').count(), 0);
    freshPeople.release(); await page.locator('.reimbursement-summary-total').waitFor();
    await page.locator('#reimbursementPeopleDialog .dialog-close').click();
    const failedPeople = hold('/api/reimbursement-people', {error: 'Failure <svg onload=alert(1)>'}, 500);
    await page.locator('#reimbursementPeople').click(); await failedPeople.started; failedPeople.release();
    await page.locator('#reimbursementPeopleDialog .dialog-load-error').waitFor();
    assert.equal(await page.locator('#reimbursementPeopleDialog svg').count(), 0);
    const retriedPeople = hold('/api/reimbursement-people', people());
    await page.locator('#reimbursementPeopleDialog [data-dialog-retry]').click(); await retriedPeople.started; await loading('reimbursementPeopleDialog');
    retriedPeople.release(); await page.locator('.reimbursement-summary-total').waitFor();
    await page.locator('#reimbursementPeopleDialog').press('Escape');

    const staleBoard = hold('/api/material-matching', {...board, vouchers: [{key: 'OLD', name: 'Stale material'}]});
    await page.locator('#materialMatching').click(); await staleBoard.started; await loading('matchingDeskDialog');
    assert.equal(await page.locator('#matchingUpload').isDisabled(), true);
    assert.equal(await page.locator('.matching-layout').isVisible(), false);
    await page.locator('#matchingDeskDialog').press('Escape');
    const freshBoard = hold('/api/material-matching', board);
    await page.locator('#materialMatching').click(); await freshBoard.started; staleBoard.release();
    await page.waitForTimeout(30); await loading('matchingDeskDialog');
    assert.equal(await page.locator('#matchingUpload').isDisabled(), true);
    freshBoard.release(); await page.waitForFunction(() => !document.querySelector('#matchingUpload').disabled);
    assert.equal(await page.locator('.matching-layout').isVisible(), true);
    assert.equal(await page.locator('#matchingVouchers [data-voucher-key="OLD"]').count(), 0);
    await page.locator('#matchingClose').click();
    const failedBoard = hold('/api/material-matching', {error: 'Material read failed'}, 500);
    await page.locator('#materialMatching').click(); await failedBoard.started; failedBoard.release();
    await page.locator('#matchingLoadState .dialog-load-error').waitFor();
    assert.equal(await page.locator('#matchingClose').isEnabled(), true);
    const retriedBoard = hold('/api/material-matching', board);
    await page.locator('#matchingLoadState [data-dialog-retry]').click(); await retriedBoard.started; await loading('matchingDeskDialog');
    retriedBoard.release(); await page.waitForFunction(() => !document.querySelector('#matchingUpload').disabled);
    await page.locator('#matchingClose').click();

    const reportState = hold('/api/state', state()), reportRows = hold('/api/report-preview', snapshot());
    await page.locator('#previewReport').click(); await reportState.started; await loading('reportPreviewDialog');
    for (const id of ['downloadPreview', 'printPreview', 'previewTrial']) assert.equal(await page.locator('#' + id).isDisabled(), true);
    assert.equal(await page.locator('#previewFrame').isVisible(), false);
    reportState.release(); await reportRows.started; await loading('reportPreviewDialog');
    reportRows.release(); await reportReady('10.00'); await page.locator('#closePreview').click();
    const staleRows = hold('/api/report-preview', snapshot());
    await page.locator('#previewReport').click(); await staleRows.started; await loading('reportPreviewDialog');
    await page.locator('#closePreview').click();
    item = {...item, title: 'Latest fee', amount_cents: 2000, amount: '20.00', version: 'v2'};
    const freshRows = hold('/api/report-preview', snapshot());
    await page.locator('#previewReport').click(); await freshRows.started; staleRows.release();
    await page.waitForTimeout(30); await loading('reportPreviewDialog');
    freshRows.release(); await reportReady('20.00');
    assert.match(await page.frameLocator('#previewFrame').locator('img').first().getAttribute('src'), /v2-table/);
    assert.equal(await page.frameLocator('#previewFrame').locator('details,a,.expense').count(), 0);
    await page.locator('#reportPreviewDialog').press('Escape');
    const staleState = hold('/api/state', {error: 'OLD report error'}, 500);
    await page.locator('#previewReport').click(); await staleState.started; await page.locator('#closePreview').click();
    const freshState = hold('/api/state', state());
    await page.locator('#previewReport').click(); await freshState.started; staleState.release();
    await page.waitForTimeout(30); await loading('reportPreviewDialog');
    assert.equal(await page.locator('#previewLoadState .dialog-load-error').count(), 0);
    freshState.release(); await reportReady('20.00'); await page.locator('#closePreview').click();
    const failedState = hold('/api/state', {error: 'Read failed'}, 500);
    await page.locator('#previewReport').click(); await failedState.started; failedState.release();
    await page.locator('#previewLoadState .dialog-load-error').waitFor();
    assert.equal(await page.locator('#downloadPreview').isDisabled(), true);
    const retryState = hold('/api/state', state());
    await page.locator('#previewLoadState [data-dialog-retry]').click(); await retryState.started; await loading('reportPreviewDialog');
    retryState.release(); await reportReady('20.00'); await page.locator('#closePreview').click();

    const geometry = [];
    for (const theme of ['light', 'dark']) {
      await page.locator('#themeSelect').selectOption(theme,{force:true});
      const read = hold('/api/state', state());
      await page.locator('#previewReport').click(); await read.started; await loading('reportPreviewDialog');
      geometry.push(await page.locator('#reportPreviewDialog .dialog-loading').evaluate(el => ({theme: document.documentElement.dataset.theme,
        fontSize: getComputedStyle(el.querySelector('p')).fontSize, progress: getComputedStyle(el.querySelector('progress')).height})));
      await page.locator('#closePreview').click(); read.release();
    }
    assert.deepEqual(geometry.map(row => row.fontSize), ['14px', '14px']);
    const initialState = hold('/api/state', state()), openingState = hold('/api/state', state());
    await page.reload(); await initialState.started;
    await page.locator('#materialMatching').click(); await openingState.started; await loading('matchingDeskDialog');
    await page.locator('#matchingClose').click(); initialState.release(); openingState.release();
    await page.locator('[data-item=f1]').waitFor();
    assert.equal(await page.locator('#matchingDeskDialog').isVisible(), false, 'Closing during initial state reading must keep the dialog closed');
    assert.deepEqual(errors, []); assert.deepEqual(writes, []);
    return {passed: true, allThreeOpenBeforeData: true, uniformIndeterminateLoading: true,
      closeAndEscapeWhileReading: true, lateResponsesIsolated: true, retryAllThree: true,
      reportBothReadPhasesCovered: true, noOldContentWhileLoading: true, reportActionsDisabledUntilReady: true,
      onePersonnelDialog: true, beforeInitialStateReady: true, escapedErrorText: true, geometry, businessWrites: 0};
  } finally {for (const release of releases) release(); await browser.close();}
}
module.exports = run;
if (require.main === module) run(require('playwright')).then(result => console.log(JSON.stringify(result))).catch(error => {console.error(error); process.exitCode = 1;});
