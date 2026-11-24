// 波形提示条的转义：歌词文本是"不可信输入"，只能被显示，不能被当成标签。
//
// 判据不用"字符串里有没有 <"，而是**真的渲染一遍看有没有生出元素** ——
// 字符串匹配在"转义写错一半"的时候照样能过。
const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const web = path.join(__dirname, '..', 'saltlrc', 'web');
let failures = 0;
function check(name, ok, detail) {
  if (ok) { console.log('  ok   ' + name); return; }
  failures += 1;
  console.log('  FAIL ' + name + (detail === undefined ? '' : ' → ' + detail));
}

// common.js 是普通脚本（不是模块），取它定义的三个函数出来用。
const source = fs.readFileSync(path.join(web, 'common.js'), 'utf8');
const scope = new Function(source + '\nreturn {escapeHtml, tipTime, waveTipHtml};')();
const { escapeHtml, tipTime, waveTipHtml } = scope;

const dom = new JSDOM('<!doctype html><div id="tip"></div>');
const tip = dom.window.document.getElementById('tip');

// ---- 1) 转义本身 ---------------------------------------------------------
const nasty = '<img src=x onerror="alert(1)">';
check('escapeHtml 把尖括号转掉', !/[<>]/.test(escapeHtml(nasty)), escapeHtml(nasty));
check('escapeHtml 转了引号', !escapeHtml(nasty).includes('"'), escapeHtml(nasty));
check('escapeHtml 不丢内容', escapeHtml('a<b').includes('a') && escapeHtml('a<b').includes('b'),
      escapeHtml('a<b'));

// ---- 2) 渲染：带标签的歌词文本不许变成真的元素 -----------------------------
tip.innerHTML = waveTipHtml({
  kind: 'word', text: nasty, position: 1, total: 9, time: 65500, delta: 0,
});
check('提示条里没有生出 <img>', tip.querySelector('img') === null, tip.innerHTML);
check('提示条里没有生出任何带事件的元素',
      tip.querySelectorAll('[onerror],[onload],[onclick]').length === 0, tip.innerHTML);
check('原文照常显示（转义不是吞掉）', tip.textContent.includes('<img'), tip.textContent);

// 句首 / 句尾 那种带 hint 的分支也要过转义。
tip.innerHTML = waveTipHtml({ kind: 'end', hint: nasty, time: 1200 });
check('hint 分支同样不生元素', tip.querySelector('img') === null, tip.innerHTML);

// ---- 3) 正常内容不受影响 -------------------------------------------------
tip.innerHTML = waveTipHtml({
  kind: 'word', text: '月', position: 2, total: 4, time: 65500, delta: 120,
});
const plain = tip.textContent;
check('第几个字还在', /第\s*2\s*\/\s*4\s*字/.test(plain), plain);
check('时间读数还在', plain.includes(tipTime(65500)), plain);
check('移动量还在', plain.includes('+120ms'), plain);
check('有 delta 时不显示那条操作提示', !plain.includes('拖动调整'), plain);

console.log(failures ? ('提示条转义检查失败：' + failures + ' 项') : '提示条转义检查通过');
process.exit(failures ? 1 : 0);
