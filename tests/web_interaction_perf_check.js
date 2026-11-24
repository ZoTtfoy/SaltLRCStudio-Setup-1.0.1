// Regression: interacting with the table (hovering, clicking, following
// playback) must not rebuild the static waveform layer.
//
// Why this exists: the hover/playback outline used to be painted INSIDE the
// static layer, so every hovered row, every clicked row and every playback row
// change set the layer dirty and repainted the whole 900px waveform plus both
// lyric tracks. Measured at ~2150 canvas calls per interaction, which is why
// sweeping the pointer down the table — or clicking quickly through a long
// lyric — could not keep up. The outline is now an overlay drawn on top of the
// blit, like the playhead: ~28 calls, and no invalidation at all.
//
// Two things are asserted separately, because they can fail independently:
//   * COST   — no layer rebuild, and a bounded number of main-canvas calls;
//   * TRUTH  — the outline is still drawn, in the right place. Moving drawing
//              code is exactly the change that silently deletes it.
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

// ---- per-canvas recording contexts -----------------------------------------
// Each canvas gets its own recorder so calls can be attributed: "the static
// layer was not repainted" is only meaningful if layer calls are counted apart
// from main-canvas calls.
const recorders = new Map();
function recorderFor(label) {
  let rec = recorders.get(label);
  if (rec) return rec;
  rec = {label, calls: [], count: 0};
  const target = {measureText: (t) => ({width: String(t).length * 7}), canvas: {width: 0, height: 0}};
  rec.ctx = new Proxy(target, {
    get(t, p) {
      if (p in t) return t[p];
      return (...args) => { rec.calls.push({op: String(p), args}); rec.count++; };
    },
    set(t, p, v) { t[p] = v; return true; },
  });
  recorders.set(label, rec);
  return rec;
}
const resetRecorders = () => { for (const r of recorders.values()) { r.calls = []; r.count = 0; } };

const cache = {};
let seq = 0;
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
    addEventListener(type, fn) { (this._ev ||= {})[type] = fn; },
    removeEventListener() {}, setPointerCapture() {}, releasePointerCapture() {},
    closest() { return null; }, scrollIntoView() {}, focus() {}, select() {}, click() {},
    removeAttribute() {}, setAttribute() {}, getAttribute() { return null; },
    getContext() { return (this._rec || (this._rec = recorderFor(id))).ctx; },
    querySelector: () => element('q:' + id + ':' + (seq++)),
    querySelectorAll: () => [],
    getBoundingClientRect() { return {left: 0, top: 0, right: 900, bottom: 300, width: 900, height: 300}; },
  };
  cache[id] = el;
  return el;
}
const store = {};
global.localStorage = {getItem: (k) => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); }, removeItem: (k) => { delete store[k]; }};
global.sessionStorage = {getItem: () => null, setItem() {}, removeItem() {}};
global.window = {devicePixelRatio: 1, addEventListener() {}, fetch: () => Promise.resolve({ok: true, status: 200, json: async () => ({}), text: async () => ''})};
global.location = {href: 'http://127.0.0.1/studio.html', search: '', pathname: '/studio.html'};
let created = 0;
global.document = {
  getElementById: element,
  createElement: (t) => {
    // Tag every created canvas so the layer's recorder is addressable.
    const id = t === 'canvas' ? 'canvas#' + (++created) : 'new:' + t + ':' + (seq++);
    const e = element(id); e.tagName = String(t).toUpperCase(); return e;
  },
  querySelector: () => element('qs:' + (seq++)),
  querySelectorAll: () => [],
  addEventListener() {},
  body: element('body'),
};
global.ResizeObserver = class {observe() {}};
let rafQueue = [];
let rafId = 0;
global.requestAnimationFrame = (fn) => { rafQueue.push({id: ++rafId, fn}); return rafId; };
global.cancelAnimationFrame = (id) => { rafQueue = rafQueue.filter((e) => e.id !== id); };
global.navigator = {};
global.confirm = () => false;
global.alert = () => {};
global.Blob = class {constructor() {}};
URL.createObjectURL = () => 'blob:x';
URL.revokeObjectURL = () => {};
global.TextEncoder = require('util').TextEncoder;
let now = 0;
global.performance = {now: () => now};

