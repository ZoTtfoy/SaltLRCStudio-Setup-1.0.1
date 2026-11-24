// 本地工程库的端到端检查（在真页面里跑，不是单测桩）。
//
// 需求原文："每导入一个音频/歌词文件的时候新建一个工程，将其保存在本地；第一次
// 打开软件时询问用户保存路径；如果用户未保存退出时询问用户是否保存，询问二次，
// 用户需确认两次；如果是则保留工程，如果否，则删除本地已保存的文件。"
//
// 所以这里走的是真链路：设置保存路径（走界面上的按钮）→ 导入一个条目（走
// repaintQueue 那个唯一出口）→ 磁盘上真的出现工程文件 → 改一下歌词 → 文件跟着
// 变 → 退出时问两次 → 按答案保留或删除。
//
// 用法: node web_project_check.js <base-url> <scratch-folder>
const fs = require('fs');
const path = require('path');
const { JSDOM, VirtualConsole } = require('jsdom');

const BASE = process.argv[2];
const ROOT_DIR = process.argv[3];
if (!BASE || !ROOT_DIR) {
  console.error('usage: node web_project_check.js <base-url> <scratch-folder>');
  process.exit(2);
}

const results = [];
function check(name, ok, detail = '') {
  results.push(ok);
  console.log((ok ? 'PASS ' : 'FAIL ') + name + (detail ? ' :: ' + detail : ''));
}

const confirmCalls = [];
let confirmAnswers = [];

async function api(route, body) {
  const response = await fetch(BASE + route, {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body || {}),
  });
  return response.json();
}

function projectFolders() {
  return fs.readdirSync(ROOT_DIR, {withFileTypes: true})
    .filter((entry) => entry.isDirectory()).map((entry) => entry.name).sort();
}

function readProject(name) {
  const file = path.join(ROOT_DIR, name, 'project.saltproj');
  return fs.existsSync(file) ? fs.readFileSync(file, 'utf8') : null;
}

