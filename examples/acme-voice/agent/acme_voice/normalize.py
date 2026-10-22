"""Turn what the ASR wrote into what a customer meant, for the acme brain.

Speech recognizers write spoken identifiers their own way: "92,000" for the postcode 92000,
"See 1042" or "C1,188" for customer C-1042 or C-1188. The acme brain reads text as typed in a
chat, so the voice agent rewrites those forms first, as production voice agents do between ASR
and the language layer. Only number formatting and the customer-id letter are touched; words
are never guessed.
"""

import re
from typing import Final

_THOUSANDS: Final = re.compile(r"(?<=\d),(?=\d{3}\b)")
_CUSTOMER: Final = re.compile(r"\b(?:c|see|sea)[\s-]*(\d{4})\b", re.IGNORECASE)


def normalize_transcript(text: str) -> str:
    """``text`` with digit groups joined and spoken customer ids written as ``C-1234``."""
    joined = _THOUSANDS.sub("", text.strip())
    return _CUSTOMER.sub(lambda match: f"C-{match.group(1)}", joined)
