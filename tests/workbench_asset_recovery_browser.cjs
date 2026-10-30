// Real HTML/scripts with isolated synthetic HTTP responses; no running service or ledger.
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const {chromium} = require('playwright');

const origin = 'http://127.0.0.1:18865';
const counterKey = 'invoice-workbench-asset-reloads';
const source = path.resolve(__dirname, '../src/invoice_print_layout/web');
const state = {app_mode: 'invoice', token: 'synthetic', projects: [], items: [], inbox: [],
  categories: ['其他'], roles: {}, stages: {}, category_auto_ready: false};
const scenarios = ['style', 'script', 'blocking-script', 'dialog', 'typing', 'editable', 'limit',
  'hidden', 'hidden-inflight', 'timeout', 'storage-read', 'storage-write', 'storage-remove', 'reset', 'manual', 'normal', 'quick-style', 'foreign'];

(async () => {
  const browser = await chromium.launch({headless: true});
  try {
    for (const scenario of scenarios) {
      const page = await browser.newPage();
      page.setDefaultTimeout(10000);
      let documents = 0, styleRequests = 0, runtimeMode = 'ready', writes = 0;
      const errors = [], held = [];
      await page.clock.install();
      await page.addInitScript(({scenario}) => {
        if (scenario === 'reset') window.reloadCountAtStart = sessionStorage.getItem('invoice-workbench-asset-reloads');
        window.testHidden = false;
        Object.defineProperty(document, 'hidden', {get: () => window.testHidden});
        window.assetProbes = [];
        const originalFetch = window.fetch;
        window.fetch = async (url, options) => {
          const probe = url === '/api/runtime' && options?.signal ? {cache: options.cache, done: false, aborted: false} : null;
          if (probe) window.assetProbes.push(probe);
          try { return await originalFetch(url, options); }
          finally { if (probe) { probe.done = true; probe.aborted = options.signal.aborted; } }
        };
        if (scenario === 'storage-read') Object.defineProperty(window, 'sessionStorage', {get() { throw Error('denied'); }});
        if (scenario === 'storage-remove') Storage.prototype.removeItem = function () { throw Error('denied'); };
        if (scenario === 'storage-write') {
          const set = Storage.prototype.setItem;
          Storage.prototype.setItem = function (key, value) {
            if (this === sessionStorage) throw Error('denied');
            return set.call(this, key, value);
          };
        }
      }, {scenario});
      page.on('pageerror', error => errors.push({document: documents, message: error.message}));
      await page.route(origin + '/**', async route => {
        const req = route.request(), url = new URL(req.url());
        const json = body => route.fulfill({contentType: 'application/json', body: JSON.stringify(body)});
        if (req.method() !== 'GET') writes++;
        if (url.pathname === '/api/state') return json(state);
        if (url.pathname === '/api/runtime') {
          if (runtimeMode === 'hang') { held.push(route); return; }
          if (runtimeMode === 'offline') return route.abort('connectionrefused');
          if (runtimeMode === '503') return route.fulfill({status: 503, body: ''});
          return json({started_at: 'synthetic-start', build_id: 'synthetic-build'});
        }
        if (url.pathname === '/api/settings') return json({version: 2, enabled: false, providers: [], organize: {}, mail: {projects: []}});
        if (url.pathname.startsWith('/api/')) return json({queue: []});
        const name = url.pathname === '/' ? 'index.html' : url.pathname.slice(1);
        if (name === 'index.html') documents++;
        if (name === 'style.css') {
          styleRequests++;
          if ((scenario === 'style' && documents === 1) || (scenario === 'quick-style' && styleRequests === 1)) return route.abort('connectionrefused');
        }
        const failedScript = scenario === 'blocking-script' ? 'workbench-theme.js' : 'app.js';
        if (name === failedScript && !['normal', 'foreign', 'style', 'quick-style'].includes(scenario) &&
          (documents === 1 || scenario === 'limit' || (scenario === 'reset' && documents % 2 === 1))) return route.abort('connectionrefused');
        if (!/^[a-z-]+\.(html|js|css)$/.test(name)) return route.abort();
        return route.fulfill({contentType: name.endsWith('.js') ? 'text/javascript' : name.endsWith('.css') ? 'text/css' : 'text/html',
          body: await fs.readFile(path.join(source, name))});
      });
      const advance = async milliseconds => {
        try { await page.clock.runFor(milliseconds); }
        catch (error) {
          // Real dependent timers throw when app.js is absent; still exercise the inline recovery clock.
          if (!String(error).includes('ReferenceError: state is not defined')) throw error;
        }
      };
      const status = page.locator('#workbenchAssetStatus');
      const hidden = value => page.evaluate(value => { window.testHidden = value; document.dispatchEvent(new Event('visibilitychange')); }, value);
      const healthy = async () => {
        await page.waitForFunction(() => typeof loadCompleted !== 'undefined' && loadCompleted && document.documentElement.dataset.workbenchAssets === 'ready');
        assert.equal(await status.isVisible(), false);
        assert.equal(await page.locator('body').evaluate(node => getComputedStyle(node).fontFamily.includes('Microsoft YaHei')), true);
        await page.locator('#settingsOpen').click();
        await page.locator('#settingsDialog[open]').waitFor();
        await page.evaluate(() => document.querySelector('#settingsDialog').close());
      };
      const reload = async action => {
        const navigation = page.waitForEvent('domcontentloaded');
        await action();
        await navigation;
      };
      await page.goto(origin);
      if (scenario === 'style' || scenario === 'quick-style') {
        // Exhaust the original document's CSS attempts before restoring the asset.
        for (let step = 0; step < 5; step++) {
          await advance(500);
          if (await status.isVisible() || await page.locator('html').getAttribute('data-workbench-assets') === 'ready') break;
        }
      }
      if (['normal', 'quick-style', 'foreign'].includes(scenario)) {
        await healthy();
        if (scenario === 'foreign') {
          await page.route('https://foreign.example/**', route => route.abort('connectionrefused'));
          await page.evaluate(async () => {
            await Promise.all(['link', 'script'].map(tag => new Promise(resolve => {
              const node = document.createElement(tag); node.onerror = resolve;
              if (tag === 'link') { node.rel = 'stylesheet'; node.href = 'https://foreign.example/style.css'; }
              else node.src = 'https://foreign.example/app.js';
              document.head.append(node);
            })));
          });
        }
        await advance(60000);
        assert.equal(documents, 1);
        assert.equal(await status.count(), 0, 'normal and foreign-resource paths never show recovery');
        assert.equal(await page.evaluate(() => window.assetProbes.length), 0);
        assert.equal(await page.evaluate(key => sessionStorage.getItem(key), counterKey), null);
      } else {
        await status.waitFor();
        assert.match(await status.innerText(), /页面没加载完整，正在等待工作台服务/);
        if (scenario === 'dialog') await page.evaluate(() => document.querySelector('dialog').showModal());
        if (scenario === 'typing') await page.locator('#search').fill('未保存的输入');
        if (scenario === 'editable') await page.evaluate(() => {
          const editor = document.createElement('div'); editor.contentEditable = 'true'; document.body.append(editor); editor.focus();
        });
        if (['dialog', 'typing', 'editable', 'storage-read', 'storage-write'].includes(scenario)) {
          await advance(3000);
          await page.waitForFunction(() => document.querySelector('#workbenchAssetStatus').textContent.includes('工作台已恢复'));
          await advance(15000);
          assert.equal(documents, 1, 'editing or unavailable storage requires explicit refresh');
          assert.equal(await status.locator('button').innerText(), '刷新');
          if (scenario.startsWith('storage-')) assert.match(await status.innerText(), /无法保存自动刷新次数/);
          if (scenario === 'typing') assert.equal(await page.locator('#search').inputValue(), '未保存的输入');
          await page.evaluate(() => document.querySelectorAll('dialog[open]').forEach(dialog => dialog.close()));
          await reload(() => status.locator('button').click());
          await healthy();
        } else if (scenario === 'limit') {
          for (let count = 1; count <= 3; count++) {
            await reload(() => advance(3000));
            await status.waitFor();
            assert.equal(documents, count + 1);
            assert.equal(await page.evaluate(key => sessionStorage.getItem(key), counterKey), String(count));
          }
          await advance(3000);
          await page.waitForFunction(() => document.querySelector('#workbenchAssetStatus').textContent.includes('自动刷新已达上限'));
          await advance(60000);
          assert.equal(documents, 4, 'at most three automatic reloads during consecutive failures');
          await reload(() => status.locator('button').click());
          assert.equal(documents, 5, 'manual refresh remains available at the cap');
        } else if (scenario === 'manual') {
          runtimeMode = 'offline';
          await reload(() => status.locator('button').click());
          await healthy();
          assert.equal(await page.evaluate(key => sessionStorage.getItem(key), counterKey), null, 'manual reload does not spend automatic budget');
        } else {
          if (scenario === 'hidden') {
            await hidden(true); await advance(15000);
            assert.equal(await page.evaluate(() => window.assetProbes.length), 0);
            assert.equal(documents, 1);
            await hidden(false);
          }
          if (scenario === 'timeout' || scenario === 'hidden-inflight') {
            runtimeMode = 'hang'; await advance(3000);
            await page.waitForFunction(() => window.assetProbes.length === 1);
            if (scenario === 'hidden-inflight') await hidden(true);
            else await advance(2000);
            await page.waitForFunction(() => window.assetProbes[0].done);
            assert.equal(await page.evaluate(() => window.assetProbes[0].aborted), true);
            assert.equal(documents, 1);
            if (scenario === 'hidden-inflight') {
              await advance(15000);
              assert.equal(await page.evaluate(() => window.assetProbes.length), 1);
              await hidden(false);
            }
          }
          // A reachable HTTP error and a network failure both keep waiting.
          runtimeMode = '503';
          let probes = await page.evaluate(() => window.assetProbes.length);
          await advance(3000);
          await page.waitForFunction(count => window.assetProbes.length === count + 1 && window.assetProbes.at(-1).done, probes);
          assert.equal(documents, 1);
          runtimeMode = 'offline'; probes++;
          await advance(3000);
          await page.waitForFunction(count => window.assetProbes.length === count + 1 && window.assetProbes.at(-1).done, probes);
          assert.equal(documents, 1);
          assert.ok(await page.evaluate(() => window.assetProbes.every(probe => probe.cache === 'no-store')));
          runtimeMode = 'ready';
          await reload(() => advance(3000));
          await healthy();
          assert.equal(documents, 2);
          assert.equal(await page.evaluate(key => sessionStorage.getItem(key), counterKey), scenario === 'storage-remove' ? '1' : null);
          if (scenario === 'reset') {
            assert.equal(await page.evaluate(() => window.reloadCountAtStart), '1', 'automatic reload spends one attempt before successful loading');
            for (let cycle = 2; cycle <= 4; cycle++) {
              await reload(() => page.reload());
              await status.waitFor();
              await reload(() => advance(3000));
              await healthy();
              assert.equal(documents, cycle * 2, 'later failures retain their automatic recovery budget');
              assert.equal(await page.evaluate(() => window.reloadCountAtStart), '1');
              assert.equal(await page.evaluate(key => sessionStorage.getItem(key), counterKey), null, 'successful recovery clears the counter');
            }
          }
        }
      }
      assert.equal(writes, 0, 'recovery never mutates business data');
      // Missing app.js can break dependents on the broken document, but the recovered page must be clean.
      if (scenario !== 'limit') assert.deepEqual(errors.filter(error => error.document === documents), []);
      for (const route of held) await route.abort().catch(() => {});
      await page.close();
      console.log(`PASS asset recovery: ${scenario} (${documents} documents)`);
    }
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
