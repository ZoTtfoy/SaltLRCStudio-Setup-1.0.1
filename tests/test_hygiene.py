"""仓库卫生：这些规矩不靠自觉，靠一条静态断言。

`tempfile.mkdtemp()` 只允许出现在 `tests/_scratch.py` 里：多个文件各写一份
就会把残留目录堆在 `%TEMP%` 里（清理方式还可能不一致，甚至完全不清理）。

界线说明：这里只钉**临时目录的创建出口**，不钉"用哪个清理函数" ——
关键是"创建"这件事只有一个出口。
"""
import pathlib
import re
import unittest

HERE = pathlib.Path(__file__).parent
ROOT = HERE.parent
TESTS = HERE          # 测试文件就在本目录
EXEMPT = {"_scratch.py"}          # 唯一允许出现 mkdtemp 的文件


class HygieneTests(unittest.TestCase):
    def test_temp_directories_have_exactly_one_creator(self):
        """`mkdtemp` 只许出现在 `tests/_scratch.py` 里。

        它是"临时目录只有一个出口"这条规矩的守卫。漏一个文件就还有一处泄漏。

        判据只认 `mkdtemp` 这个标识符，不认注释 —— 所以先剥注释再判：
        注释里出现这些字样同样会命中（CSS 的 `width:auto`、`.iss` 的
        `[UninstallDelete]`、JS 的 `download(`、`#proposals`）。
        """
        # 关键字**拼出来**，不要在代码里写成字面量 —— 否则这条检查会把自己抓了。
        needle = "mkd" + "temp("
        offenders = []
        for path in sorted(TESTS.glob("*.py")):
            if path.name in EXEMPT:
                continue
            source = path.read_text(encoding="utf-8")
            bare = re.sub(r'"""(?:.|\n)*?"""', "", source)      # 模块/函数 docstring
            bare = re.sub(r"'''(?:.|\n)*?'''", "", bare)
            bare = re.sub(r"(?m)^\s*#.*$", "", bare)           # 行注释
            if needle in bare:
                line = next(i + 1 for i, text in enumerate(bare.splitlines()) if needle in text)
                offenders.append("%s 第 %d 行" % (path.name, line))
        self.assertEqual(offenders, [],
                         "这些地方自己建临时目录了 —— 请改用 tests/_scratch.py 的 scratch()：\n  "
                         + "\n  ".join(offenders))

    def test_the_shared_helper_registers_its_cleanup(self):
        """`_scratch.scratch()` 必须真的把目录登记进"退出时删除"的清单。

        这条是上一条的反面：只有一个出口，但那个出口自己不清理，等于没修。
        """
        source = (TESTS / "_scratch.py").read_text(encoding="utf-8")
        self.assertIn("atexit.register", source, "临时目录要在进程退出时删掉")
        self.assertIn("shutil.rmtree", source)
        self.assertRegex(source, r"_DIRS\.append\(path\)",
                         "建出来的目录要进清单，否则永远不会被清")

    def test_no_test_writes_into_a_users_own_folders(self):
        """测试不许把**用户的个人目录**写进代码（桌面 / 文档 / 下载）。

        临时目录一律用 `scratch()`。

        判据**刻意收窄**到这三类目录名：工具位置（node 二进制、本工程根目录、
        `.workbuddy\\binaries\\…`）是正当的硬编码，不该被这条盯上。
        """
        needle = ("Desk" + "top", "Docu" + "ments", "Down" + "loads")
        offenders = []
        for path in sorted(TESTS.glob("*.py")):
            source = path.read_text(encoding="utf-8")
            for match in re.finditer(r'["\']([A-Za-z]:\\\\?[^"\']*)["\']', source):
                literal = match.group(1)
                if not any(word + "\\\\" in literal for word in needle):
                    continue
                # 只有"盘符 + …\\Desktop\\…"这种绝对路径才算写死
                if re.match(r"^[A-Za-z]:\\\\", literal):
                    offenders.append("%s：%s" % (path.name, literal))
        self.assertEqual(offenders, [],
                         "测试里写死了用户的个人目录（请改用 tests/_scratch.py 的 scratch()）：\n  "
                         + "\n  ".join(offenders))


if __name__ == "__main__":
    unittest.main()
