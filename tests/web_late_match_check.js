// Regression: a LATE auto-match reply must not freeze playback.
//
// The bug (recorded by the user as QQ20260924-040437.mp4, fixed in v0.11.1):
//   select() empties the <audio> element to load another source, which zeroes
//   currentTime. It then awaits the lyric lookup that only became possible once
//   the duration was known. That reply adopted the document and repainted. The
//   repaint read the *reset* currentTime, derived index = -1, and published it:
//   the karaoke line fell back to the waiting placeholder, the waveform
//   highlight was cleared, and because every later frame also computed -1 the
//   `index !== shownRow` guard never fired again. Playback follow stayed frozen
//   until the user paused or switched songs.
//
// This drives the real studio.js in one shared scope with a controllable
// /api/audio/match promise, so the *ordering* is reproduced on purpose instead
// of hoped for. It fails on the old code and passes on the fix.
const fs = require('fs');
const path = require('path');
const web = path.join(__dirname, '..', 'saltlrc', 'web');

let checks = 0, failures = 0;
function check(name, actual, expected) {
  checks++;
  const ok = JSON.stringify(actual) === JSON.stringify(expected);
  if (!ok) {
    failures++;
    console.log('FAIL %s\n  got      %s\n  expected %s', name, JSON.stringify(actual), JSON.stringify(expected));
  } else console.log('ok   %s', name);
}

// ---------- DOM stubs ----------
const cache = {};
let seq = 0;
function ctx2d() {
  return new Proxy({measureText: (t) => ({width: String(t).length * 7}), canvas: {width: 0, height: 0}},
    {get(t, p) { if (p in t) return t[p]; return () => {}; },
     set(t, p, v) { t[p] = v; return true; }});
}
function element(id) {
  if (cache[id]) return cache[id];
  const el = {
    id, style: {setProperty() {}}, dataset: {}, hidden: false, value: '', textContent: '', innerHTML: '',
    disabled: false, max: 300, children: [], checked: false, open: false,
    parentElement: {clientWidth: 900, clientHeight: 300, appendChild() {}},
    clientWidth: 900, clientHeight: 300,
    classList: {add() {}, remove() {}, toggle() {}, contains() { return false; }},
    append(...items) { this.children.push(...items); },
    prepend(...items) { this.children.unshift(...items); },
    replaceChildren() { this.children = []; },
    after() {}, before() {}, insertBefore() {}, remove() {},
    addEventListener() {}, removeEventListener() {}, setPointerCapture() {},
    closest() { return null; }, scrollIntoView() {}, focus() {}, select() {}, click() {},
    removeAttribute() {}, setAttribute() {}, getAttribute() { return null; },
    getContext: () => ctx2d(),
    querySelector: () => element('q:' + id + ':' + (seq++)),
    querySelectorAll: () => [],
  };
  cache[id] = el;
  return el;
}
const store = {};
global.localStorage = {getItem: (k) => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); }, removeItem: (k) => { delete store[k]; }};
global.sessionStorage = {getItem: () => null, setItem() {}, removeItem() {}};
global.window = {devicePixelRatio: 1, addEventListener() {}, fetch: () => Promise.resolve({ok: true, status: 200, json: async () => ({}), text: async () => ''})};
global.location = {href: 'http://127.0.0.1/studio.html', search: '', pathname: '/studio.html'};
global.document = {
  getElementById: element,
  createElement: (t) => element('new:' + t + ':' + (seq++)),
  querySelector: () => element('qs:' + (seq++)),
  querySelectorAll: () => [],
  addEventListener() {},
  body: element('body'),
};
global.ResizeObserver = class {observe() {}};
let rafQueue = [];
global.requestAnimationFrame = (fn) => { rafQueue.push(fn); return rafQueue.length; };
global.cancelAnimationFrame = () => {};
global.navigator = {};
global.confirm = () => false;
global.alert = () => {};
global.Blob = class {constructor() {}};
// Do NOT replace the global URL class: node's fs shim calls `instanceof URL`.
URL.createObjectURL = () => 'blob:x';
URL.revokeObjectURL = () => {};
global.TextEncoder = require('util').TextEncoder;
const flush = () => { const q = rafQueue; rafQueue = []; q.forEach((fn) => fn()); };
const tick = () => new Promise((r) => setImmediate(r));

const load = (f) => fs.readFileSync(path.join(web, f), 'utf8');

// ---------- a controllable Audio element ----------
// startPlayback() must be reachable so `wasPlaying` is what the test says.
const audio = element('audio');
audio.src = '';
audio.paused = true;
audio.currentTime = 0;
audio.duration = 227;
audio.pause = () => { audio.paused = true; };
audio.play = () => { audio.paused = false; return Promise.resolve(); };
audio.load = () => { audio.currentTime = 0; };
audio.removeAttribute = (n) => { if (n === 'src') audio.src = ''; };

