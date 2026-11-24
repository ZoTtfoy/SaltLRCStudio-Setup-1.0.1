// Regression: the playing line must be pinned to the TOP of the table.
//
// Requested as "将当前行置顶，每播放一行就刷新一次". The old behaviour only kept the
// row *visible*, so it drifted down the table until just the line being sung and
// the one after it were on screen — measured in the user's screenshot at 73% down
// with a single row below. For a timing editor that is backwards: the lines you
// are about to work on are the ones that come next.
//
// jsdom has no layout engine, so every getBoundingClientRect() is zeros and a
// scroll assertion there would be meaningless. This drives the real studio.js
// with explicit geometry instead, so the arithmetic is actually exercised.
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
function atMost(name, actual, limit) {
  checks++;
  if (actual > limit) {
    failures++;
    console.log('FAIL %s\n  got      %s\n  expected <= %s', name, actual, limit);
  } else console.log('ok   %s (%s <= %s)', name, actual, limit);
}

// ---- geometry model -------------------------------------------------------
// The table is a viewport (BOX_H tall) over ROW_H-tall rows, with a sticky
// header. Rect tops are derived from scrollTop so scrolling actually changes
// what the assertions see — a fixed rect would let any arithmetic pass.
const BOX_TOP = 200, BOX_H = 400, HEADER_H = 30, ROW_H = 50;
const ROW_COUNT = 40;
const rows = [];
let wrap = null;

function makeRow(index) {
  const el = {
    index, tagName: 'TR', style: {setProperty() {}}, dataset: {}, children: [], textContent: '',
    classList: {
      _set: new Set(),
      add(c) { this._set.add(c); }, remove(c) { this._set.delete(c); },
      toggle(c, on) { if (on === undefined) { this._set.has(c) ? this._set.delete(c) : this._set.add(c); } else if (on) this._set.add(c); else this._set.delete(c); },
      contains(c) { return this._set.has(c); },
    },
    querySelector: () => null, querySelectorAll: () => [],
    closest(sel) { return sel === '.tablewrap' ? wrap : null; },
    addEventListener() {}, removeEventListener() {}, append() {}, replaceChildren() {},
    setAttribute() {}, removeAttribute() {}, getAttribute() { return null; },
    getBoundingClientRect() {
      // Row tops move with the container's scroll offset, exactly like a browser.
      const top = BOX_TOP + HEADER_H + index * ROW_H - wrap.scrollTop;
      return {top, bottom: top + ROW_H, left: 0, right: 400, width: 400, height: ROW_H};
    },
  };
  return el;
}
const header = {getBoundingClientRect: () => ({top: BOX_TOP, bottom: BOX_TOP + HEADER_H, height: HEADER_H, width: 400})};
wrap = {
  id: 'tablewrap', style: {setProperty() {}}, scrollTop: 0, clientHeight: BOX_H,
  getBoundingClientRect: () => ({top: BOX_TOP, bottom: BOX_TOP + BOX_H, left: 0, right: 400, width: 400, height: BOX_H}),
  querySelector: (sel) => (sel === 'thead' ? header : null),
  addEventListener() {}, classList: {add() {}, remove() {}, toggle() {}, contains() { return false; }},
};

// ---- DOM stub -------------------------------------------------------------
const cache = {};
let seq = 0;
function ctx2d() {
  return new Proxy({measureText: (t) => ({width: String(t).length * 7}), canvas: {width: 0, height: 0}},
    {get(t, p) { if (p in t) return t[p]; return () => {}; }, set(t, p, v) { t[p] = v; return true; }});
}
function element(id) {
  if (cache[id]) return cache[id];
  const el = {
    id, style: {setProperty() {}}, dataset: {}, hidden: false, value: '', textContent: '', innerHTML: '',
    children: [], checked: true, open: false, width: 0, height: 0, tagName: 'DIV',
    parentElement: {clientWidth: 900, clientHeight: 300, appendChild() {},
                    classList: {add() {}, remove() {}, toggle() {}, contains() { return false; }}},
    clientWidth: 900, clientHeight: 300,
    classList: {add() {}, remove() {}, toggle() {}, contains() { return false; }},
    append(...items) { this.children.push(...items); },
    prepend(...items) { this.children.unshift(...items); },
    replaceChildren() { this.children = []; },
    after() {}, before() {}, insertBefore() {}, remove() {},
    addEventListener() {}, removeEventListener() {}, setPointerCapture() {}, releasePointerCapture() {},
    closest() { return null; }, scrollIntoView() {}, focus() {}, select() {}, click() {},
    removeAttribute() {}, setAttribute() {}, getAttribute() { return null; },
    getContext: () => ctx2d(),
    querySelector: () => element('q' + (seq++)), querySelectorAll: () => [],
    getBoundingClientRect() { return {left: 0, top: 0, right: 900, bottom: 300, width: 900, height: 300}; },
  };
  cache[id] = el;
  return el;
}
global.localStorage = {getItem: () => null, setItem() {}, removeItem() {}};
global.sessionStorage = {getItem: () => null, setItem() {}, removeItem() {}};
global.window = {devicePixelRatio: 1, addEventListener() {}, fetch: () => Promise.resolve({ok: true, status: 200, json: async () => ({}), text: async () => ''})};
global.location = {href: 'http://x/', search: '', pathname: '/studio.html'};
global.document = {getElementById: element, createElement: (t) => { const e = element('new:' + t + ':' + (seq++)); e.tagName = t.toUpperCase(); return e; },
  querySelector: () => element('s' + (seq++)), querySelectorAll: () => [], addEventListener() {}, body: element('body')};
