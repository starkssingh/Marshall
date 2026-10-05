# ADR 0066 — Sprint 7, part 2: the feature library (FEAT-001 … FEAT-006, FEAT-008)

- **Status:** accepted (build-only; readings flagged for the owner's review: C-33)
- **Date:** 2026-10-05
- **Decided by:** Claude, within the plan (Phase 8, Sprint 7) and the owner's instruction for
  this session: FEAT-001 (spec, registry, the leakage harness auto-applied to every registered
  feature so an unchecked feature fails CI), FEAT-002 … FEAT-006 and FEAT-008; no global
  scalers; RSI, MACD and ATR checked against hand computations; no feature selection or importance
  on any data; synthetic data only
- **Tasks:** FEAT-001, FEAT-002, FEAT-003, FEAT-004, FEAT-005, FEAT-006, FEAT-008

Synthetic data only. No feature set has been materialized on real data, nothing was selected,
ranked or tuned on data, and nothing here is evidence. FEAT-007 (gated on DATA-011), FEAT-009
(external series) and FEAT-010 (diagnostics, Sprint 8) are not part of this session.

## FEAT-001 — the framework

1. **A feature** (`xq.features.base`) is a registered, versioned, pure function
   `compute(bars, params, context) -> DataFrame` of the complete bars of one timeframe: one row
   per bar indexed by the bar's `available_at`, using that bar, earlier bars and the calendar
   only. `register_feature(name, version, family, params, lookback, warmup)` validates the name
   (lower snake case, never `tgt_`/`fwd_`), the code version (≥ 1) and the family (`price`,
   `momentum`, `volatility`, `structure`, `time`) and returns an immutable `Feature`;
   `lookback` and `warmup` are functions of the parameters. Parameter models are frozen pydantic
   models without unknown keys and without research defaults: **every parameter value lives in
   `config/features.yaml`**.
2. **A `FeatureSpec`** is one configured use: the plan's
   `FeatureSpec(name, version, family, timeframe, params, lookback, warmup, inputs)` plus its
   output column (default `<name>_<parameter values>`; a multi-column feature adds `_<sub>`).
3. **The registry** (`xq.features.registry.FEATURES`) is a static `MappingProxyType` built from
   the family modules' tuples (no module-level mutable state; a duplicate name is refused).
4. **Feature sets** are named and versioned in `config/features.yaml` (`FeatureSetConfig`:
   `description`, `base_columns`, `features`). `compute_feature_set(specs, inputs, context)`
   computes a set from the dataset builder's input frames; `resolve_feature_set(cfg, ref)` gives
   the builder a `FeatureSetDef` for a configured set as for the built-in `base.v1` (the vault
   evaluation computes features the same way).
   - With `base_columns: true` a set also carries `base.v1`'s columns (decision bar, context bars,
     calendar), so the board's signal bars (`ctx_<tf>_*`) stay available.
   - **Identity.** A configured set's definition, the framework's code version and every used
     feature's code version are hashed into the dataset's config digest and its code versions
     (`features:<set>`, `feature:<name>`), and **locked** in the new `feature_sets` table
     (migration 0017, as `target_sets`) on first use: a changed definition under the same version
     is refused (`FeatureSetChangedError`).
