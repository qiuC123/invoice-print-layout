// Classic scrollbars, actual stage content and pointer scrolling against synthetic responses.
const assert = require('node:assert/strict'), fs = require('node:fs/promises'), path = require('node:path');
async function run(pw) {
  const browser = await pw.chromium.launch({headless: true, ignoreDefaultArgs: ['--hide-scrollbars']});
  const origin = 'http://127.0.0.1:18842', allErrors = [], results = [];
  async function check(stable) {
    const page = await browser.newPage({viewport: {width: 1280, height: 1100}}); page.setDefaultTimeout(10000);
    let populatedSuggestions = false;
    const item = (id, title) => ({id, title, project_id: 'p1', project: '阶段布局测试', category: '酒店', amount_cents: 1000, amount: '10.00',
      stage: 'draft', verified: false, ready: false, complete: true, version: id + '-v1', expense_date: '', missing: [],
      attachments: [{id: id + '-source', name: 'synthetic.pdf', role: 'invoice', available: true}], events: []});
    const items = [item('short', '短凭证'), item('long', '长凭证'), ...Array.from({length: 25}, (_, index) => item('fee' + index, '合成费用 ' + index))];
    const state = {app_mode: 'invoice', token: 'synthetic', projects: [{id: 'p1', name: '阶段布局测试'}], items, inbox: [], categories: ['酒店'], roles: {invoice: '发票'}, stages: {draft: '待核对'}, category_auto_ready: false};
    page.on('pageerror', error => allErrors.push(error.message));
    await page.route(origin + '/**', async route => {
      const req = route.request(), url = new URL(req.url()), json = value => route.fulfill({contentType: 'application/json', body: JSON.stringify(value)});
      if (url.pathname === '/api/state') return json(state);
      if (url.pathname === '/api/reimbursement-people') return json({project_id: 'p1', people: [], summary: {rows: []}});
      if (url.pathname === '/api/material-matching') return json({vouchers: [], payments: [], other: [], matches: [],
        suggestions: populatedSuggestions ? Array.from({length: 30}, (_, index) => ({id: 'group' + index, title: '候选 ' + index,
          snapshot_token: 'token' + index, project_id: 'p1', amount_cents: 1000, voucher_keys: [], payment_ids: [], reason: '合计一致，请人工核对'})) : [], report_types: []});
      if (req.method() === 'POST') {
        assert.equal(url.pathname, '/api/expense-print-preview', 'Layout checks must never save or submit fees');
        const body = req.postDataJSON(); return json({id: body.id, version: body.expected_version, complete: true, missing: [], page_count: 1, pdf: 'synthetic.pdf', preview_pages: [body.id + '.svg']});
      }
      if (url.pathname.startsWith('/export/')) {
        const height = url.pathname.endsWith('long.svg') ? 5000 : 100;
        return route.fulfill({contentType: 'image/svg+xml', body: `<svg xmlns="http://www.w3.org/2000/svg" width="1000" height="${height}"><rect width="1000" height="${height}" fill="white"/><text x="20" y="60">SYNTHETIC PREVIEW</text></svg>`});
      }
      if (url.pathname.startsWith('/api/')) return json({queue: []});
      const name = url.pathname === '/' ? 'index.html' : url.pathname.slice(1);
      if (!/^[a-z-]+\.(html|js|css)$/.test(name)) return route.abort();
      let body = await fs.readFile(path.resolve(__dirname, '../src/invoice_print_layout/web', name), 'utf8');
      if (!stable && name === 'style.css') body = body.replace('html.invoice-assistant,html.embedded-workbench{scrollbar-gutter:stable}', '').replace('.invoice-assistant .detail,.embedded-workbench .detail{scrollbar-gutter:stable}', '').replace(/\/\* BEGIN fixed invoice workspace \*\/[\s\S]*?\/\* END fixed invoice workspace \*\//, '');
      return route.fulfill({contentType: name.endsWith('.js') ? 'text/javascript' : name.endsWith('.css') ? 'text/css' : 'text/html', body});
    });
    const geometry = () => page.evaluate(() => {
      const selectors = ['main', '.metrics', '.toolbar', '#workbenchActions', '.workspace', '.listpanel', '#detail', '#tabs', '.listhead'];
      const box = node => {const rect = node.getBoundingClientRect(); return {x: rect.x, width: rect.width};};
      return {outerOverflow: document.documentElement.scrollHeight > document.documentElement.clientHeight,
        main: box(document.querySelector('main')), boxes: Object.fromEntries(selectors.map(selector => [selector, box(document.querySelector(selector))])),
        metrics: [...document.querySelectorAll('.metric')].map(box), columns: getComputedStyle(document.querySelector('.workspace')).gridTemplateColumns,
        rootWidth: document.documentElement.clientWidth, detailWidth: document.getElementById('detail').clientWidth,
        listOverflow: document.getElementById('list').scrollHeight > document.getElementById('list').clientHeight};
    });
    const selectStage = async stage => {
      await page.locator(`#tabs [data-view=${stage}]`).click();
      await page.evaluate(() => scrollTo(0, 0)); return geometry();
    };
    const compare = (left, right) => {
      assert.equal(left.columns, right.columns);
      for (const selector of Object.keys(left.boxes)) {
        assert.ok(Math.abs(left.boxes[selector].x - right.boxes[selector].x) < 1, selector + ' x changed');
        assert.ok(Math.abs(left.boxes[selector].width - right.boxes[selector].width) < 1, selector + ' width changed');
      }
      left.metrics.forEach((metric, index) => {assert.ok(Math.abs(metric.x - right.metrics[index].x) < 1); assert.ok(Math.abs(metric.width - right.metrics[index].width) < 1);});
    };
    try {
      await page.goto(origin + '/?project=p1');
      const short = await selectStage('submitted'), long = await selectStage('review');
      assert.equal(short.outerOverflow, false, 'The short phase must genuinely have no outer overflow');
      assert.equal(long.outerOverflow, !stable, 'Only the old layout may scroll the entire long review page');
      const rootDelta = Math.abs(short.main.width - long.main.width);
      if (stable) {
        compare(short, long); compare(short, await selectStage('submitted')); await selectStage('review');
        assert.equal(long.listOverflow, true);
        assert.deepEqual(await page.locator('#workbenchActions button:visible').allTextContents(), ['人员金额', '材料合成台', '报销预览']);
        assert.equal(long.boxes['#workbenchActions'].x + long.boxes['#workbenchActions'].width, long.boxes['.toolbar'].x + long.boxes['.toolbar'].width);
        assert.equal(await page.getByRole('button', {name: '合并勾选的重复费用', exact: true}).count(), 0);
        assert.equal(await page.locator('#synthesisSelectionTools,[data-view=synthesis]').count(), 0);
        assert.equal(long.metrics.length, 2); assert.ok(Math.abs(long.metrics[0].width - long.metrics[1].width) < 1);
        const fixedGeometry = () => page.evaluate(() => Object.fromEntries(['header', '.metrics', '.toolbar', '#tabs', '.listhead'].map(selector => {
          const rect = document.querySelector(selector).getBoundingClientRect(); return [selector, {x: rect.x, y: rect.y, width: rect.width}];
        })));
        const fixedBefore = await fixedGeometry(), listRect = await page.locator('#list').boundingBox();
        await page.mouse.move(listRect.x + listRect.width / 2, listRect.y + 120); await page.mouse.wheel(0, 450);
        await page.waitForFunction(() => document.getElementById('list').scrollTop > 0);
        assert.deepEqual(await fixedGeometry(), fixedBefore);
        assert.equal(await page.locator('#detail').evaluate(el => el.scrollTop), 0);
        assert.equal(await page.evaluate(() => document.scrollingElement.scrollTop), 0);
        await page.locator('#list').evaluate(el => {el.scrollTop = el.scrollHeight;}); await page.mouse.wheel(0, 900);
        await page.waitForTimeout(150);
        assert.equal(await page.evaluate(() => document.scrollingElement.scrollTop), 0, 'List boundary must not chain to the page');
        await page.locator('#list').evaluate(el => {el.scrollTop = 0;});
      }
      await page.locator('[data-item=short] .title').click(); await page.waitForFunction(() => document.querySelector('#detail .print-pages img')?.complete);
      const detailGeometry = () => page.evaluate(() => ({width: document.getElementById('detail').clientWidth,
        imageWidth: document.querySelector('#detail .print-pages img').getBoundingClientRect().width,
        overflow: document.getElementById('detail').scrollHeight > document.getElementById('detail').clientHeight,
        top: document.scrollingElement.scrollTop, listWidth: document.querySelector('.listpanel').getBoundingClientRect().width}));
      const small = await detailGeometry(); assert.equal(small.overflow, false);
      await page.locator('[data-item=long] .title').click(); await page.waitForFunction(() => document.querySelector('#detail .print-pages img')?.complete);
      const tall = await detailGeometry(); assert.equal(tall.overflow, true);
      const detailDelta = Math.abs(small.imageWidth - tall.imageWidth);
      if (stable) {
        assert.equal(small.width, tall.width); assert.ok(detailDelta < 1, 'Preview image width shifted with the inner scrollbar');
        const listTop = await page.locator('#list').evaluate(el => el.scrollTop);
        const rect = await page.locator('#detail').boundingBox(); await page.mouse.move(rect.x + rect.width / 2, Math.min(rect.y + 200, 1000)); await page.mouse.wheel(0, 400);
        await page.waitForFunction(() => document.getElementById('detail').scrollTop > 0);
        const scrolled = await detailGeometry(); assert.equal(scrolled.top, tall.top); assert.equal(scrolled.listWidth, tall.listWidth);
        assert.equal(await page.locator('#list').evaluate(el => el.scrollTop), listTop);
        await page.locator('#detail').evaluate(el => {el.scrollTop = el.scrollHeight;}); await page.mouse.wheel(0, 900);
        await page.waitForTimeout(150); assert.equal(await page.evaluate(() => document.scrollingElement.scrollTop), 0);
        await page.setViewportSize({width: 390, height: 844});
        const mobileShort = await selectStage('submitted'), mobileLong = await selectStage('review'); compare(mobileShort, mobileLong);
        assert.equal(mobileShort.columns.trim().split(/\s+/).length, 1);
        assert.equal(mobileLong.outerOverflow, true, 'Phone keeps natural single-column scrolling');
        await page.setViewportSize({width: 1280, height: 1100}); populatedSuggestions = true; await page.reload();
        await selectStage('review');
        assert.equal(await page.locator('#list .synthesis-tools').count(), 0);
        const toolbarTop = await page.locator('.listhead').evaluate(el => el.getBoundingClientRect().top);
        const synthesisList = await page.locator('#list').boundingBox();
        await page.mouse.move(synthesisList.x + 150, synthesisList.y + 150); await page.mouse.wheel(0, 400);
        await page.waitForFunction(() => document.getElementById('list').scrollTop > 0);
        assert.equal(await page.locator('.listhead').evaluate(el => el.getBoundingClientRect().top), toolbarTop);
        assert.equal(await page.evaluate(() => document.scrollingElement.scrollTop), 0);
      } else {assert.ok(rootDelta > 5, 'Control must reproduce the root width jump'); assert.ok(detailDelta > 5, 'Control must reproduce the preview width jump');}
      return {stable, rootDelta, detailDelta, shortPageNoScrollbar: !short.outerOverflow, longPageScrollbar: long.outerOverflow, listOverflow: long.listOverflow};
    } finally {await page.close();}
  }
  try {results.push(await check(false)); results.push(await check(true)); assert.deepEqual(allErrors, []); return {passed: true, results, independentBothPaneScroll: true, fixedFiltersAndStageActions: true, mobileSingleColumn: true};}
  finally {await browser.close();}
}
module.exports = run;
if (require.main === module) run(require('playwright')).then(console.log).catch(error => {console.error(error); process.exitCode = 1;});
