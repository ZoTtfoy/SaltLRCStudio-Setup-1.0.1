"""本地工程库（L2）：每导入一个文件建一个工程，退出时保留或删除。

这一层的每一条都关系到**不可逆的删除**，所以测试盯的是边界而不是顺利路径：

* 保存路径"存在"不等于"能写"（只检查存在性会在第一次保存时才发现问题）；
* 工程的目录名由模块生成，调用方给的字符串进不了路径；
* 删除**只允许**发生在保存路径之内，而且保存路径本身永不删除；
* 换过保存路径之后，旧工程拒绝删除 —— 宁可不删，也不越界；
* 「删掉本次会话的」不能变成"删掉这个文件夹里的全部内容"。

LOCALAPPDATA / SALTLRC_CONFIG_DIR 都指向临时目录：跑测试绝不能碰用户的
真实配置（否则一次测试就把用户设好的保存位置改掉）。
"""
import json
import os
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from _scratch import scratch
from saltlrc import projects, store
from saltlrc.server import Handler


def setUpModule():
    """把所有 store 指到临时目录 —— 在这层做，而不是某个 TestCase 里。

    模块级是因为用例类是按字母序跑的：只给 ProjectsTests 设的话，
    AtomicTextTests 会先跑到真实的 %LOCALAPPDATA%。
    """
    os.environ["SALTLRC_CONFIG_DIR"] = scratch("saltlrc-cfg-")


def tearDownModule():
    # 目录由 tests/_scratch.py 在进程退出时统一清掉（"临时目录只有一个出口"）。
    # 这里只把自己改过的环境变量还原。
    os.environ.pop("SALTLRC_CONFIG_DIR", None)


class ProjectsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scratch = os.environ["SALTLRC_CONFIG_DIR"]

        class Quiet(Handler):
            def log_message(self, *args):
                pass

            def handle_one_request(self):
                try:
                    Handler.handle_one_request(self)
                except Exception:
                    self.close_connection = True

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Quiet)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = "http://127.0.0.1:%d" % cls.server.server_port

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        # 每个用例一份干净的配置目录 + 干净的保存路径。
        # 配置目录**复用同一个**（在 setUpModule 里建好的那个），每个用例前把它清空；
        # 保存路径每个用例新建一份。
        _rmtree(self.scratch)
        os.makedirs(self.scratch, exist_ok=True)
        self.root = Path(scratch("saltlrc-projects-"))

    def post(self, route, payload):
        request = urllib.request.Request(
            self.url + route, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", "replace")
            raise AssertionError("%s -> %s: %s" % (route, error.code, body))

    def post_error(self, route, payload):
        """返回 (状态码, 错误原文)；用来断言"用户能看懂"，不是一句 500。"""
        request = urllib.request.Request(
            self.url + route, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read().decode("utf-8", "replace"))

    # ------------------------------------------------------------ 保存路径
    def test_without_a_root_nothing_is_created(self):
        self.assertFalse(projects.config()["configured"])
        self.assertIsNone(projects.root())
        with self.assertRaises(projects.ProjectError) as caught:
            projects.create("某首歌")
        self.assertIn("保存路径", str(caught.exception))

    def test_an_existing_but_unwritable_root_is_refused(self):
        """只检查"目录存在"是不够的：得真的写一个文件才算数。"""
        blocker = self.root / "不是文件夹"
        blocker.write_text("x", encoding="utf-8")
        # 把一个**文件**当保存路径：makedirs 会失败，于是 set_root 必须报错。
        file_path = blocker / "sub"
        with self.assertRaises(projects.ProjectError):
            projects.set_root(str(file_path))
        # 什么都没记下来：失败的配置不能留下半个"已配置"的状态。
        self.assertFalse(projects.config()["configured"])
        # 探测文件必须被清掉，不然用户目录里会多出一个陌生文件。
        self.assertEqual([p.name for p in self.root.iterdir()], ["不是文件夹"])

    def test_a_good_root_is_remembered_and_probed_cleanly(self):
        target = self.root / "工程"
        result = projects.set_root(str(target))
        self.assertTrue(result["configured"])
        self.assertTrue(target.is_dir())
        self.assertEqual(list(target.iterdir()), [], "探测文件必须删掉")
        self.assertEqual(projects.root(), str(target))

    def test_the_suggested_default_is_an_absolute_path(self):
        suggestion = projects.suggest()
        self.assertTrue(os.path.isabs(suggestion), suggestion)
        self.assertIn("SaltLRCStudio", suggestion)

    # ------------------------------------------------------------ 建工程
    def test_a_project_is_named_and_placed_by_the_module(self):
        projects.set_root(str(self.root))
        record = projects.create("../../邪恶/歌名.lrc")
        folder = Path(record["dir"])
        self.assertEqual(folder.parent, self.root, "工程目录必须直接在保存路径下")
        self.assertNotIn("..", folder.name)
        self.assertNotIn("/", folder.name)
        self.assertNotIn("\\", folder.name)
        self.assertIn("歌名", folder.name, "名字主干要留下来，便于用户自己找")
        self.assertTrue(folder.is_dir())

    def test_building_many_projects_at_once_keeps_every_record(self):
        """并发建工程：一个都不能失败，会话记录一条都不能丢。

        "导入文件夹"时前端是**并发**发请求的，而 `create()` 是
        "读会话记录 → 追加自己 → 写回去"。没有锁的时候两件坏事同时发生：

          * **丢记录**：两个线程读到同一份旧列表，后写的把前一个的条目盖掉
            → 磁盘上有工程、会话里没有它 → 退出时删不掉、"本次建了几个"也报错；
          * **WinError 5**：两个线程同时对同一个文件做原子替换，Windows 报"拒绝访问"
            → 建工程报错，而此时工程目录**已经建出来了**（半成品：有目录、没内容）。

        单线程的测试发现不了这两件事 —— 所以这里必须真开线程。
        """
        projects.set_root(str(self.root))
        made, failures = [], []
        lock = threading.Lock()

        def work(index):
            try:
                record = projects.create('并发%d' % index)
                with lock:
                    made.append(record["id"])
            except Exception as error:                       # noqa: BLE001 - 全都要记下来
                with lock:
                    failures.append(repr(error))

        threads = [threading.Thread(target=work, args=(index,)) for index in range(24)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(failures, [], "并发建工程有失败：%s" % failures[:2])
        self.assertEqual(len(made), 24, "只建成了 %d 个" % len(made))
        self.assertEqual(len(projects.session_projects()), 24,
                         "会话记录丢了条目 —— 那些工程退出时既删不掉也数不到")
        self.assertEqual(len(set(os.listdir(self.root))), 24, "目录数不对")

    def test_the_same_name_twice_does_not_collide(self):
        projects.set_root(str(self.root))
        first = projects.create("同一首")
        second = projects.create("同一首")
        self.assertNotEqual(first["dir"], second["dir"])
        self.assertTrue(Path(first["dir"]).is_dir() and Path(second["dir"]).is_dir())

    def test_write_then_read_is_character_for_character(self):
        projects.set_root(str(self.root))
        record = projects.create("往返")
        text = '{"name":"晴天","lyrics":"C:\\\\音乐\\\\a.lrc","line":"换行\\n与制表\\t和 emoji 🎵"}'
        written = projects.write(record["id"], text)
        self.assertEqual(written["bytes"], len(text.encode("utf-8")))
        got = projects.read(record["id"])
        self.assertEqual(got["text"], text, "写下去什么，读回来必须一模一样")
        # 目录里只有工程文件本身：临时文件不能留下。
        self.assertEqual([p.name for p in Path(record["dir"]).iterdir()], ["project.saltproj"])

    def test_a_chinese_folder_path_works_all_the_way(self):
        """中文/日文路径必须一路走通，且要走**真的 HTTP 层**。

        编码类缺陷（JSON、UTF-8、Windows 路径）只在这条路上才暴露；
        对话框那一层的指针截断见 tests/test_folders.py。
        """
        target = self.root / "中文 目录" / "歌词工程"
        reply = self.post("/api/projects/root", {"path": str(target)})
        self.assertTrue(reply["configured"])
        self.assertEqual(Path(reply["root"]), target)
        self.assertTrue(target.is_dir())

        song = "晴天 日本語 歌名.lrc"
        record = self.post("/api/projects/create", {"name": song})
        self.assertIn("晴天", record["dir"], "目录名里的中文要留下来")
        self.assertEqual(Path(record["dir"]).parent, target)
        self.post("/api/projects/write", {"id": record["id"], "text": "内容：中文与日本語 🎵"})
        self.assertEqual(self.post("/api/projects/read", {"id": record["id"]})["text"],
                         "内容：中文与日本語 🎵")
        # 会话记录（session-projects.json）也要能原样读回来 —— 它是 ensure_ascii=False
        # 写出去的 UTF-8，编码错的话这里会变成 \uXXXX 或者乱码。
        entry = self.post("/api/projects/config", {})["session"][0]
        self.assertEqual(entry["name"], song)
        self.assertEqual(entry["dir"], record["dir"])
        # 中文目录也必须删得掉（`_inside` 比的是 realpath，别在编码上出岔子）。
        self.assertEqual(self.post("/api/projects/discard", {})["removed"], 1)
        self.assertFalse(Path(record["dir"]).exists())
        self.assertTrue(target.is_dir(), "保存路径本身不能删")

    def test_writing_an_unknown_project_is_refused(self):
        projects.set_root(str(self.root))
        with self.assertRaises(projects.ProjectError):
            projects.write("不存在的工程", "{}")

    def test_saving_twice_overwrites_instead_of_piling_up(self):
        projects.set_root(str(self.root))
        record = projects.create("反复保存")
        projects.write(record["id"], "第一次")
        projects.write(record["id"], "第二次")
        self.assertEqual(projects.read(record["id"])["text"], "第二次")
        self.assertEqual([p.name for p in Path(record["dir"]).iterdir()], ["project.saltproj"])

    def test_a_failed_project_save_keeps_the_previous_copy(self):
        """工程写盘必须走 store 的原子写，而且这里要**行为**上验到。

        只测 store.atomic_write_text 是不够的：把 projects.write 换成
        `open(target,'w').write(text)` 时，那条测试照样全绿。所以这里让最后一步
        失败，看旧的那份在不在。
        """
        projects.set_root(str(self.root))
        record = projects.create("别丢档")
        projects.write(record["id"], "第一版")
        with mock.patch("saltlrc.store.os.replace", side_effect=OSError("模拟磁盘故障")):
            with self.assertRaises(OSError):
                projects.write(record["id"], "第二版")
        self.assertEqual(projects.read(record["id"])["text"], "第一版", "失败的保存不能毁掉上一版")
        self.assertEqual([p.name for p in Path(record["dir"]).iterdir()], ["project.saltproj"])

    # ------------------------------------------------------------ 删除的边界
    def test_discard_deletes_only_this_sessions_projects(self):
        projects.set_root(str(self.root))
        mine = projects.create("本次")
        projects.write(mine["id"], "{}")
        # 用户自己放在同一个文件夹里的东西，一根汗毛都不能动。
        keep_folder = self.root / "别动我"
        keep_folder.mkdir()
        (keep_folder / "重要.txt").write_text("不可替代", encoding="utf-8")
        loose = self.root / "散落的文件.lrc"
        loose.write_text("[00:00.00]x", encoding="utf-8")

        result = projects.discard_session()
        self.assertEqual(result["removed"], 1)
        self.assertEqual(result["failed"], [])
        self.assertFalse(Path(mine["dir"]).exists())
        self.assertTrue(keep_folder.is_dir(), "保存路径里的其它文件夹不能被删")
        self.assertTrue(loose.is_file(), "保存路径里的散装文件不能被删")

    def test_the_root_itself_is_never_deleted(self):
        """记录被篡改成"工程就是保存路径"时，宁可不删也不越界。"""
        projects.set_root(str(self.root))
        projects.create("甲")
        projects._write_session([{"id": "伪造", "dir": str(self.root), "name": "伪造"}])
        with self.assertRaises(projects.ProjectError):
            projects.delete("伪造")
        self.assertTrue(self.root.is_dir(), "保存路径本身永远不能被删掉")

    def test_deleting_a_project_that_is_already_gone_is_not_an_error(self):
        """目录已经不在了，再删一次要算**成功** —— 否则退出时会弹一句假的"删不掉"。

        `_rmtree` 末尾的 `os.rmdir` 要容错：目录先在资源管理器里被拿掉之后，
        "删除本次会话的工程"不该报 `ProjectError("删不掉工程目录：…")`。
        删除方向上的"多做一次无用功"不是错误；**越界**才是（上一条测试管那个）。
        """
        projects.set_root(str(self.root))
        created = projects.create("已经不在的工程")
        folder = Path(created["dir"])
        self.assertTrue(folder.is_dir())
        _rmtree(str(folder))                       # 模拟用户在资源管理器里先手工删掉
        self.assertFalse(folder.exists())

        self.assertTrue(projects.delete(created["id"]), "已经不在了应当算删成功")
        self.assertNotIn(created["id"], [item.get("id") for item in projects._session()],
                         "删成功之后要从会话记录里摘掉，否则退出时还会再删一次")

    def test_after_changing_the_root_the_old_projects_are_not_deleted(self):
        """换过保存路径之后，旧工程不在新路径之内 —— 必须拒绝删除。"""
        first = self.root / "旧位置"
        projects.set_root(str(first))
        old = projects.create("旧工程")
        self.assertTrue(Path(old["dir"]).is_dir())
        second = self.root / "新位置"
        projects.set_root(str(second))

        result = projects.discard_session()
        self.assertEqual(result["removed"], 0)
        self.assertEqual(len(result["failed"]), 1)
        self.assertIn("保存路径", result["failed"][0])
        self.assertTrue(Path(old["dir"]).is_dir(), "越界时宁可不删")

    def test_keep_clears_the_record_but_leaves_the_files(self):
        projects.set_root(str(self.root))
        record = projects.create("保留")
        projects.write(record["id"], "{}")
        self.assertEqual(projects.keep_session()["kept"], 1)
        self.assertEqual(projects.session_projects(), [])
        self.assertTrue(Path(record["dir"]).is_dir(), "保留 = 文件原样留着")
        self.assertTrue((Path(record["dir"]) / "project.saltproj").is_file())

    def test_a_new_session_never_deletes_anything(self):
        """强杀留下的记录：新会话只清记录，文件不动。

        出错方向必须是多留文件，不是多删文件 —— 一次"删除"不能删掉用户根本
        不记得的东西。
        """
        projects.set_root(str(self.root))
        leftover = projects.create("上次没走完退出流程")
        projects.write(leftover["id"], "{}")
        self.assertEqual(projects.start_session()["previous"], 1)
        self.assertEqual(projects.session_projects(), [])
        self.assertTrue(Path(leftover["dir"]).is_dir())
        self.assertTrue((Path(leftover["dir"]) / "project.saltproj").is_file())

    # ------------------------------------------------------------ 路由
    def test_the_routes_round_trip(self):
        reply = self.post("/api/projects/root", {"path": str(self.root / "路由")})
        self.assertTrue(reply["configured"])
        config = self.post("/api/projects/config", {})
        self.assertTrue(config["configured"])
        self.assertIsInstance(config["suggested"], str)

        created = self.post("/api/projects/create", {"name": "路由歌"})
        written = self.post("/api/projects/write", {"id": created["id"], "text": "内容"})
        self.assertEqual(Path(written["path"]).read_text(encoding="utf-8"), "内容")
        self.assertEqual(self.post("/api/projects/read", {"id": created["id"]})["text"], "内容")
        self.assertEqual(len(self.post("/api/projects/config", {})["session"]), 1)
        self.assertEqual(self.post("/api/projects/keep", {})["kept"], 1)
        self.assertTrue(Path(created["dir"]).is_dir(), "保留不清文件")

    def test_a_bad_root_is_a_readable_400_not_a_500(self):
        """ProjectError 必须能变成 400 + 原文。

        它的每句话（"请选择保存路径"）都是提示；基类一旦改成 RuntimeError，
        这些提示就全变成"500 服务器内部错误"。
        """
        code, body = self.post_error("/api/projects/root", {"path": ""})
        self.assertEqual(code, 400, body)
        self.assertIn("保存路径", body["error"])
        code, body = self.post_error("/api/projects/write", {"id": "没有这个", "text": "x"})
        self.assertEqual(code, 400, body)
        self.assertIn("本次会话", body["error"])

    def test_picking_a_folder_uses_the_native_dialog_and_a_cancel_changes_nothing(self):
        from saltlrc import folders
        real = folders.pick_folder
        folders.pick_folder = lambda title=None: None
        self.addCleanup(lambda: setattr(folders, "pick_folder", real))
        reply = self.post("/api/projects/root", {"pick": True})
        self.assertEqual(reply, {"status": "cancelled"})
        self.assertFalse(projects.config()["configured"], "取消不能让路径被清掉")

        folders.pick_folder = lambda title=None: str(self.root / "选中的")
        reply = self.post("/api/projects/root", {"pick": True})
        self.assertTrue(reply["configured"])


class AtomicTextTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(scratch("saltlrc-atomic-"))

    def test_text_is_written_verbatim_and_leaves_no_temp_files(self):
        target = self.folder / "sub" / "project.saltproj"
        text = '{"a":"\\\\反斜杠","b":"换行\\n","c":"🎵"}'
        store.atomic_write_text(str(target), text)
        self.assertEqual(target.read_text(encoding="utf-8"), text)
        self.assertEqual([p.name for p in target.parent.iterdir()], ["project.saltproj"])

    def test_replacing_keeps_a_single_file(self):
        target = self.folder / "x.txt"
        store.atomic_write_text(str(target), "一")
        store.atomic_write_text(str(target), "二")
        self.assertEqual(target.read_text(encoding="utf-8"), "二")
        self.assertEqual([p.name for p in self.folder.iterdir()], ["x.txt"])

    def test_a_failed_write_leaves_the_old_content_and_no_debris(self):
        """原子写的**实际保证**：要么是全新内容，要么是完整旧内容。

        只断言"没有临时文件"是不够的 —— 先截断再写（非原子）也能通过那条。
        这里让最后一步的 rename 失败，旧内容必须还在，临时文件必须被清掉。
        """
        target = self.folder / "x.txt"
        store.atomic_write_text(str(target), "旧内容")
        with mock.patch("saltlrc.store.os.replace", side_effect=OSError("模拟磁盘故障")):
            with self.assertRaises(OSError):
                store.atomic_write_text(str(target), "新内容")
        self.assertEqual(target.read_text(encoding="utf-8"), "旧内容", "失败不能毁掉旧内容")
        self.assertEqual([p.name for p in self.folder.iterdir()], ["x.txt"], "失败不能留下残渣")

    def test_the_scratch_config_dir_does_not_leak_into_the_real_profile(self):
        """配置目录的覆盖开关必须生效 —— 否则测试会改写用户的真实设置。"""
        self.assertNotIn("SaltLRCStudio", store.config_dir())
        self.assertEqual(store.config_dir(), os.environ["SALTLRC_CONFIG_DIR"])


def _rmtree(path):
    import shutil
    shutil.rmtree(path, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
