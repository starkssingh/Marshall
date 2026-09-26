"""DATA-007: non-destructive cleaning rules flag exactly the injected defects."""

from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError

from helpers.ticks import (
    SECOND_NS,
    crossed,
    dense_ticks,
    exact_duplicate,
    flagged_ids,
    frozen_quote,
    inject,
    non_positive,
    price_spike,
    row_like,
    same_time_other_price,
    wide_spread,
)
from xq.core.config import CleaningConfig, load_config
from xq.core.time import to_ns
from xq.data.clean import CLEANING_FLAGS, RULES, MarketWindow, clean_ticks, rules_version
from xq.data.flags import TickFlag

REPO_CONFIG = Path(__file__).resolve().parents[3] / "config"
# Trading day Monday 2024-03-11 (EDT): market 2024-03-10 22:00 UTC to 2024-03-11 21:00 UTC.
MARKET = MarketWindow(
    to_ns(pd.Timestamp("2024-03-10 22:00", tz="UTC")),
    to_ns(pd.Timestamp("2024-03-11 21:00", tz="UTC")),
)


@pytest.fixture(scope="module")
def cfg() -> CleaningConfig:
    return load_config("research", config_dir=REPO_CONFIG).cleaning_config()


@pytest.fixture(scope="module")
def base() -> pd.DataFrame:
    return dense_ticks("2024-03-11 00:00", "2024-03-11 06:00", seed=11)


def with_drop(cfg: CleaningConfig, *rules: str, log_flags: bool = True) -> CleaningConfig:
    return CleaningConfig.model_validate(
        {**cfg.model_dump(), "drop": list(rules), "log_flag_actions": log_flags}
    )


def assert_flagged_exactly(frame: pd.DataFrame, expected: dict[str, set[int]]) -> None:
    for name, rule in RULES.items():
        assert flagged_ids(frame, rule.flag) == expected.get(name, set()), name


def all_defects(base: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, set[int]]]:
    frame, stale = frozen_quote(base, 3000)
    last = len(frame) - 1
    after_close = row_like(
        frame,
        last,
        ts_utc=MARKET.close_ns + 30 * 60 * SECOND_NS,
        bid=frame["bid"].iloc[last] + 0.02,
    )
    injected = {
        "DUP_EXACT": exact_duplicate(frame, 400),
        "DUP_TS_DIFF_PRICE": same_time_other_price(frame, 600),
        "NONPOSITIVE": non_positive(frame, 800),
        "CROSSED": crossed(frame, 1000),
        "SPREAD_OUTLIER": wide_spread(frame, 1200),
        "SPIKE": price_spike(frame, 1400),
        "CLOSED_MARKET": after_close,
    }
    frame, ids = inject(frame, list(injected.values()))
    expected = {name: {rid} for name, rid in zip(injected, ids, strict=True)}
    expected["STALE"] = set(stale)
    return frame, expected


def test_clean_fixture_gets_no_flags(base: pd.DataFrame, cfg: CleaningConfig) -> None:
    outcome = clean_ticks(base, cfg, MARKET)
    assert outcome.flagged == 0
    assert outcome.dropped == 0
    assert outcome.actions.empty
    pd.testing.assert_frame_equal(outcome.ticks, base)


@pytest.mark.parametrize(
    ("rule", "make"),
    [
        ("DUP_EXACT", exact_duplicate),
        ("DUP_TS_DIFF_PRICE", same_time_other_price),
        ("NONPOSITIVE", non_positive),
        ("CROSSED", crossed),
        ("SPREAD_OUTLIER", wide_spread),
        ("SPIKE", price_spike),
    ],
)
def test_each_injected_defect_is_flagged_exactly(
    base: pd.DataFrame, cfg: CleaningConfig, rule: str, make: object
) -> None:
    frame, ids = inject(base, [make(base, 1500)])  # type: ignore[operator]
    outcome = clean_ticks(frame, cfg, MARKET)
    assert_flagged_exactly(outcome.ticks, {rule: set(ids)})
    assert outcome.flagged == 1


def test_stale_quote_is_flagged_after_the_limit_only(
    base: pd.DataFrame, cfg: CleaningConfig
) -> None:
    frame, stale = frozen_quote(base, 1500, seconds=300, every=10)
    outcome = clean_ticks(frame, cfg, MARKET)
    assert len(stale) == 18  # repeats at 130 s ... 300 s
    assert_flagged_exactly(outcome.ticks, {"STALE": set(stale)})


def test_closed_market(base: pd.DataFrame, cfg: CleaningConfig) -> None:
    last = len(base) - 1
    # A new quote exactly at the close (outside the half-open market hours).
    late = row_like(base, last, ts_utc=MARKET.close_ns, bid=base["bid"].iloc[last] + 0.02)
    frame, ids = inject(base, [late])
    assert_flagged_exactly(clean_ticks(frame, cfg, MARKET).ticks, {"CLOSED_MARKET": set(ids)})
    everything = set(base["row_num"])
    closed_day = clean_ticks(base, cfg, MarketWindow(None, None)).ticks
    assert_flagged_exactly(closed_day, {"CLOSED_MARKET": everything})


def test_all_defects_together_are_flagged_exactly(base: pd.DataFrame, cfg: CleaningConfig) -> None:
    frame, expected = all_defects(base)
    outcome = clean_ticks(frame, cfg, MARKET)
    assert_flagged_exactly(outcome.ticks, expected)
    assert outcome.flagged == sum(len(ids) for ids in expected.values())
    assert len(outcome.actions) == outcome.flagged
    assert set(outcome.actions["action"]) == {"flag"}


