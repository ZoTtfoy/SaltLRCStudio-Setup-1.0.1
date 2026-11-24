// Regression guard: load both pages in jsdom and assert every interactive
// element is wired. Two shipped bugs were exactly this shape — a script error
// aborted the binding loop, so buttons looked fine but did nothing.
// Usage: node tests/web_dom_check.js <base-url>
const { JSDOM, VirtualConsole } = require('jsdom');

const BASE = process.argv[2];
if (!BASE) {
  console.error('usage: node web_dom_check.js <base-url>');
  process.exit(2);
}

// A saved session, exactly as the editor writes it, for the launch check below.
const SAVED_SESSION = JSON.stringify({
  at: Date.now(),
  selected: 0,
  items: [
    {name: '上一轮甲.lrc', document: {groups: [
      {start: 0, main: {text: '甲', words: [], end: null, start: null},
       translation: {text: '', words: [], end: null, start: null}}]}},
    {name: '上一轮乙.lrc', document: {groups: [
      {start: 0, main: {text: '乙', words: [], end: null, start: null},
       translation: {text: '', words: [], end: null, start: null}}]}},
  ],
});

const PAGES = [
  // Readiness is gated on a global the page's *own* script defines at top level
  // (library.js declares `state`, studio.js declares `wave`); a name that does
  // not exist would never become true and the load would always time out.
  { url: '/library', ready: "typeof state !== 'undefined' && typeof buildOptions === 'function'",
    ids: ['save', 'exportFolder', 'build', 'batch', 'reload', 'search', 'appVersion',
          // login dialog: one entry point per authentication method
          'loginOpen', 'loginState', 'provider', 'providerNote',
          'tabQr', 'tabPassword', 'tabSms',
          'qrStart', 'pwSubmit', 'smsSend', 'smsSubmit', 'loginLogout', 'loginClose',
          // the word editor's action buttons: reported as "取消和应用不可用", and
          // they were not on this list, so losing their handler went unnoticed
          'applyWords', 'closeWordEdit'] },
  { url: '/studio.html', ready: "typeof wave !== 'undefined' || typeof current !== 'undefined'",
    seed: {'saltlrc-studio-session': SAVED_SESSION},
    ids: ['export', 'zip', 'save', 'check', 'undo', 'redo',
          'undoStep', 'redoStep', 'clearSession', 'add',
          // 「识别与对齐」页：v0.22.0 起只剩一个按钮 + 一个预览开关。
          // 注意只列"点一下就该立刻有反应"的控件：`#sourceTrack` / `#maxShift`
          // 是**点按钮时被读**的下拉与数字框，跟设置表单那些字段一样，
          // 接线不会被丢掉，所以不该在这份名单里（名单是给"失去处理函数就变哑巴"
          // 的控件用的）。
          'smartFit', 'fitApply', 'fitDiscard', 'fitPreview', 'saveTranscript',
          // 逐字编辑器里新增的「从音频检测这一行」
          'wordFromAudio',
          'pickLibrary', 'help', 'matchProvider',
          // our own transport, replacing the browser's native media bar
          'playPause', 'playTime', 'volume', 'volumeValue',
          // 倍速：试听速度（选项由 studio.js 的 SPEEDS 生成，所以它必须有 onchange）
          'speed',
          // 反复播放：走带条上的实时开关（onchange 驱动）
          'repeat',
          // session recovery is now an explicit action, not an auto-load
          'restoreSession',
          // settings + AI translation. Only the controls that act immediately
          // are listed here: the form fields are read when 保存 is pressed, the
          // same as #wordEndTime / #tail, so "has onclick" is not the question
          // to ask about them (see the field check further down).
          // the editor's own word-timing dialog
          'applyWords', 'closeWords',
          'settings', 'settingsClose', 'settingsSaved',
          'setTest', 'setClearKey', 'aiTranslate', 'aiTranslateAll',
          // 本地工程库：首次启动的弹窗 + 「检查与工程」页的入口。
          // 这一组曾经没有名字：控件不在名单里，接线丢了也不会有测试变红。
          'projectUse', 'projectBrowse', 'projectLater', 'projectChange'] },
];

