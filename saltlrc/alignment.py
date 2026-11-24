"""Audio evidence -> reviewable lyric proposals; never distribute time by character count."""
import math
import unicodedata
from copy import deepcopy
from difflib import SequenceMatcher
from .document import Word
from .language import identify


def normalize(text):
    return "".join(c for c in unicodedata.normalize("NFKC", text).casefold() if c.isalnum())


def source_track(doc, language=None):
    """Pick which axis the alignment should work on.

    Word-level refinement needs a track that actually HAS word timings, so
    "auto" prefers main when it carries them. A translation track with only
    text and no word timings would make 行内细化 report 0/N usable lines on a
    bilingual song.
    """
    languages = identify(doc)
    if language:
        for name in ("translation", "main"):
            if languages[name]["code"] == language:
                return name
    if any(g.main.words for g in doc.groups):
        return "main"
    if any(g.translation.words for g in doc.groups):
        return "translation"
    return "translation" if any(g.translation.text for g in doc.groups) else "main"


def checked_segments(segments, duration):
    if not isinstance(segments, list) or len(segments) > 50000:
        raise ValueError("无效的转写片段列表")
    result = []
    for s in segments:
        start, end = s.get("start"), s.get("end")
        if not isinstance(start, (float, int)) or not isinstance(end, (float, int)):
            raise ValueError("识别时间必须为秒数")
        if not math.isfinite(start + end) or not 0 <= start < end <= duration / 1000 + .1:
            raise ValueError("识别时间为空、倒序或超出音频时长")
        if not isinstance(s.get("text"), str):
            raise ValueError("识别文本必须为字符串")
        result.append({"start": round(start * 1000), "end": round(end * 1000), "text": s["text"]})
    if any(a["start"] > b["start"] for a, b in zip(result, result[1:])):
        raise ValueError("识别片段必须按时间排序")
    return result


def match_transcript(doc, transcript, field="auto", max_shift=30000):
    duration = transcript["duration_ms"]
    if type(duration) is not int or not 0 < duration <= 1200000:
        raise ValueError("音频时长无效")
    segments = checked_segments(transcript["segments"], duration)
    if field == "auto":
        field = source_track(doc, transcript.get("language"))
    if field not in ("main", "translation"):
        raise ValueError("未知对齐轨道")
    expected, owners, offsets = "", [], []
    for index, g in enumerate(doc.groups):
        value = normalize(getattr(g, field).text)
        offsets.append((len(expected), len(value)))
        expected += value
        owners.extend([index] * len(value))
    actual, evidence = "", []
    for index, s in enumerate(segments):
        value = normalize(s["text"])
        actual += value
        evidence.extend([index] * len(value))
    if len(expected) > 30000 or len(actual) > 50000:
        raise ValueError("歌词过长，请按单曲处理")
    matched = {}
    for block in SequenceMatcher(None, expected, actual, autojunk=False).get_matching_blocks():
        for step in range(block.size):
            matched[block.a + step] = evidence[block.b + step]
    proposals = []
    last_end = -1
    for i, (offset, length) in enumerate(offsets):
        g = doc.groups[i]
        ids = [matched[p] for p in range(offset, offset + length) if p in matched]
        coverage = len(ids) / length if length else 0
        start = segments[min(ids)]["start"] if ids else None
        end = segments[max(ids)]["end"] if ids else None
        old = getattr(g, field).start
        old = g.start if old is None else old
        reasons = []
        if coverage < .8:
            reasons.append("原歌词与识别文本匹配不足 80%")
        if start is not None and abs(start - old) > max_shift:
            reasons.append("时间偏移超出设定范围")
        if start is not None and start < last_end:
            reasons.append("候选片段与上一句重叠")
        if ids:
            last_end = end
        # Word candidates use entire observed ASR chunks only. Never synthesize intra-chunk timestamps.
        track = getattr(g, field)
        words = []
        if coverage == 1 and ids:
            cursor, last_id = offset, None
            for char in track.text:
                chars = normalize(char)
                sid = matched.get(cursor) if chars else last_id
                if sid is None and ids:
                    sid = ids[0]
                tick = segments[sid]["start"] if sid is not None else start
                if words and words[-1]["start"] == tick:
                    words[-1]["text"] += char
                else:
                    words.append({"start": tick, "text": char})
                last_id = sid
                cursor += len(chars)
            if any(a["start"] >= b["start"] for a, b in zip(words, words[1:])):
                words = []
        proposals.append({"index": i, "field": field, "old_start": old, "start": start,
                          "end": end, "coverage": round(coverage, 3), "words": words,
                          "text": track.text, "matched_text": "".join(segments[j]["text"] for j in sorted(set(ids))),
                          "status": "ready" if ids and not reasons else "review", "reasons": reasons,
                          "kind": "asr-text-match"})
    return {"engine": transcript.get("engine", "imported"), "language": transcript.get("language"),
            "duration_ms": duration, "field": field, "proposals": proposals,
            "notice": "匹配率不是声学置信度。先试听候选；未匹配句保留原轴。中文译文仅同步句首，不生成伪逐字轴。"}


