// Regression: seeking during playback must not stop the frame loop.
//
// The reported bug ("播放音频后，去点击波形图的时候，整个界面会卡住不再渲染"):
//   Clicking the waveform not on a tick/block issues a seek, which sets
//   <audio>.currentTime and makes the browser fire `seeked`. The handler wired
//   to `seeked` was shared with `pause` and unconditionally cancelled the
//   requestAnimationFrame loop. Nothing restarted it, because `play` only fires
//   when playback *starts* — so one click mid-playback left the sound running
//   while the playhead, the karaoke highlight and the follow-scroll all froze.
//
// This drives the real studio.js: start playback, click the waveform, fire
// `seeked` like the browser would, and then check that later frames still
// repaint. It fails on the old code and passes on the fix.
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
    addEventListener(type, fn) { (this._ev ||= {})[type] = fn; },
    removeEventListener() {}, setPointerCapture() {}, releasePointerCapture() {},
    closest() { return null; }, scrollIntoView() {}, focus() {}, select() {}, click() {},
    removeAttribute() {}, setAttribute() {}, getAttribute() { return null; },
    getContext: () => ctx2d(),
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
global.document = {
  getElementById: element,
  createElement: (t) => { const e = element('new:' + t + ':' + (seq++)); e.tagName = t.toUpperCase(); return e; },
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
global.performance = {now: () => 0};

// A controllable <audio>: the test decides what the browser would have fired.
const audio = element('audio');
audio.src = 'blob:x';
audio.paused = true;
audio.ended = false;
audio.currentTime = 0;
audio.duration = 227;
audio.pause = () => { audio.paused = true; };
audio.play = () => { audio.paused = false; return Promise.resolve(); };
audio.load = () => { audio.currentTime = 0; };
audio.removeAttribute = (n) => { if (n === 'src') audio.src = ''; };
let currentTimeWrites = 0;
// Recording every assignment proves the click really did seek.
Object.defineProperty(audio, 'currentTime', {
  get() { return this._t || 0; },
  set(v) { currentTimeWrites++; this._t = v; },
  configurable: true,
});

global.fetch = () => Promise.resolve({ok: true, json: () => Promise.resolve({errors: [], items: [], groups: [], document: {groups: []}})});

const load = (f) => fs.readFileSync(path.join(web, f), 'utf8');
const SCRIPTS = require('./_page_scripts.js')('index.html');
let api;
try {
  api = new Function(SCRIPTS.map(load).join('\n') + `
  return {wave, queue, setSelected: (i) => { selected = i; },
          getRafLoop: () => rafLoop,
          getWaveTime: () => wave.time,
          getShownRow: () => shownRow,
          audio: $('audio'),
          tick: () => tick()};`)();
} catch (e) { console.log('BOOT FAILED: %s', e.stack || e.message); process.exit(1); }

const groups = [];
for (let i = 0; i < 40; i++) {
  const t = 1000 + i * 2000;
  const text = '歌词第' + (i + 1) + '句';
  const words = [];
  for (let j = 0; j < text.length; j++) words.push({text: text[j], start: t + j * 150});
  groups.push({start: t,
    main: {text, start: t, end: t + 1600, words},
    translation: {text: 'line ' + (i + 1), start: t, end: t + 1600, words: []}});
}
const item = {name: 'x.lrc', document: {groups}, pending: false, history: [], match: null, duration: 90000};
api.queue.push(item);
api.setSelected(0);

// Drain one animation frame (only the callbacks queued before it started).
const flushFrame = () => { const q = rafQueue; rafQueue = []; q.forEach((e) => e.fn()); };
const drain = () => { let guard = 0; while (rafQueue.length && guard++ < 50) flushFrame(); };
const tick = () => new Promise((r) => setImmediate(r));

(async () => {
  await tick();
  api.wave.doc = item.document;
  api.wave.docVersion++;
  api.wave.duration = 90;
  api.wave.peaks = [{rate: 40, values: new Float32Array(3600).fill(0.5)}];
  drain();

  // ---- start playback ----
  audio.paused = false;
  audio.onplay();
  check('播放开始时帧循环已启动', api.getRafLoop() !== 0, true);

  // Advance "playback" and confirm the UI is repainting: wave.time follows
  // currentTime, which is the visible behaviour of a live playhead.
  audio.currentTime = 5.0;
  drain();
  const t1 = api.getWaveTime();
  audio.currentTime = 6.0;
  drain();
  const t2 = api.getWaveTime();
  check('播放中帧循环持续推进播放头（5s -> 6s）', t2 > t1 && t2 > 5.9, true);

  // ---- click the waveform while playing ----
  // A click that lands on neither a tick nor a block issues a seek. The stub
  // canvas has no hits and an empty wave document, so this is the seek branch.
  const canvas = element('waveCanvas');
  const before = currentTimeWrites;
  canvas._ev.pointerdown({clientX: 300, clientY: 260, pointerId: 3});
  canvas._ev.pointerup({clientX: 300, clientY: 260, pointerId: 3});
  check('点击波形图确实发起了跳转', currentTimeWrites > before, true);

  // The browser now reports the seek as complete. This is the event that used
  // to kill the loop.
  audio.onseeked();

  // ---- playback must CONTINUE rendering after the seek ----
  audio.currentTime = 8.0;
  drain();
  const t3 = api.getWaveTime();
  audio.currentTime = 9.0;
  drain();
  const t4 = api.getWaveTime();
  check('跳转后帧循环仍在运行（没有停在暂停态）', api.getRafLoop() !== 0, true);
  check('跳转后播放头仍在推进（8s -> 9s）', t4 > t3 && t4 > 8.9, true);

  // ---- and the karaoke row must still follow ----
  audio.currentTime = 9.2;
  drain();
  const row = api.getShownRow();
  audio.currentTime = 11.2;
  drain();
  check('跳转后歌词行仍跟随播放（9.2s -> 11.2s 跨行）', api.getShownRow() > row, true);

  // ---- pausing must still stop the loop (the fix must not break this) ----
  audio.paused = true;
  audio.onpause();
  check('暂停后帧循环停止', api.getRafLoop(), 0);

  // ---- and resuming must start it again ----
  audio.paused = false;
  audio.onplay();
  check('继续播放后帧循环重启', api.getRafLoop() !== 0, true);

  console.log('\n%d checks, %d failed', checks, failures);
  process.exit(failures ? 1 : 0);
})().catch((e) => { console.log('ERROR %s', e.stack || e.message); process.exit(1); });
