// 刻度线检查：几条挤在一起时，"能抓的那条"要抓得准、看得见。
//
// 判据全部是几何量与绘制指令，不靠肉眼：
//   * 命中取**最近**的一条（不是画得最晚的那条）；
//   * 两条落在同一像素时按类型定序（句尾 > 句首 > 逐字），而且**同一位置永远给同一条**；
//   * 抓取半径 ±TICK_GRAB，出界就抓不到；
//   * 句首/句尾画得比逐字边界**粗**；指针压着的那条比两者都粗、且换颜色。
const fs = require('fs');
const path = require('path');
const web = path.join(__dirname, '..', 'saltlrc', 'web');
const load = (file) => fs.readFileSync(path.join(web, file), 'utf8');

let failures = 0;
function check(name, ok, detail) {
  if (ok) { console.log('  ok   ' + name); return; }
  failures += 1;
  console.log('  FAIL ' + name + (detail === undefined ? '' : ' → ' + detail));
}

// ---- 最小环境：打桩 canvas，把每次 stroke 的线宽与颜色记下来 -------------------
const RECORDED = [];
const STATE = {};
const BASE = {
  measureText: (text) => ({width: String(text).length * 7}),
  canvas: {width: 900, height: 400},
  beginPath() {}, closePath() {}, fill() {}, clearRect() {},
  rect(x, y, w, h) { RECORDED.push({op: 'rect', x, y, w, h}); },
  fillRect() {},
  save() {}, restore() {}, clip() {}, arc() {}, translate() {}, scale() {},
  setTransform() {}, setLineDash() {}, fillText() {}, strokeRect() {},
  createLinearGradient: () => ({addColorStop() {}}),
  createRadialGradient: () => ({addColorStop() {}}),
  moveTo() { STATE.last = null; },
  lineTo(x, y) {
    RECORDED.push({op: 'line', x, y, width: STATE.lineWidth || 1, colour: STATE.strokeStyle || ''});
  },
  stroke() { RECORDED.push({op: 'stroke', width: STATE.lineWidth || 1, colour: STATE.strokeStyle || ''}); },
};
const makeContext = () => new Proxy(BASE, {
  get: (target, key) => (key in target ? target[key] : () => undefined),
  set: (target, key, value) => { STATE[key] = value; target[key] = value; return true; },
});
const layer = {
  width: 660, height: 292, clientWidth: 660, parentElement: {clientWidth: 660},
  getContext: makeContext, style: {}, addEventListener() {},
  getBoundingClientRect: () => ({left: 0, top: 0, width: 660, height: 292}),
};
global.window = {devicePixelRatio: 1, addEventListener() {}};
global.document = {
  addEventListener() {}, createElement: () => layer, documentElement: {dataset: {}},
  getElementById: () => null, querySelector: () => layer, querySelectorAll: () => [],
};
// 令牌取值直接从 style.css 的 :root 里读 —— 不在这里再抄一份颜色，
// 否则测试自己就是第二个权威。
const CSS = fs.readFileSync(path.join(web, 'style.css'), 'utf8');
const ROOT_START = CSS.indexOf(':root{');
const TOKENS = {};
for (const match of CSS.slice(ROOT_START, CSS.indexOf('\n}', ROOT_START))
                        .matchAll(/(--[a-z0-9-]+)\s*:\s*([^;]+);/g)) {
  TOKENS[match[1]] = match[2].trim();
}
global.getComputedStyle = () => ({getPropertyValue: (name) => TOKENS[name] || ''});
global.performance = {now: () => 0};
global.requestAnimationFrame = (cb) => setTimeout(() => cb(0), 0);
global.cancelAnimationFrame = () => {};
global.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} };

const SaltWaveform = new Function(
  load('theme.js') + '\n' + load('waveform.js') + '\nreturn SaltWaveform;')();

// pps = 100 时 x = 毫秒 / 10，便于把时间直接当坐标用。
const group = (start, text, words, end) => ({
  start,
  main: {text, start, end: end == null ? null : end, words},
  translation: {text: '', start, end: null, words: []},
});
const word = (text, start) => ({text, start});

// 三行，刻意造出两种"挤在一起"：
//   * 第 2 行的句尾 3600 与第 3 行的句首 3600 落在**同一像素**（类型定序才分得开）；
//   * 第 2 行两个字的起点 3400 与 3450 只差 5px（取最近才抓得准）。
// 旧实现是"遍历里最后命中的那条"（≈ 画得最晚的），这两种都会抓错。
const doc = {
  groups: [
    group(1000, 'ab', [word('a', 1000), word('b', 1100)], 1100),
    group(3000, 'cde', [word('c', 3000), word('d', 3400), word('e', 3450)], 3600),
    group(3600, 'fg', [word('f', 3600), word('g', 3700)], 3900),
  ],
};
const wave = new SaltWaveform(layer, {onSeek() {}, onChange() {}, onSelect() {}});
wave.pps = 100;
wave.offset = 0;
wave.setDocument(doc);
wave.paint();

