// Real candidate/batch UI with synthetic versioned HTTP data, never a live ledger.
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
async function run(pw) {
  const browser = await pw.chromium.launch({headless: true});
  const page = await browser.newPage({viewport: {width: 1500, height: 1000}});
  page.setDefaultTimeout(10000);
  const origin = 'http://127.0.0.1:18798', errors = [], batches = [], otherWrites = [];
  const evidence = process.env.INVOICE_BROWSER_EVIDENCE ? path.resolve(process.env.INVOICE_BROWSER_EVIDENCE) :
    path.resolve(__dirname, '../workspace/invoice-operations/synthesis-batch-20261013');
  const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j1ioAAAAASUVORK5CYII=', 'base64');
  const types = [{code: 'living', label: '生活费', editable_name: false, names: ['住宿费(附清单)', '员工统一购餐费(附清单)']},
    {code: 'transport', label: '车费', editable_name: false, names: ['火车、飞机', '打的(出租车、滴滴等)']},
    {code: 'materials', label: '现场购材料', editable_name: true, names: []},
    {code: 'other', label: '其他', editable_name: true, names: []}];
  const card = (id, role, amount, fields = {}) => ({id, key: 'q:' + id, project_id: 'p1', role, status: 'review', revision: 1,
    name: id + '.png', amount_cents: amount, fields: {merchant: '合成商家 ' + id, ...fields},
    thumbnail: '/intake-file/' + id, url: '/intake-file/' + id,
    report_choice: id === 'va' ? {project_type: 'living', project_name: '住宿费(附清单)'} : {project_type: '', project_name: ''}});
  const vouchers = [card('va', 'invoice', 135300), card('vb', 'invoice', 5000,
    {merchant: '<img src=x onerror="window.batchXss=1">'})];
  const payments = [card('pa', 'payment', 135300), card('pb', 'payment', 5000), card('pc', 'payment', 135300)];
  const state = {app_mode: 'invoice', token: 'synthetic', projects: [{id: 'p1', name: '合成批量项目'}, {id: 'p2', name: '合成其他项目'}],
    items: [], inbox: [], categories: ['酒店', '其他'], roles: {invoice: '发票', payment: '付款截图'}, stages: {draft: '待核对'}, category_auto_ready: false};
  let epoch = 1, failure = '', consumed = false, delayed = false, releaseBatch;
  let delayPeople = false, peopleStarted = false, releasePeople;
  const board = (id = 'p1') => ({vouchers: consumed || id === 'p2' ? [] : vouchers,
    payments: consumed || id === 'p2' ? [] : payments, other: [], matches: [], supplement_candidates: [], report_types: types,
    suggestions: consumed || id === 'p2' ? [] : [
      {id: 'sa', project_id: 'p1', snapshot_token: 'token-a-' + epoch, voucher_keys: ['q:va'], payment_ids: ['pa'], amount_cents: 135300,
        revisions: {va: 1, pa: 1}, expected_versions: {}, needs_name_review: true, reason: '金额一致，开票商家与平台店名需人工核对', warnings: ['请确认平台店名对应酒店']},
      {id: 'sb', project_id: 'p1', snapshot_token: 'token-b-' + epoch, voucher_keys: ['q:vb'], payment_ids: ['pb'], amount_cents: 5000,
        revisions: {vb: 1, pb: 1}, expected_versions: {}, reason: '金额和商家明确对应'},
      {id: 'sc', project_id: 'p1', snapshot_token: 'token-c-' + epoch, voucher_keys: ['q:va'], payment_ids: ['pc'], amount_cents: 135300,
        revisions: {va: 1, pc: 1}, expected_versions: {}, ambiguous: true, reason: '同一发票存在另一组付款组合'}], ai: {available: true}});
  const committedKeys = new Map();
  page.on('pageerror', error => errors.push(error.message));
  await page.route(origin + '/**', async route => {
    const req = route.request(), url = new URL(req.url());
    const json = (body, status = 200) => route.fulfill({status, contentType: 'application/json', body: JSON.stringify(body)});
    if (url.pathname === '/api/runtime') return json({started_at:'synthetic-start',build_id:'synthetic-build'});
    if (url.pathname === '/api/state') return json(state);
    if (url.pathname === '/api/intake') return json({queue: []});
    if (url.pathname === '/api/reimbursement-people') {
      if (delayPeople) {delayPeople = false; peopleStarted = true; await new Promise(resolve => {releasePeople = resolve;});}
      return json({people: [], summary: {rows: []}});
    }
    if (url.pathname === '/api/material-matching') return json(board(url.searchParams.get('project') || 'p1'));
    if (req.method() === 'POST') {
      const body = req.postDataJSON();
      if (url.pathname === '/api/material-matching/create-batch') {
        batches.push(body);
        assert.equal(body.project_id, 'p1'); assert.ok(body.idempotency_key);
        assert.equal(body.groups.length, 2);
        assert.deepEqual(body.groups.map(group => group.suggestion_id), ['sa', 'sb']);
        for (const group of body.groups) assert.deepEqual(Object.keys(group).sort(),
          ['expected_versions', 'fields', 'payment_ids', 'revisions', 'snapshot_token', 'suggestion_id', 'voucher_keys']);
        if (delayed) {delayed = false; await new Promise(resolve => {releaseBatch = resolve;});}
        if (failure === 'conflict') {failure = ''; epoch++; return json({error: '候选材料版本已变化，请重新选择'}, 409);}
        if (failure === 'unavailable') {failure = ''; return json({error: '服务暂时不可用'}, 503);}
        assert.deepEqual(body.groups.map(group => group.snapshot_token), ['token-a-' + epoch, 'token-b-' + epoch]);
        if (committedKeys.has(body.idempotency_key)) return json({...committedKeys.get(body.idempotency_key), replayed: true});
        const items = body.groups.map((group, index) => ({id: 'created-' + index, title: group.fields.project_name,
          project_id: 'p1', project: '合成批量项目', amount_cents: Math.round(Number(group.fields.amount) * 100), amount: group.fields.amount,
          stage: 'draft', verified: false, complete: false, ready: false, review_requested: true, category: index ? '其他' : '酒店',
          attachments: [], missing: ['人工核对'], events: [], version: 'created-v1-' + index}));
        state.items.push(...items); consumed = true;
        const result = {items, matches: [], created: items.length, replayed: false, message: '整批已进入待核对'};
        committedKeys.set(body.idempotency_key, result); return json(result);
      }
      otherWrites.push(url.pathname);
      if (url.pathname === '/api/expense-print-preview') return json({id: body.id, version: body.expected_version,
        complete: false, missing: ['人工核对'], preview_pages: [], page_count: 0});
      throw Error('Unexpected write ' + url.pathname);
    }
    if (url.pathname.startsWith('/intake-file/')) return route.fulfill({contentType: 'image/png', body: png});
    const name = url.pathname === '/' ? 'index.html' : url.pathname.slice(1);
    if (!/^[a-z-]+\.(html|js|css)$/.test(name)) return route.abort();
    return route.fulfill({contentType: name.endsWith('.js') ? 'text/javascript' : name.endsWith('.css') ? 'text/css' : 'text/html',
      body: await fs.readFile(path.resolve(__dirname, '../src/invoice_print_layout/web', name))});
  });
  const check = id => page.locator(`[data-synthesis-check="${id}"]`);
  const field = (name, index) => page.locator(`[data-batch-${name}="${index}"]`);
  const refresh = async () => {await page.evaluate(() => window.refreshSynthesisCandidates());};
  try {
    await fs.mkdir(evidence, {recursive: true});
    await page.goto(origin + '/?project=p1');
    assert.equal(await page.locator('#tabs [data-view="synthesis"],#metrics [data-view="synthesis"]').count(), 0);
    await page.locator('#materialMatching').click();
    await check('sa').waitFor();
    assert.equal(await page.locator('#clearSynthesisSelected').count(), 0, 'Candidate selection omits the removed clear-selection button');
    assert.equal(await page.locator('#refreshSynthesis,#openSynthesisDesk').count(), 0);
    await page.locator('[data-synthesis-id="sa"] .synthesis-row-preview').click();
    const previewWidth = await page.locator('#matchingPreviewContent .matching-voucher-proof img').first().evaluate(node => node.getBoundingClientRect().width);
    assert.ok(previewWidth > 400, 'candidate proof expands beyond the old small thumbnail');
    assert.equal(await page.locator('#matchingPreviewContent .matching-voucher-proof img').first().evaluate(node => getComputedStyle(node).maxHeight), 'none');
    assert.equal(await page.locator('#matchingPreviewContent .matching-proof[href]').count(), 2);
    assert.match(await page.locator('#matchingSuggestionReason').innerText(), /商家名称待核对.*请确认平台店名对应酒店/s);
    assert.equal(batches.length, 0, 'preview remains read-only');
    await page.locator('#selectSynthesisVisible').check();
    assert.equal(await page.locator('#confirmSelectedSynthesis').isDisabled(), true, 'shared invoice prevents batch submission');
    assert.match(await page.locator('.synthesis-selection-status').innerText(), /共用了同一份材料/);
    await page.locator('#selectSynthesisVisible').uncheck(); assert.equal(await check('sa').isChecked(), false);
    assert.equal(await page.locator('[data-synthesis-check]:checked').count(), 0, 'Unchecking the current-candidates checkbox cancels its selection');
    await check('sa').check(); await check('sb').check();
    epoch++;
    await refresh();
    assert.equal(await check('sa').isChecked(), false, 'new snapshot must be explicitly selected');
    assert.match(await page.locator('.synthesis-selection-status').innerText(), /2 组.*版本已变化/);
    await check('sa').check(); await check('sb').check();
    delayPeople = true;
    await page.locator('#confirmSelectedSynthesis').click();
    for (let n = 0; !peopleStarted && n < 160; n++) await page.waitForTimeout(25);
    assert.equal(peopleStarted, true, 'Opening confirmation refreshes the personnel context');
    await page.locator('#matchingClose').click();
    await page.locator('#materialMatching').click(); await check('sa').waitFor();
    releasePeople(); await page.waitForTimeout(40);
    assert.equal(await page.locator('#synthesisBatchDialog').count(), 0, 'A late personnel read from a closed session cannot open confirmation in a new session');
    assert.equal(await page.locator('[data-synthesis-check]:checked').count(), 0, 'Reopening starts with fresh candidate checks');
    await check('sa').check(); await check('sb').check();
    delayPeople = true; peopleStarted = false;
    await page.locator('#confirmSelectedSynthesis').click();
    for (let n = 0; !peopleStarted && n < 160; n++) await page.waitForTimeout(25);
    assert.equal(peopleStarted, true);
    await check('sb').uncheck();
    releasePeople(); await page.waitForTimeout(40);
    assert.equal(await page.locator('#synthesisBatchDialog').count(), 0, 'Changing candidate checks during personnel refresh invalidates the old opening request');
    assert.equal(await check('sa').isChecked(), true);
    assert.equal(await check('sb').isChecked(), false);
    assert.equal(batches.length, 0, 'Changing candidates while opening remains read-only');
    await check('sb').check();
    await page.locator('#confirmSelectedSynthesis').click();
    await page.locator('#synthesisBatchDialog').waitFor();
    assert.equal(batches.length, 0, 'opening batch confirmation is read-only');
    assert.equal(await field('type', 0).inputValue(), 'living');
    assert.equal(await field('name', 0).inputValue(), '住宿费(附清单)');
    assert.equal(await field('amount', 0).inputValue(), '1353.00');
    assert.equal(await page.locator('#confirmSynthesisBatch').isDisabled(), true, 'unknown category/name requires confirmation');
    assert.match(await page.locator('.synthesis-batch-group').first().innerText(), /商家名称待核对.*请确认平台店名对应酒店/s);
    await field('type', 1).selectOption('transport');
    assert.equal(await field('name', 1).evaluate(node => node.tagName), 'SELECT');
    assert.deepEqual(await field('name', 1).locator('option').allTextContents(), ['选择对应细则', '火车、飞机', '打的(出租车、滴滴等)']);
    await field('name', 1).selectOption('火车、飞机');
    await field('type', 1).selectOption('materials');
    assert.equal(await field('name', 1).evaluate(node => node.tagName), 'INPUT');
    assert.equal(await field('name', 1).inputValue(), '', 'changing type clears incompatible detail');
    await field('name', 1).fill('手写材料名称'); await field('amount', 1).fill('');
    assert.equal(await page.locator('#confirmSynthesisBatch').isDisabled(), true);
    await field('amount', 1).fill('-1'); assert.equal(await page.locator('#confirmSynthesisBatch').isDisabled(), true);
    await field('amount', 1).fill('50.25');
    assert.equal(await page.locator('#confirmSynthesisBatch').isEnabled(), true);
    const draft = {type: await field('type', 1).inputValue(), name: await field('name', 1).inputValue(), amount: await field('amount', 1).inputValue()};
    failure = 'conflict';
    await page.locator('#confirmSynthesisBatch').click();
    await page.locator('#synthesisBatchStatus').filter({hasText: '原表单保留供对照'}).waitFor();
    assert.equal(batches.length, 1); assert.equal(state.items.length, 0, 'batch conflict cannot create a partial expense');
    assert.equal(await page.locator('#confirmSynthesisBatch').isDisabled(), true);
    assert.equal(await field('name', 1).inputValue(), draft.name);
    await page.locator('#synthesisBatchDialog [data-batch-close]').last().click();
    await check('sa').check(); await check('sb').check();
    await page.locator('#confirmSelectedSynthesis').click();
    await field('type', 1).selectOption('other'); await field('name', 1).fill('第二次手写名称'); await field('amount', 1).fill('51.20');
    delayed = true; failure = 'unavailable';
    await page.locator('#confirmSynthesisBatch').click();
    await page.waitForFunction(() => document.getElementById('confirmSynthesisBatch').disabled);
    await page.locator('#confirmSynthesisBatch').dispatchEvent('click');
    assert.equal(batches.length, 2, 'busy batch cannot submit again');
    releaseBatch();
    await page.locator('#synthesisBatchStatus').filter({hasText: '服务暂时不可用'}).waitFor();
    assert.equal(batches.length, 2, '503 never retries automatically');
    assert.equal(state.items.length, 0);
    assert.equal(await page.locator('#matchingDeskDialog').evaluate(node => node.open), true, 'Failed batch keeps the material desk open');
    assert.equal(await page.locator('#synthesisBatchDialog').evaluate(node => node.open), true, 'Failed batch keeps its editable confirmation open');
    assert.equal(await page.locator('#tabs [data-view="review"]').getAttribute('class'), 'active', 'Failure cannot invent a synthesis main stage');
    assert.equal(await field('name', 1).inputValue(), '第二次手写名称'); assert.equal(await field('amount', 1).inputValue(), '51.20');
    await field('name', 1).fill('明确修改后的名称'); failure = 'unavailable';
    await page.locator('#confirmSynthesisBatch').click();
    await page.waitForFunction(() => !document.getElementById('confirmSynthesisBatch').disabled);
    assert.equal(batches.length, 3);
    assert.notEqual(batches[1].idempotency_key, batches[2].idempotency_key, 'editing a failed request starts a new idempotency identity');
    await page.setViewportSize({width: 390, height: 844});
    assert.equal(await page.locator('#synthesisBatchDialog').evaluate(node => node.scrollWidth <= node.clientWidth + 1), true);
    assert.equal(await page.evaluate(() => window.batchXss), undefined);
    await page.screenshot({path: path.join(evidence, 'synthetic-batch-mobile.png')});
    await page.locator('#confirmSynthesisBatch').click();
    await page.locator('#synthesisBatchDialog').waitFor({state: 'detached'});
    assert.equal(await page.locator('#matchingDeskDialog').evaluate(node => node.open), false, 'Successful batch closes both dialogs');
    assert.equal(batches.length, 4);
    assert.equal(batches[2].idempotency_key, batches[3].idempotency_key, 'unchanged explicit retry keeps same idempotency key');
    assert.deepEqual(batches[2], batches[3]);
    assert.equal(state.items.length, 2); assert.equal(committedKeys.size, 1);
    assert.ok(state.items.every(item => item.stage === 'draft' && !item.verified && !item.ready && item.review_requested));
    assert.equal(await page.locator('#tabs [data-view="review"]').getAttribute('class'), 'active');
    assert.equal(await page.evaluate(() => active), 'created-0');
    assert.deepEqual(await page.evaluate(() => [...checked]), [], 'Candidate confirmation cannot carry expense checks into review');
    assert.equal(otherWrites.filter(value => /\/transition$|\/create$|\/match$|\/intake\//.test(value)).length, 0);
    consumed = false; await page.locator('#materialMatching').click(); await refresh();
    await check('sa').check();
    await page.goto(origin + '/?project=p2'); await page.locator('#materialMatching').click();
    await page.locator('#matchingSuggestionSummary').filter({hasText: '0 组'}).waitFor();
    assert.equal(await page.locator('#confirmSelectedSynthesis').isDisabled(), true, 'other project cannot retain previous candidate selection');
    assert.deepEqual(errors, []);
    const result = {passed: true, fullWidthOriginalPreview: true, removedDuplicateTools: true, candidateViewingReadOnly: true,
      selectAllClearAndCheckboxes: true, sharedMaterialsBlocked: true, snapshotChangesUncheck: true, projectChangeClears: true,
      linkedRequiredClassification: true, openNamesOnlyForMaterialsOrOther: true, amountValidation: true,
      atomicFailureNotPretendedComplete: true, conflictKeepsFieldsAndRequiresReselection: true,
      unavailableKeepsFormNoAutoRetry: true, sameBodyExplicitRetryIdempotent: true, editedRetryGetsNewIdentity: true, busyPreventsDuplicates: true,
      delayedPersonnelCannotOpenInReopenedDesk: true,
      changedChecksDuringPersonnelReadCannotOpenStaleBatch: true,
      explicitConfirmationToPendingReview: 2, noVerificationSubmissionOrRecognitionWrites: true, xssEscaped: true, narrowScreenFits: true, pageErrors: errors};
    await fs.writeFile(path.join(evidence, 'batch-browser.json'), JSON.stringify(result, null, 2)); return result;
  } finally {releasePeople?.(); releaseBatch?.(); await browser.close();}
}
module.exports = run;
if (require.main === module) run(require('playwright')).then(console.log).catch(error => {console.error(error); process.exitCode = 1;});