def apply_proposals(doc, result, indices, link=True):
    if not isinstance(indices, list) or not indices:
        raise ValueError("请选择要应用的候选")
    updated = deepcopy(doc)
    chosen = set(indices)
    for p in result["proposals"]:
        if p["index"] not in chosen:
            continue
        i, field = p["index"], p["field"]
        if not 0 <= i < len(updated.groups) or field not in ("main", "translation"):
            raise ValueError("候选组无效")
        g = updated.groups[i]
        for track in (g.main, g.translation):
            if track.start is None:
                track.start = g.start
        target = getattr(g, field)
        old = target.start if target.start is not None else g.start
        if old != p["old_start"] or target.text != p["text"]:
            raise ValueError("歌词已改变，请重新分析后应用")
        start, end = p["start"], p["end"]
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= result["duration_ms"]:
            raise ValueError("候选时间无效")
        delta = start - old
        for name in ("main", "translation") if link else (field,):
            track = getattr(g, name)
            track.start = (track.start if track.start is not None else g.start) + delta
            for word in track.words:
                word.start += delta
            if track.end is not None:
                track.end += delta
        if field == "main" or link:
            g.start += delta
        if p["words"]:
            target.words = [Word(**w) for w in p["words"]]
        target.start, target.end = start, end
        if link:
            # Explicit dual-axis sync anchors the other sentence, preserving its relative word rhythm.
            other = g.main if field == "translation" else g.translation
            if other.start != start:
                correction = start - other.start
                for word in other.words:
                    word.start += correction
                if other.end is not None:
                    other.end += correction
            g.start = other.start = start
        for track in (g.main, g.translation):
            ticks = [track.start] + [w.start for w in track.words] + ([track.end] if track.end is not None else [])
            if any(t is not None and (t < 0 or t > result["duration_ms"]) for t in ticks):
                raise ValueError(f"第 {i + 1} 组原有逐字轴移位后超出音频，请单轨应用并人工修正")
            if track.words:
                times = [w.start for w in track.words] + ([track.end] if track.end is not None else [])
                effective_start = g.start if track.start is None else track.start
                if track.words[0].start != effective_start or any(a >= b for a, b in zip(times, times[1:])):
                    raise ValueError(f"第 {i + 1} 组候选与已有逐字时间冲突，请先修正逐字轴")
    if any(a.start >= b.start for a, b in zip(updated.groups, updated.groups[1:])):
        raise ValueError("应用后主轨句首倒序/重复，请调整选择或手工修正")
    updated.alignment = {"engine": result["engine"], "language": result.get("language"),
                         "applied": sorted(chosen), "proposals": result["proposals"]}
    updated.notes.append("音频候选时间已应用；中文译词仍需语义对应与试听审核")
    return updated
