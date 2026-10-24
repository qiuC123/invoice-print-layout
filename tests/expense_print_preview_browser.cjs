// Real frontend files with synthetic HTTP responses; no live service or ledger.
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');

async function run(pw) {
  const browser = await pw.chromium.launch({headless: true});
  const page = await browser.newPage({viewport: {width: 1500, height: 1000}});
  page.setDefaultTimeout(10000);
  const origin = 'http://127.0.0.1:18772';
  const errors = [], requests = [], previewCounts = new Map();
  const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j1ioAAAAASUVORK5CYII=', 'base64');
  const item = (id, fields = {}) => ({
    id, title: `合成费用 ${id}`, project: '预览测试项目', project_id: 'p1',
    category: '材料采购', amount: '24.00', amount_cents: 2400,
    stage: 'draft', verified: false, complete: true, ready: false,
    attachments: [], events: [], missing: [], version: `${id}-v1`,
    expense_date: '2026-10-10', note: '', merchant: '合成商家',
    order_number: '', followup_date: '', invoice_state: 'received',
    basis: '发票／收据＋付款截图', waiting_days: 0, ...fields,
  });
  const state = {
    app_mode: 'invoice', logistics_available: false, items: [
      item('A', {material_match: {group_id: 'synthetic-group', provisional: true, payment_total_cents: 2400,
        payment_count: 1, support_count: 1}}), item('B'), item('C', {verified: true, ready: true}),
      item('D', {complete: false, review_requested: true, missing: ['付款截图']}),
      item('E'),
      item('F', {stage: 'cancelled', merged_into: 'A', complete: false, missing: ['付款截图']}),
    ],
    projects: [{id: 'p1', name: '预览测试项目'}], inbox: [],
    categories: ['材料采购', '餐饮', '酒店', '其他'],
    roles: {purchase: '收据', payment: '付款截图', invoice: '发票', other: '待分类'},
    stages: {draft: '待核对', submitted: '已提交', reimbursed: '已报销'},
    token: 'synthetic-test-token', report_template_ready: true,
    report_person: '合成报销人', category_auto_ready: false,
  };
  let releaseA;
  const delayedA = new Promise(resolve => {releaseA = resolve;});
  const response = x => ({
    id: x.id, version: x.version, complete: x.complete, missing: x.missing,
    partial: !x.complete, pdf: `${x.id}-${x.version}.pdf`,
    preview_pages: [`${x.id}-${x.version}.png`],
    layout: x.complete ? 'A4' : '仅已收材料预览（非正式报销包）', page_count: 1,
  });
  page.on('pageerror', error => errors.push(error.message));
  await page.route(`${origin}/**`, async route => {
    const request = route.request(), url = new URL(request.url());
    const json = (body, status = 200) => route.fulfill({
      status, contentType: 'application/json', body: JSON.stringify(body),
    });
    if (request.method() === 'POST') {
      const data = request.postDataJSON();
      requests.push({path: url.pathname, data});
      if (url.pathname === '/api/expense-print-preview') {
        const x = state.items.find(value => value.id === data.id);
        assert.ok(x, 'preview must target an existing expense');
        assert.equal(data.expected_version, x.version, 'preview must use the displayed version');
        const count = (previewCounts.get(x.id) || 0) + 1;
        previewCounts.set(x.id, count);
        if (x.id === 'A' && count === 1) await delayedA;
        if (x.id === 'E' && count === 1) return json({error: '合成预览暂时失败，请重试'}, 503);
        return json(response(x));
      }
      if (url.pathname === '/api/transition') {
        const x = state.items.find(value => value.id === data.id);
        assert.ok(x);
        assert.equal(data.expected_version, x.version);
        if (data.action === 'return_materials') {
          assert.ok(!x.complete && x.review_requested && !x.verified);
          Object.assign(x, {review_requested: false, version: `${x.id}-v2`});
          return json(x);
        }
        assert.equal(data.action, 'verify');
        assert.ok(x.complete && !x.verified && x.stage === 'draft');
        Object.assign(x, {verified: true, ready: true, version: `${x.id}-v2`});
        return json(x);
      }
      throw Error(`Unexpected write from preview flow: ${url.pathname}`);
    }
    if (url.pathname === '/api/state') return json(state);
    if (url.pathname === '/api/intake') return json({queue: []});
    if (url.pathname === '/api/preferences') return json({rules: [], learned: [], sections: {}});
    if (url.pathname === '/api/report-snapshot') return json({rows: []});
    if (url.pathname.startsWith('/export/') && url.pathname.endsWith('.png')) {
      return route.fulfill({contentType: 'image/png', body: png});
    }
    if (url.pathname.startsWith('/export/') && url.pathname.endsWith('.pdf')) {
      return route.fulfill({contentType: 'application/pdf', body: '%PDF-1.4\n% synthetic PDF'});
    }
    const name = url.pathname === '/' ? 'index.html' : url.pathname.slice(1);
    if (!/^[a-z-]+\.(html|js|css)$/.test(name)) return route.abort();
    return route.fulfill({
      contentType: name.endsWith('.js') ? 'text/javascript' : name.endsWith('.css') ? 'text/css' : 'text/html',
      body: await fs.readFile(path.resolve(__dirname, '../src/invoice_print_layout/web', name)),
    });
  });
  const select = id => page.locator(`[data-item="${id}"]`).click();
  const waitForImage = id => page.waitForFunction(expected => {
    const images = [...document.querySelectorAll('#expensePrintPages img')];
    return images.length === 1 && images[0].getAttribute('src').includes(`${expected}-`) &&
      images[0].complete && images[0].naturalWidth > 0;
  }, id);
  const count = id => previewCounts.get(id) || 0;
  try {
    await page.goto(`${origin}/?project=p1`);
    await page.locator('[data-item="A"]').waitFor();
    assert.equal(await page.locator('[data-view="ready"]').count(), 0, 'obsolete ready tab and card must be removed');
    assert.equal(await page.locator('#tabs [data-view="submitted"]').innerText(), '已核对');
    assert.equal(await page.locator('#tabs [data-view="reimbursed"]').count(), 0, 'expense reimbursed tab is removed');

    await select('A');
    await page.waitForFunction(() => document.getElementById('expensePrintStatus')?.textContent.includes('正在'));
    assert.equal(count('A'), 1);
    await select('B');
    await waitForImage('B');
    const aResponse = page.waitForResponse(res => res.url().endsWith('/api/expense-print-preview') &&
      res.request().postDataJSON().id === 'A');
    releaseA();
    await aResponse;
    // A completes after B, but must never replace the selected expense's page/link.
    await waitForImage('B');
    assert.ok((await page.locator('#expensePrintPdf').getAttribute('href')).includes('B-'));
    assert.equal(await page.locator('#editForm').count(), 0, 'expense preview must replace the text editor');

    await page.evaluate(() => {window.retainedPrintPage = document.querySelector('#expensePrintPages img'); window.retainedExpenseRow = document.querySelector('[data-item="B"]');});
    await page.locator('[data-check="B"]').check();
    await page.locator('[data-check="B"]').uncheck();
    assert.equal(await page.evaluate(() => window.retainedExpenseRow === document.querySelector('[data-item="B"]')), true, 'Checkbox changes must not rebuild the expense list');
    await page.locator('#selectVisible').check();
    assert.equal(await page.locator('#list [data-check]:checked').count(), 4);
    await page.locator('#selectVisible').uncheck();
    await page.locator('#search').fill('合成');
    await waitForImage('B');
    assert.equal(await page.evaluate(() => window.retainedPrintPage === document.querySelector('#expensePrintPages img')), true, 'An unchanged visible preview keeps its decoded image and DOM');
    assert.equal(count('B'), 1, 'selection/search rerenders must reuse an unchanged preview');
    await select('A');
    await waitForImage('A');
    assert.match(await page.locator('#detail').innerText(), /1 份补充材料，不计入金额/);
    assert.equal(count('A'), 1, 'a completed preview must be reused when revisiting the same version');
    await page.evaluate(() => {window.beforeRefreshPage = document.querySelector('#expensePrintPages img');});
    await page.evaluate(() => load());
    await waitForImage('A');
    assert.equal(count('A'), 2, 'A fresh state read must revalidate originals even when the expense version is unchanged');
    assert.equal(await page.evaluate(() => window.beforeRefreshPage === document.querySelector('#expensePrintPages img')), false);

    await select('D');
    await waitForImage('D');
    assert.equal(await page.locator('#expensePrintPages img').count(), 1, 'missing expense shows received originals');
    assert.match(await page.locator('#expensePrintStatus').innerText(), /仅已收材料.*付款截图/);
    assert.match(await page.locator('#detail .eyebrow').innerText(), /已收材料/);
    assert.ok(await page.locator('#expensePrintPdf').isVisible(), 'partial material PDF may be inspected');
    assert.equal(await page.locator('#detail [data-action="verify"]').count(), 1, 'partial material has an explicit acknowledgement path');
    assert.equal(state.items.find(x => x.id === 'D').verified, false);
    assert.equal(state.items.find(x => x.id === 'D').ready, false);
    const writesBeforeMissingDialog = requests.filter(x => x.path !== '/api/expense-print-preview').length;
    await page.locator('#detail [data-action="verify"]').click();
    await page.locator('#verifyDialog[open]').waitFor();
    assert.match(await page.locator('#verifyScope').innerText(), /付款截图/);
    assert.match(await page.locator('#confirmVerify').innerText(), /仍然核对完成/);
    assert.equal(requests.filter(x => x.path !== '/api/expense-print-preview').length, writesBeforeMissingDialog);
    await page.locator('#cancelVerify').click(); await page.waitForFunction(() => !singleVerifyBusy);
    assert.equal(requests.filter(x => x.path !== '/api/expense-print-preview').length, writesBeforeMissingDialog,
      'Cancelling missing-material review must not write or acknowledge the expense');
    assert.equal(state.items.find(x => x.id === 'D').verified, false);
    assert.equal(state.items.find(x => x.id === 'D').ready, false);
    assert.equal(count('D'), 1);
    assert.equal(await page.locator('#detail [data-action="return_materials"]').count(), 0, 'missing material remains in the unified review view');
    assert.match(await page.locator('[data-item="D"] .badge').innerText(), /待核对/);
    assert.deepEqual(state.items.find(x => x.id === 'D').missing, ['付款截图']);
    assert.equal(state.items.find(x => x.id === 'D').amount_cents, 2400);

    await select('E');
    await page.waitForFunction(() => document.getElementById('expensePrintStatus')?.textContent.includes('合成预览暂时失败'));
    const retry = page.locator('#detail').getByRole('button', {name: /重试/});
    await retry.click();
    await waitForImage('E');
    assert.equal(count('E'), 2, 'failed preview must have a usable retry');

    await page.locator('#tabs [data-view="review"]').click();
    assert.equal(await page.locator('[data-item="C"]').count(), 0, 'reviewed bill leaves review');
    await page.locator('#tabs [data-view="submitted"]').click();
    await page.locator('[data-item="C"]').waitFor();
    await select('C');
    await waitForImage('C');
    assert.match(await page.locator('#detail').innerText(), /已核对/);
    await page.locator('#makeExpenseReport').click();
    await page.locator('#reportDialog[open]').waitFor();
    assert.match(await page.locator('#reportScope').innerText(), /已选择 1 笔/);
    assert.ok(await page.locator('[data-check="C"]').isChecked(), 'single expense action must select its own bill');
    await page.locator('#closeReport').click();
    await page.locator('[data-check="C"]').uncheck();

    await page.locator('#tabs [data-view="review"]').click();
    await select('B');
    await waitForImage('B');
    const bBeforeVerification = count('B');
    await page.locator('#detail [data-action="verify"]').click();
    await page.waitForFunction(() => document.querySelector('[data-item="B"] .badge')?.textContent.includes('已核对'));
    assert.equal(state.items.find(x => x.id === 'B').stage, 'draft');
    assert.ok(await page.locator('[data-item="B"]').isVisible(), 'verified bill is visible in submission view');
    assert.equal(await page.locator('#tabs [data-view="submitted"]').getAttribute('class'), 'active');
    assert.equal(await page.locator('#list [data-item].selected').count(), 0, 'Completed fees do not retain preview focus in their new stage');
    assert.equal(await page.locator('[data-check="B"]').isChecked(), false);
    await select('B');
    await waitForImage('B');
    assert.equal(count('B'), bBeforeVerification + 1, 'changed bill version needs a new preview');
    await page.locator('#makeExpenseReport').click();
    await page.locator('#reportDialog[open]').waitFor();
    assert.match(await page.locator('#reportScope').innerText(), /已选择 1 笔/);
    await page.locator('#closeReport').click();
    await page.locator('[data-check="B"]').uncheck();

    await page.locator('#tabs [data-view="review"]').click();
    await page.locator('[data-check="A"]').check();
    assert.equal(await page.locator('#exportSelected').isVisible(),false);
    await page.evaluate(()=>$('exportSelected').onclick());
    await page.waitForFunction(() => !document.getElementById('error').hidden);
    assert.match(await page.locator('#error').innerText(), /已核对/);
    assert.equal(await page.locator('#reportDialog[open]').count(), 0, 'unreviewed draft must not open report production');

    await page.locator('#tabs [data-view="deleted"]').click();
    await select('F');
    assert.equal(count('F'), 0, 'merged original record is read-only and does not generate a separate print package');
    assert.match(await page.locator('[data-item="F"] .badge').innerText(), /已合并/);
    assert.match(await page.locator('#detail').innerText(), /原费用记录只读保留.*不参与待核对或待提交金额/);
    assert.equal(await page.locator('#detail [data-action="draft"]').count(), 0,
      'merged source expense must not offer independent restore and duplicate the group total');

    assert.equal(await page.locator('[data-view="ready"]').count(), 0);
    assert.equal(await page.locator('#tabs [data-view="submitted"]').innerText(), '已核对');
    assert.deepEqual(errors, []);
    assert.deepEqual(requests.filter(x => x.path !== '/api/expense-print-preview').map(x => x.path), ['/api/transition'],
      'preview, selection and opening report dialog must not update, submit or make reports');
    assert.equal(state.items.find(x => x.id === 'C').stage, 'draft');
    return {passed: true, scope: 'synthetic HTTP frontend only', previewRequests: Object.fromEntries(previewCounts),
      verifiedBillsInSubmissionView: true, automaticLedgerWrites: 0, liveEndToEnd: 'not run'};
  } finally {
    releaseA();
    await browser.close();
  }
}

module.exports = run;
if (require.main === module) run(require('playwright')).then(console.log).catch(error => {
  console.error(error); process.exitCode = 1;
});
