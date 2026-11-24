"""Word-level timing and document assembly for fetched lyrics.

Netease serves line-level truth: the original lyric and its translation both
carry real line start times, but per-word (YRC) timings are restricted to
logged-in sessions. When real word timings are absent we estimate them inside
each line, bounded by the next line's start so a line never bleeds into the
next one. Estimates are marked as such and never presented as transcribed truth.
"""

import re

from . import tokenizers
from .document import Document, Group, Track, Word
from .lrctext import CREDIT, parse_lrc

CJK = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uf900-\ufaff\uff66-\uff9f]")
LATIN_WORD = re.compile(r"[A-Za-z0-9']+")
HIRAGANA = re.compile(r"[\u3041-\u309f]")
KATAKANA = re.compile(r"[\u30a0-\u30ff\uff66-\uff9f]")
KANJI = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")
TAG_LIKE = re.compile(r"([\[<])(\d+:)")

DEFAULT_OPTIONS = {
    "unit": "smart",       # "smart" (words / kana groups), "char", or "word"
    "tail": 0.95,          # fraction of the gap to the next line that this line occupies
    "min_line_ms": 600,
    "max_line_ms": 12000,
    "require_translation": False,
    "main_source": "original",   # "original" keeps the sung lyric as the main track
}

_WEIGHT_CJK = 1.0
_WEIGHT_LATIN = 0.8
_WEIGHT_PUNCT = 0.3


def _classify(unit):
    """Weight of one display unit, summed per character so "Hey," is not
    punished for carrying a comma."""
    total = 0.0
    for char in unit:
        if char.isspace():
            continue
        if CJK.fullmatch(char):
            total += _WEIGHT_CJK
        elif LATIN_WORD.fullmatch(char):
            total += _WEIGHT_LATIN
        else:
            total += _WEIGHT_PUNCT
    return total if total > 0 else _WEIGHT_PUNCT


def tokenize(text, mode="smart", lang=None):
    """Split lyric text into display units.

    "smart" (default) applies the rule for the line's language — see
    :mod:`saltlrc.tokenizers`: Chinese one unit per character, Japanese one unit
    per pronunciation (漢字+送りがな), English one unit per word, Korean one unit
    per syllable block, and a labelled mixed fallback when the script does not
    identify a language. ``lang`` overrides detection when the caller already
    knows the song's language, which beats guessing from one short line.

    "word" keeps Latin words whole but leaves every CJK character alone;
    "char" splits everything per character, Latin included. Those two stay
    language-independent on purpose: someone who asks for one cell per character
    means it for every script.

    Whitespace is never a unit of its own: it is merged into the neighbouring
    unit, so the concatenated unit texts reproduce the line exactly. The
    exporter and the validator both rely on that: a dropped space would break
    every line that contains one.
    """
    units, _info = tokenizers.tokenize(text, mode=mode, lang=lang)
    return units


def tokenize_info(text, mode="smart", lang=None, detector=None):
    """Same as :func:`tokenize` but also reports which rule produced the units."""
    return tokenizers.tokenize(text, mode=mode, lang=lang, detector=detector)
    units = []
    buffer = ""
    for char in text:
        if LATIN_WORD.match(char):
            buffer += char
            continue
        if buffer:
            units.extend(list(buffer) if mode == "char" else [buffer])
            buffer = ""
        units.append(char)
    if buffer:
        units.extend(list(buffer) if mode == "char" else [buffer])

    if mode == "smart":
        units = _regroup(units)

    merged, lead = [], ""
    for unit in units:
        if unit.isspace():
            if merged:
                merged[-1] += unit
            else:
                lead += unit
            continue
        merged.append(lead + unit)
        lead = ""
    if lead:
        merged.append(lead)
    return merged


def sanitize(text):
    """Neutralise text that would be re-read as a time tag on export."""
    return TAG_LIKE.sub(lambda m: "（" + m[2] if m[1] == "[" else "＜" + m[2], text)


def distribute(start, end, text, mode="char"):
    """Spread ``text`` across ``[start, end)`` as monotonic word timings."""
    units = tokenize(text, mode)
    if not units:
        return []
    weights = [_classify(u) for u in units]
    total = sum(weights)
    if total <= 0:
        weights = [1.0] * len(units)
        total = float(len(units))
    span = max(0, end - start)
    words = []
    cursor = 0.0
    for unit, weight in zip(units, weights):
        words.append(Word(start + int(round(span * cursor / total)), unit))
        cursor += weight
    # Guarantee strict monotonicity; the last unit must also precede the end.
    for index in range(1, len(words)):
        if words[index].start <= words[index - 1].start:
            words[index].start = words[index - 1].start + 1
    if words[-1].start >= end:
        limit = end - 1
        for index in range(len(words) - 1, -1, -1):
            if words[index].start >= limit:
                words[index].start = limit - (len(words) - 1 - index)
            else:
                break
    return words


