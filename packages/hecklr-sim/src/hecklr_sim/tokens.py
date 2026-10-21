"""Whole-token matching shared by the consistency guard and replay grounding.

A token edge is any character that is not a letter or digit in any script; underscores and
punctuation separate tokens, so ``C-26`` is not found inside ``C-2675`` but is found in
``id_C-26``. One definition keeps the guard and replay grounding from disagreeing.
"""

import re
from functools import lru_cache

WORD_EDGE_BEFORE = r"(?<![^\W_])"
"""Lookbehind: the previous character is not a letter or digit."""
WORD_EDGE_AFTER = r"(?![^\W_])"
"""Lookahead: the next character is not a letter or digit."""


def mentions(text: str, token: str) -> bool:
    """Whether ``token`` occurs in ``text`` as a whole token, not as part of a longer one."""
    return _token_pattern(token).search(text) is not None


@lru_cache(maxsize=1024)
def _token_pattern(token: str) -> re.Pattern[str]:
    # The guard checks the same few tokens against every draft of a trial.
    return re.compile(rf"{WORD_EDGE_BEFORE}{re.escape(token)}{WORD_EDGE_AFTER}")
