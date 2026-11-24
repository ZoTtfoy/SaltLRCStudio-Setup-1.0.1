// 自动落盘 + 应用内提示的端到端检查（真页面、真后端、真磁盘）。
//
// 为什么要单独一个脚本（v0.24.0）：
//   用户报"保存工程的响应速度很慢"。量下来两个真热点：
//     ① `Project.flush()` 对队列里**每一个**文档做 `JSON.stringify` 取指纹 ——
//        300 首时每次编辑 **1039ms** 的纯主线程开销（`tests/_bench_io.py` 的 F 段）；
//     ② `persist()` 把**整个队列**拼成一个 JSON 写进 localStorage —— 同一量级。
//   两个都是 O(队列)，而每次编辑真正变的只有一条。
//
// 这里的判据就是"**改一条只碰一条**"，用**可观察的事实**来量：
//   · 打到 `/api/projects/write` 的请求数（改一条 → 1 个）；
//   · 页面里 `JSON.stringify` 被喂进"文档级对象"的次数
//     （改一条 → 1 次；改成旧写法会变成"队列条数 + 1"，这里 12 条就是 13 次）。
//   第二条是关键：请求数在旧写法下**也是 1**（指纹扫完发现只有一条变了），
//   所以只看请求数根本证明不了这次重构。**判据要贴着症状写。**
//
// 用法: node web_autosave_check.js <base-url> <scratch-folder>
const fs = require('fs');
const path = require('path');
const { JSDOM, VirtualConsole } = require('jsdom');

const BASE = process.argv[2];
const ROOT_DIR = process.argv[3];
if (!BASE || !ROOT_DIR) {
  console.error('usage: node web_autosave_check.js <base-url> <scratch-folder>');
  process.exit(2);
}

const ITEMS = 12;

const results = [];
function check(name, ok, detail = '') {
  results.push(ok);
  console.log((ok ? 'PASS ' : 'FAIL ') + name + (detail ? ' :: ' + detail : ''));
}

async function api(route, body) {
  const response = await fetch(BASE + route, {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body || {}),
  });
  return response.json();
}

