"""打包与发布：把"发布前必须成立的事"写成断言。

`packaging/` 里的东西以前没有任何测试守着 —— `installer.iss` 和 `release.ps1`
都是"写一次、之后靠记性"的脚本，而这类脚本坏掉的方式很安静：
`Source:` 指向的文件被改名/搬走了，直到发布那天编译失败才发现。

这里不追求"能编译"（本机没装 Inno Setup，编译不了），而是把**能静态验证的部分**全验掉：
引用的每个文件真的在、版本号与唯一权威一致、AppId 稳定不变、
以及"卸载不许删用户数据"这条规矩还在。
"""
import os
import re
import shutil
import subprocess
import sys
import time
import unittest
from pathlib import Path

from _scratch import scratch

ROOT = Path(__file__).resolve().parent.parent
PACKAGING = ROOT / 'packaging'
ISS = PACKAGING / 'installer.iss'
MACROS = {'{#AppExe}': 'SaltLRCStudio.exe', '{#AppName}': 'SaltLRC Studio'}


class InstallerTests(unittest.TestCase):
    def setUp(self):
        if not ISS.is_file():
            self.skipTest('installer.iss 不在（源码树可独立分发）')
        raw = ISS.read_text(encoding='utf-8')
        self.text = raw
        # `;` 开头是注释 —— 注释里会**引用**被禁用的写法（"这里故意没有 [UninstallDelete]"），
        # 所以判"有没有这个东西"必须去注释后再看。这是本项目重复过的坑：
        # CSS 那条测试也被注释里的旧选择器骗过一次。
        self.live = re.sub(r'(?m)^\s*;.*$', '', raw)

    def test_the_version_default_matches_the_one_authority(self):
        """安装包里的版本号默认值必须跟 `saltlrc/__init__.py` 一致。

        release.ps1 会用 `/DAppVersion=` 覆盖它，但**单独编译**（例如直接用 ISCC）
        时用的就是这个默认值 —— 漂了就会做出一个"版本号对不上"的安装包。
        """
        from saltlrc import __version__
        match = re.search(r'#define AppVersion "([^"]+)"', self.text)
        self.assertIsNotNone(match, 'installer.iss 里没有 AppVersion')
        self.assertEqual(match.group(1), __version__, 'installer.iss 的版本号没跟上')

    def test_every_file_it_ships_actually_exists(self):
        """`Source:` 指向的每个文件都必须真的在 —— 改名/搬走后要立刻发现。

        这类脚本只在这里安静坏掉：`Source:` 指向的文件被改名/搬走后，要到编译期才失败。
        """
        if not (ROOT / 'dist').is_dir():
            self.skipTest('还没有构建产物（dist/ 不存在）：源码树可以独立分发')
        sources = re.findall(r'Source:\s*"([^"]+)"', self.text)
        self.assertTrue(sources, '没解析到任何 Source:')
        missing = []
        for entry in sources:
            cleaned = entry
            for name, value in MACROS.items():
                cleaned = cleaned.replace(name, value)
            if '{#' in cleaned:
                missing.append('%s（有没展开的宏）' % entry)
                continue
            if not (PACKAGING / cleaned).resolve().is_file():
                missing.append(entry)
        self.assertEqual(missing, [], '安装包引用了不存在的文件：%s' % missing)

    def test_it_installs_per_user_and_needs_no_admin(self):
        """按用户安装：不写 Program Files、不要管理员 —— 和"本机工具"的定位一致。"""
        self.assertIn('PrivilegesRequired=lowest', self.text)
        self.assertIn('DefaultDirName={autopf}', self.text)

    def test_uninstalling_never_deletes_user_data(self):
        """卸载不许删凭据/设置/工程 —— 不可逆，而且用户预见不到。

        规矩写在这里，而不是靠"记得别加"：`[UninstallDelete]` 一出现就红。
        """
        self.assertNotIn('[UninstallDelete]', self.live,
                         '卸载不许删 %LOCALAPPDATA%\\SaltLRCStudio（凭据/设置/工程都在那儿）')
        self.assertIn('卸载不碰用户数据', self.text, '这条规矩要写在脚本里，别只留在脑子里')

    def test_the_app_id_is_a_stable_guid(self):
        """AppId 是升级安装认出"同一个程序"的凭据 —— 换成随机 GUID 会并排装出两个来。

        ⚠️ Inno Setup 里 `{{` 是**转义成一个字面左大括号**，所以规范写法是
        `AppId={{GUID}`（结尾只有**一个** `}`）—— 它代表的值是 `{GUID}`。
        写成 `{{GUID}}` 会多出一个字面 `}`。这条正则按规范写法匹配，别"顺手补齐"大括号。
        """
        match = re.search(r'#define AppId "\{\{([0-9A-Fa-f-]{36})\}"', self.live)
        self.assertIsNotNone(match, 'AppId 必须是稳定的 GUID（Inno 写法：AppId={{GUID}）')
        self.assertEqual(match.group(1).upper(), '7C1F2D34-9B5A-4E71-A6C3-2F0B9D8E4A15',
                         'AppId 不许改：改了就不是升级，是并排装两个')

    def test_a_powershell_script_with_chinese_must_carry_a_utf8_bom(self):
        """带中文的 `.ps1` **必须**存成 UTF-8 **带 BOM**。

        这条不是洁癖：PowerShell 5.1 按系统 ANSI（本机是 GBK）解码没有 BOM 的脚本，
        中文全变乱码，而且 UTF-8 字节序列会**吃掉字符串的收尾引号** →
        一片"表达式或语句中包含意外的标记"，整段脚本跑不起来。

        （对照：**数据文件**反过来 —— `使用说明.txt` / `pyproject.toml` 不该有 BOM，
        所以脚本里写文件用的是 `Write-NoBom`。两件事都要对。）
        """
        scripts = sorted(PACKAGING.glob('*.ps1'))
        if not scripts:
            self.skipTest('packaging/ 里没有 .ps1')
        problems = []
        for path in scripts:
            raw = path.read_bytes()
            ascii_only = all(byte < 128 for byte in raw)
            has_bom = raw.startswith(b'\xef\xbb\xbf')
            if not ascii_only and not has_bom:
                problems.append(path.name + '（含非 ASCII 但没有 BOM → PS 5.1 会乱码）')
        self.assertEqual(problems, [], '这些脚本要补 UTF-8 BOM：%s' % problems)

    def test_the_release_script_covers_the_checklist(self):
        """发布脚本必须把"每次手做的那一串"都做掉，而且**出错就停**。

        漏一步的代价是"本机跑的是新版、交付出去的是旧版"（本项目第 1 条老坑）。
        """
        path = PACKAGING / 'release.ps1'
        if not path.is_file():
            self.skipTest('release.ps1 不在（源码树可独立分发）')
        script = path.read_text(encoding='utf-8')
        for step, why in [('unittest', '跑测试'),
                          ('build.ps1', '打包'),
                          ('smoke-exe.ps1', 'exe 冒烟'),
                          ('Get-FileHash', '交付前比对哈希'),
                          ('--no-browser', '从交付路径启动时不许弹窗'),
                          ('ErrorActionPreference', '出错要停，不许继续往下做')]:
            self.assertIn(step, script, 'release.ps1 缺少：%s（%s）' % (step, why))

    def test_the_release_gate_only_accepts_a_fully_green_suite(self):
        """发布闸门只认全绿（允许 skip）—— 没有"已知失败"这一档。

        特例（`$knownOnly`：允许**恰好一个** FAIL/ERROR，只要输出里出现
        `No module named 'numpy'`）本身就是风险：**"允许一个失败"等于给所有失败
        留了一扇门**，而且它让"全量测试"这个信号从 100% 降成"除了一处"。
        脚本里出现 `knownOnly` 或 `No module named 'numpy'` 即判失败。

        `tests/test_adapters.py` 对三方库（numpy / faster_whisper）打桩，于是
        ASR 契约在**装没装可选依赖的机器上**都跑得出结论；缺依赖时的可读报错
        另有专门断言。所以判据只有朴素的一条：输出里必须有 `^OK`。

        行为侧不在这里测（跑一次 release.ps1 就能看到）。
        """
        path = PACKAGING / 'release.ps1'
        if not path.is_file():
            self.skipTest('release.ps1 不在（源码树可独立分发）')
        script = path.read_text(encoding='utf-8')
        self.assertNotIn('knownOnly', script,
                         'release.ps1 又给某个失败开了特例："允许一个失败"就是给所有失败留门')
        self.assertNotIn("No module named 'numpy'", script,
                         '闸门不该再认识 numpy 这个字样：可选依赖走打桩 + 可读报错断言')
        # 第 2 步（全量测试）与第 6 步（交付源码里再跑一遍）必须是同一条判据：
        # 一处写 `-notmatch`、一处写 `-match`，两处都盯着同一个 `^OK`。
        self.assertEqual(script.count("'(?m)^OK'"), 2,
                         '全量测试与交付源码测试都要"只认 OK"，两处判据必须一致')


