"""List API prices, for putting the ledger's tokens into API-equivalent dollars.

Dollars per million tokens: (input, output, cache read). A cache write is
priced from input: 1.25x for the 5-minute cache, 2x for the 1-hour one.
These go stale; `AS_OF` is printed with every dollar figure so a reader can
tell. A model not listed here is counted in tokens and left out of the dollar
totals rather than priced by guess.
"""
from __future__ import annotations

from typing import Optional, Tuple

AS_OF = "2026-06-24"

WRITE_5M = 1.25
WRITE_1H = 2.0

# Longest prefix first, so `claude-fable-5-1` is not priced as `claude-fable-5`.
_TABLE = (
    ("claude-fable-5-1", (10.0, 50.0, 0.25)),
    ("claude-mythos-5-1", (10.0, 50.0, 0.25)),
    ("claude-fable-5", (10.0, 50.0, 1.0)),
    ("claude-opus-5-5", (4.0, 20.0, 0.20)),
    ("claude-opus-5", (5.0, 25.0, 0.50)),
    ("claude-opus-4-8", (5.0, 25.0, 0.50)),
    ("claude-opus-4-7", (5.0, 25.0, 0.50)),
    ("claude-opus-4-6", (5.0, 25.0, 0.50)),
    ("claude-sonnet-5", (2.0, 10.0, 0.20)),
    ("claude-sonnet-4-6", (3.0, 15.0, 0.30)),
    ("claude-haiku-4-5", (1.0, 5.0, 0.10)),
)


def price(model: Optional[str]) -> Optional[Tuple[float, float, float]]:
    """(input, output, cache read) in $/M tokens, or None when unknown."""
    if not model:
        return None
    for prefix, rates in _TABLE:
        if model == prefix or model.startswith(prefix + "-"):
            return rates
    return None
