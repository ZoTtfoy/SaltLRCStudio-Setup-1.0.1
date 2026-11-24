"""量"打开 → 编辑 → 保存"这条链路：**先量，再改**。

逐段打印耗时与峰值内存：A 打开、B 编辑、C 导出、D 工程落盘、E 内嵌音频、F 指纹。

放在 `tests/` 下、以 `_` 开头：它是**夹具/工具**，不是用例
（`discover -p "test_*.py"` 不会收它），并且会跟着源码一起交付。

用法：
    python tests/_bench_io.py            # 默认 60 行 / 每行 8 字 / 30 MB 音频
    python tests/_bench_io.py --audio 0  # 跳过内嵌音频那一段（省内存）
"""
import argparse
import base64
import json
import os
import struct
import sys
import time
import tracemalloc

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from saltlrc import edits, projects, store                  # noqa: E402
from saltlrc.document import Document                       # noqa: E402
from saltlrc.exporter import export                         # noqa: E402
from saltlrc.parser import parse                            # noqa: E402


def make_lrc(lines, words):
    """一份"像真歌"的 LRC：主歌词是**逐字时间戳**那种形状 + 一行翻译。

    形状照 `exporter` 自己写出来的样子（`[t0]字[t1]字…[tn]`，末尾那一个是句尾），
    而且**主歌词与翻译行共用一个句首时间戳** —— 解析器靠它把两行配成一组。
    逐字时间戳必须递增，否则 `insert_group` 当场拒绝。
    """
    def stamp(ms):
        return "[%02d:%02d.%03d]" % (ms // 60000, (ms % 60000) // 1000, ms % 1000)

    out = []
    for index in range(lines):
        start = index * 3000
        main = ''.join(stamp(start + i * 120) + '字' for i in range(words))
        main += stamp(start + words * 120)          # 句尾
        out.append(main)
        out.append(stamp(start) + '翻译行 %d' % index)
    return '\n'.join(out) + '\n'


def timed(label, function, repeat=1):
    """跑一遍并打出「平均耗时 + 峰值内存」。

    峰值用 tracemalloc（只看 Python 侧分配），所以它反映的是"这一段代码
    自己造了多大的临时对象" —— 正是"内存与占用"要盯的东西。
    """
    tracemalloc.start()
    begin = time.perf_counter()
    result = None
    for _ in range(repeat):
        result = function()
    elapsed = (time.perf_counter() - begin) / repeat
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    print('  %-34s %9.2f ms   峰值 %9.1f KB' % (label, elapsed * 1000, peak / 1024))
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--lines', type=int, default=60)
    parser.add_argument('--words', type=int, default=8)
    parser.add_argument('--audio', type=int, default=30, help='内嵌音频 MB，0 = 跳过')
    parser.add_argument('--queue', type=int, default=300, help='队列里有几首歌（指纹那一段）')
    options = parser.parse_args()

    text = make_lrc(options.lines, options.words)
    print('夹具：%d 行 / 每行 %d 字；LRC 文本 %.1f KB' % (
        options.lines, options.words, len(text) / 1024))
    print()

    print('A 打开')
    document = timed('parse()', lambda: parse(text))
    payload = document.to_dict()
    blob = json.dumps(payload, ensure_ascii=False)
    print('     → %d 组；文档 JSON %.1f KB' % (len(document.groups), len(blob) / 1024))
    timed('Document.from_dict()', lambda: Document.from_dict(payload))
    timed('json.dumps(document)', lambda: json.dumps(payload, ensure_ascii=False))

    print()
    print('B 编辑（中间那一行，最坏位置）')
    middle = len(payload['groups']) // 2
    timed('insert_group（含反序列化）',
          lambda: edits.insert_group(Document.from_dict(payload), middle))
    timed('delete_group（含反序列化）',
          lambda: edits.delete_group(Document.from_dict(payload), middle))
    timed('insert_group（只算编辑本身）',
          lambda: edits.insert_group(Document.from_dict(payload), middle), 1)

    print()
    print('C 导出')
    timed('export(salt)', lambda: export(document, salt=True, fmt='salt'))
    timed('export(lrc) ', lambda: export(document, salt=False, fmt='lrc'))

    print()
    print('D 工程落盘（**不内嵌音频** = 本地工程库每次编辑走的那条）')
    # 临时目录只有一个出口（tests/_scratch.py）—— mkdtemp 只许出现在那个文件里，
    # 工具脚本也用它建目录。
    from _scratch import scratch as scratch_dir
    work = scratch_dir('saltlrc-bench-')
    os.environ['SALTLRC_CONFIG_DIR'] = os.path.join(work, 'cfg')
    projects.set_root(os.path.join(work, 'projects'))
    record = projects.create('基准测试')
    plain = {'kind': 'saltlrc-project', 'version': 1, 'name': '基准测试',
             'duration': None, 'document': payload, 'audio': None}
    plain_body = json.dumps(plain, ensure_ascii=False)
    print('     → 工程 JSON %.1f KB' % (len(plain_body) / 1024))
    timed('json.dumps（客户端那一步）', lambda: json.dumps(plain, ensure_ascii=False))
    timed('json.loads（服务端那一步）', lambda: json.loads(plain_body))
    timed('projects.write（原子写）',
          lambda: projects.write(record['id'], plain_body))

    if options.audio:
        print()
        print('E 内嵌音频的工程（%d MB 的歌）—— **旧路径 vs 新路径**' % options.audio)
        size = options.audio * 1024 * 1024
        raw = b'\x00\x01' * (size // 2)
        slim = json.dumps({'kind': 'saltlrc-project', 'version': 1, 'name': '带音频',
                           'duration': None, 'document': payload, 'audio': None},
                          ensure_ascii=False).encode('utf-8')

        print('  旧路径（v0.23.4 及以前：base64 进 JSON）')
        old_total = time.perf_counter()
        encoded = timed('  base64 编码 %d MB' % options.audio,
                        lambda: base64.b64encode(raw).decode('ascii'))
        fat = json.dumps({'kind': 'saltlrc-project', 'version': 1, 'name': '带音频',
                          'duration': None, 'document': payload,
                          'audio': {'name': 'a.flac', 'type': 'audio/flac',
                                    'size': len(raw), 'data': encoded}}, ensure_ascii=False)
        print('     → POST 体 %.1f MB（音频 base64 后膨胀 33%%）' % (len(fat) / 1048576))
        timed('  服务端 json.loads（%.1f MB 的体）' % (len(fat) / 1048576),
              lambda: json.loads(fat))
        timed('  写盘 %.1f MB' % (len(fat) / 1048576),
              lambda: projects.write(record['id'], fat))
        old_seconds = time.perf_counter() - old_total

        print('  新路径（v0.24.0：容器 + /api/save-binary，字节进字节出）')
        new_total = time.perf_counter()
        head = b'SaltLRC1' + struct.pack('<I', len(slim))
        container = timed('  拼容器（一次搬运）', lambda: head + slim + raw)
        target = os.path.join(work, 'container.saltproj')

        def write_bytes(blob):
            with open(target, 'wb') as handle:
                handle.write(blob)
            return len(blob)

        timed('  写盘 %.1f MB' % (len(container) / 1048576), lambda: write_bytes(container))
        new_seconds = time.perf_counter() - new_total

        print('  → 合计：旧 %.0f ms / %.1f MB 落盘；新 %.0f ms / %.1f MB（省掉 base64 与解析两趟）'
              % (old_seconds * 1000, len(fat) / 1048576,
                 new_seconds * 1000, len(container) / 1048576))

    print()
    print('F 指纹：前端 flush() 对队列里**每一个**文档各做一次 JSON.stringify')
    one = len(blob)
    for count in (10, 50, options.queue):
        docs = [payload] * count
        timed('JSON.stringify × %d 首' % count,
              lambda docs=docs: json.dumps(docs, ensure_ascii=False))
        print('     → 每次 flush 要走过 %.1f MB 文本' % (one * count / 1048576))

    # （临时目录由 tests/_scratch.py 在进程退出时统一收掉，这里不用管）


if __name__ == '__main__':
    main()