global.ResizeObserver = class {observe() {}};
let rafQ = [];
global.requestAnimationFrame = (fn) => { rafQ.push(fn); return rafQ.length; };
global.cancelAnimationFrame = () => {};
const flush = () => { const q = rafQ; rafQ = []; q.forEach((fn) => fn()); };
global.navigator = {};
global.performance = {now: () => 0};

global.fetch = () => Promise.resolve({ok: true, json: () => Promise.resolve({errors: [], items: [], groups: [], document: {groups: []}})});

const load = (f) => fs.readFileSync(path.join(web, f), 'utf8');
const SCRIPTS = require('./_page_scripts.js')('index.html');
let api;
try {
  api = new Function(SCRIPTS.map(load).join('\n') + `
  return {preview, queue, current, setSelected: (i) => { selected = i; }, getShownRow: () => shownRow,
          pinRowToTop, rowsEl: $('rows'), audio: $('audio')};`)();
} catch (e) { console.log('BOOT FAILED: %s', e.stack || e.message); process.exit(1); }

// A document whose lines are 2s apart; the audio is "playing" at whatever
// currentTime the test sets.
const groups = [];
for (let i = 0; i < 40; i++) {
  const t = i * 2000;
  groups.push({start: t,
    main: {text: '第' + (i + 1) + '句', start: t, end: t + 1800, words: []},
    translation: {text: '', start: t, end: null, words: []}});
}
const item = {name: 'x.lrc', document: {groups}, pending: false, history: [], match: null, duration: 90000};
api.queue.push(item);
api.setSelected(0);

// Give #rows the row elements the geometry model describes. `rows` must be
// POPULATED here: building the children from an empty array left the table with
// no rows at all, so the guarded `rows[index]` call was silently skipped and the
// assertion read a scrollTop nothing had touched.
for (let i = 0; i < ROW_COUNT; i++) rows.push(makeRow(i));
api.rowsEl.children = rows;
check('测试夹具已就绪：表格里有行', api.rowsEl.children.length, ROW_COUNT);
api.audio.src = 'blob:x';
api.audio.currentTime = 0;

const tick = () => new Promise((r) => setImmediate(r));

(async () => {
  await tick();
  flush();

  // ---- 1. the first line sits at the top ----
  wrap.scrollTop = 0;
  api.audio.currentTime = 0.5;                 // inside line 0
  api.preview();
  check('第一行播放时停在顶部（不额外滚动）', wrap.scrollTop, 0);

  // ---- 2. when playback moves to a line further down, it is pinned to the top ----
  api.audio.currentTime = 10.5;                // inside line 5
  api.preview();
  // Line 5's natural top is BOX_TOP + HEADER_H + 5*ROW_H; to bring that to
  // BOX_TOP + HEADER_H the container must scroll by exactly 5 rows.
  check('播放到第 6 句时它被顶到顶部', wrap.scrollTop, 5 * ROW_H);
  check('被顶上去的正是当前行', api.getShownRow(), 5);

  // ...and the row really is at the top once scrolled.
  const topOfRow5 = rows[5].getBoundingClientRect().top;
  check('该行确实落在粘性表头下方', Math.round(topOfRow5 - (BOX_TOP + HEADER_H)), 0);

  // ---- 3. it re-pins on EVERY line change, which is what was asked for ----
  for (const line of [8, 12, 13, 20]) {
    api.audio.currentTime = line * 2 + 0.5;
    api.preview();
    const got = Math.round(rows[line].getBoundingClientRect().top - (BOX_TOP + HEADER_H));
    check(`换到第 ${line + 1} 句时重新顶到顶部`, got, 0);
  }
  check('容器的滚动位置随行号推进', wrap.scrollTop, 20 * ROW_H);

  // ---- 4. a line that is already at the top must not be touched ----
  const before = wrap.scrollTop;
  api.preview();                               // same line, no change
  check('同一行内重复刷新不动滚动位置', wrap.scrollTop, before);

  // ---- 5. follow OFF means the table stays where the user put it ----
  api.rowsEl.checked = true;
  element('follow').checked = false;
  wrap.scrollTop = 123;
  api.audio.currentTime = 60.5;                // far away line
  api.preview();
  check('关闭「跟随播放」后不再自动置顶', wrap.scrollTop, 123);
  element('follow').checked = true;

  // ---- 6. it never scrolls more than needed (no runaway) ----
  wrap.scrollTop = 0;
  api.audio.currentTime = 1.5;
  api.preview();
  const after = wrap.scrollTop;
  atMost('第 1 句时不该有可滚动的量', Math.abs(after), 0);

  console.log('\n%d checks, %d failed', checks, failures);
  process.exit(failures ? 1 : 0);
})().catch((e) => { console.log('ERROR %s', e.stack || e.message); process.exit(1); });
