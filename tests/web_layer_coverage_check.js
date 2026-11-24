// Regression: the static layer blit must cover the WHOLE canvas.
//
// The bug this guards (shipped in v0.11.2–v0.11.4, seen by the user as "the bug
// got worse"): the layer is painted at a whole-second anchor and then blitted at
// (anchor - offset) * pps * dpr. When the layer was exactly one viewport wide,
// any non-whole-second `offset` shifted it and left a strip of the canvas
// uncovered — measured at 45 CSS px at the default zoom, 100 px at 200 px/s and
// 200 px at 400 px/s, on 198 of 201 sampled offsets.
//
// Follow mode makes `offset` fractional on every frame of playback, so this was
// not an edge case: it was a band flickering at the canvas edge for the whole
// song. It only became *visible* when the canvas was declared opaque
// (alpha:false), because clearRect then leaves opaque black instead of letting
// the page background show through. Hence "worse" after an unrelated-looking
// optimisation — the two changes interact.
//
// The invariant is geometric and deterministic, so it is asserted directly:
// for every zoom level and a sweep of fractional offsets, the blitted layer
// must start at or before x=0 and end at or after the canvas width.
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

// ---- stubs ----------------------------------------------------------------
let layerDraws = [];
let currentLayer = null;
function ctx() {
  return new Proxy({measureText: (t) => ({width: String(t).length * 7}), canvas: {width: 0, height: 0}},
    {get(t, p) {
      if (p in t) return t[p];
      if (p === 'drawImage') {
        return (img, dx) => { if (img === currentLayer) layerDraws.push({dx, w: img.width}); };
      }
      return () => {};
    }, set(t, p, v) { t[p] = v; return true; }});
}
const cache = {}; let seq = 0;
function element(id) {
  if (cache[id]) return cache[id];
  const el = {id, style: {setProperty() {}}, dataset: {}, hidden: false, value: '', textContent: '', innerHTML: '',
    children: [], checked: false, open: false, width: 0, height: 0,
    parentElement: {clientWidth: 900, clientHeight: 300, appendChild() {}}, clientWidth: 900, clientHeight: 300,
    classList: {add() {}, remove() {}, toggle() {}, contains() { return false; }},
    append() {}, prepend() {}, replaceChildren() {}, after() {}, before() {}, insertBefore() {}, remove() {},
    addEventListener() {}, removeEventListener() {}, setPointerCapture() {}, releasePointerCapture() {},
    closest() { return null; }, scrollIntoView() {}, focus() {}, select() {}, click() {},
    removeAttribute() {}, setAttribute() {}, getAttribute() { return null; },
    getContext: () => ctx(),
    querySelector: () => element('q' + (seq++)), querySelectorAll: () => [],
    getBoundingClientRect() { return {left: 0, top: 0, right: 900, bottom: 300, width: 900, height: 300}; }};
  cache[id] = el; return el;
}
global.localStorage = {getItem: () => null, setItem() {}, removeItem() {}};
global.sessionStorage = {getItem: () => null, setItem() {}, removeItem() {}};

const DPR = 2, CSSW = 900, DEVW = CSSW * DPR;
global.window = {devicePixelRatio: DPR, addEventListener() {}, fetch: () => Promise.resolve({ok: true, status: 200, json: async () => ({}), text: async () => ''})};
global.devicePixelRatio = DPR;
global.location = {href: 'http://x/', search: '', pathname: '/'};
global.document = {getElementById: element, createElement: (t) => element('c' + (seq++)),
  querySelector: () => element('s' + (seq++)), querySelectorAll: () => [], addEventListener() {}, body: element('body')};
global.ResizeObserver = class {observe() {}};
let rafQ = [];
global.requestAnimationFrame = (fn) => { rafQ.push(fn); return rafQ.length; };
global.cancelAnimationFrame = () => {};
const flush = () => { const q = rafQ; rafQ = []; q.forEach((fn) => fn()); };
global.navigator = {};
global.performance = {now: () => 0};

const src = fs.readFileSync(path.join(web, 'theme.js'), 'utf8') + '\n'
  + fs.readFileSync(path.join(web, 'waveform.js'), 'utf8');
let SaltWaveform;
try { SaltWaveform = new Function(src + '\nreturn SaltWaveform;')(); }
catch (e) { console.log('BOOT FAILED: %s', e.message); process.exit(1); }

const canvas = element('main');
const wave = new SaltWaveform(canvas, {});
currentLayer = wave.layer;
wave.duration = 300;
wave.doc = {groups: []};
wave.peaks = [{rate: 40, values: new Float32Array(3000).fill(0.5)}];

// The one thing under test: how much of the canvas the blit fails to cover.
function worstGapAt(offset) {
  wave.offset = offset;
  layerDraws = [];
  wave.invalidate();
  flush();
  const d = layerDraws[layerDraws.length - 1];
  if (!d) return {gap: Infinity, note: 'no blit'};
  const gapLeft = d.dx > 0 ? d.dx : 0;
  const gapRight = d.dx + d.w < DEVW ? DEVW - (d.dx + d.w) : 0;
  return {gap: Math.max(gapLeft, gapRight), dx: d.dx, w: d.w};
}

