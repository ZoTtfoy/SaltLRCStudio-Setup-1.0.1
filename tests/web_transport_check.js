// 走带条：倍速 + 「再次播放从暂停点开始」。
//
// 用户："我需要倍速功能，还有每次播放后暂停，再次播放从暂停点开始，
//        并加一个这个选项的开关在设置里"。
//
// 为什么不是"浏览器本来就会这样"：<audio> 自己的 currentTime 在两种情况下
// **不是证据** —— 换音源（removeAttribute('src') + load()）会把它清零，
// 解码/加载还没到位时它也可能是 0。所以"下次从哪播放"必须由我们自己记着，
// 由页面显式写回去。这个检查就是钉住这一点：
//
//   * 暂停后即使元素读数被清零，按播放也要回到暂停点；
//   * 设置里关掉开关 → 按播放从 0 开始（暂停不算位置）；
//   * 用户显式点过的位置在两种模式下都算数；
//   * 播到结尾 → 标记清掉，下次从 0（否则会把播放头拖回结尾，看着像卡住）；
//   * 倍速同时写 playbackRate 与 defaultPlaybackRate —— 换音源时浏览器会把
//     前者重置回后者，只写一个的话每选一首歌倍速就悄悄变回 1×。
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
    replaceChildren(...items) { this.children = items; },
    after() {}, before() {}, insertBefore() {}, remove() {},
    addEventListener(type, fn) { (this._ev ||= {})[type] = fn; },
    removeEventListener() {}, setPointerCapture() {}, releasePointerCapture() {},
    // 焦点相关：这一版要测"鼠标点完控件、空格还会不会去切换它"。
    // closest 指回自己（模拟"点到的就是这个控件"）。
    closest(sel) { return this._closest === undefined ? this : this._closest; },
    matches(sel) { return sel === ':focus-visible' ? !!this._focusVisible : false; },
    scrollIntoView() {}, select() {}, click() {},
    focus() { global.document.activeElement = this; },
    blur() { if (global.document.activeElement === this) global.document.activeElement = null; },
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
  activeElement: null,
  _ev: {},
  addEventListener(type, fn) { (this._ev[type] ||= []).push(fn); },
  body: element('body'),
};
global.ResizeObserver = class {observe() {}};
global.requestAnimationFrame = () => 1;
global.cancelAnimationFrame = () => {};
global.navigator = {};
global.confirm = () => false;
global.alert = () => {};
global.Blob = class {constructor() {}};
URL.createObjectURL = () => 'blob:x';
URL.revokeObjectURL = () => {};
global.TextEncoder = require('util').TextEncoder;
global.performance = {now: () => 0};

// 一个可控的 <audio>：什么时候"浏览器把它清零了"由测试说了算。
const audio = element('audio');
audio.src = 'blob:x';
audio.paused = true;
audio.ended = false;
audio.duration = 227;
audio.preservesPitch = true;
audio.playbackRate = 1;
audio.defaultPlaybackRate = 1;
Object.defineProperty(audio, 'currentTime', {
  get() { return this._t || 0; },
  set(v) { this._t = v; this.seconds = v; },
  configurable: true,
});
// 真实的 play()/pause() 会翻转 paused 并触发事件 —— 这里照做，好让页面的事件
// 处理器（记暂停点的地方）真的被调用。
// 页面用 onplay/onpause/onended 属性接的，所以假元素也照真实事件那样回调它们 ——
// 否则"暂停点"那条只在 togglePlay 里被记到，页面自己的事件通路就没被测到。
audio.play = () => {
  if (audio.ended) audio.ended = false;          // 浏览器在 ended 后 play() 会回到最早位置
  audio.paused = false;
  audio.onplay?.();
  return Promise.resolve();
};
audio.pause = () => {
  if (audio.paused) return;
  audio.paused = true;
  audio.onpause?.();
};
audio.load = () => { audio.currentTime = 0; };

global.fetch = () => Promise.resolve({ok: true, json: () => Promise.resolve({errors: [], items: [], groups: [], document: {groups: []}})});

