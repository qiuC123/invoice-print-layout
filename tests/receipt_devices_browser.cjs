'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
async function run(pw) {
  const browser = await pw.chromium.launch({headless: true});
  const page = await browser.newPage({viewport: {width: 1100, height: 1100}});
  const root = path.resolve(__dirname, '../src/invoice_print_layout/web'), origin = 'http://127.0.0.1:18949';
  const evidence = process.env.INVOICE_BROWSER_EVIDENCE, errors = [], calls = [], cases = [];
  if (evidence) await fs.mkdir(evidence, {recursive: true});
  const qr = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScLbtAAAAABJRU5ErkJggg==';
  let app = {enabled: false, port: 8769, urls: [], devices: [], expires_in: 0, error: ''};
  let phone = {enabled: false, addresses: [{address: '192.168.1.8', label: '合成 Wi-Fi'}], remote: {installed: false, ready: false}};
  let failure = '', readFailure = false;
  page.on('pageerror', error => errors.push(error.message));
  await page.route(origin + '/**', async route => {
    const request = route.request(), url = new URL(request.url());
    const json = (body, status = 200) => route.fulfill({status, contentType: 'application/json', body: JSON.stringify(body)});
    if (url.pathname === '/') return route.fulfill({contentType: 'text/html; charset=utf-8', body: '<meta charset="utf-8"><header><div class="actions"></div></header><script>var state={token:"synthetic-device-token"};function esc(s){const d=document.createElement("div");d.textContent=String(s);return d.innerHTML.replaceAll(String.fromCharCode(34),"&quot;").replaceAll(String.fromCharCode(39),"&#39;");}</script><script src="/phone-link.js"></script><link rel="stylesheet" href="/phone-link.css">'});
    if (url.pathname === '/phone-link.js' || url.pathname === '/phone-link.css') return route.fulfill({contentType: url.pathname.endsWith('.js') ? 'text/javascript; charset=utf-8' : 'text/css; charset=utf-8', body: await fs.readFile(path.join(root, url.pathname.slice(1)), 'utf8')});
    assert.ok(url.pathname.startsWith('/api/phone-link'), url.pathname);
    const body = request.method() === 'POST' ? request.postDataJSON() : null;
    calls.push({path: url.pathname, body});
    if (!body) {
      if (readFailure) {readFailure = false; return json({error: '设备状态暂不可用'}, 503);}
      return json({...phone, receipt_app: app});
    }
    assert.equal(request.headers()['x-workbench-token'], 'synthetic-device-token');
    if (failure === url.pathname) {failure = ''; return json({error: '合成连接失败，请重试'}, 503);}
    if (url.pathname.endsWith('/devices/pair')) {
      assert.deepEqual(body, {});
      app = {...app, enabled: true, urls: ['http://192.168.1.8:8769', 'http://10.1.2.3:8769'], code: '01234567', qr, expires_in: 600};
      return json(app);
    }
    if (url.pathname.endsWith('/devices/revoke')) {
      assert.deepEqual(body, {device_id: 'synthetic-phone'});
      app = {...app, enabled: false, devices: app.devices.map(device => ({...device, revoked_at: '2026-11-01T12:40:00+08:00'}))};
      return json(app);
    }
    if (url.pathname.endsWith('/start')) {
      assert.deepEqual(body, {address: '192.168.1.8'});
      phone = {...phone, enabled: true, mode: 'lan', clients: 0, expires_in: 600, url: 'http://192.168.1.8:8767', pair_url: 'http://192.168.1.8:8767/mobile#synthetic-browser-pair', qr};
    } else if (url.pathname.endsWith('/rotate')) phone.pair_url = 'http://192.168.1.8:8767/mobile#synthetic-browser-rotated';
    else if (url.pathname.endsWith('/stop')) phone = {enabled: false, addresses: [{address: '192.168.1.8', label: '合成 Wi-Fi'}], remote: {installed: false, ready: false}};
    else assert.fail('Unexpected write: ' + url.pathname);
    return json({...phone, receipt_app: app});
  });
  const panel = () => page.locator('#phoneLinkDialog'), section = () => panel().getByRole('region', {name: '收票 App', exact: true});
  const appButton = name => section().getByRole('button', {name, exact: true});
  const screenshot = async name => {if (evidence) await page.screenshot({path: path.join(evidence, name + '.png')});};
  try {
    await page.goto(origin + '/'); await page.getByRole('button', {name: '手机连接', exact: true}).click();
    await section().getByText('尚未监听', {exact: true}).waitFor();
    await section().getByText('暂无已配对设备。', {exact: true}).waitFor();
    assert.equal(calls.filter(call => call.body).length, 0);
    assert.equal(await section().getByAltText('收票 App 配对二维码').count(), 0);
    assert.equal(await panel().getByRole('button', {name: '开启手机连接', exact: true}).isEnabled(), true);
    await screenshot('synthetic-app-closed'); cases.push('initial device listener closed without implicit writes; browser LAN controls remain available');

    failure = '/api/phone-link/devices/pair'; await appButton('配对新手机').click();
    await panel().getByRole('alert').filter({hasText: '合成连接失败，请重试'}).waitFor();
    assert.equal(await appButton('配对新手机').isEnabled(), true);
    await appButton('配对新手机').click();
    await section().getByText('正在监听 · 端口 8769', {exact: true}).waitFor();
    assert.equal(await section().getByLabel('收票 App 配对码', {exact: true}).inputValue(), '01234567');
    assert.equal(await section().getByAltText('收票 App 配对二维码').getAttribute('src'), qr);
    await section().getByText('在收票 App 中扫码，10 分钟内有效。', {exact: true}).waitFor();
    for (const url of app.urls) await section().getByText(url, {exact: true}).waitFor();
    assert.equal(await panel().getByLabel('连接方式', {exact: true}).inputValue(), 'lan');
    await panel().getByLabel('连接方式', {exact: true}).selectOption('remote');
    assert.equal(await section().getByLabel('收票 App 配对码', {exact: true}).inputValue(), '01234567');
    await panel().getByLabel('连接方式', {exact: true}).selectOption('lan');
    assert.equal(await section().getByLabel('收票 App 配对码', {exact: true}).inputValue(), '01234567');
    await screenshot('synthetic-app-pairing'); cases.push('recoverable pairing failure, leading-zero 8 digit code, QR, 10 minute expiry, listener and LAN addresses');

    app = {...app, qr: '', code: '', expires_in: 0, devices: [
      {id: 'synthetic-phone', name: '合成手机 <demo>', paired_at: '2026-11-01T12:00:00+08:00', last_seen_at: '2026-11-01T12:30:00+08:00', revoked_at: ''},
      {id: 'old-phone', name: '已撤销合成手机', paired_at: '2026-11-01T11:00:00+08:00', last_seen_at: '', revoked_at: '2026-11-01T11:30:00+08:00'}]};
    await appButton('刷新设备状态').click();
    await section().getByText('合成手机 <demo> · 配对：2026-11-01T12:00:00+08:00 · 最近连接：2026-11-01T12:30:00+08:00', {exact: false}).waitFor();
    assert.equal(await section().getByText('已撤销合成手机', {exact: false}).count(), 0);
    assert.equal(await section().locator('demo').count(), 0);
    assert.equal(await section().getByLabel('收票 App 配对码').count(), 0);
    readFailure = true; await appButton('刷新设备状态').click();
    await panel().getByRole('alert').filter({hasText: '设备状态暂不可用'}).waitFor();
    assert.equal(await appButton('刷新设备状态').isEnabled(), true);
    assert.equal(await section().getByRole('button', {name: '撤销 合成手机 <demo>', exact: true}).count(), 1);
    await appButton('刷新设备状态').click(); await screenshot('synthetic-app-device');
    await panel().getByRole('button', {name: '刷新连接状态', exact: true}).click();
    await section().getByRole('button', {name: '撤销 合成手机 <demo>', exact: true}).waitFor();
    cases.push('paired device name escaped, paired and last connection times displayed, revoked devices hidden, refresh failure preserves controls and list');

    failure = '/api/phone-link/devices/revoke'; await section().getByRole('button', {name: '撤销 合成手机 <demo>', exact: true}).click();
    await panel().getByRole('alert').filter({hasText: '合成连接失败，请重试'}).waitFor();
    assert.equal(await section().getByRole('button', {name: '撤销 合成手机 <demo>', exact: true}).isEnabled(), true);
    await section().getByRole('button', {name: '撤销 合成手机 <demo>', exact: true}).click();
    await section().getByText('尚未监听', {exact: true}).waitFor();
    await section().getByText('暂无已配对设备。', {exact: true}).waitFor();
    assert.equal(await section().locator('[data-app-revoke]').count(), 0);
    await panel().getByLabel('连接方式', {exact: true}).selectOption('remote');
    assert.equal(await section().locator('[data-app-revoke]').count(), 0);
    await panel().getByLabel('连接方式', {exact: true}).selectOption('lan');
    assert.equal(await section().locator('[data-app-revoke]').count(), 0);
    await section().getByText('暂无已配对设备。', {exact: true}).waitFor();
    cases.push('browser LAN/remote mode switches preserve current App pairing code and never restore a revoked device');
    cases.push('revoke failure can retry; last paired device disappears and listener closed state displayed');

    await panel().getByRole('button', {name: '开启手机连接', exact: true}).click();
    await panel().getByText('同一 Wi-Fi 连接已开启 · 已配对 0 台', {exact: true}).waitFor();
    assert.equal(await panel().getByLabel('手机配对链接', {exact: true}).inputValue(), phone.pair_url);
    await section().getByText('尚未监听', {exact: true}).waitFor();
    await panel().getByRole('button', {name: '更新二维码', exact: true}).click();
    await page.waitForFunction(() => document.querySelector('[data-link]').value.endsWith('#synthetic-browser-rotated'));
    await panel().getByRole('button', {name: '关闭手机连接', exact: true}).click();
    await panel().getByRole('button', {name: '开启手机连接', exact: true}).waitFor();
    await section().getByText('尚未监听', {exact: true}).waitFor();
    app = {...app, error: '8769 端口已被占用'};
    await appButton('刷新设备状态').click();
    await section().getByText('尚未监听 · 8769 端口已被占用', {exact: true}).waitFor();
    await screenshot('synthetic-app-listener-error');
    await panel().getByRole('button', {name: '完成', exact: true}).click(); await panel().waitFor({state: 'detached'});
    cases.push('existing browser start/QR rotation/stop still work independently; listener error visible');
    assert.deepEqual(errors, []);
    const result = {status: 'PASS', cases, requests: calls.length};
    if (evidence) await fs.writeFile(path.join(evidence, 'result.json'), JSON.stringify(result, null, 2));
    return result;
  } finally {await browser.close();}
}
module.exports = run;
if (require.main === module) run(require('playwright')).then(result => console.log(JSON.stringify(result))).catch(error => {console.error(error); process.exitCode = 1;});
