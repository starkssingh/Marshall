"""The vocabulary of pre-registered slices (ROB-006; checked at registration, C-24, ADR 0055).

A hypothesis declares the slices its results will be broken down by in its ``slices`` field,
before any result exists. The names are checked when the hypothesis is **registered**, so a typo
is refused before the hypothesis is locked rather than surfacing at report time. They are checked
again when a registered version's slices are loaded (`xq.robustness.slicing`). The vocabulary lives
here, in the tracking layer, so that registration can use it without importing the robustness
code that computes the slices.

Names are compared in lower case, with spaces, hyphens and slashes read as underscores:

- ``year``: the calendar year of the trading day;
- ``volatility_tercile`` (also ``volatility tercile``, ``vol_tercile``, ``volatility``): low, mid
  or high daily sigma-hat, cut at quantiles of the sliced period itself. The grouping is fixed
  only after the period is over, so its tables are labelled **"descriptive, cut ex post"**;
- ``session``: the session a trade was entered in;
- any name containing ``regime``: a legitimate declaration, refused only when computed until a
  causal regime model exists (REG-007).

Every slice is descriptive: reported, not tested (no p-values, no trials).
"""

from __future__ import annotations

import re

YEAR = "year"
VOLATILITY = "volatility_tercile"
SESSION = "session"
VOCABULARY = (YEAR, VOLATILITY, SESSION)
_ALIASES = {"vol_tercile": VOLATILITY, "volatility": VOLATILITY}
#: How each slice's table is labelled in a report.
LABELS = {
    YEAR: "descriptive",
    VOLATILITY: "descriptive, cut ex post",
    SESSION: "descriptive",
}


class SliceError(ValueError):
    """A declared slice that is unknown or cannot be computed yet."""


def canonical_slice(name: str) -> str:
    """The vocabulary name of a declared slice.

    Raises:
        SliceError: for a name outside the vocabulary (a regime slice is kept: it is declared
            legitimately and refused only when computed, until REG-007).
    """
    key = re.sub(r"[\s\-/]+", "_", name.strip().lower())
    key = _ALIASES.get(key, key)
    if key not in VOCABULARY and not is_regime_slice(key):
        raise SliceError(f"unknown slice {name!r}; the vocabulary is {list(VOCABULARY)}")
    return key


def is_regime_slice(name: str) -> bool:
    """Whether a (canonical) slice needs a regime model."""
    return "regime" in name


def slice_label(name: str) -> str:
    """How a (canonical) slice's table is labelled: every slice is descriptive, and volatility
    terciles are also cut ex post."""
    return LABELS.get(name, "descriptive")
