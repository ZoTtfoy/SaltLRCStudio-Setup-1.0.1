// The <script src> order of a page, read from the page itself.
//
// The order is already stated once, in the markup. Every check that replays the
// page used to keep its own copy of the list, which is how splitting studio.js
// broke five of them at once: history.js was never loaded, so functions the page
// calls simply did not exist and the checks reported phantom failures.
//
// One authority — the page — and the checks derive from it.

const fs = require('fs');
const path = require('path');

module.exports = function pageScripts(page) {
  const file = path.join(__dirname, '..', 'saltlrc', 'web', page);
  const html = fs.readFileSync(file, 'utf8');
  const out = [];
  const re = /<script src="\/([A-Za-z0-9_.-]+)"><\/script>/g;
  let match;
  while ((match = re.exec(html)) !== null) out.push(match[1]);
  if (!out.length) throw new Error('页面里没有解析到任何 <script src>：' + file);
  return out;
};