def _ps_lines(script):
    """去掉 PowerShell 的注释与单引号字符串内容，返回剩下的行。

    判据只看**代码**，不看"文件里出现过这个词"：一句写给用户看的提示语里完全可以
    提到某个调用名（例如 `Die` 的文案里提到 `Invoke-WebRequest`），注释里也会
    引用被禁用的写法。字符串与注释都要去掉，否则断言比的是字样。

    （`''` 是 PS 在单引号字符串里转义单引号的方式；转两次 = 净效果不变，所以
    这个开关式扫描对它是正确的。）
    """
    lines = []
    for line in script.splitlines():
        kept = []
        in_string = False
        for char in line:
            if char == "'":
                in_string = not in_string
                continue
            if in_string:
                continue
            if char == '#':
                break
            kept.append(char)
        lines.append(''.join(kept))
    return lines


def _ps_function(script, name):
    """把 PowerShell 里某个函数的**完整定义**抠出来（`function NAME(...) {…}`，按花括号配对）。

    返回整段定义而不是只有花括号里那一段：行为测试会把结果当**真代码**跑，
    光有花括号里那段不是可调用的东西（会报 "The term 'X' is not recognized"）。

    按函数抠而不是在整份脚本里 `assertNotIn`：`& cmd.exe /c` 在启动交付 exe
    那一处是允许的（它要一个后台、脱离的子进程），但在 `Run-Native` 里不许有
    （那种写法没有任何超时把手）—— 全文搜索会把这两件事混成一件。
    """
    start = script.index('function ' + name)
    open_brace = script.index('{', start)
    depth = 0
    for index in range(open_brace, len(script)):
        char = script[index]
        if char == '{':
            depth += 1
        elif char == '}':
            depth -= 1
            if depth == 0:
                return script[start:index + 1]
    raise AssertionError('release.ps1 里 %s 的花括号不配对' % name)


