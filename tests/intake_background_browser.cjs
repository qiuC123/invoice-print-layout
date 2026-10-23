// Actual upload modules, twenty-file batches and held background recognition.
// All APIs and documents are synthetic; this never reads a user's ledger.
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');

async function run(pw) {
  const browser = await pw.chromium.launch({headless: true});
  const assets = path.resolve(__dirname, '../src/invoice_print_layout/web');
  const origin = 'http://127.0.0.1:18944';
  const results = [];
  async function scenario(kind, resume = false) {
    const context = await browser.newContext();
    const page = await context.newPage(); page.setDefaultTimeout(10000);
    const writes = [], errors = [], entries = [];
    const old = {id: 'old', key: 'q:old', name: 'synthetic-existing.png', revision: 1, role: 'other',
      project_id: 'p1', amount_cents: 3000, status: 'review', issues: [], result: '已保存',
      fields: {amount: '30.00'}, url: '/intake-file/old'};
    const board = {vouchers: [], payments: [], other: [old], unused: [], archived: [], deleted: [],
      matches: [], suggestions: [], report_types: [], supplement_candidates: []};
    let job = null, boardReads = 0;
    const harness = `
      'use strict'; const projectScope='p1';
      let state={background_intake_ready:true,token:'synthetic',projects:[{id:'p1',name:'Synthetic'}],items:[],roles:{invoice:'发票',payment:'付款'}};
      let active=null,view='review',checked=new Set();
      const $=id=>document.getElementById(id), esc=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
      const money=c=>(c/100).toFixed(2), currentInvoiceProjectId=()=>projectScope,projectContext=()=>state.projects[0];
      const dialogLoadingMarkup=text=>'<p>'+esc(text)+'</p>',dialogLoadErrorMarkup=text=>'<p>'+esc(text)+'</p><button data-dialog-retry>重试</button>';
      function notify(text){$('feedback').textContent=text;}
      async function api(route,body){const r=await fetch('/api/'+route,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body||{})});const data=await r.json();if(!r.ok)throw Error(data.error);return data;}
      async function act(fn){try{await fn();}catch(error){notify(error.message);}}
      async function load(){};
    `;
    if (resume) entries.push({id: 'unfinished', batch: 'old-batch', project_id: 'p1', status: 'received', fields: {}, issues: [], name: 'unfinished.png'});
    page.on('pageerror', error => errors.push(error.message));
    await page.route(origin + '/**', async route => {
      const request = route.request(), url = new URL(request.url());
      const json = value => route.fulfill({contentType: 'application/json', body: JSON.stringify(value)});
      if (request.method() === 'POST') {
        const body = request.postDataJSON(); writes.push({route: url.pathname, body});
        if (url.pathname === '/api/intake/receive') {
          const entry = {id: `new-${entries.length}`, project_id: 'p1', batch: body.batch,
            name: body.name, status: 'received', result: '已保存', fields: {}, issues: []};
          entries.push(entry); return json(entry);
        }
        if (url.pathname === '/api/intake/enqueue') {
          job = {batch: body.batch, ids: body.ids, total: body.ids.length, completed: 0, failed: 0, running: 1, state: 'running'};
          return json(job);
        }
        if (url.pathname === '/api/intake/classify') {
          Object.assign(old, {revision: old.revision + 1, role: body.role, fields: body.fields});
          return json(old);
        }
        assert.fail('Unexpected mutation while background work is held: ' + url.pathname);
      }
      if (url.pathname === '/api/intake/progress') return json({batches: job ? [job] : [],
        pending: !job && resume ? entries : [], pending_count: !job && resume ? entries.length : 0});
      if (url.pathname === '/api/intake') return json({queue: [old, ...entries]});
      if (url.pathname === '/api/material-matching') {boardReads++; return json(board);}
      if (url.pathname === '/harness.js') return route.fulfill({contentType: 'text/javascript', body: harness});
      if (url.pathname === '/') return route.fulfill({contentType: 'text/html; charset=utf-8', body: `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"></head><body><button id="materialMatching">材料合成台</button><p id="feedback"></p><script src="/harness.js"></script><script src="/intake-progress.js"></script><script src="/intake.js"></script><script src="/matching-desk.js"></script></body></html>`});
      if (url.pathname.startsWith('/intake-file/')) return route.fulfill({contentType: 'image/png', body: Buffer.from('synthetic')});
      return route.fulfill({contentType: 'text/javascript', body: await fs.readFile(path.join(assets, url.pathname.slice(1)))});
    });
    try {
      await page.goto(origin + '/');
      if (kind === 'matching') await page.evaluate(() => window.openMaterialMatching());
      else await page.evaluate(() => window.openUnifiedIntake());
      if (resume) {
        await page.locator('#matchingBackgroundStatus [data-resume-intake]').waitFor();
        assert.equal(writes.length, 0, 'a restart/page load must never start recognition');
        await page.locator('#matchingBackgroundStatus [data-resume-intake]').click();
        await page.waitForFunction(() => document.getElementById('matchingBackgroundStatus').textContent.includes('识别中'));
      } else {
        const start = performance.now();
        await page.locator(kind === 'matching' ? '#matchingFiles' : '#intakeFiles').setInputFiles(
          Array.from({length: 20}, (_, index) => ({name: `synthetic-${index}.png`, mimeType: 'image/png', buffer: Buffer.from('synthetic ' + index)})));
        const close = page.locator(kind === 'matching' ? '#matchingClose' : '#intakeDialog [data-close]');
        await page.waitForFunction(() => document.querySelector('#matchingProgressText')?.textContent === '文件已保存，可以关闭窗口。' || document.querySelector('#feedback').textContent.includes('本批已保存 20'));
        assert.equal(await close.isEnabled(), true);
        const closableMs = performance.now() - start;
        assert.ok(closableMs < 5000, `window should be closable before OCR is released: ${closableMs}ms`);
        assert.equal(entries.length, 20);
        const enqueue = writes.filter(write => write.route === '/api/intake/enqueue');
        assert.equal(enqueue.length, 1); assert.equal(enqueue[0].body.ids.length, 20);
        assert.ok(writes.slice(0, 20).every(write => write.route === '/api/intake/receive'));
        assert.equal(job.completed, 0, 'recognition remains held while the window is released');
        await close.click();
        await page.evaluate(() => window.openMaterialMatching());
        results.push({kind, files: 20, closable_ms: Math.round(closableMs)});
      }
      // A second material is editable while the batch's recognition remains held.
      await page.locator('[data-classify-id="old"]').click();
      await page.locator('.matching-classify input[name="amount"]').fill('42.00');
      await page.locator('.matching-classify button[type="submit"]').click();
      await page.waitForFunction(() => !document.querySelector('.matching-classify'));
      assert.equal(old.fields.amount, '42.00'); assert.equal(job.state, 'running');
      const readsBefore = boardReads;
      job.completed = job.total; job.running = 0; job.state = 'finished';
      await page.waitForFunction(() => document.getElementById('matchingBackgroundStatus').textContent.includes('识别完成'));
      await page.waitForFunction(() => document.getElementById('matchingMessage').textContent.includes('后台识别完成'));
      assert.ok(boardReads > readsBefore, 'batch completion automatically refreshes the matching board');
      assert.equal(errors.length, 0, errors.join('\n'));
    } finally {await context.close();}
  }
  try {
    await scenario('intake'); await scenario('matching'); await scenario('matching', true);
    return {passed: true, results, resumed_only_on_click: true, edits_during_recognition: true, no_live_data: true};
  } finally {await browser.close();}
}
module.exports = run;
if (require.main === module) run(require('playwright')).then(result => console.log(JSON.stringify(result))).catch(error => {console.error(error); process.exitCode = 1;});