def _usable(lines, drop_credits):
    out = []
    for item in lines:
        text = (item.get("text") or "").strip()
        if not text:
            continue
        if drop_credits and CREDIT.match(text):
            continue
        out.append(item)
    return out


def parse_manual(main_text, translation_text="", drop_credits=True):
    """Turn user-pasted text into timed lines.

    The main lyric must carry timestamps. The translation may be a timed LRC
    block, or a plain line list with exactly as many rows as the main lyric,
    in which case rows pair up in order.
    """
    main = _usable(parse_lrc(main_text or "")["lines"], drop_credits)
    if not main:
        raise ValueError("主歌词没有可用的时间轴，请粘贴带 [mm:ss.xx] 时间标签的歌词")
    translation = []
    if (translation_text or "").strip():
        timed = _usable(parse_lrc(translation_text)["lines"], False)
        if timed:
            translation = timed
        else:
            rows = [row.strip() for row in translation_text.splitlines() if row.strip()]
            if len(rows) != len(main):
                raise ValueError("纯文本翻译有 %d 行，主歌词有 %d 行；请让行数一致，或提供带时间的翻译"
                                 % (len(rows), len(main)))
            translation = [{"time": item["time"], "text": row, "words": []}
                           for item, row in zip(main, rows)]
    return {"main": main, "translation": translation}


def align_translation(main_lines, translation_lines, tolerance=2500):
    """Map translation lines onto main lines.

    Netease usually stamps the translation with the original's timings, but not
    always. Exact matches win; the remainder pairs by nearest timestamp, once
    each, in chronological order.
    """
    mapping = {}
    if not translation_lines:
        return mapping
    remaining = sorted(translation_lines, key=lambda item: item["time"])
    used = set()
    for index, line in enumerate(main_lines):
        for position, candidate in enumerate(remaining):
            if position in used:
                continue
            if candidate["time"] == line["time"]:
                mapping[index] = candidate
                used.add(position)
                break
    for index, line in enumerate(main_lines):
        if index in mapping:
            continue
        best, best_gap = None, None
        for position, candidate in enumerate(remaining):
            if position in used:
                continue
            gap = abs(candidate["time"] - line["time"])
            if gap <= tolerance and (best_gap is None or gap < best_gap):
                best, best_gap, best_position = candidate, gap, position
        if best is not None:
            mapping[index] = best
            used.add(best_position)
    return mapping


def build(main_lines, translation_lines=None, duration=None, options=None, word_level=False):
    """Assemble a :class:`Document` from provider lines."""
    opts = dict(DEFAULT_OPTIONS)
    opts.update(options or {})
    lines = sorted((l for l in (main_lines or []) if (l.get("text") or "").strip()),
                   key=lambda item: item["time"])
    doc = Document()
    if not lines:
        doc.unresolved.append("没有可使用的主歌词行")
        return doc
    mapping = align_translation(lines, translation_lines or [])
    notes = []
    for index, line in enumerate(lines):
        start = int(line["time"])
        text = sanitize((line.get("text") or "").strip())
        if not text:
            continue
        next_start = lines[index + 1]["time"] if index + 1 < len(lines) else None
        if next_start is not None:
            span = int(round((int(next_start) - start) * float(opts["tail"])))
        elif duration:
            span = int(duration) - start
        else:
            span = int(opts["max_line_ms"])
        span = max(int(opts["min_line_ms"]), min(int(span), int(opts["max_line_ms"])))
        end = start + span
        supplied = line.get("words") or []
        if word_level and len(supplied) > 1 and all(w.get("time") is not None for w in supplied):
            words = [Word(int(w["time"]), sanitize(w["text"])) for w in supplied if w.get("text")]
            if words and words[0].start != start:
                words[0].start = start
            end = max(end, words[-1].start + 1) if words else end
        else:
            words = distribute(start, end, text, opts["unit"])
        group = Group(start, Track(text, words, end, start))
        candidate = mapping.get(index)
        if candidate and (candidate.get("text") or "").strip():
            group.translation = Track(sanitize(candidate["text"].strip()), [], None, start)
        doc.groups.append(group)
    # Deduplicate identical consecutive starts introduced by provider quirks.
    cleaned = []
    for group in doc.groups:
        if cleaned and cleaned[-1].start == group.start:
            notes.append(f"{group.start}ms 重复句首已合并")
            continue
        cleaned.append(group)
    doc.groups = cleaned
    if not word_level:
        notes.append("逐字时间为按字符权重估算，非语音识别结果")
    if translation_lines and not mapping:
        notes.append("翻译行未能与主歌词时间对齐，已忽略翻译轨")
    doc.notes = list(dict.fromkeys(notes))
    return doc