class ReleaseScriptTests(unittest.TestCase):
    def setUp(self):
        self.path = PACKAGING / 'release.ps1'
        if not self.path.is_file():
            self.skipTest('release.ps1 不在（源码树可独立分发）')
        self.script = self.path.read_text(encoding='utf-8')

    def test_no_step_can_wait_forever_without_saying_so(self):
        """发布脚本里**每一次等待都必须有上限，超时必须是"失败"，不是"沉默"**。

        `& cmd.exe /c '"exe" "a" > out 2> err'` 这种写法**没有超时**：被启动的
        进程一旦卡住，整条流程就**永久停在那里、一个字都不输出**（日志停在
        "从交付路径启动"之后，看起来和"正在慢慢跑"一模一样）；
        `Invoke-WebRequest` 不写 `-TimeoutSec` 同理。

        判据全是静态可查的事实：
          1. `Run-Native` 有超时参数，且用 `WaitForExit(<毫秒>)` 收口；
          2. `Run-Native` 里**不许**再有 `cmd.exe /c`（那种写法没有超时把手）；
          3. 两条流都要异步读（只读一条 + 另一条写满缓冲区 = 互相死等，超时也救不了）；
          4. 脚本里每个 `Invoke-WebRequest` 都必须带 `-TimeoutSec`。
        """
        body = _ps_function(self.script, 'Run-Native')
        self.assertIn('$timeoutSec', body, 'Run-Native 必须能收一个超时上限')
        self.assertIn('WaitForExit($timeoutSec * 1000)', body,
                      'Run-Native 必须用 WaitForExit(毫秒) 给子进程设上限，否则它会一直等')
        self.assertNotIn('cmd.exe', body,
                         'Run-Native 不许再用 cmd.exe 重定向：那条路没有超时把手')
        self.assertIn('Quote-Arg', body,
                      '拼命令行参数必须走 Quote-Arg：朴素的 `"$arg"` 会在参数自带引号处'
                      '把参数截断（行为测试里有 q1/q2 两条盯着这件事）')
        for stream in ('StandardOutput.ReadToEndAsync', 'StandardError.ReadToEndAsync'):
            self.assertIn(stream, body,
                          'Run-Native 必须**异步**读 %s —— 只读一条会与另一条互相死等'
                          % stream)
        offenders = [line.strip() for line in _ps_lines(self.script)
                     if 'Invoke-WebRequest' in line and '-TimeoutSec' not in line]
        self.assertEqual(offenders, [],
                         '这些 Invoke-WebRequest 没设 -TimeoutSec（默认上限长到不实用）：%s'
                         % offenders)

    def test_the_cleanup_releases_the_exe_it_started(self):
        """收尾必须把脚本自己启动的那个 exe 停掉。

        交付目录里的 exe 被进程锁住时，`Copy-Item` 会**静默失败**，下一次发布就会
        把一个旧文件当新版交付出去。只在某一步的开头停一次进程不够：被强杀时留下的
        exe 会一齐留下 `%TEMP%` 里的隔离配置目录与它听着的端口。
        """
        self.assertIn('SaltLRCStudio', _ps_function(self.script, 'Cleanup-Temp'),
                      'Cleanup-Temp 应该顺手把 SaltLRCStudio 进程停掉（否则会锁住交付目录的 exe）')

    def test_the_release_script_never_shells_out_through_cmd(self):
        """整份发布脚本里**不许再出现 `cmd.exe`**。

        * `& cmd.exe /c 'start "" /B app.exe … > "文件" 2>&1'` —— **那一句不会返回**。
          exe 起来了、输出也进了文件，但 PowerShell 一直等到 app 退出为止，
          于是脚本永久停在那一行、**一个字都不输出**（日志停在 exe 打的那两行上，
          看起来和"正在慢慢跑"一模一样）。加 `> "文件" 2>&1` **并不能**救它 ——
          这一路上没有可用的"回得来"语义。
        * `Start-Process` 在本机直接抛异常：PS 5.1 会拼环境变量字典，而本机同时有
          `https_proxy` 与 `HTTPS_PROXY`（大小写不同的同一个变量）→
          `已添加项。字典中的关键字:"https_proxy"所添加的关键字:"HTTPS_PROXY"`。

        外部程序一律走 .NET 起进程 + 自己排空两条流，并且**等它真的退出**
        （而不是睡三秒再宣布"已退出"）。

        判据看**去掉注释与字符串之后**的代码：注释里会提到 `cmd.exe`。
        """
        live = '\n'.join(_ps_lines(self.script))
        self.assertNotIn('cmd.exe', live,
                         'release.ps1 里又用 cmd.exe 了 —— 那一条路没有"回得来"的语义，'
                         '用它启动长期运行的子进程会让脚本永久停住且不输出任何东西')
        self.assertIn('ProcessStartInfo', live, '外部程序要走 .NET 起进程')
        self.assertIn('RedirectStandardOutput = $true', live, '两条流必须新建管道')
        self.assertIn('$app.StandardOutput.ReadToEndAsync()', live,
                      'stdout 必须**立刻异步排空**（不排空，子进程写满管道缓冲区会自己卡住）')
        self.assertIn('$app.StandardError.ReadToEndAsync()', live, 'stderr 同理')
        self.assertIn('$app.WaitForExit(30000)', live,
                      '退出那一步必须**等它真的退了**，而不是睡几秒再宣布"已退出"')