// jsdom has no canvas implementation; the waveform legitimately needs one.
const NOISE = /Not implemented: HTMLCanvasElement|Could not parse CSS|Not implemented: window\./i;

const results = [];

function check(name, ok, detail = '') {
  results.push(ok);
  console.log((ok ? 'PASS ' : 'FAIL ') + name + (detail ? ' :: ' + detail : ''));
}

async function loadOnce(url, readyExpr, seed) {
  const errors = [];
  const virtualConsole = new VirtualConsole();
  virtualConsole.on('jsdomError', (e) => errors.push(e.message + '\n' + (e.stack || '')));
  virtualConsole.on('error', (m) => errors.push('console.error: ' + m));

  const dom = await JSDOM.fromURL(BASE + url, {
    runScripts: 'dangerously',
    resources: 'usable',
    pretendToBeVisual: true,
    virtualConsole,
    beforeParse(window) {
      window.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} };
      // jsdom 没有 canvas：换主题会让页面重画波形，而 getContext 返回 null。
      // 给一个空实现，"页面脚本有没有报错"这条判据才不会把 jsdom 缺的能力算进来。
      const stubContext = () => new Proxy({
        measureText: (text) => ({width: String(text).length * 7}),
      }, {get: (target, key) => (key in target ? target[key] : () => {}),
          set: (target, key, value) => { target[key] = value; return true; }});
      window.HTMLCanvasElement.prototype.getContext = stubContext;
      window.fetch = (input, init) => {
        const href = new URL(input, BASE).href;
        // The 退出 button POSTs /api/exit. The check server has to survive that,
        // so the request is answered locally — otherwise clicking exit in one
        // page would stop the backend the remaining pages still need.
        if (href.endsWith('/api/exit')) {
          return Promise.resolve({ok: true, json: () => Promise.resolve({status: 'exiting'})});
        }
        return fetch(href, init);
      };
      window.confirm = () => true;
      window.alert = () => {};
      window.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {} });
      // jsdom (30.x) implements the `open` property but NOT showModal/close, so
      // every dialog-opening path used to throw here — and because the "no script
      // errors" assertion runs before the interactions, the failure was invisible
      // and dialog behaviour was never actually exercised. Polyfilling them (the
      // same way confirm/matchMedia are stubbed) makes dialogs testable at all.
      const dialogProto = window.HTMLDialogElement && window.HTMLDialogElement.prototype;
      if (dialogProto && typeof dialogProto.showModal !== 'function') {
        dialogProto.showModal = function () { this.open = true; };
        dialogProto.show = function () { this.open = true; };
        dialogProto.close = function () { this.open = false; };
      }
      // A page may need a pre-existing saved session to exercise launch
      // behaviour; seeding has to happen before the page's scripts run.
      if (seed) for (const [k, v] of Object.entries(seed)) window.localStorage.setItem(k, v);
    },
  });
  // jsdom fetches <script> tags asynchronously with resources:'usable'. A
  // fixed sleep is a race: under load the last script is still in flight and
  // the page reports "Could not load script" — a false failure unrelated to
  // the code.
  //
  // Readiness is gated on the global the page's own script defines, not on
  // readyState: jsdom sets readyState to 'complete' even when a script fetch
  // is still outstanding, so waiting for it resolves too early and the check
  // then fails for reasons that have nothing to do with the page.
  const ready = await new Promise((resolve) => {
    const deadline = Date.now() + 60000;
    const poll = () => {
      let ok = false;
      try { ok = dom.window.eval(readyExpr); } catch { ok = false; }
      if (ok) setTimeout(() => resolve(true), 400);
      else if (Date.now() > deadline) resolve(false);
      else setTimeout(poll, 50);
    };
    poll();
  });
  return { dom, errors, ready };
}

