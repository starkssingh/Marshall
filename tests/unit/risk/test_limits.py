"""RISK-003: every halt triggers exactly at its threshold and not a hair before; caps bound the
target on hand-computed cases."""

from dataclasses import replace
from datetime import date

from helpers.event_backtest import CFG, INSTRUMENT, ns
from xq.risk.limits import cap_target, entry_halts
from xq.risk.state import MarketState, RiskState

LIMITS = CFG.risk_config().limits
NOW = ns("2024-03-12 15:00")
MINUTE = 60_000_000_000

CALM = RiskState(
    ts=NOW,
    trading_day=date(2024, 3, 12),
    capital=100_000.0,
    equity=100_000.0,
    peak_equity=100_000.0,
    drawdown=0.0,
    worst_drawdown=0.0,
    day_start_equity=100_000.0,
    day_pnl=0.0,
    position_lots=0.0,
    mark=2000.0,
    open_notional=0.0,
    margin_used=0.0,
    consecutive_losses=0,
    last_loss_at=None,
    trades_today=0,
)
MARKET = MarketState(NOW, 1999.9, 2000.1, NOW)


def halts(**changes: object) -> list[str]:
    return entry_halts(replace(CALM, **changes), LIMITS, NOW)  # type: ignore[arg-type]


def test_the_limits_are_the_provisional_profile() -> None:
    assert (LIMITS.max_drawdown, LIMITS.max_daily_loss) == (0.15, 0.03)
    assert (LIMITS.max_consecutive_losses, LIMITS.cooldown_minutes) == (5, 240)
    assert LIMITS.max_trades_per_day == 12
    assert halts() == []


def test_the_drawdown_halt_triggers_exactly_at_its_threshold() -> None:
    assert halts(worst_drawdown=0.1499999) == []
    (reason,) = halts(worst_drawdown=0.15)
    assert reason.startswith("drawdown halt")
    # sticky: the current drawdown has recovered, the worst has not
    assert halts(worst_drawdown=0.15, drawdown=0.0) == [reason]


def test_the_daily_loss_halt_triggers_exactly_at_its_threshold() -> None:
    assert halts(day_pnl=-2_999.99) == []
    (reason,) = halts(day_pnl=-3_000.0)
    assert reason.startswith("daily loss halt")
    assert halts(day_pnl=+50_000.0) == []


def test_the_cooldown_needs_the_loss_count_and_ends_exactly_on_time() -> None:
    last = NOW - 239 * MINUTE
    assert halts(consecutive_losses=4, last_loss_at=last) == []
    (reason,) = halts(consecutive_losses=5, last_loss_at=last)
    assert reason.startswith("cooldown")
    assert halts(consecutive_losses=5, last_loss_at=NOW - 240 * MINUTE) == []
    assert halts(consecutive_losses=5, last_loss_at=NOW - 240 * MINUTE + 1) == [reason]


def test_the_trades_per_day_halt_triggers_at_the_limit() -> None:
    assert halts(trades_today=11) == []
    (reason,) = halts(trades_today=12)
    assert reason.startswith("trades per day")


def test_every_halt_in_force_is_reported() -> None:
    reasons = halts(worst_drawdown=0.2, day_pnl=-5_000.0, trades_today=12)
    assert [r.split(":")[0] for r in reasons] == [
        "drawdown halt",
        "daily loss halt",
        "trades per day",
    ]


def cap(target: float, **kwargs: object) -> tuple[float, list[str]]:
    args: dict[str, object] = {
        "state": CALM,
        "market": MARKET,
        "limits": LIMITS,
        "instrument": INSTRUMENT,
        "margin_rate": 0.05,
        "price": 2000.0,
    }
    args.update(kwargs)
    return cap_target(target, **args)  # type: ignore[arg-type]


def test_a_target_within_every_cap_is_unchanged() -> None:
    assert cap(0.49) == (0.49, [])
    assert cap(-1.25) == (-1.25, [])
    assert cap(0.0) == (0.0, [])


def test_the_notional_cap_binds_and_rounds_down() -> None:
    # 3.0 x 100,000 / (100 oz x 2,000) = 1.5 lots
    assert cap(4.0) == (1.5, ["capped by max_notional"])
    assert cap(-4.0) == (-1.5, ["capped by max_notional"])
    # correlated exposure elsewhere uses up part of the notional: (300,000 - 50,000) / 200,000
    assert cap(4.0, correlated_notional=50_000.0) == (1.25, ["capped by max_notional"])
    # at 2,003 the bound is 1.4977... lots, rounded down to the step
    assert cap(4.0, price=2003.0) == (1.49, ["capped by max_notional"])


def test_the_margin_and_lot_caps_bind() -> None:
    # 0.5 x 100,000 / (200,000 x 0.2) = 1.25 lots of margin before 1.5 of notional
    assert cap(4.0, margin_rate=0.2) == (
        1.25,
        ["capped by max_notional", "capped by max_margin_use"],
    )
    rich = replace(CALM, equity=10_000_000.0)
    assert cap(100.0, state=rich) == (20.0, ["capped by max_lots"])


def test_a_session_cap_binds_only_while_its_session_is_in_force() -> None:
    limits = LIMITS.model_copy(update={"session_max_exposure": {"asia": 1.0}})
    asia = replace(MARKET, sessions=("asia",))
    assert cap(1.2, limits=limits) == (1.2, [])
    assert cap(1.2, limits=limits, market=asia) == (0.5, ["capped by session asia"])


def test_no_equity_means_no_exposure() -> None:
    broke = replace(CALM, equity=-10.0)
    assert cap(1.0, state=broke) == (0.0, ["capped by max_notional"])
