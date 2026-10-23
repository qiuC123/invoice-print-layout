// Upload-hook contract using the real two legacy modules and synthetic APIs.
// This does not mount the Agent panel, call a model, or touch a live ledger.
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');

async function run(pw) {
  const browser = await pw.chromium.launch({headless: true});
  const assets = path.resolve(__dirname, '../src/invoice_print_layout/web');
  const origin = 'http://127.0.0.1:18817';
  const results = [];
  const file = name => ({name: name + '.png', mimeType: 'image/png', buffer: Buffer.from('synthetic ' + name)});

  async function scenario(kind, mode, {names = ['new', 'review', 'duplicate', 'linked', 'foreign', 'unassigned', 'archived', 'converted', 'same-id', 'failed'], startFails = false, openFails = false, project = 'p1', holdFirst = false} = {}) {
    const context = await browser.newContext({viewport: {width: 1450, height: 1000}});
    const page = await context.newPage(); page.setDefaultTimeout(10000);
    const writes = [], errors = [], queue = [{id: 'history', project_id: 'p1', status: 'review', name: 'old.png', issues: [], fields: {}, result: '已保存'}];
    let releaseFirst, firstSeen;
    const held = new Promise(resolve => {releaseFirst = resolve;}), seen = new Promise(resolve => {firstSeen = resolve;});
    const board = {vouchers: [], payments: [], other: [], unused: [], archived: [], deleted: [], matches: [], suggestions: [], report_types: [], supplement_candidates: []};
    const harness = `
      'use strict';
      const projectScope=${JSON.stringify(project)};
      let state={token:'synthetic',projects:[{id:'p1',name:'Synthetic One'}],items:[],roles:{invoice:'发票',payment:'付款截图'}};
      let active=null,view='review',checked=new Set();
      const $=id=>document.getElementById(id);
      const esc=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
      const money=c=>(c/100).toFixed(2);
      const currentInvoiceProjectId=()=>projectScope;
      const projectContext=()=>state.projects.find(p=>p.id===projectScope);
      const dialogLoadingMarkup=text=>'<p>'+esc(text)+'</p>';
      const dialogLoadErrorMarkup=text=>'<p>'+esc(text)+'</p><button data-dialog-retry>重试</button>';
      window.__agentCalls=[];window.__opened=0;window.__loads=0;
      function notify(text,error=false){$('feedback').textContent=text;$('feedback').dataset.error=String(error);}
      async function api(route,body){const r=await fetch('/api/'+route,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body||{})});const result=await r.json();if(!r.ok)throw Error(result.error||'synthetic failure');return result;}
      async function load(){window.__loads++;}
      async function act(fn){try{await fn();}catch(error){notify(error.message,true);}}
      ${mode === 'missing' ? '' : `window.manualInvoiceAgent={shouldAutoOrganize:()=>${mode === 'auto'},
        startReceived:async value=>{window.__agentCalls.push(value);await api('agent/start',value);},
        open:()=>{window.__opened++;${openFails ? "throw Error('synthetic panel refresh unavailable');" : ''}}};`}
    `;
    await page.route(origin + '/**', async route => {
      const req = route.request(), url = new URL(req.url());
      const json = (value, status = 200) => route.fulfill({status, contentType: 'application/json', body: JSON.stringify(value)});
      if (req.method() === 'POST') {
        const body = req.postDataJSON(); writes.push({route: url.pathname, body});
        if (url.pathname === '/api/intake/receive') {
          if (holdFirst && writes.filter(x => x.route === url.pathname).length === 1) {firstSeen(); await held;}
          const name = body.name.replace(/\.png$/, '');
          if (name === 'failed') return json({error: 'synthetic upload unavailable'}, 503);
          const entry = {id: name === 'same-id' ? 'new' : name, name: body.name, project_id: project, status: 'received', item_id: '', issues: [], fields: {}, result: '已保存'};
          if (name === 'review') entry.status = 'review';
          if (name === 'duplicate') {entry.status = 'duplicate'; entry.duplicate = true;}
          if (name === 'linked') entry.item_id = 'existing-expense';
          if (name === 'foreign') entry.project_id = 'p2';
          if (name === 'unassigned') entry.project_id = '';
          if (['archived', 'converted'].includes(name)) entry.status = name;
          queue.push(entry); return json(entry);
        }
        if (url.pathname === '/api/intake/prepare') return json({entry: queue.find(x => x.id === body.id), converted_to_pdf: false});
        if (url.pathname === '/api/material-matching/auto') return json({suggested: 0});
        if (url.pathname === '/api/agent/start') return startFails ? json({error: 'synthetic start unavailable'}, 503) : json({job: {id: 'job-one', state: 'queued'}});
        assert.fail('Unexpected mutation: ' + url.pathname);
      }
      if (url.pathname === '/api/material-matching') return json(board);
      if (url.pathname === '/api/intake') return json({queue});
      if (url.pathname === '/harness.js') return route.fulfill({contentType: 'text/javascript', body: harness});
      if (url.pathname === '/') return route.fulfill({contentType: 'text/html; charset=utf-8', body: `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"></head><body><button id="materialMatching">材料合成台</button><p id="feedback" role="status"></p><dialog id="unrelated" open>Unrelated dialog</dialog><script src="/harness.js"></script><script src="/${kind === 'matching' ? 'matching-desk' : 'intake'}.js"></script></body></html>`});
      const name = url.pathname.slice(1);
      assert.ok(['matching-desk.js', 'intake.js'].includes(name));
      return route.fulfill({contentType: 'text/javascript', body: await fs.readFile(path.join(assets, name))});
    });
    page.on('pageerror', error => errors.push(error.message));
    try {
      await page.goto(origin + '/');
      if (kind === 'matching') await page.locator('#materialMatching').click();
      else await page.evaluate(() => window.openUnifiedIntake());
      const input = page.locator(kind === 'matching' ? '#matchingFiles' : '#intakeFiles');
      const dialog = page.locator(kind === 'matching' ? '#matchingDeskDialog' : '#intakeDialog');
      if (kind === 'matching') await page.waitForFunction(() => !document.getElementById('matchingUpload').disabled);
      await input.setInputFiles(names.map(file));
      if (holdFirst) {
        await seen;
        await input.evaluate(node => node.dispatchEvent(new Event('change')));
        releaseFirst();
      }
      await page.waitForFunction(({kind}) => {
        const input = document.getElementById(kind === 'matching' ? 'matchingFiles' : 'intakeFiles');
        return !input.disabled && !input.files.length;
      }, {kind});
      const payloads = await page.evaluate(() => ({calls: window.__agentCalls, opened: window.__opened, loads: window.__loads}));
      const starts = writes.filter(x => x.route === '/api/agent/start');
      const receives = writes.filter(x => x.route === '/api/intake/receive');
      assert.equal(receives.length, names.length, 'each selected file is received once, including while change is repeated');
      assert.equal(new Set(receives.map(x => x.body.batch)).size, 1, 'one captured batch id');
      assert.ok(receives.every(x => x.body.project_id === project), 'upload captures its project');
      assert.equal(await page.locator('#unrelated').evaluate(node => node.open), true, 'only its own upload dialog may close');
      if (mode === 'auto' && project) {
        assert.equal(writes.filter(x => x.route === '/api/intake/prepare' || x.route === '/api/material-matching/auto').length, 0, 'the Agent owns preparation; no competing legacy scan');
        const eligible = names.filter(name => ['new', 'review', 'same-id'].includes(name)).map(name => name === 'same-id' ? 'new' : name);
        const expected = [...new Set(eligible)];
        assert.equal(starts.length, expected.length ? 1 : 0, 'one start for just this batch, none for only ineligible materials');
        if (expected.length) {
          assert.deepEqual(payloads.calls[0], {projectId: project, entryIds: expected, batch: receives[0].body.batch});
          assert.deepEqual(starts[0].body, payloads.calls[0]);
          assert.equal(payloads.calls[0].entryIds.includes('history'), false, 'old queue entries are never adopted');
          assert.equal(await dialog.evaluate(node => node.open), startFails);
          assert.equal(payloads.opened, startFails ? 0 : 1);
          if (startFails) {
            assert.match(await (kind === 'matching' ? page.locator('#matchingMessage') : page.locator('#feedback')).innerText(), /原件.*保留|原件已保存/);
            await page.locator(kind === 'matching' ? '#matchingRefresh' : '#intakeRefresh').click();
            assert.equal(writes.filter(x => x.route === '/api/agent/start').length, 1, 'read retry cannot restart the saved batch');
          } else if (openFails) {
            assert.match(await page.locator('#feedback').innerText(), /任务已启动.*无需重新上传/);
            assert.equal(starts.length, 1, 'panel display failure does not retry the accepted task');
          } else {
            assert.match(await page.locator('#feedback').innerText(), new RegExp(`重复 ${names.includes('duplicate') ? 1 : 0} 份`));
            assert.match(await page.locator('#feedback').innerText(), new RegExp(`上传失败 ${names.includes('failed') ? 1 : 0} 份`));
          }
        } else assert.equal(await dialog.evaluate(node => node.open), true);
        assert.equal(payloads.loads, 0, 'upload hands off without rebuilding the user workbench');
      } else {
        assert.equal(starts.length, 0, 'manual/off/missing-module path creates no Agent job');
        assert.equal(writes.filter(x => x.route === '/api/intake/prepare').length, names.filter(name => !['duplicate', 'failed'].includes(name)).length);
        assert.equal(writes.filter(x => x.route === '/api/material-matching/auto').length, 1, 'manual preparation and suggestion path retained');
        assert.equal(await dialog.evaluate(node => node.open), true);
        assert.equal(payloads.loads, 1);
        if (mode === 'auto' && !project) assert.match(await page.locator('#feedback').innerText(), /先从侧栏选择项目/);
      }
      assert.deepEqual(errors, []);
      assert.equal(writes.some(x => /\/(?:upload|transition|review\/confirm|report|material-matching\/create)$/.test(x.route)), false, 'no fee attachment, verification, creation or submission is triggered by the hook');
      results.push({kind, mode, project: !!project, startFails, openFails, receives: receives.length, starts: starts.length});
    } finally {releaseFirst(); await context.close();}
  }
  try {
    for (const kind of ['matching', 'intake']) {
      await scenario(kind, 'auto', {holdFirst: true});
      await scenario(kind, 'auto', {names: ['new'], startFails: true});
      await scenario(kind, 'auto', {names: ['new'], openFails: true});
      await scenario(kind, 'auto', {names: ['duplicate', 'linked', 'foreign', 'unassigned', 'archived']});
      await scenario(kind, 'off', {names: ['new', 'duplicate']});
      await scenario(kind, 'missing', {names: ['new', 'duplicate']});
    }
    await scenario('intake', 'auto', {names: ['new'], project: ''});
    return {passed: true, scenarios: results.length, results, no_live_data: true};
  } finally {await browser.close();}
}
module.exports = run;
if (require.main === module) run(require('playwright')).then(result => console.log(JSON.stringify(result))).catch(error => {console.error(error); process.exitCode = 1;});
