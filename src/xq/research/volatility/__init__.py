"""Volatility research (Phase 6: VOL-001 ... VOL-005; VOL-006 lives in `xq.models.volatility`).

Range estimators and ATR (`estimators`), realized measures and the train-only diurnal factor
(`realized`), fixed benchmarks (`benchmarks`), the GARCH family (`garch`) and the evaluation on
identical folds (`evaluate`). Every method passes a recovery test on a simulated process before
anything uses it (`xq.research.recovery`). Sprint 6 is build-only: nothing here has run on real
data and no forecaster is promoted (ADR 0044).
"""