PROBE_HEAD = """$ErrorActionPreference = 'Stop'
# 子进程的输出编码**由我们自己钉住**：不设的话它跟着控制台代码页走（本机 GBK），
# 而 Python 那边按 UTF-8 读 —— 中文断言就会变成"看似失败其实只是乱码"。
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
# `Run-Native` 失败时叫 `Die`。桩的作用是让"超时被当成**失败**"这件事可判定：
# 打印一个人一眼能认出的标记，并给一个专属退出码（3）。
function Die($text) { Write-Host ('DIEFAIL ' + $text); exit 3 }
"""

PROBE_NORMAL = """$py = '@PY@'
$r = Run-Native $py @('-c', 'import sys; sys.stdout.write("OUT"); sys.stderr.write("ERR")') 'probe' 60
Write-Host ('code=' + $r.Code)
Write-Host ('text=' + $r.Text)
$r2 = Run-Native $py @('-c', 'import os; print(os.getcwd())') 'cwd' 60
Write-Host ('cwd=' + $r2.Text.Trim())
$r3 = Run-Native $py @('-c', 'import sys; sys.exit(7)') 'exit' 60
Write-Host ('exit=' + $r3.Code)
$r4 = Run-Native $py @('-c', 'import sys; sys.stdout.write(sys.argv[1])', '@Q1@') 'q1' 60
Write-Host ('q1=' + $r4.Text)
$r5 = Run-Native $py @('-c', 'import sys; sys.stdout.write(sys.argv[1])', '@Q2@') 'q2' 60
Write-Host ('q2=' + $r5.Text)
"""

