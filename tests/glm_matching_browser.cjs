'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');

async function run(pw) {
  const browser = await pw.chromium.launch({headless: true});
  const page = await browser.newPage({viewport: {width: 1440, height: 1000}});
  const origin = 'http://127.0.0.1:18795';
  const evidence = process.env.INVOICE_BROWSER_EVIDENCE ? path.resolve(process.env.INVOICE_BROWSER_EVIDENCE) :
    path.resolve(__dirname, '../workspace/invoice-operations/glm-integration-20261012');
  const errors = [], writes = [], prepareRequests = [];
  const state = {token: 'synthetic', projects: [{id: 'p1', name: '合成项目'}], items: [], inbox: [],
    categories: ['其他'], roles: {invoice: '发票', payment: '付款截图'},
    stages: {draft: '待核对', submitted: '已提交', reimbursed: '已报销'},
    category_auto_ready: false, report_template_ready: false, report_person: ''};
  const board = {vouchers: [], payments: [], other: [], matches: [], suggestions: [], report_types: [], supplement_candidates: [],
    ai: {enabled: true, available: true, model: 'glm-5.3-flash', message: '已启用'}};
  let unavailable = false, uploaded = 0;
  page.on('pageerror', error => errors.push(error.message));
  await page.route(origin + '/**', async route => {
    const url = new URL(route.request().url());
    const json = (body, status = 200) => route.fulfill({status, contentType: 'application/json', body: JSON.stringify(body)});
    if (url.pathname.startsWith('/api/')) {
      if (route.request().method() === 'POST') writes.push(url.pathname);
      if (url.pathname === '/api/state') return json(state);
      if (url.pathname === '/api/material-matching') return json(board);
      if (url.pathname === '/api/material-matching/auto') {
        if (unavailable) return json({error: '材料读取失败，原件保留'}, 400);
        return json({engine: 'rules', matched: 0, suggested: 0, suggestions: [], ai: {requests: 0}, issues: [], conflicts: [{reason: '存在多个合理组合，请手工选择'}]});
      }
      if (url.pathname === '/api/intake/receive') return json({id: 'synthetic-' + ++uploaded, revision: 0, status: 'received'});
      if (url.pathname === '/api/intake/prepare') {
        const body = route.request().postDataJSON(); prepareRequests.push(body);
        if (body.id === 'retry-payment') {
          board.payments[0].ai_analysis = {status: 'not_needed'};
          board.payments[0].issues = [];
          return json({message: '材料已重新识别，等待手工匹配', converted_to_pdf: false,
            entry: {...board.payments[0]}});
        }
        return json({converted_to_pdf: false,
          entry: {ai_analysis: {status: 'failed', message: 'GLM暂时不可用，材料保留'}}});
      }
      if (url.pathname === '/api/intake') return json({queue: []});
      return json({});
    }
    const name = url.pathname === '/' ? 'index.html' : url.pathname.slice(1);
    return route.fulfill({contentType: name.endsWith('.js') ? 'text/javascript' : name.endsWith('.css') ? 'text/css' : 'text/html',
      body: await fs.readFile(path.resolve(__dirname, '../src/invoice_print_layout/web', name))});
  });
  try {
    await fs.mkdir(evidence, {recursive: true});
    await page.goto(origin + '/?project=p1');
    await page.locator('#manualAgent summary').click();
    await page.evaluate(()=>window.dispatchEvent(new CustomEvent('invoice-settings-changed',{detail:{organize:{auto_after_upload:false,use_model:true,model_available:false}}})));
    await page.locator('#materialMatching').click();
    await page.locator('#matchingAuto:not([disabled])').waitFor();
    assert.equal(await page.locator('#matchingAIStatus').count(), 0);
    assert.equal(await page.locator('#matchingAuto').innerText(), '寻找合成组合');
    await page.locator('#matchingAuto').click();
    await page.locator('#matchingMessage').filter({hasText: '存在多个合理组合，请手工选择'}).waitFor();
    assert.match(await page.locator('#matchingMessage').innerText(), /找到 0 组建议组合.*下方手工选择材料.*右侧预览.*进入待核对.*存在多个合理组合/s);
    assert.doesNotMatch(await page.locator('#matchingMessage').innerText(), /主页面.*待合成/);
    board.ai.available = false;
    await page.locator('#matchingRefresh').click();
    await page.locator('#matchingAuto').click();
    await page.locator('#matchingMessage').filter({hasText: '存在多个合理组合，请手工选择'}).waitFor();
    assert.doesNotMatch(await page.locator('#matchingMessage').innerText(), /GLM暂时不可用/);
    unavailable = true;
    await page.locator('#matchingAuto').click();
    await page.locator('#matchingMessage').filter({hasText: '材料读取失败'}).waitFor();
    assert.equal(writes.filter(value => value === '/api/material-matching/auto').length, 3, 'no automatic retries');
    assert.match(await page.locator('#matchingMessage').innerText(), /原件保留/);
    unavailable = false;
    await page.locator('#matchingFiles').setInputFiles({name: 'synthetic.png', mimeType: 'image/png', buffer: Buffer.from('synthetic')});
    await page.locator('#matchingMessage').filter({hasText: '1份识别结果需人工处理'}).waitFor();
    assert.match(await page.locator('#matchingMessage').innerText(), /新增 1 份，已收过 0 份.*跳过 0 个.*失败 0 项.*原件留在材料区/);
    assert.match(await page.locator('#matchingProgressText').innerText(), /存疑材料留待人工处理/);
    board.ai.available = true;
    board.payments = [{key: 'q:retry-payment', id: 'retry-payment', role: 'payment', name: '合成付款截图.png',
      amount_cents: 12400, project_id: 'p1', revision: 1, status: 'review', thumbnail: '', url: '',
      fields: {merchant: '合成旅行社', category: '酒店', goods: '合成宾馆'}, issues: ['AI识别未完成'],
      ai_analysis: {status: 'failed', failure_code: 'evidence_invalid', failure_field: 'expense_date',
        message: '消费/付款日期未能在提取出的文字中核对。请重新识别或手工处理。'}}];
    await page.locator('#matchingRefresh').click();
    await page.getByText('消费/付款日期未能在提取出的文字中核对。请重新识别或手工处理。', {exact: true}).waitFor();
    await page.getByText('用途：合成宾馆', {exact: true}).waitFor();
    await page.locator('[data-prepare-id="retry-payment"]').click();
    await page.locator('#matchingMessage').filter({hasText: '材料已重新识别'}).waitFor();
    assert.equal(await page.locator('[data-prepare-id="retry-payment"]').count(), 0);
    assert.equal(prepareRequests.at(-1).force_ai, undefined, 'ordinary retry must not force cloud recognition');
    board.payments[0].issues = ['商品不明', '归属不明', '归属未定'];
    board.payments[0].project_id = '';
    board.payments[0].ai_analysis = {status: 'review', message: '购买内容仍需人工确认。'};
    await page.locator('#matchingRefresh').click();
    assert.equal(await page.locator('[data-prepare-id="retry-payment"]').count(), 0, 'manual review is not an extraction failure');
    assert.doesNotMatch(await page.locator('#matchingPayments').innerText(), /商品不明|归属不明|归属未定|未选择所属项目/);
    assert.equal(await page.locator('#intakeFilter option').filter({hasText: /商品不明|归属不明|归属未定/}).count(), 0);
    board.payments[0].project_id = 'p1';
    await page.locator('#matchingRefresh').click();
    assert.doesNotMatch(await page.locator('#matchingPayments').innerText(), /归属不明|归属未定|未选择所属项目/);
    assert.equal(await page.locator('#matchingAIStatus').count(), 0);
    assert.doesNotMatch(await page.locator('#matchingDeskDialog').innerText(), /关键字段不足|本地提取文字或OCR|编号分别用于区分和防重/);
    assert.equal(await page.locator('[data-assist-id]').count(), 0, 'manual AI recognition entry has been removed');
    assert.doesNotMatch(await page.locator('#matchingDeskDialog').innerText(), /AI辅助识别/);
    assert.deepEqual(prepareRequests.at(-1), {id: 'retry-payment'});
    assert.equal(prepareRequests.length, 2, 'only initial automatic preparation and ordinary failed-reading retry');
    assert.equal(writes.filter(value => value === '/api/material-matching/auto').length, 4, 'reading retry must not auto-match');
    assert.equal(writes.filter(value => /\/create$|\/match$|\/transition$/.test(value)).length, 0);
    assert.deepEqual(errors, []);
    await page.screenshot({path: path.join(evidence, 'synthetic-glm-ui.png')});
    const result = {passed: true, implementationStatusHidden: true, ambiguityRetained: true,
      failureNotRetried: true, failedExtractionCountVisible: true, detailedFailureVisible: true,
      explicitRecognitionRetry: true, recognizedPurposeVisible: true, retryDoesNotAutoMatch: true,
      rulesAvailableWithoutAI: true, manualAssistanceRemoved: true, fallbackExplanationHidden: true, manualReviewHasNoRetryButton: true,
      falseExpenseWrites: 0, pageErrors: errors};
    await fs.writeFile(path.join(evidence, 'glm-browser.json'), JSON.stringify(result, null, 2));
    return result;
  } finally {await browser.close();}
}
module.exports = run;
if (require.main === module) run(require('playwright')).then(console.log).catch(error => {console.error(error); process.exitCode = 1;});
