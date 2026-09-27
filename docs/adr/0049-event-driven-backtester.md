# ADR 0049 — The event-driven backtester (Sprint 11)

- **Status:** accepted
- **Date:** 2026-09-27
- **Decided by:** Claude, within the plan (section 6, Phase 13) and the owner's Sprint 11
  instructions (ADR 0048); open points are flagged for the owner's review of Sprint 11
- **Tasks:** BT-004, BT-005, BT-006, BT-007, BT-008, BT-009, BT-010

## Context

The plan asks for a two-tier backtester: the vectorized screener (BT-002, done) for research and
an event-driven reference simulator for candidates, with a reconciliation test between them
(section 2, assumption 8). The event tier must route every order through a risk decision
(`RiskEngine.evaluate`, RISK-005, Sprint 12), which does not exist yet, and is built and tested
on synthetic data only (ADR 0048).

## BT-004 — event core

1. **Events and ordering.** `TickEvent`, `BarEvent`, `SignalEvent`, `OrderEvent`, `FillEvent`,
   `TimerEvent` as the plan lists, plus `ExecutionBarEvent` for execution without ticks (bar
   mode). The queue orders by `(timestamp, rank, sequence)`. The plan says `(timestamp,
   sequence)`; the rank is added so that "at or after" means the same in both tiers whatever the
   insertion order: at one instant, timers (rollover financing, day-end marks, expiries) come
   first, then order arrivals, then quotes, then fills, then signal bars, then intents. An order
   arriving at t can fill on a quote at t (the screener's first quote at or after the intended
   time); a rollover at t charges the position held strictly before t and a day's end marks with
   the last quote strictly before it (as BT-002 does).
2. **Deterministic clock.** `SimulationClock` moves only with processed events and refuses to go
   back. Ids are counters (`I000001`, `D-I000001`, `O-I000001`, ...), never random, so two runs
   of the same inputs produce the same trace, ledger and result.