const audio = element('audio');
audio.src = 'blob:x'; audio.paused = true; audio.currentTime = 0; audio.duration = 300;
audio.pause = () => { audio.paused = true; };
audio.play = () => { audio.paused = false; return Promise.resolve(); };
audio.load = () => {};
audio.removeAttribute = () => {};
global.fetch = () => Promise.resolve({ok: true, json: () => Promise.resolve({errors: [], items: [], groups: [], document: {groups: []}})});

const load = (f) => fs.readFileSync(path.join(web, f), 'utf8');
const SCRIPTS = require('./_page_scripts.js')('index.html');
let api;
try {
  api = new Function(SCRIPTS.map(load).join('\n') + `
  return {wave, queue, setSelected: (i) => { selected = i; }, locate, current};`)();
} catch (e) { console.log('BOOT FAILED: %s', e.stack || e.message); process.exit(1); }

const LINE_MS = 2200, FIRST_MS = 2000;
const groups = [];
for (let i = 0; i < 120; i++) {
  const t = FIRST_MS + i * LINE_MS;
  const text = '这是第' + (i + 1) + '句歌词，用来占位构成一个真实长度的文档';
  const words = [];
  for (let j = 0; j < text.length; j++) words.push({text: text[j], start: t + j * 120});
  groups.push({start: t,
    main: {text, start: t, end: t + 1800, words},
    translation: {text: 'translation line ' + (i + 1), start: t, end: t + 1800, words: []}});
}
const item = {name: 'x.lrc', document: {groups}, pending: false, history: [], match: null, duration: 300000};
api.queue.push(item);
api.setSelected(0);

const flushFrame = () => { const q = rafQueue; rafQueue = []; now += 16.7; q.forEach((e) => e.fn()); };
const drain = () => { let g = 0; while (rafQueue.length && g++ < 30) flushFrame(); };
const tick = () => new Promise((r) => setImmediate(r));
// Ask the waveform which canvases it owns rather than guessing from creation
// order — an earlier version of this test looked up 'canvas#1' by convention,
// which silently became "a recorder that never receives anything", so the
// "no rebuild" assertions passed without testing anything.
let layerCanvasId = null, mainCanvasId = null;
const blank = () => ({count: 0, calls: []});
const layerRec = () => recorders.get(layerCanvasId) || blank();
const mainRec = () => recorders.get(mainCanvasId) || blank();

