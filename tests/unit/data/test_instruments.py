"""DATA-001: instrument and venue specification, lot maths."""

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from xq.core.config import InstrumentSpec, load_config
from xq.core.errors import ConfigError

REPO_CONFIG = Path(__file__).resolve().parents[3] / "config"

TERMS: dict[str, Any] = {
    "symbol": "XAUUSD",
    "base_ccy": "XAU",
    "quote_ccy": "USD",
    "tick_size": "0.01",
    "contract_size": "100",
    "lot_step": "0.01",
    "min_lot": "0.01",
    "max_lot": "100",
}


@pytest.fixture
def spec() -> InstrumentSpec:
    return InstrumentSpec.model_validate(TERMS)


def test_repository_instrument_config_validates() -> None:
    xauusd = load_config("research", config_dir=REPO_CONFIG).instrument("xauusd")
    assert xauusd.symbol == "XAUUSD"
    assert xauusd.tick_size == Decimal("0.01")
    assert xauusd.contract_size == Decimal("100")
    assert xauusd.rollover.tz == "America/New_York"


def test_unknown_instrument_is_a_config_error() -> None:
    cfg = load_config("research", config_dir=REPO_CONFIG)
    with pytest.raises(ConfigError, match="unknown instrument 'eurusd'; configured: xauusd"):
        cfg.instrument("eurusd")


@pytest.mark.parametrize(
    ("requested", "expected"),
    [
        ("0.157", "0.15"),  # always rounds down
        ("0.01", "0.01"),
        ("0.009", "0"),  # below the minimum lot: cannot trade
        ("0", "0"),
        ("2.5", "2.50"),
        ("1000", "100"),  # capped at the maximum lot
        (0.3, "0.30"),  # floats are read through their decimal repr, not binary expansion
    ],
)
def test_round_lots(spec: InstrumentSpec, requested: str | float, expected: str) -> None:
    assert spec.round_lots(requested) == Decimal(expected)


def test_round_lots_rejects_negative_sizes(spec: InstrumentSpec) -> None:
    with pytest.raises(ValueError, match="non-negative"):
        spec.round_lots("-0.1")


def test_value_and_notional(spec: InstrumentSpec) -> None:
    # 0.10 lot of 100 oz = 10 oz: a $1.00 move in gold is $10 of P&L.
    assert spec.value_per_price_unit(Decimal("0.10")) == Decimal("10.00")
    assert spec.notional(Decimal("2350.25"), Decimal("0.10")) == Decimal("23502.5000")
    assert spec.price_to_ticks(Decimal("1.37")) == Decimal("137")


def test_venue_override_changes_only_listed_terms(spec: InstrumentSpec) -> None:
    with_venue = InstrumentSpec.model_validate(
        {**TERMS, "venues": {"small_lots": {"contract_size": "10", "max_lot": "50"}}}
    )
    venue = with_venue.for_venue("small_lots")
    assert venue.contract_size == Decimal("10")
    assert venue.max_lot == Decimal("50")
    assert venue.tick_size == spec.tick_size
    assert with_venue.for_venue("other") is with_venue
    assert with_venue.for_venue(None) is with_venue


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"tick_size": "0"}, "tick_size must be positive"),
        ({"min_lot": "200"}, "min_lot must not exceed max_lot"),
        ({"min_lot": "0.015"}, "min_lot must be a multiple of lot_step"),
        ({"surprise": 1}, "surprise"),
    ],
)
def test_invalid_terms_are_rejected(changes: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        InstrumentSpec.model_validate({**TERMS, **changes})


def test_instrument_terms_follow_config_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XQ_INSTRUMENTS__XAUUSD__MAX_LOT", "50")
    cfg = load_config("research", {"instruments.xauusd.min_lot": "0.1"}, config_dir=REPO_CONFIG)
    assert cfg.instrument("xauusd").max_lot == Decimal("50")
    assert cfg.instrument("xauusd").min_lot == Decimal("0.1")