3. **Decision chain and schemas.** A strategy returns `TradeIntent`s from `on_bar(bar, ctx)`; the
   engine stamps the intent id and the decision time (the bar's `available_at`), so a strategy
   cannot choose its decision time. `TradeIntent`, `RiskDecision` and `OrderIntent` are built now
   in their planned home, `xq.signals.schema`, in the plan's shape (Phase 15) plus the fields the
   engine needs (`exposure`, the requested exposure a fraction of capital; `target_lots` and
   `side` on the decision; `expected_position_lots` on the order). `OrderIntent` can only be
   built from an approved `RiskDecision` and must carry its side, size, stop and target (enforced
   by the type). SIGNAL-001 completes the schema set and exports JSON Schemas in Sprint 12.
4. **Placeholder risk approver.** `xq.risk.placeholder.PassThroughRiskApprover` approves every
   intent without any risk check and sizes it mechanically: sign × exposure × initial capital ÷
   (mid of the latest quote at the decision × contract size), rounded down to the lot step
   within the minimum and maximum lot. Every decision carries `config_version
   "placeholder-pass-through-1"` and the reason "PLACEHOLDER pass-through approver: no risk
   checks"; every result and report carries its label. RISK-005 replaces it in Sprint 12.
5. **Data modes.** Tick mode: the broker executes on quotes; signal bars are built from the same
   quotes by the DATA-008 `build_bars` (bars ending after the data's coverage are dropped). Bar
   mode: the broker executes on one-minute bid/ask bars; each is streamed as its open (at the
   bar's start) and its range (just before its end). The strategy sees only completed signal bars
   at their `available_at`.
6. **Closed market.** A decision taken while the market is closed places no order (ADR 0032); the
   engine refuses it before the risk decision and records the refusal.
7. **Layout.** New modules: `xq/backtest/events.py` and `engine.py` (planned),
   `xq/signals/schema.py` (planned, partial), `xq/risk/placeholder.py` (temporary, removed when
   RISK-005 lands).

## BT-005 — broker simulator

1. **Fills.** Market orders fill at the first quote at or after arrival on the correct side (buy
   at the ask, sell at the bid) plus the cost model's slippage (sigma-hat known at the decision,
   as-of lookup), or expire after `max_fill_delay` (a timer at arrival + delay + 1 ns). Stop
   orders fill at the first quote at or beyond the stop plus slippage, so a gap fills at the
   gapped price. Limit orders fill at the limit price, never better and without slippage (the
   pessimistic choice; a real venue may improve). Brackets are OCO legs on the whole resulting
   position, active from the next quote after the parent's fill.
2. **Closed market.** Nothing fills on a quote while the market is closed; working stops and
   limits wait and gap-fill at the reopen.
3. **Bar mode.** Market orders fill at the open of the first one-minute bar starting at or after
   arrival (the first price certainly after it); stops and limits passed by the open fill at
   the open; levels touched by the bar's range fill at the level (stops plus slippage). When a
   bar touches both legs of a bracket, the stop loss fills (pessimistic) and the bar is counted
   as ambiguous; with ticks, the quotes decide. Range fills are stamped just before the bar's
   end and carry the bar's start.
4. **Arrival checks.** An order is rejected if the position is no longer the one its decision
   saw, and an order that opens, increases or flips exposure is rejected if the margin it needs
   (`|position after| x contract x mid x margin_rate`) exceeds the equity. A rejected order
   leaves working orders alone; an accepted one cancels the working orders of earlier intents
   ("replaced"). Partial fills are not simulated (P3 in the plan).
5. **Cost decomposition per fill.** `lots x (price - mid) x contract = spread_cost +
   slippage_cost` against the reference mid, where the spread cost is the half-spread and the
   slippage cost the rest; commission at the mid (`CostModel.commission_usd`). A limit fill's
   reference quote is its limit on the order's side with the triggering quote's spread (as for
   a level touched inside a bar), so it pays the half-spread and no slippage.

## BT-006 — portfolio accounting

1. **A CFD account.** Cash starts at the capital and moves only by realized price P&L,
   commissions and financing; the position is valued at the latest mid (the screener marks at mid
   too). Equity = cash + unrealized P&L of the open FIFO lots. Independently, equity = capital +
   every fill's cash flow − commissions − financing + position × mark × contract; the two are
   compared after every event of engine runs (and in a property test), which is the accounting
   identity the plan asks for.
2. **FIFO lots for trade statistics.** A fill closes the oldest opposite lots first; each closed
   (part of a) lot is a trade with its price P&L at fill prices (so spread and slippage are
   inside it) and its pro-rata share of the commissions of the fills that opened and closed it
   and of the financing charged while it was open. Open lots are reported marked at the mid.
   The trades' P&L adds up to the change in equity.
3. **Financing** at each rollover timer on the position held over it (fills strictly before the
   rollover), at the mid of the last quote before it, by `CostModel.financing_usd` — a cost on
   both sides while the cost model is provisional (ADR 0032), three times on the configured
   weekday.
4. **Daily frame.** Day-end snapshots become the screener's daily layout (`DAILY_COLUMNS`), with a
   charge at a day's end counted in that day, so both tiers share the BT-003 metrics. Margin used
   is `|position| x contract x mark x margin_rate`.

## BT-007 — decision ledger

1. **One row per step, in order**, with linking ids: `intent` (decision time), `refusal` (reason,
   before the risk decision), `decision` (approved or not, size, reasons, approver version),
   `order` (its decision and intent; bracket legs also their parent), `order_rejected`,
   `order_cancelled`, `order_expired` (reason) and `fill`. The broker records fills, legs,
   cancels, rejections and expiries when they happen, so a fill precedes the bracket it opens
   and the OCO cancel it causes.
2. **Bracket legs carry their parent's decision**: that decision approved the stop and target.
   Engine-generated exits (time stop, weekend exit) are intents with their own decisions.
3. **`Ledger.check_links`** lists every broken link — an order without a decision or with a
   rejected one, a fill or cancel of an unknown order, a decision without its intent. Every event
   result carries that list (`link_problems`); tests require it to be empty and show it catches
   planted breaks.
4. **Storage.** `ledger.parquet` plus `ledger_summary.parquet` (row counts by kind and reason).
   Ids are deterministic counters, so the same inputs give byte-identical ledgers.
5. **Result.** `run_event_backtest` returns an `EventBacktestResult`, a `BacktestResult` with the
   screener's columns where they mean the same (the BT-003 metrics apply unchanged) plus the
   ledger, the equity at every signal bar, the brackets and the ambiguous-bar share: in bar mode
   the broker's count of bars touching both legs (resolved to the stop); in tick mode the
   one-minute bars in which a bracket ended and whose range reached both levels (resolved by the
   ticks), over the one-minute bars during which a bracket was active.

## BT-008 — session constraints

1. **What is blocked.** Orders that open, increase or flip exposure never fill inside a blackout;
   orders that only reduce exposure are never blocked. The engine refuses an intent that clearly
   opens or flips a position at a decision time inside a blackout (before the risk decision); the
   broker rejects a market entry arriving inside one, cancels a market entry whose first quote
   falls inside one, and lets a resting stop or limit entry wait until the blackout ends.
2. **Which blackouts** (`backtest.event.blackouts`): the `rollover` event window, 16:45–18:15
   New York on every day including the Sunday reopen — the owner's C-3 window (ADR 0026), not the
   plan's older 16:55–18:05 default; the `us_data_release` window (−5/+30 min), the only
   configured news window; and the last 60 minutes before a weekly close, a close followed by at
   least 24 hours without trading (weekends and full-day holidays such as Good Friday). The 60
   minutes are a provisional default of this ADR (the plan names the rule, not the length).
3. **Exact intervals.** Windows are computed once as UTC intervals over the market clock's range
   (clock windows per calendar day in their own zone, anchor windows around the session table's
   anchors); a test shows they agree with the dataset columns `in_<name>_window` (DS-007) at
   20,000 random instants and every minute across the March 2024 DST changes.
4. **Flat before the weekend** is optional (default off) and exits 30 minutes before the weekly
   close (16:30 New York, before the rollover window's slippage multiplier), as an intent through
   the risk approver.
5. **Margin** is `backtest.event.margin_rate`, PROVISIONAL at 0.05 (1:20) until broker terms.

## BT-009 — reconciliation

1. **Shared strategies.** `ExposureStrategy` replays the screener's target exposures and
   `RuleStrategy` runs a BASE-002 rule bar by bar on the signal bars seen so far (so its decisions
   are causal by construction; the test shows they equal the rule computed on the full series).
   Both send an intent only when the target differs from the last *executed* target, as the
   screener does.
2. **Tolerance** (the plan's default, `backtest.event.reconcile_tolerance`): the largest daily
   equity difference is at most 5 % of the screener's total costs.
3. **Every difference explained.** The event tier's executed positions are replayed through the
   screener (the *adjusted* screen), which splits the difference into an *execution effect*
   (adjusted − screener: what the tiers executed differently, itemized per decision with a cause
   — sizing, an event-only rule such as a blackout or a weekend exit, or a follow-on of an earlier
   difference) and a *residual* (event − adjusted: the fill, cost, financing and accounting
   mechanics on identical orders), which must be below one cent. A decision without a known
   cause, or a larger residual, is unexplained.
4. **Findings.** Reconciling found two screener defects, fixed in their own commits: a quote while
   the market was closed could be a fill quote, and the rollover ending the last quote's trading
   day was not charged on an open position. After them the residual is below 1e-6 USD. The
   remaining, explained difference is **sizing**: the event tier sizes at the decision's mid and
   rounds down to the lot step through the risk approver, the screener fractional lots at the
   fill's mid. At 100,000 USD and half exposure (0.25 lots) one lot step is 4 % of the position,
   so sizing alone can exceed 5 % of costs; the reconciliation reports it separately and the
   tolerance tests use rule baselines at both 100,000 and 10,000,000 USD, plus an exposure
   schedule at 10,000,000 USD. Event-only rules (the entry blackouts) are switched off for the
   tolerance check and, when on, show up as explained "event rule" differences.
5. **Speed.** The event tier prices each fill's slippage from a per-minute table of the window
   multipliers built once per trading day (`CostModel.slippage_bps_at`), exact because every
   configured boundary falls on a whole minute (otherwise it computes the multiplier directly);
   a test shows it equals the vectorized formula across a DST change.

## BT-010 — report

1. **One report for both tiers** (`build_backtest_report`, deterministic `ReportBuilder` output):
   the tier and data mode; the cost basis on every net figure ("screening, placeholder costs"
   while costs are provisional) and, for the event tier, the risk approver's label (the Sprint 11
   PLACEHOLDER says it checks nothing); the BT-003 metrics; the cost decomposition (gross at the
   reference mids, spread, slippage, commission, financing, net); equity and drawdown; monthly
   returns compounded by month of the trading day; the trade distribution; exposure by session;
   the ambiguous-bar share and its resolution; and, for the event tier, the ledger summary and
   its broken links.
2. **Cost-fragile** when gross P&L is below 1.5 × total costs (plan Phase 13, research
   validation); a strategy without a gross edge is cost-fragile by that rule.
3. **Exposure by session** is measured on a five-minute grid of market-open time, valuing the
   position at its latest fill's mid (both tiers have fills; the screener has no per-quote
   marks). It is a description of where the strategy holds risk, not a P&L attribution.
4. **Records.** `write_backtest` writes the report (and the event tier's ledger) under
   `reports/backtests/<run_id>/<name>/`, logs them as run artifacts and adds a row to the new
   `backtests` table (migration 0009): the plan's columns (`backtest_id, run_id,
   strategy_version, cost_model_version, start, end, metrics_json, ledger_path`) plus `tier`,
   `strategy_id` and `report_path`. `cost_model_version` is the venue and a hash of the cost
   configuration.
5. **No CLI yet.** Like the Sprint 6 reports, event backtests and reconciliations have no CLI
   command until there is real data to run them on; they run from library code and tests.

## Open points for the owner's review of Sprint 11

- The weekly-close blackout length (60 minutes) and the flat-before-weekend lead (30 minutes)
  are provisional defaults of this ADR.
- The reconciliation tolerance applies to the raw equity difference, sizing included; at small
  capital, lot-step rounding alone can exceed 5 % of costs (reported separately as sizing).
- Limit orders never fill better than their price (no improvement) — pessimistic until paper
  trading measures the venue.
- The event tier is pure Python and replays every quote; its speed on four years of real ticks is
  unmeasured.
