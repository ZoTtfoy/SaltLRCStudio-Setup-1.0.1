"use strict";
/**
 * Load-time smoke for the front-end pages.
 *
 * Usage: node web_load_harness.js file1.js [file2.js ...]
 *
 * Evaluates the given page scripts (in load order) against a DOM stub and
 * fails on any synchronous ReferenceError / TypeError. This exists because the
 * word editor's buttons once silently died: library.js called `bind()`, which
 * was only defined in studio.js, so the script aborted mid-load and every
 * handler after that line never got attached. Loading the real scripts is the
 * only reliable way to catch that class of bug.
 */
const fs = require('fs');
const vm = require('vm');

const args = process.argv.slice(2);
const files = [];
let checkHandlers = [];
let runFile = null;
for (const arg of args) {
  if (arg.startsWith('--check-handlers=')) checkHandlers = arg.split('=')[1].split(',').filter(Boolean);
  else if (arg.startsWith('--run=')) runFile = arg.split('=').slice(1).join('=');
  else files.push(arg);
}
if (!files.length) {
  console.error('usage: node web_load_harness.js [--check-handlers=id1,id2] <page scripts in load order>');
  process.exit(2);
}

const asyncErrors = [];

function classList() {
  return {add() {}, remove() {}, toggle() {}, contains() { return false; }, replace() {}};
}

function canvasCtx() {
  return new Proxy({canvas: null}, {
    get(target, prop) {
      if (prop === 'measureText') return () => ({width: 10});
      if (prop === 'createLinearGradient' || prop === 'createRadialGradient') return () => ({addColorStop() {}});
      if (prop === 'getImageData') return () => ({data: new Uint8ClampedArray(4), width: 1, height: 1});
      return () => undefined;
    },
    set() { return true; },
  });
}

function matches(el, sel) {
  if (!el) return false;
  if (sel.startsWith('.')) return String(el.className || '').split(/\s+/).includes(sel.slice(1));
  return String(el.tagName || '').toLowerCase() === sel.toLowerCase();
}

function findChild(root, sel) {
  for (const child of root.children || []) {
    if (matches(child, sel)) return child;
    const deeper = findChild(child, sel);
    if (deeper) return deeper;
  }
  return null;
}

function makeEl(id) {
  const target = {
    id, children: [], dataset: {}, style: {}, classList: classList(),
    value: '', textContent: '', innerHTML: '', className: '', hidden: false, checked: false,
    disabled: false, open: false, title: '', href: '', download: '', type: '',
    tagName: 'DIV', nodeName: 'DIV', length: 0, selectedIndex: 0, selected: false,
    scrollTop: 0, scrollLeft: 0, scrollHeight: 400, offsetHeight: 400, offsetTop: 0,
    width: 800, height: 400, clientWidth: 800, clientHeight: 400, offsetWidth: 800,
    files: [], result: null, duration: 0, currentTime: 0, paused: true, ended: false,
    click() {}, focus() {}, blur() {}, select() {}, remove() {},
    insertAdjacentElement() {}, insertAdjacentHTML() {}, insertAdjacentText() {},
    after() {}, before() {}, replaceWith() {}, cloneNode() { return makeEl(id); },
    removeChild() {}, contains() { return false; }, matches() { return false; },
    animate() { return {cancel() {}, finish() {}}; },
    setPointerCapture() {}, releasePointerCapture() {},
    addEventListener() {}, removeEventListener() {}, dispatchEvent() { return true; },
    setAttribute() {}, getAttribute() { return null; }, removeAttribute() {},
    getBoundingClientRect() { return {left: 0, top: 0, right: 800, bottom: 400, width: 800, height: 400}; },
    scrollIntoView() {}, showModal() {}, close() {},
    getContext() { return canvasCtx(); },
    pause() {}, load() {}, play() { return Promise.resolve(); },
  };
  // Real child bookkeeping so behaviour checks can walk what a page built.
  const push = (nodes, atStart) => {
    for (const node of nodes) {
      if (node && typeof node === 'object') {
        if (atStart) target.children.unshift(node); else target.children.push(node);
      }
    }
  };
  target.append = (...nodes) => push(nodes, false);
  target.appendChild = (node) => { push([node], false); return node; };
  target.prepend = (...nodes) => push(nodes, true);
  target.replaceChildren = (...nodes) => { target.children.length = 0; push(nodes, false); };
  target.querySelector = (sel) => findChild(target, sel) ?? makeEl('(query:' + sel + ')');
  target.querySelectorAll = (sel) => {
    const found = [];
    const collect = (node) => {
      for (const child of node.children || []) {
        if (matches(child, sel)) found.push(child);
        collect(child);
      }
    };
    collect(target);
    return found;
  };
  const noops = new Map();
  return new Proxy(target, {
    get(t, prop) {
      if (prop in t) return t[prop];
      if (prop === 'parentElement' || prop === 'parentNode') return makeEl('(parent)');
      if (prop === 'firstChild' || prop === 'lastChild' || prop === 'nextSibling' ||
          prop === 'previousSibling' || prop === 'nextElementSibling') return null;
      if (typeof prop === 'symbol') return undefined;
      // Any other method the page calls on a real element: accept and ignore.
      if (!noops.has(prop)) noops.set(prop, () => undefined);
      return noops.get(prop);
    },
    set(t, prop, value) { t[prop] = value; return true; },
  });
}

