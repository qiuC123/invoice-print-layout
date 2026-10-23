// Real preview DOM with synthetic composed pages; no real expense mutations.
const assert = require('node:assert/strict'), fs = require('node:fs/promises'), path = require('node:path');
async function run(pw) {
  const browser = await pw.chromium.launch({headless: true});
  const page = await browser.newPage({viewport: {width: 1272, height: 1000}});
  page.setDefaultTimeout(10000);
  const origin = 'http://127.0.0.1:18866', errors = [], calls = [], plans = [], releases = [], largePageRequests = new Set();
  const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j1ioAAAAASUVORK5CYII=', 'base64');
  const item = (id, category) => ({id, title: 'Synthetic ' + id, project_id: 'p1', project: 'Synthetic project', category,
    amount_cents: 100, amount: '1.00', stage: 'draft', verified: true, ready: true, complete: true,
    attachments: [], events: [], missing: [], version: 'v1', expense_date: '', merchant: '', order_number: ''});
  const items = [item('taxi', '打车'), item('hotel', '酒店')], before = JSON.stringify(items);
  let includeForeign=false,failNextStateRead=false;
  const foreign={...item('foreign','酒店'),project_id:'p2',project:'Other synthetic project'};
  const state = () => ({app_mode: 'invoice', token: 'synthetic', items:includeForeign?[...items,foreign]:items, projects: [{id: 'p1', name: 'Synthetic project'},...(includeForeign?[{id:'p2',name:'Other synthetic project'}]:[])],
    inbox: [], roles: {}, categories: ['打车', '酒店'], stages: {draft: '待核对'}, category_auto_ready: false,
    report_template_ready: true, report_person: 'Synthetic reporter'});
  const result = name => ({pdf: 'MATERIALS-ONLY.pdf', preview_pages: ['MATERIALS-ONLY.png'],
    combined_pdf: name + '-combined.pdf', combined_preview_pages: [name + '-table.png', name + '-materials.png'],
    combined_page_sizes: [{width: 841.89, height: 595.28}, {width: 595.28, height: 841.89}]});
  function hold(payload, status = 200) {
    let release, start;
    const gate = new Promise(resolve => {release = resolve;}), started = new Promise(resolve => {start = resolve;});
    plans.push({payload, status, gate, start}); releases.push(release); return {release, started};
  }
  page.on('pageerror', error => errors.push(error.message));
  await page.route(origin + '/**', async route => {
    const request = route.request(), url = new URL(request.url());
    const json = (body, status = 200) => route.fulfill({status, contentType: 'application/json', body: JSON.stringify(body)});
    if (request.method() === 'POST') {
      calls.push({path: url.pathname, body: request.postDataJSON()});
      if (url.pathname !== '/api/report-preview') throw Error('Unexpected business write: ' + url.pathname);
      assert.equal(request.headers()['x-workbench-token'], 'synthetic', 'The dialog uses its fresh read token');
      const plan = plans.shift();
      if (plan) {plan.start(); await plan.gate; return json(plan.payload, plan.status);}
      return json(result(request.postDataJSON().ids.join('-')));
    }
    if (url.pathname === '/api/state') {
      if(failNextStateRead){failNextStateRead=false;return json({error:'Synthetic state read failure'},500);}
      return json(state());
    }
    if (url.pathname === '/api/reimbursement-people') return json({project_id: 'p1', people: [], summary: {rows: []}});
    if (url.pathname.startsWith('/api/')) return json({queue: []});
    if (url.pathname.startsWith('/export/')) {
      if (url.pathname.includes('many-')) largePageRequests.add(url.pathname);
      return route.fulfill({contentType: 'image/png', body: png});
    }
    const name = url.pathname === '/' ? 'index.html' : url.pathname.slice(1);
    if (!/^[a-z-]+\.(html|js|css)$/.test(name)) return route.abort();
    return route.fulfill({contentType: name.endsWith('.js') ? 'text/javascript' : name.endsWith('.css') ? 'text/css' : 'text/html',
      body: await fs.readFile(path.resolve(__dirname, '../src/invoice_print_layout/web', name))});
  });
  const frame = page.frameLocator('#previewFrame');
  const ready = async name => {
    await frame.locator('img').first().waitFor();
    await page.waitForFunction(name => document.querySelector('#previewFrame').contentDocument?.querySelector('img')?.src.includes(name), name);
    assert.equal(await page.locator('#reportPreviewDialog').getAttribute('aria-busy'), 'false');
    assert.equal(await page.locator('#printPreview').isEnabled(), true);
  };
  try {
    await page.goto(origin + '/?project=p1'); await page.locator('#previewReport').waitFor();
    await page.evaluate(() => {state.token = 'OUTDATED-LIST-TOKEN';});
    const initial = hold(result('initial'));
    await page.locator('#previewReport').click(); await initial.started;
    assert.equal(await page.locator('#reportPreviewDialog').isVisible(), true);
    assert.equal(await page.locator('#previewLoadState progress').isVisible(), true);
    assert.equal(await page.locator('#closePreview').isEnabled(), true);
    assert.equal(await page.locator('#printPreview').isDisabled(), true);
    initial.release(); await ready('initial-table');
    assert.equal(await page.locator('#previewProject,#previewProjectToggle,#previewProjectMenu,.multi-filter[data-filter="previewProject"]').count(), 0);
    assert.equal(await frame.locator('img').count(), 2);
    assert.equal(await frame.locator('.landscape').count(), 1);
    assert.equal(await frame.locator('.portrait').count(), 1);
    assert.equal(await frame.locator('a,details,.expense,.notice,h1,nav').count(), 0, 'Only final page images are present');
    assert.match(await frame.locator('img').first().getAttribute('src'), /initial-table\.png$/);
    assert.doesNotMatch(await frame.locator('body').innerHTML(), /MATERIALS-ONLY|\/file\//);
    await page.evaluate(() => {window.openedPreview = []; window.open = (...args) => {window.openedPreview.push(args); return null;};});
    await page.locator('#printPreview').click();
    assert.deepEqual(await page.evaluate(() => window.openedPreview), [[origin + '/export/initial-combined.pdf', '_blank', 'noopener']]);
    const downloading = page.waitForEvent('download'); await page.locator('#downloadPreview').click();
    const download = await downloading, html = await fs.readFile(await download.path(), 'utf8');
    assert.equal(download.suggestedFilename(), '报销打印预览.html');
    assert.equal((html.match(/src="data:image\/png;base64,/g) || []).length, 2);
    assert.doesNotMatch(html, /http:|\/file\/|<details|<iframe|MATERIALS-ONLY/);
    assert.match(html, /@page landscape\{size:A4 landscape;margin:0\}/);
    assert.match(html, /figcaption\{display:none\}/);

    const geometry = [];
    for (const theme of ['light', 'dark']) {
      await page.locator('#closePreview').click(); await page.locator('#themeSelect').selectOption(theme);
      await page.locator('#previewReport').click(); await ready('taxi-hotel-table');
      geometry.push(await page.locator('#reportPreviewDialog').evaluate(dialog => ({theme: document.documentElement.dataset.theme,
        controls: [...dialog.querySelectorAll('.preview-tools button')].map(el => ({
          height: el.getBoundingClientRect().height, radius: getComputedStyle(el).borderRadius})),
        head: getComputedStyle(dialog.querySelector('h2')).fontSize})));
      assert.equal(await frame.locator('html').getAttribute('data-theme'), theme);
      assert.equal(await frame.locator('img').first().evaluate(el => getComputedStyle(el).backgroundColor), 'rgb(255, 255, 255)');
      assert.equal(await frame.locator('a,details').count(), 0);
    }
    assert.ok(geometry.every(row => row.head === '20px' && row.controls.length === 4 && row.controls.every(el => Math.abs(el.height - 40) < 1 && el.radius === '6px')));
    const allCount = calls.length;
    const oldFilter = hold(result('OLD-HOTEL'));
    await page.locator('#previewCategory').selectOption('酒店',{force:true}); await oldFilter.started;
    await page.locator('#previewCategory').selectOption('打车',{force:true}); await ready('taxi-table');
    oldFilter.release(); await page.waitForTimeout(50); await ready('taxi-table');
    await page.locator('#previewCategory').selectOption('',{force:true}); await ready('taxi-hotel-table');
    assert.equal(calls.length, allCount + 2, 'Returning to a frozen scope reuses its composed-page promise');

    const failed = hold({error: 'Synthetic <svg onload=alert(1)> failure'}, 500);
    await page.locator('#previewTrial').click(); await failed.started; failed.release();
    await page.locator('#previewLoadState .dialog-load-error').waitFor();
    assert.equal(await page.locator('#previewLoadState svg').count(), 0);
    assert.equal(await page.locator('#printPreview').isDisabled(), true);
    await page.locator('#previewLoadState [data-dialog-retry]').click(); await ready('taxi-hotel-table');

    const stale = hold(result('CLOSED-OLD'));
    await page.locator('#previewTrial').click(); await stale.started; await page.locator('#closePreview').click();
    const fresh = hold(result('fresh'));
    await page.locator('#previewReport').click(); await fresh.started; stale.release(); await page.waitForTimeout(30);
    assert.equal(await page.locator('#previewLoadState progress').isVisible(), true);
    fresh.release(); await ready('fresh-table');
    const many = result('many');
    many.combined_preview_pages = Array.from({length: 60}, (_, i) => `many-${i}.png`);
    many.combined_page_sizes = Array.from({length: 60}, () => ({width: 595.28, height: 841.89}));
    const large = hold(many);
    await page.locator('#previewTrial').click(); await large.started; large.release(); await ready('many-0');
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    assert.equal(await frame.locator('img').count(), 60);
    const initialLargePageRequests=largePageRequests.size;
    assert.ok(initialLargePageRequests < 10, `only nearby page images load initially, saw ${initialLargePageRequests}`);
    assert.equal(await frame.locator('img').last().getAttribute('loading'), 'lazy');
    assert.equal(await frame.locator('img').last().getAttribute('width'), '595');
    assert.equal(await frame.locator('img').last().getAttribute('height'), '842');
    assert.ok(await frame.locator('body').evaluate(node => node.scrollHeight > 50000), 'unloaded pages reserve their final size');
    await frame.locator('.print-page').last().scrollIntoViewIfNeeded();
    await page.waitForFunction(() => {
      const image=document.querySelector('#previewFrame').contentDocument?.querySelector('img[alt="最终打印页 第60页"]');
      return image?.src.endsWith('/many-59.png')&&image.complete&&image.naturalWidth>0;
    });
    assert.ok([...largePageRequests].some(url => url.endsWith('/many-59.png')), 'the last page loads when reached');
    await page.evaluate(() => {
      const doc=document.querySelector('#previewFrame').contentDocument;
      window.largePreviewBeforeTheme={first:doc.querySelector('img'),scroll:doc.scrollingElement.scrollTop};
    });
    await page.locator('#themeSelect').selectOption('dark',{force:true});
    assert.equal(await frame.locator('html').getAttribute('data-theme'),'dark');
    assert.equal(await page.evaluate(() => {
      const doc=document.querySelector('#previewFrame').contentDocument,before=window.largePreviewBeforeTheme;
      return before.first===doc.querySelector('img')&&before.scroll===doc.scrollingElement.scrollTop;
    }),true,'theme changes retain the current page and loaded images');
    await page.setViewportSize({width: 390, height: 844});
    assert.equal(await page.locator('#reportPreviewDialog').evaluate(el => el.scrollWidth <= el.clientWidth), true);
    assert.equal(await frame.locator('body').evaluate(el => el.scrollWidth <= innerWidth), true);
    await page.locator('#reportPreviewDialog').press('Escape');
    assert.equal(await page.locator('#reportPreviewDialog').isVisible(), false);

    // Explicit Agent scope must use its item IDs without modifying the original
    // desk's hidden selection or filters. All preceding preview checks stay intact.
    await page.setViewportSize({width:1272,height:1000});
    await page.evaluate(()=>{
      checked=new Set(['hotel']);setExpenseFilterValues('categoryFilter',['酒店']);
      document.getElementById('search').value='不会匹配任何费用';
      window.agentScopeBefore={checked:[...checked],category:expenseFilterValues('categoryFilter'),search:document.getElementById('search').value};
    });
    const explicit=hold(result('AGENT-TAXI'));
    await page.evaluate(()=>{window.agentPreviewRead=window.openAgentReportPreview({projectId:'p1',itemIds:['taxi']});});
    await explicit.started;
    assert.deepEqual(calls.at(-1).body.ids,['taxi'],'Agent preview ignores global hotel selection and category/search filters');
    explicit.release();await ready('AGENT-TAXI-table');
    assert.match(await page.locator('#previewScope').innerText(),/本次智能整理的 1 笔事项/);
    assert.deepEqual(await page.evaluate(()=>({checked:[...checked],category:expenseFilterValues('categoryFilter'),search:document.getElementById('search').value})),await page.evaluate(()=>agentScopeBefore));
    await page.locator('#closePreview').click();

    const beforeCrossProject=calls.length;
    const foreignProject=await page.evaluate(()=>{try{window.openAgentReportPreview({projectId:'p2',itemIds:['foreign']});return '';}catch(error){return error.message;}});
    assert.match(foreignProject,/项目已变化/);assert.equal(calls.length,beforeCrossProject);
    assert.equal(await page.locator('#reportPreviewDialog').isVisible(),false);
    includeForeign=true;
    await page.evaluate(()=>window.openAgentReportPreview({projectId:'p1',itemIds:['foreign']}));
    await page.locator('#previewLoadState .dialog-load-error').waitFor();
    assert.match(await page.locator('#previewLoadState').innerText(),/报销预览只包含当前项目/);
    assert.equal(calls.length,beforeCrossProject,'An item from another project cannot reach report composition');
    await page.locator('#closePreview').click();includeForeign=false;

    failNextStateRead=true;
    await page.evaluate(()=>window.openAgentReportPreview({projectId:'p1',itemIds:['taxi']}));
    await page.locator('#previewLoadState .dialog-load-error').waitFor();
    assert.match(await page.locator('#previewLoadState').innerText(),/无法读取预览数据/);
    assert.equal(calls.length,beforeCrossProject);
    await page.locator('#previewLoadState [data-dialog-retry]').click();await ready('taxi-table');
    assert.deepEqual(calls.at(-1).body.ids,['taxi'],'Retry after a source-read failure retains explicit Agent IDs');
    await page.locator('#closePreview').click();
    const agentFailed=hold({error:'Synthetic scoped composition failure'},500);
    await page.evaluate(()=>{window.agentPreviewRead=window.openAgentReportPreview({projectId:'p1',itemIds:['taxi']});});
    await agentFailed.started;agentFailed.release();
    await page.locator('#previewLoadState .dialog-load-error').waitFor();
    await page.locator('#previewLoadState [data-dialog-retry]').click();await ready('taxi-table');
    assert.deepEqual(calls.at(-1).body.ids,['taxi'],'Retry after composition failure also retains explicit Agent IDs');
    assert.deepEqual(await page.evaluate(()=>({checked:[...checked],category:expenseFilterValues('categoryFilter'),search:document.getElementById('search').value})),await page.evaluate(()=>agentScopeBefore));
    await page.locator('#closePreview').click();
    assert.equal(JSON.stringify(items), before); assert.deepEqual(errors, []);
    assert.ok(calls.every(call => call.path === '/api/report-preview'));
    return {passed: true, finalPdfAndImagesShared: true, printDoesNotPrintWorkbench: true,
      noOriginalLinkCards: true, offlineHtmlEmbedsExactPages: true, immediateClosableLoading: true,
      staleResponsesIsolated: true, retryableFailure: true, frozenScopeCache: true, largeReportsLoadNearbyPages: true,
      agentExactScopeIgnoresGlobalChecksAndFilters:true,agentCrossProjectRejected:true,agentReadAndCompositionRetryRetainsIds:true,agentPreservesManualScope:true,
      initialLargePageRequests, themeRetainsPreviewPosition: true,
      themesGeometry: geometry, mobileOverflow: false, businessWrites: 0};
  } finally {for (const release of releases) release(); await browser.close();}
}
module.exports = run;
if (require.main === module) run(require('playwright')).then(result => console.log(JSON.stringify(result))).catch(error => {console.error(error); process.exitCode = 1;});