const BAND_Y = 170;                        // 主轨命中区是 y-5 .. y+37
const GRAB = SaltWaveform.TICK_GRAB;

// ---- 1) 同一像素上有两条：句尾（行的边界）优先于逐字 -------------------------
const both = wave.hitAt(110, BAND_Y);
check('同一像素上抓到的是「能抓的那条」（句尾优先于逐字）',
      both && both.kind === 'end', both && both.kind);
const again = wave.hitAt(110, BAND_Y);
check('同一位置重复问，答案永远一样',
      again && both && again.kind === both.kind && again.time === both.time,
      again && (again.kind + '@' + again.time));

// ---- 2) 上一行的句尾与下一行的句首同处一像素：按类型定序，不许随绘制顺序漂 -----
const shared = wave.hitAt(360, BAND_Y);
check('句尾与下一行句首同处一像素时给句尾（不随绘制顺序变）',
      shared && shared.kind === 'end' && shared.time === 3600,
      shared && (shared.kind + '@' + shared.time));
check('同一像素的答案也是稳定的', (wave.hitAt(360, BAND_Y) || {}).kind === 'end',
      (wave.hitAt(360, BAND_Y) || {}).kind);

// ---- 3) 两条挨着时取最近 ----------------------------------------------------
const nearest = wave.hitAt(342, BAND_Y);
check('取最近的一条（3400 那个字，而不是 3450）',
      nearest && nearest.kind === 'word' && nearest.time === 3400,
      nearest && (nearest.kind + '@' + nearest.time));
const other = wave.hitAt(348, BAND_Y);
check('偏到另一侧时换人', other && other.kind === 'word' && other.time === 3450,
      other && (other.kind + '@' + other.time));

// ---- 3) 抓取半径就是 ±TICK_GRAB --------------------------------------------
check('正好在半径上还抓得到', !!wave.hitAt(340 - GRAB, BAND_Y), 'x=' + (340 - GRAB));
check('越过半径就抓不到（附近没有别的刻度）',
      wave.hitAt(340 - GRAB - 4, BAND_Y) === null,
      JSON.stringify(wave.hitAt(340 - GRAB - 4, BAND_Y)));
check('命中的 x 就在刻度上', Math.abs((nearest || {}).x - 340) < 0.001, (nearest || {}).x);

// ---- 4) 粗细：句首/句尾比逐字边界粗 ----------------------------------------
const widthsAt = (x) => RECORDED.filter((r) => r.op === 'line' && Math.abs(r.x - x) < 0.001)
                                .map((r) => r.width);
const lineWidths = widthsAt(110);          // 句尾（也正好是那个字的起点）
const wordWidths = widthsAt(340);          // 逐字边界
check('句首/句尾用更粗的线',
      lineWidths.length > 0 && Math.max.apply(null, lineWidths) === SaltWaveform.TICK_WIDTH_LINE,
      JSON.stringify(lineWidths));
check('逐字边界用细线',
      wordWidths.length > 0 && Math.max.apply(null, wordWidths) === SaltWaveform.TICK_WIDTH_WORD,
      JSON.stringify(wordWidths));
check('行的边界确实比逐字粗',
      SaltWaveform.TICK_WIDTH_LINE > SaltWaveform.TICK_WIDTH_WORD,
      SaltWaveform.TICK_WIDTH_LINE + ' vs ' + SaltWaveform.TICK_WIDTH_WORD);

// ---- 5) 指针压着的那条比两者都粗、且换了颜色 --------------------------------
const staticHoverColour = RECORDED.filter((r) => r.op === 'line' && Math.abs(r.x - 340) < 0.001)
                                 .map((r) => r.colour).filter(Boolean).pop() || '';
RECORDED.length = 0;
wave.pointer = wave.hitAt(340, BAND_Y);
wave.paintPointer(makeContext());
const hovered = RECORDED.filter((r) => r.op === 'line' && Math.abs(r.x - 340) < 0.001);
const hoverWidth = hovered.length ? Math.max.apply(null, hovered.map((r) => r.width)) : 0;
check('悬停的那条比静态层更粗',
      hoverWidth > SaltWaveform.TICK_WIDTH_LINE,
      hoverWidth + ' vs ' + SaltWaveform.TICK_WIDTH_LINE);
check('悬停的那条换了颜色（不只靠粗细）',
      hovered.some((r) => r.colour && r.colour !== staticHoverColour),
      JSON.stringify(hovered.map((r) => r.colour)) + ' 静态层是 ' + JSON.stringify(staticHoverColour));
check('悬停还会画一个更宽的抓手方块',
      RECORDED.some((r) => r.op === 'rect' && r.w >= 4), '没有宽度 ≥4 的 rect');

console.log(failures ? ('刻度检查失败：' + failures + ' 项') : '刻度检查通过');
process.exit(failures ? 1 : 0);
