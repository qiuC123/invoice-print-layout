// Fixture failures must reject and stop only their own Python child.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {EventEmitter} = require('node:events');

async function check(mode) {
  let stopped = false;
  const child = new EventEmitter();
  child.stdout = new EventEmitter();
  child.stderr = new EventEmitter();
  child.exitCode = null;
  child.stdin = {end(value) {
    assert.equal(value, 'stop\n');
    stopped = true;
    child.exitCode = 0;
    child.emit('exit', 0);
  }};
  child.kill = () => {throw Error('cooperative shutdown should not require killing');};
  const load = name => name === 'node:child_process' ? {spawn() {
    if (mode !== 'timeout') queueMicrotask(() => child.stdout.emit('data', '{"base":"http://127.0.0.1:1"}\n'));
    return child;
  }} : require(name);
  const sandbox = {require: load, module: {exports: {}}, __dirname, process: {env: {}},
    setTimeout(callback, delay) {
      if (delay === 30000 && mode === 'timeout') {queueMicrotask(callback); return null;}
      return setTimeout(callback, delay);
    }, clearTimeout};
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, 'organize_nonblocking_browser.cjs'), 'utf8'), sandbox);
  const browser = {
    async newPage() {return {on() {}, async goto() {throw Error('page failed');}};},
    async close() {throw Error('close failed');}
  };
  await assert.rejects(sandbox.module.exports({chromium: {async launch() {
    if (mode === 'launch') throw Error('launch failed');
    return browser;
  }}}), mode === 'timeout' ? /fixture timeout/ : mode === 'launch' ? /launch failed/ : /close failed/);
  assert.equal(stopped, true, mode + ' must stop the fixture');
}

(async () => {
  for (const mode of ['timeout', 'launch', 'close']) await check(mode);
  console.log('Fixture cleanup contracts passed');
})().catch(error => {console.error(error); process.exitCode = 1;});
