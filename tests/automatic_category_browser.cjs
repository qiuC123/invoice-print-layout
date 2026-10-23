const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const {chromium} = require('playwright');

const origin = 'http://127.0.0.1:18768';
const web = path.resolve(__dirname, '../src/invoice_print_layout/web');
const evidence = process.env.INVOICE_BROWSER_EVIDENCE || path.resolve(__dirname, '../workspace/invoice-operations/unified-intake-entry-20261010');
const staticFiles = new Set(['index.html', 'workbench-theme.js', 'workbench-theme.css', 'multi-filter.js', 'multi-filter.css', 'report-preview.css', 'app.js', 'receipt.js', 'report.js', 'mail-search.js',
  'expense-classifier.js', 'intake.js', 'matching-desk.js', 'matching-desk.css', 'style.css']);
const reportTypes = [{code: 'other', label: '其他', names: [], editable_name: true},
  {code: 'materials', label: '现场购材料', names: [], editable_name: true},
  {code: 'living', label: '生活费', names: ['住宿费(附清单)'], editable_name: false}];
const waitUntil = async predicate => {
  for (let n = 0; n < 160; n++) {
    if (predicate()) return;
    await new Promise(resolve => setTimeout(resolve, 25));
  }
  assert.fail('Timed out waiting for the synthetic request');
};

