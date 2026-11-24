// 智能贴唱页：一个按钮 + 一个**可选**预览，以及"预览时时间轴读哪一份"。
//
// 用户："帮我完全重构识别对齐功能，其运行逻辑为导入歌词（不管是本地还是联网搜索）→
//        按照本地模型对其进行智能贴唱 → 可选同步预览到波形编辑界面，
//        重构其识别方式和识别逻辑"。
// 选定的口径：纯本机能量分析 / **只贴整行** / 预览要能**播放着核对** / 旧的三个按钮并成一个。
//
// 算法本身由 `tests/test_fit.py`（真值夹具）与 `tests/test_align_routes.py`（真 HTTP）验；
// 这个检查盯的是**页面这一层**：
//   ① 点「智能贴唱」真的发请求、拿结果、写进状态与列表；
//   ② 预览开着时，波形与"按时间找行"都读**贴后**那一份（否则高亮/表格各说各话）；
//   ③ 关掉预览就回到原稿；
//   ④ 「应用贴唱」写进文档（可撤销）并清掉预览层；
//   ⑤ 「丢弃」什么都不改；
//   ⑥ 贴唱之后又改了歌词 → 不许应用（免得把后来的编辑盖掉）；
//   ⑦ 失败（没音频 / 接口报错）必须在这一页说出来。
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
    disabled: false, max: 300, children: [], checked: false, open: false, tagName: 'DIV',
    parentElement: {clientWidth: 900, clientHeight: 300, appendChild() {},
                    classList: {add() {}, remove() {}, toggle() {}, contains() { return false; }}},
    clientWidth: 900, clientHeight: 300,
    classList: {add() {}, remove() {}, toggle() {}, contains() { return false; }},
    append(...items) { this.children.push(...items); },
    prepend(...items) { this.children.unshift(...items); },
    replaceChildren(...items) { this.children = items; },
    after() {}, before() {}, insertBefore() {}, remove() {},
    addEventListener(type, fn) { (this._ev ||= {})[type] = fn; },
    removeEventListener() {}, setPointerCapture() {}, releasePointerCapture() {},
    closest() { return this; }, matches() { return false; }, contains() { return false; },
    scrollIntoView() {}, focus() {}, blur() {}, select() {}, click() {},
    removeAttribute() {}, setAttribute() {}, getAttribute() { return null; },
    getContext: () => ctx2d(),
    querySelector: () => element('q:' + id + ':' + (seq++)),
    querySelectorAll: () => [],
    getBoundingClientRect() { return {left: 0, top: 0, right: 900, bottom: 300, width: 900, height: 300}; },
  };
  if (id === 'fitPreview') el.checked = true;      // 与页面一致：默认打开
  if (id === 'sourceTrack') el.value = 'auto';
  if (id === 'maxShift') el.value = '8000';
  cache[id] = el;
  return el;
}
const store = {};
global.localStorage = {getItem: (k) => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); }, removeItem: (k) => { delete store[k]; }};
global.sessionStorage = {getItem: () => null, setItem() {}, removeItem() {}};
global.window = {devicePixelRatio: 1, addEventListener() {}, fetch: () => Promise.resolve({ok: true, json: async () => ({})})};
global.location = {href: 'http://127.0.0.1/studio.html', search: '', pathname: '/studio.html'};
global.document = {
  getElementById: element,
  createElement: (t) => { const e = element('new:' + t + ':' + (seq++)); e.tagName = t.toUpperCase(); return e; },
  createTextNode: (text) => ({textContent: text}),
  querySelector: () => element('qs:' + (seq++)),
  querySelectorAll: () => [],
  activeElement: null,
  _ev: {},
  addEventListener(type, fn) { (this._ev[type] ||= []).push(fn); },
  body: element('body'),
};
global.ResizeObserver = class {observe() {}};
global.requestAnimationFrame = () => 1;
global.cancelAnimationFrame = () => {};
global.navigator = {};
global.confirm = () => true;
global.alert = () => {};
global.Blob = class {constructor(parts) { this.parts = parts; }};
URL.createObjectURL = () => 'blob:x';
URL.revokeObjectURL = () => {};
global.TextEncoder = require('util').TextEncoder;
global.performance = {now: () => 0};

