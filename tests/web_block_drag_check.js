// Block-drag on the waveform, end to end without a browser:
//   A) waveform.js event layer: grabbing a block starts a 'block' drag,
//      moving updates the delta, releasing reports it; a click without
//      movement seeks instead; the boundary ticks keep priority.
//   B) studio.js changeTime 'block' branch: the whole line shifts (words and
//      end included) and is clamped so a line can never cross a neighbour.
const fs = require('fs');
const path = require('path');
const web = path.join(__dirname, '..', 'saltlrc', 'web');

let checks = 0, failures = 0;
function check(name, actual, expected) {
  checks++;
  const ok = JSON.stringify(actual) === JSON.stringify(expected);
  if (!ok) { failures++; console.log('FAIL %s\n  got      %s\n  expected %s', name, JSON.stringify(actual), JSON.stringify(expected)); }
  else console.log('ok   %s', name);
}

// ---------- shared stubs ----------
const cache = {};
let seq = 0;
function ctx2d() {
  return new Proxy({ measureText: (t) => ({width: String(t).length * 7}), canvas: {width: 0, height: 0} },
    {get(t, prop) { if (prop in t) return t[prop]; return () => {}; },
     set(t, prop, v) { t[prop] = v; return true; }});
}
function element(id) {
  if (cache[id]) return cache[id];
  const el = {
    id, style: {setProperty() {}}, dataset: {}, hidden: false, value: '', textContent: '', innerHTML: '',
    disabled: false, max: 300, children: [],
    parentElement: {clientWidth: 900, clientHeight: 300, appendChild() {}},
    clientWidth: 900, clientHeight: 300,
    classList: {add() {}, remove() {}, toggle() {}, contains() { return false; }},
    append(...items) { this.children.push(...items); },
    replaceChildren() { this.children = []; },
    addEventListener() {}, removeEventListener() {}, setPointerCapture() {},
    closest() { return null; }, scrollIntoView() {}, focus() {}, select() {}, click() {},
    pause() {}, load() {}, play() { return Promise.resolve(); },
    removeAttribute() {}, setAttribute() {}, getAttribute() { return null; },
    getContext: () => ctx2d(),
    querySelector: () => element('q:' + id + ':' + (seq++)),
    querySelectorAll: () => [],
  };
  cache[id] = el;
  return el;
}
const store = {};
global.localStorage = {
  getItem: (k) => (k in store ? store[k] : null),
  setItem: (k, v) => { store[k] = String(v); },
  removeItem: (k) => { delete store[k]; },
};
global.sessionStorage = {getItem: () => null, setItem() {}, removeItem() {}};
global.window = {devicePixelRatio: 1, addEventListener() {}, fetch: () => Promise.resolve({ok: true, status: 200, json: async () => ({}), text: async () => ''})};
global.document = {
  getElementById: element,
  createElement: (tag) => element('new:' + tag + ':' + (seq++)),
  querySelector: () => element('qs:' + (seq++)),
  querySelectorAll: () => [],
  addEventListener() {},
  body: element('body'),
};
global.ResizeObserver = class { observe() {} };
let rafQueue = [];
global.requestAnimationFrame = (fn) => { rafQueue.push(fn); return rafQueue.length; };
global.cancelAnimationFrame = () => {};
global.fetch = () => Promise.reject(new Error('offline test'));
global.navigator = {};
global.confirm = () => false;
global.alert = () => {};
const flush = () => { const q = rafQueue; rafQueue = []; q.forEach((fn) => fn()); };

const load = (file) => fs.readFileSync(path.join(web, file), 'utf8');

// ---------- A) waveform event layer ----------
const listeners = new Map();
const waveCanvas = {
  width: 0, height: 0, style: {setProperty() {}}, clientWidth: 900,
  parentElement: {clientWidth: 900},
  getBoundingClientRect: () => ({left: 0, top: 0}),
  addEventListener(type, fn) { (listeners.get(type) || listeners.set(type, []).get(type)).push(fn); },
  setPointerCapture() {},
  getContext: () => ctx2d(),
};
const emit = (type, event) => (listeners.get(type) || []).forEach((fn) => fn(event));

