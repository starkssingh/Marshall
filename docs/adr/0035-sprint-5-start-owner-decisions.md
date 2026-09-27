# ADR 0035 — Owner decisions at the start of Sprint 5: the H-0001 revision and a build-only sprint

- **Status:** accepted
- **Date:** 2026-09-27
- **Decided by:** project owner, reviewing the H-0001 draft (C-14) and starting Sprint 5
- **Tasks:** BASE-005 (H-0001 draft); Sprint 5 scope (EDA-001 … EDA-006, EXP-005, DATA-013)

## Context

Sprint 4 ended with a draft pre-registration of the baseline board, `experiments/hypotheses/
H-0001.yaml`, for the owner to review before any run on real data (ADR 0034, C-14). No real broker
data exists yet (C-8). Sprint 5's research tasks (EDA-002 … EDA-006) are meant to run on the
discovery window of real data.

## Decision 1 — H-0001 is revised and stays unregistered

The owner asked for four changes. The draft is revised accordingly and is **not** registered:

1. **Rule baselines on the full pre-vault history.** A rule baseline's parameters are fixed in
   advance, so it needs no training window: it is evaluated over the full pre-vault history of the
   dataset after its own warm-up (the bars its lookbacks and its volatility target need). The
   fold-aligned version — the same returns restricted to the walk-forward test folds — is stored
   too, so later candidates can be compared with it on identical days. Forecast-sign baselines
   are fitted (`historical_mean`, `climatology`), so they stay on the walk-forward test folds.
2. **Rule baselines on 1d and 1h signal bars, not 15m.** Each rule and its volatility-targeted
   version runs on daily and on hourly signal bars (lookbacks count bars of the signal
   timeframe). The 15m base timeframe remains the decision grid for fills and costs. The trial
   budget becomes 36: 24 rule strategies (6 rules × plain and volatility-targeted × 1d and 1h)
   plus 12 forecast-sign strategies (4 targets × `random_walk`, `historical_mean`,
   `climatology`). A rule's fold-aligned version is a view of the same configuration's returns,
   not a further trial.
3. **Windows set at registration.** The discovery and evaluation windows read "set from the real
   data's depth at registration". They are deliberately not timestamps, so `xq exp register`
   refuses the draft until they are filled in from the real data (a test pins this).
4. **Descriptive slices by year and by session**, reported and not tested (no p-values, no
   trials).

Two readings are Claude's and are flagged for the owner's review of the revision: lookbacks count
bars of the signal timeframe (the same fixed parameters on 1d and 1h bars), and the fold-aligned
version is not counted as a separate trial.

## Decision 2 — Sprint 5 is build-only

Real broker data is not available, so Sprint 5 implements EDA-001, EDA-006, EDA-002, EDA-003,
EDA-004, EDA-005 and EXP-005 as code with tests on synthetic data and on simulated processes with
known properties (GARCH(1,1) for volatility clustering, AR(1) for autocorrelation, injected
hour-of-week effects for seasonality). In particular:

- no EDA report is generated on real or pseudo-real data;
- no values are written to `config/horizons.yaml` (EDA-006 builds the admission table and the
  writer, tested in temporary directories only);
- DATA-013 (secondary long-history feed) is deferred until the owner decides whether a secondary
  feed is needed.

## Consequences

- The board runner does not yet implement the revision (full-history rule evaluation with a
  fold-aligned view, two signal timeframes in one board, year and session slices). That work
  follows the owner's approval of the revised draft and must be done before H-0001 is registered
  and run (C-15 in `docs/STATUS.md`).
- Sprint 5's working system (`xq research eda --dataset <id>`) exists and is tested on synthetic
  data only; the horizon admission list, the hypotheses backlog and the EDA reports wait for real
  data (C-8).