const load = (f) => fs.readFileSync(path.join(web, f), 'utf8');
const SCRIPTS = require('./_page_scripts.js')('index.html');
let api;
try {
  api = new Function(SCRIPTS.map(load).join('\n') + `
  return {audio: $('audio'), speed: $('speed'), repeat: $('repeat'), togglePlay,
          applySpeed, restoreSpeed, snapSpeed, playbackStart, playbackAnchor,
          resumeAfterPause, repeatSegment, markPlaybackPosition, notePausePoint,
          SPEEDS, SPEED_KEY, wave,
          // 两个开关共用一个 features 对象，所以按名字改值，别整个替换掉。
          setFeature: (name, on) => {
            const features = Object.assign({}, (appSettings && appSettings.features) || {});
            features[name] = on;
            appSettings = Object.assign({}, appSettings || {}, {features});
          },
          getMark: () => playbackMark,
          getSegment: () => segmentStart,
          // 键盘这一层（空格 = 播放/暂停，还是被浏览器拿去切换焦点控件）
          isTypingTarget, keyboardFocus,
          keydown: (event) => document._ev.keydown.forEach((fn) => fn(event)),
          clickOn: (target) => document._ev.click.forEach((fn) => fn({target})),
          setAppSettings: (value) => { appSettings = value; },
          activeElement: () => global.document.activeElement};`)();
} catch (e) { console.log('BOOT FAILED: %s', e.stack || e.message); process.exit(1); }

const tick = () => new Promise((r) => setImmediate(r));

