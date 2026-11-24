"""Chinese: split by character (按字).

Each Chinese character is one syllable and one tap.

Rules:

* every Chinese character is its own unit;
* a Latin run stays a whole word ("baby" is not b/a/b/y);
* digits stay with their run, so "2024" is one unit rather than four;
* a kana run is grouped by the Japanese script rule (a Chinese line that quotes
  Japanese should still be tappable);
* punctuation and symbols are each their own unit.
"""

from .common import CJK_RE, WORD_RE, group_kana_runs

LABEL = "中文 · 按字"


def regroup(units):
    """One unit per Chinese character; everything else keeps the shared rules."""
    out = []
    for unit in units:
        # A first-pass unit is either a whole Latin/digit run or one character.
        # Only the multi-character non-Latin case is split here; the script
        # check keeps a Latin word whole.
        if len(unit) > 1 and not _is_latin_run(unit) and CJK_RE.match(unit[0]):
            out.extend(list(unit))
        else:
            out.append(unit)
    return group_kana_runs(out)


def _is_latin_run(unit):
    return all(WORD_RE.match(char) for char in unit)
