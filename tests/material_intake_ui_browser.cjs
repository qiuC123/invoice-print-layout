// Actual page scripts with synthetic APIs: no mail, cloud request, or live ledger writes.
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');

async function run(pw) {
  const browser = await pw.chromium.launch({headless: true});
  const page = await browser.newPage({viewport: {width: 1450, height: 1000}});
  page.setDefaultTimeout(10000);
  const origin = 'http://127.0.0.1:18793', errors = [], writes = [];
  const assets = path.resolve(__dirname, '../src/invoice_print_layout/web');
  const projects = [{id: 'p1', name: 'Synthetic One'}, {id: 'p2', name: 'Synthetic Two'}];
  const state = {app_mode: 'invoice', token: 'synthetic', projects, items: [], inbox: [],
    categories: ['其他'], roles: {invoice: '发票', payment: '付款截图'}, stages: {}, category_auto_ready: false};
  const card = {id: 'old', key: 'q:old', name: 'synthetic-existing.png', revision: 1, role: 'invoice',
    project_id: 'p1', amount_cents: 3000, status: 'review', issues: [], fields: {amount: '30.00', category: '其他'},
    url: '/intake-file/old', thumbnail: '/intake-file/old'};
  const board = {vouchers: [card], payments: [], other: [], unused: [], matches: [], suggestions: [],
    report_types: [], supplement_candidates: [], ai: {available: false}};
  let claimFails = true, prepareFails = true, mailFails = true, holdClaim = null, holdMail = null;
  let boardReads = 0;
  const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j1ioAAAAASUVORK5CYII=', 'base64');
  page.on('pageerror', error => errors.push(error.message));
  await page.addInitScript(() => {window.prompt = () => {throw Error('prompt() is not supported.');};});
  await page.route(origin + '/**', async route => {
    const req = route.request(), url = new URL(req.url());
    const json = (value, status = 200) => route.fulfill({status, contentType: 'application/json', body: JSON.stringify(value)});
    if (req.method() === 'POST') {
      const body = req.postDataJSON(); writes.push({route: url.pathname, body});
      if (url.pathname === '/api/intake/claim') {
        if (holdClaim) await holdClaim;
        if (claimFails) {claimFails = false; return json({error: 'synthetic claim unavailable'}, 503);}
        if (body.url.includes('/manual')) return json({status: 'manual', url: body.url, message: '请打开领取入口，再上传PDF。'});
        return json({status: 'received', entry: {id: 'claimed', status: 'received'}, message: '原始PDF已保存到材料区。'});
      }
      if (url.pathname === '/api/intake/prepare') {
        if (prepareFails) {prepareFails = false; return json({error: 'synthetic preparation unavailable'}, 503);}
        return json({entry: {id: 'claimed', status: 'review'}});
      }
      if (url.pathname === '/api/mail') {
        if (holdMail) await holdMail;
        if (mailFails) {mailFails = false; return json({error: 'synthetic mail unavailable'}, 503);}
        return json({message: '补收完成，材料已放入合成台。', downloaded: 1});
      }
      throw Error('Unexpected mutation: ' + url.pathname);
    }
    if (url.pathname === '/api/state') return json(state);
    if (url.pathname === '/api/material-matching') {boardReads++; return json({...board,
      vouchers: url.searchParams.get('project') === 'p2' ? [] : board.vouchers});}
    if (url.pathname === '/api/intake') return json({queue: []});
    if (url.pathname.startsWith('/intake-file/')) return route.fulfill({contentType: 'image/png', body: png});
    const name = url.pathname === '/' ? 'index.html' : url.pathname.slice(1);
    if (!/^[a-z-]+\.(html|js|css)$/.test(name)) return route.abort();
    return route.fulfill({contentType: name.endsWith('.js') ? 'text/javascript' : name.endsWith('.css') ? 'text/css' : 'text/html', body: await fs.readFile(path.join(assets, name))});
  });
  const closeClaim = () => page.locator('#invoiceClaimDialog [data-claim-close]').first().click();
  const submit = () => page.locator('#invoiceClaimSubmit').click();
  try {
    await page.goto(origin + '/?project=p1');
    await page.locator('#materialMatching').click();
    assert.equal(await page.locator('#recognizeReceipt, #checkMail').count(), 0);
    assert.equal(await page.locator('#matchingDeskDialog #matchingSuggestions').count(), 1, 'proposed combinations belong to the material desk');
    assert.equal(await page.locator('#tabs [data-view="synthesis"]').count(), 0);
    assert.equal(await page.locator('#metrics .metric').count(), 2);
    assert.equal(await page.locator('#matchingUpload').isVisible(), true);
    assert.equal(await page.locator('#matchingMail').isVisible(), true);
    assert.equal(await page.locator('#matchingClaim').isVisible(), true);
    assert.equal(await page.locator('#previewReport').count(), 1);
    assert.equal(await page.locator('#createProject').count(), 1);
    assert.equal(await page.locator('#matchingSave').evaluate(node => getComputedStyle(node).cursor), 'default');
    await page.locator('[data-voucher-key="q:old"]').click();
    await page.locator('#matchingClaim').click();
    await submit();
    assert.match(await page.locator('#invoiceClaimStatus').innerText(), /有效.*链接/);
    await page.locator('#invoiceClaimUrl').fill('javascript:alert(1)'); await submit();
    assert.equal(writes.length, 0, 'empty and invalid URLs do not write');
    await closeClaim();
    assert.equal(writes.length, 0, 'cancel does not write');
    await page.locator('#matchingClaim').click();
    await page.locator('#invoiceClaimUrl').fill('https://tax.example/pdf');
    let releaseClaim; holdClaim = new Promise(resolve => {releaseClaim = resolve;});
    await submit();
    await page.waitForFunction(() => document.getElementById('invoiceClaimSubmit').disabled);
    assert.equal(await page.locator('#invoiceClaimSubmit').evaluate(node => getComputedStyle(node).cursor), 'default');
    await page.locator('#invoiceClaimForm').evaluate(node => {node.requestSubmit(); node.requestSubmit();});
    releaseClaim(); holdClaim = null;
    await page.locator('#invoiceClaimStatus').filter({hasText: 'synthetic claim unavailable'}).waitFor();
    assert.equal(writes.filter(x => x.route === '/api/intake/claim').length, 1, 'pending duplicate submission suppressed');
    assert.equal(await page.locator('#invoiceClaimUrl').inputValue(), 'https://tax.example/pdf');
    await submit();
    await page.locator('#invoiceClaimStatus').filter({hasText: '整理未完成'}).waitFor();
    assert.equal(await page.locator('#invoiceClaimUrl').isDisabled(), true);
    await submit();
    await page.locator('#invoiceClaimStatus').filter({hasText: '原始PDF已保存到材料区'}).waitFor();
    assert.equal(await page.locator('#invoiceClaimSubmit').isDisabled(), true);
    assert.equal(writes.filter(x => x.route === '/api/intake/claim').length, 2, 'preparation retry reuses saved PDF');
    assert.deepEqual(writes.filter(x => x.route === '/api/intake/prepare').map(x => x.body), [{id: 'claimed'}, {id: 'claimed'}]);
    assert.equal(writes.find(x => x.route === '/api/intake/claim').body.project_id, 'p1');
    await closeClaim();
    assert.equal(await page.locator('[data-voucher-key="q:old"]').getAttribute('aria-pressed'), 'true', 'unchanged selection survives material refresh');
    await page.locator('#matchingClaim').click();
    await page.locator('#invoiceClaimUrl').fill('https://tax.example/manual'); await submit();
    await page.locator('#invoiceClaimLink a').waitFor();
    assert.equal(await page.locator('#invoiceClaimLink a').getAttribute('href'), 'https://tax.example/manual');
    assert.match(await page.locator('#invoiceClaimLink a').getAttribute('rel'), /noopener/);
    await closeClaim();
    await page.locator('#matchingMail').click();
    await page.locator('#matchingMessage').filter({hasText: 'synthetic mail unavailable'}).waitFor();
    let releaseMail; holdMail = new Promise(resolve => {releaseMail = resolve;});
    await page.locator('#matchingMail').click();
    assert.equal(await page.locator('#matchingMail').isDisabled(), true);
    await page.locator('#matchingMail').evaluate(node => node.dispatchEvent(new MouseEvent('click')));
    releaseMail(); holdMail = null;
    await page.locator('#matchingMessage').filter({hasText: '补收完成，材料已放入合成台'}).waitFor();
    assert.equal(writes.filter(x => x.route === '/api/mail').length, 2);
    assert.deepEqual(writes.filter(x => x.route === '/api/mail').map(x => x.body), [
      {project_id: 'p1', material_only: true}, {project_id: 'p1', material_only: true}]);
    assert.ok(boardReads >= 4, 'claim and mail refresh materials');
    await page.setViewportSize({width: 390, height: 844});
    await page.locator('#matchingClaim').click();
    assert.equal(await page.locator('#invoiceClaimDialog').evaluate(node => node.getBoundingClientRect().width <= window.innerWidth), true);
    await closeClaim(); await page.locator('#matchingClose').click();
    await page.goto(origin+'/?project=p2');
    await page.locator('#materialMatching').click();
    assert.equal(await page.locator('#matchingProject').count(),0);
    await page.locator('#matchingClaim').click();
    await page.locator('#invoiceClaimUrl').fill('https://tax.example/manual'); await submit();
    await page.locator('#invoiceClaimLink a').waitFor();
    assert.equal(writes.at(-1).body.project_id, 'p2', 'claim uses selected synthesis project');
    assert.deepEqual(errors, []);
    assert.equal(writes.some(x => /create|analyze|resolve|projects|report$/.test(x.route)), false, 'collection never creates a fee');
    return {passed: true, writes: writes.length, boardReads, no_live_data: true,
      scope: 'single collection entry, project-scoped mail, page-owned claim form, validation/cancel, duplicate suppression, retry without re-claim, selection preservation, default disabled cursor, narrow screen'};
  } finally {await browser.close();}
}
module.exports = run;
if (require.main === module) run(require('playwright')).then(result => console.log(JSON.stringify(result))).catch(error => {console.error(error); process.exitCode = 1;});
