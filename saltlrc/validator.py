"""导出前的校验。

职责：守住"导出器能正确写出这首歌"这条线；问题文案给出"下一步能做什么"。
同一类问题逐行重复时收成一条汇总（见 `MAX_DETAILED`）。
"""

import re

from .bilingual import japanese, chinese_candidate

GROUP_PREFIX = re.compile(r"^第 (\d+) 组：(.+)$")

# 同一类问题出现在 ≤ 这个行数时逐条列出（"定位到那一行"仍然好用）；再多只留一条汇总。
MAX_DETAILED = 5


def _label(index):
    return f"第 {index + 1} 组：" if index is not None else ""


def _finish(entries):
    """把 (index, level, title, hint) 的流水收成 (errors, warnings, items)。

    同一类（level + 标题相同）超过 MAX_DETAILED 行时收成一条：

        ``共 59 行缺少主歌词逐字时间（第 2、3、4… 组）—— <怎么做>``

    `items` 里这条的 index 指向**第一个**出问题的行，所以「定位」照样能用。
    少于等于 MAX_DETAILED 时逐条输出（每条带"第 N 组"前缀，定位到具体行最好用）。
    """
    order, grouped = [], {}
    for index, level, title, hint in entries:
        key = (level, title, hint)
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(index)

    errors, warnings, items = [], [], []
    for level, title, hint in order:
        indexes = grouped[(level, title, hint)]
        bucket = errors if level == "error" else warnings
        if len(indexes) <= MAX_DETAILED:
            for index in indexes:
                text = title + ("（%s）" % hint if hint else "")
                bucket.append(_label(index) + text)
                items.append({"level": level, "index": index, "message": text})
            continue
        numbered = [value for value in indexes if value is not None]
        shown = "、".join("第 %d 组" % (value + 1) for value in numbered[:3])
        tail = "…" if len(numbered) > 3 else ""
        summary = "共 %d 行%s（%s%s）" % (len(indexes), title, shown, tail)
        if hint:
            summary += "——" + hint
        bucket.append(summary)
        items.append({"level": level, "index": numbered[0] if numbered else None,
                      "message": summary, "count": len(indexes)})
    return errors, warnings, items


