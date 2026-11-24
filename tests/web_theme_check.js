// 主题行为检查：切换、缓存、以及"换主题后静态层必须重画"。
//
// 最后一条是关键：波形把静态层画一次就缓存起来，判据里如果漏掉主题，
// 换主题后画面会停在旧颜色上 —— 而且不会报错，只会看着不对。
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

// ---- 最小的浏览器环境：本地存储、页面根节点、以及"CSS 令牌取值" -------------
const store = new Map();
const handlers = new Map();
let styleReads = 0;
const TOKENS = {
  '--wave-bg': ' #102e35 ',
  '--wave-cursor': '#f5f1e4',
  '--wave-cell-main': '#285c53',
};
const themeValues = {dark: TOKENS, light: {...TOKENS, '--wave-bg': '#dbe7e5'}};

global.localStorage = {
  getItem: (key) => (store.has(key) ? store.get(key) : null),
  setItem: (key, value) => store.set(key, String(value)),
};
global.document = {
  documentElement: {dataset: {}},
  addEventListener: (type, fn) => { handlers.set(type, fn); },
  dispatchEvent: (event) => { const fn = handlers.get(event.type); if (fn) fn(event); return true; },
  getElementById: () => null,
  querySelectorAll: () => [],
  createElement: () => ({style: {}, width: 0, height: 0, getContext: () => makeCtx()}),
};
global.CustomEvent = class { constructor(type, init) { this.type = type; this.detail = (init || {}).detail; } };
global.window = {devicePixelRatio: 1, addEventListener() {}};
global.performance = {now: () => 0};
global.requestAnimationFrame = (cb) => setTimeout(() => cb(0), 0);
global.cancelAnimationFrame = () => {};
global.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} };
global.getComputedStyle = () => ({
  getPropertyValue: (name) => {
    styleReads += 1;
    const table = themeValues[document.documentElement.dataset.theme === 'light' ? 'light' : 'dark'];
    return table[name] || '';
  },
});

const Theme = new Function(load('theme.js') + '\nreturn Theme;')();

// ---- 1) 默认深色；切换会写属性、写缓存、广播一次 ----------------------------
check('默认是深色', Theme.current() === 'dark', Theme.current());
let announced = 0;
handlers.clear();
document.addEventListener('themechange', () => { announced += 1; });
Theme.apply('light');
check('切换后属性是 light', Theme.current() === 'light', Theme.current());
check('切换后写入首屏缓存',
      localStorage.getItem(Theme.storageKey) === 'light',
      String(localStorage.getItem(Theme.storageKey)));
check('切换会广播一次', announced === 1, announced);
Theme.apply('dark');
check('非 light 的值一律当深色', Theme.current() === 'dark', Theme.current());

// ---- 2) 令牌读一次就缓存，主题变化后必须重读 --------------------------------
Theme.apply('dark');
styleReads = 0;
Theme.token('--wave-bg');
Theme.token('--wave-bg');
Theme.token('--wave-cursor');
check('同一个令牌只读一次', styleReads === 2, styleReads);
check('读到的是去掉空白的值', Theme.token('--wave-bg') === '#102e35', Theme.token('--wave-bg'));
Theme.apply('light');
const afterSwitch = Theme.token('--wave-bg');
check('换主题后重读，拿到新主题的值', afterSwitch === '#dbe7e5', afterSwitch);

// ---- 3) 换主题必须让静态层重画（判据里含主题） -------------------------------
const rects = [];
const makeCtx = () => new Proxy({
  fillRect: (x, y, w, h) => rects.push({x, y, w, h}),
  rect: (x, y, w, h) => rects.push({x, y, w, h}),
  measureText: (text) => ({width: String(text).length * 7}),
}, {get: (target, key) => (key in target ? target[key] : () => {}),
    set: (target, key, value) => { target[key] = value; return true; }});
const layer = {
  width: 660, height: 290, clientWidth: 660, parentElement: {clientWidth: 660},
  getContext: makeCtx, style: {}, addEventListener() {},
  getBoundingClientRect: () => ({left: 0, top: 0, width: 660, height: 290}),
};
const SaltWaveform = new Function(
  load('theme.js') + '\n' + load('waveform.js') + '\nreturn SaltWaveform;')();
const wave = new SaltWaveform(layer, {onSeek() {}, onChange() {}, onSelect() {}});
wave.pps = 100;
wave.offset = 0;
wave.setDocument({groups: [{
  start: 1000,
  main: {text: 'abc', start: 1000, end: null, words: []},
  translation: {text: '', start: 1000, end: null, words: []},
}]});

Theme.apply('dark');
wave.paint();
const first = wave.stats.layerPaints;
wave.paint();
check('没有变化时只做 blit（不重画图层）', wave.stats.layerPaints === first, wave.stats.layerPaints);
Theme.apply('light');
wave.paint();
check('换主题后第一次绘制必须重画静态层',
      wave.stats.layerPaints === first + 1, wave.stats.layerPaints);
Theme.apply('dark');
wave.invalidate();
wave.paint();
check('换回来也要重画', wave.stats.layerPaints === first + 2, wave.stats.layerPaints);

console.log(failures ? ('主题检查失败：' + failures + ' 项') : '主题检查通过');
process.exit(failures ? 1 : 0);
