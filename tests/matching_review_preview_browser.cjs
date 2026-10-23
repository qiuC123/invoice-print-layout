// The matching desk reviews original images, independently of final print layout.
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');

async function run(pw) {
  const browser = await pw.chromium.launch({headless: true});
  const page = await browser.newPage({viewport: {width: 1500, height: 1000}});
  page.setDefaultTimeout(10000);
  const origin = 'http://127.0.0.1:18884', writes = [], errors = [], requestedImages = new Set();
  const evidence = process.env.INVOICE_BROWSER_EVIDENCE ? path.resolve(process.env.INVOICE_BROWSER_EVIDENCE) :
    path.resolve(__dirname, '../workspace/invoice-operations/large-matching-preview-20261016');
  // Deliberately taller than a screen and wider than every preview pane. The
  // bottom marker proves that the full original remains reachable by scrolling.
  const original = Buffer.from('<svg xmlns="http://www.w3.org/2000/svg" width="1080" height="2354" viewBox="0 0 1080 2354">' +
    '<rect width="1080" height="2354" fill="white"/><rect x="4" y="4" width="1072" height="2346" fill="none" stroke="#1671a4" stroke-width="8"/>' +
    '<text x="50" y="130" font-family="sans-serif" font-size="45" fill="#162c3c">SYNTHETIC ORIGINAL / TOP</text>' +
    '<path d="M50 250H1030M50 750H1030M50 1250H1030M50 1750H1030M50 2150H1030" stroke="#aaa" stroke-width="3"/>' +
    '<text x="50" y="2270" font-family="sans-serif" font-size="42" fill="#a31a32">FULL CONTENT / BOTTOM</text></svg>');
  const owner = '合成核对人员', shop = '合成核对便利店';
  const material = (id, role, fields = {}, extra = {}) => ({id, key: 'q:' + id, project_id: 'p1', role,
    name: 'synthetic_original_' + id + '_0123456789abcdef.png', amount_cents: 1700,
    fields, status: 'review', revision: 1, reimbursement_person_id: 'person1', reimbursement_person_label: owner,
    thumbnail: '/intake-file/' + id + '/thumbnail.svg', url: '/intake-file/' + id + '/original.svg',
    issues: [], report_choice: {project_type: 'other', project_name: '合成核对用途'}, ...extra});
  const first = material('v0', 'invoice', {shop_name: shop, merchant: shop});
  const second = material('v1', 'receipt', {}, {source_role: 'order'});
  const vouchers = [first, second,
    material('rail', 'invoice', {merchant: '合成铁路'}, {payment_not_required: true}),
    material('missing', 'receipt', {merchant: '合成缺项店'}, {issues: ['缺少付款凭证，请补充后核对。']}),
    ...Array.from({length: 8}, (_, index) => material('extra' + index, 'invoice', {merchant: '合成其他商家 ' + index})),
    material('existing', 'invoice', {merchant: '合成已有费用'}, {key: 'e:existing', item_id: 'existing', version: 'ev1', can_edit: true})];
  const payments = [material('p0', 'payment', {shop_name: shop, shopping_platform: '淘宝闪购', payment_channel: '支付宝', goods: '矿泉水与工作餐'}),
    material('p1', 'payment', {shopping_platform: '淘宝闪购', payment_channel: '支付宝'})];
  const supports = [material('s0', 'trip_sheet', {}), material('s1', 'other', {goods: '合成运单附件'})];
  const board = {vouchers, payments, other: supports, archived: [], deleted: [], matches: [], suggestions: [],
    supplement_candidates: [], report_types: [{code: 'other', label: '其他', editable_name: true, names: []}]};
  const state = {app_mode: 'invoice', token: 'synthetic-review-token', projects: [{id: 'p1', name: '合成原件核对项目'}],
    items: [], inbox: [], categories: ['其他'], roles: {invoice: '发票', receipt: '收据', payment: '付款截图'},
    stages: {draft: '待核对'}, category_auto_ready: false};
  page.on('pageerror', error => errors.push(error.message));
  await page.route(origin + '/**', async route => {
    const request = route.request(), url = new URL(request.url());
    if (!['GET', 'HEAD'].includes(request.method())) {
      writes.push({method: request.method(), path: url.pathname});
      return route.fulfill({status: 403, contentType: 'application/json', body: '{"error":"read-only preview fixture"}'});
    }
    const json = value => route.fulfill({contentType: 'application/json', body: JSON.stringify(value)});
    if (url.pathname === '/api/state') return json(state);
    if (url.pathname === '/api/material-matching') return json(board);
    if (url.pathname === '/api/intake') return json({queue: []});
    if (url.pathname === '/api/reimbursement-people') return json({project_id: 'p1',
      people: [{id: 'person1', project_id: 'p1', label: owner, state: 'active'}], summary: {rows: []}});
    if (url.pathname.startsWith('/intake-file/')) {
      requestedImages.add(url.pathname);
      return route.fulfill({contentType: 'image/svg+xml', body: original});
    }
    const file = url.pathname === '/' ? 'index.html' : url.pathname.slice(1);
    if (!/^[a-z-]+\.(html|js|css)$/.test(file)) return route.abort();
    return route.fulfill({contentType: file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html',
      body: await fs.readFile(path.resolve(__dirname, '../src/invoice_print_layout/web', file))});
  });
  const card = id => page.locator(`[data-voucher-key="q:${id}"], [data-payment-id="${id}"], [data-support-id="${id}"]`);
  const draft = () => page.evaluate(() => ({type: document.getElementById('matchingProjectType').value,
    amount: document.getElementById('matchingAmount').value, name: document.getElementById('matchingProjectName').value,
    person: document.getElementById('matchingReimbursementPerson').value}));
  const positions = () => page.evaluate(() => ({outer: scrollY, modal: document.getElementById('matchingDeskDialog').scrollTop,
    left: document.querySelector('.matching-pools').scrollTop, right: document.querySelector('.matching-preview').scrollTop,
    toolbar: document.querySelector('.matching-toolbar').getBoundingClientRect().top}));
  const wheel = async selector => {
    const area = await page.locator(selector).boundingBox();
    await page.mouse.move(area.x + area.width / 2, area.y + area.height / 2);
    await page.mouse.wheel(0, 550);
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  };
  const waitImage = async image => {
    await image.scrollIntoViewIfNeeded();
    await image.evaluate(node => node.complete ? null : new Promise((resolve, reject) => {
      node.addEventListener('load', resolve, {once: true}); node.addEventListener('error', () => reject(Error('Synthetic original failed to load')), {once: true});
    }));
  };
  try {
    await fs.mkdir(evidence, {recursive: true});
    await page.goto(origin + '/?project=p1');
    await page.waitForFunction(() => typeof window.openMaterialMatching === 'function' && !!state.token);
    await page.locator('#materialMatching').click();
    await card('v0').waitFor();
    const pureTypes = new Set(['发票', '收据', '收据 · 平台购买记录', '付款截图', '普通材料 · 可创建或补充']);
    const typeLabels = await page.locator('.matching-card > small').evaluateAll(nodes => nodes.map(node => node.textContent.trim()));
    assert.deepEqual(typeLabels.filter(value => pureTypes.has(value)), [], 'section headings replace repeated card type labels');
    assert.equal(await card('v1').locator('strong').innerText(), '购买记录', 'semantic title fallback still exists');
    assert.equal(await card('p1').locator('strong').innerText(), '付款截图');
    assert.equal(await card('s0').locator('strong').innerText(), '行程单');
    assert.match(await page.locator('[data-voucher-key="e:existing"]').innerText(), /已有费用 · 补入／合并/);
    assert.match(await card('rail').innerText(), /高铁发票无需支付截图/);
    assert.match(await card('missing').innerText(), /缺少付款凭证，请补充后核对/);
    assert.match(await card('p0').innerText(), /用途：矿泉水与工作餐/);
    await page.evaluate(() => {window.initialMaterialButtons = [...document.querySelectorAll('.matching-card')];});
    for (const id of ['v0', 'v1', 'p0', 'p1', 's0', 's1']) {
      assert.match(await card(id).innerText(), /¥ 17\.00/);
      assert.ok((await card(id).innerText()).includes('费用归属人：' + owner));
      await card(id).click();
    }
    assert.equal(await page.evaluate(() => window.initialMaterialButtons.every(node => node.isConnected &&
      [...document.querySelectorAll('.matching-card')].includes(node))), true, 'selection preserves existing cards and thumbnail DOM');
    assert.equal(await page.locator('.matching-card[aria-pressed=true]').count(), 6);
    await page.locator('#matchingAmount').fill('123.45');
    await page.locator('#matchingProjectName').fill('尚未保存的合成核对用途');
    const savedDraft = await draft();
    const expected = {'.matching-voucher-proofs': [first, second], '.matching-payment-proofs': payments,
      '.matching-support-proofs': supports};
    const geometries = [];
    for (const theme of ['light', 'dark']) for (const width of [1500, 390]) {
      await page.locator('#themeSelect').selectOption(theme, {force: true});
      await page.setViewportSize({width, height: 1000});
      const imageMetrics = [];
      for (const [selector, values] of Object.entries(expected)) {
        const grid = page.locator('#matchingPreviewContent ' + selector), figures = grid.locator('figure');
        assert.equal(await figures.count(), 2);
        for (const [index, value] of values.entries()) {
          const figure = figures.nth(index), image = figure.locator('img');
          await waitImage(image);
          assert.equal(await figure.locator('a').getAttribute('href'), value.url, 'original URL is unchanged');
          const metrics = await image.evaluate(node => {
            const rect = node.getBoundingClientRect(), figure = node.closest('figure').getBoundingClientRect(),
              anchor = node.closest('a').getBoundingClientRect(), style = getComputedStyle(node);
            return {naturalWidth: node.naturalWidth, naturalHeight: node.naturalHeight,
              width: rect.width, height: rect.height, figureWidth: figure.width, figureHeight: figure.height,
              anchorHeight: anchor.height, maxHeight: style.maxHeight, clipPath: style.clipPath};
          });
          assert.equal(metrics.naturalWidth, 1080); assert.equal(metrics.naturalHeight, 2354);
          assert.ok(metrics.width >= metrics.figureWidth * .95, `${theme}/${width}/${value.id}: original fills the preview column`);
          assert.ok(Math.abs(metrics.height - metrics.width * 2354 / 1080) <= 1.5,
            `${theme}/${width}/${value.id}: complete original aspect ratio is retained`);
          assert.equal(metrics.maxHeight, 'none', 'long originals have no arbitrary preview height cap');
          assert.equal(metrics.clipPath, 'none');
          assert.ok(metrics.anchorHeight >= metrics.height && metrics.figureHeight >= metrics.height, 'no inner wrapper crops the original');
          if (width === 1500) assert.ok(metrics.height > 900, 'desktop long original remains large and scrollable');
          imageMetrics.push({id: value.id, ...metrics});
        }
        const layout = await figures.evaluateAll(nodes => nodes.map(node => {
          const rect = node.getBoundingClientRect(); return {left: rect.left, top: rect.top, width: rect.width, bottom: rect.bottom};
        }));
        assert.ok(Math.abs(layout[0].left - layout[1].left) <= 1 && Math.abs(layout[0].width - layout[1].width) <= 1,
          selector + ' originals occupy a single full-width column');
        assert.ok(layout[1].top >= layout[0].bottom, selector + ' originals stack without overlap');
      }
      if (width === 1500) {
        await page.locator('.matching-pools,.matching-preview').evaluateAll(nodes => nodes.forEach(node => {node.scrollTop = 0;}));
        const before = await positions();
        const actionBounds = await page.locator('#matchingSave').boundingBox(), paneBounds = await page.locator('.matching-preview').boundingBox();
        assert.ok(actionBounds.y >= paneBounds.y && actionBounds.y + actionBounds.height <= paneBounds.y + paneBounds.height + 1,
          'the confirmation action stays reachable while reviewing long originals');
        await page.locator('[data-matching-jump="matchingPaymentSection"]').click();
        const jump = await positions();
        assert.ok(jump.right > before.right, 'payment navigation skips long voucher images');
        assert.equal(jump.left, before.left); assert.equal(jump.modal, before.modal); assert.equal(jump.outer, before.outer);
        const heading = await page.locator('#matchingPaymentSection').boundingBox();
        assert.ok(heading.y > paneBounds.y && heading.y < paneBounds.y + 130, 'payment heading is visible below navigation');
        await page.locator('[data-matching-jump="matchingReviewInfo"]').click();
        assert.deepEqual(await draft(), savedDraft, 'navigation preserves the draft');
        assert.equal(await page.locator('#matchingDeskDialog').evaluate(node => node.scrollHeight <= node.clientHeight + 1), true);
        await wheel('.matching-pools');
        await page.waitForFunction(() => document.querySelector('.matching-pools').scrollTop > 0);
        const afterLeft = await positions();
        assert.ok(afterLeft.left > before.left); assert.equal(afterLeft.right, before.right);
        assert.equal(afterLeft.modal, before.modal); assert.equal(afterLeft.outer, before.outer); assert.equal(afterLeft.toolbar, before.toolbar);
        await wheel('.matching-preview');
        await page.waitForFunction(() => document.querySelector('.matching-preview').scrollTop > 0);
        const afterRight = await positions();
        assert.ok(afterRight.right > afterLeft.right); assert.equal(afterRight.left, afterLeft.left);
        assert.equal(afterRight.modal, before.modal); assert.equal(afterRight.outer, before.outer); assert.equal(afterRight.toolbar, before.toolbar);
      } else {
        assert.equal(await page.locator('.matching-pools').evaluate(node => getComputedStyle(node).overflowY), 'visible');
        assert.equal(await page.locator('.matching-preview').evaluate(node => getComputedStyle(node).overflowY), 'visible');
        await page.locator('.matching-support-proofs figure').last().locator('figcaption').scrollIntoViewIfNeeded();
        assert.equal(await page.locator('#matchingDeskDialog').evaluate(node => node.scrollTop > 0), true,
          'mobile reaches the original bottom through natural modal scrolling');
      }
      assert.deepEqual(await draft(), savedDraft, 'reviewing originals preserves unsaved form fields');
      const bounds = await page.locator('#matchingDeskDialog').evaluate(node => ({scroll: node.scrollWidth, client: node.clientWidth,
        page: document.documentElement.scrollWidth, width: innerWidth}));
      assert.ok(bounds.scroll <= bounds.client + 1 && bounds.page <= bounds.width + 1, `${theme}/${width}: content stays within the viewport`);
      assert.deepEqual(writes, []); assert.deepEqual(errors, []);
      geometries.push({theme, width, ...bounds, images: imageMetrics});
      await page.locator('.matching-voucher-proofs figure').first().locator('figcaption').scrollIntoViewIfNeeded();
      await page.screenshot({path: path.join(evidence, `synthetic-large-review-${theme}-${width}.png`)});
    }
    const result = {passed: true, fixture: {width: 1080, height: 2354, offlineSvg: true},
      selectedOriginals: 6, fullWidthSingleColumn: true, originalAspectRatioAndContentPreserved: true,
      repeatedTypeSmallLabelsRemoved: true, titleTypeFallbacksRetained: true, existingExpenseAndRailNotesRetained: true,
      genuineWarningsAndDistinctPurposeRetained: true, amountAndOwnerRetained: true, originalUrlsUnchanged: true,
      independentDesktopPaneScroll: true, naturalMobileScroll: true, unsavedFieldsPreserved: true,
      materialNodesRetainedOnSelection: true, directProofNavigation: true, confirmationAlwaysReachable: true,
      imagesServed: requestedImages.size, geometries, businessWrites: writes.length, pageErrors: errors};
    await fs.writeFile(path.join(evidence, 'matching-review-preview-browser.json'), JSON.stringify(result, null, 2));
    return result;
  } finally {await browser.close();}
}
module.exports = run;
if (require.main === module) run(require('playwright')).then(result => console.log(JSON.stringify({
  passed: result.passed, geometries: result.geometries.map(({theme, width}) => ({theme, width})),
  selectedOriginals: result.selectedOriginals, businessWrites: result.businessWrites, pageErrors: result.pageErrors,
}))).catch(error => {console.error(error); process.exitCode = 1;});