const SaltWaveform = new Function(load('theme.js') + '\n' + load('waveform.js') + '\nreturn SaltWaveform;')();
const wave = new SaltWaveform(waveCanvas, {
  seek: (t) => { wave._seeked = t; },
  change: (c) => { wave._changed = c; },
  zoom() {}, pan() {},
});
wave.duration = 180;
const doc = {groups: [{
  start: 5000,
  main: {text: 'AB', start: 5000, end: 7000, words: [{text: 'A', start: 5000}, {text: 'B', start: 5500}]},
  translation: {text: '', start: 5000, end: null, words: []},
}]};
wave.setDocument(doc);
flush(); // build hits

// A1: grabbing inside the block (away from the ±6px tick zones) starts a block drag
emit('pointerdown', {clientX: 520, clientY: 170, pointerId: 1});
check('按住块内空白开始 block 拖拽', wave.drag && wave.drag.kind, 'block');

// A2: moving 90px at 90px/s = 1000ms
emit('pointermove', {clientX: 610, clientY: 170, pointerId: 1});
check('拖拽中记录目标时间', wave.drag.time, Math.round(610 / 90 * 1000));

// A3: release reports the delta
emit('pointerup', {clientX: 610, clientY: 170, pointerId: 1});
check('松开报告 kind=block', wave._changed && wave._changed.kind, 'block');
check('松开报告 delta=1000ms', Math.round(wave._changed.delta), 1000);
check('松开后不再处于拖拽', wave.drag, null);

// A4: click without movement seeks instead of recording a no-op edit
wave._changed = null; wave._seeked = null;
emit('pointerdown', {clientX: 520, clientY: 170, pointerId: 2});
emit('pointerup', {clientX: 520, clientY: 170, pointerId: 2});
check('原地点击不产生编辑', wave._changed, null);
check('原地点击改为定位播放', wave._seeked > 0, true);

// A5: the boundary ticks keep priority (fine adjustments still work)
emit('pointerdown', {clientX: 450, clientY: 170, pointerId: 3}); // 句首竖线 x=5000ms*90/1000=450
check('句首竖线仍按 start 拖拽', wave.drag && wave.drag.kind, 'start');
// A5b: same "a click is not a drag" rule as A4, for TICKS. The block path had
// it from the start; the tick path did not, so a plain click within the ±6px
// grab zone silently shifted that boundary (and pushed a junk undo step).
wave._changed = null; wave._seeked = null;
emit('pointerup', {clientX: 450, clientY: 170, pointerId: 3});   // press and release, no move
check('原地点击刻度不产生编辑', wave._changed, null);
check('原地点击刻度改为定位播放', wave._seeked > 0, true);
// A5c: but a tick that IS dragged still reports its movement
wave._changed = null; wave._seeked = null;
emit('pointerdown', {clientX: 450, clientY: 170, pointerId: 4});
emit('pointermove', {clientX: 468, clientY: 170, pointerId: 4});  // 18px = 200ms
emit('pointerup', {clientX: 468, clientY: 170, pointerId: 4});
check('拖动刻度仍产生编辑', wave._changed && wave._changed.kind, 'start');
check('拖动刻度报告位移 200ms', Math.round(wave._changed.delta), 200);

// A6: full-song strip — paints, pans when dragging the window, seeks outside it
const stripCtx = ctx2d();
const strip = {
  width: 0, height: 0, style: {setProperty() {}}, clientWidth: 900,
  parentElement: {clientWidth: 900},
  getBoundingClientRect: () => ({left: 0, top: 0, width: 900}),
  addEventListener(type, fn) { (listeners.get('s' + type) || listeners.set('s' + type, []).get('s' + type)).push(fn); },
  setPointerCapture() {},
  getContext: () => stripCtx,
};
wave.attachOverview(strip);
flush();
check('贴条被绘制', strip.width, 900);
const emitStrip = (type, event) => (listeners.get('s' + type) || []).forEach((fn) => fn(event));
// x=30 of 900 with duration 180s → t=6s: inside the viewport (0..10s) → pan mode
wave._seeked = null;
emitStrip('pointerdown', {clientX: 30, pointerId: 4});
check('窗口内按下是平移而不是跳转', wave._seeked, null);
emitStrip('pointermove', {clientX: 210, pointerId: 4}); // delta 180px = 36s
check('拖亮框平移视窗', wave.offset, 36);
emitStrip('pointerup', {clientX: 210, pointerId: 4});
// x=800 → t=160s: outside the viewport (36..46s) → seek + centre the view
emitStrip('pointerdown', {clientX: 800, pointerId: 5});
check('窗口外点击跳转播放位置', wave._seeked, 160);
check('跳转后视窗居中到新位置', wave.offset, Math.min(155, 170));
emitStrip('pointerup', {clientX: 800, pointerId: 5});

