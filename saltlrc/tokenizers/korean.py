"""Korean: split by syllable block (按音节).

Hangul is written in syllable blocks; each block is one syllable and one tap, so
the per-character units the first pass produces are already correct and
:func:`regroup` is an identity.

Rules:

* each Hangul syllable block is its own unit;
* a Latin run stays a whole word;
* CJK and kana keep their per-character / script handling.
"""

LABEL = "韩语 · 按音节"


def regroup(units):
    """One unit per Hangul syllable — already what the first pass produced."""
    return list(units)
