import json
import re
from .document import Document, Group, Track, Word
from .repair import TIME, milliseconds
from .bilingual import prefer_chinese, shift

META = re.compile(r"^\[([A-Za-z][\w-]*):(.*?)\]$")


def _yrc_entry(line):
    """一行网易云逐字（YRC）→ ``(start, Track)``；不是 YRC 就返回 ``None``。

    YRC 行长这样：``{"t":0,"c":[{"tx":"作曲: ","t":0,"d":500}, …]}``
    （`t` 是行时间；`c` 里每个单元自己的 `t` 是**相对行首的偏移**、`d` 是时长）。

    `yrc` 导出写出来的就是这个格式，所以解析器也要认它。真实曲库里的 `.lrc` 常常是
    **混合**的：版权/制作信息那几行走 YRC、正文走普通 LRC。认不出来的行按"未解析"记。
    """
    if not line.startswith("{"):
        return None
    try:
        payload = json.loads(line)
    except ValueError:
        return None
    if not isinstance(payload, dict) or "t" not in payload or "c" not in payload:
        return None
    try:
        start = int(payload["t"])
    except (TypeError, ValueError):
        return None
    words, cursor = [], start
    for unit in payload.get("c") or []:
        if not isinstance(unit, dict):
            continue
        text = str(unit.get("tx") or "")
        if not text:
            continue
        try:
            offset = int(unit.get("t") or 0)
            duration = int(unit.get("d") or 0)
        except (TypeError, ValueError):
            offset, duration = 0, 0
        at = max(start, start + offset)
        words.append(Word(at, text))
        cursor = max(cursor, at + max(0, duration))

    # **不变量**：首个逐字时间必须等于句首；逐字时间必须严格递增；句尾必须晚于最后一个字。
    # YRC 里这三条都可能不成立（相邻单元同毫秒、时长缺失），所以这里就地夹紧，
    # 保证结果能通过 `check()`。
    fixed = []
    for index, word in enumerate(words):
        floor = start if index == 0 else fixed[index - 1].start + 1
        fixed.append(Word(max(floor, word.start), word.text))
    if not fixed:
        return start, Track("", [], None)
    end = max(cursor, fixed[-1].start + 1)
    return start, Track("".join(w.text for w in fixed), fixed, end)


def parse(text, auto_chinese=True):
    doc = Document()
    records = []
    previous = None
    for number, raw in enumerate(text.lstrip("\ufeff").splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        meta = META.fullmatch(line)
        if meta:
            key = meta[1].lower()
            if key in doc.metadata:
                doc.notes.append(f"元数据 {key} 重复，使用最后值")
            doc.metadata[key] = meta[2]
            continue
        yrc = _yrc_entry(line)
        if yrc is not None:
            # 一行网易云逐字：直接就是一条带逐字时间的记录（见 `_yrc_entry` 的说明）
            entries = [yrc]
        else:
            matches = list(TIME.finditer(line))
            if not matches or matches[0].start() != 0:
                doc.unresolved.append(f"第 {number} 行未解析：{raw}")
                continue
            try:
                times = [milliseconds(m) for m in matches]
            except ValueError as error:
                doc.unresolved.append(f"第 {number} 行：{error}：{raw}")
                continue
            if any(":" in m[0].split(":", 1)[1] for m in matches):
                doc.notes.append(f"第 {number} 行冒号小数时间已规范化")
            pieces = [line[m.end():matches[i + 1].start() if i + 1 < len(matches) else len(line)]
                      for i, m in enumerate(matches)]
            # [t1][t2]text is repeated line syntax. Angle-bracket markers are word syntax.
            repeat = len(matches) > 1 and all(m[1] == "[" for m in matches) and not any(pieces[:-1]) and bool(pieces[-1])
            if repeat:
                entries = [(t, Track(pieces[-1])) for t in times]
            elif len(matches) == 1:
                entries = [(times[0], Track(pieces[0]))]
            else:
                first = 1 if not pieces[0] and matches[1][1] == "<" else 0
                end = times[-1] if pieces[-1] == "" else None
                stop = len(times) - 1 if end is not None else len(times)
                words = [Word(times[i], pieces[i]) for i in range(first, stop)]
                entries = [(times[0], Track("".join(w.text for w in words), words, end))]
        for start, track in entries:
            if re.search(r"[\[<]\d+:", track.text):
                doc.unresolved.append(f"第 {number} 行含未识别的时间标签：{raw}")
            if previous is not None and start < previous:
                doc.notes.append("句首时间倒序，已稳定排序（逐字时间不自动重排）")
            previous = start
            records.append((start, track))
    buckets = {}
    for start, track in sorted(records, key=lambda item: item[0]):
        tracks = buckets.setdefault(start, [])
        if track in tracks:
            doc.notes.append(f"{start}ms 完全重复行已去重")
        else:
            tracks.append(track)
    for start, tracks in buckets.items():
        doc.groups.append(Group(start, tracks[0], tracks[1] if len(tracks) > 1 else Track()))
        if len(tracks) > 2:
            doc.unresolved.append(f"{start}ms 超过两条不同歌词，额外内容：" + " / ".join(t.text for t in tracks[2:]))
    offset = doc.metadata.pop("offset", None)
    if offset is not None:
        try:
            doc = shift(doc, int(offset))
            doc.notes.append(f"已应用 offset={offset}ms，导出不再重复偏移")
        except ValueError as error:
            doc.unresolved.append(f"offset 无法应用：{error}")
    if auto_chinese:
        prefer_chinese(doc)
    doc.notes = list(dict.fromkeys(doc.notes))
    return doc