5. **The leakage harness, auto-applied** (`tests/leakage/test_all_features.py`): every spec of
   every configured feature set is computed alone through `compute_feature_set` (the builder's
   own path, joins included) on eight synthetic weeks of 15m bars with 1h, 4h and 1d context
   bars, must produce finite values, and must pass truncation invariance, future perturbation and
   the availability audit. `unchecked_features(cfg)` lists registered features that no
   configured set uses, and the suite fails on any: **a feature cannot be registered without
   being checked.** A planted leak (the next bar's close) is caught on the base timeframe and as
   a multi-timeframe feature, and an unused registered feature is reported.
6. **Normalization** (the plan's rule): no global scalers. Features are scaled by trailing
   statistics only (`bar_sigma`, the timeframe's EWMA volatility of one-bar log returns known at
   the bar; `trailing_zscore`), or at model time by a `TrainingFoldScaler` fitted on one training
   fold's rows and applied to the others (tested: test rows never move the scale).

## FEAT-002 — price structure (`xq.features.price`)

`log_return` (1, 2, 4, 8, 16, 32, 64 bars), `range_sigma` (the bar's log range), `candle` (body,
upper and lower wick ratios of the range; missing for a bar without range), `gap` (the previous
close to this open after a break — the daily break and rollover, a weekend, missing bars — in the
previous bar's sigma; 0 on a contiguous bar), `extreme_distance` (to the rolling high and low in
sigma units and the position in the range, 96 bars), `session_vwap` (to the trading day's VWAP
so far, typical price weighted by tick count) and `ema_distance` (EMA 20 and 100).

- **Sigma units** divide by `bar_sigma`: the zero-mean EWMA volatility of the timeframe's own
  one-bar log returns, span 96 on 15m bars (one trading day, as `fwd_returns.v1`'s interim
  sigma-hat), defined from the 96th return. It is a trailing statistic, not a fitted scaler.
- **Tick-volume caveat:** the VWAP weights are tick counts, a measure of quote activity on one
  feed, not traded volume. FEAT-007's admission rule (cross-feed stability, DATA-011) applies
  before a model may rely on the weighting.
- **Known truth** (`tests/unit/features/test_price.py`): every feature against a hand
  computation (EWMA sigma by its weights; the gap after the 21:00 UTC close; the VWAP's tick
  weights and its reset at the trading-day roll).

## FEAT-003 — momentum (`xq.features.momentum`)

`roc` (16 bars), `rsi` (Wilder, 14), `macd_hist` (12, 26, 9, in sigma units of the price),
`ma_slope_t` (the t-statistic of the least-squares slope of the log close, 20 and 96 bars) and
`sign_agreement` (the mean sign of the 1, 4, 16 and 64-bar log returns).

- **RSI:** the first averages are the simple means of the first `window` changes, then Wilder's
  recursion; 100 with only gains, 50 without movement.
- **MACD:** recursive EMAs (`a = 2 / (n + 1)`) from the first close, the MACD known once `slow`
  closes exist, the signal line an EMA of the MACD over `signal` values; the histogram is divided
  by `close * bar_sigma` so it is comparable across price levels.
- **Slope t-statistic:** a window whose residual sum of squares is below 1e-12 of its total (a
  perfect line, up to rounding) has no t-statistic.
- **Known truth** (`tests/unit/features/test_momentum.py`): RSI and MACD step by step by hand;
  the slope t-statistic equal to SciPy's `linregress` slope over its standard error.

## FEAT-004 — volatility (`xq.features.volatility`)

`range_vol` (each of VOL-001's five trailing estimators, window 20, as `config/volatility.yaml`),
`atr` (Wilder, 14, relative to the close — never dollars), `vol_ratio` (16 over 96 bars),
`vol_of_vol` (the 96-bar standard deviation of the log of the 16-bar volatility), `ewma_sigma`
(`bar_sigma`, span 96: the interim sigma-hat on the base bars) and `range_expansion` (the log
range over the mean of the 20 bars before it).

- **Sigma-hat from VOL-006 fitted per fold is not a dataset column.** A fitted forecaster's
  value depends on its training fold, so it is produced at model time inside each fold by
  `serve_sigma` (Sprint 6) and never stored with the features; storing it would be a global
  fit. ML-002 wires it in-fold.
- VOL-001's estimators refuse bars whose high and low do not bracket the open and close; the
  features read `high = max(open, high, close)` and `low = min(open, low, close)` (`consistent`),
  which leaves every real bar unchanged and keeps the harness's column-wise perturbations of
  future bars from raising.
- **Known truth** (`tests/unit/features/test_volatility.py`): Wilder's ATR step by step by hand
  (true ranges 2, 2, 1, 3; ATR 5/3 then 19/9), Parkinson and close-to-close by hand, the ratio,
  the volatility of volatility and the range expansion against direct computations.

## FEAT-006 — time and event proximity (`xq.features.time`)

`time_of_day` (sin and cos of the New York clock time on a 24-hour circle), `day_of_week`
(one-hot of the trading day's weekday, 17:00 New York roll), `session` (one-hot flags of every
configured session and the London–New York overlap) and `event_minutes` (minutes to the next and
since the last LBMA AM, LBMA PM, US data release and rollover, capped at 1,440).

- Functions of the decision time and `config/sessions.yaml` only, read from the same per-day
  session table as DS-007's calendar columns; known in advance and DST-correct by construction.
- A next or last occurrence beyond the cap, or beyond the calendar's seven-day lookup (a Friday
  evening's next release), reads as the cap, so the value never depends on how far the data
  extends.
- **Known truth** (`tests/unit/features/test_time.py`): the session flags equal the session
  table's `open <= t < close` on three days (both zones in winter time, New York only in summer
  time, both in summer time; London opens at 08:00 UTC in March and 07:00 UTC in April); hand
  values for the clock circle, the trading day's weekday across the roll and the minutes to and
  since events.

## FEAT-005 — market structure (`xq.features.structure`)

`breakout` (the close beyond the previous 20 and 96 bars' high and low channel, in sigma units;
the current bar is excluded from the channel), `efficiency_ratio` (Kaufman, 20), `adx` (Wilder,
14, with +DI and -DI), `swing` (the latest confirmed swing high and low, strength 3: distances in
sigma units and ages in bars), `mean_reversion_z` (the trailing z-score of the close, 20 and 96),
`compression` (the percentile rank of the 20-bar range among its last 96 values), `prior_day` and
`prior_session` (Tokyo, London, New York: the latest *completed* occurrence's high and low) and
`round_distance` (to the nearest multiple of 10, 50 and 100 USD, in sigma units — a price level,
not a threshold).

- **Swing confirmation lag.** Bar i is a swing high when its high is strictly above those of the
  `strength` bars on each side; that is known only once those later bars have closed, so the swing
  is confirmed at bar i + strength and used from that bar's availability on. It is computed on a
  *trailing* window of 2 · strength + 1 bars ending at the confirming bar (never a centered
  window); a tie is no swing.
- **ADX:** the true range, +DM and -DM are smoothed by Wilder's average seeded with the mean of
  their first `window` values from the second bar; ADX is Wilder's average of DX, defined from bar
  2 · window − 1. The spec's warm-up is the directional indicators' (window + 1); ADX's own
  starts later.
- **Prior levels** use only completed periods: a trading day's high and low from its last bar on,
  a session occurrence's (bars starting inside it on the same trading day) once a later bar
  outside it exists — computing on data cut inside a session gives the same values.
- **Known truth** (`tests/unit/features/test_structure.py`): the swing high of bar 2 appears on
  bar 4 with age 2 and is unknown on data cut at bars 3 and 4's predecessors; a newer swing
  replaces an older one; ties are no swing; ADX, +DI and -DI step by step by hand; the breakout,
  efficiency ratio, z-score, compression rank, prior day (across the 21:00 UTC roll), prior
  London session (unknown while it runs) and round-number distance by hand.

## FEAT-008 — multi-timeframe context (`xq.features.mtf`)

Any registered feature may run on the bars of a context timeframe (`timeframe:` on an instance).
It is computed on that timeframe's complete bars exactly as on the base bars and joined onto the
base decision times by `asof_join` on `available_at` (`join_on_availability`): each decision reads
the latest context bar available at or before it, never the bar that contains it and never by bar
start. Columns are `mtf_<tf>_<column>`, with one provenance column `mtf_<tf>_available_at` per
timeframe, which the harness audits.

- `core.v1` carries, on 1h and 4h bars, the one-bar log return, RSI 14, ATR 14, the 20-bar slope
  t-statistic and the 20-bar extreme distances, with the efficiency ratio on 1h and ADX on 4h;
  on 1d bars, the daily return, RSI 14, ATR 14, Yang-Zhang volatility over 20 days (it carries
  the overnight gaps), the 20-day extreme distances and the 20-day z-score. Sigma units on a
  context timeframe use a span of 20 of its bars. The 15m base bars are the default timeframe.
- A dataset must load every timeframe its feature set uses (`context_timeframes`); otherwise the
  build stops with a `ConfigError` naming it. A 1d feature with a 20-day window is missing for the
  first weeks after a dataset's warm-up when the warm-up is shorter than that (ds_base's is 10
  days); nothing is filled.
- **Known truth** (`tests/unit/features/test_mtf.py`): with hourly bars published five minutes
  after they end, the 11:00 decision still reads the 09:00 bar and the 11:15 decision the 10:00
  bar; every switch happens at the first decision at or after an availability; changing bars not
  yet published leaves every earlier value unchanged; a daily bar is read only from the 21:00 UTC
  roll (17:00 New York, EDT) on.
