// Track captions must be pinned to the row, not scroll with the content.
//
// Reported as "把这个翻译轨道和主轨道提示优化一下，不用显示语言了". Two separate
// defects were behind that:
//   1. the text named a language ("主轨 · 中文" / "翻译轨 · 日语"), but the main
//      track is not necessarily Chinese nor the translation track Japanese —
//      v0.4 dropped exactly that assumption, so naming a language was wrong;
//   2. the caption was painted INSIDE the cached layer, so it was blitted at an
//      offset like the content — it slid sideways with the waveform and, at some
//      viewports, off the canvas entirely, leaving no caption at all.
//
// The invariant that matters is (2): the caption's x on screen must be the same
// at every viewport offset. A test that only checked "is the label drawn?" would
// pass with the scrolling behaviour still in place.
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

// ---- stub with per-canvas text and rect capture ---------------------------
let texts = {};          // canvasId -> [{text, x, y}]
let rects = {};          // canvasId -> [{x, y, w, h}]
function ctxFor(id) {
  return new Proxy(
    {measureText: (t) => ({width: String(t).length * 7}), canvas: {width: 0, height: 0}},
    {get(t, p) {
      if (p in t) return t[p];
      if (p === 'fillText') return (text, x, y) => { (texts[id] ||= []).push({text, x, y}); };
      if (p === 'fillRect') return (x, y, w, h) => { (rects[id] ||= []).push({x, y, w, h}); };
      return () => {};
    }, set(t, p, v) { t[p] = v; return true; }});
}
const cache = {}; let seq = 0;
function element(id) {
  if (cache[id]) return cache[id];
  const el = {
    id, style: {setProperty() {}}, dataset: {}, hidden: false, value: '', textContent: '', innerHTML: '',
    children: [], checked: false, open: false, width: 0, height: 0,
    parentElement: {clientWidth: 900, clientHeight: 300, appendChild() {},
                    classList: {add() {}, remove() {}, toggle() {}, contains() { return false; }}},
    clientWidth: 900, clientHeight: 300,
    classList: {add() {}, remove() {}, toggle() {}, contains() { return false; }},
    append() {}, prepend() {}, replaceChildren() {}, after() {}, before() {}, insertBefore() {}, remove() {},
    addEventListener() {}, removeEventListener() {}, setPointerCapture() {}, releasePointerCapture() {},
    closest() { return null; }, scrollIntoView() {}, focus() {}, select() {}, click() {},
    removeAttribute() {}, setAttribute() {}, getAttribute() { return null; },
    getContext: () => ctxFor(id),
    querySelector: () => element('q' + (seq++)), querySelectorAll: () => [],
    getBoundingClientRect() { return {left: 0, top: 0, right: 900, bottom: 300, width: 900, height: 300}; }};
  cache[id] = el; return el;
}
global.localStorage = {getItem: () => null, setItem() {}, removeItem() {}};
global.sessionStorage = {getItem: () => null, setItem() {}, removeItem() {}};
global.window = {devicePixelRatio: 1, addEventListener() {}, fetch: () => Promise.resolve({ok: true, status: 200, json: async () => ({}), text: async () => ''})};
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
const main = element('main');
const wave = new SaltWaveform(main, {});
wave.duration = 300;
wave.doc = {groups: []};
wave.peaks = [{rate: 40, values: new Float32Array(3000).fill(0.5)}];
wave.pps = 90;

const layerId = wave.layer.id;
const allLayer = () => Object.values(texts[layerId] || {}).map((t) => t.text);
const textsOn = (id) => (texts[id] || []);
function resetTexts() { texts = {}; rects = {}; }

// ---- 1. the caption must not name a language ------------------------------
wave.offset = 40;
wave.layerAnchor = null; wave.invalidate(); flush();
resetTexts();
wave.invalidate(); flush();
const layerTexts = allLayer();
check('静态层里不再出现「中文」', layerTexts.filter((t) => t.includes('中文')).length, 0);
check('静态层里不再出现「日语」', layerTexts.filter((t) => t.includes('日语')).length, 0);

