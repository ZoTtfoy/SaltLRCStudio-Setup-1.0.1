// 渲染真实观感：把打桩 canvas 记下的矩形导出成 JSON。
// 覆盖两条绘制路径 × 两个轨道：大波形 paintLayer、迷你条 paintStripBackground。
const path = require('path');

global.window = {devicePixelRatio: 1, addEventListener() {},
  fetch: () => Promise.resolve({ok: true, status: 200, json: async () => ({}), text: async () => ''})};
global.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} };
global.requestAnimationFrame = (cb) => setTimeout(() => cb(0), 0);
global.cancelAnimationFrame = () => {};
global.performance = {now: () => 0};

const rects = [];
const makeCtx = () => new Proxy({
  fillRect: (x, y, w, h) => rects.push({x, y, w, h, style: null, kind: 'fillRect'}),
  rect: (x, y, w, h) => rects.push({x, y, w, h, style: null, kind: 'rect'}),
  measureText: (t) => ({width: String(t).length * 7}),
}, {get(t, p) { return p in t ? t[p] : () => {}; }, set(t, p, v) { t[p] = v; return true; }});
const layer = {
  width: 660, height: 290, clientWidth: 660, parentElement: {clientWidth: 660},
  getContext: makeCtx, style: {}, addEventListener() {},
  getBoundingClientRect: () => ({left: 0, top: 0, width: 660, height: 290}),
};
global.document = {
  addEventListener() {}, createElement: () => layer,
  querySelector: () => layer, querySelectorAll: () => [], getElementById: () => layer,
};

const load = (name) => require('fs').readFileSync(
  path.join(__dirname, '..', 'saltlrc', 'web', name), 'utf8');
const SaltWaveform = new Function(load('theme.js') + '\n' + load('waveform.js') + '\nreturn SaltWaveform;')();
const wave = new SaltWaveform(layer, {onSeek: () => {}, onChange: () => {}, onSelect: () => {}});
wave.pps = 100;
wave.offset = 0;

// 真实情形：翻译每行都有；行距 2s（< 2.5s，会触发固定回退）与 4s 混合；
// 第 4 行故意没有翻译（翻译缺失很常见）。
const line = (start, main, trans) => ({
  start,
  main: {text: main, start, end: null, words: []},
  translation: {text: trans || '', start, end: null, words: []},
});
const groups = [];
for (let i = 0; i < 70; i++) {
  const at = 5000 + i * 3200;            // 4 分钟的曲子，每行约 3.2 秒
  groups.push(line(at, 'line ' + i, i === 30 ? '' : '翻译 ' + i));
}
groups.push(line(5000 + 70 * 3200 + 3000, '', ''));
wave.setDocument({groups});

wave.paintLayer(660, 290, 1, 660, 290, 0, undefined, wave.panSpan());
const bigwave = rects.slice();
rects.length = 0;
wave.stripLayer = {width: 0, height: 0, getContext: makeCtx};
wave.paintStripBackground(660, 44, 1, 660, 44);
const strip = rects.slice();

console.log(JSON.stringify({bigwave, strip}, null, 1));