def test_values_are_never_modified(base: pd.DataFrame, cfg: CleaningConfig) -> None:
    frame, _ = all_defects(base)
    kept = clean_ticks(frame, cfg, MARKET).ticks
    columns = ["ts_utc", "bid", "ask", "bid_size", "ask_size", "raw_file_id", "row_num"]
    pd.testing.assert_frame_equal(kept[columns], frame[columns])
    non_cleaning = kept["flags"].to_numpy() & ~np.uint32(CLEANING_FLAGS)
    np.testing.assert_array_equal(non_cleaning, frame["flags"].to_numpy())


def test_drop_removes_only_configured_rules_and_logs_originals(
    base: pd.DataFrame, cfg: CleaningConfig
) -> None:
    frame, expected = all_defects(base)
    outcome = clean_ticks(frame, with_drop(cfg, "DUP_EXACT", "CROSSED"), MARKET)
    gone = expected["DUP_EXACT"] | expected["CROSSED"]
    assert outcome.dropped == 2
    assert not gone & set(outcome.ticks["row_num"])
    assert len(outcome.ticks) == len(frame) - 2

    drops = outcome.actions[outcome.actions["action"] == "drop"]
    assert set(drops["rule_id"]) == {"DUP_EXACT", "CROSSED"}
    for action in drops.to_dict("records"):
        original = action["original_values"]
        source_row = frame.loc[frame["row_num"] == original["row_num"]].iloc[0]
        assert original["bid"] == source_row["bid"]
        assert original["ask"] == source_row["ask"]
        assert original["raw_file_id"] == "synthetic"
        assert action["ts_utc"] == source_row["ts_utc"]


def test_flag_logging_can_be_limited_to_drops(base: pd.DataFrame, cfg: CleaningConfig) -> None:
    frame, _ = all_defects(base)
    outcome = clean_ticks(frame, with_drop(cfg, "DUP_EXACT", log_flags=False), MARKET)
    assert list(outcome.actions["action"]) == ["drop"]
    assert outcome.flagged > 0  # flags are still set on the ticks


def test_actions_carry_rule_details(base: pd.DataFrame, cfg: CleaningConfig) -> None:
    frame, _ = all_defects(base)
    actions = clean_ticks(frame, cfg, MARKET).actions
    by_rule = {
        rule: group["original_values"].tolist() for rule, group in actions.groupby("rule_id")
    }
    (spike,) = by_rule["SPIKE"]
    assert spike["z"] > cfg.spike.z_threshold
    (spread,) = by_rule["SPREAD_OUTLIER"]
    assert spread["spread"] > cfg.spread_outlier.multiple * spread["trailing_median_spread"]
    assert all(v["unchanged_seconds"] > cfg.stale.seconds for v in by_rule["STALE"])


def test_result_does_not_depend_on_input_order(base: pd.DataFrame, cfg: CleaningConfig) -> None:
    frame, _ = all_defects(base)
    shuffled = frame.sample(frac=1.0, random_state=3)
    pd.testing.assert_frame_equal(
        clean_ticks(shuffled, cfg, MARKET).ticks, clean_ticks(frame, cfg, MARKET).ticks
    )


def test_causal_rules_are_truncation_invariant(base: pd.DataFrame, cfg: CleaningConfig) -> None:
    frame, _ = all_defects(base)
    full = clean_ticks(frame, cfg, MARKET).ticks["flags"].to_numpy()
    causal = np.uint32(sum(rule.flag for rule in RULES.values() if rule.causal))
    for cut in (401, 1001, 1201, 1402, 2500, 3100, len(frame) - 1):
        prefix = clean_ticks(frame.iloc[:cut], cfg, MARKET).ticks["flags"].to_numpy()
        np.testing.assert_array_equal(prefix & causal, full[:cut] & causal, err_msg=f"cut={cut}")


def test_spike_needs_later_ticks(base: pd.DataFrame, cfg: CleaningConfig) -> None:
    frame, ids = inject(base, [price_spike(base, 1500)])
    position = int(np.flatnonzero(frame["row_num"].to_numpy() == ids[0])[0])
    before_reversal = clean_ticks(frame.iloc[: position + 1], cfg, MARKET).ticks
    assert not flagged_ids(before_reversal, TickFlag.SPIKE)
    assert flagged_ids(clean_ticks(frame, cfg, MARKET).ticks, TickFlag.SPIKE) == set(ids)
    assert not RULES["SPIKE"].causal


def test_missing_quotes_are_not_judged_by_price_rules(
    base: pd.DataFrame, cfg: CleaningConfig
) -> None:
    missing = row_like(base, 1500, bid=np.nan, flags=int(TickFlag.MISSING_QUOTE))
    missing["ts_utc"] = int(base["ts_utc"].iloc[1500]) + 1
    frame, _ = inject(base, [missing])
    assert_flagged_exactly(clean_ticks(frame, cfg, MARKET).ticks, {})


def test_only_duplicates_non_positive_and_crossed_may_be_dropped(cfg: CleaningConfig) -> None:
    with pytest.raises(ValidationError, match="DUP_EXACT"):
        with_drop(cfg, "SPIKE")


def test_rules_version_tracks_parameters(cfg: CleaningConfig) -> None:
    version = rules_version(cfg)
    assert version.startswith(f"{cfg.version}-")
    assert rules_version(CleaningConfig.model_validate(cfg.model_dump())) == version
    changed = cfg.model_dump()
    changed["spike"]["z_threshold"] = 9.0
    assert rules_version(CleaningConfig.model_validate(changed)) != version
