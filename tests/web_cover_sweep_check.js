// Regression: EVERY viewport-moving interaction must leave the canvas covered.
//
// The reported symptom was "波形有内容但缺一块/错位, 拖动或快速点击时" — the
// waveform has content but a chunk is missing or appears shifted, during drags
// and rapid clicks. That is what an uncovered strip of canvas looks like: the
// layer is blitted at an offset, and whatever the blit misses shows the plain
// background fill, so it reads as a missing (or shifted) piece.
//
// web_layer_coverage_check.js covers playback/follow. This sweeps the
// INTERACTIVE routes, because each changes the viewport a different way and the
// coverage condition has to hold for all of them: strip drag/seek, wheel pan,
// zoom, splitter resize, rapid clicking, and drag start/end transitions.
//
// Two independent bugs were found by writing it, both invisible to the older
// tests:
//   * the coverage check itself was missing (fixed in v0.11.7);
//   * paint() clamped the width to a 200px floor while viewportSeconds() used
//     the raw pane width, so on a 150px pane the check measured a narrower
//     viewport than was drawn and passed with a real 20px strip unpainted.
//
// Ground truth for the gap is the canvas's ACTUAL backing-store width
// (`canvas.width`, which paint() sets) — not the CSS width. Measuring against
// the CSS width silently agreed with neither, and made this check lenient
// exactly where the geometry was most likely to be wrong.
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

// ---- stubs ---------------------------------------------------------------
let layerCanvas = null;
let lastBlit = null;
function ctx() {
  return new Proxy({measureText: (t) => ({width: String(t).length * 7}), canvas: {width: 0, height: 0}},
    {get(t, p) {
      if (p in t) return t[p];
      if (p === 'drawImage') return (img, dx) => { if (img && img === layerCanvas) lastBlit = {dx, w: img.width}; };
      return () => {};
    }, set(t, p, v) { t[p] = v; return true; }});
}
const cache = {}; let seq = 0;
function element(id) {
  if (cache[id]) return cache[id];
  const el = {id, style: {setProperty() {}}, dataset: {}, hidden: false, value: '', textContent: '', innerHTML: '',
    children: [], checked: false, open: false, width: 0, height: 0,
    parentElement: {clientWidth: 900, clientHeight: 300, appendChild() {},
                    classList: {add() {}, remove() {}, toggle() {}, contains() { return false; }}},
    clientWidth: 900, clientHeight: 300,
    classList: {add() {}, remove() {}, toggle() {}, contains() { return false; }},
    append() {}, prepend() {}, replaceChildren() {}, after() {}, before() {}, insertBefore() {}, remove() {},
    addEventListener(type, fn) { (this._ev ||= {})[type] = fn; },
    removeEventListener() {}, setPointerCapture() {}, releasePointerCapture() {},
    closest() { return null; }, scrollIntoView() {}, focus() {}, select() {}, click() {},
    removeAttribute() {}, setAttribute() {}, getAttribute() { return null; },
    getContext: () => ctx(),
    querySelector: () => element('q' + (seq++)), querySelectorAll: () => [],
    getBoundingClientRect() { return {left: 0, top: 0, right: 900, bottom: 300, width: 900, height: 300}; }};
  cache[id] = el; return el;
}
global.localStorage = {getItem: () => null, setItem() {}, removeItem() {}};
global.sessionStorage = {getItem: () => null, setItem() {}, removeItem() {}};
const DPR = 2;
global.window = {devicePixelRatio: DPR, addEventListener() {}, fetch: () => Promise.resolve({ok: true, status: 200, json: async () => ({}), text: async () => ''})};
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
const SaltWaveform = new Function(src + '\nreturn SaltWaveform;')();
const mainCanvas = element('main');
const wave = new SaltWaveform(mainCanvas, {});
layerCanvas = wave.layer;
wave.duration = 300;
wave.doc = {groups: []};
wave.peaks = [{rate: 40, values: new Float32Array(3000).fill(0.5)}];
wave.pps = 90;

