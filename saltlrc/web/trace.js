/* 操作追踪 · interaction trace (client half)
 *
 * 这个浮层的颜色是**写死的深色**：它是叠在工作区上的诊断面板，不走主题令牌。
 * Records what the user did, what the app answered, and — the useful part —
 * which gestures produced NO visible effect at all. "点了没反应" looks the same
 * whether the click never arrived, the handler returned silently, or the effect
 * happened somewhere invisible; only the sequence can tell them apart.
 *
 * The record itself lives in the core (`saltlrc/trace.py`) so it survives a
 * reload and can be exported; this file is the thin capture layer: a
 * gesture can only be seen from inside the page.
 *
 * Rules:
 *  - recording must never break what it records (every hook is wrapped);
 *  - nothing is sent per event: entries are batched, so tracing stays cheap;
 *  - the stall judgement is made server-side, from the same data the export
 *    prints, so what you read is what was measured.
 */
(function () {
  const TRACE = {at: Date.now(), gesture: null, seq: 0, lastGestureAt: 0};
  const WINDOW_MS = 900;          // keep in step with saltlrc/trace.py STALL_MS
  const FLUSH_MS = 400;
  const PENDING = [];
  let seen = 0;                   // observations since the last gesture

  const label = (element) => {
    if (!element) return '?';
    const id = element.id ? '#' + element.id : '';
    const text = (element.textContent || '').trim().slice(0, 24);
    return (element.tagName || '?').toLowerCase() + id + (text ? '「' + text + '」' : '');
  };

  // The server does the analysis; the page only says what happened, and when.
  //
  // `t` is an epoch millisecond stamp, NOT "ms since this page loaded". The
  // page and the core share one machine clock, so an epoch stamp is comparable
  // across batches, reloads and tabs; a page-relative one is not, and the core
  // cannot repair it after the fact — entries leave here in batches (on gesture
  // boundaries and on a timer), so a gesture and the observation that follows it
  // often travel separately. `since` is kept alongside purely as a cross-check.
  function push(kind, name, detail) {
    TRACE.seq += 1;
    const now = Date.now();
    const event = {kind, name: String(name).slice(0, 120),
                   detail: detail === undefined || detail === null ? '' : String(detail).slice(0, 400),
                   t: now, since: now - TRACE.at, page: location.pathname,
                   gesture: TRACE.gesture};
    PENDING.push(event);
    if (kind !== 'gesture') seen += 1;
    if (PENDING.length >= 40) flush();
    return event;
  }

  function flush() {
    if (!PENDING.length) return;
    const batch = PENDING.splice(0, PENDING.length);
    try {
      fetch('/api/trace', {method: 'POST', headers: {'Content-Type': 'application/json'},
                           body: JSON.stringify({events: batch})}).catch(() => {});
    } catch (error) { /* tracing must never break the page */ }
  }

  /* What the page observed for itself. A click that opens a dialog, applies an
   * edit or writes a rule change all announce it here — that is what makes
   * "did this gesture do anything?" answerable without guessing at the DOM. */
  function step(name, detail) { push('step', name, detail); }

  /* A fault the page detected by itself (a control that could not be wired, a
   * required element that is missing). It is an `error`, not a `step`: the
   * report groups faults on their own, so a fault the page already knew about
   * is not buried in the ordinary narration. Flushed at once: a load-time
   * fault happens before any gesture, so nothing else would carry it. */
  function fault(name, detail) { const event = push('error', name, detail); flush(); return event; }

  /* Watch one gesture: everything observed until the next gesture belongs to it. */
  function gesture(name, detail, element) {
    flush();                                   // the previous gesture's tail first
    seen = 0;
    TRACE.gesture = (TRACE.gesture || 0) + 1;
    TRACE.lastGestureAt = Date.now();
    const event = push('gesture', name, detail || label(element));
    // If nothing observable followed within the window, say so from the page too:
    // the server reaches the same verdict, this only makes it appear promptly.
    setTimeout(() => {
      if (seen === 0 && TRACE.gesture === event.gesture) {
        push('step', '看起来没有反应', '这个操作之后没有任何请求、对话框变化或状态变化');
        flush();
      }
    }, WINDOW_MS + 50);
    return event;
  }

  // ---- capture: clicks, typing, and dialog life-cycle ---------------------
  document.addEventListener('click', (event) => {
    const target = event.target && event.target.closest
      ? event.target.closest('button,a,[role=button],.item,.hstep,.wordCell,input[type=checkbox],label')
      : null;
    gesture('click', label(target || event.target), target);
  }, true);

  document.addEventListener('keydown', (event) => {
    if (event.key === 'Tab' || event.key === 'Shift') return;
    gesture('key', event.key + (event.ctrlKey ? ' Ctrl' : '') + (event.shiftKey ? ' Shift' : ''));
  }, true);

  document.addEventListener('change', (event) => {
    const target = event.target;
    if (!target || !target.tagName) return;
    if (target.tagName === 'INPUT' || target.tagName === 'SELECT' || target.tagName === 'TEXTAREA') {
      push('step', '改了字段', label(target) + ' = ' + String(target.value).slice(0, 40));
    }
  }, true);

  for (const type of ['close', 'cancel']) {
    document.addEventListener(type, (event) => {
      if (event.target && event.target.tagName === 'DIALOG') step('对话框关闭', label(event.target));
    }, true);
  }
  // `showModal()` has no event, so opening is reported by the app calling
  // Trace.dialogOpen() — see the word editors.

  // ---- capture: every API call, its status and how long it took ----------
  const realFetch = window.fetch.bind(window);
  window.fetch = function (input, init) {
    const url = typeof input === 'string' ? input : (input && input.url) || '';
    if (!url.includes('/api/') || url.includes('/api/trace')) return realFetch(input, init);
    const route = url.split('/api/')[1];
    const started = Date.now();
    push('api-start', route);
    return realFetch(input, init).then(
      (response) => {
        push('api-end', route, response.status + ' · ' + (Date.now() - started) + 'ms');
        return response;
      },
      (error) => {
        push('error', route, '请求失败：' + (error && error.message));
        throw error;
      });
  };

  window.addEventListener('error', (event) => {
    push('error', '脚本错误', (event.message || '') + ' @' + (event.filename || ''));
  });
  window.addEventListener('unhandledrejection', (event) => {
    const reason = event.reason;
    push('error', '未处理的异常', (reason && reason.message) || String(reason));
  });
  window.addEventListener('pagehide', flush);

  window.Trace = {
    step,
    fault,
    gesture,
    dialogOpen: (element) => step('对话框打开', label(element)),
    flush,
    /* The panel's own button: hand the whole record to the user as text, so it
     * can be pasted straight into a report. */
    async report() {
      flush();
      const response = await realFetch('/api/trace', {
        method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'});
      const data = await response.json();
      return text(data);
    },
  };

  // ---- the panel: see it, copy it, export it -----------------------------
  /* The server rebases the page's relative clock onto its own, so `t` is an
   * epoch value here — print it as a time, not as "ms". */
  function stamp(ms) {
    if (typeof ms !== 'number' || ms < 100000000000) return String(ms) + 'ms';
    const at = new Date(ms);
    const pad = (n, w) => String(n).padStart(w || 2, '0');
    return pad(at.getHours()) + ':' + pad(at.getMinutes()) + ':' + pad(at.getSeconds()) +
           '.' + pad(at.getMilliseconds(), 3);
  }

  function text(data) {
    const lines = ['SaltLRC 操作追踪 · 共 ' + data.total + ' 条',
                   '手势 ' + data.analysis.gestures + ' 个，其中 ' +
                   data.analysis.stalled.length + ' 个没有可见效果，错误 ' +
                   data.analysis.errors.length + ' 条', ''];
    for (const item of data.events) {
      lines.push(stamp(item.t) + ' ' + item.kind + ' ' + item.name +
                 (item.detail ? ' · ' + item.detail : ''));
    }
    if (data.analysis.errors.length) {
      lines.push('', '== 错误 ==');
      for (const item of data.analysis.errors) {
        lines.push('  ' + stamp(item.t) + ' ' + item.name + ' ' + (item.detail || ''));
      }
    }
    if (data.analysis.stalled.length) {
      lines.push('', '== 点了却没有任何反应的（最可疑） ==');
      for (const item of data.analysis.stalled) {
        lines.push('  ' + stamp(item.t) + ' ' + (item.page || '') + ' ' + item.name + ' ' +
                   (item.detail || '') + (item.late_ms ? '（晚了 ' + item.late_ms + 'ms）' : ''));
      }
    }
    return lines.join('\n');
  }

  function build() {
    const host = document.createElement('div');
    host.id = 'tracePanel';
    host.style.cssText = 'position:fixed;left:14px;bottom:14px;z-index:98;width:420px;' +
      'max-height:52vh;display:none;flex-direction:column;background:rgba(9,22,27,.95);' +
      'color:#dceee9;border:1px solid #2b5a5c;border-radius:12px;font:12px/1.5 Consolas,Menlo,monospace;' +
      'box-shadow:0 10px 30px rgba(0,0,0,.5);padding:10px 12px';
    host.innerHTML =
      '<div style="display:flex;align-items:center;gap:8px;margin-bottom:6px">' +
      '<b style="flex:1">操作追踪</b>' +
      '<button id="traceCopy" style="font:11px inherit;padding:3px 8px">复制全部</button>' +
      '<button id="traceClear" style="font:11px inherit;padding:3px 8px">清空</button>' +
      '<button id="traceClose" style="font:11px inherit;padding:3px 8px">×</button></div>' +
      '<div id="traceBody" style="overflow:auto;white-space:pre-wrap;color:#a9c9c2"></div>';
    document.body.append(host);
    host.querySelector('#traceClose').onclick = () => { host.style.display = 'none'; };
    host.querySelector('#traceClear').onclick = () => {
      realFetch('/api/trace/clear', {method: 'POST', headers: {'Content-Type': 'application/json'},
                                     body: '{}'}).then(() => refresh());
    };
    host.querySelector('#traceCopy').onclick = async (event) => {
      const content = await Trace.report();
      try { await navigator.clipboard.writeText(content); event.target.textContent = '已复制'; }
      catch (error) { window.prompt('复制下面的内容：', content); }
      setTimeout(() => { event.target.textContent = '复制全部'; }, 1500);
    };
    return host;
  }

  let panel = null;
  async function refresh() {
    if (!panel || panel.style.display === 'none') return;
    try {
      const content = await Trace.report();
      panel.querySelector('#traceBody').textContent = content;
    } catch (error) { /* keep the last view */ }
  }

  window.Trace.toggle = function () {
    if (!panel) panel = build();
    const showing = panel.style.display === 'none';
    panel.style.display = showing ? 'flex' : 'none';
    if (showing) refresh();
    return showing;
  };

  // Ctrl+Shift+T opens the panel. Not rebindable: this is the diagnostic path and
  // it has to work even when the user's own key bindings are the thing that broke.
  document.addEventListener('keydown', (event) => {
    if (event.ctrlKey && event.shiftKey && event.key.toLowerCase() === 't') {
      event.preventDefault();
      window.Trace.toggle();
    }
  });

  // ---- self-check: a <dialog> that is not open but is still rendered -------
  //
  // This is how a broken dialog presents. Setting `display` on a `<dialog>` in
  // author CSS beats the engine's own `dialog:not([open]){display:none}` —
  // author styles outrank the user-agent sheet whatever the specificity — so the
  // closed box keeps rendering, absolutely positioned at its place in the
  // document (near the end of <body>), with NO backdrop, looking like a stray
  // panel that "won't go away". Reported only when it happens, so a healthy page
  // adds nothing to the record. An engine with no opinion (jsdom returns '' for
  // computed display) is not counted, or every harness run would be noise.
  let lastAnomaly = null;
  function dialogAnomaly() {
    const parts = [];
    for (const box of document.querySelectorAll('dialog')) {
      if (box.open) continue;
      let display = '';
      try { display = getComputedStyle(box).display; } catch (error) { continue; }
      if (display === 'none' || display === '') continue;
      let rect = '';
      try {
        const r = box.getBoundingClientRect();
        rect = ' @' + Math.round(r.left) + ',' + Math.round(r.top) + ' ' +
               Math.round(r.width) + 'x' + Math.round(r.height);
      } catch (error) { /* no layout info here */ }
      parts.push('#' + (box.id || '?') + ' 未打开却在显示（display=' + display + '）' + rect);
    }
    return parts.join(' | ');
  }

  setInterval(() => {
    const anomaly = dialogAnomaly();
    if (anomaly !== lastAnomaly) {
      lastAnomaly = anomaly;
      if (anomaly) push('step', '对话框异常可见', anomaly);
    }
    flush(); refresh();
  }, FLUSH_MS * 4);
  window.addEventListener('load', () => { push('step', '页面就绪', location.pathname); flush(); });
})();