// 桩 fetch：按路由给回复，并记下每次调用（路由 + body）
const calls = [];
const FIT_DOCUMENT = {groups: [
  {start: 2100, main: {text: '甲', words: [], start: 2100, end: 3300},
   translation: {text: '', words: [], start: null, end: null}},
  {start: 6600, main: {text: '乙', words: [], start: 6600, end: 7900},
   translation: {text: '', words: [], start: null, end: null}},
], metadata: {}, notes: [], unresolved: [], alignment: {}};
const FIT_REPORT = {
  field: 'main', duration_ms: 16000, onsets: 3, offsets: 3, moved: 2, weak: 0, kept: 0,
  notice: '本机能量贴唱：从音频里找到 3 个人声起点。2 行贴到起点。请预览后应用（可撤销）。',
  document: FIT_DOCUMENT,
  lines: [
    {index: 0, old_start: 3500, old_end: 5000, start: 2100, end: 3300, moved_ms: -1400,
     confidence: 'snapped', reason: '已贴到人声起点（移动 -1400 ms）'},
    {index: 1, old_start: 8000, old_end: 9500, start: 6600, end: 7900, moved_ms: -1400,
     confidence: 'snapped', reason: '已贴到人声起点（移动 -1400 ms）'},
  ],
};
let failFit = false;
global.fetch = (input, init) => {
  const url = typeof input === 'string' ? input : (input && input.url) || '';
  const route = url.includes('/api/') ? url.split('/api/')[1] : url;
  let body = null;
  try { body = init && init.body ? JSON.parse(init.body) : null; } catch { body = null; }
  calls.push({route, body});
  if (route === 'align/fit' && failFit) {
    return Promise.resolve({ok: false, status: 400,
                            json: async () => ({error: '没有检测到可靠的人声起点'}),
                            text: async () => ''});
  }
  const value = route === 'align/fit' ? FIT_REPORT
              : route === 'language' ? {main: {code: 'zh', confidence: .9}, translation: {code: 'ja', confidence: .9}}
              : {};
  return Promise.resolve({ok: true, status: 200, json: async () => value, text: async () => ''});
};
global.window.fetch = global.fetch;

const load = (f) => fs.readFileSync(path.join(web, f), 'utf8');
const SCRIPTS = require('./_page_scripts.js')('index.html');
let api;
try {
  api = new Function(SCRIPTS.map(load).join('\n') + `
  return {smartFit, timelineGroups, fitPreviewOn, applyFitPreview, clearFit, renderFitList,
          queue, current, setSelected: (i) => { selected = i; },
          push: (item) => { queue.push(item); },
          fitPreview: $('fitPreview'), fitState: $('fitState'), fitReport: $('fitReport'),
          fitList: $('fitList'), message: $('message'),
          handlers: {smartFit: $('smartFit').onclick, fitApply: $('fitApply').onclick,
                     fitDiscard: $('fitDiscard').onclick, fitPreview: $('fitPreview').onclick},
          wave, setAudioFile: (name, file) => { audioFiles.set(key(name), file); }};`)();
} catch (e) { console.log('BOOT FAILED: %s', e.stack || e.message); process.exit(1); }

// 假音频：audioPayload() 要 decode + pcm16，桩里给最小的可用替身。
const fakeFile = {name: 'x.wav', size: 100, arrayBuffer: async () => new ArrayBuffer(8)};
global.AudioContext = class { constructor() {} decodeAudioData() { return Promise.resolve({duration: 16}); } close() {} };
global.OfflineAudioContext = class {
  constructor() { this.destination = {}; }
  createBufferSource() { return {buffer: null, connect() {}, start() {}}; }
  createBuffer() { return {getChannelData: () => new Float32Array(16000 * 16)}; }
  startRendering() { return Promise.resolve({getChannelData: () => new Float32Array(16000 * 16)}); }
};

const tick = () => new Promise((r) => setImmediate(r));
const catchMessage = async (fn) => {
  try { await fn(); return 'no-throw'; } catch (error) { return error.message; }
};

