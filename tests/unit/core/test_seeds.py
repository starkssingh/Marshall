"""ARCH-005: seeded randomness is reproducible within and across processes."""

import random
import subprocess
import sys

import numpy as np
import pytest

from xq.core.seeds import derive_seed, make_rng, set_global_seed

DRAW_SCRIPT = """
import random
import numpy as np
from xq.core.seeds import derive_seed, make_rng, set_global_seed
set_global_seed(1234)
print(random.random(), np.random.random(), make_rng(99).random(), derive_seed(7, "fold", 3))
"""


def test_global_seed_reproduces_draws() -> None:
    set_global_seed(42)
    first = (random.random(), np.random.random())  # noqa: NPY002
    set_global_seed(42)
    second = (random.random(), np.random.random())  # noqa: NPY002
    assert first == second


def test_generators_are_reproducible_and_independent() -> None:
    assert make_rng(5).random(3).tolist() == make_rng(5).random(3).tolist()
    assert make_rng(5).random() != make_rng(6).random()


def test_draws_are_identical_across_processes() -> None:
    def run() -> str:
        result = subprocess.run(
            [sys.executable, "-c", DRAW_SCRIPT], capture_output=True, text=True, check=True
        )
        return result.stdout

    assert run() == run()


def test_derive_seed_is_stable_and_key_sensitive() -> None:
    # Pinned value: derive_seed must never change, or recorded runs stop being reproducible.
    assert derive_seed(7, "fold", 3) == 851735239
    assert derive_seed(7, "fold", 3) != derive_seed(7, "fold", 4)
    assert derive_seed(7, "fold", 3) != derive_seed(7, "fold", "3")
    assert derive_seed(7, "fold", 3) != derive_seed(8, "fold", 3)
    assert 0 <= derive_seed(7, "fold", 3) < 2**32


@pytest.mark.parametrize("bad", [-1, 2**32, 1.5, True])
def test_invalid_seeds_are_rejected(bad: object) -> None:
    with pytest.raises(ValueError, match="seed"):
        make_rng(bad)  # type: ignore[arg-type]