const elements = new Map();
const documentStub = {
  getElementById(id) {
    if (!elements.has(id)) elements.set(id, makeEl(id));
    return elements.get(id);
  },
  createElement(tag) { return makeEl('<' + tag + '>'); },
  createTextNode(text) { return {textContent: String(text)}; },
  querySelector(sel) { return makeEl('(query:' + sel + ')'); },
  querySelectorAll() { return []; },
  addEventListener() {}, removeEventListener() {},
  body: makeEl('body'), documentElement: makeEl('html'), head: makeEl('head'),
  activeElement: null, title: '', visibilityState: 'visible', hidden: false,
};

const storage = () => {
  const map = new Map();
  return {
    getItem: (key) => (map.has(key) ? map.get(key) : null),
    setItem: (key, value) => map.set(key, String(value)),
    removeItem: (key) => map.delete(key),
    clear: () => map.clear(),
    key: (index) => [...map.keys()][index] ?? null,
    get length() { return map.size; },
  };
};

const fetchStub = async () => ({
  ok: true, status: 200,
  json: async () => ({}),
  blob: async () => ({}),
  text: async () => '',
});

const windowStub = {
  addEventListener() {}, removeEventListener() {},
  // trace.js wraps `window.fetch`, so the stub has to be reachable through
  // `window` too — a global `fetch` alone is not what the page reads.
  fetch: fetchStub,
  matchMedia() { return {matches: false, addEventListener() {}, addListener() {}}; },
  requestAnimationFrame(cb) { return setTimeout(() => cb(performance.now()), 0); },
  cancelAnimationFrame(id) { clearTimeout(id); },
  location: {href: 'http://127.0.0.1:8765/studio.html', pathname: '/studio.html', search: '', hash: '',
             origin: 'http://127.0.0.1:8765', host: '127.0.0.1:8765'},
  devicePixelRatio: 1, innerWidth: 1200, innerHeight: 800, scrollY: 0,
  // `display: ''` means "this engine has no opinion", which is what trace.js
  // treats as "not an anomaly". A real value here would make every closed
  // dialog look like a rendering fault.
  getComputedStyle() { return {getPropertyValue: () => '', fontSize: '14px', display: ''}; },
  open() { return null; },
  history: {pushState() {}, replaceState() {}},
  isSecureContext: true,
  showDirectoryPicker: undefined,
};

