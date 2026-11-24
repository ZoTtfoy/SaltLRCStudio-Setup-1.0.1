"""本地工程库 —— layer 2（文件系统）。

**落盘的那一份**：编辑器的自动保存存在浏览器 storage 里，而「退出即清空」是刻意的设计
（见 `PLAN` 里的会话策略），一次会话里的歌词退出就没了。工程库每次导入音频/歌词
就建一个工程，写在用户指定的目录里，退出前由用户决定留不留。

**保存路径**存在 `%LOCALAPPDATA%\\SaltLRCStudio\\project-root.json`，
与其它 store 共用 :mod:`saltlrc.store` 的原子写 —— 半截文件会静默丢掉内容，
这类"设置莫名其妙不生效"的 bug 只允许有一个实现。

**"本会话创建了哪些工程"** 记在 `session-projects.json`：
退出时要删除的是**这次会话建的那些**，绝不是用户目录里的全部内容。
每次启动由 :func:`start_session` 清一次记录（只清记录，不删文件）。

安全边界（删除是不可逆的，所以写死在这里而不是交给调用方）：

* 一个工程的目录必须**在保存路径之内**才允许删（`_inside`）；
* **保存路径本身永不删除**，哪怕它被当成某个工程的目录；
* 目录名由本模块生成（时间戳 + 净化后的名字），不接受调用方传路径。
"""

import datetime
import json
import os
import re
import threading
import time

from . import store

# 保存路径（用户选的那个目录）
ROOT_FILE = "project-root.json"
# 本次会话建过的工程；退出时按它决定保留还是删除
SESSION_FILE = "session-projects.json"

# 目录名里不能出现的字符（Windows 保留字符 + 路径分隔符）
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class ProjectError(ValueError):
    """保存路径不可用、或工程不存在。调用方负责转成提示。

    故意继承 :class:`ValueError`：``server.do_POST`` 只把
    ``ValueError/TypeError/...`` 转成 400 + 原文，其它异常一律是
    ``500 服务器内部错误``。这里的每条消息（"请选择保存路径"、
    "这个文件夹不能写入…"）都是给用户看的 —— 挂到 ``RuntimeError`` 上
    会让它们变成一句用户看不懂的 500。改基类前先读 server.py 的 except 列表。
    """


def suggest():
    """给首次使用的用户一个像样的默认值：``文档\\SaltLRCStudio 工程``。"""
    home = os.path.expanduser("~")
    documents = os.path.join(home, "Documents")
    base = documents if os.path.isdir(documents) else home
    return os.path.join(base, "SaltLRCStudio 工程")


# ---------------------------------------------------------------- 保存路径

def _root_record():
    data = store.read_json(store.app_path(ROOT_FILE), None)
    return data if isinstance(data, dict) else {}


def config():
    root = _root_record().get("root") or None
    return {"root": root, "configured": bool(root)}


def root():
    """保存路径；未配置返回 None。"""
    value = _root_record().get("root")
    return value or None


def set_root(path):
    """记住保存路径，并确保它存在且可写。

    先真的建一个临时文件再删掉 —— 只检查"目录存在"是不够的：
    只读目录、被占用的网络盘、需要管理员权限的系统目录都能通过存在性检查，
    然后在第一次保存时失败。
    """
    raw = (path or "").strip().strip('"')
    if not raw:
        raise ProjectError("请选择保存路径")
    target = os.path.abspath(os.path.expanduser(raw))
    try:
        os.makedirs(target, exist_ok=True)
        probe = os.path.join(target, ".saltlrc-write-test")
        with open(probe, "w", encoding="utf-8") as handle:
            handle.write("ok")
        os.remove(probe)
    except OSError as error:
        raise ProjectError("这个文件夹不能写入：%s（%s）" % (target, error)) from error
    store.atomic_write_json(store.app_path(ROOT_FILE), {"root": target},
                            prefix=".project-root-")
    return {"root": target, "configured": True}


# ---------------------------------------------------------------- 工程目录

def _safe(name):
    """把文件名净化成能当目录名的字符串。"""
    stem = os.path.splitext(os.path.basename(str(name or "")))[0]
    cleaned = _UNSAFE.sub("_", stem).strip(" .")
    return cleaned[:60] or "工程"


def _inside(path, parent):
    """``path`` 是否在 ``parent`` 之内（且不等于它）。"""
    try:
        real = os.path.realpath(path)
        base = os.path.realpath(parent)
    except OSError:
        return False
    return real != base and real.startswith(base + os.sep)


def _session():
    data = store.read_json(store.app_path(SESSION_FILE), None)
    entries = data.get("projects") if isinstance(data, dict) else None
    return entries if isinstance(entries, list) else []


def _write_session(entries):
    store.atomic_write_json(store.app_path(SESSION_FILE), {"projects": entries},
                            prefix=".session-")


# 「本次会话建过哪些工程」是**一个**小 JSON，读-改-写必须是原子的。
#
# **"导入文件夹"会并发建工程**：多个请求同时进来，每个都"读列表 → 追加自己 → 写回去"。
# 没有锁时会出现两种坏事：
#   ① **丢记录**：两个线程读到同一份旧列表，后写的那个把前一个的条目覆盖掉
#      → 磁盘上有工程、会话里没有它 → 退出时删不掉、"本次建了几个"也报错；
#   ② **WinError 5**：两个线程同时往同一个文件做原子替换，Windows 报"拒绝访问"
#      → 抛出"建工程失败：[WinError 5] 拒绝访问。: '.session-xxx.tm'"，
#      而工程目录已经建出来了（半成品：有目录、没内容）。
# 锁只保护**这个小 JSON**，不保护工程文件本身（那可能是几十 MB，没必要排队）。
_SESSION_LOCK = threading.RLock()