(async () => {
  await tick();

  // ---- 前置：一首有歌词、有音频的歌 ----------------------------------------
  const document = {groups: [
    {start: 3500, main: {text: '甲', words: [], start: 3500, end: 5000},
     translation: {text: '', words: [], start: null, end: null}},
    {start: 8000, main: {text: '乙', words: [], start: 8000, end: 9500},
     translation: {text: '', words: [], start: null, end: null}},
  ], metadata: {}, notes: [], unresolved: [], alignment: {}};
  api.push({name: 'x.lrc', document, history: [], report: {errors: [], warnings: []}, timeline: null});
  api.setSelected(0);
  api.message.textContent = '';
  api.handlers.smartFit();                    // bind() 会捕获异常并写状态行（不往外抛）
  await tick();
  check('前置：没有音频时贴唱会拒绝并在状态行出声',
        api.message.textContent, '请先添加与歌词同名的音频');
  api.setAudioFile('x.lrc', fakeFile);

  // ---- ① 点按钮：发请求、拿结果、写状态 ------------------------------------
  calls.length = 0;
  api.message.textContent = '';
  await api.handlers.smartFit();
  const request = calls.find((c) => c.route === 'align/fit');
  check('点「智能贴唱」会请求 /api/align/fit', !!request, true);
  check('请求带上歌词、音频、轨道与最大移动量',
        !!(request.body.document && request.body.audio && request.body.field === 'auto'
           && request.body.max_shift === 8000), true);
  check('状态行说了结果与下一步', /预览/.test(api.message.textContent), true);
  // 必须**原样**是服务端那句说明。这里刻意用全等而不是 /人声起点/ 这类宽泛匹配：
  // 进度文案"正在听音频找人声起点…"里也有这四个字，宽泛匹配会被它满足 ——
  // 那就成了一条无论接线在不在都绿的空断言（v0.22.0 反向验证抓到的假绿）。
  check('页内状态原样写出服务端的说明', api.fitState.textContent,
        '本机能量贴唱：从音频里找到 3 个人声起点。2 行贴到起点。请预览后应用（可撤销）。');
  check('结果列表逐行列出（2 行）', api.fitList.children.length, 2);
  check('页内汇总栏有数字', /2 行贴到起点/.test(api.fitReport.textContent), true);

  // ---- ② 预览开着：时间轴读**贴后**的那一份 --------------------------------
  check('预览默认打开', api.fitPreviewOn(), true);
  check('预览时时间轴读贴后的 groups',
        api.timelineGroups(api.current()).map((g) => g.main.start), [2100, 6600]);
  check('波形上确实有 setFit 这个方法', typeof api.wave.setFit, 'function');
  check('波形拿到了要叠加的行（只传会动的两行）',
        (api.wave.fitLines || []).map((line) => line.start), [2100, 6600]);

  // ---- ③ 关掉预览：回到原稿 ------------------------------------------------
  api.fitPreview.checked = false;
  api.handlers.fitPreview();
  check('关掉预览后时间轴读原稿',
        api.timelineGroups(api.current()).map((g) => g.main.start), [3500, 8000]);
  check('关掉预览后波形不再叠加', api.wave.fitLines || null, null);
  check('关掉预览会说明白', /只看原稿/.test(api.message.textContent), true);
  api.fitPreview.checked = true;
  api.handlers.fitPreview();

  // ---- ④ 应用：写进文档（可撤销）并清掉预览 --------------------------------
  const before = api.current().document.groups.map((g) => g.main.start);
  await api.handlers.fitApply();
  const item = api.current();
  check('应用前是原稿', before, [3500, 8000]);
  check('应用后文档真的变了', item.document.groups.map((g) => g.main.start), [2100, 6600]);
  check('应用后 group.start 与 main.start 一致（句首只有一个事实）',
        item.document.groups.every((g) => g.start === g.main.start), true);
  check('应用后可撤销（历史里多了"智能贴唱"这一步）',
        item.timeline.steps[item.timeline.steps.length - 1].label, '智能贴唱');
  check('应用后预览层收掉', api.wave.fitLines || null, null);
  check('应用后不再保留 fit 结果', item.fit, undefined);

  // ---- ⑤ 丢弃：什么都不改 --------------------------------------------------
  item.document.groups[0].main.start = 3500;      // 回到原稿，再贴一次
  await api.handlers.smartFit();
  api.message.textContent = '';
  api.handlers.fitDiscard();
  check('丢弃不动文档', api.current().document.groups.map((g) => g.main.start), [3500, 6600]);
  check('丢弃会出声', /丢弃/.test(api.message.textContent), true);
  check('丢弃后没有预览层', api.wave.fitLines || null, null);

  // ---- ⑥ 贴唱之后改过歌词：不许应用（免得盖掉后来的编辑）------------------
  await api.handlers.smartFit();
  api.current().document.groups[0].main.text = '改过了';
  api.message.textContent = '';
  api.handlers.fitApply();                     // bind() 捕获后写状态行
  await tick();
  check('贴唱后改过歌词就不给应用', api.message.textContent,
        '贴唱之后歌词被改过（换行/改字），请重新贴唱，免得把后来的编辑盖掉');

  // ---- ⑦ 失败必须在页内说出来 ----------------------------------------------
  failFit = true;
  api.fitState.textContent = '';
  await catchMessage(() => api.handlers.smartFit());
  check('接口报错时页内说明原因', /贴唱没做成/.test(api.fitState.textContent), true);
  check('页内说明里带上了服务端的理由', /人声起点/.test(api.fitState.textContent), true);
  failFit = false;

  console.log('\n%d checks, %d failed', checks, failures);
  process.exit(failures ? 1 : 0);
})();
