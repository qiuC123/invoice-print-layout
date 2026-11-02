// Real invoice-mode server, synthetic data, UI mutations only.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const net = require('node:net');
const {spawn, spawnSync} = require('node:child_process');
const {createHash} = require('node:crypto');
const root = path.resolve(__dirname, '../../../..');
const python = path.join(root, '.venv/Scripts/python.exe');
const env = {...process.env, PYTHONPATH: path.join(root, 'src'), PYTHONUTF8: '1', PYTHONDONTWRITEBYTECODE: '1'};
const [command = 'run', name] = process.argv.slice(2);
assert.match(name || '', /^[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}$/, 'Provide a unique run name (letters, digits, _ or -)');
const run = path.join(root, '.runtime/verify', name);
Object.assign(env, {TYPESAFE_API_KEY: '', JEV_API_KEY: ''});
const sessionFile = path.join(run, 'session.json');
let createdHere = false;
const save = (file, data) => fs.writeFileSync(path.join(run, file), JSON.stringify(data, null, 2));
const read = file => JSON.parse(fs.readFileSync(path.join(run, file), 'utf8'));
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
function log(message) {
  console.log(message);
  if (fs.existsSync(run)) fs.appendFileSync(path.join(run, 'driver.log'), message + '\n');
}
function py(args) {
  const result = spawnSync(python, args, {cwd: root, env, encoding: 'utf8', windowsHide: true});
  assert.equal(result.status, 0, result.stderr || result.stdout || String(result.error));
  return result.stdout;
}
function session() {
  const s = read('session.json');
  assert.equal(s.workspace, path.join(run, 'data'));
  assert.equal(s.base, `http://127.0.0.1:${s.port}`);
  assert(s.port > 0 && s.port !== 8765 && s.pid > 0);
  return s;
}
function listener(port) {
  const probe = spawnSync('powershell.exe', ['-NoProfile', '-Command',
    `$ownerId = @(Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort ${port} -State Listen -ErrorAction Stop).OwningProcess; Get-CimInstance Win32_Process -Filter "ProcessId=$ownerId" | Select-Object ProcessId,ParentProcessId,CommandLine,CreationDate | ConvertTo-Json -Compress`],
    {encoding: 'utf8', windowsHide: true});
  assert.equal(probe.status, 0, probe.stderr);
  return JSON.parse(probe.stdout);
}
async function json(s, route) {
  const response = await fetch(s.base + route, {signal: AbortSignal.timeout(5000)});
  assert(response.ok, `${route}: ${response.status}`);
  return response.json();
}
async function start() {
  assert(!fs.existsSync(run), 'Run directory exists; use a new name, never overwrite evidence');
  fs.mkdirSync(run, {recursive: true});
  createdHere = true;
  py([path.join(__dirname, 'fixture.py'), 'prepare', run]);
  const port = await new Promise((resolve, reject) => {
    const socket = net.createServer(); socket.on('error', reject);
    socket.listen(0, '127.0.0.1', () => {const p = socket.address().port; socket.close(() => resolve(p));});
  });
  assert.notEqual(port, 8765);
  const workspace = path.join(run, 'data');
  const expected = JSON.parse(py(['-m', 'invoice_print_layout.workbench_runtime', '--workspace', workspace, '--mode', 'invoice']));
  const log = fs.openSync(path.join(run, 'server.log'), 'a');
  const child = spawn(python, ['-m', 'invoice_print_layout.workbench_web', '--workspace', workspace,
    '--port', String(port), '--mode', 'invoice'], {cwd: root, env, windowsHide: true, detached: true, stdio: ['ignore', log, log]});
  fs.closeSync(log);
  child.unref();
  const s = {base: `http://127.0.0.1:${port}`, port, workspace, pid: child.pid, expected,
    command: 'python -m invoice_print_layout.workbench_web --mode invoice', created: new Date().toISOString()};
  save('session.json', s);
  let runtime;
  for (let i = 0; i < 60; i++) {
    try {runtime = await json(s, '/api/runtime'); break;} catch {await pause(500);}
  }
  assert(runtime, 'Server not ready; inspect server.log');
  s.started_at = runtime.started_at; save('session.json', s);
  const owner = listener(port);
  // Windows venv's python.exe can be a launcher whose child owns the socket.
  assert(owner.ProcessId === child.pid || owner.ParentProcessId === child.pid);
  assert(owner.CommandLine.includes(workspace) && owner.CommandLine.includes('invoice_print_layout.workbench_web'));
  s.launcher_pid = child.pid; s.pid = owner.ProcessId; s.process_created = owner.CreationDate;
  save('session.json', s);
  await doctor();
}
async function doctor() {
  const s = session();
  process.kill(s.pid, 0);
  const runtime = await json(s, '/api/runtime');
  const expected = JSON.parse(py(['-m', 'invoice_print_layout.workbench_runtime', '--workspace', s.workspace, '--mode', 'invoice']));
  for (const key of ['app_id', 'app_mode', 'workspace_id', 'build_id']) assert.equal(runtime[key], expected[key], key);
  assert.equal(runtime.started_at, s.started_at); assert.equal(runtime.ready, true);
  assert(expected.capabilities.every(c => runtime.capabilities.includes(c)));
  const settings = await json(s, '/api/settings');
  assert.equal(settings.enabled, false); assert.equal(settings.organize.use_model, false);
  assert.equal(settings.organize.auto_after_upload, true); assert.equal(settings.mail.enabled, false);
  const state = await json(s, '/api/state');
  assert(state.token && Array.isArray(state.items) && Array.isArray(state.projects));
  // Tie the listener to our recorded PID without enumerating any unrelated port/process.
  const owner = listener(s.port);
  assert.equal(owner.ProcessId, s.pid, 'Listening PID differs');
  assert.equal(owner.CreationDate, s.process_created, 'PID was reused');
  save('doctor.json', {checked: new Date().toISOString(), pid: s.pid, port: s.port, runtime,
    settings: {ai_enabled: settings.enabled, organize: settings.organize, mail_enabled: settings.mail.enabled},
    state: {projects: state.projects.length, expenses: state.items.length, authenticated_local_ui: true}});
  log(`Doctor PASS ${s.base} PID=${s.pid}`);
  return s;
}
async function stop() {
  if (!fs.existsSync(sessionFile)) {
    if (createdHere) {
      removeTemporaryData();
      save('cleanup.json', {stopped: true, process_not_started: true, temporary_data_removed: true,
        evidence_retained: fs.readdirSync(run)});
    }
    return;
  }
  const s = session();
  let runtime;
  try {runtime = await json(s, '/api/runtime');} catch (error) {
    try {process.kill(s.pid, 0);} catch {save('cleanup.json', {stopped: true, already_exited: true}); return;}
    throw Error(`Cannot verify live instance identity; did not kill PID ${s.pid}: ${error.message}`);
  }
  assert.equal(runtime.workspace_id, s.expected.workspace_id);
  assert.equal(runtime.build_id, s.expected.build_id);
  assert.equal(runtime.started_at, s.started_at);
  const state = await json(s, '/api/state');
  const response = await fetch(s.base + '/api/shutdown', {method: 'POST',
    headers: {'Content-Type': 'application/json', 'X-Workbench-Token': state.token}, body: '{}'});
  assert(response.ok, 'Own instance shutdown rejected');
  let exited = false;
  for (let i = 0; i < 80; i++) {
    try {process.kill(s.pid, 0);} catch {exited = true; break;}
    await pause(250);
  }
  assert(exited, 'Own instance did not exit; preserve data for diagnosis');
  const closed = await new Promise(resolve => {
    const socket = net.connect(s.port, '127.0.0.1');
    socket.once('error', () => resolve(true)); socket.once('connect', () => {socket.destroy(); resolve(false);});
  });
  assert(closed, 'Temporary port still responds; did not touch another process');
  removeTemporaryData();
  const evidence = fs.readdirSync(run).filter(file => !['session.json', 'cleanup.json'].includes(file));
  assert(evidence.length > 0);
  save('cleanup.json', {stopped: true, pid_exited: true, temporary_port_closed: true,
    temporary_data_removed: true, evidence_retained: evidence});
  log(`Cleanup PASS; evidence retained: ${run}`);
}
function removeTemporaryData() {
  for (const dir of ['data', 'materials']) {
    const target = path.resolve(run, dir);
    assert.equal(path.dirname(target), run);
    if (!fs.existsSync(target)) continue;
    assert(fs.realpathSync(target).startsWith(fs.realpathSync(run) + path.sep), 'Cleanup path escaped run');
    fs.rmSync(target, {recursive: true, force: false});
  }
}
async function drive() {
  const s = await doctor(), fixture = read('fixture.json');
  const {chromium} = require('playwright');
  const browser = await chromium.launch({headless: true});
  const context = await browser.newContext({viewport: {width: 1440, height: 1000}});
  await context.tracing.start({screenshots: true, snapshots: true, sources: false});
  const page = await context.newPage();
  page.setDefaultTimeout(15000);
  const results = [], traffic = [], errors = [], networkFailures = [];
  const pendingResponses = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('requestfailed', request => networkFailures.push({url: request.url(), failure: request.failure()}));
  page.on('response', response => {
    if (response.request().method() !== 'POST') return;
    pendingResponses.push((async () => {
      let result; try {result = await response.json();} catch {result = {unreadable: true};}
      traffic.push({at: new Date().toISOString(), path: new URL(response.url()).pathname,
        status: response.status(), request: response.request().postDataJSON(), result});
    })());
  });
  const state = () => json(s, '/api/state');
  const jobs = () => json(s, '/api/agent/jobs?project=' + encodeURIComponent(new URL(page.url()).searchParams.get('project') || ''));
  async function until(check, message, timeout = 120000) {
    const end = Date.now() + timeout;
    do {const value = await check(); if (value) return value; await pause(500);} while (Date.now() < end);
    throw Error(message);
  }
  async function capture(label) {
    await page.screenshot({path: path.join(run, label + '.png'), fullPage: true});
    fs.writeFileSync(path.join(run, label + '.aria.txt'), await page.locator('body').ariaSnapshot());
    const snapshot = await state(); delete snapshot.token; save(label + '.state.json', snapshot);
    const taskState = await jobs(); delete taskState.token; save(label + '.jobs.json', taskState);
    save(label + '.intake.json', await json(s, '/api/intake'));
  }
  async function feature(id, action) {
    log(`START ${id}`);
    try {const detail = await action(); results.push({id, status: '通过', ...detail});}
    catch (error) {
      results.push({id, status: '失败', reason: error.stack});
      try {await capture(id + '-failed');} catch {}
      await doctor();
    }
    save('results.json', {results, complete: false});
    log(`${results.at(-1).status} ${id}`);
  }
  async function ready(scoped = true) {
    // User-equivalent reload recovery; do not fulfil, mock or throttle HTTP responses.
    for (let attempt = 0; attempt < 3; attempt++) {
      try {
        await page.waitForFunction(() => typeof document.getElementById('createProject')?.onclick === 'function' &&
          typeof document.getElementById('materialMatching')?.onclick === 'function' &&
          typeof window.manualInvoiceAgent?.startReceived === 'function' &&
          document.querySelector('#manualAgent')?.childElementCount > 0 &&
          typeof document.getElementById('previewReport')?.onclick === 'function' &&
          document.documentElement.dataset.workbenchAssets === 'ready', null, {timeout: 6000});
        if (scoped) await page.locator('#materialMatching:enabled').waitFor({timeout: 6000});
        return;
      }
      catch {
        await capture('asset-recovery-' + traffic.length + '-' + attempt);
        if (attempt === 2) throw Error('Real page assets did not become operable after two reloads');
        await page.reload({waitUntil: 'load'});
      }
    }
  }
  async function desk() {
    if (await page.locator('#matchingDeskDialog').isVisible()) return;
    await ready(); await page.getByRole('button', {name: '材料合成台', exact: true}).click();
    await page.locator('#matchingDeskDialog').waitFor();
  }
  async function closeDesk() {
    if (await page.locator('#matchingDeskDialog').isVisible()) await page.locator('#matchingClose').click();
  }
  async function settled(count) {
    await until(async () => {
      const data = await state();
      const taskState = await jobs();
      return data.items.filter(item => !item.deleted && !item.merged_into).length === count &&
        taskState.jobs.length && taskState.jobs.every(job => !['queued', 'running', 'pending'].includes(job.state));
    }, `Expected ${count} automatically paired expenses`);
  }
  async function dragFiles(files, target) {
    const box = await page.locator(target).boundingBox(); assert(box);
    const cdp = await context.newCDPSession(page);
    try {
      const data = {items: [], files, dragOperationsMask: 1};
      const x = box.x + box.width / 2, y = box.y + Math.min(box.height / 2, 100);
      await cdp.send('Input.dispatchDragEvent', {type: 'dragEnter', x, y, data});
      await cdp.send('Input.dispatchDragEvent', {type: 'dragOver', x, y, data});
      await page.screenshot({path: path.join(run, 'drop-overlay-' + results.length + '.png')});
      await cdp.send('Input.dispatchDragEvent', {type: 'drop', x, y, data});
    } finally {await cdp.detach();}
  }
  async function download(link, filename, pages, markers) {
    const href = await link.getAttribute('href'); assert(href && href.startsWith('/export/'));
    const response = await context.request.get(s.base + href); assert(response.ok());
    const file = path.join(run, filename); fs.writeFileSync(file, await response.body());
    py([path.join(__dirname, 'fixture.py'), 'pdf', file, String(pages), ...markers]);
    return filename;
  }
  let project;
  try {
    await page.goto(s.base, {waitUntil: 'load'});
    await ready(false);
    await feature('F1-projects', async () => {
      for (const [name, start, end] of [['合成验证甲', '2026-10-01', '2026-10-31'], ['合成验证乙', '2026-11-01', '']]) {
        await page.locator('#createProject').click();
        const dialog = page.locator('#project-create');
        await dialog.locator('[name=name]').fill(name);
        await dialog.locator('[name=start_date]').fill(start);
        await dialog.locator('[name=end_date]').fill(end);
        await capture('F1-create-' + name.slice(-1));
        const previous = page.url();
        await Promise.all([page.waitForURL(url => url.href !== previous, {waitUntil: 'load'}),
          dialog.getByRole('button', {name: '创建项目', exact: true}).click()]);
        await ready();
        await page.locator('#projectPeriod').getByText(end ? `项目时间：${start} 至 ${end}` : `项目时间：${start} 起，进行中`, {exact: true}).waitFor();
      }
      const previous = page.url();
      await Promise.all([page.waitForURL(url => url.href !== previous, {waitUntil: 'load'}),
        page.getByRole('link', {name: '合成验证甲 · 发票报销', exact: true}).click()]);
      await ready();
      project = (await state()).projects.find(p => p.name === '合成验证甲'); assert(project);
      await page.locator(`#projectLinks [data-project-menu-id="${project.id}"]`).click({button: 'right'});
      await page.locator('[data-project-action=period]').click();
      const period = page.locator('#projectActionDialog');
      await period.locator('[name=end_date]').fill('2026-10-30');
      await period.getByRole('button', {name: '保存项目时间', exact: true}).click();
      await page.locator('#projectPeriod').getByText('项目时间：2026-10-01 至 2026-10-30', {exact: true}).waitFor();
      await page.reload(); await ready();
      assert.equal((await state()).projects.find(p => p.id === project.id).end_date, '2026-10-30');
      await capture('F1-project-switch-period');
      return {project_id: project.id, entries: ['新建项目', '侧栏切换', '右键项目→设置项目时间'], persisted: true};
    });
    assert(project, 'F1 project prerequisite failed');
    await feature('F2-upload-files', async () => {
      await desk();
      const chooser = page.waitForEvent('filechooser');
      await page.getByRole('button', {name: '＋ 收材料', exact: true}).click();
      await (await chooser).setFiles(fixture.pairs.slice(0, 7).flatMap(p => [p.invoice, p.payment]));
      await capture('F2-upload-start'); await settled(7); await capture('F2-upload-organized');
      return {originals: 14, expenses: 7, native_filechooser: true};
    });
    assert.equal(results.at(-1).status, '通过', 'Primary upload/organize prerequisite failed; later features were not run');
    await feature('F2-folder', async () => {
      await desk(); const chooser = page.waitForEvent('filechooser');
      await page.getByRole('button', {name: '导入文件夹', exact: true}).click();
      await (await chooser).setFiles(path.join(run, 'materials/folder'));
      await page.locator('#notice').filter({hasText: /跳过 1/}).waitFor();
      await capture('F2-folder-upload-summary');
      await settled(8); await capture('F2-folder-imported');
      return {recursive_originals: 2, unsupported_skipped: 1,
        native_upload_confirmation: '确认进不去：原生窗口不在 page DOM；已试 filechooser + setFiles(directory)，只能交付目录，未操作系统确认窗'};
    });
    await feature('F2-drop-files-desk', async () => {
      await desk(); const p = fixture.pairs[8];
      await dragFiles([p.invoice, p.payment], '#matchingDeskDialog');
      await settled(9); await capture('F2-desk-dropped'); return {originals: 2, method: 'CDP real local file paths'};
    });
    await feature('F2-drop-folder-main', async () => {
      await closeDesk(); await dragFiles([path.join(run, 'materials/drag-folder')], 'main');
      await settled(10); await capture('F2-main-folder-dropped');
      return {recursive_originals: 2, method: 'CDP real local directory path'};
    });
    await feature('F2-drop-folder-desk', async () => {
      await desk(); await dragFiles([path.join(run, 'materials/drag-folder')], '#matchingDeskDialog');
      await until(async () => /已收过 2/.test(await page.locator('#matchingMessage').innerText()), 'Duplicate folder drop not reported');
      assert.equal((await state()).items.length, 10); await capture('F2-desk-folder-duplicate');
      return {duplicate_originals: 2, expenses_unchanged: 10};
    });
    await feature('F2-drop-files-main', async () => {
      await closeDesk(); const p = fixture.pairs[8]; await dragFiles([p.invoice, p.payment], 'main');
      await until(async () => /已收过 2/.test(await page.locator('#matchingMessage').innerText()), 'Duplicate file drop not reported');
      assert.equal((await state()).items.length, 10); await capture('F2-main-files-duplicate');
      return {duplicate_originals: 2, expenses_unchanged: 10};
    });
    await feature('F3-local-organize-match', async () => {
      const data = await state(), taskState = await jobs(); delete taskState.token; save('F3-jobs.json', taskState);
      assert.equal(data.items.length, 10);
      for (const pair of fixture.pairs) {
        const item = data.items.find(item => item.amount_cents === pair.amount_cents); assert(item, pair.merchant);
        assert.equal(item.verified, false); assert.equal(item.stage, 'draft');
        assert(item.attachments.some(a => a.role === 'invoice') && item.attachments.some(a => a.role === 'payment'));
        assert.equal(item.material_match.alignment_status, 'balanced');
      }
      assert(taskState.jobs.length >= 4 && taskState.jobs.every(j => j.models === 0 && !j.use_model));
      const settings = await json(s, '/api/settings'); assert.equal(settings.enabled, false);
      await closeDesk(); await page.reload(); await ready(); await capture('F3-real-paired-drafts');
      return {expenses: 10, total_cents: 35500, models: 0, mutations: 'Upload UI started organizer; no seeded rows or mocked APIs'};
    });
    await feature('F4-review-next-visible', async () => {
      await closeDesk(); await page.locator('#tabs [data-view=review]').click();
      const rows = page.locator('#list [data-item]'); assert.equal(await rows.count(), 10);
      const ids = await rows.evaluateAll(nodes => nodes.map(n => n.dataset.item));
      const selected = ids[ids.length - 3], next = ids[ids.length - 2];
      await page.locator(`[data-item="${selected}"]`).click();
      await page.locator('#expensePrintPdf:not([hidden])').waitFor();
      const geometry = () => page.locator(`[data-item="${next}"]`).evaluate((node, selected) => {
        const r = node.getBoundingClientRect(), list = document.getElementById('list'), p = list.getBoundingClientRect();
        const current = list.querySelector(`[data-item="${selected}"]`)?.getBoundingClientRect();
        return {top: r.top, bottom: r.bottom, list_top: p.top, list_bottom: p.bottom,
          viewport: innerHeight, scroll_top: list.scrollTop,
          current_top: current?.top, current_bottom: current?.bottom,
          notice_visible: !document.getElementById('notice').hidden,
          visible: r.top >= p.top - 1 && r.bottom <= p.bottom + 1 && r.bottom <= innerHeight};
      }, selected);
      // Put the current row at the lower edge so the next row must be revealed after removal and the notice.
      await page.locator('#list').hover();
      const initial = await geometry();
      await page.mouse.wheel(0, initial.current_bottom - (initial.list_bottom - 2));
      await page.waitForFunction(id => {
        const list = document.getElementById('list'), row = list.querySelector(`[data-item="${id}"]`);
        return Math.abs(row.getBoundingClientRect().bottom - (list.getBoundingClientRect().bottom - 2)) <= 2;
      }, selected);
      await capture('F4-before-next');
      const before = await geometry(); save('F4-before-next-visibility.json', before);
      assert(!before.notice_visible, 'F4-next precondition: completion notice must start hidden');
      assert(before.current_top >= before.list_top && before.current_bottom <= before.list_bottom,
        'F4-next precondition: current row must remain visible');
      assert(!before.visible && before.bottom > before.list_bottom + 1,
        'F4-next precondition: next row must start outside the visible list below its lower edge');
      await page.locator('#verifyNext').click();
      await page.locator(`[data-item="${next}"].selected`).waitFor();
      await page.locator('#notice').filter({hasText: '本笔核对完成，已打开当前筛选下的下一笔。'}).waitFor();
      await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
      const visible = await geometry(); save('F4-next-visibility.json', visible);
      await capture('F4-after-next');
      // Continue through the remaining real rows with both single-review buttons.
      await page.locator('#verifyNext').click();
      await until(async () => (await state()).items.filter(i => i.verified).length === 2, 'Second verification not persisted');
      await page.locator(`[data-item="${ids.at(-1)}"].selected`).waitFor();
      for (const id of ids) {
        const item = (await state()).items.find(i => i.id === id); if (item.verified) continue;
        await page.locator('#tabs [data-view=review]').click();
        await page.locator(`[data-item="${id}"]`).click();
        await page.locator('#detail [data-action=verify]').click();
        await page.locator('#tabs [data-view=submitted].active').waitFor();
        await until(async () => (await state()).items.find(i => i.id === id).verified, 'Single review did not persist');
      }
      await page.locator('#tabs [data-view=submitted]').click();
      await page.locator(`[data-item="${selected}"]`).click();
      await page.locator(`[data-check="${selected}"]`).check();
      await page.locator('#returnSelected').click();
      await page.locator('#confirmVerify').click();
      await until(async () => !(await state()).items.find(i => i.id === selected).verified, 'Return not persisted');
      await page.locator('#notice').filter({hasText: /退回/}).waitFor();
      await page.locator('#tabs [data-view=review]').click();
      await page.locator(`[data-item="${selected}"]`).click();
      await capture('F4-returned-review');
      await page.locator('#detail [data-action=verify]').click();
      await until(async () => (await state()).items.every(i => i.verified), 'Reverification not persisted');
      await page.locator('#tabs [data-view=submitted]').click(); await capture('F4-all-reviewed');
      assert(visible.visible, 'F4-next: Next row outside visible list after verification and completion notice');
      return {reviewed: 10, continuous_reviews: 2, next_row_before: before, next_row: visible, return_and_reverify: true};
    });
    await feature('F5-single-vector-pdf', async () => {
      await closeDesk(); await page.locator('#tabs [data-view=submitted]').click();
      const item = (await state()).items[0];
      await page.locator(`[data-item="${item.id}"]`).click();
      await page.locator('#expensePrintPdf:not([hidden])').waitFor();
      const marker = '9900000000000000' + String((item.amount_cents / 100) - 30).padStart(4, '0');
      const filename = await download(page.locator('#expensePrintPdf'), 'F5-single.pdf', 1, [marker]);
      await capture('F5-single-preview'); return {pdf: filename, pages: 1, extractable_vector_text: true};
    });
    await feature('F5-batch-package', async () => {
      await page.locator('#tabs [data-view=submitted]').click();
      await page.getByRole('checkbox', {name: '全选当前列表', exact: true}).check(); await page.locator('#exportSelected').click();
      await page.locator('#reportDialog[open]').waitFor();
      await page.locator('#reportTemplateFile').setInputFiles(path.join(run, 'materials/synthetic-template.xlsx'));
      await page.locator('#createReport:enabled').waitFor(); await page.locator('#closeReport').click();
      const previewResponse = page.waitForResponse(r => r.url().endsWith('/api/report-preview') && r.request().method() === 'POST', {timeout: 120000});
      await page.locator('#previewReport').click();
      const response = await previewResponse, preview = await response.json(); save('F5-batch-preview-response.json', preview);
      assert(response.ok(), JSON.stringify(preview));
      await page.frameLocator('#previewFrame').locator('.print-page img').first().waitFor({timeout: 120000});
      await capture('F5-batch-preview');
      const pdfResponse = await context.request.get(s.base + '/export/' + encodeURIComponent(preview.combined_pdf)); assert(pdfResponse.ok());
      const previewFile = path.join(run, 'F5-batch-preview.pdf'); fs.writeFileSync(previewFile, await pdfResponse.body());
      // Ten invoice+payment expenses each occupy one A4 page, preceded by one report page.
      const markers = fixture.pairs.map(p => '9900000000000000' + String(p.index).padStart(4, '0'));
      py([path.join(__dirname, 'fixture.py'), 'pdf', previewFile, '11', ...markers]);
      await page.locator('#closePreview').click(); await page.locator('#exportSelected').click();
      const completed = page.waitForResponse(r => r.url().endsWith('/api/report') && r.request().method() === 'POST', {timeout: 130000});
      await page.locator('#createReport:enabled').click();
      const packageResponse = await completed, packageResult = await packageResponse.json();
      save('F5-package-response.json', {status: packageResponse.status(), result: packageResult});
      assert(packageResponse.ok() && !packageResult.error, packageResult.error || 'Package creation failed');
      await page.locator('#reportDownloads').getByRole('link', {name: '下载总 PDF', exact: true}).waitFor();
      await download(page.locator('#reportDownloads').getByRole('link', {name: '下载总 PDF', exact: true}), 'F5-package.pdf', 11, markers);
      for (const [label, file] of [['下载 MD 清单', 'F5-package.md'], ['下载 Excel 报销表', 'F5-package.xlsx']]) {
        const href = await page.getByRole('link', {name: label, exact: true}).last().getAttribute('href');
        const response = await context.request.get(s.base + href); assert(response.ok()); fs.writeFileSync(path.join(run, file), await response.body());
      }
      await capture('F5-package-created'); return {expenses: 10, preview_pages: 11, package_pages: 11, extractable_vector_text: true};
    });
    // All original source bytes must survive every user mutation unchanged.
    const data = await state();
    const sourceChecks = [];
    for (const item of data.items) for (const a of item.attachments.filter(a => a.role !== 'package')) {
      const expected = Object.entries(fixture.sha256).find(([file]) => path.basename(file) === a.name); assert(expected, a.name);
      const response = await context.request.get(s.base + '/file/' + encodeURIComponent(a.id)); assert(response.ok());
      const sha256 = createHash('sha256').update(await response.body()).digest('hex');
      assert.equal(sha256, expected[1], `Original changed: ${a.name}`);
      sourceChecks.push({id: a.id, name: a.name, sha256, unchanged: true});
    }
    assert.equal(sourceChecks.length, 20);
    save('originals.json', {fixture_sha256: fixture.sha256, attachments: sourceChecks});
    await doctor();
  } catch (error) {
    results.push({id: 'flow-prerequisite', status: '失败', reason: error.stack});
    throw error;
  } finally {
    await Promise.allSettled(pendingResponses);
    // Base64 uploads are synthetic, but bulky; retain names/hashes rather than duplicating payloads.
    for (const entry of traffic) if (entry.request?.data) entry.request.data = '[synthetic payload omitted]';
    save('ui-posts.json', traffic); save('browser-errors.json', {errors, networkFailures});
    save('results.json', {complete: true, base: s.base, browser: 'isolated headless Chromium', browser_version: browser.version(), results});
    await context.tracing.stop({path: path.join(run, 'trace.zip')}); await browser.close();
  }
  assert(results.every(r => r.status === '通过'), 'Some features failed; inspect results.json');
}
(async () => {
  if (command === 'run') {
    assert(!fs.existsSync(run), 'Run directory exists; use a new name');
    try {await start(); await drive();} finally {await stop();}
  }
  else if (command === 'start') {
    assert(!fs.existsSync(run), 'Run directory exists; use a new name');
    try {await start();} catch (error) {await stop(); throw error;}
  }
  else if (command === 'doctor') await doctor();
  else if (command === 'drive') {try {await drive();} catch (error) {await stop(); throw error;}}
  else if (command === 'stop') await stop();
  else throw Error('Use run, start, doctor, drive or stop');
})().then(() => {
  save(command + '-command.json', {command: process.argv.slice(1), exit_code: 0});
}).catch(error => {
  console.error(error.stack); process.exitCode = 1;
  if (fs.existsSync(run) && (createdHere || !['run', 'start'].includes(command))) {
    fs.appendFileSync(path.join(run, 'driver.log'), error.stack + '\n');
    save(command + '-command.json', {command: process.argv.slice(1), exit_code: 1, error: error.stack});
  }
});
