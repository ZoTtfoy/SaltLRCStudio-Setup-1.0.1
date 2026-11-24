"""Fallback for text whose script does not identify a language.

:func:`saltlrc.language.detect` returns ``und`` for anything it cannot pin down —
a line of symbols, an emoji line, a script none of the modules cover. This module
applies the **same grouping the Japanese module uses**: kanji absorbs okurigana,
kana is grouped, Latin stays a whole word.

The grouping rules live in :mod:`.japanese` and this module calls them.
"""

from . import japanese

LABEL = "未识别 · 混合规则"


def regroup(units):
    """Identical to the Japanese grouping."""
    return japanese.regroup(units)
