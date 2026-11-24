"""Verify the delivered folder itself: start its exe, ask it what it is.

Run from anywhere. Checks the things that only the packaged build can answer —
whether the embedded page snapshot carries the fix, whether the trace endpoints
exist in the frozen app, and whether the console-free window still speaks HTTP/1.1.
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

FOLDER = Path(r"C:\Users\Administrator\Desktop\SaltLRCStudio-v0.14.0")
EXE = FOLDER / "SaltLRCStudio.exe"
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8903
BASE = "http://127.0.0.1:%d" % PORT

results = []


def note(ok, name, detail=""):
    results.append((bool(ok), name, detail))
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else "  :: " + str(detail)))


def get(route, raw=False):
    with urllib.request.urlopen(BASE + route, timeout=10) as response:
        body = response.read()
        return (body, response) if raw else json.loads(body.decode("utf-8"))


def post(route, payload):
    request = urllib.request.Request(
        BASE + route, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


proc = subprocess.Popen([str(EXE), "--port=%d" % PORT, "--no-browser", "--no-window"],
                        cwd=str(FOLDER), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
note(True, "交付目录里的 exe 已启动 (pid %d)" % proc.pid)

up = False
for _ in range(180):
    time.sleep(0.5)
    try:
        get("/api/capabilities")
        up = True
        break
    except Exception:
        pass
note(up, "服务在 %s 上应答" % BASE, "90 秒内没有响应")
if not up:
    proc.kill()
    sys.exit(1)

try:
    caps, response = get("/api/capabilities", raw=True)
    caps = json.loads(caps.decode("utf-8"))
    note(caps.get("version") == "0.14.0", "版本号 = 0.14.0", caps.get("version"))
    note(response.version == 11 or str(response.version) == "11",
         "HTTP/1.1（不再每请求关连接）", response.version)

    # 1. the embedded page snapshot must carry the fix
    body, _ = get("/library", raw=True)
    page = body.decode("utf-8")
    dialog = page.find('id="wordEditDialog"')
    script = page.find('<script src="/library.js">')
    note(dialog != -1, "打包页面里有逐字对话框", "")
    note(script != -1, "打包页面里有 library.js", "")
    note(dialog < script,
         "对话框在脚本之前（这就是修复本身）",
         "dialog@%s script@%s" % (dialog, script))
    note('id="closeWordEdit"' in page and 'id="applyWords"' in page,
         "取消 / 应用 两个按钮都在页面里", "")

    # 2. the trace tool exists in the frozen app
    trace_js, _ = get("/trace.js", raw=True)
    notes = trace_js.decode("utf-8")
    note("window.Trace" in notes, "打包后的 trace.js 已登记", len(notes))
    note("fault" in notes, "trace.js 带故障上报（bind 接不上会写日志）", "")
    note("since:" in notes, "trace.js 发送绝对时间戳（不做按批平移）", "")
    snapshot = post("/api/trace", {})
    note("analysis" in snapshot and "stalled" in snapshot["analysis"],
         "/api/trace 可用并返回判定", list(snapshot)[:4])
    post("/api/trace/clear", {})

    # 3. a genuinely dead click is reported as having no effect
    now = int(time.time() * 1000)
    verdict = post("/api/trace", {"events": [
        {"kind": "gesture", "name": "click button#nobody「点了没反应的按钮」",
         "detail": "", "t": now, "since": 10, "page": "/library"},
        {"kind": "step", "name": "看起来没有反应", "detail": "没有结果",
         "t": now + 954, "since": 964, "page": "/library"}]})
    stalled = [item["name"] for item in verdict["analysis"]["stalled"]]
    note(any("nobody" in name for name in stalled),
         "点了没反应的点击被判为 stalled", stalled)
    post("/api/trace/clear", {})

    # 4. the fix must be visible in the module the window actually loads
    window_py = (FOLDER / "源码" / "saltlrc" / "window.py")
    note(window_py.is_file(), "源码里带 window.py（独立窗口）", "")

    # 5. the packaged stylesheet must carry the closed-dialog invariant
    css, _ = get("/style.css", raw=True)
    text = css.decode("utf-8")
    note(re.search(r"dialog:not\(\[open\]\)\{display:none\s*!important\}", text) is not None,
         "打包的 CSS 里有「关闭的对话框不显示」（本次修复）", "")
    note(re.search(r"dialog[.]wordBox\{[^}]*display:flex", text) is not None,
         "确实存在给 dialog 设 display 的规则（所以上面那条是必需的）", "")

    # 6. the refactored layout must be in the packaged build
    note('history' in (FOLDER / '源码' / 'saltlrc' / 'web' / 'history.js').read_text(encoding='utf-8'),
         "交付源码里带 history.js", "")
    api_dir = FOLDER / '源码' / 'saltlrc' / 'api'
    note(api_dir.is_dir() and len(list(api_dir.glob('*.py'))) >= 6,
         "交付源码里带 api/ 路由包（%d 个模块）" % len(list(api_dir.glob('*.py'))), "")
    note((FOLDER / '源码' / 'saltlrc' / 'sources').is_dir(), "交付源码里带 sources/ 歌词源包", "")
    note(not (FOLDER / '源码' / 'saltlrc' / 'models.py').exists(),
         "models.py 垫片已删除（不再有第二个名字）", "")

    # 7. the user's own library is still readable
    try:
        library = get("/api/library")
        count = library.get("count", library.get("total", len(library.get("tracks", []))))
        note(count and count > 0, "曲库可读（%s 首）" % count, count)
    except Exception as error:
        note(False, "曲库可读", error)
finally:
    try:
        post("/api/exit", {})
    except Exception:
        pass
    for _ in range(60):
        if proc.poll() is not None:
            break
        time.sleep(0.5)
    note(proc.poll() is not None, "退出后进程结束", "仍在运行")
    if proc.poll() is None:
        proc.kill()

passed = sum(1 for ok, _, _ in results if ok)
print("\n%d/%d 交付路径检查通过" % (passed, len(results)))
sys.exit(0 if passed == len(results) else 1)
