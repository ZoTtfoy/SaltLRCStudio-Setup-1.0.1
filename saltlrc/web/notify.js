"use strict";
/* 应用内提示的**唯一出口**：失败弹框、后台失败挂横幅、一般提示走状态行。
 *
 * **分级**：
 *   · `Notify.fail`   —— **你主动做的事没办成**：模态弹框，必须点一下「知道了」。
 *                        里面是中文原因 + "那我要怎么做" + 「复制详情」。
 *                        **你自己点了「取消」这类不是错误的事不要调它。**
 *   · `Notify.banner` —— **后台持续发生的失败**（例如自动落盘反复失败）：顶部一条
 *                        可关闭的横幅，不打断你；同一把 key 反复失败只占一行。
 *   · `Notify.info`   —— 常规提示，走状态行 `say()`。
 *
 * **DOM 自己建**，不改两个页面的 HTML：联网查找字词那一页与编辑器页共用同一套外观，
 * 也不必关心脚本与元素的先后顺序。
 *
 * **它自己是错误通路，绝不能抛异常**：每一步都兜住，兜不住就退到 `alert`
 * （打包窗口里 alert 仍然可用）。
 */
const Notify = (() => {
  let dialog = null;
  let dialogKey = '';
  let bar = null;
  const rows = new Map();          // key -> 那一行的元素

  // ------------------------------------------------------------ 工具
  function safeText(value) {
    if (value === null || value === undefined) return '';
    if (typeof value === 'string') return value;
    if (value instanceof Error) return value.message || String(value);
    try { return String(value); } catch { return '(无法显示的内容)'; }
  }

  async function copyText(text) {
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(text);
        return true;
      }
    } catch { /* 落到下面的 execCommand 回退路径 */ }
    try {
      const area = document.createElement('textarea');
      area.value = text;
      area.setAttribute('readonly', '');
      area.style.position = 'fixed';
      area.style.top = '-1000px';
      document.body.append(area);
      area.select();
      const done = document.execCommand('copy');
      area.remove();
      return done;
    } catch { return false; }
  }

  function fault(title, detail) {
    try { window.Trace?.fault?.('界面报错', title + (detail ? '：' + detail : '')); } catch { /* 忽略 */ }
  }

  // ------------------------------------------------------------ 模态弹框
  function button(id, label, className) {
    const element = document.createElement('button');
    element.id = id;
    element.type = 'button';
    element.textContent = label;
    if (className) element.className = className;
    return element;
  }

  function ensureDialog() {
    if (dialog) return dialog;
    dialog = document.createElement('dialog');
    dialog.id = 'errorDialog';
    dialog.className = 'errorDialogBox';

    const title = document.createElement('h2');
    title.id = 'errorTitle';
    const detail = document.createElement('p');
    detail.id = 'errorDetail';
    detail.className = 'errorDetail';
    const hints = document.createElement('ul');
    hints.id = 'errorHints';
    hints.className = 'errorHints';
    const hint = document.createElement('p');
    hint.className = 'hint';
    hint.textContent = '按 Ctrl+Shift+T 可以打开操作追踪，看清那一下到底发生了什么；'
      + '「复制详情」会把这条错误连同最近的追踪一起放进剪贴板。';

    const actions = document.createElement('div');
    actions.className = 'toolbar dialogActions';
    const trace = button('errorTrace', '打开操作追踪');
    const copy = button('errorCopy', '复制详情');
    const spacer = document.createElement('span');
    spacer.className = 'spacer';
    spacer.setAttribute('aria-hidden', 'true');
    const close = button('errorClose', '知道了', 'button');
    actions.append(trace, copy, spacer, close);

    dialog.append(title, detail, hints, hint, actions);
    document.body.append(dialog);

    close.onclick = () => { dialog.close(); window.Trace?.step?.('提示已确认', dialogKey); };
    copy.onclick = async () => {
      const body = [
        '【SaltLRC Studio 提示】' + safeText(title.textContent),
        safeText(detail.textContent),
        ...Array.from(hints.children).map((item) => '- ' + item.textContent),
        '',
        '--- 操作追踪 ---',
      ].join('\n');
      let report = '';
      try { report = await window.Trace?.report?.() || ''; } catch { report = '(追踪读取失败)'; }
      copy.textContent = await copyText(body + '\n' + report) ? '已复制' : '复制失败（请手动截图）';
      setTimeout(() => { copy.textContent = '复制详情'; }, 1800);
    };
    trace.onclick = () => {
      // 模态弹框会挡住页面，追踪面板点不动 —— 先关掉自己再打开面板。
      dialog.close();
      try { window.Trace?.toggle?.(); } catch { /* 忽略 */ }
    };
    return dialog;
  }

  /**
   * 你主动做的事没办成 → 弹框。
   *
   * @param {string} title  一句话说清哪件事没成（"导出失败"）
   * @param {string|Error} detail 中文原因（服务端给的原文就很好）
   * @param {string[]} hints  "那我要怎么做"（可以不给）
   * @param {string} key     同一个 key 反复失败不重复弹（默认用标题）
   */
  function fail(title, detail, hints, key) {
    const heading = safeText(title) || '出错了';
    const reason = safeText(detail);
    fault(heading, reason);
    try {
      const box = ensureDialog();
      dialogKey = key || (heading + '\n' + reason);
      box.querySelector('#errorTitle').textContent = heading;
      const detailBox = box.querySelector('#errorDetail');
      detailBox.textContent = reason || '（没有更多说明）';
      detailBox.hidden = !reason;
      const list = box.querySelector('#errorHints');
      list.replaceChildren();
      for (const line of hints || []) {
        if (!line) continue;
        const item = document.createElement('li');
        item.textContent = safeText(line);
        list.append(item);
      }
      list.hidden = !list.children.length;
      if (box.open) return;                      // 已经开着同一条：只更新内容
      box.showModal();
      window.Trace?.dialogOpen?.(box);
    } catch (error) {
      // 弹框本身失败也要出声 —— 这是最后一道，只能靠 alert。
      try { window.alert(heading + (reason ? '\n\n' + reason : '')); } catch { /* 放弃 */ }
    }
  }

  // ------------------------------------------------------------ 顶部横幅
  function ensureBar() {
    if (bar) return bar;
    bar = document.createElement('div');
    bar.id = 'alertBar';
    bar.className = 'alertBar';
    bar.setAttribute('role', 'status');
    bar.hidden = true;
    document.body.prepend(bar);
    return bar;
  }

  /**
   * 后台持续发生的失败 → 顶部横幅（不打断）。
   *
   * 同一把 `key` 反复失败只占一行、只更新文字 —— 否则一次断网能刷出几百条。
   */
  function banner(key, text) {
    const message = safeText(text);
    fault('后台失败', message);
    try {
      const box = ensureBar();
      let row = rows.get(key);
      if (!row) {
        row = document.createElement('div');
        row.className = 'alertRow';
        const label = document.createElement('span');
        label.className = 'alertText';
        const copy = button('', '复制', 'alertAction');
        const close = button('', '关闭', 'alertAction');
        copy.onclick = async () => {
          copy.textContent = await copyText('【SaltLRC Studio 后台失败】' + label.textContent) ? '已复制' : '失败';
          setTimeout(() => { copy.textContent = '复制'; }, 1600);
        };
        close.onclick = () => clearBanner(key);
        row.append(label, copy, close);
        box.append(row);
        rows.set(key, row);
      }
      row.querySelector('.alertText').textContent = message;
      box.hidden = false;
    } catch { /* 横幅失败就只是没有横幅，消息已经在追踪里 */ }
  }

  /** 那件事恢复正常了 → 把对应那一行收掉。 */
  function clearBanner(key) {
    try {
      const row = rows.get(key);
      if (row) { row.remove(); rows.delete(key); }
      if (bar && !rows.size) bar.hidden = true;
    } catch { /* 忽略 */ }
  }

  /** 常规提示：仍旧走状态行（两个页面各自实现了 say）。 */
  function info(text) {
    try {
      if (typeof say === 'function') say(text);
    } catch { /* 忽略 */ }
  }

  return {fail, banner, clearBanner, info};
})();
window.Notify = Notify;