// A sweep that includes the dangerous cases: fractional offsets (what follow
// mode produces every frame), and both song boundaries where the anchor clamps.
const offsets = [];
for (let i = 0; i <= 200; i++) offsets.push(30 + i * 0.01);
offsets.push(0, 0.001, 0.25, 0.5, 0.75, 0.999, 1.5, 299.9, 290.5, 150.25, 150.5);

console.log('画布设备像素宽 %d (CSS %d, dpr %d)', DEVW, CSSW, DPR);
for (const pps of [20, 45, 90, 137, 200, 400]) {
  wave.pps = pps;
  let worst = 0, worstOff = 0;
  for (const off of offsets) {
    const {gap} = worstGapAt(off);
    if (gap > worst) { worst = gap; worstOff = off; }
  }
  atMost(`pps=${pps}：贴图未覆盖的最大宽度（设备像素）`, worst, 0);
  if (worst > 0) console.log('       最差出现在 offset=%s', worstOff);
}

// ---- the invariant must survive a drag, which widens the margin ----
wave.pps = 90;
wave.drag = {kind: 'pan', time: 0};
let dragWorst = 0;
for (const off of offsets) {
  const {gap} = worstGapAt(off);
  if (gap > dragWorst) dragWorst = gap;
}
wave.drag = null;
atMost('拖动中：贴图未覆盖的最大宽度（设备像素）', dragWorst, 0);

// ---- a zoomed-out view where the whole song fits: no padding at all ----
wave.duration = 0;                       // no audio → songSeconds from the lyric
wave.doc = {groups: [{start: 0, main: {text: 'a', start: 0, end: 2000, words: []},
                     translation: {text: '', start: 0, end: null, words: []}}]};
wave.pps = 20;
let tinyWorst = 0;
for (const off of [0, 0.5, 1, 2]) { const {gap} = worstGapAt(off); if (gap > tinyWorst) tinyWorst = gap; }
atMost('无音频的短文档：贴图未覆盖的最大宽度（设备像素）', tinyWorst, 0);

// ---- playing with follow on: coverage must hold over TIME, not just per paint ----
//
// Every check above forces a rebuild first (worstGapAt calls invalidate()), so
// they only prove "a freshly painted layer covers the canvas". That is not the
// case that broke: the layer is painted ONCE and then the viewport keeps moving
// under follow. Nothing else ever sets the dirty flag during playback, so once
// the viewport walked past the painted margin the canvas stayed blank —
// measured before the fix: first gap at 9s of playback, the whole canvas empty
// by 17s, with exactly one rebuild in twenty seconds. The user saw that as
// "跟随功能不能用了".
//
// So: drive setTime(t, true) like preview() does, frame by frame, and check
// coverage on every single frame. The rebuild count is asserted too, because
// "cover it by repainting every frame" would satisfy coverage while undoing the
// optimisation this whole design exists for.
wave.duration = 300;
wave.doc = {groups: []};
wave.peaks = [{rate: 40, values: new Float32Array(3000).fill(0.5)}];
wave.pps = 90;
wave.offset = 0;
wave.layerAnchor = null;
flush();

let followRebuilds = 0;
const followPaintLayer = wave.paintLayer.bind(wave);
wave.paintLayer = function (...a) { followRebuilds++; return followPaintLayer(...a); };

let followWorst = 0, followWorstT = 0, firstGapT = null;
const FRAMES = 20 * 60;                  // twenty seconds of playback
for (let frame = 0; frame < FRAMES; frame++) {
  const t = frame / 60;
  wave.setTime(t, true);                 // follow, exactly what preview() calls
  flush();
  const d = layerDraws[layerDraws.length - 1];
  const right = d ? d.dx + d.w : -1;
  const gapLeft = d && d.dx > 0 ? d.dx : 0;
  const gapRight = d && right < DEVW ? DEVW - right : (d ? 0 : DEVW);
  const gap = Math.max(gapLeft, gapRight);
  if (gap > followWorst) { followWorst = gap; followWorstT = t; }
  if (gap > 0 && firstGapT === null) firstGapT = t;
}
atMost('跟随播放 20 秒：贴图未覆盖的最大宽度（设备像素）', followWorst, 0);
if (followWorst > 0) console.log('       首次出现空白 t=%ss，最差 t=%ss', firstGapT, followWorstT);
// Distance, not frames: 20s of playback can advance the viewport at most 20s,
// and each rebuild buys half the margin, so a handful is plenty. Repainting
// every frame (1200) would fail this by two orders of magnitude.
atMost('跟随播放 20 秒：静态层重建次数（按滚动距离，不按帧数）', followRebuilds, 40);
check('跟随播放：确实在推进视窗（否则这个检查没有意义）', wave.offset > 0, true);

console.log('\n%d checks, %d failed', checks, failures);
process.exit(failures ? 1 : 0);