(async () => {
  await tick();
  api.wave.doc = item.document;
  api.wave.docVersion++;
  api.wave.duration = 300;
  api.wave.peaks = [{rate: 40, values: new Float32Array(12000).fill(0.5)}];
  api.wave.offset = 30;                 // mid-song, so rows are on screen
  // A paint must actually happen before anything can be counted.
  api.wave.invalidate();
  drain();
  layerCanvasId = api.wave.layer.id;
  mainCanvasId = api.wave.canvas.id;
  if (!recorders.has(layerCanvasId)) { console.log('BOOT: 静态层没有被绘制过'); process.exit(1); }

  // ---- the layer really did get painted, and it holds the lyric content ----
  check('静态层已绘制（冷启动时应当重建一次）', layerRec().count > 0, true);
  check('静态层包含歌词文字', layerRec().calls.some(c => c.op === 'fillText'), true);

  // warm-up: the first interaction may legitimately settle the anchor
  api.wave.setHover(3); flushFrame();
  api.wave.setHover(4); flushFrame();

  // ---- A) hovering rows must not rebuild the layer ----
  resetRecorders();
  for (let i = 0; i < 40; i++) { api.wave.setHover(i % 40); flushFrame(); }
  check('hover 40 行：静态层重建次数', layerRec().count, 0);
  atMost('hover 40 行：主画布总调用', mainRec().count, 40 * 45);
  atMost('hover 40 行：平均每次调用', Math.round(mainRec().count / 40), 45);

  // ---- B) clicking rows must not rebuild the layer ----
  // `locate()` pans the viewport when the row is OUTSIDE it — and when it pans,
  // the visible content really has changed, so rebuilding is correct and
  // required (the blit can only serve viewports inside what it painted).
  // This scenario therefore pins the viewport and clicks rows that are already
  // on screen, which is the case that must stay free.
  api.wave.offset = 30;
  api.wave.draw();
  flushFrame();
  resetRecorders();
  for (let i = 0; i < 20; i++) { api.locate(13 + (i % 4)); flushFrame(); }
  check('点击屏幕内 20 行：静态层重建次数', layerRec().count, 0);
  atMost('点击屏幕内 20 行：平均每次调用', Math.round(mainRec().count / 20), 45);
  check('点击屏幕内的行没有移动视窗', Math.round(api.wave.offset), 30);

  // ---- C) playback row changes must not rebuild the layer ----
  resetRecorders();
  for (let i = 0; i < 60; i++) { api.wave.setHighlight(i); flushFrame(); }
  check('高亮切换 60 次：静态层重建次数', layerRec().count, 0);
  atMost('高亮切换：平均每次调用', Math.round(mainRec().count / 60), 45);

  // ---- D) the outline must still be DRAWN, and in the right place ----
  // Moving drawing from the layer to the overlay is exactly the kind of change
  // that can silently drop it: without this the cost assertions above would
  // still pass with the outline deleted.
  //
  // The viewport has to be pinned first: locate() pans (that is what focusLine
  // does), so by now the view has scrolled far from where it started. Line 13
  // starts at 30.6s, so pinning the view at 30s puts it on screen.
  const VISIBLE = 13;
  api.wave.offset = 30;
  api.wave.setHover(null);
  api.wave.setHighlight(0);              // off-screen, so the first paint is clean
  api.wave.draw();
  flushFrame();
  resetRecorders();
  api.wave.setHighlight(VISIBLE);        // the measured paint
  flushFrame();
  const rects = mainRec().calls.filter(c => c.op === 'rect');
  check('高亮框已画在主画布上（离开静态层后仍存在）', rects.length >= 1, true);
  // Both tracks of the highlighted line are outlined, at y≈157 / y≈224.
  const ys = rects.map(c => Math.round(c.args[1]));
  check('主轨与翻译轨各有一个框', ys.includes(157) && ys.includes(224), true);
  const row = groups[VISIBLE];
  const expectedX = (row.main.start / 1000 - api.wave.offset) * api.wave.pps;
  const mainRect = rects.find(c => Math.round(c.args[1]) === 157);
  atMost('高亮框 x 与行的开始时间一致（误差 1px）',
         Math.abs(mainRect.args[0] - (expectedX - 1)), 1);
  const expectedW = Math.max(3, (row.main.end - row.main.start) / 1000 * api.wave.pps) + 2;
  atMost('高亮框宽度与行时长一致（误差 1px）', Math.abs(mainRect.args[2] - expectedW), 1);
  check('高亮改动没有让静态层重建', layerRec().count, 0);
  // The drawn case is the expensive one, so it gets its own budget.
  atMost('画高亮时主画布调用总数', mainRec().count, 60);

  // ---- E) an outline scrolled out of view must not be drawn on screen ----
  resetRecorders();
  api.wave.setHighlight(0);                 // line 0 starts at 2s, view starts at 30s
  api.wave.draw();
  flushFrame();
  check('视野外的高亮不画框', mainRec().calls.filter(c => c.op === 'rect').length, 0);

  console.log('\n%d checks, %d failed', checks, failures);
  process.exit(failures ? 1 : 0);
})().catch((e) => { console.log('ERROR %s', e.stack || e.message); process.exit(1); });
