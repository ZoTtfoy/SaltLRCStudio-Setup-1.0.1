"""Shared building blocks for the per-language word splitters.

Every language module gets the same input and must satisfy the same invariant:
**concatenating the units must reproduce the source line exactly, character for
character.** The exporter and the validator both depend on it — a dropped space
once made every English line unexportable — so the splitting rules here only ever
regroup characters that are already there, and the whitespace merge at the end
is the single place allowed to move them around.
"""

import re

# Script ranges. Kept identical to the ones the estimator already used, so the
# displayed units and the per-character weights can never disagree.
HIRAGANA = "\\u3041-\\u309f"
KATAKANA = "\\u30a0-\\u30ff\\uff66-\\uff9f"      # incl. halfwidth katakana
KANJI = "\\u3400-\\u9fff\\uf900-\\ufaff"
HANGUL = "\\uac00-\\ud7af"

HIRAGANA_RE = re.compile("[" + HIRAGANA + "]")
KATAKANA_RE = re.compile("[" + KATAKANA + "]")
KANJI_RE = re.compile("[" + KANJI + "]")
KANA_RE = re.compile("[" + HIRAGANA + KATAKANA + "]")
HANGUL_RE = re.compile("[" + HANGUL + "]")
CJK_RE = re.compile("[" + HIRAGANA + KATAKANA + KANJI + "]")
# A "word character" for the Latin script: letters, digits and the apostrophe
# that holds contractions together ("don't" must stay one unit).
WORD_RE = re.compile(r"[A-Za-z0-9']")

# Longest kana run kept as a single unit, and the size long runs are cut into.
# Singers breathe in groups of a few mora; one cell per mora would be unusable.
KANA_WHOLE_MAX = 5
KANA_CHUNK = 3

# A kanji run absorbs at most this many trailing kana as okurigana, and at most
# this many kanji before the group is considered too long to be one breath.
OKURIGANA_MAX = 3
KANJI_RUN_MAX = 4


def first_pass(text, per_char_latin=False):
    """Split into raw units: Latin runs become one unit, everything else one
    character per unit.

    Latin runs are words by definition, so they survive intact at this stage for
    every language (``per_char_latin`` forces the old "char" mode instead).
    Everything a language module may want to regroup — kanji, kana, punctuation —
    is delivered one character at a time, which is what makes the regrouping
    rules readable.
    """
    units = []
    buffer = ""
    for char in text:
        if WORD_RE.match(char):
            buffer += char
            continue
        if buffer:
            units.extend(list(buffer) if per_char_latin else [buffer])
            buffer = ""
        units.append(char)
    if buffer:
        units.extend(list(buffer) if per_char_latin else [buffer])
    return units


def chunks(units, whole_max, size):
    """Keep short runs whole; cut long runs into fixed-size groups."""
    if len(units) <= whole_max:
        return ["".join(units)]
    return ["".join(units[i:i + size]) for i in range(0, len(units), size)]


def group_kana_runs(units):
    """Group hiragana and katakana runs into singable lengths, leaving the rest.

    Shared by the Japanese rules and by Chinese/English lines that happen to
    carry kana — the rule is a property of the script, not of the language.
    """
    out = []
    index = 0
    while index < len(units):
        unit = units[index]
        if HIRAGANA_RE.fullmatch(unit) or KATAKANA_RE.fullmatch(unit):
            katakana = bool(KATAKANA_RE.fullmatch(unit))
            match = KATAKANA_RE if katakana else HIRAGANA_RE
            end = index
            while end < len(units) and match.fullmatch(units[end]):
                end += 1
            out.extend(chunks(units[index:end], KANA_WHOLE_MAX, KANA_CHUNK))
            index = end
        else:
            out.append(unit)
            index += 1
    return out


def merge_spaces(units):
    """Attach whitespace to a neighbouring unit.

    Spaces are never a unit of their own — you cannot sing one, and tapping it
    would put a timestamp on nothing. Each space rides along with the unit before
    it (or, for leading whitespace, the unit after it), which is what keeps the
    concatenation equal to the original text.
    """
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