# 两个"参数里带特殊字符"的样本：含空格的引号、含空格的结尾反斜杠。
# 拼命令行时它们分别踩 Windows 解析规则的两条：引号要写成 `\"`、
# 结尾反斜杠要翻倍（否则它会把收尾的那个引号转义掉）。
QUOTE_ARG = 'a b"c'
TRAILING_BACKSLASH_ARG = 'd e\\'

PROBE_STUCK = """$py = '@PY@'
Run-Native $py @('-c', 'import time; time.sleep(40)') 'slow' 3
Write-Host 'NOT-REACHED'
"""


class RunNativeBehaviourTests(unittest.TestCase):
    """`Run-Native` 自己也要被测：静态断言只能证明"写着有超时"。

    上限是否真的生效，只能拿一个**真的会卡住的子进程**来验：静态上函数名在、
    调用也在，看不出"这条路根本没有上限"。

    从 release.ps1 里抠出真函数跑，而不是抄一份：抄的那份会漂
    （"两个地方各算一遍，迟早不一致"）。代价是测试要读一份 `.ps1` 的源码。
    """

    @classmethod
    def setUpClass(cls):
        if not (PACKAGING / 'release.ps1').is_file():
            raise unittest.SkipTest('release.ps1 不在（源码树可独立分发）')
        if not shutil.which('powershell'):
            raise unittest.SkipTest('本机没有 powershell —— release.ps1 本来就跑不起来')
        script = (PACKAGING / 'release.ps1').read_text(encoding='utf-8')
        # `Run-Native` 靠 `Quote-Arg` 拼命令行参数，少了它探针会报
        # "The term 'Quote-Arg' is not recognized" —— 两个函数都要拎过来。
        cls.body = (_ps_function(script, 'Quote-Arg') + '\n'
                    + _ps_function(script, 'Run-Native'))

    def _probe(self, tail):
        """把真函数 + 一段收尾脚本拼成一个 .ps1 跑起来，返回 (结果, 工作目录)。"""
        directory = Path(scratch('saltlrc-probe-'))
        work = directory / 'cwd-probe'
        work.mkdir()
        path = directory / 'probe.ps1'
        # 那个 `\n` 不是排版：抠出来的函数体以 `}` 收尾，直接接上 `$py = ...`
        # 就会变成 `}$py = ...` —— 一条"表达式里出现意外的标记"的语法错。
        source = (PROBE_HEAD + self.body + '\n'
                  + tail.replace('@PY@', sys.executable)
                        .replace('@Q1@', QUOTE_ARG)
                        .replace('@Q2@', TRAILING_BACKSLASH_ARG))
        # **必须带 BOM**：抠出来的函数体里有中文（那句超时提示），
        # PS 5.1 对没有 BOM 的脚本按 GBK 解码 → 引号被吃掉 → 语法错。
        # 这正是 packaging/*.ps1 那条静态断言盯的同一件事。
        path.write_text(source, encoding='utf-8-sig')
        done = subprocess.run(
            ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(path)],
            cwd=str(work), capture_output=True, text=True,
            encoding='utf-8', errors='replace', timeout=120)
        return done, work

    def test_it_returns_both_streams_the_exit_code_and_the_cwd(self):
        """五条：两条流都收得回来、退出码是真的、**工作目录跟着当前目录**、参数不被截断。

        第三条不是细节：第 6 步要在"交付的 `源码\\`"里跑测试，靠的就是"当前目录"。
        换成固定 `$root` 的话，那一整步会在**工作区**里跑 —— 于是它证明不了
        交付出去的那份拷贝是自洽的，而输出看起来完全一样（最坏的一种假绿）。

        第四、五条（`q1` / `q2`）盯参数拼接：参数里带 `"` 时，朴素的
        `'"' + $arg + '"'` 会在那个引号处**提前结束这个参数** —— 子进程拿到的是
        `snip.write(OUT)`，报 `NameError: name 'OUT' is not defined`。
        规则按 Windows 的 CommandLineToArgvW 来（引号写 `\\"`、引号前与结尾的
        反斜杠翻倍），这条测试就是那条规则的看门人。
        """
        done, work = self._probe(PROBE_NORMAL)
        self.assertIn('code=0', done.stdout,
                      '正常退出应当是 0：\n%s' % done.stdout + done.stderr)
        self.assertIn('text=OUTERR', done.stdout,
                      'stdout 与 stderr 都要收回来（只收一条就会与另一条死等）')
        self.assertIn('exit=7', done.stdout, '子进程的退出码必须原样返回')
        found = re.search(r'^cwd=(.*)$', done.stdout, re.M)
        self.assertIsNotNone(found, '探针没打出 cwd：\n%s' % done.stdout)
        self.assertEqual(os.path.normcase(os.path.abspath(found.group(1).strip())),
                         os.path.normcase(os.path.abspath(str(work))),
                         'Run-Native 的工作目录没跟着当前目录走')
        self.assertIn('q1=' + QUOTE_ARG, done.stdout,
                      '参数里的引号被截断了（应当原样传给孩子进程）：\n%s' % done.stdout)
        self.assertIn('q2=' + TRAILING_BACKSLASH_ARG, done.stdout,
                      '参数结尾的反斜杠被当成转义符了：\n%s' % done.stdout)

    def test_a_stuck_child_is_killed_and_reported_as_a_failure(self):
        """卡住的子进程必须**被杀掉并报成失败**，而不是一直等、什么都不说。

        超时的唯一症状是**没有任何输出**。
        判据有三条 —— 走了失败路径（退出码 3）、说了话、而且**没有真的等 40 秒**。
        """
        started = time.monotonic()
        done, _ = self._probe(PROBE_STUCK)
        elapsed = time.monotonic() - started
        self.assertNotIn('NOT-REACHED', done.stdout, '超时之后不该继续往下跑')
        self.assertEqual(done.returncode, 3,
                         '超时必须走 Die（报成失败）：\n%s' % done.stdout + done.stderr)
        self.assertIn('DIEFAIL', done.stdout, '超时要出声，不能沉默')
        self.assertIn('超过 3 秒', done.stdout,
                      '提示里要写明是超时、上限多少：\n%s' % done.stdout)
        self.assertLess(elapsed, 30,
                        '子进程要睡 40 秒，居然等了 %.1f 秒才回来' % elapsed)


if __name__ == '__main__':
    unittest.main()
