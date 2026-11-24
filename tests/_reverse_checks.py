"""反向验证每一处修复：把代码改回旧写法，测试必须变红。

"加了个测试然后全绿"不能证明测试有效 —— 要看见它在该红的时候红。
而且夹具自己也会错（例如只插 `<!--` 不插配对的 `-->`、或"原地加 hidden
而不是真搬按钮"），所以每条都要求：变异后**判红**、还原后**判绿**、红的理由对得上。

用法：python tests/_reverse_checks.py          # 全部
      python tests/_reverse_checks.py 3 7     # 只跑第 3、7 条
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable


def run(pattern, keyword):
    done = subprocess.run(
        [PY, '-m', 'unittest', 'discover', '-s', 'tests', '-p', pattern, '-t', 'tests', '-k', keyword],
        cwd=str(ROOT), capture_output=True, text=True, timeout=900)
    return done.returncode, done.stdout + done.stderr


CASES = [
    dict(
        label='1. 自动落盘退回"扫全队列取指纹"（每次编辑 O(队列)）',
        file='saltlrc/web/projects.js',
        pairs=[('  async flush() {\n    clearTimeout(this.timer);',
                '  async flush() {\n    clearTimeout(this.timer);\n'
                '    for (const item of this.ids.keys()) JSON.stringify(item.document || null);')],
        pattern='test_web_dom.py', keyword='autosave',
        expect='只序列化了真正变过的那份文档'),
    dict(
        label='2. 队列变了不再登记进会话快照（导入的歌崩溃后丢失）',
        file='saltlrc/web/studio.js',
        pairs=[('  touchSession(queue);\n', '')],
        pattern='test_web_dom.py', keyword='autosave',
        expect='会话快照一条一个键'),
    dict(
        label='3. 二进制出口又把字节过一遍文本编解码',
        file='saltlrc/server.py',
        pairs=[('        result = self._write_blob(Path(path), raw)',
                '        result = self._write_blob(Path(path), raw.decode("utf-8", "replace").encode("utf-8"))')],
        pattern='test_save_as.py', keyword='raw_bytes',
        expect='逐字节一致'),
    dict(
        label='4. 去掉会话记录的锁（并发建工程又丢记录/报 WinError 5）',
        file='saltlrc/projects.py',
        pairs=[('    with _SESSION_LOCK:\n', '    if True:\n')],
        pattern='test_projects.py', keyword='many_projects',
        expect='并发建工程'),
    dict(
        label='5. 容器里又走 base64（音频膨胀 33%、多两趟搬运）',
        file='saltlrc/web/document.js',
        pairs=[('    const audio = (audioBytes && audioBytes.byteLength) ? audioBytes : new Uint8Array(0);',
                '    const audio = (audioBytes && audioBytes.byteLength)\n'
                '      ? encoder.encode(bytesToBase64(audioBytes)) : new Uint8Array(0);')],
        pattern='test_web_pages.py', keyword='document_layer',
        expect='容器里音频来自文件尾巴'),
    dict(
        label='6. Salt 逐字又写回"全用 []"（逐字与逐行在文件里又长得一样）',
        file='saltlrc/exporter.py',
        pairs=[('''                body = "".join(f"<{stamp(w.start)}>{w.text}" for w in g.main.words)
                tail = f"<{stamp(g.main.end)}>" if g.main.end is not None else ""
                lines.append(f"[{stamp(g.start)}]{body}{tail}")''',
                '''                end = g.main.end if g.main.end is not None else g.main.words[-1].start
                lines.append("".join(f"[{stamp(w.start)}]{w.text}" for w in g.main.words)
                             + f"[{stamp(end)}]")''')],
        pattern='test_core.py', keyword='salt_keeps_line_and_word',
        expect='<00:01.250>月色'),
    dict(
        label='7. batch.py 又自己写一份音频扩展名清单',
        file='saltlrc/batch.py',
        pairs=[('AUDIO = audioformats.extensions()',
                'AUDIO = {".mp3", ".flac", ".m4a", ".wav", ".ogg", ".aac"}')],
        pattern='test_audioformats.py', keyword='',
        expect='audioformats.py'),
    dict(
        label='8.「缺逐字」又当回错误（一行的问题让整首降级 → 别的行逐字被截掉）',
        file='saltlrc/validator.py',
        pairs=[('''                bad("没有逐字时间（导出时这一行写成逐行形式）", "warning",''',
                '''                bad("缺少主歌词逐字时间",''')],
        pattern='test_core.py', keyword='word_and_line_forms_mix',
        expect='缺少主歌词逐字时间'),
    dict(
        label='9. 没有逐字的行又只写一个裸时间戳（那一行的文字被丢掉）',
        file='saltlrc/exporter.py',
        pairs=[('''                lines.append(f"[{stamp(g.start)}]{g.main.text}" if g.main.text.strip()
                             else f"[{stamp(g.start)}]")''',
                '''                lines.append(f"[{stamp(g.start)}]")''')],
        pattern='test_core.py', keyword='word_and_line_forms_mix',
        expect='风吹过'),
    dict(
        label='10. 分栏又内联写 grid-template-columns（内联压过 @media，断点失效）',
        file='saltlrc/web/splitter.js',
        pairs=[("    document.querySelector('main').style.setProperty('--split-left', clamped + 'px');",
                "    document.querySelector('main').style.gridTemplateColumns =\n"
                "      clamped + 'px 10px minmax(0,1fr)';")],
        pattern='test_web_pages.py', keyword='splitter_only',
        expect='gridTemplateColumns'),
    dict(
        label='11. 基础列定义写死宽度（不读变量 → 窄屏拖分栏失效）',
        file='saltlrc/web/style.css',
        pairs=[('grid-template-columns:min(var(--split-left),50%) minmax(0,1fr)}',
                'grid-template-columns:280px minmax(0,1fr)}')],
        pattern='test_web_pages.py', keyword='splitter_only',
        expect='--split-left'),
    dict(
        label='12. 窄屏不再隐藏把手（轨道数与网格项数对不上）',
        file='saltlrc/web/style.css',
        pairs=[('@media(max-width:1249px){#splitter{display:none}}',
                '@media(max-width:1249px){#splitter{opacity:.5}}')],
        pattern='test_web_pages.py', keyword='splitter_only',
        expect='藏起来'),
    dict(
        label='13. 删注册表退回"直接 DeleteKey"（取消关联留下半支键）',
        file='saltlrc/assoc.py',
        pairs=[('    for child in registry.children(path):\n        erase(registry, path + "\\\\" + child)\n',
                '    for child in []:\n        erase(registry, path + "\\\\" + child)\n')],
        pattern='test_assoc.py', keyword='whole_tree',
        expect='取消关联之后一个键都不该留下'),
    dict(
        label='14. 安装器也去写关联（两个写者）',
        file='packaging/installer.iss',
        pairs=[('[Files]', '[Registry]\nRoot: HKCU; Subkey: "Software\\Classes\\.saltproj"; ValueType: string; ValueName: ""; ValueData: "SaltLRCStudio.Project"\n\n[Files]')],
        pattern='test_assoc.py', keyword='installer_does_not_register',
        expect='安装器不许自己写关联'),
    dict(
        label='15. 页面直接读 reply.headers（响应不完整就把整页初始化弄坏）',
        file='saltlrc/web/studio.js',
        pairs=[("""    if (reply.status === 204) return;
    if (typeof reply.arrayBuffer !== 'function') return;
    if (reply.status !== 200) {""",
                """    if (!reply.ok || reply.status === 204) return;
    if (false) {"""),
               ("""    const header = reply.headers && typeof reply.headers.get === 'function'
      ? reply.headers.get('X-Project-Name') : null;
    const label = header ? decodeURIComponent(header) : '';""",
                """    const label = decodeURIComponent(reply.headers.get('X-Project-Name') || 'project.saltproj');""")],
        pattern='test_web_dom.py', keyword='render_and_bind_every_control',
        expect='不完整的响应'),
    dict(
        label='16. 旧版本的"打开方式"命令不再修（双击去开一个早就不存在的文件）',
        file='saltlrc/assoc.py',
        pairs=[("""    apps = applications_key(target)
    if reg.exists(apps):
        reg.write(apps + r"\\shell\\open\\command", None, command_for(target))
        # 图标也得给一份：走这一支时 Windows 取的是它的 DefaultIcon。
        reg.write(apps + r"\\DefaultIcon", None, icon)""",
                """    if False:
        pass""")],
        pattern='test_assoc.py', keyword='stale',
        expect='修完之后双击该落到本程序'),
    dict(
        label='17. 等待窗口用完又报成"码已过期"（页面停手 → 扫码后登不上）',
        file='saltlrc/login.py',
        pairs=[('    return QR_TIMEOUT, None\n', '    return QR_EXPIRED, None\n')],
        pattern='test_login.py', keyword='finished_wait_window',
        expect='不能报成'),
    dict(
        label='18. 页面把"等待窗口用完"当成"码已过期"（停手 → 确认后没人接）',
        file='saltlrc/web/library.js',
        pairs=[("      if (state.state === 'timeout') { $('qrState').textContent = '仍在等待扫码…'; continue; }\n",
                "      if (state.state === 'timeout') { $('qrState').textContent = '二维码已过期，请重新生成'; break; }\n")],
        pattern='test_web_dom.py', keyword='render_and_bind_every_control',
        expect='等待窗口超时之后继续轮询'),
    dict(
        label='19. 注释里又写回"当初/之所以"（中文判据失效）',
        file='saltlrc/web/common.js',
        pairs=[('"use strict";', '// 当初踩过坑，之所以这样写\n"use strict";')],
        pattern='test_comment_style.py', keyword='gains_new_traces',
        expect='这些文件的注释里出现了过程/缘由类说法'),
    dict(
        label='20. 注释里又写回英文的"讲原因"（英文判据失效）',
        file='saltlrc/web/common.js',
        pairs=[('// Helpers shared by every page.',
                '// previously this lived in studio.js because the order differed\n'
                '// Helpers shared by every page.')],
        pattern='test_comment_style.py', keyword='gains_new_traces',
        expect='这些文件的注释里出现了过程/缘由类说法'),
    dict(
        label='21. 扫描器又把"三引号数据"当成注释（模板串里的代码被误判）',
        file='tests/test_comment_style.py',
        pairs=[('                elif token.type == tokenize.STRING and token.start[0] in docstrings:',
                '                elif token.type == tokenize.STRING '
                "and token.string.lstrip().startswith(('\"\"\"', \"'''\")):")],
        pattern='test_comment_style.py', keyword='triple_quoted',
        expect='把三引号数据当成注释了'),
    dict(
        label='22. 静态层的判据里去掉主题（换主题后画面停在旧颜色上）',
        file='saltlrc/web/waveform.js',
        pairs=[('           this.layerPeaks === this.peaks.length &&\n'
                '           this.layerTheme === Theme.current();',
                '           this.layerPeaks === this.peaks.length;')],
        pattern='test_theme.py', keyword='behaviour',
        expect='换主题后第一次绘制必须重画静态层'),
    dict(
        label='23. 深色里新加一个颜色令牌却不给浅色取值（浅色下留一块深斑）',
        file='saltlrc/web/style.css',
        pairs=[('  --bg:#141d23;', '  --bg:#141d23; --probe:#123456;')],
        pattern='test_theme.py', keyword='light_value',
        expect='浅色块里缺少这些令牌'),
    dict(
        label='24. 浅色取值发浑（机械翻转明度就会这样）',
        file='saltlrc/web/style.css',
        pairs=[('  --panel:hsl(168 18% 98%);', '  --panel:hsl(168 60% 62%);')],
        pattern='test_theme.py', keyword='muddy',
        expect='浅色取值发浑'),
    dict(
        label='25. 页面不在首屏前定主题（先画一帧深色再变浅色）',
        file='saltlrc/web/index.html',
        pairs=[("try{var t=localStorage.getItem('saltlrc-theme');",
                'try{var t=null;')],
        pattern='test_theme.py', keyword='first_paint',
        expect='没有从首屏缓存里读主题'),
    dict(
        label='26. 插入新行又不看上一行的句尾（新行落进别人唱过的那一段）',
        file='saltlrc/edits.py',
        pairs=[('            previous_end = _sung_end(previous) if previous is not None else None\n'
                '            if previous_end is not None:\n'
                '                low = max(low, previous_end + MIN_SLACK)\n',
                '            previous_end = None\n')],
        pattern='test_edits.py', keyword='clear_of_the_previous',
        expect='AssertionError'),
    dict(
        label='27. 刻度命中退回"最后画的那条"（同一位置点两次抓到不同的东西）',
        file='saltlrc/web/waveform.js',
        pairs=[("""    let best = null, bestDistance = Infinity, bestRank = Infinity;
    for (const hit of this.hits) {
      const distance = Math.abs(hit.x + shift - x);
      if (distance > tolerance) continue;
      if (y < hit.y || y > hit.y + hit.height) continue;
      const rank = SaltWaveform.TICK_RANK[hit.kind];
      const wins = !best || distance < bestDistance - 1
        || (distance <= bestDistance + 1 && rank < bestRank);
      if (wins) { best = hit; bestDistance = distance; bestRank = rank; }
    }
    return best;""",
                """    return [...this.hits].reverse().find(
      h => x >= h.x - tolerance + shift && x <= h.x + tolerance + shift
           && y >= h.y && y <= h.y + h.height) || null;""")],
        pattern='test_web_pages.py', keyword='tick_grab_and_drawing',
        expect='刻度线检查失败'),
    dict(
        label='28. 句首/句尾画得和逐字边界一样粗（几条挤在一起分不出抓的是哪条）',
        file='saltlrc/web/waveform.js',
        pairs=[('  static TICK_WIDTH_LINE = 2;', '  static TICK_WIDTH_LINE = 1;')],
        pattern='test_web_pages.py', keyword='tick_grab_and_drawing',
        expect='刻度线检查失败'),
    dict(
        label='29. 设置改回"必须点保存才生效"（勾选项不再即时保存）',
        file='saltlrc/web/studio.js',
        pairs=[('  saveSettings({quiet: true});\n});', '  void event;\n});')],
        pattern='test_web_dom.py', keyword='render_and_bind_every_control',
        expect='改一个勾选项就存下并生效'),
    dict(
        label='30. 预览又变回常驻的大面板（不播放也占一大块）',
        file='saltlrc/web/style.css',
        pairs=[('#preview .prow.dim, #preview .prow.trans{display:none}',
                '#preview .prow.dim, #preview .prow.trans{display:block}')],
        pattern='test_web_pages.py', keyword='keeps_one_size',
        expect='空闲态必须把相邻两行与翻译行收起来'),
    dict(
        label='31. 提示条又把歌词文本直接插进 innerHTML（字里带标签就会执行）',
        file='saltlrc/web/common.js',
        pairs=[('${escapeHtml(text.slice(0, 24))}', '${text.slice(0, 24)}')],
        pattern='test_web_pages.py', keyword='tip_escapes',
        expect='提示条转义检查失败'),
    dict(
        label='32. /api/lyric 又不限后缀（同源脚本能把任意本地文件读走）',
        file='saltlrc/server.py',
        pairs=[('            if Path(path).suffix.lower() != ".lrc":\n'
                '                return self.send(400, {"error": "只能读取 .lrc 歌词文件"})',
                '            if False:\n'
                '                return self.send(400, {"error": "只能读取 .lrc 歌词文件"})')],
        pattern='test_server.py', keyword='lyric_read_only',
        expect='.lrc'),
    dict(
        label='33. 调试口令退回写死的默认值（手册里的口令不算口令）',
        file='saltlrc/diagnostics.py',
        pairs=[('    password = os.environ.get("SALTLRC_DEBUG_PASSWORD")\n'
                '    if not password:\n'
                '        return None',
                '    password = os.environ.get("SALTLRC_DEBUG_PASSWORD") or "saltlrc"')],
        pattern='test_server.py', keyword='debug_page',
        expect='401 != 403'),
]


def main():
    problems = []
    # 只跑指定的那几条（调试/补跑用）：python tests/_reverse_v0240.py 4
    # 按**标签号**筛（`'7. …'` → `'7'`），不是列表位置 —— 新用例插在中间时
    # 位置会整体后移，用它筛会跑错条（第一次就这么跑错了，红了一条不相干的）。
    wanted = [one for one in sys.argv[1:] if one]
    cases = [case for case in CASES
             if not wanted or case['label'].split('.')[0].strip() in wanted]
    for case in cases:
        path = ROOT / case['file']
        original = path.read_text(encoding='utf-8')
        code, text = run(case['pattern'], case['keyword'])
        if code != 0:
            problems.append(case['label'] + ' —— 基线就是红的，这条验证没有意义')
            print('SKIP ' + case['label'])
            print(text[-800:])
            continue
        broken = original
        ok = True
        for old, new in case['pairs']:
            if old not in broken:
                problems.append(case['label'] + ' —— 找不到要替换的原文（夹具失效）')
                print('SKIP ' + case['label'])
                ok = False
                break
            broken = broken.replace(old, new, 1)
        if not ok:
            continue
        path.write_text(broken, encoding='utf-8')
        try:
            code, text = run(case['pattern'], case['keyword'])
        finally:
            path.write_text(original, encoding='utf-8')
        code2, _ = run(case['pattern'], case['keyword'])
        # 判红：只要**这条用例没通过**、而且报错文本里出现了预期的字样即可。
        # 不能只认 AssertionError —— 被测代码抛出的 ValueError 逃出测试时，
        # unittest 报的是 **ERROR**，而基线检查已经保证过"文件本身能正常导入"。
        red = code != 0 and case['expect'] in text
        print('%s %s' % ('RED ' if red else 'GREEN(!)', case['label']))
        if not red:
            problems.append(case['label'] + ' —— 改回旧写法但测试没红（或红的理由不对）')
            print(text[-1200:])
        elif code2 != 0:
            problems.append(case['label'] + ' —— 还原之后没有回到绿色')

    print()
    if problems:
        print('反向验证不通过：')
        for one in problems:
            print('  - ' + one)
        raise SystemExit(1)
    print('反向验证通过：%d 条全部变红（理由对得上），且还原后全绿。' % len(cases))


if __name__ == '__main__':
    main()