(async () => {
  const browser = await chromium.launch({headless: true});
  try {
    for (const scenario of ['normal', 'dialog-before', 'dialog-during', 'other-dialog',
      'needs-info', 'error-state', 'needs-info-response', 'request-error']) {
      const page = await browser.newPage({viewport: {width: 390, height: 844}});
      if (scenario === 'other-dialog') await page.setViewportSize({width: 1280, height: 900});
      const errors = [], posts = [];
      page.on('pageerror', error => errors.push(error.message));
      const item = {id: 'a', version: 'a'.repeat(64), title: '测试物资', project: '测试项目', project_id: 'p1',
        merchant: '测试商家', category: '其他', stage: 'draft', verified: false, ready: false,
        complete: false, amount: '10.00', amount_cents: 1000, note: '初始用途',
        invoice_state: 'not_requested', waiting_days: 0, expense_date: '',
        basis: '材料存在性检查；金额与内容需人工核对', missing: ['分类'], attachments: [], events: []};
      let status = {pending: !['needs-info', 'error-state'].includes(scenario),
        status: scenario === 'error-state' ? 'error' : 'needs_info', reason: '需要补充具体商品'};
      let calls = 0, release = null;
      const initiallyEnabled = !['dialog-before', 'other-dialog'].includes(scenario);
      await page.route(origin + '/**', async route => {
        const request = route.request(), url = new URL(request.url());
        const json = value => route.fulfill({contentType: 'application/json', body: JSON.stringify(value)});
        if (request.method() === 'POST') posts.push({path: url.pathname, body: request.postDataJSON()});
        if (url.pathname === '/api/state') return json({app_mode: 'invoice', logistics_available: false,
          items: [item], projects: [{id:'p1',name:'测试项目'}], inbox: [], categories: ['材料采购', '酒店', '其他'],
          roles: {invoice: '发票'}, stages: {draft: '待提交'}, token: 'synthetic',
          category_suggestion_ready: true, category_auto_ready: initiallyEnabled, classification: {a: status}});
        if (url.pathname === '/api/expense-print-preview') return json({id: item.id,
          version: item.version, complete: false, missing: item.missing, preview_pages: []});
        if (url.pathname === '/api/intake') return json({queue: []});
        if (url.pathname === '/api/category/auto') {
          calls++;
          assert.equal(request.postDataJSON().id, 'a');
          assert.equal(request.postDataJSON().retry, false, 'Background work must not implicitly retry');
          if (scenario === 'dialog-during') await new Promise(resolve => { release = resolve; });
          if (scenario === 'request-error') return route.fulfill({status: 400,
            contentType: 'application/json', body: JSON.stringify({error: 'Synthetic unavailable classifier'})});
          if (scenario === 'needs-info-response') {
            status = {pending: false, status: 'needs_info', saved: false, reason: '需要补充具体商品'};
            return json(status);
          }
          item.category = '材料采购'; item.missing = ['发票']; item.version = 'c'.repeat(64);
          status = {pending: false, status: 'saved', saved: true, category: '材料采购', source: 'cache',
            reason: '相同内容缓存', evidence: {goods: '<img src=x onerror=alert(1)>扎带'}};
          return json(status);
        }
        if (url.pathname === '/api/material-matching') return json({report_types: reportTypes,
          vouchers:[],payments:[],matches:[],other:[{key:'q:synthetic-support',id:'synthetic-support',role:'detail',
            name:'未保存材料.pdf',project_id:'p1',revision:1,fields:{title:'机器识别名称',category:'其他'},
            report_choice:{project_type:'other',project_name:'机器识别名称'},amount_cents:null,issues:[]}]});
        if (url.pathname.startsWith('/api/')) return route.fulfill({status: 400,
          contentType: 'application/json', body: JSON.stringify({error: 'Unexpected synthetic API request'})});
        const name = url.pathname === '/' ? 'index.html' : url.pathname.slice(1);
        if (!staticFiles.has(name)) return route.fulfill({status: 404, body: ''});
        return route.fulfill({contentType: name.endsWith('.js') ? 'text/javascript' :
          name.endsWith('.css') ? 'text/css' : 'text/html', body: await fs.readFile(path.join(web, name))});
      });
      await page.goto(origin + '/?project=p1');
      assert.equal(await page.getByRole('button', {name: '收件／待处理', exact: true}).count(), 0);
      assert.equal(await page.locator('#recognizeReceipt,#checkMail').count(), 0,
        'Collection must use the material matching desk rather than a duplicate main-page entry');
      assert.equal(await page.locator('#materialMatching').isVisible(), true);
      await page.locator('[data-item=a]').click();
      await page.locator('#expensePrintPages .print-empty').waitFor();
      assert.match(await page.locator('#expensePrintPages').innerText(), /分类|发票/);
      assert.equal(await page.locator('#editForm,#suggestCategory,#retryCategory,#applyCategorySuggestion').count(), 0);

      if (scenario === 'dialog-before' || scenario === 'other-dialog') {
        if (scenario === 'dialog-before') {await page.locator('#matchingDeskDialog').waitFor({state:'attached'});await page.locator('#materialMatching').click();await page.locator('[data-support-id=synthetic-support]').click();}
        else await page.locator('#createProject').click();
        await page.evaluate(() => { state.category_auto_ready = true; window.expenseClassifier.schedule(); });
        await page.waitForTimeout(500);
        assert.equal(calls, 0, 'An open dialog must pause automatic processing');
        if (scenario === 'dialog-before') {
          await page.locator('#matchingProjectType').selectOption('other');
          await page.locator('#matchingProjectName').fill('新建事项未保存用途');
          await page.locator('#matchingAmount').fill('12.34');
          await page.waitForTimeout(4500);
          assert.equal(calls, 0, 'Periodic scheduling must also respect the open dialog');
          assert.equal(await page.locator('#matchingProjectName').inputValue(), '新建事项未保存用途');
          assert.equal(await page.locator('#matchingProjectType').inputValue(), 'other');
          assert.equal(await page.locator('#matchingAmount').inputValue(), '12.34');
          await page.locator('#matchingClose').click();
        } else await page.locator('#project-create [data-cancel]').click();
      }
      if (scenario === 'dialog-during') {
        await waitUntil(() => release !== null);
        {await page.locator('#matchingDeskDialog').waitFor({state:'attached'});await page.locator('#materialMatching').click();await page.locator('[data-support-id=synthetic-support]').click();}
        await page.locator('#matchingProjectType').selectOption('materials');
        await page.locator('#matchingProjectName').fill('请求途中填写的未保存用途');
        await page.locator('#matchingAmount').fill('56.78');
        release();
        await page.waitForTimeout(600);
        assert.equal(await page.locator('#matchingProjectName').inputValue(), '请求途中填写的未保存用途');
        assert.equal(await page.locator('#matchingProjectType').inputValue(), 'materials');
        assert.equal(await page.locator('#matchingAmount').inputValue(), '56.78');
        assert.equal(await page.locator('[data-item=a] .sub').first().textContent().then(text => text.includes('材料采购')), false,
          'The result must defer page refresh while a dialog contains unsaved fields');
        await page.locator('#matchingClose').click();
      }

      if (['needs-info', 'error-state', 'needs-info-response', 'request-error'].includes(scenario)) {
        if (scenario.endsWith('response') || scenario === 'request-error') await waitUntil(() => calls === 1);
        await page.waitForTimeout(4500);
        assert.equal(calls, ['needs-info', 'error-state'].includes(scenario) ? 0 : 1,
          'Non-pending outcomes and failed requests must not enter an implicit retry loop');
        assert.equal(item.category, '其他');
      } else {
        await page.waitForFunction(() => document.querySelector('[data-item=a] .sub')?.textContent.includes('材料采购'));
        await page.waitForTimeout(scenario === 'normal' ? 4500 : 450);
        assert.equal(calls, 1, 'A saved result must not be classified again on re-render');
      }
      assert.equal(item.stage, 'draft'); assert.equal(item.verified, false); assert.equal(item.ready, false);
      assert.equal(item.amount_cents, 1000); assert.equal(item.note, '初始用途'); assert.deepEqual(item.events, []);
      assert.equal(posts.filter(post => !['/api/category/auto', '/api/expense-print-preview'].includes(post.path)).length, 0,
        'Classification and previews must not invoke update, verification, submission or suggestions');
      assert.equal(await page.locator('#categorySuggestion img').count(), 0);
      await page.setViewportSize({width: 390, height: 844});
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
      assert.deepEqual(errors, []);
      if (scenario === 'normal') {
        await fs.mkdir(evidence, {recursive: true});
        await page.screenshot({path: path.join(evidence, 'synthetic-automatic-category-mobile.png'), fullPage: true});
      }
      await page.close();
    }
    console.log('automatic category browser checks passed: read-only sidebar, background saves, dialog pauses, no implicit retries');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exit(1); });
