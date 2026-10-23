'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require('playwright');
const root = path.join(__dirname, '..', 'src', 'invoice_print_layout', 'web');
(async () => {
  const browser = await chromium.launch({headless: true});
  const context = await browser.newContext({viewport: {width: 1050, height: 800}, permissions: ['clipboard-read', 'clipboard-write']});
  const calls = [], errors = [], cases = [];
  let data = {enabled: false, addresses: [], remote: {installed: false, ready: false}}, fail = false;
  const pair = code => ({enabled: true, mode: 'remote', clients: 0, expires_in: 600,
    url: 'http://invoice-desktop.tailtest.ts.net/mobile', pair_url: 'http://invoice-desktop.tailtest.ts.net/mobile#' + code,
    qr: 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScLbtAAAAABJRU5ErkJggg=='});
  await context.route('**/*', async route => {
    const request = route.request(), pathname = new URL(request.url()).pathname;
    if (pathname === '/') {await route.fulfill({contentType: 'text/html; charset=utf-8', body: '<meta charset="utf-8"><header><div class="actions"></div></header><script>var state={token:"desktop-test-token"};function esc(s){const d=document.createElement("div");d.textContent=String(s);return d.innerHTML.replaceAll(String.fromCharCode(34),"&quot;").replaceAll(String.fromCharCode(39),"&#39;");}</script><script src="/phone-link.js"></script><link rel="stylesheet" href="/phone-link.css">'});return;}
    if (pathname === '/phone-link.js' || pathname === '/phone-link.css') {await route.fulfill({contentType: pathname.endsWith('.js') ? 'text/javascript; charset=utf-8' : 'text/css; charset=utf-8', body: fs.readFileSync(path.join(root, pathname.slice(1)), 'utf8')});return;}
    assert.ok(pathname.startsWith('/api/phone-link'), pathname);
    const body = request.method() === 'POST' ? request.postDataJSON() : null;
    calls.push({path: pathname, body});
    if (body) {
      assert.equal(request.headers()['x-workbench-token'], 'desktop-test-token');
      if (fail) {await route.fulfill({status: 400, contentType: 'application/json', body: JSON.stringify({error: '连接暂未完成，请重试'})});return;}
      if (pathname.endsWith('/connect')) {assert.deepEqual(body, {mode: 'remote'});data.remote = {installed: true, ready: false, auth_url: 'https://login.tailscale.com/a/abc123'};}
      if (pathname.endsWith('/start')) {assert.deepEqual(body, {mode: 'remote'});data = {...data, ...pair('first-pair')};}
      if (pathname.endsWith('/rotate')) data = {...data, ...pair('second-pair')};
      if (pathname.endsWith('/stop')) {data = {enabled: false, addresses: [], remote: {installed: true, ready: true}};}
    }
    await route.fulfill({contentType: 'application/json', body: JSON.stringify(data)});
  });
  const page = await context.newPage();page.on('pageerror', e => errors.push(e.message));
  try {
    await page.goto('http://127.0.0.1:18773/');assert.deepEqual(errors, []);await page.getByRole('button', {name: '手机连接', exact: true}).click();
    await page.getByLabel('连接方式').waitFor();
    assert.equal(calls.filter(c => c.body).length, 0);
    assert.equal(await page.getByRole('button', {name: '开启手机连接', exact: true}).isDisabled(), true);
    await page.getByLabel('连接方式').selectOption('remote');
    assert.equal(await page.getByRole('button', {name: '连接私人网络', exact: true}).isDisabled(), true);
    cases.push('no implicit network start; missing LAN and missing component controls disabled');
    data.remote = {installed: true, ready: false};
    await page.getByRole('button', {name: '刷新连接状态', exact: true}).click();
    await page.getByRole('button', {name: '连接私人网络', exact: true}).click();
    await page.getByRole('link', {name: '打开账号登录链接', exact: true}).waitFor();
    assert.equal(await page.getByRole('link', {name: '打开账号登录链接', exact: true}).getAttribute('href'), 'https://login.tailscale.com/a/abc123');
    assert.equal(calls.filter(c => c.path.endsWith('/start')).length, 0);
    cases.push('remote connect displays own-account login without exposing a phone listener');
    data.remote = {installed: true, ready: true};
    await page.getByRole('button', {name: '刷新连接状态', exact: true}).click();
    await page.getByRole('button', {name: '开启异地访问', exact: true}).dblclick();
    await page.getByText('异地访问已开启 · 已配对 0 台', {exact: true}).waitFor();
    assert.equal(calls.filter(c => c.path.endsWith('/start')).length, 1);
    assert.equal(await page.getByLabel('手机配对链接', {exact: true}).inputValue(), pair('first-pair').pair_url);
    await page.getByRole('button', {name: '复制手机配对链接', exact: true}).click();
    await page.getByText('已复制，请在手机浏览器打开。', {exact: true}).waitFor();
    assert.equal(await page.evaluate(() => navigator.clipboard.readText()), pair('first-pair').pair_url);
    cases.push('account-ready explicit start is single-flight; complete pairing URL copied for remote phone');
    await page.getByRole('button', {name: '更新二维码', exact: true}).click();
    await page.waitForFunction(() => document.querySelector('[data-link]').value.endsWith('#second-pair'));
    fail = true;await page.getByRole('button', {name: '关闭手机连接', exact: true}).click();
    await page.getByRole('alert').filter({hasText: '连接暂未完成，请重试'}).waitFor();
    assert.equal(await page.getByRole('button', {name: '关闭手机连接', exact: true}).isEnabled(), true);
    fail = false;await page.getByRole('button', {name: '关闭手机连接', exact: true}).click();
    await page.getByRole('button', {name: '开启异地访问', exact: true}).waitFor();
    assert.equal(await page.locator('.phone-link-qr').count(), 0);
    assert.equal(await page.getByLabel('连接方式').inputValue(), 'remote');
    await page.getByRole('button', {name: '完成', exact: true}).click();await page.locator('#phoneLinkDialog').waitFor({state: 'detached'});
    cases.push('QR rotation, recoverable operation error, stop removes pairing data and retains mode');
    assert.deepEqual(errors, []);console.log(JSON.stringify({status: 'PASS', cases, requests: calls.length}));
  } finally {await browser.close();}
})().catch(error => {console.error(error);process.exitCode = 1;});
