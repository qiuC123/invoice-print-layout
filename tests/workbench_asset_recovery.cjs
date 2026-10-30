// A deterministic resource/event clock for the actual first-paint bootstrap.
// It never loads the application, calls a network API, or opens a real ledger.
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, '../src/invoice_print_layout/web/index.html'), 'utf8');
const bootstrapMatch = html.match(/<script\b[^>]*\bid=["']workbenchAssetBootstrap["'][^>]*>([\s\S]*?)<\/script>/i);
assert.ok(bootstrapMatch, 'the first-paint recovery bootstrap must ship in index.html');
assert.ok(bootstrapMatch.index < html.indexOf('src="/workbench-theme.js"'), 'theme bootstrap runs before external theme JavaScript');
assert.ok(bootstrapMatch.index < html.indexOf('rel="stylesheet"'), 'resource listeners are installed before stylesheet parsing');
const specs = [...html.matchAll(/<link\b([^>]+)>/gi)].map(match => {
  const attrs = Object.fromEntries([...match[1].matchAll(/([\w-]+)=["']([^"']*)["']/g)].map(attr => [attr[1], attr[2]]));
  return attrs;
}).filter(attrs => attrs.rel === 'stylesheet');
assert.ok(specs.length >= 5, 'the real workbench cascade is under test');

function harness(options = {}) {
  let now = 0, nextTimer = 1;
  const timers = new Map(), observers = [], requests = [], storageWrites = [], forbidden = [];
  const origin = 'http://127.0.0.1:8765', pageUrl = origin + '/?project=synthetic';
  const storage = new Map(options.saved == null ? [] : [['invoice-workbench-theme', options.saved]]);
  class Target {
    constructor() { this.listeners = new Map(); }
    addEventListener(type, fn, config = {}) {
      const list = this.listeners.get(type) || [];
      list.push({fn, once: Boolean(config && config.once)}); this.listeners.set(type, list);
    }
    removeEventListener(type, fn) {
      this.listeners.set(type, (this.listeners.get(type) || []).filter(item => item.fn !== fn));
    }
    invoke(event) {
      for (const item of [...(this.listeners.get(event.type) || [])]) {
        if (item.once) this.removeEventListener(event.type, item.fn);
        item.fn.call(this, event);
      }
    }
    dispatchEvent(event) {
      if (!event.target) event.target = this;
      event.preventDefault ||= () => { event.defaultPrevented = true; };
      event.stopPropagation ||= () => {};
      if (this !== document && this !== window) document.invoke(event);
      this.invoke(event);
      if (this !== window && event.bubbles) window.invoke(event);
      return !event.defaultPrevented;
    }
  }
  class Element extends Target {
    constructor(tagName) {
      super(); this.tagName = tagName.toUpperCase(); this.nodeName = this.tagName; this.nodeType = 1;
      this.children = []; this.dataset = {}; this.style = {}; this.attrs = {}; this.hidden = false;
      this.parentNode = null; this.textContent = ''; this.value = ''; this.sheet = null;
      this.classList = {contains: value => (this.className || '').split(/\s+/).includes(value),
        add: value => { this.className = ((this.className || '') + ' ' + value).trim(); },
        remove: value => { this.className = (this.className || '').split(/\s+/).filter(item => item !== value).join(' '); }};
    }
    get id() { return this.attrs.id || ''; }
    set id(value) { this.attrs.id = value; }
    get rel() { return this.attrs.rel || ''; }
    set rel(value) { this.attrs.rel = value; }
    get href() { return this.attrs.href ? new URL(this.attrs.href, pageUrl).href : ''; }
    set href(value) { this.attrs.href = String(value); if (this.parentNode && this.rel === 'stylesheet') requests.push({node: this, href: this.href, time: now}); }
    get src() { return this.attrs.src ? new URL(this.attrs.src, pageUrl).href : ''; }
    set src(value) { this.attrs.src = String(value); forbidden.push('script-src'); }
    get isConnected() { return this === document.documentElement || Boolean(this.parentNode && this.parentNode.isConnected); }
    get parentElement() { return this.parentNode; }
    setAttribute(name, value) {
      if (name === 'href') { this.href = value; return; }
      this.attrs[name] = String(value);
      if (name.startsWith('data-')) this.dataset[name.slice(5).replace(/-([a-z])/g, (_, char) => char.toUpperCase())] = String(value);
    }
    getAttribute(name) {
      if (name.startsWith('data-')) return this.dataset[name.slice(5).replace(/-([a-z])/g, (_, char) => char.toUpperCase())] ?? null;
      return this.attrs[name] ?? null;
    }
    hasAttribute(name) { return this.getAttribute(name) !== null; }
    removeAttribute(name) { delete this.attrs[name]; }
    appendChild(child) {
      if (child.parentNode) child.parentNode.removeChild(child);
      this.children.push(child); child.parentNode = this;
      if (child.tagName === 'LINK' && child.rel === 'stylesheet') requests.push({node: child, href: child.href, time: now});
      if (child.tagName === 'SCRIPT') forbidden.push('new-script');
      for (const observer of observers) if (observer.active) observer.callback([{type: 'childList', target: this, addedNodes: [child], removedNodes: []}]);
      return child;
    }
    append(...children) { for (const child of children) this.appendChild(typeof child === 'string' ? Object.assign(new Element('span'), {textContent: child}) : child); }
    removeChild(child) { this.children.splice(this.children.indexOf(child), 1); child.parentNode = null; return child; }
    insertBefore(child, before) {
      if (child.parentNode) child.parentNode.removeChild(child);
      const index = this.children.indexOf(before); this.children.splice(index < 0 ? this.children.length : index, 0, child); child.parentNode = this;
      if (child.tagName === 'LINK') forbidden.push('link-reordered');
      return child;
    }
    matches(selector) {
      return selector.split(',').some(raw => {
        const part = raw.trim();
        if (part.startsWith('#')) return this.id === part.slice(1);
        if (part.startsWith('.')) return this.classList.contains(part.slice(1));
        const tag = part.match(/^[\w-]+/); if (tag && tag[0].toUpperCase() !== this.tagName) return false;
        const constraints = [...part.matchAll(/\[([\w-]+)(?:=["']?([^\]"']+)["']?)?\]/g)];
        return (tag || constraints.length) && constraints.every(([, name, value]) => value === undefined ? this.hasAttribute(name) : this.getAttribute(name) === value);
      });
    }
    closest(selector) { for (let node = this; node; node = node.parentNode) if (node.matches(selector)) return node; return null; }
    querySelectorAll(selector) {
      const found = []; for (const child of this.children) { if (child.matches(selector)) found.push(child); found.push(...child.querySelectorAll(selector)); } return found;
    }
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
    click() { this.dispatchEvent({type: 'click', target: this, bubbles: true}); }
  }
  const document = new Target(), window = new Target();
  // Keep these synchronous CSS contracts hidden; the real-browser suite covers runtime recovery.
  Object.assign(document, {hidden: true, readyState: 'loading', URL: pageUrl, baseURI: pageUrl,
    createElement: tag => new Element(tag), createTextNode: text => Object.assign(new Element('span'), {textContent: text})});
  document.documentElement = new Element('html'); document.documentElement.className = 'invoice-assistant';
  document.head = document.documentElement.appendChild(new Element('head'));
  document.body = null;
  document.querySelectorAll = selector => document.documentElement.querySelectorAll(selector);
  document.querySelector = selector => document.documentElement.querySelector(selector);
  document.getElementById = id => document.querySelector('#' + id);
  document.getElementsByTagName = tag => document.querySelectorAll(tag);
  const schedule = (fn, delay = 0) => { const id = nextTimer++; timers.set(id, {at: now + Number(delay), fn}); return id; };
  let reloads = 0;
  const location = new URL(pageUrl); location.reload = () => reloads++;
  const localStorage = {
    getItem(key) { if (options.storageDenied) throw new Error('storage denied'); return storage.get(key) ?? null; },
    setItem(key, value) { if (options.storageDenied) throw new Error('storage denied'); storage.set(key, String(value)); storageWrites.push([key, String(value)]); },
  };
  const context = {document, window, location, localStorage, URL, console, setTimeout: schedule,
    clearTimeout: id => timers.delete(id), Element, HTMLElement: Element, HTMLLinkElement: Element,
    MutationObserver: class { constructor(callback) { this.callback = callback; this.active = false; observers.push(this); } observe() { this.active = true; } disconnect() { this.active = false; } },
    fetch: () => { forbidden.push('fetch'); throw new Error('bootstrap must not fetch business data'); },
    XMLHttpRequest: class { constructor() { forbidden.push('xhr'); throw new Error('bootstrap must not use XHR'); } },
    Event: class { constructor(type, opts = {}) { this.type = type; Object.assign(this, opts); } },
  };
  Object.assign(window, {document, location, localStorage, setTimeout: schedule, clearTimeout: context.clearTimeout});
  vm.runInNewContext(bootstrapMatch[1], context, {filename: 'index.html#workbenchAssetBootstrap', timeout: 1000});
  const links = specs.map(attrs => { const link = new Element('link'); Object.assign(link.attrs, attrs); document.head.appendChild(link); return link; });
  if (options.crossOrigin) { const link = new Element('link'); link.rel = 'stylesheet'; link.attrs.href = 'https://other.example/foreign.css'; document.head.appendChild(link); links.push(link); }
  const originalOrder = [...document.head.children];
  const select = new Element('select'); select.id = 'themeSelect';
  function domReady() {
    if (!document.body) { document.body = document.documentElement.appendChild(new Element('body')); document.body.appendChild(select); }
    document.readyState = 'interactive'; document.dispatchEvent({type: 'DOMContentLoaded'});
  }
  function windowLoad() { document.readyState = 'complete'; window.dispatchEvent({type: 'load'}); }
  function emit(link, type) { if (type === 'load') link.sheet = {cssRules: [{cssText: 'body {}'}]}; else link.sheet = null; link.dispatchEvent({type, target: link}); }
  function advance(milliseconds) {
    const target = now + milliseconds; let guard = 0;
    while (true) {
      const due = [...timers].filter(([, task]) => task.at <= target).sort((a, b) => a[1].at - b[1].at || a[0] - b[0])[0];
      if (!due) break; assert.ok(++guard < 100, 'timer recovery is bounded');
      timers.delete(due[0]); now = due[1].at; due[1].fn();
    }
    now = target;
  }
  const loads = except => { for (const link of links) if (link !== except && new URL(link.href).origin === origin) emit(link, 'load'); };
  const count = link => requests.filter(item => item.node === link).length;
  const noBusinessEffects = () => { assert.deepEqual(forbidden, []); assert.deepEqual(document.head.children, originalOrder, 'existing cascade node order is retained'); };
  return {document, window, links, select, requests, storageWrites, storage, timers, domReady, windowLoad, emit, advance, loads, count, noBusinessEffects, reloads: () => reloads};
}

const cases = {};
cases['single-failure'] = () => {
  const h = harness(), bad = h.links[0]; h.loads(bad); h.emit(bad, 'error'); h.domReady(); h.windowLoad();
  h.advance(1000); assert.equal(h.count(bad), 2, 'one original request and one recovery request');
  assert.match(bad.href, /[?&]wb_style_retry=/); h.emit(bad, 'load'); h.advance(20000);
  assert.equal(h.count(bad), 2); assert.equal(h.document.documentElement.dataset.workbenchAssets, 'ready'); h.noBusinessEffects();
};
cases['permanent-failure'] = () => {
  const h = harness(), bad = h.links[0]; h.loads(bad); h.domReady(); h.windowLoad();
  for (let step = 0; step < 8; step++) { h.emit(bad, 'error'); h.advance(10000); }
  assert.equal(h.count(bad), 3, 'automatic recovery has at most two retries');
  assert.equal(h.document.documentElement.dataset.workbenchAssets, 'recovering');
  const status = h.document.getElementById('workbenchAssetStatus'); assert.ok(status && !status.hidden, 'permanent failures are visible');
  assert.ok(status.querySelectorAll('button').some(button => button.textContent === '立即重试')); h.noBusinessEffects();
  assert.equal(h.reloads(), 0, 'hidden page must not automatically reload');
};
cases['delayed-success'] = () => {
  const h = harness(), delayed = h.links[0]; h.loads(delayed); h.domReady(); h.advance(4000); h.emit(delayed, 'load'); h.windowLoad(); h.advance(30000);
  assert.equal(h.count(delayed), 1, 'slow but successful CSS is not redownloaded');
  assert.equal(h.document.documentElement.dataset.workbenchAssets, 'ready'); h.noBusinessEffects();
};
cases['cascade-order'] = () => {
  const h = harness(), a = h.links[0], b = h.links[4];
  for (const link of h.links) if (link !== a && link !== b) h.emit(link, 'load');
  h.emit(b, 'error'); h.emit(a, 'error'); h.domReady(); h.windowLoad(); h.advance(1000);
  h.emit(a, 'load'); h.emit(b, 'load'); h.advance(20000);
  assert.equal(h.links.length, specs.length); assert.equal(h.count(a), 2); assert.equal(h.count(b), 2); h.noBusinessEffects();
};
cases['manual-recovery'] = () => {
  const h = harness(), bad = h.links[0]; h.loads(bad); h.domReady(); h.windowLoad();
  for (let step = 0; step < 3; step++) { h.emit(bad, 'error'); h.advance(10000); }
  const status = h.document.getElementById('workbenchAssetStatus'); assert.ok(status);
  const button = status.querySelectorAll('button').find(node => node.textContent === '立即重试'); assert.ok(button);
  button.click(); h.advance(1000); assert.equal(h.reloads(), 1, 'explicit recovery reloads the entire page');
  assert.equal(h.count(bad), 3); for (const good of h.links.slice(1)) assert.equal(h.count(good), 1); h.noBusinessEffects();
};
cases['cross-origin'] = () => {
  const h = harness({crossOrigin: true}), foreign = h.links.at(-1); h.loads(); h.domReady(); h.windowLoad(); h.emit(foreign, 'error'); h.advance(30000);
  assert.equal(h.count(foreign), 1, 'foreign styles must not be rewritten or retried'); h.noBusinessEffects();
};
cases['storage-denied'] = () => {
  const h = harness({storageDenied: true}); assert.equal(h.document.documentElement.dataset.theme, 'light');
  h.loads(); h.domReady(); h.windowLoad(); h.select.value = 'dark'; h.select.dispatchEvent({type: 'change', bubbles: true});
  assert.equal(h.document.documentElement.dataset.theme, 'dark', 'storage refusal must not break the page preference'); h.noBusinessEffects();
};
cases['theme-fallback'] = () => {
  const h = harness({saved: 'dark'}); assert.equal(h.document.documentElement.dataset.theme, 'dark', 'saved mode applies before CSS');
  h.loads(); h.domReady(); h.windowLoad(); assert.equal(h.select.value, 'dark');
  h.select.value = 'light'; h.select.dispatchEvent({type: 'change', bubbles: true});
  assert.equal(h.document.documentElement.dataset.theme, 'light'); assert.equal(h.document.documentElement.style.colorScheme, 'light');
  assert.deepEqual(h.storageWrites, [['invoice-workbench-theme', 'light']]); h.noBusinessEffects();
};
cases['theme-already-bound'] = () => {
  const h = harness(); h.select.dataset.workbenchThemeBound = 'true'; h.select.value = 'dark'; h.loads(); h.domReady(); h.windowLoad();
  h.select.value = 'dark'; h.select.dispatchEvent({type: 'change', bubbles: true});
  assert.deepEqual(h.storageWrites, [], 'external theme handler is the sole owner when already bound'); h.noBusinessEffects();
};
cases['theme-invalid'] = () => {
  const h = harness({saved: 'system'}); assert.equal(h.document.documentElement.dataset.theme, 'light');
  h.loads(); h.domReady(); h.windowLoad(); h.select.value = 'unexpected'; h.select.dispatchEvent({type: 'change', bubbles: true});
  assert.equal(h.document.documentElement.dataset.theme, 'light'); assert.deepEqual(h.storageWrites, []); h.noBusinessEffects();
};
cases['timeout-recovery'] = () => {
  const h = harness(), stalled = h.links[0]; h.loads(stalled); h.domReady(); h.advance(5200);
  assert.equal(h.count(stalled), 2, 'a stalled request recovers without requiring a window load event');
  h.emit(stalled, 'load'); h.windowLoad(); h.advance(30000); assert.equal(h.count(stalled), 2);
  assert.equal(h.document.documentElement.dataset.workbenchAssets, 'ready'); h.noBusinessEffects();
};
const selected = process.argv[2] ? [process.argv[2]] : Object.keys(cases);
for (const name of selected) { assert.ok(cases[name], 'unknown recovery scenario: ' + name); cases[name](); console.log('PASS ' + name); }
