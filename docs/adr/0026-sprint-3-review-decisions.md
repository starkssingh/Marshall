# ADR 0026 — Owner decisions from the Sprint 3 review

- **Status:** accepted
- **Date:** 2026-09-26
- **Decided by:** project owner, answering the open questions of the Sprint 3 pull request
- **Refines:** ADR 0018 (event windows), ADR 0023 (trial clustering), ADR 0025 (forward returns)

## Decisions

1. **Execution latency 1 s and maximum fill delay 300 s stay, provisionally** (ADR 0025 §2–3).
   Target builds now report how often fills were late: every labelled target row records its
   `fill_delay_s` (the later of its entry and exit fill, measured from the intended fill time),
   and the manifest and `xq dataset build` report, per target, the labelled rows, the number of
   fills delayed by more than 5 s and the largest delay. The 5 s reporting threshold is
   configuration (`datasets.fill_delay_report_s`), not part of the target definition.
2. **Event windows.** The US data release window stays `[08:30 − 5 min, 08:30 + 30 min)`. The
   rollover window becomes the New York clock window **16:45–18:15** on every day: the pre-close,
   the daily break and the reopen spread spike. It is defined by clock time rather than around the
   17:00 rollover anchor, so it also covers the Sunday reopen, which follows no rollover.
3. **Trial clustering.** The correlation threshold stays 0.7. Trial returns are first summed per
   trading day (17:00 New York roll), and a pair needs at least **60 common trading days** to be
   compared; with less, the pair counts as independent (ADR 0023 §4).
4. **Trading-time horizons.** Horizons, and the execution latency, count only market-open time
   from the trading calendar (market hours of `config/sessions.yaml`). A decision shortly before a
   close, or on a Friday afternoon, therefore gets a label whose holding period includes the
   overnight or weekend gap, instead of no label. A new boolean `crosses_close` marks labels whose
   holding period (entry to exit) spans at least one market close. A decision taken while the
   market is closed (for example at the 17:00 close itself) is entered at the reopen. Fill delays
   stay wall-clock durations measured from the intended fill time. `label_end` remains the exit
   fill time, so purging keeps working; the leakage tests and the label-window assertions measure
   horizons in market time. The `forward_return` kind moves to code version 2, so dataset ids
   change; the `fwd_returns.v1` definition itself is unchanged.
5. **`ds_base.yaml` start (2021-09-26) and the four horizons (15m, 1h, 4h, 1d)** stay for now.

Also requested: rebuild the Docker image and run the test suite inside it, because `scipy` was
added in Sprint 3 without re-checking the image.

## Consequences

- Scaling the vol-normalized targets by `sqrt(h)` now uses market minutes, consistent with the
  horizon definition.
- A trading-time label can span a long closure (a holiday weekend); `crosses_close` lets research
  separate or exclude those labels deliberately rather than by accident.