def _update_session(mutate):
    """对会话记录做一次原子的读-改-写；``mutate(entries)`` 就地进行修改。

    唯一的入口 —— 分开写"读一下再写一下"就又回到上面那两种坏事了。
    """
    with _SESSION_LOCK:
        entries = _session()
        result = mutate(entries)
        _write_session(entries)
        return result


def session_projects():
    """本次会话建过的工程（最新在前）。"""
    return list(reversed(_session()))


def create(name):
    """为一次导入建一个工程目录，返回它的记录。

    目录名 = ``<时间戳>-<净化后的名字>``，**由本模块生成**；
    同一秒内重复导入也不会撞名（末尾补序号）。
    """
    base = root()
    if not base:
        raise ProjectError("还没有设置保存路径：先在提示里选一个文件夹")
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    stem = "%s-%s" % (stamp, _safe(name))
    folder = os.path.join(base, stem)
    index = 2
    while os.path.exists(folder):
        folder = os.path.join(base, "%s(%d)" % (stem, index))
        index += 1
    try:
        os.makedirs(folder, exist_ok=False)
    except OSError as error:
        raise ProjectError("建不了工程目录：%s（%s）" % (folder, error)) from error
    record = {"id": os.path.basename(folder), "dir": folder, "name": str(name or ""),
              "created": time.time()}
    _update_session(lambda entries: entries.append(record))
    return record


def find(project_id):
    for entry in _session():
        if entry.get("id") == project_id:
            return entry
    return None


def write(project_id, text):
    """把工程内容写进它的目录（同一个工程反复覆盖，不产生垃圾文件）。"""
    entry = find(project_id)
    if not entry:
        raise ProjectError("这个工程不在本次会话里：%s" % project_id)
    base = root()
    if not base or not _inside(entry["dir"], base):
        raise ProjectError("工程目录不在当前保存路径之内，已拒绝写入")
    target = os.path.join(entry["dir"], "project.saltproj")
    store.atomic_write_text(target, text, prefix=".project-")
    return {"path": target, "bytes": len(text.encode("utf-8"))}


def read(project_id):
    entry = find(project_id)
    if not entry:
        raise ProjectError("这个工程不在本次会话里：%s" % project_id)
    target = os.path.join(entry["dir"], "project.saltproj")
    try:
        with open(target, "r", encoding="utf-8") as handle:
            return {"id": project_id, "text": handle.read(), "path": target}
    except OSError as error:
        raise ProjectError("读不了工程文件：%s（%s）" % (target, error)) from error


def _rmtree(path):
    """删掉一个目录。**已经不在磁盘上就算成功** —— 目标状态已经达成。

    `delete()` 在 `_rmtree` 之后才把工程从会话记录里摘掉。目录在 `_rmtree` 之前
    就被别的东西删掉时，末尾的 `os.rmdir` 会抛 `FileNotFoundError`，`delete()` 把它翻成
    `ProjectError("删不掉工程目录：…")` —— 一句假的失败提示。
    删除这一头"多做一次无用功"不该被报成错误（对照：越界删除必须报错，那条边界不变）。
    """
    if not os.path.isdir(path):
        return
    for current, folders, files in os.walk(path, topdown=False):
        for name in files:
            try:
                os.remove(os.path.join(current, name))
            except OSError:
                pass
        for name in folders:
            try:
                os.rmdir(os.path.join(current, name))
            except OSError:
                pass
    os.rmdir(path)


def delete(project_id):
    """删掉一个工程目录。只允许删保存路径**之内**的目录。"""
    entry = find(project_id)
    if not entry:
        return False
    base = root()
    folder = entry.get("dir") or ""
    if not base or not _inside(folder, base):
        # 路径被改过、或保存路径换了 —— 宁可不删，也不越界。
        raise ProjectError("工程目录不在当前保存路径之内，已拒绝删除：%s" % folder)
    try:
        _rmtree(folder)
    except OSError as error:
        raise ProjectError("删不掉工程目录：%s（%s）" % (folder, error)) from error
    _update_session(lambda entries: entries.__setitem__(
        slice(None), [item for item in entries if item.get("id") != project_id]))
    return True


def discard_session():
    """删掉本次会话建的全部工程，返回删掉的个数。"""
    removed, failed = 0, []
    for entry in list(_session()):
        try:
            if delete(entry.get("id")):
                removed += 1
        except ProjectError as error:
            failed.append(str(error))
    return {"removed": removed, "failed": failed}


def _clear_session():
    """清空会话记录并返回"清掉了几条"。

    单独写成一个小函数而不是 `lambda entries: len(entries) or entries.clear() or 0`：
    那种写法非空时会被 `or` 短路，**clear() 根本不会执行**。
    """
    def clear(entries):
        count = len(entries)
        entries.clear()
        return count
    return _update_session(clear)


def keep_session():
    """保留本次会话的工程：只清掉"会话记录"，磁盘文件原样留着。"""
    count = _clear_session()
    return {"kept": count}


def start_session():
    """开一次新会话：只清会话记录，**一个文件都不删**。

    每次启动都必须先做这件事，否则上一次的残留记录会被当成"这次建的"，
    退出时一次"删除"就能删掉用户根本不记得的东西 —— 删除不可逆，
    所以这条出错方向必须是"多留文件"，永远不是"多删文件"。
    如果上一次会话被强杀（没走退出流程），它的工程就只留在磁盘上，
    不再被追踪 —— 这是刻意的：没有人明确说过要删。
    """
    count = _clear_session()
    return {"previous": count}