const mainTexts = textsOn(main.id).filter((t) => t.text === '主轨' || t.text === '翻译轨');
check('画布上画出了两个轨道提示', mainTexts.length, 2);
check('提示文字就是「主轨」和「翻译轨」',
      mainTexts.map((t) => t.text).sort(), ['主轨', '翻译轨'].sort());
check('提示里不含任何语言名',
      mainTexts.filter((t) => /中文|日语|英文|翻译轨 ·/.test(t.text)).length, 0);

// ---- 2. THE invariant: the caption does not move with the viewport ---------
// Painted into the layer it would be blitted at dx=(anchor-offset)*pps, so its
// x would track the offset. Pinned, its x is constant.
const xsByOffset = [];
for (const off of [0, 5, 40, 137.5, 280]) {
  wave.offset = Math.min(off, wave.maxOffset());
  wave.layerAnchor = null;
  wave.invalidate();
  flush();
  resetTexts();
  wave.draw();
  flush();
  const hits = textsOn(main.id).filter((t) => t.text === '主轨' || t.text === '翻译轨');
  if (hits.length !== 2) { xsByOffset.push('missing@' + off); continue; }
  xsByOffset.push(hits.map((t) => t.x).join(','));
}
check('提示的 x 与视窗偏移无关（每档都相同）',
      Array.from(new Set(xsByOffset)), ['11,11']);

// ...and it stays inside the visible left edge, where a row caption belongs.
// Checked on EVERY sample, not just the first: a caption that is correct at
// offset 0 and negative at offset 280 has slid off the canvas.
check('提示贴在画布左边缘（每档都在 x 0..20）',
      xsByOffset.every((s) => {
        const x = Number(String(s).split(',')[0]);
        return Number.isFinite(x) && x >= 0 && x <= 20;
      }), true);

// ---- 3. the layer must not contain the caption at all ---------------------
// If it were still painted there it would scroll, however the overlay behaves.
for (const off of [10, 120]) {
  wave.offset = off;
  wave.layerAnchor = null; wave.invalidate(); flush();
  resetTexts();
  wave.invalidate(); flush();
  const inLayer = allLayer().filter((t) => t === '主轨' || t === '翻译轨');
  check('静态层里没有轨道提示（偏移 ' + off + '）', inLayer.length, 0);
}

// ---- 4. the caption survives a repaint without a rebuild -----------------
// Overlay content is repainted every frame; a caption that only appeared on a
// rebuild would vanish as soon as playback started scrolling within the margin.
wave.offset = 0; wave.layerAnchor = null; wave.invalidate(); flush();
resetTexts();
for (let i = 0; i < 5; i++) { wave.offset = i * 0.1; wave.draw(); flush(); }
const frames = textsOn(main.id).filter((t) => t.text === '主轨' || t.text === '翻译轨').length;
check('每次重绘都画提示（5 帧 × 2 条）', frames, 10);

// ---- 5. a backing chip keeps it readable ---------------------------------
// The caption sits in the thin gap above each band, where grid labels and block
// edges scroll past. Without a chip those can run right through the text.
resetTexts();
wave.draw(); flush();
const chips = (rects[main.id] || []).filter((r) => r.x === 6);
check('两条提示各有一个底色衬底', chips.length, 2);
check('衬底足以容纳一行字（高度）', chips.every((r) => r.h >= 12 && r.h <= 20), true);
check('衬底宽度按文字长度给（不是硬编码的固定宽）',
      chips.map((r) => r.w).sort((a, b) => a - b), [24, 31]);
// '主轨' is 2 chars and '翻译轨' is 3, and the stub measures 7px per char, so the
// widths must differ by exactly one character. Equal widths would mean the chip
// is a constant rather than following the label.
check('衬底衬在两条提示各自的基线之上',
      chips.map((r) => r.y).sort((a, b) => a - b), [133, 200]);

console.log('\n%d checks, %d failed', checks, failures);
process.exit(failures ? 1 : 0);
