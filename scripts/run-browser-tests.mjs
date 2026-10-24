// Run every browser/renderer contract in an isolated Node process, in order.
import {spawn} from 'node:child_process';
import {readdir, mkdir, writeFile} from 'node:fs/promises';
import {existsSync} from 'node:fs';
import {createRequire} from 'node:module';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const files = (await readdir(path.join(root, 'tests'))).filter(name => name.endsWith('.cjs')).sort();
const args = process.argv.slice(2);
if (args.includes('--list')) {
  console.log(files.join('\n'));
} else {
  const require = createRequire(import.meta.url);
  try { require.resolve('playwright'); }
  catch (_) { console.error('Playwright 未找到。请安装本地开发依赖，或将其 node_modules 绝对路径加入 NODE_PATH。'); process.exit(2); }
  const outputArg = args.find(value => value.startsWith('--output='));
  const output = path.resolve(root, outputArg ? outputArg.slice(9) : 'workspace/browser-tests/' + Date.now());
  const timeout = Number(process.env.BROWSER_TEST_TIMEOUT_MS || 180000);
  if (!Number.isFinite(timeout) || timeout <= 0) throw Error('BROWSER_TEST_TIMEOUT_MS must be positive');
  await mkdir(output, {recursive: true});
  const python = process.env.PHOTO_TEST_PYTHON || ['.venv/Scripts/python.exe', '.venv/bin/python']
    .map(file => path.join(root, file)).find(existsSync) || 'python';
  const results = [], started = performance.now();
  for (const name of files) {
    const start = performance.now();
    const result = await new Promise(resolve => {
      const child = spawn(process.execPath, [path.join(root, 'tests', name)], {cwd: root, windowsHide: true,
        env: {...process.env, PHOTO_TEST_PYTHON: python, PYTHONUTF8: '1',
          INVOICE_BROWSER_EVIDENCE: path.join(output, 'evidence', name.replace(/\.cjs$/, ''))},
        stdio: ['ignore', 'pipe', 'pipe']});
      let stdout = '', stderr = '', timedOut = false;
      const timer = setTimeout(() => {timedOut = true; child.kill();}, timeout);
      child.stdout.on('data', value => { stdout += value; });
      child.stderr.on('data', value => { stderr += value; });
      child.once('error', error => { stderr += error.message; });
      child.once('close', code => {clearTimeout(timer); resolve({name, passed: code === 0 && !timedOut,
        code, timedOut, seconds: (performance.now() - start) / 1000, stdout, stderr});});
    });
    await writeFile(path.join(output, name + '.log'), result.stdout + result.stderr);
    const {stdout, stderr, ...summary} = result;
    results.push(summary);
    console.log(`${result.passed ? 'PASS' : 'FAIL'} ${name} (${result.seconds.toFixed(2)}s)`);
    if (!result.passed) console.log((stderr || stdout).trim().split(/\r?\n/).slice(-12).join('\n'));
    await writeFile(path.join(output, 'results.json'), JSON.stringify({results, complete: false}, null, 2));
  }
  const passed = results.filter(result => result.passed).length;
  const summary = {complete: true, total: files.length, passed, failed: files.length - passed,
    seconds: (performance.now() - started) / 1000, results};
  await writeFile(path.join(output, 'results.json'), JSON.stringify(summary, null, 2));
  console.log(`Browser tests: ${passed} passed, ${summary.failed} failed, ${summary.total} total in ${summary.seconds.toFixed(2)}s`);
  console.log(`Results: ${path.join(output, 'results.json')}`);
  process.exitCode = summary.failed ? 1 : 0;
}