(async () => {
  fs.mkdirSync(ROOT_DIR, {recursive: true});
  const sentinel = path.join(ROOT_DIR, '用户自己的东西');
  fs.mkdirSync(sentinel, {recursive: true});
  fs.writeFileSync(path.join(sentinel, '笔记.txt'), '不可替代', 'utf8');

  const errors = [];
  const virtualConsole = new VirtualConsole();
  virtualConsole.on('jsdomError', (e) => errors.push(e.message));

  const dom = await JSDOM.fromURL(BASE + '/studio.html', {
    runScripts: 'dangerously', resources: 'usable', pretendToBeVisual: true, virtualConsole,
    beforeParse(window) {
      window.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} };
      // jsdom 没有 canvas 实现（getContext 返回 null），而页面现在换主题时会要求
      // 重画波形 → 在 null 上取 setTransform 就成了一条"页面脚本报错"。
      // 判据要测的是页面，不是 jsdom 缺的那块能力；几何断言由打桩 canvas 的
      // 那几个检查负责（_render_blocks.js 等）。
      const stubContext = new Proxy({
        measureText: (text) => ({width: String(text).length * 7}),
      }, {get: (target, key) => (key in target ? target[key] : () => {}),
          set: (target, key, value) => { target[key] = value; return true; }});
      window.HTMLCanvasElement.prototype.getContext = () => stubContext;
      window.fetch = (input, init) => {
        const href = new URL(input, BASE).href;
        // 退出接口必须被打桩：真让它把后端关掉，后面的断言就没服务可问了。
        if (href.endsWith('/api/exit')) {
          return Promise.resolve({ok: true, json: () => Promise.resolve({status: 'exiting'})});
        }
        return fetch(href, init);
      };
      window.confirm = (message) => {
        confirmCalls.push(String(message));
        return confirmAnswers.length ? confirmAnswers.shift() : false;
      };
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
  const w = dom.window;
  const doc = w.document;
  const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  // 超时给宽一点：全量测试里同时跑好几个 jsdom 时，页面加载会慢很多，
  // 而"等不到"与"页面坏了"是两件事 —— 宁可多等，也不要假红。
  async function until(expr, timeout = 30000) {
    const deadline = Date.now() + timeout;
    for (;;) {
      let ok = false;
      try { ok = w.eval(expr); } catch { ok = false; }
      if (ok) return true;
      if (Date.now() > deadline) return false;
      await wait(60);
    }
  }

  // ---- 0. 页面起来了，而且第一件事是问保存位置 -----------------------------
  // 等 Project.asked 而不是 Project.started：started 在 start() 开头就置位了，
  // 那时配置接口还在路上（这里第一次就是这么假红的 —— 断言比被测行为跑得早）。
  if (!(await until('typeof Project === "object" && Project.asked === true'))) {
    // 页面里的报错要一起说出来：只说"没有问保存位置"看不出是被什么打断的。
    const real = errors.filter((e) => !/HTMLCanvasElement|Not implemented/.test(e));
    check('页面加载完成', false, 'Project 没有问保存位置；页面报错：'
          + (real.length ? real.slice(0, 3).join(' | ') : '（无）'));
    console.log('\n' + results.length + ' checks, ' + results.filter((r) => !r).length + ' failed');
    process.exit(1);
  }
  check('首次打开会问保存位置', doc.getElementById('projectDialog').open === true, '');
  check('没有保存路径时不建工程', w.eval('Project.created') === 0, '');

  // ---- 1. 走界面按钮设置保存路径 ------------------------------------------
  doc.getElementById('projectPath').value = ROOT_DIR;
  doc.getElementById('projectUse').click();
  check('设置保存路径（点「用这个文件夹」）',
        await until('Project.info.configured === true'), w.eval('JSON.stringify(Project.info)'));
  check('弹窗关掉了', doc.getElementById('projectDialog').open === false, '');
  // 服务端回的是 Windows 绝对路径（反斜杠），命令行的临时目录是正斜杠 ——
  // 比字符串之前先把两边统一，否则这条断言量的只是分隔符写法。
  const shown = (doc.getElementById('projectRoot').textContent || '').replace(/\\/g, '/');
  check('「检查与工程」页显示了这个路径', shown === ROOT_DIR.replace(/\\/g, '/'), shown);

  // ---- 1b. 路径不合法时，错误必须出现在**弹窗里** --------------------------
  // 用户主动触发的失败必须出声，而且要在他能看见的地方：状态行在弹窗背后，
  // 只写那儿等于没提示（v0.13.1 的教训）。空路径会让服务端回 400
  // "请选择保存路径" —— 这不是打桩，是真让后端拒一次。
  w.eval('Project.ask(true)');
  doc.getElementById('projectPath').value = '   ';
  doc.getElementById('projectUse').click();
  await wait(800);
  check('路径不合法时在弹窗里报错',
        (doc.getElementById('projectState').textContent || '').includes('保存路径'),
        JSON.stringify(doc.getElementById('projectState').textContent));
  check('报错时弹窗不关（否则用户看不到为什么）',
        doc.getElementById('projectDialog').open === true, '');
  doc.getElementById('projectLater').click();
  await wait(100);
  check('关掉之后不再重复弹', doc.getElementById('projectDialog').open === false, '');

  // ---- 2. 导入一个条目 → 磁盘上要真的多一个工程 ----------------------------
  w.eval(`queue.push({name: '测试甲.lrc', document: emptyDocument(),
                     report: {errors: [], warnings: []}, history: [], match: null});
          repaintQueue();`);
  check('导入后建了工程', await until('Project.created === 1'), 'created=' + w.eval('Project.created'));
  await wait(400);
  const folders = projectFolders();
  const created = folders.filter((name) => name !== '用户自己的东西');
  check('保存路径里出现了工程文件夹', created.length === 1, JSON.stringify(folders));
  const text = created.length ? readProject(created[0]) : null;
  check('工程文件写下去了', !!text, created[0] || '(没有)');
  if (text) {
    let packed = null;
    try { packed = JSON.parse(text); } catch { packed = null; }
    check('工程文件是合法的 .saltproj', !!packed && packed.kind === 'saltlrc-project',
          packed ? String(packed.kind) : 'parse failed');
    check('工程里记着歌名', !!packed && packed.name === '测试甲.lrc', packed ? packed.name : '');
    check('自动保存的工程不内嵌音频', !!packed && packed.audio === null, '');
  }

  // ---- 3. 改歌词 → 本地那份跟着变（写盘只走 persist 一条路）----------------
  w.eval(`queue[0].document = {groups: [{start: 1000,
             main: {text: '改过的歌词', start: 1000, end: 3000, words: []},
             translation: {text: '', start: null, end: null, words: []}}],
           metadata: {}, notes: [], unresolved: [], alignment: {}};
          remember(queue[0], '测试改动');`);
  await wait(1500);
  check('编辑之后工程文件跟着更新',
        !!created.length && (readProject(created[0]) || '').includes('改过的歌词'),
        '');

  // ---- 4. 退出：两次确认，答案不一致就什么都不做 ---------------------------
  confirmAnswers = [false, false];   // 第一问"不保留"，第二问又反悔
  confirmCalls.length = 0;
  doc.getElementById('exitApp').click();
  await wait(600);
  check('退出时问了两次', confirmCalls.length === 2, 'calls=' + confirmCalls.length);
  check('第一问讲的是保留还是删除',
        /保留/.test(confirmCalls[0] || '') && /删除|删掉/.test(confirmCalls[0] || ''),
        (confirmCalls[0] || '').split('\n')[0]);
  check('第二问是"再次确认"（删除不可恢复）',
        /再次确认/.test(confirmCalls[1] || '') && /不可恢复/.test(confirmCalls[1] || ''),
        (confirmCalls[1] || '').split('\n')[0]);
  check('两次答案不一致 → 工程一个都没动', projectFolders().includes(created[0]),
        JSON.stringify(projectFolders()));

  // ---- 5. 退出：两次都答"删除" → 文件真的被删掉，别人的东西不能碰 ----------
  confirmAnswers = [false, true];
  confirmCalls.length = 0;
  doc.getElementById('exitApp').click();
  await wait(1200);
  check('工程文件夹被删掉了', !projectFolders().includes(created[0]),
        JSON.stringify(projectFolders()));
  check('保存路径里别人的文件夹没被碰',
        fs.existsSync(path.join(sentinel, '笔记.txt')), '');
  check('退出时会话存储被清空', w.localStorage.getItem('saltlrc-studio-session') === null, '');
  const afterDiscard = await api('/api/projects/config');
  check('服务端的会话记录也清空了', afterDiscard.session.length === 0,
        'session=' + afterDiscard.session.length);

  // ---- 6. 退出：两次都答"保留" → 文件留着，只清记录 ------------------------
  w.eval(`queue.push({name: '测试乙.lrc', document: emptyDocument(),
                     report: {errors: [], warnings: []}, history: [], match: null});
          repaintQueue();`);
  check('第二个条目也建了工程', await until('Project.created === 2'), 'created=' + w.eval('Project.created'));
  await wait(400);
  const second = projectFolders().filter((name) => name !== '用户自己的东西');
  check('第二个工程的文件夹在', second.length === 1, JSON.stringify(second));
  confirmAnswers = [true, true];
  confirmCalls.length = 0;
  doc.getElementById('exitApp').click();
  await wait(1200);
  check('保留时问了两次', confirmCalls.length === 2, 'calls=' + confirmCalls.length);
  check('保留 → 文件夹还在', second.length === 1 && projectFolders().includes(second[0]),
        JSON.stringify(projectFolders()));
  const afterKeep = await api('/api/projects/config');
  check('保留后会话记录清空（文件不动）', afterKeep.session.length === 0,
        'session=' + afterKeep.session.length);

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
