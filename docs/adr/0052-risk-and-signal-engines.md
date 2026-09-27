# ADR 0052 — Risk and signal engines (Sprint 12 A)

- **Status:** accepted
- **Date:** 2026-09-27
- **Decided by:** Claude, within the plan (section 6, Phases 14 and 15) and the owner's Sprint 12 A
  instructions (ADR 0051); open points are flagged for the owner's review
- **Tasks:** RISK-001 … RISK-006, SIGNAL-001 … SIGNAL-005

## RISK-001 — risk state

1. **Derived from the account only.** Equity, peak equity, the drawdown `(peak − equity) / peak`
   and the worst drawdown since the start or the last manual reset; the trading day (17:00 New
   York roll), its starting equity (the previous day's closing equity, else the first
   observation) and P&L; the position, its notional and margin; consecutive losing round trips
   (flat to flat or to a flip; realized price P&L net of commissions — financing is not included)
   and when the last one closed; entries filled in the trading day.
2. **Observed at decisions and day ends.** Equity is observed at every risk decision and at every
   trading day's end, not at every quote, so peak equity and drawdown are those of the observed
   equity. The engine records each observation as an `account` row in the decision ledger, next to
   the `fill` rows.
3. **Reconstructable from the ledger.** `rebuild_risk_state` replays the ledger's `account` and
   `fill` rows through the same tracker; a test shows the rebuilt state equals the live state at
   every decision of engine runs (the plan's "rebuild equals live state").
4. **Market state.** `MarketState` holds what the engine knows about the market at a decision: the
   latest quote and its age, the daily sigma-hat, a reference spread, the sessions in force and
   the kill switch's reason. The engine assembles it, so the risk engine stays pure.

## RISK-002 — sizing, and the risk profile

1. **Risk profiles** live in `config/risk/<profile>.yaml` (the plan's `config/risk/*.yaml`),
   chosen by `backtest.risk_profile` (`default`), and carry a `version` recorded on every decision
   with a hash of the profile. The owner's plan defaults are kept: 0.5 % of equity risked per
   trade and new exposure halted at a 15 % drawdown. The other values are provisional choices of
   this ADR (see the profile's comments): throttle from 5 % to 15 % drawdown, probability scaling
   from 0.5 to 0.6, 20 lots, 3 × equity notional, 50 % margin use, a 3 % daily loss, a 4-hour
   cooldown after 5 losing round trips, 12 entries a day, stops between 3 spreads and 5 daily
   sigmas, a 120 s stale-quote breaker and a 5 × median-spread breaker.
2. **Sizing** is fixed fractional (the risk budget over the stop distance) or volatility
   targeting, then capped by the strategy's requested exposure — a strategy can ask for less,
   never more, so models still never size — scaled by the calibrated win probability and by the
   drawdown throttle, and rounded down to the lot step within the instrument's lot range. Every
   step can only shrink the size; property tests pin the caps.

## RISK-003 — limits and halts

1. **Halts refuse new exposure only.** An order that opens, increases or flips a position is
   refused while a halt is in force; an order that only reduces a position is never blocked by a
   halt (RISK-005 applies this). Each halt triggers at its threshold (`>=`): the drawdown halt on
   the *worst* drawdown since the start or the last manual reset (sticky: a recovery does not lift
   it, only `RiskStateTracker.reset_halt` does), the daily loss halt on the trading day's loss as
   a share of its starting equity (lifted at the next trading day), the cooldown for
   `cooldown_minutes` after `max_consecutive_losses` losing round trips in a row (lifted exactly
   at the end, or by a winning round trip), and the entries-per-day halt.
2. **Caps bound the target, not the order.** The target position is cut to the smallest of the
   caps — lots, notional (plus correlated exposure from other instruments through the
   `CorrelatedExposure` hook, none with one instrument), margin use and, while a named session is
   in force, the session's exposure — and rounded down to the lot step. Caps use the decision's
   reference price (the side's quote) and equity at the decision; equity at or below zero allows
   no exposure. The decision records which caps bound it.
3. **Floating point.** Sizes are rounded to the lot step from their value at 12 decimals, as in
   RISK-002, so a cap can be exceeded by at most 5e-13 lots through representation error; the
   property test allows a relative 1e-9.

## RISK-004 — stop policy

1. **Every long or short intent carries a price stop**, including one that only reduces a
   position on the same side: a new order cancels the earlier intent's bracket when it reaches
   the broker, and its own stop and target become the bracket on the whole resulting position, so
   an order without a stop would leave the rest of the position unprotected. A `flat` intent needs
   none. Time stops are allowed *in addition*, never instead: fixed-fractional sizing
   needs the distance to a price stop.
2. **Bounds** (the plan's `[k_min·spread, k_max·σ̂]`), measured from the entry reference — the
   side's quote for a market entry, the order's price for a limit or stop entry: a stop closer
   than `min_spread_multiple` × the current spread, and at least one tick, is **widened** outward
   to the tick (it becomes the decision's `adjusted_stop`, and sizing uses the widened distance,
   so the risk budget still holds); a stop farther than `max_sigma_multiple` × daily σ̂ × price is
   **refused**, not tightened — tightening would change the strategy's exit, and the plan's
   refusal is the conservative reading. Without a σ̂ at the decision no stop can be bounded and
   the entry is refused.
3. **Sanity of the bracket.** A target must be on the winning side of the entry reference (a
   target already through the market would close the position at once), and a time stop must be
   after the decision time.

## RISK-005 — the risk engine and the `OrderIntent` construction rule

1. **Pure evaluation.** `RiskEngine.evaluate(intent, state, market)` reads nothing but its inputs
   (no clock, file or environment): the event engine assembles the `RiskState` and the
   `MarketState` and records every decision in the decision ledger ("every decision logged").
   Decisions are deterministic: the same inputs give an equal decision.
2. **Order of the checks.** Exits (`flat`) are always approved, with a market order, whatever
   halts or missing data are in force. New exposure passes, in order: the data it needs (a quote;
   a win probability, when the intent carries one, must be calibrated — an uncalibrated one never
   sizes a position), the stop policy, sizing, the halts (only when the sized target opens,
   increases or flips the position) and the caps. Sizing measures the stop distance from the
   entry reference and values exposure at the decision's mid, with the equity of the risk state.
3. **What a refusal does.** A refused intent is rejected — the position is held with its bracket,
   since no order is sent — except when the position is on the other side: the strategy no longer
   wants it and closing it only reduces risk, so the decision is an approved market exit whose
   first reason starts `risk rule:`. Halts never block an intent whose sized target only reduces
   the position on the same side. A target equal to the position needs no order (approved,
   "target unchanged: no order").
4. **Audit.** `limits_snapshot` holds the risk state, the limits, the market (quote, age,
   sigma-hat, sessions), the entry reference and stop distance, and every sizing step;
   `config_version` is `<profile version>@<first 12 hex digits of the SHA-256 of the profile>`;
   the ledger keeps the reasons in order.
5. **Construction rule, enforced twice.** A `RiskDecision` carries a private *issued* mark that
   only `issue_decision` in `xq/risk/engine.py` sets; copies (`model_copy`) and decisions
   rebuilt from data are not issued. `OrderIntent` validates that its decision is approved and
   issued and that its side, size, order type, price, stop and target are the decision's (the
   decision now carries `order_type` and `price`, so a risk-forced exit is a market order even
   for a limit-entry intent); `OrderIntent.model_construct` and `model_copy(update=...)` raise.
   An AST test over `src/` fails on any `OrderIntent(...)` other than in
   `OrderIntent.from_decision`, any `OrderIntent.model_construct/model_validate*/model_copy`, any
   `RiskDecision(...)` or `issue_decision(...)` outside the risk engine, and any write of the mark
   outside the schema and the engine, including through aliases and module attributes; planted
   violations prove each form is caught. Python cannot make the rule absolute (a determined
   caller can still reach private attributes), which is why the static test exists.
6. **The event engine accepts only a `RiskEngine`** (a type check, not a protocol), so no other
   approver can be plugged in; the placeholder module is removed. The risk state's position must
   equal the broker's at every decision (checked).
7. **Sigma-hat in the event tier.** A supplied series (the value known at each instant) or, by
   default, the interim EWMA of signal-bar log returns with the profile's span (96 bars, the
   STATUS interim sigma-hat) scaled by the square root of the signal bars in a regular trading
   day, unknown before 20 returns (entries are refused until then: no stop can be bounded). It is
   the dataset primitive `ewma_volatility`, computed online (tested equal). Strategies see the
   same value in their context, so their stops are in sigma-hat units; `ExposureStrategy` and
   `RuleStrategy` place a stop `stop_sigmas` (default 3) daily sigma-hats from the mid, on the
   losing side of the entry quote. VOL-006's selection replaces the interim estimate when it is
   approved (C-18).
8. **Reconciliation with a real risk engine.** The screener has no risk rules. Sizing differences
   are the sizing effect; rejections and risk-forced exits are *event rules* (explained, and
   inside the tolerance check). Tests of execution mechanics run the real engine with a profile in
   which the requested exposure binds and the halts cannot (a 5 % budget to 4.5-sigma stops, the
   instrument's lot cap); a test with the default profile shows its sizes, cooldown rejections and
   flips cut to exits are all explained.

## RISK-006 — kill switch and data-health breakers

1. **Kill switch.** On while the profile's file exists, while its environment variable is set to a
   true value (`XQ_KILL_SWITCH` by default: `1`, `true`, `yes`, `on`), or after
   `KillSwitch.engage` until `release` (the hook for the plan's database flag, which arrives with
   the paper runtime). It blocks new exposure; exits stay allowed, and an intent against the
   position still closes it (RISK-005). Reading the flag is I/O, so the event engine reads it at
   each decision and passes its reason in the market state; `RiskEngine.evaluate` stays pure.
2. **Flatten policy.** With the profile's `flatten`, the event engine sends a `flat` intent
   through the risk engine at every signal bar while the switch is on and a position is open (so
   an exit that expires is retried). The default profile keeps positions (`flatten: false`,
   provisional).
3. **Backtests ignore the machine's switch by default.** `run_event_backtest` honours a kill
   switch only when one is passed: an environment flag set on the machine running a historical
   simulation says nothing about the past, and silently refusing entries in research would be a
   hidden result change. Paper and live runtimes will always pass one (PROD-003 adds an
   independent check in the execution gateway).
4. **Breakers.** New exposure is refused on a latest quote older than `stale_quote_s` (120 s,
   strictly older) or with a spread strictly above `spread_multiple` (5) times the median spread
   of the last `spread_window` (500) quotes, known once ten quotes have been seen. The median
   includes the latest quote, so one abnormal quote cannot move its own reference far. Both are
   provisional and apply in the event tier; the plan's "abnormal spread" is read as relative to
   the recent median rather than to an hour-of-week profile, which is the signal filter's job
   (SIGNAL-003).

## SIGNAL-001 — schemas and JSON Schema export

1. **The plan's shapes, plus what audit and linking need.** `Forecast` (plan fields plus
   `forecast_id`, the barrier `target_id` and `side` its `p_tp_first` refers to, the standard
   error `p_se` for the conservative EV, and `calibration_id`, required when `calibrated`),
   `RegimeState` (filtered probabilities summing to one; the label is one of them),
   `SignalCandidate` (plan fields plus `candidate_id` and `p_forecast` next to the `p_win` used
   for EV) and `SignalRecord` (the candidate, the forecasts themselves, model and feature-set
   versions, sigma-hat, spread, every filter's outcome, the EV check, the outcome — `intent`,
   `rejected`, `not selected` — with reasons, and the intent it produced). The size is not in the
   record: it is the risk engine's, in the decision ledger, reached through the intent's
   `signal_id` (= `record_id`). `TradeIntent` carries `p_win` and `calibrated` (RISK-005).
2. **Horizons are durations** (ISO 8601 in JSON); all instants are tz-aware; all schemas are
   frozen and refuse unknown fields.
3. **JSON Schemas** of the seven interfaces are written to `docs/specs/interfaces/` by
   `write_json_schemas` (`uv run python -m xq.signals.schema docs/specs/interfaces`) and a test
   fails when a committed file differs from the models. A `RiskDecision` round-trips as data but
   comes back *not issued*, so an `OrderIntent` cannot be revived from JSON without the risk
   engine.

## SIGNAL-002 — expected value

1. **Units.** Take-profit, stop-loss and cost are multiples of the forecast's sigma-hat over the
   trade's horizon, so EV is in sigma units and the plan's test "EV_net > θ·σ̂" becomes
   `EV_net > θ` with θ in sigma units. The round-trip cost in basis points of price converts with
   `cost_in_sigmas`.
2. **Strict thresholds.** A candidate qualifies only if `EV_net > θ` **and** `p > p_min`; equality
   does not qualify.
3. **Conservative variant.** With a forecast standard error `p_se` and a quantile `z`, the lower
   bound `max(0, p − z·p_se)` replaces p in both EV and the `p_min` test. Golden cases pin both
   variants (p = 0.6, TP 2, SL 1, cost 0.2: gross 0.8, net 0.6; with `p_se` 0.05 and z 1.645:
   p 0.51775, net 0.35325).

## SIGNAL-003 — filters

1. **The regime filter is an interface with a pass-through only** (owner's instruction, ADR 0051):
   `RegimeFilter` accepts the filtered `RegimeState` at the decision and the strategy's allowed
   regimes; the only implementation, `PassThroughRegimeFilter`, blocks nothing and writes
   "PLACEHOLDER pass-through regime filter: no regime model until REG-007 (Sprint 8)" into every
   record it passes. The real filter arrives with REG-007.
2. **Sessions and blackouts** use the session calendar (local times converted to UTC per date):
   a per-minute table per trading day built from the dataset's `calendar_columns`, exact because
   every configured boundary is on a whole minute (checked at construction; a test compares the
   lookup with `calendar_columns` at random instants across both DST changes).
3. **Volatility band**: the daily sigma-hat at the decision within `[low, high]`, inclusive; no
   sigma-hat blocks.
4. **Spread**: the current spread strictly below `k` × its New York hour-of-week median. The
   median uses only spreads observed before the decision (the caller observes a bar's spread after
   deciding on it). While the hour has fewer than `min_obs` observations the overall median stands
   in; with fewer than `min_obs` overall the candidate is blocked (no reference, no trade). New
   York time is used because gold's liquidity follows the local sessions, not UTC.

## SIGNAL-004 — orchestration and audit records

1. **A strategy is its YAML** (`experiments/configs/strategies/*.yaml`, `StrategySpec`): the model
   and barrier target it trades, its sides, horizon, barriers in sigma-hat units, EV thresholds
   (with the optional conservative variant), requested exposure and filters. The committed
   `template_barrier.yaml` is a template for the tests on synthetic forecasts, not a research
   candidate; its thresholds are placeholders to be fixed on validation folds.
2. **Candidates.** Every forecast of the strategy's model, target, instrument and sides at a
   decision is a candidate: entry at the side's quote, stop and target `sl_sigmas` and
   `tp_sigmas` of the forecast's horizon sigma-hat away, time stop at the end of the horizon.
   Forecasts of other models are not the strategy's candidates and are not recorded; a forecast of
   the strategy's model without `p_tp_first` is a configuration error and raises.
3. **Rejections, all recorded with reasons.** An uncalibrated forecast is refused; so is one not
   made at the decision time (later would be look-ahead, earlier stale), one whose conservative
   variant lacks `p_se`, one whose EV does not qualify, and any a filter blocks. The costs in EV
   are the current spread plus twice the cost model's slippage (its sigma term from the daily
   sigma-hat scaled to one minute) and commission.
4. **Selection.** Of the candidates left, the highest EV_net becomes the one `TradeIntent` (market
   entry, the requested exposure, the candidate's stop, target and time stop, its calibrated —
   possibly conservative — probability for the risk engine's scaling, `signal_id` = the record
   id); a candidate on the side already held is "not selected", as are the others, with the
   winner named. The engine never sizes and never exits: exits are the bracket, the time stop and
   the risk engine's rules.
5. **Layering.** The signal engine uses the backtest cost model (`xq.backtest.costs`) for the
   round-trip cost, the same one the event tier fills with; the cost model is shared
   infrastructure, and moving it to its own package is left to a layout ADR if the runtime needs
   it without the backtester.

## SIGNAL-005 — the signal engine in the event backtester

1. **Adapter.** `SignalStrategy` (in `xq.backtest.strategies`) wraps a `SignalEngine`, a forecast
   source (the forecasts made at the decision time from the bars seen so far) and an optional
   regime source (none exists before REG-007). At each signal bar it decides with the context's
   quote, sigma-hat and position, keeps every forecast and every record, and observes the bar's
   spread *after* deciding, so the spread reference stays causal. Shadow replay and the paper
   runtime will call the same engine (PAPER-001); only the event backtester exists now.
2. **Audit trail.** The intent's `signal_id` is its record's id and the ledger stores it, so
   `SignalStrategy.audit(ledger)` traces every fill to its order, risk decision (approved, profile
   version), intent (its reason is empty for a signal, `time stop` for the engine's exit of one),
   signal record and forecasts. A synthetic run (a causal momentum stub declared calibrated, not a
   model) shows every fill traced, one record per candidate, every signal intent matching its
   record, and the risk engine sizing on the calibrated probability; with the same stub
   uncalibrated nothing reaches the risk engine.
3. **Open point for the owner — probability scaling and payoffs.** The default risk profile scales
   size on the raw calibrated probability (zero at 0.5, full at 0.6), which suits 1:1 payoffs. For
   asymmetric barriers the break-even probability differs (1/3 for 2:1), so a good 2:1 trade with
   p = 0.45 would be sized to zero. The forecast-to-fill test uses a profile matched to its 2:1
   barriers (zero at 0.34, full at 0.5). Options: (a) keep raw-p scaling and require each
   strategy's risk profile to match its payoff; (b) scale on the edge instead, p minus the
   break-even `SL / (TP + SL)`. Recorded as a carry-over in STATUS; the default is unchanged
   until decided.
