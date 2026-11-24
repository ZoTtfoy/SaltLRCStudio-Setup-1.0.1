"use strict";
/**
 * Draggable column splitter for the two-pane layout.
 *
 * Drop it into any page with `main > aside`. Dragging rewrites the grid's
 * first column width in place, so the rest of the layout (which keeps using
 * `minmax(0,1fr)`) cannot break. Preference is remembered per page.
 */
(function () {
  const MIN = 200, MAX = 560;

  // **只写一个 CSS 变量 `--split-left`，不写内联的 grid-template-columns**。
  //
  // 内联样式压过所有样式表规则（包括 @media）。样式表只在窗口 ≥1250px 时用三轨布局，
  // 并在这以下把 #splitter 设成 display:none；窗口拖到 1250px 以下时把手不再是网格项，
  // 而内联模板仍是三轨 → `section`（整个编辑器）掉进只有 10px 宽的第二轨。
  //
  // 所以：**CSS 拥有网格与断点，JS 只提供那个宽度值**。
  function apply(width, save) {
    const clamped = Math.max(MIN, Math.min(MAX, Math.round(width)));
    document.querySelector('main').style.setProperty('--split-left', clamped + 'px');
    if (save) localStorage.setItem(KEY, String(clamped));
    return clamped;
  }

  const KEY = 'saltlrc-split:' + location.pathname;

  function init() {
    const main = document.querySelector('main');
    const aside = main && main.querySelector('aside');
    if (!aside) return;
    const handle = document.createElement('div');
    handle.id = 'splitter';
    handle.title = '拖动调整左右宽度；双击恢复默认';
    aside.after(handle);

    const saved = Number(localStorage.getItem(KEY));
    if (saved >= MIN && saved <= MAX) apply(saved, false);

    let start = null;
    handle.addEventListener('pointerdown', (event) => {
      start = {x: event.clientX, width: aside.getBoundingClientRect().width};
      handle.classList.add('dragging');
      handle.setPointerCapture(event.pointerId);
      event.preventDefault();
    });
    handle.addEventListener('pointermove', (event) => {
      if (start) apply(start.width + (event.clientX - start.x), false);
    });
    const stop = (event) => {
      if (!start) return;
      apply(start.width + (event.clientX - start.x), true);
      start = null;
      handle.classList.remove('dragging');
    };
    handle.addEventListener('pointerup', stop);
    handle.addEventListener('pointercancel', () => { start = null; handle.classList.remove('dragging'); });
    handle.addEventListener('dblclick', () => {
      // 双击恢复默认：把变量去掉，宽度就回到样式表里那个默认值。
      localStorage.removeItem(KEY);
      main.style.removeProperty('--split-left');
    });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
