"""English: split by word (按单词).

One unit per word. The first pass already delivers Latin runs whole, so
:func:`regroup` is an identity here; this module states the rule explicitly:

* a Latin run is one word ("don't" stays together: the apostrophe is part of the
  word, not a break);
* digits stay with their run ("2024" is one unit);
* CJK characters are handled per character, kanji included;
* punctuation and symbols are each their own unit.
"""

LABEL = "英语 · 按单词"


def regroup(units):
    """One unit per word — the first pass already produced exactly that."""
    return list(units)
