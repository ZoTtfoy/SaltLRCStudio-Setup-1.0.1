import json

from .document import Word
from .repair import stamp, to_bytes
from .validator import validate


def _yrc_lines(doc):
    """Netease YRC: one JSON object per line, per-character offsets/durations."""
    lines = []
    groups = doc.groups
    for index, group in enumerate(groups):
        track = group.main
        if not track.text:
            continue
        start = int(group.start)
        # 没有逐字的行整行当一个单元：时长优先用这一行自己的句尾，没有句尾就用下一行的句首
        # （标准 LRC 的"延续到下一行"）；只有最后一行退回 +500ms。
        words = list(track.words) or [Word(start, track.text)]
        if track.end is not None:
            end = int(track.end)
        elif index + 1 < len(groups):
            end = int(groups[index + 1].start)
        else:
            end = int(words[-1].start) + 500
        chars = []
        for position, word in enumerate(words):
            next_start = int(words[position + 1].start) if position + 1 < len(words) else end
            chars.append({"tx": word.text,
                          "t": max(0, int(word.start) - start),
                          "d": max(0, next_start - int(word.start))})
        lines.append({"t": start, "c": chars})
    return lines


def export(doc, salt=True, duration=None, translation_timestamp=True,
           require_translation=False, require_language=False, fmt=None, encoding="utf-8"):
    """Render a document as LRC or YRC text.

    ``fmt`` is ``salt`` (word-level main line + plain translation), ``lrc``
    (line-a-time bilingual) or ``yrc`` (Netease JSON, main track only). YRC has
    no notion of a translation track, so the translation is dropped there.
    """
    fmt = (fmt or ("salt" if salt else "lrc")).lower()
    if fmt not in ("salt", "lrc", "yrc"):
        raise ValueError("不支持的导出格式：%s" % fmt)
    report = validate(doc, strict=fmt in ("salt", "yrc"), duration=duration,
                      require_translation=require_translation,
                      require_language=require_language)
    if report["errors"]:
        raise ValueError("\n".join(report["errors"]))

    if fmt == "yrc":
        body = "\n".join(json.dumps(line, ensure_ascii=False, separators=(",", ":"))
                         for line in _yrc_lines(doc))
        return body + "\n" if body else ""

    lines = []
    word_level = fmt == "salt"
    # The Salt profile writes no metadata; standard LRC keeps it.
    if not word_level:
        for key, value in doc.metadata.items():
            if key != "offset" and not any(c in key + value for c in "\r\n[]"):
                lines.append(f"[{key}:{value}]")
    for g in doc.groups:
        if word_level:
            if g.main.words:
                # **逐字行**：行首 `[句首]` + 每个字 `<时间>字`。
                #
                # 句尾为 None 时**不写结尾标记**：播放器按"这一行延续到下一行"处理
                # （标准 LRC 的约定），解析器读回来 `end` 仍然是 None，
                # 所以"导出 → 回读 → 再导出"结果一致。
                body = "".join(f"<{stamp(w.start)}>{w.text}" for w in g.main.words)
                tail = f"<{stamp(g.main.end)}>" if g.main.end is not None else ""
                lines.append(f"[{stamp(g.start)}]{body}{tail}")
            else:
                # **没有逐字的行写逐行形式** `[句首]整行`；空行（伴奏过门）写一个裸时间戳。
                lines.append(f"[{stamp(g.start)}]{g.main.text}" if g.main.text.strip()
                             else f"[{stamp(g.start)}]")
        else:
            lines.append(f"[{stamp(g.start)}]{g.main.text}")
        if g.translation.text:
            if translation_timestamp:
                translation_start = g.start if word_level or g.translation.start is None else g.translation.start
                lines.append(f"[{stamp(translation_start)}]{g.translation.text}")
            else:
                lines.append(g.translation.text)
    return "\n".join(lines) + "\n"