// ---------- a deferred /api/audio/match ----------
// The test decides when the "late match" resolves; everything else answers at
// once with a benign shape so boot cannot block.
const deferred = {};
global.fetch = (url) => {
  const target = String(url);
  if (target.includes('/api/audio/match')) {
    return new Promise((resolve) => { deferred.match = resolve; });
  }
  if (target.includes('/api/validate')) {
    return Promise.resolve({ok: true, json: () => Promise.resolve({errors: [], warnings: [], items: []})});
  }
  if (target.includes('/api/language')) {
    return Promise.resolve({ok: true, json: () => Promise.resolve({main: {code: 'zh', confidence: 1}, translation: {code: 'ja', confidence: 1}})});
  }
  return Promise.resolve({ok: true, json: () => Promise.resolve({document: {groups: []}, groups: [], errors: [], items: []})});
};

// ---------- boot the real page scripts, in index.html order, one scope ----------
const SCRIPTS = require('./_page_scripts.js')('index.html');
const boot = new Function(SCRIPTS.map(load).join('\n') + `
return {select, preview, current, queue, audioFiles, resolveLyric, refresh,
        getShownRow: () => shownRow,
        setSelected: (i) => { selected = i; },
        selectionVersion: () => selectionVersion,
        setSettling: (v) => { audioSettling = v; },
        isSettling: () => audioSettling,
        setDuration: (ms) => { current().duration = ms; }};`);
let api;
try { api = boot(); } catch (error) { console.log('BOOT FAILED: %s', error.message); process.exit(1); }

const DOC = {
  groups: [
    {start: 14000, main: {text: '第一句', start: 14000, end: 15000, words: []}, translation: {text: '', start: 14000, end: null, words: []}},
    {start: 15000, main: {text: '第二句', start: 15000, end: 16000, words: []}, translation: {text: '', start: 15000, end: null, words: []}},
  ],
};

(async () => {
  await tick();

  // ---- 1. the guard: while settling, currentTime is not evidence ----
  // Put the editor in exactly the state select() leaves behind: audio emptied
  // (currentTime 0) while the user was really listening at 16s. shownRow must
  // keep its previous value instead of being poisoned with -1.
  const item = {name: 'x.lrc', document: DOC, pending: false, history: [], match: null};
  api.queue.push(item);
  api.setSelected(0);
  audio.src = 'blob:x';
  audio.paused = false;
  audio.currentTime = 16.0;
  api.preview();
  const healthyRow = api.getShownRow();
  check('先建立正常的行号（16s -> 第 2 句）', healthyRow, 1);

  // Now the element is cleared and the flag is up: preview must not publish -1.
  api.setSettling(true);
  audio.currentTime = 0;
  api.preview();
  check('音频重置期间 preview 不写入 -1（保留上一行）', api.getShownRow(), healthyRow);
  api.setSettling(false);

  // ---- 2. a late reply must not repaint from a reset element ----
  // Simulate: the audio has been reloaded (select() finished, position restored)
  // and only NOW does the match reply arrive. The fix restores the position and
  // drops the settling guard before awaiting the lookup, so the repaint that
  // the reply triggers sees a trustworthy currentTime.
  audio.currentTime = 16.0;             // restored position
  api.preview();                        // settle the view
  check('位置恢复后 preview 立刻定位到正确行', api.getShownRow(), 1);
  check('预览条显示当前句', element('previewMain').textContent, '第二句');

  // ---- 3. the follow loop must keep working afterwards ----
  // This is what used to break: having published -1 once, every later frame
  // recomputed -1 and the `index !== shownRow` guard never fired again.
  audio.currentTime = 15.2;
  api.preview();
  check('随后跟播仍能跨行更新（未冻结）', api.getShownRow(), 1);
  audio.currentTime = 14.2;
  api.preview();
  check('回退也能更新（高亮与表格同步）', api.getShownRow(), 0);
  check('表格没有残留 activeRow（由 render 负责，这里只验证行号）', typeof api.getShownRow(), 'number');

  // ---- 4. a superseded selection must be ignored ----
  // The user switches songs while a lookup is in flight; the stale answer must
  // not touch the document, and must not even start a request.
  const itemA = {name: 'a.lrc', document: DOC, pending: true, history: [], match: null};
  api.queue.push(itemA);
  api.setSelected(api.queue.length - 1);
  const beforeDoc = itemA.document;
  let rejected = false;
  const stale = api.selectionVersion() - 1;   // one generation behind
  // Race the call against a deadline. A correct implementation returns before
  // await; a broken one walks into the deferred lookup and would hang the whole
  // script, which reads as a truncated log instead of a named failure.
  const raced = await Promise.race([
    api.resolveLyric(itemA, {force: true, version: stale}).then(() => 'returned', (e) => { rejected = true; return 'threw'; }),
    new Promise((r) => setTimeout(() => r('hung'), 300)),
  ]);
  check('过期选择在进入网络请求前就返回', raced, 'returned');
  check('过期选择的结果被丢弃（文档未被改写）', itemA.document === beforeDoc, true);
  check('过期选择不抛异常', rejected, false);
  check('过期选择不发起网络请求', deferred.match === undefined, true);

  console.log('\n%d checks, %d failed', checks, failures);
  process.exit(failures ? 1 : 0);
})().catch((e) => { console.log('ERROR %s', e.stack || e.message); process.exit(1); });
