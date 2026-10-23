// Real wheel, keyboard and nested-dialog behavior against synthetic material data.
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');

async function run(pw) {
  const browser = await pw.chromium.launch({headless: true});
  const page = await browser.newPage({viewport: {width: 1450, height: 1000}});
  page.setDefaultTimeout(10000);
  const origin = 'http://127.0.0.1:18804', errors = [], writes = [];
  const evidence = process.env.INVOICE_BROWSER_EVIDENCE ? path.resolve(process.env.INVOICE_BROWSER_EVIDENCE) :
    path.resolve(__dirname, '../workspace/invoice-operations/matching-pane-scroll-20261013');
  const image = Buffer.from('<svg xmlns="http://www.w3.org/2000/svg" width="1000" height="700"><rect width="1000" height="700" fill="white"/><rect x="50" y="150" width="900" height="430" fill="none" stroke="#d55" stroke-width="3"/><text x="60" y="90" font-family="sans-serif" font-size="32" fill="#b33">Synthetic invoice / payment proof</text></svg>');
  const card = (id, role, extra = {}) => ({id, key: 'q:' + id, name: '合成原件 ' + id + '.png', role,
    amount_cents: 1000, project_id: 'p1', revision: 1, status: 'review', fields: {merchant: '合成商家'},
    report_choice: {project_type: 'other', project_name: '合成报销名称'}, issues: [],
    thumbnail: '/intake-file/' + id, url: '/intake-file/' + id, can_archive: true, ...extra});
  const longName = '合成很长但必须完整可读的材料名称'.repeat(20) + '.pdf';
  const board = {vouchers: Array.from({length: 27}, (_, index) => card('v' + index, 'invoice', index === 0 ? {name: longName} : {})),
    payments: Array.from({length: 15}, (_, index) => card('p' + index, 'payment')), other: [card('other', 'other')],
    archived: [card('archive', 'invoice', {status: 'archived', can_archive: false, can_restore: true, can_delete: true}),
      card('rail', 'payment', {status: 'unused', can_archive: false, can_restore: false, can_delete: true,
        unused_reason: '高铁报销只需12306发票，支付截图无需参与合成。'})],
    deleted: [card('deleted', 'receipt', {status: 'deleted', can_archive: false, can_restore_deleted: true})],
    matches: Array.from({length: 7}, (_, index) => ({id: 'group' + index, group_id: 'group' + index, title: '合成已匹配 ' + index,
      version: 'g1', voucher_count: 1, payment_count: 1, can_supplement: false, provisional: true})),
    report_types: [{code: 'other', label: '其他', editable_name: true, names: []}], supplement_candidates: [], suggestions: [], ai: {available: true}};
  const state = {app_mode: 'invoice', token: 'synthetic', projects: [{id: 'p1', name: '合成独立滚动项目'}], inbox: [],
    items: Array.from({length: 20}, (_, index) => ({id: 'expense' + index, project_id: 'p1', project: '合成独立滚动项目',
      title: '合成背景费用 ' + index, amount_cents: 1000, amount: '10.00', stage: 'draft', complete: true,
      verified: false, ready: false, review_requested: true, version: 'ev1', category: '其他', missing: [], attachments: [], events: [], expense_date: ''})),
    categories: ['其他'], roles: {invoice: '发票', payment: '付款截图'}, stages: {draft: '待核对'}, category_auto_ready: false};
  page.on('pageerror', error => errors.push(error.message));
  await page.route(origin + '/**', async route => {
    const req = route.request(), url = new URL(req.url());
    const json = body => route.fulfill({contentType: 'application/json', body: JSON.stringify(body)});
    if (req.method() === 'POST') {writes.push(url.pathname); throw Error('Scroll/focus checks must not write materials or expenses');}
    if (url.pathname === '/api/state') return json(state);
    if (url.pathname === '/api/material-matching') return json(board);
    if (url.pathname === '/api/intake') return json({queue: []});
    if (url.pathname.startsWith('/intake-file/')) return route.fulfill({contentType: 'image/svg+xml', body: image});
    const name = url.pathname === '/' ? 'index.html' : url.pathname.slice(1);
    if (!/^[a-z-]+\.(html|js|css)$/.test(name)) return route.abort();
    return route.fulfill({contentType: name.endsWith('.js') ? 'text/javascript' : name.endsWith('.css') ? 'text/css' : 'text/html',
      body: await fs.readFile(path.resolve(__dirname, '../src/invoice_print_layout/web', name))});
  });
  const positions = () => page.evaluate(() => ({outer: scrollY,
    modal: document.getElementById('matchingDeskDialog').scrollTop,
    left: document.querySelector('.matching-pools').scrollTop,
    right: document.querySelector('.matching-preview').scrollTop,
    toolbar: document.querySelector('.matching-toolbar').getBoundingClientRect().top,
    header: document.querySelector('.matching-header').getBoundingClientRect().top}));
  const wheel = async (selector, delta) => {
    const area = await page.locator(selector).boundingBox();
    await page.mouse.move(area.x + area.width / 2, area.y + area.height / 2);
    await page.mouse.wheel(0, delta);
    // Wheel scrolling updates on animation frames, so read after the next paint.
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  };
  const draft = () => page.evaluate(() => ({type: document.getElementById('matchingProjectType').value,
    amount: document.getElementById('matchingAmount').value, name: document.getElementById('matchingProjectName').value}));
  const settlePaneScroll = selector => page.locator(selector).evaluate(node => new Promise((resolve, reject) => {
    let previous = node.scrollTop, stable = 0, frames = 0;
    const next = () => {
      const current = node.scrollTop;
      stable = current === previous ? stable + 1 : 0; previous = current;
      if (stable >= 4) resolve(); else if (++frames >= 120) reject(Error('Keyboard pane scrolling did not settle')); else requestAnimationFrame(next);
    };
    requestAnimationFrame(next);
  }));
  try {
    await fs.mkdir(evidence, {recursive: true});
    await page.goto(origin + '/?project=p1');
    await page.locator('#materialMatching').click();
    await page.locator('[data-voucher-key="q:v0"]').click();
    for (const id of ['p0', 'p1', 'p2', 'p3']) await page.locator(`[data-payment-id="${id}"]`).click();
    await page.locator('#matchingAmount').fill('40.25'); await page.locator('#matchingProjectName').fill('手写字段'.repeat(30));
    const beforeDraft = await draft();
    assert.ok(await page.locator('.matching-voucher-proof img').first().evaluate(node => node.complete && node.naturalWidth === 1000 && node.getBoundingClientRect().width > 400), 'the selected original retains its large preview');
    await page.locator('.matching-pools').evaluate(node => {node.scrollTop = 0;});
    await page.locator('.matching-preview').evaluate(node => {node.scrollTop = 0;});
    const initial = await positions();
    assert.equal(await page.locator('#matchingDeskDialog').evaluate(node => node.scrollHeight <= node.clientHeight + 1), true, 'desktop modal is not a shared scroll container');
    assert.equal(await page.locator('.matching-pools').evaluate(node => node.scrollHeight > node.clientHeight), true);
    assert.equal(await page.locator('.matching-preview').evaluate(node => node.scrollHeight > node.clientHeight), true);
    await wheel('.matching-pools', 630);
    await page.waitForFunction(() => document.querySelector('.matching-pools').scrollTop > 0);
    const afterLeft = await positions();
    assert.ok(afterLeft.left > initial.left);
    assert.equal(afterLeft.right, initial.right); assert.equal(afterLeft.modal, initial.modal); assert.equal(afterLeft.outer, initial.outer);
    assert.equal(afterLeft.toolbar, initial.toolbar); assert.equal(afterLeft.header, initial.header);
    await wheel('.matching-preview', 560);
    await page.waitForFunction(() => document.querySelector('.matching-preview').scrollTop > 0);
    const afterRight = await positions();
    assert.ok(afterRight.right > afterLeft.right); assert.equal(afterRight.left, afterLeft.left);
    assert.equal(afterRight.outer, initial.outer); assert.equal(afterRight.modal, initial.modal);
    assert.deepEqual(await draft(), beforeDraft, 'pane scrolling preserves unsaved fields');
    await page.locator('.matching-pools').evaluate(node => {node.scrollTop = node.scrollHeight;});
    const leftEnd = await positions();
    await wheel('.matching-pools', 500);
    const leftBoundary = await positions();
    assert.equal(leftBoundary.right, leftEnd.right); assert.equal(leftBoundary.outer, initial.outer); assert.equal(leftBoundary.modal, initial.modal);
    await page.locator('#matchingUnusedSection > summary').click();
    await page.locator('#matchingDeletedSection > summary').click();
    await page.locator('[data-material-action="restore-deleted"]').scrollIntoViewIfNeeded();
    assert.equal(await page.locator('[data-material-action="restore-deleted"]').isVisible(), true);
    await page.locator('.matching-groups > summary').click();
    await page.locator('[data-undo-group="group6"]').scrollIntoViewIfNeeded();
    assert.equal(await page.locator('[data-undo-group="group6"]').isVisible(), true, 'last matched group remains reachable');
    await page.locator('.matching-preview').evaluate(node => {node.scrollTop = node.scrollHeight;});
    await page.locator('#matchingSave').scrollIntoViewIfNeeded();
    assert.equal(await page.locator('#matchingSave').isVisible(), true);
    const rightEnd = await positions();
    await wheel('.matching-preview', 500);
    const rightBoundary = await positions();
    assert.equal(rightBoundary.left, rightEnd.left); assert.equal(rightBoundary.outer, initial.outer); assert.equal(rightBoundary.modal, initial.modal);
    await page.locator('.matching-pools').focus();
    await page.keyboard.press('Home');
    await page.waitForFunction(() => document.querySelector('.matching-pools').scrollTop === 0);
    await page.keyboard.press('PageDown');
    await page.waitForFunction(() => document.querySelector('.matching-pools').scrollTop > 0);
    await settlePaneScroll('.matching-pools');
    assert.equal(await page.evaluate(() => document.activeElement.classList.contains('matching-pools')), true);
    const beforeKeyboardRight = await positions();
    await page.locator('.matching-preview').focus(); await page.keyboard.press('Home');
    await page.waitForFunction(() => document.querySelector('.matching-preview').scrollTop === 0);
    await page.keyboard.press('PageDown');
    await page.waitForFunction(() => document.querySelector('.matching-preview').scrollTop > 0);
    await settlePaneScroll('.matching-preview');
    const afterKeyboardRight = await positions(); assert.equal(afterKeyboardRight.left, beforeKeyboardRight.left);
    await page.locator('.matching-pools').evaluate(node => {node.scrollTop = 0;});
    await page.locator('[data-classify-id="v0"]').click();
    const editor = page.locator('.matching-classify[open]'); await editor.waitFor();
    assert.equal(await editor.evaluate(node => node.scrollWidth <= node.clientWidth + 1), true, 'long names do not widen the nested editor');
    await editor.getByRole('button', {name: '保存材料分类'}).scrollIntoViewIfNeeded();
    assert.equal(await editor.getByRole('button', {name: '保存材料分类'}).isVisible(), true);
    await page.keyboard.press('Escape'); await editor.waitFor({state: 'detached'});
    assert.equal(await page.locator('#matchingDeskDialog').getAttribute('open'), '');
    assert.deepEqual(await draft(), beforeDraft);
    assert.equal(await page.locator('.matching-layout').evaluate(node => node.scrollWidth <= node.clientWidth + 1), true);
    await page.screenshot({path: path.join(evidence, 'synthetic-independent-desktop.png')});
    await page.setViewportSize({width: 390, height: 844});
    await page.locator('#matchingSave').scrollIntoViewIfNeeded();
    assert.equal(await page.locator('#matchingSave').isVisible(), true, 'mobile final creation control is reachable');
    assert.equal(await page.locator('#matchingDeskDialog').evaluate(node => node.scrollTop > 0 && node.scrollHeight > node.clientHeight), true, 'mobile uses one natural vertical scroll');
    assert.equal(await page.locator('.matching-pools').evaluate(node => getComputedStyle(node).overflowY), 'visible');
    assert.equal(await page.locator('.matching-preview').evaluate(node => getComputedStyle(node).overflowY), 'visible');
    assert.equal(await page.locator('#matchingDeskDialog').evaluate(node => node.scrollWidth <= node.clientWidth + 1), true);
    assert.deepEqual(await draft(), beforeDraft);
    await page.locator('[data-classify-id="v0"]').scrollIntoViewIfNeeded();
    await page.locator('[data-classify-id="v0"]').click();
    await editor.waitFor();
    await editor.getByRole('button', {name: '保存材料分类'}).scrollIntoViewIfNeeded();
    assert.equal(await editor.evaluate(node => node.scrollWidth <= node.clientWidth + 1), true);
    await page.keyboard.press('Tab');
    assert.equal(await page.evaluate(() => document.activeElement.closest('.matching-classify') !== null), true, 'keyboard focus remains in the top nested dialog');
    await page.keyboard.press('Escape'); await editor.waitFor({state: 'detached'});
    await page.locator('#matchingSave').scrollIntoViewIfNeeded();
    await page.screenshot({path: path.join(evidence, 'synthetic-natural-mobile.png')});
    await page.locator('#matchingClose').click();
    assert.equal(await page.evaluate(() => getComputedStyle(document.documentElement).overflowY), 'visible', 'closing restores page scrolling');
    assert.deepEqual(writes, []); assert.deepEqual(errors, []);
    const result = {passed: true, desktopPanesIndependent: true, outerPageAndToolbarFixed: true, bothPaneBoundariesContained: true,
      archivedDeletedAndMatchedReachable: true, createButtonReachable: true, keyboardPaneScrollAndNestedFocus: true,
      nestedEditorReachable: true, mobileNaturalScroll: true, longFieldsNoHorizontalOverflow: true, unsavedFieldsPreserved: true,
      falseWrites: 0, pageErrors: errors};
    await fs.writeFile(path.join(evidence, 'scroll-browser.json'), JSON.stringify(result, null, 2)); return result;
  } finally {await browser.close();}
}
module.exports = run;
if (require.main === module) run(require('playwright')).then(console.log).catch(error => {console.error(error); process.exitCode = 1;});
