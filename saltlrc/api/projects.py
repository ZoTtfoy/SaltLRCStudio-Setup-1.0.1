"""本地工程库的路由 —— layer 3。

页面只管"建 / 写 / 读 / 保留 / 删除"这五个动作，**路径一律由 L2
(:mod:`saltlrc.projects`) 生成和校验**：页面传进来的只有工程 id，永远不是路径。
这一条是刻意的 —— 删除是不可逆的，让一个字符串决定删哪个目录，
迟早会有人把 ``..\\..`` 或一个绝对路径塞进来。
"""

from .. import folders, projects


def handle(self, path, data):
    if self.path == "/api/projects/config":
        # 启动时读一次：有没有保存路径、这次会话建了多少个工程。
        state = projects.config()
        self.send(200, {"root": state["root"], "configured": state["configured"],
                        "suggested": projects.suggest(),
                        "session": projects.session_projects()})
        return True
    if self.path == "/api/projects/root":
        if data.get("pick"):
            # 系统文件夹选择框由用户点「浏览…」触发 —— 不在这里自己弹窗。
            chosen = folders.pick_folder("SaltLRC Studio：选择工程的保存位置")
            if not chosen:
                self.send(200, {"status": "cancelled"})
                return True
            data = dict(data, path=chosen)
        result = projects.set_root(data.get("path"))
        self.send(200, result)
        return True
    if self.path == "/api/projects/create":
        self.send(200, projects.create(data.get("name")))
        return True
    if self.path == "/api/projects/write":
        self.send(200, projects.write(str(data.get("id") or ""), str(data.get("text") or "")))
        return True
    if self.path == "/api/projects/read":
        self.send(200, projects.read(str(data.get("id") or "")))
        return True
    if self.path == "/api/projects/discard":
        self.send(200, projects.discard_session())
        return True
    if self.path == "/api/projects/keep":
        self.send(200, projects.keep_session())
        return True

    return False
