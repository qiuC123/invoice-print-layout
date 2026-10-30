// Synthetic real-server timings, including Edge rendering and overlapping requests.
const {spawn} = require('node:child_process');
const {once} = require('node:events');
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const {chromium} = require('playwright');
async function main() {
  const output = process.argv[2];
  if (!output) throw Error('Provide output JSON path');
  const server = spawn(process.env.PHOTO_TEST_PYTHON || 'python', ['scripts/benchmark-desk.py', ...process.argv.slice(3)],
    {windowsHide: true, env: {...process.env, PYTHONUTF8: '1'}, stdio: ['ignore', 'pipe', 'pipe']});
  let errors = ''; server.stderr.on('data', data => errors += data);
  let browser, fixture;
  try {
    fixture = await new Promise((resolve, reject) => {
      let text = ''; server.stdout.on('data', data => {text += data; if (text.includes('\n')) resolve(JSON.parse(text.split('\n')[0]));});
      server.on('exit', code => reject(Error(`Fixture exited ${code}: ${errors}`)));
    });
    browser = await chromium.launch({channel: 'msedge', headless: true});
    const result = {fixture, browser: await browser.version(), endpoints: {}, pages: []};
    for (const endpoint of ['state', 'material-matching', 'reimbursement-people']) {
      const samples = [];
      for (let i = 0; i < 6; i++) {
        const start = performance.now(), response = await fetch(`${fixture.base}/api/${endpoint}?project=${fixture.project}`);
        if (!response.ok) throw Error(await response.text());
        await response.json(); samples.push(performance.now() - start);
      }
      result.endpoints[endpoint] = {cold_ms: samples[0], samples_ms: samples.slice(1)};
    }
    for (let i = 0; i < 3; i++) {
      const page = await browser.newPage({viewport: {width: 1500, height: 1000}});
      const pageErrors = []; page.on('pageerror', e => pageErrors.push(e.message));
      let start = performance.now();
      await page.goto(`${fixture.base}/?project=${fixture.project}`);
      await page.locator('#list [data-item]').first().waitFor();
      const workbench_ms = performance.now() - start;
      start = performance.now(); await page.locator('#materialMatching').click();
      await page.waitForFunction(() => {const button = document.querySelector('#matchingUpload'); return button && !button.disabled;});
      const matching_ms = performance.now() - start;
      result.pages.push({workbench_ms, matching_ms, errors: pageErrors});
      if (pageErrors.length) throw Error(pageErrors.join('\n'));
      await page.close();
    }
    await fs.mkdir(path.dirname(output), {recursive: true});
    await fs.writeFile(output, JSON.stringify(result, null, 2));
    console.log(JSON.stringify(result, null, 2));
  } finally {
    if (browser) await browser.close();
    if (server.exitCode === null) {server.kill(); await once(server, 'exit');}
    if (fixture?.workspace) {
      const directory = path.resolve(fixture.workspace);
      if (path.dirname(directory).toLowerCase() !== path.resolve(os.tmpdir()).toLowerCase() ||
          !path.basename(directory).startsWith('f18-synthetic-')) throw Error('Unexpected synthetic directory');
      await fs.rm(directory, {recursive: true});
    }
  }
}
main().catch(e => {console.error(e); process.exitCode = 1;});