def validate(doc, strict=True, duration=None, require_translation=False, require_language=False):
    """检查一份文档，返回 ``{"errors", "warnings", "items"}``。

    ``strict`` 是 Salt 档位：**逐字与翻译配对的要求只在这个档位检查**
    （标准 LRC 不需要逐字时间）。

    **分级判据**：*导出器能"照实写出来"的问题 → **提示**（不拦导出）；
    只有"写出来就是坏文件"的才是**错误**。*

    所以"某行没有逐字时间 / 没有句尾 / 首个字的时间与句首不同 / 翻译句首与主轨不同"
    都只是提示 —— 导出时按行的实际形态写（没有逐字就写逐行形式、没有句尾就不写结尾标记）。
    仍然拦路的只有：逐字时间倒退、逐字文字与主歌词对不上、时间非法或超出音频时长。

    语言检查是可选开关（`require_language`）：日语原文 + 中文翻译是本曲库的正常情况。
    """
    # 「未解析」是**解析时的提示**，不拦导出：跳过的内容在提示里逐条写着。
    found = [(None, "warning", message, "") for message in doc.unresolved]
    found += [(None, "warning", message, "") for message in doc.notes]
    if not doc.groups:
        found.append((None, "error", "没有可导出的歌词", ""))
    last = -1
    for i, g in enumerate(doc.groups, 1):
        index = i - 1

        def bad(title, level="error", hint=""):
            """hint 是"那我要怎么做"—— 带提示的问题都要给出下一步。"""
            found.append((index, level, title, hint))

        if type(g.start) is not int or g.start < 0:
            bad("句首时间必须是非负整数毫秒", hint="在表格的「句首 / 秒」里改，或用「整体偏移」")
            continue
        if g.start <= last:
            bad("句首时间必须递增且唯一", hint="在表格里改「句首」，或先删掉重复的那一行")
        last = g.start
        for track in (g.main, g.translation):
            if track.start is not None and (type(track.start) is not int or track.start < 0):
                bad("轨道开始时间必须是非负整数毫秒")
            if any(c in track.text for c in "\r\n") or re.search(r"[\[<]\d+:", track.text):
                bad("歌词文本中含换行或时间标签", hint="文字里不能有换行，也不能带 [00:00.00] 这类时间标签")
        if not g.main.text:
            # 空行**不是错误**：伴奏过门在源文件里就写成 `[00:29.737]`，
            # 导出时写一个裸时间戳保持停顿即可。
            bad("主歌词为空（伴奏过门？）", "warning",
                hint="如果是误插的空行，用行内「删除」去掉；过门可以留着")
        if require_translation and not g.translation.text:
            bad("缺少翻译轨")
        if require_language:
            if not chinese_candidate(g.main.text):
                bad("主歌词不是中文候选文本，请检查语言顺序")
            if not g.translation.text:
                bad("缺少日语翻译轨")
            elif not japanese(g.translation.text):
                bad("翻译无假名，无法自动确认日语，请人工核对", "warning")
        if strict:
            # 这几条都是"某一行的问题"，一律降成**提示**：错误会让 `export(salt)` 抛出去，
            # 而 `_render_or_fallback` 会把**整首**降级成标准 LRC（别的行已经写好的逐字一起没了）。
            # 下面这几条导出器都写得出来（写不成逐字就写逐行、没有句尾就不写结尾标记、
            # 两个时间戳都写出来）。
            if g.main.start is not None and g.main.start != g.start:
                bad("主轨开始时间与句首不一致", "warning",
                    hint="导出时按「句首」写（文件里那一行只有一个行首位置）；"
                         "想让两边一致就用行内的「双轴同步」")
            if g.translation.text and g.translation.start is not None and g.translation.start != g.start:
                bad("双轴尚未同步；翻译句首与主轨不同", "warning",
                    hint="导出时翻译按主轨句首写（播放器靠它把两行配成一对）；"
                         "想显式对齐就用行内的「双轴同步」")
            if not g.main.words and g.main.text.strip():
                # **有字的行**才谈得上逐字时间：空行（伴奏过门 `[00:29.737]`）没有字要计时，
                # 在逐字格式里写成"一个裸时间戳"，不要求补逐字。
                # 没有逐字的行照旧导出（写成逐行形式）—— 同一份文件里两种形式可以混着来。
                bad("没有逐字时间（导出时这一行写成逐行形式）", "warning",
                    hint="Salt 格式支持逐行与逐字混在一份文件里，不补也能导出；"
                         "想要这一行也逐字高亮就双击波形上那一句，用「按字数均分」"
                         "或「从音频检测这一行」补")
        if g.translation.words:
            bad("翻译轨逐字时间仅在工程中保留，导出仅保留句首时间", "warning")
        t = g.main
        if t.words:
            if "".join(w.text for w in t.words) != t.text:
                # **仍然是错误**：同一行有两份互相矛盾的文字（表格看的是 `text`、
                # 导出写的是逐字），导出会**只写其中一份**。点一下「自动分词」即可重排。
                bad("逐字文字与主歌词不一致", hint="在逐字编辑器里「自动分词」重排一次")
            ticks = [w.start for w in t.words]
            if t.end is None:
                # 没有句尾也能照实写：**不写结尾标记**，播放器按"延续到下一行"处理
                # （标准 LRC 的约定）。所以是提示，不是错误。
                bad("缺少显式句尾时间（导出时不写结尾标记，这一行会延续到下一行）", "warning",
                    hint="在逐字编辑器里填「句尾」，或在波形上拖句尾竖线")
            else:
                ticks.append(t.end)
            if any(type(v) is not int or v < 0 for v in ticks):
                bad("逐字时间必须是非负整数毫秒")
            elif ticks:
                if ticks[0] != g.start:
                    bad("首个逐字时间与句首不同", "warning",
                        hint="两个时间戳导出时都会写出来（行首写 [句首]、字写 <>）；"
                             "想对齐用「句尾 = 当前」旁的数字框")
                if any(b < a for a, b in zip(ticks, ticks[1:])):
                    bad("逐字时间不能倒退", hint="在逐字编辑器里把顺序理一遍")
                if any(b == a for a, b in zip(ticks, ticks[1:])):
                    bad("存在相同时间的字（播放器会瞬间带过，建议在逐字编辑里拉开）", "warning")
                if any(0 < b - a < 80 for a, b in zip(ticks, ticks[1:])):
                    bad("存在不足 80ms 的字（快歌词常见，不影响写入）", "warning")
                if duration is not None and max(ticks) > duration:
                    bad("时间超出音频时长", hint="确认音频是不是加载错了，或把这行往前挪")
        if duration is not None and g.start > duration:
            bad("句首超出音频时长", hint="确认音频是不是加载错了，或把这行往前挪")
        if i < len(doc.groups) and t.end is not None and type(t.end) is int and t.end > doc.groups[i].start:
            bad("句尾与下一组重叠，请核对演唱", "warning")

    errors, warnings, items = _finish(found)
    return {"errors": errors, "warnings": warnings, "items": items}