// ---------- B) studio changeTime block branch ----------
// One combined scope so the classes studio.js references are visible.
// The order comes from index.html, not from a list kept here: history.js was
// split out of studio.js, and a hand-written list silently left it out, so
// `clearSession` (and the other history calls) did not exist in this scope.
// splitter.js is skipped: it wants a real <main> element to split, and this
// check has no layout — only the geometry under test matters here.
const combined = require('./_page_scripts.js')('index.html')
  .filter((name) => name !== 'trace.js' && name !== 'splitter.js')
  .map((name) => load(name)).join('\n');
const api = new Function(combined + '\nreturn {Doc, wave, setPan, changeTime, current, queue, timeline, select, insertRow, deleteRow, undo, redo};')();
const mkGroup = (start) => ({start,
  main: {text: 'X', start, end: start + 2000, words: [{text: 'X', start}]},
  translation: {text: '', start, end: null, words: []}});
api.queue.push({
  name: '拖拽测试.lrc', history: [],
  document: {groups: [mkGroup(1000), mkGroup(10000), mkGroup(20000)],
             metadata: {}, notes: [], unresolved: [], alignment: {}},
  report: {errors: [], warnings: []},
});

(async () => {
await api.select(0);

api.changeTime(1, 'main', 'block', null, undefined, true, 500);
check('整句平移 500ms', api.current().document.groups[1].start, 10500);
check('逐字时间跟随平移', api.current().document.groups[1].main.words[0].start, 10500);
check('句尾跟随平移', api.current().document.groups[1].main.end, 12500);
check('翻译轨联动平移', api.current().document.groups[1].translation.start, 10500);

api.changeTime(1, 'main', 'block', null, undefined, true, 999999);
check('向右拖过头被夹在下一句之前', api.current().document.groups[1].start, 19999);

api.changeTime(1, 'main', 'block', null, undefined, true, -999999);
check('向左拖过头被夹在上一句之后', api.current().document.groups[1].start, 1001);

check('每次拖拽都进入可撤销历史', api.timeline(api.current()).steps.at(-1).label, '调整时间');

// ---------- C) inserting a line in the middle of an imported lyric ----------
// The structural rule is applied by /api/edit (saltlrc/edits.py). Here the
// backend is stubbed with the very same layer-0 mirror, so this asserts the UI
// wiring: the request shape, the returned document being adopted, and the edit
// landing in the undo timeline.
global.fetch = async (url, init) => {
  const body = JSON.parse(init.body);
  const route = String(url).split('/api/')[1];
  if (route === 'edit') {
    const document = body.document;
    if (body.op === 'insert') api.Doc.insertGroup(document, body.index, {duration: body.duration ?? null});
    else if (body.op === 'delete') api.Doc.deleteGroup(document, body.index);
    return {ok: true, json: async () => ({document, report: {errors: [], warnings: []}})};
  }
  return {ok: true, json: async () => ({errors: [], warnings: [],
          main: {code: 'zh', confidence: 1}, translation: {code: 'ja', confidence: 1}})};
};

// back to a clean three-line song: 1000 / 10000 / 20000
await api.undo(); await api.undo(); await api.undo();
const startsOf = () => api.current().document.groups.map((g) => g.start);
check('撤销回到原始三行', startsOf(), [1000, 10000, 20000]);

await api.insertRow(0); // "在第 1 行下方插入"
check('插入后行数 +1', api.current().document.groups.length, 4);
check('新行取上下两行的中点', startsOf(), [1000, 5500, 10000, 20000]);
check('新行是空行', api.current().document.groups[1].main.text, '');
check('插入进入历史', api.timeline(api.current()).steps.at(-1).label, '插入行');
api.undo();
check('插入可以撤销', startsOf(), [1000, 10000, 20000]);
api.redo();
check('插入可以重做', api.current().document.groups.length, 4);

await api.deleteRow(1);
check('删除中间行', startsOf(), [1000, 10000, 20000]);

// ---------- D) setPan must not clamp to 0 without audio ----------
// The strip/wheel call options.pan → setPan, whose limit used to be
// `wave.duration || 0` (= 0 with no audio) → the viewport snapped back to 0.
check('无音频时 wave.duration 为 0', api.wave.duration, 0);
api.setPan(30);
check('无音频时 setPan 不再归零', Math.round(api.wave.offset), 15);   // 上限 25s - 视窗 10s
api.setPan(3);
check('setPan 仍能回到小值', Math.round(api.wave.offset), 3);

// ---------- A7) manual pan suspends auto-follow (the "dead slider" bug) ----------
// preview() runs every frame during playback and setTime(follow) used to snap
// the viewport back whenever the playhead was off-screen — so dragging the
// strip frame or wheel-panning while playing did nothing. It must stick.
wave.duration = 180; wave.pps = 90; wave.offset = 0; wave.resumeFollow();
wave.setTime(100, true);
check('跟随会把视窗拉到播放头', Math.round(wave.offset), Math.round(100 - 10 * .15));

// Drag the strip frame: x 500 → time 100 (inside the frame), move to 300 → -8s
emitStrip('pointerdown', {clientX: 500, pointerId: 11});
emitStrip('pointermove', {clientX: 300, pointerId: 11});
emitStrip('pointerup', {clientX: 300, pointerId: 11});
check('贴条拖框平移视窗', Math.round(wave.offset), Math.round(98.5 - 40));
check('手动平移挂起自动跟随', wave.followSuspended, true);
wave.setTime(70, true);   // playhead outside the viewport, follow requested
check('跟随挂起后播放头出界不再回弹', Math.round(wave.offset), Math.round(58.5));
check('贴条加高到 44px', strip.height, 44);

// Clicking the strip outside the frame seeks AND re-engages follow
wave._seeked = null;
emitStrip('pointerdown', {clientX: 100, pointerId: 12});
check('贴条点击跳转恢复跟随', wave.followSuspended, false);
check('贴条点击的播放位置', wave._seeked, 20);
wave.setTime(100, true);
check('恢复后跟随重新生效', Math.round(wave.offset), Math.round(98.5));

// A8: NO audio decoded — panning must still work off the document's own
// length. This is the real "slider 卡死" bug: every pan path used
// `duration || 0`, so with no audio the limit was 0 and the frame (and the
// wheel) snapped straight back to the start.
wave.setDocument({groups: [
  {start: 1000, main: {text: 'A', start: 1000, end: 5000, words: []}, translation: {text: '', start: 1000, end: null, words: []}},
  {start: 60000, main: {text: 'B', start: 60000, end: 64000, words: []}, translation: {text: '', start: 60000, end: null, words: []}},
]});
wave.duration = 0; wave.pps = 90; wave.offset = 0; wave.resumeFollow();
strip.clientWidth = 900; wave.canvas.clientWidth = 900;
check('无音频时长回退到歌词末尾', Math.round(wave._fallbackDuration()), 65);
check('无音频时视窗上限不再为 0', Math.round(wave.maxOffset()), 55);
// frame sits at 0..10s → x 0..(10/65*900=138); press inside it and drag right
emitStrip('pointerdown', {clientX: 60, pointerId: 21});
emitStrip('pointermove', {clientX: 400, pointerId: 21});
emitStrip('pointerup', {clientX: 400, pointerId: 21});
check('无音频也能拖贴条平移', Math.round(wave.offset) > 20, true);

// ---------- A9) the strip frame keeps a FIXED length ----------
// The frame width used to be xAt(min(viewport, duration)) - xAt(offset), which
// collapsed to the 10px floor whenever the visible span was longer than the
// "song" — i.e. always on a fresh launch, where the length is only the lyric
// fallback. The result was a 10px blob at the left edge (the screenshot).
// Measure it straight off the canvas: count painted pixels in the frame
// colour, which is independent of any internal helper.
const painted = () => {
  const calls = [];
  const ctx = new Proxy({}, {get: (_, prop) => {
    if (prop === 'fillRect' || prop === 'strokeRect') return (x, y, w, h) => calls.push({prop, x, y, w, h});
    return () => {};
  }, set: () => true});
  ctx.fillStyle = '';
  const original = wave.overviewCanvas.getContext;
  wave.overviewCanvas.getContext = () => ctx;
  wave.drawOverview();
  wave.overviewCanvas.getContext = original;
  return calls;
};
// The frame is drawn as fillRect(fx, 1, fw, height-2) right before its handles.
const frameWidth = () => {
  const calls = painted().filter(c => c.prop === 'fillRect' && Math.abs(c.y - 1) < .01);
  return calls.length ? Math.round(calls.at(-1).w) : -1;
};

wave.duration = 0;                       // no audio: the launch state in the shot
wave.setDocument({groups: [mkGroup(1000), mkGroup(10000), mkGroup(20000)]});
wave.pps = 90; wave.offset = 0;
strip.clientWidth = 900; wave.canvas.clientWidth = 900;
// songSeconds = 25s (20s + 5s line), viewport = 10s → the frame covers 40%.
const EXPECT_90 = Math.round(10 / 25 * 900);   // 360px at 90 px/s
check('无音频时贴条框不再塌成 10px', frameWidth(), EXPECT_90);
wave.setDocument({groups: []});          // empty document, the other launch state
check('空文档时贴条框仍是满宽', frameWidth(), 900);
wave.setDocument({groups: [mkGroup(1000), mkGroup(10000), mkGroup(20000)]});

// The whole point: panning must not resize the frame — only move it.
const widthAtOffset = [];
for (const off of [0, 5, 10, 15]) { wave.offset = off; widthAtOffset.push(frameWidth()); }
check('平移时贴条框长度保持一致', [...new Set(widthAtOffset)], [EXPECT_90]);
// Zoom changes how much of the song is visible, so the length may change —
// but only with the zoom, never with the scroll position.
wave.pps = 180; wave.offset = 0;
const zoomed = frameWidth();
check('放大后框变短（可见时间更少）', zoomed < EXPECT_90, true);
wave.offset = 8;
check('放大后平移长度依旧不变', frameWidth(), zoomed);
wave.pps = 90; wave.offset = 0;
check('缩回原缩放框长复原', frameWidth(), EXPECT_90);

// ---------- A10) the width authority ----------
// The v0.9.8 fix made the *formula* correct but left it fed by the wrong
// number. viewportSeconds() read canvas.clientWidth while the frame was drawn
// from canvas.parentElement.clientWidth, and #waveCanvas has no CSS width — so
// before the first paint() the element reports its `width` ATTRIBUTE (300).
// Frame came out 300/1221 as wide as it should be (~4x too short), which is
// the "长度不固定" the user kept seeing. The stub used to set both widths to
// the same 900 and therefore proved nothing; these assertions make them differ
// on purpose, which is the whole point.
wave.pps = 90; wave.offset = 0;
wave.setDocument({groups: [mkGroup(1000), mkGroup(10000), mkGroup(20000)]});
strip.clientWidth = 1221; strip.parentElement.clientWidth = 1221;
wave.canvas.parentElement.clientWidth = 1221;
wave.canvas.clientWidth = 1221;                 // converged: both agree
// viewport = 1221/90 = 13.567s of a 25s song → 13.567/25 * 1221 = 663px.
const EXPECT_1221 = Math.round(1221 / 90 / 25 * 1221);
check('父元素宽 1221 时框按真实比例', frameWidth(), EXPECT_1221);

// Now the launch state: the canvas has not been sized yet and reports 300.
wave.canvas.clientWidth = 300;
check('canvas 报 300 也不会让框变短（真实 bug）', frameWidth(), EXPECT_1221);
check('视窗秒数只认父元素宽', Math.round(wave.viewportSeconds() * 1000), Math.round(1221 / 90 * 1000));
// A stale canvas width must not shift the pan limit either.
check('视窗上限不受 canvas 宽度影响', Math.round(wave.maxOffset()), 25 - Math.round(1221 / 90));
wave.canvas.clientWidth = 1221;

// Zoom must be the ONLY thing that changes the length — sweep the scroll
// position across the full legal range and assert a single distinct width.
const sweep = [];
wave.pps = 90;
for (let off = 0; off <= wave.maxOffset(); off += 0.5) { wave.offset = off; sweep.push(frameWidth()); }
check('整段平移过程中框长只有一个值', [...new Set(sweep)].length, 1);
check('且等于按父宽的期望值', sweep[0], EXPECT_1221);
// At 400 px/s the viewport shows 1221/400 = 3.05s of a 25s song → 149px, so
// the proportional width is the one that applies; the 28px grip floor is only
// a lower bound. Sweep to prove the position still cannot resize it.
wave.pps = 400;
const tight = [];
for (let off = 0; off <= wave.maxOffset(); off += 0.05) { wave.offset = off; tight.push(frameWidth()); }
check('高缩放下平移框长依然恒定', [...new Set(tight)].length, 1);
check('高缩放框长仍是纯比例值', tight[0], Math.round(1221 / 400 / 25 * 1221));
// Drive the proportional width under the floor: a 25s song at 400 px/s across
// a 100px strip is 4s of song in 100px → 16px, below the 28px grip. The floor
// may widen the drawn box, but the width must still not depend on offset.
strip.clientWidth = 100; strip.parentElement.clientWidth = 100;
const floored = [];
for (let off = 0; off <= wave.maxOffset(); off += 0.05) { wave.offset = off; floored.push(frameWidth()); }
check('触到 28px 下限时框长也不随平移变化', [...new Set(floored)].length, 1);
check('触到下限时框长就是 28px', floored[0], 28);
strip.clientWidth = 1221; strip.parentElement.clientWidth = 1221;
wave.pps = 90; wave.offset = 0;

// The strip and the waveform must agree on how wide "the viewport" is: the
// strip draws at its own width, the waveform at the pane's. Both come from the
// same pane, so mapping a point on the strip to a time must round-trip.
strip.clientWidth = 1221; strip.parentElement.clientWidth = 1221;
wave.canvas.parentElement.clientWidth = 1221; wave.canvas.clientWidth = 300;
wave.setDocument({groups: [mkGroup(1000), mkGroup(10000), mkGroup(20000)]});  // 25s
wave.pps = 90; wave.offset = 0;
wave._seeked = null;
// 1000/1221 of the song = 20.475s, which sits outside the frame (it ends at
// 1221/90 = 13.567s), so this is a scrub, not a pan. The time must come from
// the strip's OWN width — the canvas still claims 300 here.
emitStrip('pointerdown', {clientX: 1000, pointerId: 31});
check('贴条映射按贴条宽度换算时间', wave._seeked, 1000 / 1221 * 25);
emitStrip('pointerup', {clientX: 1000, pointerId: 31});
// The strip's own width is the authority for its mapping, not the canvas'.
check('贴条映射宽度取自贴条父元素', Math.round(wave.stripWidth()), 1221);
wave.canvas.clientWidth = 1221;

// ---------- A11) playback highlight vs pointer hover ----------
// The reported bug: with the song playing, pointing the mouse at the table
// froze the waveform highlight (and the outline stopped tracking playback)
// until the browser was paused or another song was selected.
//
// Mechanism: preview() advances the highlight only when the *row* changes, and
// the table's mouseenter used to write that very same field. A hover landing
// mid-row left setHighlight() believing the row was already current, so the
// next preview() call skipped it — the highlight stayed on the hovered line
// instead of following playback, which looks exactly like a hang.
const hoverCanvas = {
  width: 0, height: 0, style: {setProperty() {}}, clientWidth: 900,
  parentElement: {clientWidth: 900},
  getBoundingClientRect: () => ({left: 0, top: 0}),
  addEventListener() {}, setPointerCapture() {}, getContext: () => ctx2d(),
};
const hw = new SaltWaveform(hoverCanvas, {seek() {}, change() {}, zoom() {}, pan() {}});
hw.setDocument({groups: [mkGroup(0), mkGroup(5000), mkGroup(10000)]});
check('初始没有高亮', hw.highlightIndex(), -1);
hw.setHighlight(1);
check('播放高亮落到第 2 行', hw.highlightIndex(), 1);
check('播放高亮存在独立槽位', hw.highlight, 1);
hw.setHover(2);
check('hover 期间画的是悬停行', hw.highlightIndex(), 2);
check('hover 不覆盖播放高亮', hw.highlight, 1);
check('hover 另存一槽', hw.hover, 2);
hw.setHover(null);
check('移开鼠标回落到播放行', hw.highlightIndex(), 1);
// The exact race: hover mid-row, then playback advances to a new row. The new
// playback row must survive and be painted once the pointer leaves.
hw.setHover(0);
hw.setHighlight(2);
check('播放跨行时 hover 仍优先显示', hw.highlightIndex(), 0);
check('播放跨行后播放高亮已更新', hw.highlight, 2);
hw.setHover(null);
check('移开后显示的是新的播放行（不是悬停行）', hw.highlightIndex(), 2);
// A stale hover (row removed under the pointer) must not paint a line that no
// longer exists — fall back to playback instead of drawing nothing.
hw.setHover(99);
check('越界 hover 回落到播放行', hw.highlightIndex(), 2);
hw.setHover(-1);
check('负数 hover 回落到播放行', hw.highlightIndex(), 2);
hw.setHover(null);
hw.setDocument({groups: []});
check('文档清空后不残留高亮', hw.highlightIndex(), -1);

console.log('\n%d 项检查，%d 项失败', checks, failures);
process.exit(failures ? 1 : 0);
})();
