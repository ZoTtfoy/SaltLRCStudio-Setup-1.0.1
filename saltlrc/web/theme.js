"use strict";
// 主题（深色 / 浅色）。
//
// 谁是权威：**颜色只在 style.css 里**（`:root` 与 `html[data-theme="light"]`），
// 这份脚本不写任何颜色值，只负责切换那个属性、并把画布需要的取值读出来缓存。
//
// localStorage 里那份是**首屏用的缓存**，不是第二权威：它在页面最前面被读一次
// （见 index.html 的 <head>），用来避免"先画一帧深色再变浅色"；每次从服务器
// 读到设置之后都会被覆盖成 settings.json 里的值。
//
// 页面里只有这个模块能写 documentElement.dataset.theme。

const THEME_STORAGE = 'saltlrc-theme';

const Theme = (() => {
  let cache = null;          // 令牌取值缓存：主题一变就整体作废

  function normalise(value) { return value === 'light' ? 'light' : 'dark'; }

  /** 页面根节点；最小测试环境（只有打桩 document）里可能没有。 */
  function root() { return (typeof document !== 'undefined' && document.documentElement) || null; }

  /** 当前主题，'light' 或 'dark'。没有 data-theme 就是深色（默认）。 */
  function current() {
    const el = root();
    return normalise(el && el.dataset ? el.dataset.theme : 'dark');
  }

  function remember(theme) {
    try { localStorage.setItem(THEME_STORAGE, theme); } catch (error) { /* 隐私模式：只影响首屏那一帧 */ }
  }

  /** 切到指定主题：写属性、更新首屏缓存、清掉取值缓存、广播一次。 */
  function apply(value, rememberIt) {
    const theme = normalise(value);
    const el = root();
    if (el && el.dataset) el.dataset.theme = theme;
    if (rememberIt !== false) remember(theme);
    cache = null;
    if (typeof CustomEvent === 'function' && typeof document.dispatchEvent === 'function') {
      document.dispatchEvent(new CustomEvent('themechange', {detail: {theme: theme}}));
    }
    return theme;
  }

  /**
   * CSS 令牌的当前取值，读一次缓存一份。
   *
   * 画布每帧要取十几个颜色，`getComputedStyle` 不能每帧调用；缓存整份，
   * 主题变化时作废。取不到时返回空串，调用方按"这次别改颜色"处理。
   */
  function token(name) {
    if (!cache) cache = new Map();
    if (cache.has(name)) return cache.get(name);
    let value = '';
    const styleable = typeof getComputedStyle === 'function' && root();
    try {
      if (styleable) {
        value = String(getComputedStyle(root()).getPropertyValue(name) || '').trim();
      }
    } catch (error) { value = ''; }
    if (!value && styleable && typeof window !== 'undefined' && window.Trace
        && typeof window.Trace.fault === 'function') {
      window.Trace.fault('主题令牌读不到', name);
    }
    cache.set(name, value);
    return value;
  }

  return {apply: apply, current: current, token: token, storageKey: THEME_STORAGE};
})();
