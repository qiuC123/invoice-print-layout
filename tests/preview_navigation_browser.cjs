// Synthetic report scopes, delayed responses and unchanged print DOM; no real ledger.
const assert = require('node:assert/strict'), fs = require('node:fs/promises'), path = require('node:path');
async function run(pw) {
  const browser = await pw.chromium.launch({headless: true});
  const page = await browser.newPage({viewport: {width: 1272, height: 1244}});
  page.setDefaultTimeout(10000);
  const origin = 'http://127.0.0.1:18860', errors = [], writes = [], reads = new Map(), printReads = new Map();
  const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j1ioAAAAASUVORK5CYII=', 'base64');
  const items = Array.from({length: 72}, (_, i) => ({id: 'synthetic' + i, title: 'Synthetic expense ' + i,
    project_id: 'p1', project: 'Synthetic project', category: i % 2 ? '餐饮' : '酒店',
    reimbursement_person_id: i % 2 ? 'personB' : 'personA', reimbursement_person_label: i % 2 ? 'Person B' : 'Person A',
    amount_cents: 100, amount: '1.00', stage: 'draft', verified: false, complete: true, ready: false,
    missing: [], version: 'v1', expense_date: '2026-10-15', merchant: '', order_number: '', note: '',
    attachments: [{id: 'proof' + i, name: 'proof' + i + '.png', available: true, role: 'invoice'}], events: []}));
  const state = {app_mode: 'invoice', token: 'synthetic', projects: [{id: 'p1', name: 'Synthetic project'}], items,
    inbox: [], categories: ['酒店', '餐饮'], roles: {invoice: '发票'}, stages: {draft: '待核对'},
    category_auto_ready: false, report_template_ready: true, report_person: 'Synthetic reporter'};
  const people = {project_id: 'p1', people: ['A', 'B'].map(name => ({id: 'person' + name,
    label: 'Person ' + name, state: 'active', project_id: 'p1', version: 'pv1'})), summary: {rows: []}};
  let originalReads = 0, releaseHotel, holdHotel = false;
  const hotelGate = new Promise(resolve => {releaseHotel = resolve;});
  page.on('pageerror', error => errors.push(error.message));
  await page.route(origin + '/**', async route => {
    const request = route.request(), url = new URL(request.url());
    const json = value => route.fulfill({contentType: 'application/json', body: JSON.stringify(value)});
    if (request.method() === 'POST') {
      if (url.pathname === '/api/report-preview') {
        const ids = request.postDataJSON().ids, key = JSON.stringify(ids);
        reads.set(key, (reads.get(key) || 0) + 1);
        const selected = ids.map(id => items.find(x => x.id === id));
        if (holdHotel && selected.length === 36 && selected.every(x => x.category === '酒店')) await hotelGate;
        return json({combined_pdf: key.length + '-combined.pdf', combined_preview_pages: ['table.png', ...ids.map(id => id + '.png')],
          combined_page_sizes: [{width: 842, height: 595}, ...ids.map(() => ({width: 595, height: 842}))]});
      }
      writes.push(url.pathname);
      if (url.pathname !== '/api/expense-print-preview') throw Error('No business writes are allowed');
      const body = request.postDataJSON(), item = items.find(x => x.id === body.id);
      printReads.set(item.id, (printReads.get(item.id) || 0) + 1);
      return json({id: item.id, version: item.version, complete: true, partial: false, missing: [],
        pdf: item.id + '.pdf', preview_pages: [item.id + '.png'], page_count: 1, layout: 'A4'});
    }
    if (url.pathname === '/api/state') return json(state);
    if (url.pathname === '/api/reimbursement-people') return json(people);
    if (url.pathname === '/api/material-matching') return json({vouchers: [], payments: [], other: [], matches: [], suggestions: []});
    if (url.pathname.startsWith('/api/')) return json({queue: []});
    if (url.pathname.startsWith('/file/')) {originalReads++; return route.fulfill({contentType: 'image/png', body: png});}
    if (url.pathname.startsWith('/export/')) return route.fulfill({contentType: 'image/png', body: png});
    const name = url.pathname === '/' ? 'index.html' : url.pathname.slice(1);
    if (!/^[a-z-]+\.(html|js|css)$/.test(name)) return route.abort();
    return route.fulfill({contentType: name.endsWith('.js') ? 'text/javascript' : name.endsWith('.css') ? 'text/css' : 'text/html',
      body: await fs.readFile(path.resolve(__dirname, '../src/invoice_print_layout/web', name))});
  });
  const frame = page.frameLocator('#previewFrame');
  const scope = async (count, total) => {
    await page.waitForFunction(({count, total}) => {
      const doc = document.getElementById('previewFrame').contentDocument;
      return doc?.querySelectorAll('.print-page img').length === count + 1 && document.querySelector('#previewScope').textContent.includes(total);
    }, {count, total});
  };
  try {
    await page.goto(origin + '/?project=p1'); await page.locator('#personFilter option[value=personA]').waitFor({state: 'attached'});
    await page.locator('[data-item=synthetic0]').click(); await page.locator('#expensePrintPages img').waitFor();
    await page.evaluate(() => {window.retainedPrint = document.querySelector('#expensePrintPages img');});
    await page.locator('#selectVisible').check(); assert.equal(await page.locator('[data-check]:checked').count(), 72);
    await page.locator('#selectVisible').uncheck();
    await page.locator('#search').fill('Synthetic'); await page.locator('#categoryFilter').selectOption('酒店',{force:true});
    await page.locator('#personFilter').selectOption('personA',{force:true});
    assert.equal(await page.locator('#list [data-item]').count(), 36);
    assert.equal(await page.evaluate(() => window.retainedPrint === document.querySelector('#expensePrintPages img')), true);
    assert.equal(printReads.get('synthetic0'), 1);
    await page.locator('#previewReport').click(); await scope(36, '36.00');
    assert.match(await page.locator('#previewScope').innerText(), /人员/);
    assert.equal(originalReads, 0, 'Final preview must use composed pages, never originals');
    assert.equal(await frame.locator('details, a, .expense').count(), 0, 'Final material images replace original links and cards');
    const downloadPromise = page.waitForEvent('download'); await page.locator('#downloadPreview').click();
    const download = await downloadPromise, html = await fs.readFile(await download.path(), 'utf8');
    assert.equal((html.match(/src="data:image\/png;base64,/g) || []).length, 37);
    assert.doesNotMatch(html, /data-preview-src|\/file\//);
    await page.locator('#closePreview').click(); await page.locator('#search').fill('');
    await page.locator('#categoryFilter').selectOption('',{force:true}); await page.locator('#personFilter').selectOption('',{force:true});

    await page.locator('#previewReport').click(); await scope(72, '72.00');
    const allKey = JSON.stringify(items.map(x => x.id)), diningKey = JSON.stringify(items.filter(x => x.category === '餐饮').map(x => x.id));
    const fullBefore = reads.get(allKey);
    holdHotel = true;
    const hotelStarted = page.waitForRequest(req => req.url().endsWith('/api/report-preview') && req.postDataJSON().ids.length === 36);
    await page.locator('#previewCategory').selectOption('酒店',{force:true}); await hotelStarted;
    await page.locator('#previewCategory').selectOption('餐饮',{force:true}); await scope(36, '36.00');
    assert.match(await frame.locator('img').nth(1).getAttribute('src'), /synthetic1\.png$/);
    const diningBefore = reads.get(diningKey);
    await page.locator('#previewCategory').selectOption('',{force:true}); await scope(72, '72.00');
    await page.locator('#previewCategory').selectOption('餐饮',{force:true}); await scope(36, '36.00');
    assert.equal(reads.get(allKey), fullBefore); assert.equal(reads.get(diningKey), diningBefore, 'A report session reuses its own frozen grouping snapshot');
    const hotelFinished = page.waitForResponse(res => res.url().endsWith('/api/report-preview') && res.request().postDataJSON().ids[0] === 'synthetic0');
    releaseHotel(); await hotelFinished;
    await page.waitForTimeout(50); await scope(36, '36.00');
    assert.match(await frame.locator('img').nth(1).getAttribute('src'), /synthetic1\.png$/, 'An old slow filter must not replace the latest scope');
    await page.locator('#closePreview').click();
    items[0].amount_cents = 2500; items[0].amount = '25.00'; items[0].version = 'v2';
    await page.locator('#previewReport').click(); await scope(72, '96.00');
    assert.equal(reads.get(allKey), fullBefore + 1, 'Reopening reads fresh current amounts and grouping');
    await page.locator('#closePreview').click();

    const spacing = [];
    for (const theme of ['light', 'dark']) {
      await page.locator('#themeSelect').selectOption(theme,{force:true}); await page.locator('#tabs [data-view=review]').click();
      const gap = await page.evaluate(() => {
        const el = document.querySelector('.listhead');
        const bottom = Math.max(...[...document.querySelectorAll('#selectionTools button')].filter(button => button.offsetParent).map(button => button.getBoundingClientRect().bottom));
        return el.getBoundingClientRect().bottom - bottom - parseFloat(getComputedStyle(el).borderBottomWidth);
      });
      assert.ok(gap >= 12, theme + ' button-to-divider gap: ' + gap); spacing.push({theme, gap});
      assert.equal(await page.locator('[data-view=synthesis],#synthesisSelectionTools').count(), 0);
      await page.locator('#tabs [data-view=submitted]').click(); await page.locator('#tabs [data-view=deleted]').click();
      await page.locator('#tabs [data-view=review]').click(); await page.locator('[data-item=synthetic0]').waitFor();
      assert.equal(await page.locator('#list [data-item]').count(), 72);
    }
    assert.deepEqual(errors, []); assert.ok(writes.every(route => route === '/api/expense-print-preview'));
    return {passed: true, expenseCount: 72, unchangedPrintDomRetained: true, selectionPreserved: true,
      personScopeRespected: true, originalDownloads: 0, standaloneDownloadWorks: true,
      reportSessionGroupingReused: true, slowFilterCannotOverwriteLatest: true, reopenReadsFreshData: true,
      bothThemesAndAllStagesChecked: true, spacing, businessWrites: 0};
  } finally {releaseHotel(); await browser.close();}
}
module.exports = run;
if (require.main === module) run(require('playwright')).then(result => console.log(JSON.stringify(result))).catch(error => {console.error(error); process.exitCode = 1;});
