"""Seeding for reproducible randomness (ARCH-005).

All randomness in xq flows from explicit seeds: `make_rng` builds independent NumPy generators,
`derive_seed` gives stable per-fold or per-trial seeds, and `set_global_seed` pins the global state
of libraries that do not accept a generator.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import random

import numpy as np

_SEED_BITS = 32


def set_global_seed(seed: int) -> None:
    """Seed Python's `random`, NumPy's legacy global state and, if installed, PyTorch."""
    _check_seed(seed)
    random.seed(seed)
    np.random.seed(seed)  # noqa: NPY002 - pinning the legacy global state is the point here
    if importlib.util.find_spec("torch") is not None:  # pragma: no cover - torch is optional
        torch = importlib.import_module("torch")
        torch.manual_seed(seed)


def make_rng(seed: int) -> np.random.Generator:
    """Return an independent NumPy generator seeded with `seed`."""
    _check_seed(seed)
    return np.random.default_rng(seed)


def derive_seed(base_seed: int, *keys: str | int) -> int:
    """Derive a stable 32-bit seed from `base_seed` and `keys` (e.g. a fold id).

    Uses SHA-256 rather than `hash()`, so the result is identical across processes, platforms and
    Python versions.
    """
    _check_seed(base_seed)
    material = ":".join([str(base_seed), *(f"{type(k).__name__}={k}" for k in keys)])
    digest = hashlib.sha256(material.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % (1 << _SEED_BITS)


def _check_seed(seed: int) -> None:
    if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed < (1 << _SEED_BITS):
        raise ValueError(f"seed must be an int in [0, 2**{_SEED_BITS}), got {seed!r}")
