const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const {chromium} = require('playwright');

const origin = 'http://127.0.0.1:18767';
const web = path.resolve(__dirname, '../src/invoice_print_layout/web');
const evidence = process.env.INVOICE_BROWSER_EVIDENCE || path.resolve(__dirname, '../workspace/invoice-operations/expense-print-preview-20261010');
const staticFiles = new Set(['index.html', 'workbench-theme.js', 'workbench-theme.css', 'multi-filter.js', 'multi-filter.css', 'report-preview.css', 'app.js', 'receipt.js', 'report.js', 'mail-search.js',
  'expense-classifier.js', 'intake.js', 'matching-desk.js', 'matching-desk.css', 'style.css']);
const reportTypes = [{code: 'other', label: '其他', names: [], editable_name: true},
  {code: 'materials', label: '现场购材料', names: [], editable_name: true},
  {code: 'living', label: '生活费', names: ['住宿费(附清单)'], editable_name: false}];

(async () => {
  const browser = await chromium.launch({headless: true});
  try {
    const page = await browser.newPage({viewport: {width: 1280, height: 900}});
    const errors = [], posts = [];
    let available = true, delayed = true, release = null, suggestion = null, suggestionRelease = null;
    page.on('pageerror', error => errors.push(error.message));
    const common = {project: '项目甲', project_id: 'p1', merchant: '测试商家', category: '其他', stage: 'draft',
      complete: false, verified: false, ready: false, amount: '24.60', amount_cents: 2460,
      attachments: [], events: [], invoice_state: 'not_requested', waiting_days: 0, expense_date: ''};
    const items = [{...common, id: 'a', version: 'a'.repeat(64), title: '测试采购', missing: ['分类A']},
      {...common, id: 'b', version: 'b'.repeat(64), title: '另一事项', missing: ['分类B']}];
    await page.route(origin + '/**', async route => {
      const request = route.request(), url = new URL(request.url());
      const json = value => route.fulfill({contentType: 'application/json', body: JSON.stringify(value)});
      if (request.method() === 'POST') posts.push({path: url.pathname, body: request.postDataJSON()});
      if (url.pathname === '/api/runtime') return json({started_at:'synthetic-start',build_id:'synthetic-build'});
      if (url.pathname === '/api/state') return json({app_mode: 'invoice', logistics_available: false,
        items, projects: [{id:'p1',name:'测试项目'}], inbox: [], categories: ['材料采购', '酒店', '其他'], roles: {invoice: '发票'},
        stages: {draft: '待提交'}, token: 'synthetic', category_suggestion_ready: available,
        category_auto_ready: false, classification: {}});
      if (url.pathname === '/api/expense-print-preview') {
        const body = request.postDataJSON(), item = items.find(row => row.id === body.id);
        assert.ok(item); assert.equal(body.expected_version, item.version);
        if (item.id === 'a' && delayed) await new Promise(resolve => { release = resolve; });
        return json({id: item.id, version: item.version, complete: false,
          missing: item.missing, preview_pages: []});
      }
      if (url.pathname === '/api/category/suggest') {
        assert.equal(request.postDataJSON().id, 'a');
        await new Promise(resolve => { suggestionRelease = resolve; });
        if (suggestion.error) return route.fulfill({status: 503, contentType: 'application/json',
          body: JSON.stringify({error: suggestion.error})});
        return json(suggestion);
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
    const noEditor = async () => {
      assert.equal(await page.locator('#editForm,#suggestCategory,#retryCategory,#applyCategorySuggestion').count(), 0);
      assert.equal(await page.locator('#detail input[name],#detail textarea[name]').count(), 0);
      assert.equal(await page.locator('#detail').getByRole('button', {name: '保存事项', exact: true}).count(), 0);
    };
    await page.goto(origin + '/?project=p1');
    await page.locator('[data-item=a]').click();
    for (let n = 0; !release && n < 160; n++) await page.waitForTimeout(25);
    assert.ok(release, 'The selected expense must request its actual print preview');
    await noEditor();
    await page.locator('[data-item=b]').click();
    await page.locator('#expensePrintPages').filter({hasText: '分类B'}).waitFor();
    release(); delayed = false;
    await page.waitForTimeout(300);
    assert.equal(await page.locator('#detail h2').textContent(), '另一事项');
    assert.equal(await page.locator('#expensePrintPages').textContent(), '缺：分类B',
      'A late result must not replace another expense preview');
    await page.locator('[data-item=a]').click();
    await page.locator('#expensePrintPages').filter({hasText: '分类A'}).waitFor();
    await page.locator('[data-item=b]').click();
    await page.locator('#expensePrintPages').filter({hasText: '分类B'}).waitFor();
    assert.equal(posts.filter(post => post.path === '/api/expense-print-preview').length, 2,
      'Switching unchanged expenses should reuse previews');
    await noEditor();
    assert.deepEqual(posts.map(post => post.path), ['/api/expense-print-preview', '/api/expense-print-preview']);
    assert.ok(items.every(item => item.stage === 'draft' && !item.verified && !item.ready));

    // The material desk provides editable creation fields outside the print sidebar; the old manual entry is absent.
    {await page.locator('#matchingDeskDialog').waitFor({state:'attached'});await page.locator('#materialMatching').click();await page.locator('[data-support-id=synthetic-support]').click();}
    assert.equal(await page.locator('#matchingProjectType').inputValue(), 'other');
    assert.equal(await page.locator('#matchingProjectName').inputValue(), '机器识别名称');
    await page.locator('#matchingProjectName').fill('新建时可以填写用途');
    await page.locator('#matchingAmount').fill('12.34');
    assert.equal(await page.locator('#matchingProjectName').inputValue(), '新建时可以填写用途');
    assert.equal(await page.locator('#matchingAmount').inputValue(), '12.34');
    assert.equal(await page.locator('#matchingTitle,#matchingCategory').count(), 0);
    await page.locator('#matchingClose').click();
    await fs.mkdir(evidence, {recursive: true});
    await page.screenshot({path: path.join(evidence, 'synthetic-print-sidebar-desktop.png')});
    await page.setViewportSize({width: 390, height: 844});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await page.screenshot({path: path.join(evidence, 'synthetic-print-sidebar-mobile.png'), fullPage: true});
    available = false; await page.reload();
    await page.locator('[data-item=a]').click();
    await page.locator('#expensePrintPages').filter({hasText: '分类A'}).waitFor();
    await noEditor();
    assert.equal(posts.filter(post => post.path !== '/api/expense-print-preview').length, 0,
      'Selecting expenses must not request suggestions or save text changes');

    // The retained classification panel is mounted on a synthetic edit form:
    // ordinary print-sidebars remain read-only, without reintroducing an editor.
    await page.setViewportSize({width: 1280, height: 900});
    const mountClassification = async (automatic, status = {}) => page.evaluate(({automatic, status}) => {
      document.getElementById('categorySuggestion')?.remove();
      document.getElementById('editForm')?.remove();
      const form = document.createElement('form');form.id = 'editForm';
      form.innerHTML = '<select name="category"><option>其他</option><option>材料采购</option></select>';
      document.getElementById('detail').replaceChildren(form);active = 'a';state.category_suggestion_ready = true;
      state.category_auto_ready = automatic;state.classification = {a: status};
      window.expenseClassifier.mount(state.items.find(item => item.id === 'a'));
      // Background scheduling is outside this copy-only fixture.
      state.category_auto_ready = false;
    }, {automatic, status});
    await mountClassification(true, {pending: false, saved: true, category: '材料采购',
      source: 'cache', reason: '复用相同内容的分类缓存；未再次调用模型。', evidence: {goods: '诊断原文'}});
    assert.equal(await page.locator('#categorySuggestionResult').innerText(), '已保存：材料采购');
    assert.doesNotMatch(await page.locator('#categorySuggestion').innerText(), /Jev|缓存|模型|诊断原文|分类依据/);
    for (const [status, reason] of [['needs_info', '请补充具体商品或用途。'], ['error', '分类未完成，请重试。']]) {
      await mountClassification(true, {pending: false, saved: false, status, reason});
      assert.equal(await page.locator('#categorySuggestionResult').innerText(), reason);
      assert.equal(await page.locator('#retryCategory').isVisible(), true);
    }
    await mountClassification(true, {pending: true, reason: '内部判断过程'});
    assert.equal(await page.locator('#categorySuggestionResult').innerText(), '正在获取分类建议…');
    const requestSuggestion = async answer => {
      suggestion = answer;suggestionRelease = null;await mountClassification(false);
      await page.locator('#suggestCategory').click();
      for (let n = 0; !suggestionRelease && n < 160; n++) await page.waitForTimeout(25);
      assert.ok(suggestionRelease);assert.equal(await page.locator('#categorySuggestionResult').innerText(), '正在获取分类建议…');
      suggestionRelease();await page.waitForFunction(() => !document.getElementById('suggestCategory').disabled);
    };
    await requestSuggestion({category: '材料采购', source: 'jev', reason: '固定规则和模型判断理由',
      evidence: {goods: '内部提取原文'}, warnings: ['请核对付款金额。']});
    assert.equal(await page.locator('#categorySuggestionResult > p').first().innerText(), '建议：材料采购');
    assert.match(await page.locator('#categorySuggestionResult').innerText(), /请核对付款金额/);
    assert.doesNotMatch(await page.locator('#categorySuggestion').innerText(), /Jev|固定规则|模型|内部提取原文|判断的文字/);
    assert.equal(await page.locator('#applyCategorySuggestion').isVisible(), true);
    await page.locator('#categorySuggestion').screenshot({path: path.join(evidence, 'synthetic-concise-classification.png')});
    await requestSuggestion({category: null, source: 'local', reason: '请补充商品、服务或用途。', warnings: ['材料缺页，请补充。']});
    assert.match(await page.locator('#categorySuggestionResult').innerText(), /请补充商品、服务或用途.*材料缺页/s);
    assert.equal(await page.locator('#applyCategorySuggestion').count(), 0);
    await requestSuggestion({error: '分类暂不可用，请重试或手动选择。'});
    assert.equal(await page.locator('#categorySuggestionResult').innerText(), '分类暂不可用，请重试或手动选择。');
    assert.equal(posts.filter(post => !['/api/expense-print-preview', '/api/category/suggest'].includes(post.path)).length, 0);
    assert.deepEqual(errors, []);
    console.log('expense classifier browser checks passed: print sidebar, removed editor, stale-result protection, mobile layout, concise success copy with problem and loading feedback preserved');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exit(1); });
