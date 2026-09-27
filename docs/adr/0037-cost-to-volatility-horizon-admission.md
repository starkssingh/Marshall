# ADR 0037 — Cost-to-volatility table and horizon admission

- **Status:** accepted
- **Date:** 2026-09-27
- **Tasks:** EDA-006 (on EDA-001 and BT-001)

## Context

Plan assumption 6: for retail XAUUSD, round-trip cost is a large fraction of short-horizon moves.
EDA-006 computes the cost-to-volatility ratio (round-trip cost ÷ expected absolute move) per
horizon and session; horizons above a bound (default 0.3) are excluded from directional research,
and the admission list is written to `config/horizons.yaml`. The owner asked that no values be
written to `config/horizons.yaml` in Sprint 5 (ADR 0035): there is no real data.

## Decision

1. **Horizons are bar timeframes** (`horizons.candidates`: 1m, 5m, 15m, 1h, 4h, 1d). The holding
   periods of a horizon are the adjacent close-to-close returns of its bars (ADR 0036), so the
   expected absolute move is the mean absolute log return, and `1d` is one trading day.
2. **Round-trip cost per period** (bps of the entry price) from the configured cost model
   (BT-001), so EDA and the backtests share one cost definition:
   - spread: the bar's mean quoted spread over its mid close (half a spread each way);
   - commission: both sides for one lot at the entry price, over its notional;
   - slippage: the model's slippage at entry and at the last instant of the period, with the
     session and event-window multipliers, and sigma-hat the RMS of the last
     `horizons.sigma_1m_minutes` (60) one-minute returns completed by the entry;
   - financing: the rollovers inside the period (three on the triple weekday) at the **mean** of
     the long and short rates, because the research is direction-neutral.
3. **Ratio** = mean cost ÷ mean absolute move, over all periods and per session and overlap (the
   session the period's bar starts in). A horizon is **admitted** when its overall ratio is at most
   `horizons.max_cost_to_vol` (0.3); a candidate without data is excluded. Per-session admission
   is reported too, and recorded in the admission list, but only the overall list admits.
4. **Labels.** While the cost model is provisional, every cost and ratio carries "screening,
   placeholder costs" (ADR 0032), in the table, the section text, the figure and the list.
5. **The admission list** is part of the report (`admission.yaml`, in the manifest, with the
   dataset, window, git sha and EDA config hash). It reaches `config/horizons.yaml` only through
   `xq research admit-horizons --report <dir>`, which refuses a report of an exploratory run and a
   list that differs from the report's manifest. Nothing reads `config/horizons.yaml` yet; the
   target sets (TGT-002) and later tasks restrict themselves to it once it exists.

## Consequences

- Sub-15-minute directional research is excluded or admitted by a measured ratio, not by
  assumption, once real data and broker costs exist.
- With placeholder costs the ratios are screening figures; the admission should be re-run when
  broker terms replace the placeholder model.
- Tested in temporary directories only: the repository has no `config/horizons.yaml`.
