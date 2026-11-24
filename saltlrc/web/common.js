"use strict";
// Helpers shared by every page. Keep this file tiny: each function here must
// work on both the library page and the waveform editor, so nothing shared
// may live in a page-specific script.

// 把一段文本转成"只会被显示、不会被当成标签"的 HTML。
//
// 歌词文本是**不可信输入**：它来自联网的歌词源或用户自己的文件，里面写什么都可能。
// 凡是要走进 `innerHTML` 的文本，一律先过这里 —— 否则一个字里带
// `<img onerror=…>` 就会在应用自己的源里执行脚本，而它有权调用本机接口。
function escapeHtml(value) {
  return String(value == null ? '' : value).replace(/[&<>"']/g, (ch) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  })[ch]);
}

// 时间读数：m:ss.mmm。
function tipTime(ms) {
  if (ms == null) return '—';
  const total = Math.max(0, Math.round(ms));
  const minutes = Math.floor(total / 60000);
  const seconds = Math.floor((total % 60000) / 1000);
  return `${minutes}:${String(seconds).padStart(2, '0')}.${String(total % 1000).padStart(3, '0')}`;
}

// 波形提示条的内容。
//
// **纯函数**：只吃 info、只吐一串 HTML。测试直接喂一段带标签的歌词文本，
// 验证它只会被显示、不会变成真的元素 —— 字里带 HTML 靠肉眼看不出来。
function waveTipHtml(info) {
  const parts = [];
  if (info.kind === 'word' && info.total) {
    parts.push(`第 <b>${info.position}</b>/${info.total} 字`);
  } else if (info.hint) {
    parts.push(`<b>${escapeHtml(info.hint)}</b>`);
  }
  const text = (info.text || '').trim();
  if (text) parts.push(`<b>${escapeHtml(text.slice(0, 24))}${text.length > 24 ? '…' : ''}</b>`);
  parts.push(`<span class="tipTime">${tipTime(info.time)}</span>`);
  if (info.delta) {
    const cls = info.delta > 0 ? 'plus' : 'minus';
    parts.push(`<span class="tipDelta ${cls}">${info.delta > 0 ? '+' : ''}${info.delta}ms</span>`);
  }
  if (info.ripple && info.kind === 'word' && info.followers) {
    parts.push(`<span class="tipHint">连同后面 ${info.followers} 个字一起移动</span>`);
  } else if (!info.delta) {
    parts.push('<span class="tipHint">拖动调整 · 单击选中后 ←/→ 微调 · Alt 拖动连同后面的字</span>');
  }
  return parts.join(' ');
}

function bind(id, fn) {
  const element = document.getElementById(id);
  if (!element) {
    // A missing element here means the control is DEAD: it still renders, the
    // click does nothing, the handler is never entered, and the packaged window
    // has no console to show the warning. An element declared after this
    // script's tag does not exist yet at load time, so the warning alone is
    // easy to miss; the fault also goes into the L1 interaction
    // journal.
    const message = 'bind: #' + id + ' 不存在（脚本是否写在元素之前？）';
    console.warn(message);
    window.Trace?.fault?.('控件无法绑定', message);
    return;
  }
  element.onclick = async () => {
    try { await fn(); }
    catch (error) {
      // 用户主动按下的按钮失败了 → **必须出声，而且要看得见**。
      // 两条一起走，各有各的用处：
      //   · 状态行 `say()` 是**记录**（原有行为，页面级检查与用户习惯都看它）；
      //   · `Notify.fail` 是**叫住你**（模态弹框）—— 只写状态行不够，它在页面下方、
      //     还有别的东西在写它，用户很容易根本没看见，看到的就只是"按钮没反应"。
      // Notify 自己兜住所有异常，所以这里不会因为它再抛一层。
      if (typeof say === 'function') say(error.message);
      if (window.Notify && window.Notify.fail) window.Notify.fail('操作失败', error);
      else if (typeof say !== 'function') alert(error.message);
    }
  };
}
function download(name, data, type) {
  const url = URL.createObjectURL(new Blob([data], {type})), a = document.createElement('a');
  a.href = url; a.download = name; a.click(); setTimeout(() => URL.revokeObjectURL(url), 10000);
}