// The whole suite running at once can leave the machine busy enough that the
// page's scripts never arrive inside the window, which is a test-environment
// property rather than a page defect: the same suite was measured at 88s once
// and 139s another time. Retry before calling it a failure.
async function load(url, readyExpr, seed, attempts = 3) {
  for (let attempt = 1; attempt <= attempts; attempt++) {
    const result = await loadOnce(url, readyExpr, seed);
    if (result.ready) return result;
    result.dom.window.close();
    if (attempt < attempts) {
      console.log(`(${url} 第 ${attempt} 次未在时限内加载，重试)`);
    }
  }
  return null;
}

(async () => {
  for (const page of PAGES) {
    let loaded;
    try {
      loaded = await load(page.url, page.ready, page.seed);
    } catch (e) {
      check(`${page.url} loads`, false, e.message);
      continue;
    }
    if (!loaded) {
      // Report the real cause instead of the confusing downstream symptoms
      // (missing elements, unwired buttons) that a half-loaded page produces.
      check(`${page.url} loads`, false, '脚本未在时限内加载完成');
      continue;
    }
    const { dom, errors } = loaded;
    const real = errors.filter((e) => !NOISE.test(e));
    check(`${page.url} loads without script errors`, real.length === 0, real.slice(0, 2).join(' | '));
    const doc = dom.window.document;
    for (const id of page.ids) {
      const el = doc.getElementById(id);
      if (!el) { check(`${page.url} #${id} exists`, false); continue; }
      // A <select> is driven by onchange and a label/status node may be a plain
      // span, so the wired check is per element kind rather than "has onclick".
      // A range input reports through oninput, not onclick — requiring onclick
      // there would fail a perfectly wired slider.
      const ranges = el.tagName === 'INPUT' && el.type === 'range';
      // 勾选框按 onchange 驱动（onclick 也认）—— 它与 range 一样不是"点一下"的控件，
      // 之前只认 onclick，于是 type=checkbox 的实时开关没法进这份名单。
      const checks = el.tagName === 'INPUT' && el.type === 'checkbox';
      const wired = el.tagName === 'SELECT' ? typeof el.onchange === 'function'
                  : ranges ? typeof el.oninput === 'function'
                  : checks ? (typeof el.onchange === 'function' || typeof el.onclick === 'function')
                  : el.tagName === 'SPAN' || el.tagName === 'P' ? true
                  : typeof el.onclick === 'function';
      check(`${page.url} #${id} is wired`, wired, el.tagName + (ranges ? '[range]' : ''));
    }
    if (page.url === '/library') {
      const version = doc.getElementById('appVersion');
      check('version is filled in', !!version && /\d+\.\d+\.\d+/.test(version.textContent || ''),
        version ? JSON.stringify(version.textContent) : 'missing');

      // ---- 扫码登录的轮询：等待窗口用完（timeout）必须**继续等** ----------------
      // `timeout` 与 `expired` 是两件事：前者是"这次等够了"，后者是"码真的没用了"。
      // 混在一起时，页面会在等待窗口结束的那一刻停手并显示"已过期"，而码还有效 ——
      // 用户扫码确认之后没人在等，表现就是"扫码后登不上"。
      const runQr = async (states) => {
        dom.window.__QR_STATES = states.slice();
        dom.window.eval(`api = async (path, body) => {
          if (path === 'login/qr-create') return {key: 'k', image: 'data:image/png;base64,AA'};
          if (path === 'login/qr-wait') {
            return {state: window.__QR_STATES.length ? window.__QR_STATES.shift() : 'expired'};
          }
          if (path === 'login/whoami') {
            return {provider: 'netease', stored: true, account: '我', verified: true, providers: {}};
          }
          return {};
        };`);
        doc.getElementById('qrStart').click();
        await new Promise((resolve) => setTimeout(resolve, 60));
        return String(doc.getElementById('qrState').textContent || '');
      };

      const afterTimeout = await runQr(['waiting', 'timeout', 'timeout', 'confirmed']);
      check('等待窗口超时之后继续轮询（确认后真的登上）',
            dom.window.__QR_STATES.length === 0 && afterTimeout === '已登录',
            JSON.stringify({left: dom.window.__QR_STATES.length, state: afterTimeout}));

      const afterExpired = await runQr(['timeout', 'expired', 'confirmed']);
      check('码真的过期时停手（不再继续轮询后面的状态）',
            dom.window.__QR_STATES.length === 1 && /过期/.test(afterExpired),
            JSON.stringify({left: dom.window.__QR_STATES.length, state: afterExpired}));

      // 曲库页按设置里的主题定下来：首屏缓存只负责"别闪一下"，权威是设置。
      // 这里默认深色，所以断言"页面确实应用过一次"，而不是碰巧没变。
      await new Promise((resolve) => setTimeout(resolve, 300));
      check('曲库页按设置应用了主题',
            dom.window.document.documentElement.dataset.theme === 'dark'
            && dom.window.localStorage.getItem('saltlrc-theme') === 'dark',
            JSON.stringify({theme: dom.window.document.documentElement.dataset.theme,
                            cached: dom.window.localStorage.getItem('saltlrc-theme')}));
    }
    if (page.url === '/studio.html') {
      // The queue must start EMPTY even when a saved session exists: it used to
      // be restored straight into the editor at launch, so yesterday's songs were
      // still there and two async chains (the restore's select(0) and a library
      // hand-off's select(last)) raced over which one the editor showed.
      // NOTE: check() here is boolean-style — check(name, ok, detail) — so every
      // assertion below must pass a comparison, not a number. Handing it a raw
      // count makes 0 read as FAIL and any positive count read as a vacuous PASS.
      const saved = JSON.parse(dom.window.localStorage.getItem('saltlrc-studio-session'));
      check('播种的会话确实存在（前提成立）',
            Array.isArray(saved?.items) && saved.items.length === 2,
            'items=' + (saved?.items?.length));
      const queueBox = doc.getElementById('queue');
      const items = queueBox.querySelectorAll('.item').length;
      const countText = (doc.getElementById('count').textContent || '').trim();
      check('启动后队列是空的（不自动加载上次会话）', items === 0, 'items=' + items);
      // Guard the fixture: nodes that are not .item would make the check above
      // pass while the queue box is not actually empty.
      check('队列容器里也没有其它残留节点', queueBox.children.length === 0,
            'children=' + queueBox.children.length + ' qlen=' + dom.window.eval('queue.length'));
      check('队列计数为 0', countText === '0', JSON.stringify(countText));
      const restoreButton = doc.getElementById('restoreSession');
      const selectedNow = dom.window.eval('selected');
      check('没有自动选中任何一首', selectedNow === -1, 'selected=' + selectedNow);
      check('「恢复上次会话」按钮可见并给出数量',
            !!restoreButton && restoreButton.hidden === false && /2/.test(restoreButton.textContent),
            restoreButton ? `${restoreButton.hidden} / ${restoreButton.textContent}` : 'missing');
      // And it still works when clicked: the offer is real, not decorative.
      restoreButton.click();
      const afterClick = doc.getElementById('queue').querySelectorAll('.item').length;
      check('点「恢复上次会话」后队列里出现 2 首', afterClick === 2, 'items=' + afterClick);
      check('点击后按钮隐藏', doc.getElementById('restoreSession').hidden === true, '');

      // 本地工程库。三件事必须成立，而且都不是"看起来对"就算的：
      //   ① 第一次打开要问保存位置（这个测试进程把 store.config_dir() 指到了
      //      临时目录，所以"未配置"是确定的前提，不是碰巧）；
      //   ② 没有保存路径时**不建**工程 —— 页面不许假装建了；
      //   ③ 退出按钮的两次确认走的是同步路径（下面马上要读 localStorage）。
      await new Promise((resolve) => setTimeout(resolve, 400));
      const projectDialog = doc.getElementById('projectDialog');
      check('首次打开会问工程的保存位置',
            !!projectDialog && projectDialog.open === true,
            projectDialog ? String(projectDialog.open) : 'missing');
      check('保存路径输入框预填了建议值',
            (doc.getElementById('projectPath').value || '').trim().length > 0,
            JSON.stringify(doc.getElementById('projectPath').value));
      check('没有保存路径时不建工程', dom.window.eval('Project.created') === 0,
            'created=' + dom.window.eval('Project.created'));
      // 「稍后再说」要能被当回事：关掉之后不再重复弹。
      doc.getElementById('projectLater').click();
      check('「稍后再说」后弹窗关闭', projectDialog.open === false, '');
      dom.window.eval('Project.ask()');
      check('本次会话不再重复弹（"稍后再说"被当回事）', projectDialog.open === false, '');

      // 退出 must clear the session: that is the other half of the report ("每次退出
      // 不会清空"). Re-seed first, so this proves the button clears storage rather
      // than merely observing that it was already empty.
      dom.window.localStorage.setItem('saltlrc-studio-session', SAVED_SESSION);
      doc.getElementById('exitApp').click();
      const leftOver = dom.window.localStorage.getItem('saltlrc-studio-session');
      check('点「退出」后会话存储被清空', leftOver === null, JSON.stringify(leftOver));

      // Key bindings decide who wins a keystroke, so exact modifier matching is
      // the whole point: if "Ctrl+T" also fired on Ctrl+Shift+T, binding both
      // would make one of them unreachable.
      const matches = (binding, event) =>
        dom.window.eval(`bindingMatches(${JSON.stringify(binding)}, ${JSON.stringify(event)})`);
      check('Space 匹配空格键', matches('Space', {key: ' ', code: 'Space'}) === true, '');
      check('Enter 匹配回车', matches('Enter', {key: 'Enter', code: 'Enter'}) === true, '');
      check('F1 匹配功能键', matches('F1', {key: 'F1', code: 'F1'}) === true, '');
      check('Ctrl+Z 匹配 Ctrl+z', matches('Ctrl+Z', {key: 'z', ctrlKey: true}) === true, '');
      check('Ctrl+Z 不匹配纯 z', matches('Ctrl+Z', {key: 'z'}) === false, '');
      check('Ctrl+Z 不匹配 Ctrl+Shift+Z', matches('Ctrl+Z', {key: 'Z', ctrlKey: true, shiftKey: true}) === false, '');
      check('Ctrl+T 不匹配 Ctrl+Shift+T',
            matches('Ctrl+T', {key: 'T', ctrlKey: true, shiftKey: true}) === false, '');
      check('Cmd 也能满足 Ctrl 绑定',
            matches('Ctrl+Z', {key: 'z', metaKey: true}) === true, '');
      check('绑定的动作能从事件里解析出来',
            dom.window.eval('actionFor({key: "F1"})') === 'help', '');
      check('没绑定的键不触发动作',
            dom.window.eval('actionFor({key: "q"})') === null, '');
      check('设置对话框的说明里写明了 Key 不会显示',
            /不会显示/.test(doc.getElementById('setKeyState').textContent || '')
            || /不会出现/.test(doc.getElementById('settingsDialog').textContent), '');
      // Rebind at runtime and confirm the table is what decides, not a hardcoded key.
      const original = dom.window.eval('appSettings.keys.playPause');
      dom.window.eval('appSettings.keys.playPause = "P"');
      check('改绑定后新键生效',
            dom.window.eval('actionFor({key: "p", code: "KeyP"})') === 'playPause', '');
      check('改绑定后旧键失效',
            dom.window.eval('actionFor({key: " ", code: "Space"})') === null, '');
      dom.window.eval(`appSettings.keys.playPause = ${JSON.stringify(original)}`);

      // The settings form is collected on save, so the meaningful check is that
      // every field the collector reads is actually present — a missing one
      // would only surface as a crash the first time someone pressed 保存.
      const formFields = ['setBaseUrl', 'setModel', 'setApiKey', 'setTarget', 'setInstructions',
                          'setFollow', 'setResume', 'setRepeat', 'setTapMode', 'setConfirmOverwrite',
                          'setAutoTranslate', 'setPerf', 'setAssoc', 'setAssocState', 'setDark',
                          'keyGrid', 'settingsError'];
      const missing = formFields.filter((id) => !doc.getElementById(id));
      check('设置表单的每个字段都在', missing.length === 0, 'missing=' + missing.join(','));
      // Opening it must populate the language list and the key bindings (both are
      // built by JS, so an empty container means the wiring is broken).
      doc.getElementById('settings').click();
      check('设置对话框打开', doc.getElementById('settingsDialog').open === true, '');
      check('目标语言下拉已填充', doc.getElementById('setTarget').options.length >= 8,
            'options=' + doc.getElementById('setTarget').options.length);
      check('按键绑定表已填充', doc.getElementById('keyGrid').children.length >= 6,
            'rows=' + doc.getElementById('keyGrid').children.length);
      check('已保存的 Key 不回显到输入框', doc.getElementById('setApiKey').value === '', '');
      // 没有「保存」按钮：改一项就存一项。
      check('设置里没有「保存」按钮', !doc.getElementById('settingsSave'), '');
      const follow = doc.getElementById('setFollow');
      const wantFollow = !(dom.window.eval('appSettings.features.followPlayback') !== false);
      follow.checked = wantFollow;
      follow.dispatchEvent(new dom.window.Event('change', {bubbles: true}));
      await new Promise((resolve) => setTimeout(resolve, 600));
      check('改一个勾选项就存下并生效（不需要确认键）',
            dom.window.eval('appSettings.features.followPlayback') === wantFollow,
            'want=' + wantFollow + ' got=' + dom.window.eval('appSettings.features.followPlayback'));
      check('保存动作给了可见反馈',
            (doc.getElementById('settingsSaved').textContent || '').includes('已保存'),
            JSON.stringify(doc.getElementById('settingsSaved').textContent));
      follow.checked = !wantFollow;
      follow.dispatchEvent(new dom.window.Event('change', {bubbles: true}));
      await new Promise((resolve) => setTimeout(resolve, 600));
      doc.getElementById('settingsClose').click();
      check('设置对话框可关闭', doc.getElementById('settingsDialog').open === false, '');

      // ---- 主题开关：勾选框 → 设置载荷 → applySettings → 页面属性 + 首屏缓存 ----
      // 不点「保存」：那一步会走 HTTP 与注册表。这里测的是接线本身，而
      // applySettings 正是保存成功后调用的那个函数。
      doc.getElementById('setDark').checked = false;
      check('勾选框进了设置载荷', dom.window.eval('collectSettings().features.darkMode') === false,
            String(dom.window.eval('collectSettings().features.darkMode')));
      const baseSettings = dom.window.eval('JSON.parse(JSON.stringify(appSettings || {translation: {}, features: {}, keys: {}}))');
      dom.window.eval(`applySettings(Object.assign(${JSON.stringify(baseSettings)}, {features: Object.assign({}, ${JSON.stringify(baseSettings.features || {})}, {darkMode: false})}))`);
      check('关掉深色模式后页面属性变成 light',
            dom.window.document.documentElement.dataset.theme === 'light',
            String(dom.window.document.documentElement.dataset.theme));
      check('主题写进了首屏缓存',
            dom.window.localStorage.getItem('saltlrc-theme') === 'light',
            String(dom.window.localStorage.getItem('saltlrc-theme')));
      dom.window.eval(`applySettings(Object.assign(${JSON.stringify(baseSettings)}, {features: Object.assign({}, ${JSON.stringify(baseSettings.features || {})}, {darkMode: true})}))`);
      check('再打开深色模式时属性回到 dark',
            dom.window.document.documentElement.dataset.theme === 'dark',
            String(dom.window.document.documentElement.dataset.theme));
      check('读设置时复选框跟着走',
            doc.getElementById('setDark').checked === true,
            String(doc.getElementById('setDark').checked));

      // ---- 启动时的「待打开工程」：**拿到的响应不完整也绝不许弄坏页面** ----
      //
      // 双击 .saltproj 时页面会问一次 /api/pending-open。这一段是启动路径上的
      // 附加动作，它一出错就是"整页的初始化断了"。
      // 真事：第一版直接读 `reply.headers.get(...)` —— 而这几个 jsdom 检查里的
      // fetch 替身只给了 ok/json，于是**每个**检查文件都多出一条
      // `FAIL async TypeError: ... reading 'get'`，看起来像七处各坏了一次。
      // 判据：不完整的响应 → 不抛错、也不弹错误框。
      const realFetch = dom.window.fetch;
      const dialogsBefore = doc.querySelectorAll('.errorDialogBox').length;
      dom.window.fetch = () => Promise.resolve({ok: true, json: async () => ({})});
      let refused = false;
      try { await dom.window.eval('openPendingProject()'); } catch (error) { refused = true; }
      check('不完整的响应不会让「打开启动时的工程」抛错', refused === false, String(refused));
      // 比"多了一个错误框"而不是"有没有错误框"：这一个检查之前/之后都可能
      // 有别的弹框被测到，数总量会假红。
      check('不完整的响应也不会弹错误框',
            doc.querySelectorAll('.errorDialogBox').length === dialogsBefore,
            '弹框数由 ' + dialogsBefore + ' 变成 '
            + doc.querySelectorAll('.errorDialogBox').length);
      dom.window.fetch = realFetch;

      // 光"不会崩"不够：双击的**目的是打开那个工程**，所以要真的走一遍 ——
      // 让服务端递一个最小但合法的工程，看它有没有进队列、当前项是不是它。
      const sample = JSON.stringify({
        kind: 'saltlrc-project', version: 1, savedAt: '', name: '端到端测试.lrc',
        duration: null, audio: null,
        document: {metadata: {}, groups: [
          {start: 1000, main: {text: '第一行', words: [], end: null, start: 1000},
           translation: {text: '', words: [], end: null, start: null}}]},
      });
      const queueBefore = dom.window.eval('queue.length');
      dom.window.fetch = () => Promise.resolve({
        status: 200, ok: true,
        headers: {get: (name) => (name === 'X-Project-Name' ? encodeURIComponent('e2e.saltproj') : null)},
        arrayBuffer: async () => new TextEncoder().encode(sample).buffer,
      });
      await dom.window.eval('openPendingProject()');
      check('双击的工程会真的进队列', dom.window.eval('queue.length') === queueBefore + 1,
            queueBefore + ' → ' + dom.window.eval('queue.length'));
      check('打开后当前项就是它（名字取自工程内容）',
            dom.window.eval('current() && current().name') === '端到端测试.lrc',
            String(dom.window.eval('current() && current().name')));
      dom.window.fetch = realFetch;

      // Word-tick readout: the tip element must exist, start hidden and format
      // times the way the rest of the UI does.
      const tip = doc.getElementById('waveTip');
      check('波形提示层存在', !!tip, '');
      check('提示层初始隐藏', tip.hidden === true, '');
      check('时间读数格式为 m:ss.mmm', dom.window.eval('tipTime(65500)') === '1:05.500',
            dom.window.eval('tipTime(65500)'));
      check('无时间时给占位符', dom.window.eval('tipTime(null)') === '—', '');
      // Nudging with nothing selected must do nothing at all (so the arrow keys
      // keep their ordinary meaning elsewhere).
      check('未选中刻度时微调不动作', dom.window.eval('nudgeSelected(10)') === false, '');
      // NB: inside eval() the page's own scope applies, so the element has to be
      // looked up there — a test-local variable is not visible to it.
      check('移出画布时隐藏提示',
            dom.window.eval('(hideWaveTip(), document.getElementById("waveTip").hidden)') === true, '');

      // The volume slider must reach the audio element, and the readout must
      // follow it. Asserting on <audio>.volume is the point: a slider that moves
      // without changing anything is the failure mode that looks fine on screen.
      const slider = doc.getElementById('volume');
      const audio = doc.getElementById('audio');
      const readout = doc.getElementById('volumeValue');
      const fire = (value) => { slider.value = value; slider.dispatchEvent(new dom.window.Event('input')); };
      const pct = () => (readout.textContent || '').trim();
      check('volume starts at 100%', audio.volume === 1, String(audio.volume));
      fire('40');
      check('moving the slider changes audio.volume', Math.abs(audio.volume - 0.4) < 1e-9, String(audio.volume));
      check('the readout follows the slider', pct() === '40%', pct());
      fire('0');
      check('volume can be muted with the slider', audio.volume === 0, String(audio.volume));
      check('muted readout reads 0%', pct() === '0%', pct());
      // An out-of-range value must be clamped, not applied verbatim.
      fire('250');
      check('over-range volume is clamped to 1', audio.volume === 1, String(audio.volume));
      check('clamped readout reads 100%', pct() === '100%', pct());
      // And it must survive a reload, or the control is worse than useless.
      // The stored value is the CLAMPED one, so a bogus input cannot come back
      // as a bogus volume on the next launch.
      const stored = dom.window.localStorage.getItem('saltlrc-volume');
      check('volume is persisted (clamped)', stored === '100', String(stored));
      fire('35');
      check('volume updates the stored value', dom.window.localStorage.getItem('saltlrc-volume') === '35',
            String(dom.window.localStorage.getItem('saltlrc-volume')));
    }
    // ---- 拖动分栏：只许写 CSS 变量，**不许**写内联的 grid-template-columns ----
    //
    // 用户报："整个程序在 Windows 拖动缩放的情况下内部 UI 布局会混乱。"
    // 真因（v0.24.3 修）：splitter.js 原来内联写 `grid-template-columns`，
    // 而**内联样式压过所有样式表规则，包括 @media**。样式表只在窗口 ≥1250px 时
    // 用三轨布局、并在这以下把 #splitter 设成 display:none —— 于是窗口一拖到
    // 1250px 以下：把手不再是网格项，模板却仍然三轨 → `section`（整个编辑器）
    // 掉进那个 10px 的轨道，看起来就是"布局全乱"。
    //
    // 判据贴着这一点：拖完之后**内联列定义必须还是空的**，值只能出现在 CSS 变量里。
    const splitMain = doc.querySelector('main');
    const splitHandle = doc.getElementById('splitter');
    // 这一段在**每个页面**上都会跑（外层是按页循环），所以键要跟着当前页面的
    // 路径走，不能写死 —— 第一版写死了 /studio.html，在曲库页上当场假红。
    const splitKey = 'saltlrc-split:' + dom.window.location.pathname;
    check('分栏把手存在', !!splitHandle, '');
    if (splitHandle) {
      // jsdom 没有指针捕获，补个空实现（拖动本身只用 clientX）。
      if (!splitHandle.setPointerCapture) splitHandle.setPointerCapture = () => {};
      if (!splitHandle.releasePointerCapture) splitHandle.releasePointerCapture = () => {};
      const send = (type, x) => splitHandle.dispatchEvent(
        new dom.window.MouseEvent(type, {clientX: x, bubbles: true}));
      send('pointerdown', 100);
      send('pointermove', 400);                       // 拖 300px（clamp 到 [200,560]）
      send('pointerup', 400);
      const variable = splitMain.style.getPropertyValue('--split-left');
      check('拖分栏把宽度写成 CSS 变量', variable === '300px', variable);
      check('拖分栏没有写内联的 grid-template-columns',
            splitMain.style.getPropertyValue('grid-template-columns') === '',
            splitMain.style.getPropertyValue('grid-template-columns'));
      check('分栏宽度存进了 localStorage',
            String(dom.window.localStorage.getItem(splitKey)) === '300',
            String(dom.window.localStorage.getItem(splitKey)));
      send('dblclick', 400);
      check('双击恢复默认（变量清掉）', splitMain.style.getPropertyValue('--split-left') === '', '');
      check('双击恢复默认（存储清掉）',
            !dom.window.localStorage.getItem(splitKey), '');
    }

    dom.window.close();
  }
  const failed = results.filter((r) => !r).length;
  console.log(`\n${results.length} checks, ${failed} failed`);
  process.exit(failed ? 1 : 0);
})();
