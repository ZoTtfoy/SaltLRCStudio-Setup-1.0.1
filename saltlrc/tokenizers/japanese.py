"""Japanese: split by pronunciation (発音) unit.

漢字 followed by its 送りがな is one pronunciation, e.g. 綺麗(きれい)だね. The
rules:

* a kanji run absorbs up to OKURIGANA_MAX trailing hiragana as its okurigana, and
  is itself capped at KANJI_RUN_MAX kanji;
* a kana run (hiragana or katakana) stays whole up to KANA_WHOLE_MAX mora and is
  then cut every KANA_CHUNK;
* Latin runs stay whole words;
* anything else (punctuation, symbols) is its own unit.

Grouping is by what is *written*; the kanji reading is not resolved.
"""

from .common import (HIRAGANA_RE, KANA_CHUNK, KANA_WHOLE_MAX, KANJI_RE,
                     KANJI_RUN_MAX, KATAKANA_RE, OKURIGANA_MAX, chunks)

LABEL = "日语 · 按发音"


def _kanji_group_end(units, index):
    """Index just past the kanji run at ``index`` plus its okurigana.

    Okurigana is absorbed only when the trailing kana run fits **entirely** within
    OKURIGANA_MAX. When the run is longer than the limit it is not okurigana, so
    the kanji stands alone and the kana run is grouped by the kana rule
    (一生懸命 + がんばる).
    """
    end = index
    while end < len(units) and KANJI_RE.fullmatch(units[end]) and end - index < KANJI_RUN_MAX:
        end += 1
    run = end
    while run < len(units) and HIRAGANA_RE.fullmatch(units[run]):
        run += 1
    if 0 < run - end <= OKURIGANA_MAX:
        return run
    return end


def regroup(units):
    """Merge first-pass units into pronunciation groups."""
    out = []
    index = 0
    while index < len(units):
        unit = units[index]
        if KANJI_RE.fullmatch(unit):
            end = _kanji_group_end(units, index)
            out.append("".join(units[index:end]))
            index = end
            continue
        # A kana run is grouped by its own script rule: whole up to
        # KANA_WHOLE_MAX, then cut every KANA_CHUNK.
        match = KATAKANA_RE if KATAKANA_RE.fullmatch(unit) else HIRAGANA_RE
        if match.fullmatch(unit):
            end = index
            while end < len(units) and match.fullmatch(units[end]):
                end += 1
            out.extend(chunks(units[index:end], KANA_WHOLE_MAX, KANA_CHUNK))
            index = end
            continue
        out.append(unit)
        index += 1
    return out