(async () => {
  fs.mkdirSync(ROOT_DIR, {recursive: true});
  // 先把保存路径设好：这样页面一起来就是"已配置"，不用再走一遍问路径的弹窗
  // （那条路已经由 web_project_check.js 覆盖了，这里不重复）。
  const configured = await api('/api/projects/root', {path: ROOT_DIR});
  if (!configured.root) throw Error('设置保存路径失败：' + JSON.stringify(configured));

  const errors = [];
  const virtualConsole = new VirtualConsole();
  virtualConsole.on('jsdomError', (e) => errors.push(e.message));

  let writes = 0;
  let docStringifies = 0;
  let failWrites = false;                       // 打开它就让写盘接口失败，验横幅

  // 页面做成一个函数：jsdom 在机器忙时偶尔会漏加载一个脚本，页面就"半活着"
  // （Project 在，但没起来）。那种情况要**整页重来一次**，凑合往下跑只会报出
  // 与真因无关的错（这条检查的前提就是页面真的起来了）。
  const openPage = () => JSDOM.fromURL(BASE + '/studio.html', {
    runScripts: 'dangerously', resources: 'usable', pretendToBeVisual: true, virtualConsole,
    beforeParse(window) {
      window.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} };
      // jsdom 没有 canvas 实现（getContext 返回 null），而换主题会要求重画波形。
      // 打桩之后，"页面脚本有没有报错"这条判据测的是页面本身。
      const stubContext = new Proxy({}, {
        get(target, key) {
          if (key === 'measureText') return () => ({width: 10});
          if (key === 'canvas') return {width: 900, height: 400};
          if (key === 'createLinearGradient' || key === 'createRadialGradient') {
            return () => ({addColorStop() {}});
          }
          return () => undefined;
        },
        set() { return true; },
      });
      window.HTMLCanvasElement.prototype.getContext = () => stubContext;
      // 数"文档级"序列化：既算 `item.document`（旧写法的指纹扫描），
      // 也算 `packProject(...)` 那种"里面装着文档"的载荷。
      const realStringify = window.JSON.stringify;
      const looksLikeDocument = (value) => !!value && typeof value === 'object' &&
        (Array.isArray(value.groups) ||
         (value.document && Array.isArray(value.document.groups)));
      window.JSON.stringify = function (value, ...rest) {
        if (looksLikeDocument(value)) docStringifies++;
        return realStringify.call(this, value, ...rest);
      };
      window.fetch = (input, init) => {
        const href = new URL(input, BASE).href;
        if (href.endsWith('/api/projects/write')) {
          writes++;
          if (failWrites) {
            return Promise.resolve({ok: false, status: 400,
              json: () => Promise.resolve({error: '磁盘已满（测试用）'})});
          }
        }
        return fetch(href, init);
      };
      window.confirm = () => false;
      window.alert = () => {};
      window.matchMedia = () => ({matches: false, addEventListener() {}, removeEventListener() {}});
      const proto = window.HTMLDialogElement && window.HTMLDialogElement.prototype;
      if (proto && typeof proto.showModal !== 'function') {
        proto.showModal = function () { this.open = true; };
        proto.show = function () { this.open = true; };
        proto.close = function () { this.open = false; };
      }
    },
  });
  let dom = null, w = null, doc = null;
  const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  async function until(expr, timeout = 8000) {
    const deadline = Date.now() + timeout;
    for (;;) {
      let ok = false;
      try { ok = w.eval(expr); } catch { ok = false; }
      if (ok) return true;
      if (Date.now() > deadline) return false;
      await wait(60);
    }
  }

  // 先等页面自己的启动流程跑完（Project.start() 里有 backfill），
  // 否则我们塞进去的条目会跟它自己那一轮抢着建工程。
  let loaded = false;
  for (let attempt = 1; attempt <= 2 && !loaded; attempt++) {
    dom = await openPage();
    w = dom.window;
    doc = w.document;
    await until('typeof Project === "object" && Project.started === true', 25000);
    loaded = await until('typeof Project === "object" && Project.info.configured === true', 25000);
    if (!loaded && attempt === 1) {
      dom.window.close();
      await wait(500);
    }
  }
  if (!loaded) {
    check('页面加载并读到保存路径', false, w.eval('JSON.stringify(Project.info)'));
    console.log('\n' + results.length + ' checks, ' + results.filter((r) => !r).length + ' failed');
    process.exit(1);
  }
  check('页面加载并读到保存路径', true, ROOT_DIR);

  // ---- 1. 塞 12 条进队列（模拟"导入了一个文件夹"） --------------------------
  w.eval(`
    for (let index = 0; index < ${ITEMS}; index++) {
      const document = emptyDocument();
      for (let line = 0; line < 8; line++) {
        document.groups.push({
          start: line * 3000,
          main: {text: '第' + line + '行', words: [], start: line * 3000, end: line * 3000 + 2500},
          translation: {text: '', words: [], start: null, end: null}});
      }
      queue.push({name: '测试' + index + '.lrc', document,
                  report: {errors: [], warnings: []}, history: []});
    }
    repaintQueue();
  `);
  check(ITEMS + ' 条都被建了工程', await until('Project.created === ' + ITEMS, 25000),
        'created=' + w.eval('Project.created')
        + ' failed=' + w.eval('Project.failed.size')
        + ' ids=' + w.eval('[...Project.ids.values()].filter(Boolean).length')
        + ' 状态行「' + (doc.getElementById('message').textContent || '').slice(0, 120) + '」');

  // ---- 2. 改**一条**，量这次编辑碰了多少东西 -------------------------------
  await wait(1200);                       // 让建工程那一批写盘先结束
  writes = 0;
  docStringifies = 0;
  w.eval(`
    const item = queue[5];
    item.document.groups[0].main.text = '改过的第 0 行';
    remember(item, '改文字', null);
  `);
  await wait(1500);                       // 800ms 合并窗口 + 请求

  check('改一条只写一个工程', writes === 1, 'writes=' + writes);
  // 旧写法在这里是 ITEMS + 1（扫全队列取指纹）。留一点余量给实现细节，
  // 但必须远低于队列长度 —— 否则等于没改。
  // 阈值取"队列的一半"：新写法只序列化变过的那一份（外加一两次内部克隆/快照），
  // 旧写法是"队列条数 + 1"。留出余量是为了不让一次内部实现微调就假红，
  // 但离 13 还差得远 —— 判据仍然能分辨这两者。
  check('只序列化了真正变过的那份文档（旧写法要 ' + (ITEMS + 1) + ' 次）',
        docStringifies <= ITEMS / 2, 'docStringifies=' + docStringifies);

  const files = fs.readdirSync(ROOT_DIR, {withFileTypes: true})
    .filter((entry) => entry.isDirectory()).map((entry) => entry.name).sort();
  check('磁盘上每条一个工程文件夹（' + ITEMS + ' 个）', files.length === ITEMS, JSON.stringify(files));
  const one = files.filter((name) => /-测试5$/.test(name));
  check('改过的那一条有对应文件夹', one.length === 1, JSON.stringify(one));
  const saved = JSON.parse(fs.readFileSync(path.join(ROOT_DIR, one[0], 'project.saltproj'), 'utf8'));
  check('落盘的内容是改过的那一条',
        saved.document.groups[0].main.text === '改过的第 0 行' && saved.name === '测试5.lrc',
        saved.name + ' / ' + saved.document.groups[0].main.text);

  // ---- 3. 会话快照：一条一个键，不整队列重写 -------------------------------
  const sessionKeys = Object.keys(w.localStorage).filter((k) => k.startsWith('saltlrc-studio-session'));
  const itemKeys = sessionKeys.filter((k) => k.indexOf(':item:') > 0);
  const index = JSON.parse(w.localStorage.getItem('saltlrc-studio-session') || 'null');
  check('会话快照一条一个键（' + ITEMS + ' 条）', itemKeys.length === ITEMS,
        'keys=' + itemKeys.length);
  check('索引里记着全部条目', !!index && Array.isArray(index.entries) && index.entries.length === ITEMS,
        index ? String(index.entries.length) : 'null');
  check('单条快照里就是那份文档',
        JSON.parse(w.localStorage.getItem(itemKeys[0])).document.groups.length === 8, '');

  // ---- 4. 写盘失败 → 顶部横幅（不打断），并退避重试 -------------------------
  failWrites = true;
  w.eval(`
    const item = queue[3];
    item.document.groups[0].main.text = '这一笔会失败';
    remember(item, '改文字', null);
  `);
  await wait(1500);
  const bar = doc.getElementById('alertBar');
  const rows = bar ? bar.querySelectorAll('.alertRow') : [];
  check('失败的写盘挂了横幅', !!bar && bar.hidden === false && rows.length === 1,
        rows.length + ' 行');
  check('横幅里写着原因', rows.length && /磁盘已满/.test(rows[0].textContent),
        rows.length ? rows[0].textContent.slice(0, 60) : '');
  check('失败的那一条排了重试', w.eval('Project.retry.size') === 1,
        'retry=' + w.eval('Project.retry.size'));

  // ---- 5. 恢复之后横幅自己收掉 --------------------------------------------
  failWrites = false;
  w.eval(`
    const item = queue[3];
    item.document.groups[0].main.text = '这一次能写';
    remember(item, '改文字', null);
  `);
  // 失败之后有**退避**（横幅里写着"每 5 秒重试"），所以这里要等过那个窗口 ——
  // 顺带把"退避真的会发生"也验了：立刻重试就把"失败时别把磁盘打死"这条设计丢了。
  check('失败后不会立刻重试（退避 5 秒）', w.eval('Project.retry.size') === 1, '');
  await wait(7000);
  check('写成功之后横幅收掉', doc.getElementById('alertBar').hidden === true, '');
  check('重试记录也清干净', w.eval('Project.retry.size') === 0, '');

  // ---- 6. 应用内提示：失败弹框 --------------------------------------------
  w.eval(`Notify.fail('导出失败', '第 3 行没有逐字时间', ['双击波形上那一句可以补'])`);
  const dialog = doc.getElementById('errorDialog');
  check('失败会弹框', !!dialog && dialog.open === true, dialog ? String(dialog.open) : 'missing');
  check('框里写着原因', doc.getElementById('errorDetail').textContent === '第 3 行没有逐字时间',
        doc.getElementById('errorDetail').textContent);
  check('框里有"那我要怎么做"', doc.getElementById('errorHints').textContent.indexOf('双击波形') >= 0,
        doc.getElementById('errorHints').textContent);
  doc.getElementById('errorClose').click();
  check('点「知道了」能关掉', doc.getElementById('errorDialog').open === false, '');

  // 同一把 key 反复失败不叠框（一次断网能刷出几百条就没人看了）
  w.eval(`Notify.fail('导出失败', '同一条', [], 'same'); Notify.fail('导出失败', '同一条', [], 'same');`);
  check('同一条重复失败只弹一次', doc.getElementById('errorDialog').open === true, '');
  doc.getElementById('errorClose').click();

  // ---- 7. 弹框绝不能把错误吞掉 --------------------------------------------
  // 它是错误通路本身：传进去各种奇怪东西都不许抛（抛了就等于"出错了但没人说"）。
  let survived = true;
  try {
    w.eval(`Notify.fail(null, undefined, null); Notify.fail(new Error('对象当原因'));`);
  } catch (error) { survived = false; }
  check('Notify 传奇怪东西也不抛', survived, '');
  if (doc.getElementById('errorDialog').open) doc.getElementById('errorClose').click();

  const real = errors.filter((e) => !/HTMLCanvasElement|Not implemented/.test(e));
  check('页面没有脚本错误', real.length === 0, real.slice(0, 2).join(' | '));

  dom.window.close();
  const failed = results.filter((r) => !r).length;
  console.log('\n' + results.length + ' checks, ' + failed + ' failed');
  process.exit(failed ? 1 : 0);
})().catch((error) => {
  console.error('检查脚本自身出错：', error);
  process.exit(1);
});
