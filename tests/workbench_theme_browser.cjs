// Real invoice UI with synthetic receipts, including storage failures and embedded isolation.
const assert = require('node:assert/strict'), fs = require('node:fs/promises'), path = require('node:path');
async function run(pw) {
  const browser = await pw.chromium.launch({headless: true});
  const origin = 'http://127.0.0.1:18857', key = 'invoice-workbench-theme', errors = [], writes = [], evidence = [];
  const source = path.resolve(__dirname, '../src/invoice_print_layout/web');
  const item = {id: 'f1', title: '合成费用', project_id: 'p1', project: '外观测试项目', category: '其他', amount_cents: 1000,
    amount: '10.00', stage: 'draft', verified: false, ready: false, complete: true, version: 'fv1', expense_date: '', missing: [],
    attachments: [{id: 'a1', name: 'synthetic.svg', role: 'invoice', available: true}], events: []};
  const state = {app_mode: 'invoice', token: 'synthetic', projects: [{id: 'p1', name: '外观测试项目'}], items: [item], inbox: [],
    categories: ['其他'], roles: {invoice: '发票', payment: '付款截图'}, stages: {draft: '待核对'}, category_auto_ready: false, report_template_ready: true};
  const card = {id: 'v1', key: 'q:v1', name: '合成原件.svg', role: 'invoice', amount_cents: 1000, project_id: 'p1', revision: 1,
    status: 'review', fields: {merchant: '合成商家'}, report_choice: {project_type: 'other', project_name: '合成报销名称'},
    issues: ['合成待核对信息'], thumbnail: '/intake-file/v1', url: '/intake-file/v1', can_archive: true};
  const board = {vouchers: [card], payments: [], other: [], matches: [], suggestions: [], archived: [], deleted: [],
    report_types: [{code: 'other', label: '其他', editable_name: true, names: []}], supplement_candidates: []};
  const svg = '<svg xmlns="http://www.w3.org/2000/svg" width="1000" height="600"><rect width="1000" height="600" fill="white"/><text x="30" y="60" fill="black">SYNTHETIC ORIGINAL</text></svg>';
  async function context(init, initialStateGate) {
    const result = await browser.newContext({viewport: {width: 1400, height: 1000}, colorScheme: 'dark'});
    if (init) await result.addInitScript(`if (window === window.top && location.origin === '${origin}') { ${init} }`);
    await result.route(origin + '/**', async route => {
      const req = route.request(), url = new URL(req.url()), json = body => route.fulfill({contentType: 'application/json', body: JSON.stringify(body)});
      if (url.pathname === '/api/state') {if (initialStateGate) await initialStateGate; return json(state);}
      if (url.pathname === '/api/reimbursement-people') return json({project_id: 'p1', people: [{id: 'person1', label: '合成姓名', project_id: 'p1', state: 'active', version: 'pv1'}], summary: {rows: [{person_id: 'person1', label: '合成姓名', expense_count: 1, total_cents: 1000}], total_cents: 1000, expense_count: 1}});
      if (url.pathname === '/api/material-matching') return json(board);
      if (url.pathname === '/api/report-snapshot') return json({rows: [{row: 34, name: '合成费用', items: [{id: 'f1'}]}]});
      if (req.method() === 'POST') {
        if (url.pathname === '/api/report-preview') return json({combined_pdf: 'combined.pdf', combined_preview_pages: ['table.svg', 'synthetic.svg'], combined_page_sizes: [{width: 842, height: 595}, {width: 595, height: 842}]});
        writes.push(url.pathname); assert.equal(url.pathname, '/api/expense-print-preview', 'Theme choices must never mutate business records');
        return json({id: 'f1', version: 'fv1', complete: true, missing: [], page_count: 1, preview_pages: ['synthetic.svg'], pdf: 'synthetic.pdf'});
      }
      if (url.pathname.startsWith('/export/') || url.pathname.startsWith('/intake-file/')) return route.fulfill({contentType: 'image/svg+xml', body: svg});
      if (url.pathname.startsWith('/api/')) return json({queue: []});
      if (url.pathname === '/host') return route.fulfill({contentType: 'text/html', body: '<!doctype html><html><body style="background:rgb(229,234,241);color:rgb(25,31,39)"><iframe title="embedded" src="/?project=p1&amp;embedded=1" style="width:1250px;height:900px"></iframe></body></html>'});
      if (url.pathname === '/logistics') return route.fulfill({contentType: 'text/html', body: '<!doctype html><html class="logistics"><head><script src="/workbench-theme.js"></script><link rel="stylesheet" href="/style.css"><link rel="stylesheet" href="/workbench-theme.css"></head><body><main><input value="后勤独立页面"><button>后勤按钮</button></main></body></html>'});
      const name = url.pathname === '/' ? 'index.html' : url.pathname.slice(1);
      if (!/^[a-z-]+\.(html|js|css)$/.test(name)) return route.abort();
      return route.fulfill({contentType: name.endsWith('.js') ? 'text/javascript' : name.endsWith('.css') ? 'text/css' : 'text/html', body: await fs.readFile(path.join(source, name))});
    });
    result.on('page', page => {page.setDefaultTimeout(10000); page.on('pageerror', error => errors.push(error.message));});
    return result;
  }
  const expectTheme = async (page, theme) => {
    assert.equal(await page.locator('html').getAttribute('data-theme'), theme);
    assert.equal(await page.locator('#themeSelect').inputValue(), theme);
    assert.equal(await page.locator('html').evaluate(root => getComputedStyle(root).colorScheme), theme);
  };
  const colors = page => page.evaluate(() => {
    const parse = value => [...value.matchAll(/[\d.]+/g)].map(match => Number(match[0]));
    const luminance = rgb => rgb.slice(0, 3).map(c => c / 255).map(c => c <= .04045 ? c / 12.92 : ((c + .055) / 1.055) ** 2.4).reduce((sum, c, index) => sum + c * [.2126, .7152, .0722][index], 0);
    const background = element => {
      for (let current = element; current; current = current.parentElement) {
        const rgb = parse(getComputedStyle(current).backgroundColor);
        if (rgb.length === 3 || rgb[3] === 1) return rgb;
      }
      return [255, 255, 255];
    };
    const selectors = ['body', '#search', '#categoryFilter', '.metric', '.metric em', '.row .title', '.row .sub', '.row .badge',
      '#themeAudit .muted', '#themeAudit .rule.warn', '#themeAudit .badge.good', '#themeAudit .dialog-message.is-error', '#themeAudit .primary',
      '.matching-preview label', '.matching-card strong', '.matching-card small', '.matching-card .matching-card-issues', '#matchingUpload'];
    return selectors.map(selector => {
      const node = document.querySelector(selector); if (!node) return {selector, absent: true};
      const fg = parse(getComputedStyle(node).color), bg = background(node), lo = Math.min(luminance(fg), luminance(bg)), hi = Math.max(luminance(fg), luminance(bg));
      return {selector, color: fg.slice(0, 3), background: bg.slice(0, 3), contrast: (hi + .05) / (lo + .05)};
    });
  });
  const checkContrast = (results, mode) => {
    for (const result of results) assert.ok(!result.absent && result.contrast >= 4.5, `${mode} ${result.selector} text contrast: ${JSON.stringify(result)}`);
  };
  const metricEdges = page => page.locator('.metric').evaluateAll(cards => cards.map(card => {
    const style = getComputedStyle(card), cardBox = card.getBoundingClientRect(), labelBox = card.querySelector('small').getBoundingClientRect();
    return {topWidth: style.borderTopWidth, topColor: style.borderTopColor, sideColor: style.borderRightColor,
      labelOffset: labelBox.top - cardBox.top};
  }));
  const checkMetricEdges = (cards, mode) => {
    assert.equal(cards.length, 2, 'the main page displays review and submission totals');
    for (const card of cards) {
      assert.equal(card.topWidth, '1px', `${mode} all stage cards use the same top edge`);
      assert.equal(card.topColor, card.sideColor, `${mode} ready card has no green top accent`);
      assert.ok(Math.abs(card.labelOffset - cards[0].labelOffset) < 1, `${mode} stage card content is vertically aligned`);
    }
  };
  const metricInteractions = async (page, mode) => {
    const results = [];
    const check = async (card, interaction) => {
      const edges = await card.evaluate(node => {
        const style = getComputedStyle(node);
        return {colors: ['Top', 'Right', 'Bottom', 'Left'].map(side => style['border' + side + 'Color']),
          widths: ['Top', 'Right', 'Bottom', 'Left'].map(side => style['border' + side + 'Width']),
          accent: getComputedStyle(document.documentElement).getPropertyValue('--wb-accent').trim(),
          focusVisible: node.matches(':focus-visible')};
      });
      assert.ok(edges.colors.every(color => color === edges.colors[0]), `${mode} ${interaction} card border must wrap all four edges: ${JSON.stringify(edges)}`);
      assert.deepEqual(edges.widths, ['1px', '1px', '1px', '1px']);
      if (interaction === 'keyboard focus') assert.equal(edges.focusVisible, true);
      results.push({interaction, ...edges});
    };
    for (const card of await page.locator('.metric').all()) {
      await card.hover(); await check(card, 'hover');
      await page.locator('.heading h1').hover(); await page.keyboard.press('Tab'); await card.focus(); await check(card, 'keyboard focus');
    }
    await page.locator('#search').focus(); await page.locator('.heading h1').hover();
    return results;
  };
  const dropdowns = [];
  const geometry = [];
  const typography = [];
  const checkTypography = async (page, specs, mode) => {
    for (const [selector, size, weight, lineHeight] of specs) {
      const value = await page.locator(selector).first().evaluate(node => {
        const style = getComputedStyle(node);
        return {size: parseFloat(style.fontSize), weight: Number(style.fontWeight), lineHeight: parseFloat(style.lineHeight)};
      });
      assert.deepEqual(value, {size, weight, lineHeight}, `${mode} ${selector} follows the shared text hierarchy`);
      typography.push({mode, selector, ...value});
    }
  };
  const mainText = [
    ['.heading h1', 24, 600, 32], ['.heading p', 14, 400, 22], ['.metric b', 24, 600, 32],
    ['.metric small', 12, 400, 20], ['.metric em', 12, 400, 20], ['.row .title', 14, 600, 22],
    ['.row .sub', 12, 400, 20], ['.row .money', 16, 600, 24], ['.row .badge', 12, 400, 20],
    ['#search', 13, 400, 20], ['footer', 12, 400, 20], ['.print-heading h2', 16, 600, 24]
  ];
  const matchingText = [
    ['#matchingDeskDialog h2', 20, 600, 28], ['.matching-lane h3', 16, 600, 24],
    ['.matching-preview-heading', 16, 600, 24], ['.matching-card strong', 14, 600, 22],
    ['.matching-card b', 16, 600, 24], ['.matching-card small', 12, 400, 20],
    ['.matching-create-fields label', 13, 400, 20], ['.matching-totals strong', 16, 600, 24]
  ];
  const checkPeopleText = async (page, mode) => {
    await page.locator('#reimbursementPeople').click();
    await page.locator('.reimbursement-summary table').waitFor();
    await checkTypography(page, [
      ['.reimbursement-summary h2', 20, 600, 28], ['.reimbursement-summary [data-content]>p', 14, 400, 22],
      ['.reimbursement-summary .muted', 12, 400, 20], ['.reimbursement-summary thead th', 12, 600, 20],
      ['.reimbursement-summary td', 13, 400, 20], ['.reimbursement-name-combo input', 13, 400, 20],
      ['.reimbursement-summary-total strong', 16, 600, 24]
    ], mode + ' people');
    await page.locator('.reimbursement-summary .dialog-close').click();
  };
  const checkGeometry = async (page, selectors, mode) => {
    for (const selector of selectors) {
      assert.ok(await page.locator(selector).count(), 'Geometry selector must resolve: ' + selector);
      for (const control of await page.locator(selector).all()) {
      if (!await control.isVisible()) continue;
      const value = await control.evaluate(node => {
        const style = getComputedStyle(node), box = node.getBoundingClientRect();
        return {id: node.id || node.name || node.textContent.trim().slice(0, 30), height: box.height,
          radius: style.borderTopLeftRadius, font: style.fontSize, lineHeight: style.lineHeight};
      });
      assert.equal(value.height, 40, `${mode} ${selector} has the standard height: ${JSON.stringify(value)}`);
      assert.equal(value.radius, '6px', `${mode} ${selector} has the shared corner radius`);
      geometry.push({mode, selector, ...value});
      }
    }
  };
  const checkDropdowns = async (page, scope, blank, mode) => {
    for (const select of await page.locator(scope + ' select').all()) {
      if (!await select.isVisible()) continue;
      const before = await select.inputValue();
      const style = await select.evaluate(node => {
        const s = getComputedStyle(node);
        return {id: node.id || node.name, appearance: s.appearance, image: s.backgroundImage,
          positionY: s.backgroundPositionY, size: s.backgroundSize, padding: parseFloat(s.paddingRight), disabled: node.disabled,
          height: node.getBoundingClientRect().height, radius: s.borderTopLeftRadius};
      });
      assert.equal(style.appearance, 'none', `${mode} ${style.id} uses the shared arrow`);
      assert.ok(style.image.includes('data:image/svg+xml'), `${mode} ${style.id} has a vector arrow`);
      assert.equal(style.positionY, '50%', `${mode} ${style.id} arrow is vertically centered`);
      assert.equal(style.size, '12px 12px'); assert.ok(style.padding >= 30, 'Selected text leaves room for the arrow: ' + JSON.stringify(style));
      assert.equal(style.height, 40, `${style.id} selection height matches adjacent buttons`); assert.equal(style.radius, '6px');
      if (!style.disabled) {
        await select.click(); assert.equal(await select.evaluate(node => node.matches(':open')), true, `${style.id} actually opens`);
        await page.locator(blank).click(); assert.equal(await select.evaluate(node => node.matches(':open')), false, `${style.id} closes on outside pointer`);
        assert.equal(await select.inputValue(), before, 'Dismissing a dropdown never changes the selection');
      }
      dropdowns.push({mode, scope, ...style, outsideDismissed: !style.disabled});
    }
  };
  const darkAppearance = page => page.evaluate(() => {
    const values = {}, selectors = ['body', '.rail', '.metric', '.tabs button.active', '.row.selected',
      '#projectLinks a[aria-current=page]', '#search', '#detail', '#matchingDeskDialog', '.matching-preview',
      '.matching-card.selected', '.matching-card strong', '.matching-card small', '.matching-proof',
      '.matching-card-image', '#matchingUpload', '#matchingProjectName', '#themeAudit .primary'];
    for (const selector of selectors) {
      const element = document.querySelector(selector);
      if (!element) {values[selector] = null; continue;}
      const style = getComputedStyle(element);
      values[selector] = Object.fromEntries(['color', 'backgroundColor', 'borderTopColor', 'outlineColor'].map(property => [property, style[property]]));
    }
    values.backdrop = {backgroundColor: getComputedStyle(document.querySelector('#matchingDeskDialog'), '::backdrop').backgroundColor};
    values.scrollbar = {scrollbarColor: getComputedStyle(document.querySelector('#detail')).scrollbarColor};
    values.shadow = {boxShadow: getComputedStyle(document.querySelector('#matchingDeskDialog')).boxShadow};
    return values;
  });
  const checkNeutral = appearance => {
    for (const [selector, colors] of Object.entries(appearance)) {
      assert.ok(colors, `Neutral dark palette: ${selector} must be represented`);
      for (const [property, color] of Object.entries(colors)) {
        const rgbValues = [...color.matchAll(/rgba?\((\d+), (\d+), (\d+)(?:, [\d.]+)?\)/g)];
        assert.ok(rgbValues.length, `Expected computed RGB ${selector} ${property}: ${color}`);
        for (const rgb of rgbValues) assert.ok(rgb[1] === rgb[2] && rgb[2] === rgb[3], `Dark ${selector} ${property} must be black or grey, got ${color}`);
      }
    }
  };
  let mainContext;
  try {
    let releaseInitialState; const initialStateGate = new Promise(resolve => {releaseInitialState = resolve;});
    mainContext = await context(undefined, initialStateGate); const page = await mainContext.newPage();
    await page.goto(origin + '/?project=p1', {waitUntil: 'domcontentloaded'}); await expectTheme(page, 'light');
    assert.equal(await page.locator('html').evaluate(root => getComputedStyle(root).backgroundColor), 'rgb(245, 246, 248)', 'First rendering uses light mode before the invoice state request completes');
    assert.equal(await page.locator('#list [data-item]').count(), 0, 'First-paint assertion must precede invoice state rendering');
    releaseInitialState(); await page.locator('#list [data-item=f1]').waitFor();
    const lightMetrics = await metricEdges(page); checkMetricEdges(lightMetrics, 'light');
    const lightMetricInteractions = await metricInteractions(page, 'light');
    await checkDropdowns(page, 'main', '.heading h1', 'light');
    await checkGeometry(page, ['#search', '.workbench-actions button', '#selectionTools>button'], 'light');
    assert.equal(await page.locator('.selection-tools').evaluate(node => getComputedStyle(node).gap), '8px');
    assert.equal(await page.evaluate(key => localStorage.getItem(key), key), null, 'Default mode is not a stored user preference');
    await page.locator('#list [data-item=f1] .title').click(); await page.locator('.print-pages img').waitFor();
    await checkTypography(page, mainText, 'light main');
    await page.evaluate(() => {
      const fixture = document.createElement('section'); fixture.id = 'themeAudit'; fixture.hidden = true;
      fixture.style.background = 'var(--wb-panel)';
      fixture.innerHTML = '<p class="muted">合成说明</p><p class="rule warn">待补材料</p><span class="badge good">已核对</span><p class="dialog-message is-error">合成错误</p><button class="primary">合成主操作</button>';
      document.body.append(fixture);
    });
    await page.locator('#materialMatching').click(); await page.locator('#matchingVouchers .matching-card').click();
    await page.locator('#matchingProjectName').waitFor();
    await checkDropdowns(page, '#matchingDeskDialog', '#matchingDeskDialog h2', 'light');
    await checkGeometry(page, ['#matchingDeskDialog .matching-toolbar>button', '#matchingClose', '#matchingProjectName', '#matchingAmount', '#matchingSave'], 'light matching');
    assert.equal(await page.locator('.matching-create-fields').evaluate(node => getComputedStyle(node).gap), '16px');
    const light = await colors(page); checkContrast(light, 'light');
    await checkTypography(page, matchingText, 'light matching');
    assert.equal(await page.locator('#matchingClose').evaluate(node => getComputedStyle(node).fontSize), '0px', 'The centered graphical close icon keeps its hidden text glyph');
    const protectedColors = () => page.locator('.matching-proof,.matching-card-image,.print-pages img').evaluateAll(nodes => nodes.map(node => ({background: getComputedStyle(node).backgroundColor, scheme: getComputedStyle(node).colorScheme})));
    assert.ok((await protectedColors()).every(value => value.background === 'rgb(255, 255, 255)' && value.scheme === 'light'));
    await page.locator('#matchingClose').click(); await checkPeopleText(page, 'light');
    await page.locator('#themeSelect').selectOption('dark',{force:true}); await expectTheme(page, 'dark');
    await checkTypography(page, mainText, 'dark main');
    const darkMetricInteractions = await metricInteractions(page, 'dark');
    await checkDropdowns(page, 'main', '.heading h1', 'dark');
    await checkGeometry(page, ['#search', '.workbench-actions button', '#selectionTools>button'], 'dark');
    assert.equal(await page.evaluate(key => localStorage.getItem(key), key), 'dark');
    await page.locator('#materialMatching').click(); await page.locator('#matchingVouchers .matching-card').click();
    const dark = await colors(page); checkContrast(dark, 'dark');
    await checkTypography(page, matchingText, 'dark matching');
    await checkDropdowns(page, '#matchingDeskDialog', '#matchingDeskDialog h2', 'dark');
    const darkMetrics = await metricEdges(page); checkMetricEdges(darkMetrics, 'dark');
    const neutral = await darkAppearance(page); checkNeutral(neutral);
    await page.locator('#matchingProjectName').focus();
    const focus = await page.locator('#matchingProjectName').evaluate(node => ({outlineColor: getComputedStyle(node).outlineColor}));
    checkNeutral({inputFocus: focus});
    await page.locator('#matchingUpload').hover();
    const hover = await page.locator('#matchingUpload').evaluate(node => ({backgroundColor: getComputedStyle(node).backgroundColor, color: getComputedStyle(node).color}));
    checkNeutral({primaryHover: hover});
    await page.keyboard.press('Tab'); await page.locator('.matching-proof').focus();
    assert.ok(await page.locator('.matching-proof').evaluate(node => node.matches(':focus-visible')), 'Link focus is tested with keyboard modality');
    assert.equal(await page.locator('.matching-proof').evaluate(node => getComputedStyle(node).outlineWidth), '2px', 'Dark links retain their visible keyboard focus indicator');
    const linkFocus = await page.locator('.matching-proof').evaluate(node => ({color: getComputedStyle(node).color, outlineColor: getComputedStyle(node).outlineColor}));
    checkNeutral({linkFocus});
    assert.ok((await protectedColors()).every(value => value.background === 'rgb(255, 255, 255)' && value.scheme === 'light'));
    assert.notDeepEqual(light[0].background, dark[0].background);
    await page.locator('#matchingClose').click(); await checkPeopleText(page, 'dark'); await page.locator('#previewReport').click();
    const frame = page.frameLocator('#previewFrame'); await frame.locator('.print-page img').first().waitFor();
    await checkDropdowns(page, '#reportPreviewDialog', '#reportPreviewDialog h2', 'dark');
    await checkGeometry(page, ['#reportPreviewDialog .preview-tools button', '#closePreview'], 'dark preview');
    await checkTypography(page, [['#reportPreviewDialog h2', 20, 600, 28], ['#previewScope', 12, 400, 20],
      ['#reportPreviewDialog label', 13, 400, 20]], 'dark preview');
    assert.equal(await frame.locator('.print-page img').first().evaluate(node => getComputedStyle(node).backgroundColor), 'rgb(255, 255, 255)', 'Company preview paper stays white in dark mode');
    assert.equal(await frame.locator('html').getAttribute('data-theme'), 'dark', 'Page surround follows the workbench dark palette');
    assert.equal(await frame.locator('details,a,.notice,.expense').count(), 0, 'Only final PDF page images are shown');
    await page.keyboard.press('Escape'); await page.reload(); await page.locator('#list [data-item=f1]').waitFor(); await expectTheme(page, 'dark');
    await page.locator('#themeSelect').selectOption('light',{force:true}); await page.reload(); await page.locator('#list [data-item=f1]').waitFor(); await expectTheme(page, 'light');
    // Unsupported values never activate arbitrary theme names or overwrite a valid saved choice.
    await page.locator('#themeSelect').evaluate(select => {select.add(new Option('invalid', 'invalid')); select.value = 'invalid'; select.dispatchEvent(new Event('change', {bubbles: true}));});
    await expectTheme(page, 'light'); assert.equal(await page.evaluate(key => localStorage.getItem(key), key), 'light');
    await page.locator('#themeSelect').selectOption('dark',{force:true});
    const embedded = await mainContext.newPage(); await embedded.goto(origin + '/host'); const inner = embedded.frames().find(frame => frame.url().includes('embedded=1'));
    await inner.locator('html.embedded-workbench').waitFor(); await expectTheme(inner, 'dark');
    const hostColor = await embedded.locator('body').evaluate(node => getComputedStyle(node).backgroundColor);
    await inner.locator('#themeSelect').selectOption('light',{force:true}); await expectTheme(inner, 'light');
    assert.equal(await embedded.locator('body').evaluate(node => getComputedStyle(node).backgroundColor), hostColor, 'Embedded selection cannot style its parent shell');
    await expectTheme(page, 'dark');
    const logistics = await mainContext.newPage(); await logistics.goto(origin + '/logistics');
    const untouched = await logistics.evaluate(() => ({background: getComputedStyle(document.documentElement).backgroundColor, text: getComputedStyle(document.documentElement).color,
      scheme: getComputedStyle(document.documentElement).colorScheme, inline: document.documentElement.style.colorScheme, input: getComputedStyle(document.querySelector('input')).backgroundColor}));
    assert.deepEqual(untouched, {background: 'rgb(24, 24, 24)', text: 'rgb(236, 236, 236)', scheme: 'dark', inline: '', input: 'rgb(35, 35, 35)'}, 'Theme is confined to invoice and embedded roots');
    await page.setViewportSize({width: 390, height: 844});
    await checkTypography(page, [['.heading h1', 20, 600, 28], ['.metric b', 24, 600, 32],
      ['.heading p', 14, 400, 22], ['.row .title', 14, 600, 22], ['.row .sub', 12, 400, 20]], 'mobile dark');
    await checkDropdowns(page, 'main', '.heading h1', 'mobile dark');
    await checkGeometry(page, ['#search', '.workbench-actions button', '#selectionTools>button'], 'mobile dark');
    for (const mode of ['dark', 'light']) {
      await page.locator('#themeSelect').selectOption(mode,{force:true});
      await checkTypography(page, [['.heading h1', 20, 600, 28], ['.heading p', 14, 400, 22],
        ['.row .sub', 12, 400, 20]], 'mobile ' + mode);
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `${mode} narrow main text never forces page overflow`);
      await page.locator('#materialMatching').click(); await page.locator('#matchingVouchers .matching-card').click();
      await checkTypography(page, matchingText, 'mobile ' + mode + ' matching');
      assert.ok(await page.locator('#matchingDeskDialog').evaluate(node => node.scrollWidth <= node.clientWidth), `${mode} narrow matching content fits its dialog`);
      await page.locator('#matchingClose').click();
    }
    await page.setViewportSize({width: 1400, height: 1000});
    evidence.push({defaultLightWithDarkOS: true, firstRenderingBeforeState: true, remembered: true, contrast: {light, dark},
      neutralDark: {colors: neutral, focus, hover, linkFocus}, uniformMetricEdges: {light: lightMetrics, dark: darkMetrics},
      metricInteractions: {light: lightMetricInteractions, dark: darkMetricInteractions}, dropdowns, controlGeometry: geometry, typography,
      originalsWhite: true, templateUnchanged: true, embeddedIndependent: true, logisticsUnchanged: true});
    await mainContext.close(); mainContext = null;

    for (const failure of ['invalid', 'read', 'write']) {
      const isolated = await context(failure === 'invalid' ? `localStorage.setItem('${key}', 'automatic')` : failure === 'read' ?
        "Object.defineProperty(window, 'localStorage', {get() { throw new DOMException('Storage denied', 'SecurityError'); }});" :
        "Storage.prototype.setItem = function() { throw new DOMException('Quota exceeded', 'QuotaExceededError'); };" );
      const page = await isolated.newPage(); await page.goto(origin + '/?project=p1'); await page.locator('#list [data-item=f1]').waitFor(); await expectTheme(page, 'light');
      await page.locator('#themeSelect').selectOption('dark',{force:true}); await expectTheme(page, 'dark');
      await page.reload(); await page.locator('#list [data-item=f1]').waitFor(); await expectTheme(page, 'light');
      evidence.push({storageFailure: failure, lightFallback: true, selectionStillWorks: true}); await isolated.close();
    }
    assert.deepEqual(errors, []); assert.ok(writes.every(value => value === '/api/expense-print-preview'));
    return {passed: true, evidence, businessMutations: 0};
  } finally {if (mainContext) await mainContext.close(); await browser.close();}
}
module.exports = run;
if (require.main === module) run(require('playwright')).then(result => console.log(JSON.stringify(result))).catch(error => {console.error(error); process.exitCode = 1;});