const cssWidth = () => mainCanvas.parentElement.clientWidth;
const blitGap = () => {
  if (!lastBlit) return Infinity;
  // Ground truth = the canvas's ACTUAL backing-store width, which paint() sets.
  // Using cssWidth()*DPR instead silently disagreed with paint() whenever the
  // pane was narrower than 200px (paint clamps to 200), making this check
  // lenient exactly where the geometry was most likely to be wrong.
  const devW = mainCanvas.width || Math.round(cssWidth() * DPR);
  const left = lastBlit.dx > 0 ? lastBlit.dx : 0;
  const right = lastBlit.dx + lastBlit.w < devW ? devW - (lastBlit.dx + lastBlit.w) : 0;
  return Math.max(left, right);
};

// ---- the sweep -----------------------------------------------------------
let worst = 0, worstLabel = '', samples = 0;
function step(label, fn) {
  fn();
  flush();
  const gap = blitGap();
  samples++;
  if (gap > worst) { worst = gap; worstLabel = label; }
  return gap;
}

console.log('画布 CSS 宽 %d, dpr %d, pps %d', cssWidth(), DPR, wave.pps);

// 1. plain panning across the whole song, at several zooms
for (const pps of [20, 45, 90, 200, 400]) {
  wave.pps = pps;
  for (const off of [0, 0.5, 1.7, 5, 30.25, 120.5, 289, 290]) {
    step('pps=' + pps + ' offset=' + off, () => { wave.offset = Math.min(off, wave.maxOffset()); wave.draw(); });
  }
}

// 2. drag lifecycle: reanchor at the start, then pan far past the margin
wave.pps = 90;
for (const travel of [1, 5, 20, 60, 200]) {
  step('drag start (travel ' + travel + 's)', () => {
    wave.offset = 20; wave.drag = {kind: 'pan', time: 20000}; wave.reanchorForDrag();
  });
  for (let i = 1; i <= 20; i++) {
    step('drag move ' + i + '/' + travel, () => {
      wave.offset = Math.min(wave.maxOffset(), 20 + travel * i / 20);
      wave.drag.time = wave.offset * 1000;
      wave.draw();
    });
  }
  step('drag end', () => { wave.drag = null; wave.layerAnchor = null; wave.draw(); });
}

// 3. zoom changes (the layer's pps key must force a rebuild)
for (const pps of [20, 400, 90, 137, 250, 20]) {
  step('zoom -> ' + pps, () => { wave.pps = pps; wave.draw(); });
}

// 4. pane resize (splitter drag): the canvas and the layer both change width
for (const w of [900, 600, 1221, 300, 205, 199, 150, 60, 900]) {
  step('resize -> ' + w, () => {
    mainCanvas.parentElement.clientWidth = w;
    mainCanvas.clientWidth = w;
    wave.invalidate();
  });
}

// 5. wheel panning (the FL-style scroll path in the constructor)
wave.pps = 90;
mainCanvas.parentElement.clientWidth = 900;
mainCanvas.clientWidth = 900;
wave.invalidate(); flush();
for (let i = 0; i < 40; i++) {
  step('wheel ' + i, () => { wave.offset = Math.min(wave.maxOffset(), 10 + i * 0.4); wave.draw(); });
}

// 6. seek jumps (what clicking the strip/waveform does)
for (const t of [0, 5, 250, 130.5, 289, 12, 200]) {
  step('seek -> ' + t, () => { wave.offset = Math.max(0, Math.min(wave.maxOffset(), t)); wave.draw(); });
}

// 7. rapid alternating small changes (rapid clicking / dragging)
for (let i = 0; i < 60; i++) {
  const off = i % 2 ? 100 : 100.9;
  step('rapid toggle ' + i, () => { wave.offset = off; wave.draw(); });
}

check('全程贴图覆盖完整（无缺失/错位）', worst, 0);
if (worst > 0) console.log('       最差 %d 设备像素 (%d CSS px) @ %s', worst, worst / DPR, worstLabel);
console.log('   （共 %d 个采样点）', samples);

console.log('\n%d checks, %d failed', checks, failures);
process.exit(failures ? 1 : 0);
