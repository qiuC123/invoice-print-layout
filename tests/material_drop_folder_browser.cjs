// Synthetic files and intercepted local HTTP only; never access the production workspace.
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');

async function run(pw) {
  const browser = await pw.chromium.launch({headless: true});
  const page = await browser.newPage({viewport: {width: 1500, height: 1000}});
  const origin = 'http://127.0.0.1:18861', writes = [], errors = [], hashes = new Set(), jobs = [];
  let releaseProgress, failNextStart = false;
  const progressGate = new Promise(resolve => {releaseProgress = resolve;});
  const temp = await fs.mkdtemp(path.join(os.tmpdir(), 'invoice-drop-synthetic-'));
  const evidence = process.env.INVOICE_BROWSER_EVIDENCE;
  const state = {app_mode: 'invoice', token: 'synthetic', projects: [{id: 'p1', name: '合成验收项目'}], items: [], inbox: [],
    categories: ['其他'], roles: {invoice: '发票'}, stages: {draft: '待核对'}, report_template_ready: true};
  const board = {vouchers: [], payments: [], other: [], unused: [], archived: [], deleted: [], matches: [], suggestions: [], report_types: [], supplement_candidates: []};
  const files = names => names.map(name => ({name, mimeType: 'image/png', buffer: Buffer.from('synthetic ' + name)}));
  page.setDefaultTimeout(15000);
  page.on('pageerror', error => errors.push(error.message));
  await page.route(origin + '/**', async route => {
    const request = route.request(), url = new URL(request.url());
    const json = body => route.fulfill({contentType: 'application/json', body: JSON.stringify(body)});
    if (request.method() === 'POST') {
      const body = request.postDataJSON(); writes.push({path: url.pathname, body});
      if (url.pathname === '/api/intake/receive') {
        if (body.name === 'auto-11.png') await progressGate;
        const duplicate = hashes.has(body.data); hashes.add(body.data);
        return json({id: 'q' + writes.length, project_id: body.project_id, status: duplicate ? 'duplicate' : 'received', duplicate});
      }
      if (url.pathname === '/api/intake/prepare') return json({entry: {status: 'review'}});
      if (url.pathname === '/api/material-matching/auto') return json({suggested: 0});
      if (url.pathname === '/api/agent/start') {
        assert.ok(!jobs.some(job => ['queued', 'running'].includes(job.state)), 'real backend permits only one active job per project');
        assert.ok(body.material_ids.length <= 20);
        if (failNextStart) {failNextStart = false; return route.fulfill({status: 503, contentType: 'application/json', body: '{"error":"synthetic deferred start failure"}'});}
        const job = {id: 'job-' + jobs.length, project_id: body.project_id, state: 'queued', material_ids: body.material_ids, item_ids: []};
        jobs.push(job); return json({job});
      }
      assert.fail('Unexpected write: ' + url.pathname);
    }
    if (url.pathname === '/api/state') return json(state);
    if (url.pathname === '/api/runtime') return json({started_at: 'synthetic', build_id: 'synthetic'});
    if (url.pathname === '/api/material-matching') return json(board);
    if (url.pathname === '/api/intake') return json({queue: []});
    if (url.pathname === '/api/agent/jobs') return json({jobs, model: {available: false}});
    if (url.pathname === '/api/settings') return json({organize: {auto_after_upload: true, use_model: false, model_available: false}});
    if (url.pathname === '/api/settings/organize') return json({version: 1, auto_organize: false, use_model: false});
    if (url.pathname.startsWith('/api/')) return json({people: [], providers: [], tasks: [], outbox: []});
    const name = url.pathname === '/' ? 'index.html' : url.pathname.slice(1);
    assert.match(name, /^[a-z-]+\.(html|js|css|svg)$/);
    return route.fulfill({contentType: name.endsWith('.js') ? 'text/javascript' : name.endsWith('.css') ? 'text/css' : name.endsWith('.svg') ? 'image/svg+xml' : 'text/html',
      body: await fs.readFile(path.resolve(__dirname, '../src/invoice_print_layout/web', name))});
  });
  const received = () => writes.filter(row => row.path === '/api/intake/receive');
  const screenshot = async name => {if (evidence) {await fs.mkdir(evidence, {recursive: true}); await page.screenshot({path: path.join(evidence, name + '.png')});}};
  async function ready(project = true, realAgent = false) {
    await page.goto(origin + (project ? '/?project=p1' : '/'));
    await page.waitForFunction(() => window.manualInvoiceAgent && window.openMaterialMatching && state.token);
    await page.evaluate(realAgent => {
      window.invoiceIntakeProgress = null;
      if (realAgent) return;
      window.__starts = []; window.__auto = false; window.__failStart = 0;
      window.manualInvoiceAgent = {shouldAutoOrganize: () => window.__auto, open: async () => {}, startReceived: async batch => {
        window.__starts.push(batch);
        if (window.__failStart === window.__starts.length) throw Error('synthetic start failure');
      }};
    }, realAgent);
  }
  async function open() {await page.evaluate(() => window.openMaterialMatching()); await page.waitForFunction(() => !document.getElementById('matchingUpload').disabled);}
  async function done() {await page.waitForFunction(() => !document.getElementById('matchingFiles').disabled && /新增|200 个文件/.test(document.getElementById('matchingMessage').textContent));}
  async function drag(type, names = ['drag-a.pdf', 'drag-b.PNG'], target = 'body', text = false) {
    const data = await page.evaluateHandle(({names, text}) => {
      const transfer = new DataTransfer();
      if (text) transfer.setData('text/plain', 'synthetic text');
      else for (const name of names) transfer.items.add(new File(['synthetic ' + name], name, {type: 'image/png'}));
      return transfer;
    }, {names, text});
    await page.dispatchEvent(target, type, {dataTransfer: data}); await data.dispose();
  }
  try {
    await ready(); await open();
    // Exercise paginated directory enumeration, nested entries, fallback and failure atomically.
    const directoryChecks = await page.evaluate(async () => {
      let reads = 0;
      const file = name => ({isFile: true, file: resolve => resolve(new File(['synthetic'], name))});
      const directory = pages => ({isDirectory: true, createReader: () => ({readEntries: resolve => {reads++; resolve(pages.shift() || []);}})});
      const root = directory([[file('first.pdf')], [directory([[file('nested.webp')], [file('last.jpg')], []])], []]);
      const input = entry => ({items: [{kind: 'file', webkitGetAsEntry: () => entry, getAsFile: () => null}]});
      const names = (await readDroppedMaterialFiles(input(root))).map(file => file.name);
      const readCount = reads;
      const capped = await readDroppedMaterialFiles(input(directory([Array.from({length: 300}, (_, i) => file(i + '.pdf'))])));
      let failure = '';
      try {await readDroppedMaterialFiles(input(directory([[{isFile: true, file: (resolve, reject) => reject(Error('unreadable'))}]])));} catch (error) {failure = error.message;}
      const fallback = await readDroppedMaterialFiles({items: [], files: [new File(['x'], 'fallback.png')]});
      return {names, reads: readCount, cap: capped.length, failure, fallback: fallback[0].name};
    });
    assert.deepEqual(directoryChecks, {names: ['first.pdf', 'nested.webp', 'last.jpg'], reads: 6, cap: 201, failure: 'unreadable', fallback: 'fallback.png'});
    await screenshot('folder-button-and-hint');
    await page.evaluate(() => {const select = document.getElementById('matchingBatchPerson'); select.add(new Option('合成人员', 'person1')); select.value = 'person1';});
    await page.locator('#matchingFiles').setInputFiles(files(['selected.pdf'])); await done();
    assert.equal(received().at(-1).body.reimbursement_person_id, 'person1');
    await drag('dragenter', undefined, '#matchingDeskDialog');
    assert.equal(await page.locator('#matchingDropOverlay').innerText(), '松开即收进「合成验收项目」');
    assert.equal(await page.locator('#matchingDropOverlay').evaluate(node => node.matches(':popover-open')), true);
    await screenshot('drop-overlay');
    await drag('drop', undefined, '#matchingDeskDialog'); await done();
    assert.deepEqual(received().slice(-2).map(row => row.body.name), ['drag-a.pdf', 'drag-b.PNG']);
    assert.ok(received().slice(-2).every(row => row.body.project_id === 'p1' && row.body.reimbursement_person_id === 'person1'));
    await page.locator('#matchingClose').click();
    await drag('dragenter', ['main.webp']); await screenshot('main-page-drop-overlay');
    await drag('drop', ['main.webp']); await done();
    assert.equal(received().at(-1).body.name, 'main.webp');
    await page.evaluate(() => {
      const transfer = new DataTransfer(); transfer.items.add(new File(['placeholder'], 'folder'));
      const file = name => ({isFile: true, file: resolve => resolve(new File(['synthetic ' + name], name))});
      const directory = pages => ({isDirectory: true, createReader: () => ({readEntries: resolve => resolve(pages.shift() || [])})});
      Object.defineProperty(transfer, 'items', {value: [{kind: 'file', getAsFile: () => null,
        webkitGetAsEntry: () => directory([[file('drop-root.pdf')], [directory([[file('drop-nested.png')], []])], []])}]});
      document.getElementById('matchingDeskDialog').dispatchEvent(new DragEvent('drop', {bubbles: true, cancelable: true, dataTransfer: transfer}));
    });
    await done(); assert.deepEqual(received().slice(-2).map(row => row.body.name), ['drop-root.pdf', 'drop-nested.png']);
    const folder = path.join(temp, 'folder'); await fs.mkdir(path.join(folder, 'nested'), {recursive: true});
    await fs.writeFile(path.join(folder, 'top.pdf'), 'synthetic top');
    await fs.writeFile(path.join(folder, 'nested', 'child.jpg'), 'synthetic child');
    await fs.writeFile(path.join(folder, 'notes.txt'), 'skip');
    await page.locator('#matchingFolder').setInputFiles(folder); await done();
    assert.deepEqual(received().slice(-2).map(row => row.body.name).sort(), ['child.jpg', 'top.pdf']);
    assert.match(await page.locator('#matchingMessage').innerText(), /新增 2 份.*跳过 1 个/);
    await page.locator('#matchingFiles').setInputFiles([...files(['selected.pdf', 'fresh.webp', 'unsupported.txt']),
      {name: 'too-large.png', mimeType: 'image/png', buffer: Buffer.alloc(20 * 1024 * 1024 + 1)}]);
    await done(); assert.match(await page.locator('#matchingMessage').innerText(), /新增 1 份，已收过 1 份（未重复导入），跳过 2 个/);
    await screenshot('upload-summary');
    const beforeLimit = received().length;
    await page.locator('#matchingFiles').setInputFiles(files(Array.from({length: 201}, (_, i) => 'limit-' + i + '.pdf'))); await done();
    assert.equal(received().length, beforeLimit);
    assert.match(await page.locator('#matchingMessage').innerText(), /200 个文件.*本次未上传/);
    await page.locator('#matchingClose').click();
    await drag('dragenter', [], 'body', true);
    assert.equal(await page.locator('#matchingDropOverlay').isVisible(), false);
    await page.locator('#settingsOpen').click();
    await drag('dragenter', ['blocked.pdf'], '#settingsDialog');
    assert.equal(await page.locator('#matchingDropOverlay').isVisible(), false);
    await drag('drop', ['blocked.pdf'], '#settingsDialog'); assert.equal(received().length, beforeLimit);
    await page.keyboard.press('Escape');
    await open();
    await page.evaluate(() => {const editor = document.createElement('dialog'); editor.id = 'syntheticEditor'; document.body.append(editor); editor.showModal();});
    await drag('dragenter', ['blocked-edit.pdf'], '#syntheticEditor'); await drag('drop', ['blocked-edit.pdf'], '#syntheticEditor');
    assert.equal(await page.locator('#matchingDropOverlay').isVisible(), false); assert.equal(received().length, beforeLimit);
    await ready(false); await drag('dragenter');
    assert.equal(await page.locator('#matchingDropOverlay').innerText(), '先在左侧选择一个项目');
    await drag('drop'); assert.equal(received().length, beforeLimit);
    await ready(); await open();
    await page.evaluate(() => {window.__auto = true;});
    await page.locator('#matchingFiles').setInputFiles(files(Array.from({length: 45}, (_, i) => 'auto-' + i + '.png')));
    await page.waitForFunction(() => document.getElementById('matchingProgressText').textContent === '正在收材料 12/45');
    assert.equal(await page.locator('#matchingProgressBar').getAttribute('max'), '45');
    await screenshot('upload-progress'); releaseProgress();
    await page.waitForFunction(() => !document.getElementById('matchingDeskDialog').open);
    const calls = await page.evaluate(() => window.__starts);
    assert.deepEqual(calls.map(call => call.entryIds.length), [20, 20, 5]);
    assert.equal(new Set(calls.flatMap(call => call.entryIds)).size, 45);
    assert.equal(new Set(calls.map(call => call.batch)).size, 1);
    assert.ok(calls.every(call => call.projectId === 'p1'));
    assert.match(await page.locator('#notice').innerText(), /新增 45 份/);
    // A failed middle start retains its originals and does not prevent the final chunk.
    await open(); await page.evaluate(() => {window.__starts = []; window.__failStart = 2;});
    await page.locator('#matchingFiles').setInputFiles(files(Array.from({length: 45}, (_, i) => 'partial-' + i + '.png')));
    await page.waitForFunction(() => !document.getElementById('matchingDeskDialog').open);
    assert.deepEqual(await page.evaluate(() => window.__starts.map(call => call.entryIds.length)), [20, 20, 5]);
    assert.match(await page.locator('#error').innerText(), /25 份新材料.*未能全部启动.*原件保留/);
    // Real controller + one-active-job backend contract: subsequent chunks wait durably.
    await ready(true, true); await open();
    await page.locator('#matchingFiles').setInputFiles(files(Array.from({length: 45}, (_, i) => 'real-auto-' + i + '.png')));
    await page.waitForFunction(() => !document.getElementById('matchingDeskDialog').open);
    assert.equal(jobs.length, 1);
    assert.match(await page.locator('.mia-pending-starts').innerText(), /25 份材料等待分批整理/);
    await ready(false, true);
    jobs[0].state = 'ready';
    failNextStart = true;
    await ready(true, true);
    await page.getByRole('button', {name: '重试启动本批整理', exact: true}).waitFor();
    assert.equal(jobs.length, 1);
    const failedStart = writes.filter(row => row.path === '/api/agent/start').at(-1).body;
    await page.getByRole('button', {name: '重试启动本批整理', exact: true}).click();
    await page.waitForFunction(() => document.querySelectorAll('.mia-job').length === 2);
    assert.deepEqual(writes.filter(row => row.path === '/api/agent/start').at(-1).body, failedStart, 'deferred retry preserves the request identity');
    assert.deepEqual(jobs.map(job => job.material_ids.length), [20, 20]);
    assert.match(await page.locator('.mia-pending-starts').innerText(), /5 份材料等待分批整理/);
    jobs[1].state = 'needs_input';
    await page.evaluate(() => window.manualInvoiceAgent.refresh());
    await page.waitForFunction(() => document.querySelectorAll('.mia-job').length === 3);
    assert.deepEqual(jobs.map(job => job.material_ids.length), [20, 20, 5]);
    assert.equal(new Set(jobs.flatMap(job => job.material_ids)).size, 45);
    assert.equal(await page.locator('.mia-pending-starts').innerText(), '');
    assert.deepEqual(errors, []);
    return {passed: true, recursive_directory: directoryChecks, auto_batches: [20, 20, 5], real_controller_batches: jobs.map(job => job.material_ids.length), receives: received().length, no_live_data: true};
  } finally {releaseProgress(); await browser.close(); await fs.rm(temp, {recursive: true, force: true});}
}
module.exports = run;
if (require.main === module) run(require('playwright')).then(result => console.log(JSON.stringify(result))).catch(error => {console.error(error); process.exitCode = 1;});
