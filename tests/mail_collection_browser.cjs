// Real page scripts with synthetic mail responses; never contacts a mailbox or writes a live ledger.
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');

async function run(pw) {
  const browser = await pw.chromium.launch({headless: true});
  const page = await browser.newPage({viewport: {width: 1450, height: 1000}});
  page.setDefaultTimeout(10000);
  const origin = 'http://127.0.0.1:18794', errors = [], writes = [];
  const assets = path.resolve(__dirname, '../src/invoice_print_layout/web');
  const state = {app_mode: 'invoice', token: 'synthetic', projects: [{id: 'p1', name: 'Synthetic One'}, {id: 'p2', name: 'Synthetic Two'}],
    items: [], inbox: [], categories: ['其他'], roles: {invoice: '发票', payment: '付款截图'}, stages: {}, category_auto_ready: false};
  const card = (id, role = 'invoice', extra = {}) => ({id, key: `q:${id}`, name: `${id}.png`, revision: 1, role,
    project_id: 'p1', amount_cents: 3000, status: 'review', issues: [], fields: {amount: '30.00', category: '其他'},
    url: `/intake-file/${id}`, thumbnail: `/intake-file/${id}`, ...extra});
  const old = card('existing-voucher'), oldPayment = card('existing-payment', 'payment');
  const board = {vouchers: [old], payments: [oldPayment], other: [], unused: [], matches: [], suggestions: [],
    report_types: [], supplement_candidates: [], ai: {available: false}};
  let mode = 'collected', mailHold = null, boardReads = 0;
  const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j1ioAAAAASUVORK5CYII=', 'base64');
  const unsafeSubject = '<img src=x onerror="window.mailInjected=true"> Invoice';
  const claimReason = '二维码领取链接已过期，请联系商家重新提供。';
  const linkFailure = {message_uid: 'synthetic-uid-4', subject: unsafeSubject, order_number: '<b>ORDER-4</b>',
    reason: '签名链接已过期 https://invoice.example/private.jpg?Signature=synthetic-secret'};
  const importFailure = {name: '<img src=x onerror="window.mailInjected=true">broken.pdf', reason: 'PDF结构损坏 <b>无法导入</b>'};
  const preparationFailure = {id: 'preparation-1', reason: '文字整理暂不可用 <script>window.mailInjected=true</script>'};
  page.on('pageerror', error => errors.push(error.message));
  await page.route(origin + '/**', async route => {
    const request = route.request(), url = new URL(request.url());
    const json = (value, status = 200) => route.fulfill({status, contentType: 'application/json', body: JSON.stringify(value)});
    if (request.method() === 'POST') {
      const body = request.postDataJSON(); writes.push({route: url.pathname, body});
      assert.equal(url.pathname, '/api/mail', 'collection must not create fees or confirm matches');
      assert.deepEqual(body, {project_id: mode === 'second-project' ? 'p2' : 'p1', material_only: true});
      if (mailHold) await mailHold;
      if (mode === 'unavailable') return json({error: 'synthetic mail unavailable'}, 503);
      if (mode === 'partial-failures') return json({message: '部分材料已保存，另有失败待处理。', downloaded: 2,
        link_downloaded: 1, image_downloaded: 1, link_failures: [linkFailure],
        intake: {prepared: 0, converted_to_pdf: 0, claim_pending: 0, import_failures: [importFailure], preparation_failures: [preparationFailure]}});
      if (mode === 'collected') {
        if (!board.vouchers.some(value => value.id === 'attachment-pdf')) {
          board.vouchers.push(card('attachment-pdf', 'invoice', {name: 'mail-invoice.pdf'}));
          for (let i = 1; i <= 3; i++) board.vouchers.push(card(`claimed-${i}`, 'invoice', {name: `claimed-${i}.pdf`, qr_status: 'claimed'}));
          board.other.push(card('pending-qr', 'other', {kind: 'qr_claim', qr_status: 'manual', issues: ['待领票'],
            claim_message: claimReason + ' https://invoice.example/qr?Signature=hidden-claim-secret'}));
          board.other.push(card('normal-material', 'other', {kind: 'other', claim_message: 'SHOULD-NOT-SHOW'}));
        }
        return json({message: '邮件补收完成，原件已保存。', downloaded: 6, link_downloaded: 5, image_downloaded: 4,
          link_failures: [linkFailure], intake: {prepared: 6, converted_to_pdf: 3, claim_pending: 1}});
      }
      return json({message: '邮件检查完成。', downloaded: 0, link_downloaded: 0, image_downloaded: 0,
        link_failures: [], intake: {prepared: 0, converted_to_pdf: 0, claim_pending: 0}});
    }
    if (url.pathname === '/api/state') return json(state);
    if (url.pathname === '/api/material-matching') {
      boardReads++;
      return json(url.searchParams.get('project') === 'p2' ? {...board, vouchers: [], payments: [], other: []} : board);
    }
    if (url.pathname === '/api/intake') return json({queue: []});
    if (url.pathname.startsWith('/intake-file/')) return route.fulfill({contentType: 'image/png', body: png});
    const name = url.pathname === '/' ? 'index.html' : url.pathname.slice(1);
    if (!/^[a-z-]+\.(html|js|css)$/.test(name)) return route.abort();
    return route.fulfill({contentType: name.endsWith('.js') ? 'text/javascript' : name.endsWith('.css') ? 'text/css' : 'text/html',
      body: await fs.readFile(path.join(assets, name))});
  });
  try {
    await page.goto(origin+'/?project=p1');
    await page.locator('#materialMatching').click();
    assert.equal(await page.locator('#matchingProject').count(),0);
    await page.locator('[data-voucher-key="q:existing-voucher"]').click();
    await page.locator('[data-payment-id="existing-payment"]').click();
    let release; mailHold = new Promise(resolve => {release = resolve;});
    await page.locator('#matchingMail').click();
    await page.waitForFunction(() => document.getElementById('matchingMail').disabled);
    await page.locator('#matchingMail').evaluate(node => {node.dispatchEvent(new MouseEvent('click')); node.dispatchEvent(new MouseEvent('click'));});
    assert.equal(writes.length, 1, 'busy clicks cannot send duplicate mail collection');
    release(); mailHold = null;
    await page.locator('#matchingMessage').filter({hasText: '二维码转为正式发票 PDF 3 份'}).waitFor();
    assert.match(await page.locator('#matchingMessage').innerText(), /邮件补收完成.*图片材料已保存 4 份.*待人工领票 1 份.*链接下载失败 1 项/);
    assert.match(await page.locator('#matchingMessage').innerText(), /再次点击“补收邮件”可重试/);
    assert.equal(await page.locator('#matchingMailFeedback').isVisible(), true);
    const failureText = await page.locator('#matchingMailFeedback').innerText();
    assert.ok(failureText.includes(unsafeSubject), 'mail subject is rendered as literal text');
    assert.ok(failureText.includes('<b>ORDER-4</b>'));
    assert.match(failureText, /synthetic-uid-4.*签名链接已过期/);
    assert.equal(await page.locator('#matchingMailFeedback img, #matchingMailFeedback b, #matchingMailFeedback a').count(), 0);
    assert.equal(await page.evaluate(() => window.mailInjected), undefined);
    assert.doesNotMatch(await page.locator('#matchingDeskDialog').innerText(), /synthetic-secret|hidden-claim-secret|invoice\.example/);
    assert.match(await page.locator('#matchingOther').innerText(), /二维码领取链接已过期，请联系商家重新提供/);
    assert.doesNotMatch(await page.locator('#matchingOther').innerText(), /SHOULD-NOT-SHOW/);
    assert.equal(await page.locator('#matchingOther [data-prepare-id="pending-qr"]').innerText(), '重试领票');
    assert.equal(await page.locator('[data-voucher-key="q:existing-voucher"]').getAttribute('aria-pressed'), 'true');
    assert.equal(await page.locator('[data-payment-id="existing-payment"]').getAttribute('aria-pressed'), 'true');
    assert.equal(state.items.length, 0, 'collection and proposed combinations never make a fee');
    assert.equal(await page.locator('#tabs [data-view="synthesis"]').count(), 0);
    assert.equal(await page.locator('#matchingDeskDialog #matchingSuggestions').count(), 1, 'mail collection keeps the proposed combinations inside the material desk');

    await page.setViewportSize({width: 390, height: 844});
    assert.equal(await page.locator('#matchingMailFeedback').evaluate(node => node.getBoundingClientRect().right <= window.innerWidth), true);
    assert.equal(await page.locator('#matchingDeskDialog').evaluate(node => node.scrollWidth <= node.clientWidth + 1), true);
    mode = 'empty';
    await page.locator('#matchingMail').click();
    await page.locator('#matchingMessage').filter({hasText: '本次没有新增材料'}).waitFor();
    assert.equal(await page.locator('#matchingMailFeedback').isVisible(), false, 'new round clears earlier failed mail list');
    assert.equal(await page.locator('#matchingMailFeedback li').count(), 0);
    assert.match(await page.locator('#matchingMessage').innerText(), /二维码转为正式发票 PDF 0 份.*链接下载失败 0 项/);

    mode = 'partial-failures';
    await page.locator('#matchingMail').click();
    await page.locator('#matchingMessage').filter({hasText: '失败共 3 项'}).waitFor();
    assert.equal(await page.locator('#matchingMailFeedback li').count(), 3);
    const partialText = await page.locator('#matchingMailFeedback').innerText();
    for (const expected of [unsafeSubject, importFailure.name, importFailure.reason, preparationFailure.id, preparationFailure.reason]) {
      assert.ok(partialText.includes(expected), `partial failure details must remain visible: ${expected}`);
    }
    assert.match(partialText, /链接下载失败.*再次补收邮件可重试/);
    assert.match(partialText, /导入失败.*请更换原文件后重新上传/);
    assert.match(partialText, /整理失败.*可在材料区重试识别/);
    assert.equal(await page.locator('#matchingMailFeedback img, #matchingMailFeedback b, #matchingMailFeedback script').count(), 0);
    assert.equal(await page.evaluate(() => window.mailInjected), undefined);
    assert.doesNotMatch(partialText, /synthetic-secret|invoice\.example/);
    assert.equal(await page.locator('[data-voucher-key="q:existing-voucher"]').getAttribute('aria-pressed'), 'true');

    mode = 'unavailable';
    await page.locator('#matchingMail').click();
    await page.locator('#matchingMessage').filter({hasText: 'synthetic mail unavailable'}).waitFor();
    const failedWriteCount = writes.length;
    assert.match(await page.locator('#matchingMessage').innerText(), /再次点击“补收邮件”重试.*原件保留/);
    assert.equal(await page.locator('#matchingMail').isEnabled(), true);
    await page.locator('#matchingRefresh').click();
    await page.locator('#matchingMessage').filter({hasText: '材料已刷新'}).waitFor();
    assert.equal(writes.length, failedWriteCount, '503 does not cause an automatic collection retry');
    assert.equal(await page.locator('[data-voucher-key="q:existing-voucher"]').getAttribute('aria-pressed'), 'true');
    mode = 'empty';
    await page.locator('#matchingMail').click();
    await page.locator('#matchingMessage').filter({hasText: '本次没有新增材料'}).waitFor();
    assert.equal(writes.length, failedWriteCount + 1, 'user can explicitly retry collection');

    mode = 'collected';
    await page.locator('#matchingMail').click();
    await page.locator('#matchingMailFeedback li').waitFor();
    await page.goto(origin+'/?project=p2'); await page.locator('#materialMatching').click();
    await page.locator('#matchingMessage').filter({hasText: '在左侧选择建议组合或任意材料'}).waitFor();
    assert.equal(await page.locator('#matchingMailFeedback').isVisible(), false, 'switching project clears old mail feedback');
    assert.equal(await page.locator('#matchingMailFeedback li').count(), 0);
    mode = 'second-project';
    await page.locator('#matchingMail').click();
    await page.locator('#matchingMessage').filter({hasText: '本次没有新增材料'}).waitFor();
    assert.deepEqual(writes.at(-1).body, {project_id: 'p2', material_only: true});
    await page.locator('#matchingClose').click();
    await page.locator('#materialMatching').click();
    assert.equal(await page.locator('#matchingMailFeedback li').count(), 0);
    assert.deepEqual(errors, []);
    assert.ok(boardReads >= 7);
    return {passed: true, mail_requests: writes.length, board_reads: boardReads, no_live_data: true,
      scope: 'PDF and QR image collection, conversion/pending counts, link/import/preparation failures with proper recovery, literal escaped details, private URL redaction, empty response, busy duplicates, 503/user retry, unchanged selection, project isolation, narrow screen'};
  } finally {await browser.close();}
}
module.exports = run;
if (require.main === module) run(require('playwright')).then(result => console.log(JSON.stringify(result))).catch(error => {console.error(error); process.exitCode = 1;});