(async () => {
  await tick();

  // ---- 倍速 ---------------------------------------------------------------
  check('倍速选项由 SPEEDS 一处生成（下拉已填充）', api.speed.children.length, api.SPEEDS.length);
  check('倍速选项的文案', api.speed.children.map((o) => o.textContent),
        ['0.25×', '0.5×', '0.75×', '1×（原速）', '1.25×', '1.5×', '2×', '3×']);

  check('applySpeed 写 playbackRate', api.applySpeed(1.5, true), 1.5);
  check('元素上的 playbackRate', api.audio.playbackRate, 1.5);
  // 关键：换音源（load()）时浏览器把 playbackRate 重置成 defaultPlaybackRate。
  // 只写前者 = 每选一首歌倍速悄悄回到 1×。
  check('同时写 defaultPlaybackRate（换音源不会被重置）', api.audio.defaultPlaybackRate, 1.5);
  check('慢放不变调', api.audio.preservesPitch, true);
  check('下拉跟着走', api.speed.value, '1.5');
  check('倍速被记住（下次启动还用它）', store[api.SPEED_KEY], '1.5');

  // 模拟浏览器换音源：playbackRate 回到 defaultPlaybackRate。
  api.audio.playbackRate = api.audio.defaultPlaybackRate;
  check('换音源后倍速仍是 1.5（靠 defaultPlaybackRate 兜住）', api.audio.playbackRate, 1.5);

  check('非法值吸附到最近的档位', api.snapSpeed(0.6), 0.5);
  check('0 或垃圾值 → 原速', api.snapSpeed(0), 1);
  check('超过最大档 → 最大档', api.snapSpeed(99), 3);
  api.applySpeed(1, true);
  check('回到原速', api.audio.playbackRate, 1);

  // ---- 暂停点 -------------------------------------------------------------
  api.setFeature('resumeAfterPause', true);
  api.audio.src = 'blob:x';
  api.audio.currentTime = 0;
  api.audio.paused = true;

  api.togglePlay();
  check('按播放：开始播放', api.audio.paused, false);
  check('从头开始（还没有任何标记）', api.audio.currentTime, 0);

  api.audio.currentTime = 30.4;
  api.togglePlay();                                   // 暂停
  check('按暂停：停住了', api.audio.paused, true);
  check('暂停点被记住', api.getMark(), 30.4);

  // 这就是用户报的那个场景：换音源/重新解码会把元素读数清零。
  api.audio.currentTime = 0;
  api.togglePlay();                                   // 再播放
  check('再次播放：回到暂停点，而不是从头', api.audio.currentTime, 30.4);
  check('而且真的在播', api.audio.paused, false);

  // 播放中按暂停再按播放，位置不动（不重复 seek）。
  api.audio.currentTime = 41.2;
  api.togglePlay();
  api.togglePlay();
  check('暂停点更新到 41.2', api.audio.currentTime, 41.2);

  // ---- 设置里关掉开关 ------------------------------------------------------
  api.audio.paused = true;
  api.audio.currentTime = 12.5;
  api.setFeature('resumeAfterPause', false);
  api.togglePlay();          // 播放
  api.audio.currentTime = 20.0;
  api.togglePlay();          // 暂停 —— 关掉开关时暂停不算位置
  check('关掉开关：暂停不记位置', api.getMark(), null);
  api.audio.currentTime = 0; // 元素读数被清零
  api.togglePlay();          // 再播放
  check('关掉开关：从这首开头开始', api.audio.currentTime, 0);

  // 显式位置在两种模式下都算数：否则"点一行 → 按播放"会被开关吃掉。
  api.markPlaybackPosition(64, '定位到', true);
  api.audio.paused = true;
  api.togglePlay();
  check('关掉开关时，用户显式点过的位置仍然算数', api.audio.currentTime, 64);

  api.setFeature('resumeAfterPause', true);

  // ---- 反复播放：暂停后再按播放，回到"这次开始的位置"重播 -------------------
  //
  // 用户："我还需要在任意时间轴的任意位置（指针指定）播放后暂停再次播放时跳回
  // 播放前的时间轴位置继续播放，人话就是反复播放，另外这两个功能一定会冲突，
  // 请你做好逻辑运算"。冲突是真的 —— 同一个手势（暂停后按播放）两种答案。
  // 定下来的优先级：**反复播放优先**（开着它时「从暂停点继续」不参与判断）。
  api.setFeature('repeatSegment', true);        // 两个都开，才能验出优先级
  check('反复播放：开关读回来是开的', api.repeatSegment(), true);

  // 指针指定任意位置：点波形空白 / 点贴条 / 点表格某一行走的都是这条路。
  api.wave.options.seek(12);
  check('指针指定位置：读数是 12s', api.audio.currentTime, 12);
  check('指针指定位置：上一段作废（下次按播放重新取起点）', api.getSegment(), null);

  api.audio.paused = true;
  api.togglePlay();
  check('反复播放：第一次从指针指定的 12s 起播', api.audio.currentTime, 12);
  check('反复播放：这一段起点记为 12s', api.getSegment(), 12);

  api.audio.currentTime = 25;                   // 听了一会儿
  api.togglePlay();                             // 暂停 → 暂停点是 25s
  check('暂停点照旧记下来（关掉反复后那条规则还能用它）', api.getMark(), 25);
  api.audio.currentTime = 0;                    // 换音源 / 重新加载会把读数清零
  api.togglePlay();                             // ★ 关键：再按播放
  check('反复播放：暂停后再按播放跳回这一段起点，而不是暂停点', api.audio.currentTime, 12);

  api.audio.currentTime = 30;
  api.togglePlay();
  api.audio.currentTime = 0;
  api.togglePlay();
  check('反复播放：可以一直反复（再来一次仍是这一段起点）', api.audio.currentTime, 12);

  // 任意位置、指针指定 → 换一段重播
  api.wave.options.seek(64);
  api.audio.paused = true;
  api.togglePlay();
  check('反复播放：换个指针位置就换一段（64s 起）', api.audio.currentTime, 64);
  check('新的这一段起点是 64s', api.getSegment(), 64);

  // 两个都开时，由「反复」说了算（这就是"逻辑运算"的结果）
  api.audio.currentTime = 70;
  api.togglePlay();                             // 暂停于 70s
  api.audio.paused = true;
  api.audio.currentTime = 0;
  api.togglePlay();
  check('两个都开：以「反复」为准（回 64s，不是暂停点 70s）', api.audio.currentTime, 64);

  // 关掉「反复」→ 立刻回到「从暂停点继续」
  api.setFeature('repeatSegment', false);
  api.audio.paused = false;
  api.audio.currentTime = 90;
  api.togglePlay();                             // 暂停 → 暂停点 90s
  check('关掉反复后：暂停点重新生效', api.getMark(), 90);
  api.audio.currentTime = 0;
  api.audio.paused = true;
  api.togglePlay();
  check('关掉反复后：回到「从暂停点继续」（90s）', api.audio.currentTime, 90);
  api.setFeature('repeatSegment', true);        // 后面验「结尾清两个槽位」

  // ---- 播到结尾 ------------------------------------------------------------
  api.audio.currentTime = 226.9;
  api.audio.paused = false;
  api.audio.ended = true;
  api.audio.onended?.();                        // 浏览器会发 ended
  check('播到结尾：标记清掉', api.getMark(), null);
  check('播到结尾：这一段的起点也清掉（否则会被拖回结尾）', api.getSegment(), null);
  api.audio.paused = true;
  api.audio.currentTime = 0;                    // 规范：ended 之后 play() 回到最早位置
  api.togglePlay();
  check('播到结尾后再播放：从 0 开始（不会卡在结尾）', api.audio.currentTime, 0);
  api.setFeature('repeatSegment', false);

  // ---- 没有音频时不装作能播 ------------------------------------------------
  api.audio.src = '';
  api.audio.paused = true;
  api.audio.currentTime = 0;
  api.togglePlay();
  check('没有音频：不播放（并会在状态行出声）', api.audio.paused, true);
  check('没有音频：位置读数不被改写', api.audio.currentTime, 0);

  // ---- 空格 = 播放/暂停，而不是"切换当前有焦点的控件" ----------------------
  //
  // 用户："现在因为是网页端结构，空格键会有改变选定按键状态的功能，请帮我修复这个bug"。
  // 网页里空格天生还有第二个含义：激活有焦点的控件。点一次「反复」之后焦点留在那个
  // 勾选框上，按空格切换的就是它 —— 播放/暂停反而不响应。
  //
  // 两道一起上（缺一不可）：
  //   ① `isTypingTarget` 只认文本输入 —— 勾选框/下拉不再被当成"正在打字"；
  //   ② 鼠标点完非输入控件就把焦点放掉（`:focus-visible` 为假才放），
  //      这一道不依赖"preventDefault 能不能取消浏览器对空格的激活"。
  // 键位表：页面从 /api/settings 读，这个夹具的 fetch 打桩返回 {} ——
  // 所以得先给一份真实绑定，否则 actionFor() 找不到 playPause（第一版就是这么假红的）。
  api.setAppSettings({features: {resumeAfterPause: true, repeatSegment: false},
                      keys: {playPause: 'Space', tap: 'Enter', undo: 'Ctrl+Z', redo: 'Ctrl+Y',
                             help: 'F1', aiTranslate: 'Ctrl+T'}});
  const spaceEvent = () => ({key: ' ', code: 'Space', ctrlKey: false, shiftKey: false,
                             altKey: false, metaKey: false, target: element('body'),
                             prevented: false,
                             preventDefault() { this.prevented = true; }});

  // ① 判定本身
  check('文本框算"正在输入"', api.isTypingTarget({tagName: 'INPUT', type: 'text'}), true);
  check('密码框算"正在输入"', api.isTypingTarget({tagName: 'INPUT', type: 'password'}), true);
  check('勾选框**不**算"正在输入"（原来算错了）',
        api.isTypingTarget({tagName: 'INPUT', type: 'checkbox'}), false);
  check('下拉**不**算"正在输入"', api.isTypingTarget({tagName: 'SELECT'}), false);
  check('按钮不算"正在输入"', api.isTypingTarget({tagName: 'BUTTON'}), false);
  check('文本域算"正在输入"', api.isTypingTarget({tagName: 'TEXTAREA'}), true);

  // ② 焦点在勾选框上（鼠标点的，不是 Tab 来的）→ 空格必须去播放，并挡住浏览器的激活
  api.audio.src = 'blob:x';
  api.audio.paused = true;
  api.audio.currentTime = 0;
  const repeatBox = api.repeat;
  repeatBox.tagName = 'INPUT';
  repeatBox.type = 'checkbox';
  repeatBox._focusVisible = false;               // 鼠标点出来的焦点
  repeatBox.focus();
  check('前置：焦点确实在勾选框上', api.activeElement() === repeatBox, true);
  const space1 = spaceEvent();
  const before1 = api.audio.paused;
  api.keydown(space1);
  check('空格：挡住了浏览器对勾选框的激活（preventDefault）', space1.prevented, true);
  check('空格：播放状态被切换了', api.audio.paused !== before1, true);

  // ③ 焦点在文本框上 → 空格归输入框（不能抢）
  const textBox = element('aTextInput');
  textBox.tagName = 'INPUT';
  textBox.type = 'text';
  textBox.focus();
  api.audio.paused = true;
  const space2 = spaceEvent();
  api.keydown(space2);
  check('文本框里空格不抢（不 preventDefault）', space2.prevented, false);
  check('文本框里空格不播放', api.audio.paused, true);

  // ④ Tab 走到勾选框（:focus-visible）→ 空格归那个控件（不砸无障碍）
  repeatBox._focusVisible = true;
  repeatBox.focus();
  const space3 = spaceEvent();
  api.keydown(space3);
  check('Tab 到勾选框时空格让给它（不 preventDefault）', space3.prevented, false);
  check('Tab 到勾选框时空格不播放', api.audio.paused, true);
  repeatBox._focusVisible = false;

  // ⑤ 兜底那一道：鼠标点完非输入控件就把焦点放掉
  repeatBox.focus();
  api.clickOn(repeatBox);
  check('鼠标点完勾选框：焦点被放掉（空格就没有第二个含义了）',
        api.activeElement() === null, true);

  const tabBox = element('tabbedCheckbox');
  tabBox.tagName = 'INPUT';
  tabBox.type = 'checkbox';
  tabBox._focusVisible = true;
  tabBox.focus();
  api.clickOn(tabBox);
  check('键盘(Tab)焦点不会被放掉（无障碍保留）', api.activeElement() === tabBox, true);
  tabBox._focusVisible = false;

  const field = element('someTextField');
  field.tagName = 'INPUT';
  field.type = 'text';
  field.focus();
  api.clickOn(field);
  check('点文本框不放焦点（还要继续打字）', api.activeElement() === field, true);
  field.blur();

  console.log('\n%d checks, %d failed', checks, failures);
  process.exit(failures ? 1 : 0);
})();