Object.assign(globalThis, {
  window: windowStub,
  document: documentStub,
  location: windowStub.location,
  localStorage: storage(), sessionStorage: storage(),
  fetch: fetchStub,
  confirm: () => true, alert() {}, prompt() { return null; },
  ResizeObserver: class { observe() {} unobserve() {} disconnect() {} },
  IntersectionObserver: class { observe() {} unobserve() {} disconnect() {} },
  MutationObserver: class { observe() {} disconnect() {} takeRecords() { return []; } },
  Audio: class { constructor() { Object.assign(this, {pause() {}, load() {}, play() { return Promise.resolve(); }}); } },
  FileReader: class {
    readAsArrayBuffer() { if (this.onload) this.onload({target: {result: new ArrayBuffer(0)}}); }
    readAsDataURL() { if (this.onload) this.onload({target: {result: 'data:'}}); }
  },
  CustomEvent: class { constructor(type) { this.type = type; } },
  Event: class { constructor(type) { this.type = type; } },
  requestAnimationFrame: windowStub.requestAnimationFrame,
  cancelAnimationFrame: windowStub.cancelAnimationFrame,
  matchMedia: windowStub.matchMedia,
  getComputedStyle: windowStub.getComputedStyle,
});
// navigator is getter-only on globalThis in modern Node.
Object.defineProperty(globalThis, 'navigator', {
  value: {userAgent: 'node-stub', platform: 'test', clipboard: {writeText: async () => {}}},
  configurable: true,
});

process.on('unhandledRejection', (error) => {
  const name = error && error.constructor ? error.constructor.name : 'Error';
  if (name === 'ReferenceError' || name === 'TypeError') asyncErrors.push(error);
});

let failed = false;
for (const file of files) {
  let source;
  try {
    source = fs.readFileSync(file, 'utf8');
  } catch (error) {
    console.error('FAIL cannot read ' + file + ': ' + error.message);
    process.exit(1);
  }
  try {
    // Evaluate as a classic script in the shared global context — exactly how
    // <script src> works: top-level functions become globals that later files
    // (and later scripts) can call.
    vm.runInThisContext(source, {filename: file});
    console.log('loaded ' + file);
  } catch (error) {
    failed = true;
    console.error('FAIL ' + file + ' -> ' + error.constructor.name + ': ' + error.message);
    const frames = String(error.stack || '').split('\n').slice(1, 5);
    for (const frame of frames) console.error('   ' + frame.trim());
  }
}

// Optional behaviour check: runs in the same context and may leave an async
// assertion function in globalThis.__check.
if (runFile) {
  try {
    vm.runInThisContext(fs.readFileSync(runFile, 'utf8'), {filename: runFile});
    console.log('ran ' + runFile);
  } catch (error) {
    failed = true;
    console.error('FAIL ' + runFile + ' -> ' + error.constructor.name + ': ' + error.message);
    const frames = String(error.stack || '').split('\n').slice(1, 5);
    for (const frame of frames) console.error('   ' + frame.trim());
  }
}

setTimeout(async () => {
  if (asyncErrors.length) {
    failed = true;
    for (const error of asyncErrors) console.error('FAIL async ' + error.constructor.name + ': ' + error.message);
  }
  if (typeof globalThis.__check === 'function') {
    try {
      await globalThis.__check();
      console.log('behaviour check ok');
    } catch (error) {
      failed = true;
      console.error('FAIL behaviour: ' + error.message);
    }
  }
  // A handler that never got attached is exactly what made the word editor's
  // buttons dead, so assert it explicitly instead of only trusting "it loaded".
  for (const id of checkHandlers) {
    const element = elements.get(id);
    if (!element || typeof element.onclick !== 'function') {
      failed = true;
      console.error('FAIL #' + id + ' 没有绑定 onclick（按钮会点了没反应）');
    } else {
      console.log('handler ok #' + id);
    }
  }
  if (failed) process.exit(1);
  console.log('OK ' + files.length + ' script(s) loaded clean');
  process.exit(0);
}, 120);
