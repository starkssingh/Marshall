"""The one-time vault evaluation of a validated bundle (GATE-002).

The vault (every instant at or after ``vault.start``) is opened once per bundle, for the R3 gate,
and never again:

1. **A token is issued** (`issue_vault_token`, ``xq gate vault-token``) only to a bundle whose
   status is ``validated`` (its latest R2 result passed) and that has **never** been issued one.
   A second issuance for the same bundle is refused, whatever happened to the first token. The
   secret is shown once; only its SHA-256 is stored (``vault_tokens``, DS-004), with who issued it
   and when it expires (``vault_procedure.token_ttl_hours``).
2. **The evaluation** (`run_vault_evaluation`, ``xq gate vault-evaluate``) runs in a confirmatory
   run of kind ``vault_evaluation`` (a clean git tree is required) under the origin's hypothesis.
   - The token must belong to this bundle; that is checked before any vault read.
   - The window ends at the end of a complete trading day (the last one before today's by
     default), so no partial day is read.
   - The named quality run must grade every open trading day the evaluation reads, warm-up and
     vault alike, with no FAIL day: checked against the market calendar and the quality results
     **before** the vault is read, so missing or bad data cannot consume the token.
   - The first read redeems the token for this run (`xq.datasets.vault`); every read is logged as
     a ``vault_access_granted`` warning and in ``vault_access_log``. Any other run presenting the
     token is refused.
   - The bundle's rule is evaluated on the vault by the code the board ran: the feature set's own
     function on the bars (`resolve_feature_set(...).compute`, in memory, never materialized as a
     dataset), the board's signal bars and rule positions, the screener with the configured cost
     model and sigma-hat, on the signal bars of the bundle's ``signal_timeframe``. Warm-up
     history comes from before the vault (loaded from the dataset's start, less its warm-up and
     one bar of the longest of the signal and context timeframes), the decisions and fills from
     inside it.
3. **R3** is recorded on the bundle (MREG-002) from the vault run:
   - ``net_sharpe_min``: the vault's annualized net Sharpe ratio;
   - ``walk_forward_interval``: the quantile of the vault's per-period Sharpe ratio among
     stationary-bootstrap resamples of the strategy's walk-forward (out-of-sample, fold-aligned)
     daily returns,
     each truncated to the vault's length (the gates' bootstrap convention). It must lie inside the
     central 90 %;
   - ``risk_limit_breaches_max``: days whose net loss reaches the risk profile's daily loss limit,
     plus one if the drawdown (the capital as the first peak) reaches the drawdown halt. This is the
     screening tier's reading: the screener sizes without the risk engine;
   - ``vault_access_logged``: 1 when the access log holds this run's reads.

   The vault's daily returns are appended to the bundle's ``vault`` history (MREG-004). If the
   evaluation fails after the token was redeemed, the token is spent: the vault is not opened
   again for that bundle (the owner decides any exception, in an ADR).

Only rule bundles (``baseline_rule``) can be evaluated today: a bundle of models needs ML-009.
"""

from __future__ import annotations

import json
import math
import secrets
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import numpy.typing as npt
import pandas as pd
from sqlalchemy import Engine, func, select

from xq.backtest.costs import CostModel
from xq.backtest.metrics import drawdown_metrics, return_metrics
from xq.backtest.vectorized import run_vectorized
from xq.core.config import AppConfig, GateCheck
from xq.core.ids import new_ulid
from xq.core.logging import get_logger
from xq.core.seeds import derive_seed
from xq.core.time import ensure_utc, trading_day, trading_day_bounds, trading_days, utc_now
from xq.core.types import Timeframe
from xq.data.calendar import MarketClock
from xq.data.catalog import Catalog
from xq.data.sessions import build_session_table
from xq.datasets.base_features import BASE_INPUT, FeatureContext
from xq.datasets.builder import recorded_spec, usable_quotes
from xq.datasets.spec import DatasetSpec
from xq.datasets.vault import GateToken, secret_digest, vault_start
from xq.features.registry import resolve_feature_set
from xq.models.baselines import RuleStrategyConfig, VolTargetConfig
from xq.models.board import (
    BoardConfig,
    daily_returns_on,
    rule_positions,
    rule_signal_bars,
    sigma_1m_bps,
)
from xq.registry.bundles import (
    StrategyBundle,
    append_performance,
    get_bundle,
    load_bundle,
    performance_history,
)
from xq.registry.gates import (
    EvidenceTier,
    GateResultRef,
    latest_gate_result,
    record_gate_result,
)
from xq.registry.models import RegistryStateError, Status, SubjectKind
from xq.tracking import registry
from xq.tracking.db import session_factory
from xq.tracking.models import QualityResultRecord, VaultAccess, VaultToken
from xq.tracking.runs import experiment_run
from xq.validation.sharpe import bootstrap_distribution, gate_block_length, row_sharpe

FloatArray = npt.NDArray[np.float64]
VAULT_KIND = "vault_evaluation"
REPORT_DIR = "vault"
log = get_logger(__name__)


class VaultProcedureError(RegistryStateError):
    """The vault procedure refused a step (status, a second access, a foreign token, bad data)."""


@dataclass(frozen=True)
class VaultEvaluation:
    """The outcome of `run_vault_evaluation`."""

    bundle_id: str
    run_id: str
    days: int
    net_sharpe: float
    interval_quantile: float
    breaches: int
    result: GateResultRef
    report_dir: Path


def issue_vault_token(
    cfg: AppConfig, engine: Engine, bundle_id: str, *, issued_by: str
) -> GateToken:
    """Issue the bundle's one vault token (module docstring, item 1).

    Raises:
        VaultProcedureError: for a bundle that is not validated or has been issued a token.
        BundleIntegrityError: if the bundle does not load intact.
    """
    load_bundle(engine, bundle_id)
    bundle = get_bundle(engine, bundle_id)
    if bundle.status is not Status.VALIDATED:
        raise VaultProcedureError(
            f"bundle {bundle.short_id} is {bundle.status}: only a validated bundle (R2 passed) "
            "is evaluated on the vault"
        )
    r2 = latest_gate_result(engine, SubjectKind.BUNDLE, bundle.bundle_id, "R2")
    if r2 is None or not r2.passed:
        raise VaultProcedureError(f"bundle {bundle.short_id} has no passing latest R2 result")
    now = utc_now()
    token = GateToken(new_ulid(), secrets.token_urlsafe(32))
    ttl = pd.Timedelta(hours=cfg.validation_config().vault_procedure.token_ttl_hours)
    with session_factory(engine)() as session:
        issued = session.scalars(
            select(VaultToken).where(VaultToken.bundle_id == bundle.bundle_id)
        ).first()
        if issued is not None:
            raise VaultProcedureError(
                f"bundle {bundle.short_id} was issued vault token {issued.token_id} at "
                f"{issued.issued_at}: a second vault access for the same bundle is refused"
            )
        session.add(
            VaultToken(
                token_id=token.token_id,
                secret_sha256=secret_digest(token.secret),
                bundle_id=bundle.bundle_id,
                issued_by=issued_by,
                issued_at=now,
                expires_at=now + ttl,
                revoked=False,
                redeemed_at=None,
                redeemed_by_run=None,
            )
        )
        session.commit()
    log.warning(
        "vault_token_issued",
        token_id=token.token_id,
        bundle_id=bundle.bundle_id,
        issued_by=issued_by,
        expires_at=str(now + ttl),
    )
    return token


def run_vault_evaluation(
    cfg: AppConfig,
    engine: Engine,
    bundle_id: str,
    token: GateToken,
    *,
    quality_run_id: str,
    last_day: date | None = None,
) -> VaultEvaluation:
    """The bundle's one vault evaluation and its R3 result (module docstring, items 2 and 3).

    Args:
        last_day: The last trading day of the vault window (default: the last complete one).

    Raises:
        VaultProcedureError: for a bundle that is not validated or not a rule bundle, a token of
            another bundle, a quality run that does not grade every day read or grades one FAIL,
            or no vault decision.
        VaultAccessError: for an unknown, expired or revoked token, or one another run redeemed.
        RunContextError: on a dirty git tree (the evaluation is always confirmatory).
    """
    content = load_bundle(engine, bundle_id)
    bundle = get_bundle(engine, bundle_id)
    if bundle.status is not Status.VALIDATED:
        raise VaultProcedureError(f"bundle {bundle.short_id} is {bundle.status}, not validated")
    if content.strategy.kind != "baseline_rule" or bundle.origin_run_id is None:
        raise VaultProcedureError("only a rule bundle from a board run can be evaluated now")
    with session_factory(engine)() as session:
        record = session.get(VaultToken, token.token_id)
        if record is None or record.bundle_id != bundle.bundle_id:
            raise VaultProcedureError(f"token {token} does not belong to bundle {bundle.short_id}")
    origin = registry.get_run(engine, bundle.origin_run_id)
    if origin.dataset_id is None:
        raise VaultProcedureError(f"origin run {origin.run_id} records no dataset")
    spec = recorded_spec(engine, origin.dataset_id)
    board = BoardConfig.model_validate(origin.config["run"]["board"])
    vault = vault_start(cfg)
    window_end = (
        trading_day_bounds(last_day)[1]
        if last_day is not None
        else trading_day_bounds(trading_day(utc_now()))[0]
    )
    if window_end <= vault:
        raise VaultProcedureError(f"the vault window must end after vault.start ({vault})")
    load_start = ensure_utc(spec.start - spec.warmup) - max(
        [
            Timeframe(content.strategy.signal_timeframe).duration,
            *(tf.duration for tf in spec.context_timeframes),
        ]
    )
    _check_quality(cfg, engine, quality_run_id, load_start, window_end)
    experiment = registry.get_experiment(engine, origin.experiment_id)
    with experiment_run(
        cfg,
        engine,
        experiment.hypothesis_id,
        {"bundle": bundle.bundle_id, "token_id": token.token_id, "quality_run": quality_run_id},
        kind=VAULT_KIND,
        seed=origin.seed,
        dataset_id=origin.dataset_id,
        exploratory=False,  # the vault evaluation is evidence: always confirmatory
        title=f"vault evaluation of bundle {bundle.short_id}",
    ) as run:
        daily = _vault_returns(
            cfg, engine, run.run_id, token, content, spec, board, load_start, window_end
        )
        periods = cfg.gate_periods_per_year()
        r = daily["net_return"].to_numpy(np.float64)
        sharpe = float(return_metrics(daily["net_return"], periods)["sharpe"])
        seed = derive_seed(origin.seed, "vault_interval", bundle.bundle_id)
        quantile = _interval_quantile(cfg, engine, bundle.bundle_id, seed, r)
        breaches = _breaches(cfg, r)
        with session_factory(engine)() as session:
            reads = session.scalar(
                select(func.count())
                .select_from(VaultAccess)
                .where(VaultAccess.run_id == run.run_id)
            )
        gates = cfg.gates_config()
        checks = [
            gates.criterion("R3", "net_sharpe_min").check(sharpe),
            gates.criterion("R3", "walk_forward_interval.low").check(quantile),
            gates.criterion("R3", "walk_forward_interval.high").check(quantile),
            gates.criterion("R3", "risk_limit_breaches_max").check(float(breaches)),
            gates.criterion("R3", "vault_access_logged").check(1.0 if reads else 0.0),
        ]
        directory = (
            cfg.paths.resolve(cfg.paths.reports_dir) / REPORT_DIR / bundle.short_id / run.run_id
        )
        report = _write_report(directory, bundle.bundle_id, run.run_id, daily, checks, reads or 0)
        run.log_artifact(report, kind="vault_report")
        run.log_metric("vault/net_sharpe", sharpe if math.isfinite(sharpe) else 0.0)
        run.log_metric("vault/days", float(len(daily)))
        if not len(performance_history(engine, bundle.bundle_id, "vault")):
            append_performance(engine, bundle.bundle_id, "vault", daily, run_id=run.run_id)
        run_id = run.run_id
    result = record_gate_result(
        engine,
        gates,
        subject_kind=SubjectKind.BUNDLE,
        subject_id=bundle.bundle_id,
        gate="R3",
        checks=checks,
        not_evaluated={},
        not_applicable={},
        evaluator="xq gate vault-evaluate",
        evidence_paths=[str(report), str(report.with_suffix(".md"))],
        run_id=run_id,
        # risk-limit breaches read on the screening tier: promotes to vault_passed, not to paper
        evidence_tier=EvidenceTier.SCREENING,
    )
    return VaultEvaluation(
        bundle.bundle_id, run_id, len(daily), sharpe, quantile, breaches, result, directory
    )


def _check_quality(
    cfg: AppConfig, engine: Engine, quality_run_id: str, start: pd.Timestamp, end: pd.Timestamp
) -> None:
    """Refuse, before the vault is read, a quality run that misses an open trading day of the
    window or grades one FAIL."""
    first, last = trading_day(start), trading_day(end - pd.Timedelta(1, "ns"))
    calendar = build_session_table(cfg.sessions_config(), first, last)
    open_days = calendar.loc[calendar["is_open"].astype(bool), "trading_day"]
    expected = {pd.Timestamp(d).date() for d in open_days}
    with session_factory(engine)() as session:
        rows = session.execute(
            select(QualityResultRecord.trading_day, QualityResultRecord.status).where(
                QualityResultRecord.run_id == quality_run_id,
                QualityResultRecord.trading_day >= first,
                QualityResultRecord.trading_day <= last,
            )
        ).all()
    graded = {day for day, _ in rows}
    missing = sorted(str(d) for d in expected - graded)
    if missing:
        raise VaultProcedureError(
            f"quality run {quality_run_id} does not grade {', '.join(missing)}: every trading day "
            "the vault evaluation reads must be validated first (xq validate --include-vault)"
        )
    failing = sorted({str(day) for day, status in rows if status == "fail"})
    if failing:
        raise VaultProcedureError(
            f"quality run {quality_run_id} grades FAIL on {', '.join(failing)}: the vault is not "
            "opened on data that failed its checks"
        )


def _vault_returns(
    cfg: AppConfig,
    engine: Engine,
    run_id: str,
    token: GateToken,
    content: StrategyBundle,
    spec: DatasetSpec,
    board: BoardConfig,
    load_start: pd.Timestamp,
    end: pd.Timestamp,
) -> pd.DataFrame:
    """Daily net returns of the bundle's rule on every vault trading day with a decision."""
    catalog = Catalog(cfg, engine=engine, run_id=run_id)
    timeframes = [spec.base_timeframe, *spec.context_timeframes]
    inputs: dict[str, pd.DataFrame] = {}
    for tf in timeframes:
        bars = catalog.load_bars(
            spec.source,
            spec.instrument,
            tf,
            spec.price_basis,
            load_start,
            end,
            build=spec.bar_build,
            vault_token=token,
        )
        name = BASE_INPUT if tf == spec.base_timeframe else tf.value
        inputs[name] = bars.loc[bars["is_complete"].to_numpy()].reset_index(drop=True)
    context = FeatureContext(
        spec.base_timeframe, tuple(spec.context_timeframes), cfg.sessions_config()
    )
    features = resolve_feature_set(cfg, spec.feature_set).compute(inputs, context)
    vault = vault_start(cfg)
    decisions = features.index[(features.index >= vault) & (features.index < end)]
    if not len(decisions):
        raise VaultProcedureError("no decision inside the vault window")
    rule = RuleStrategyConfig.model_validate(content.strategy.config["rule"])
    raw_target = content.strategy.config.get("vol_target")
    target = VolTargetConfig.model_validate(raw_target) if raw_target else board.vol_target
    signal, bar_periods = rule_signal_bars(
        cfg, features, content.strategy.signal_timeframe, cfg.gate_periods_per_year()
    )
    positions = rule_positions(signal, rule, target, bar_periods, pd.DatetimeIndex(decisions))
    costs = CostModel.from_config(cfg, spec.instrument, engine=engine, source_id=spec.source)
    days = sorted({d.item() for d in trading_days(pd.DatetimeIndex(decisions))})
    clock = MarketClock.for_range(
        cfg.sessions_config(), days[0] - timedelta(days=1), days[-1] + timedelta(days=10)
    )
    quotes = usable_quotes(cfg, spec, vault, end, set(), catalog=catalog, vault_token=token).loc[
        :, ["ts_utc", "bid", "ask"]
    ]
    sigma = sigma_1m_bps(cfg, spec, features).reindex(decisions)
    capital = cfg.backtest_config().capital_usd
    result = run_vectorized(positions, quotes, costs, clock, capital=capital, sigma_1m_bps=sigma)
    returns = daily_returns_on(result, days)
    trades = result.trades.loc[~result.trades["open"].astype(bool)]
    closed: dict[date, int] = {}
    for exit_time in pd.DatetimeIndex(trades["exit_time"]):
        closed[trading_day(exit_time)] = closed.get(trading_day(exit_time), 0) + 1
    return pd.DataFrame(
        {
            "net_return": returns.to_numpy(np.float64),
            "net_pnl": returns.to_numpy(np.float64) * capital,
            "trades": [closed.get(day, 0) for day in days],
        },
        index=pd.Index(days, name="trading_day"),
    )


def _interval_quantile(
    cfg: AppConfig, engine: Engine, bundle_id: str, seed: int, vault: FloatArray
) -> float:
    """The quantile of the vault's per-period Sharpe ratio among bootstrap resamples of the
    walk-forward returns truncated to the vault's length (module docstring)."""
    history = performance_history(engine, bundle_id, "backtest")
    if history.empty:
        raise VaultProcedureError(
            "the bundle has no backtest history to predict the vault from (MREG-004)"
        )
    oos = history["net_return"].to_numpy(np.float64)
    convention = cfg.gates_config().conventions.bootstrap
    block = gate_block_length(oos, convention.block_length, convention.min_block_days)
    length = min(len(vault), len(oos))
    draws = bootstrap_distribution(
        oos,
        lambda d: row_sharpe(d[:, :length]),
        n_boot=convention.n_boot,
        mean_block=block,
        seed=seed,
    )
    draws = draws[np.isfinite(draws)]
    std = float(np.std(vault, ddof=1)) if len(vault) > 1 else 0.0
    observed = float(np.mean(vault) / std) if std > 0 else 0.0
    if not len(draws):
        return math.nan
    return float(np.mean(draws <= observed))


def _breaches(cfg: AppConfig, returns: FloatArray) -> int:
    """Risk-limit breaches of the vault run on the screening tier (module docstring)."""
    limits = cfg.risk_config().limits
    days = int(np.sum(returns <= -limits.max_daily_loss))
    capital = cfg.backtest_config().capital_usd
    equity = pd.Series(capital * (1 + np.cumsum(returns)))
    drawdown = drawdown_metrics(equity, capital)["max_drawdown"]
    return days + int(drawdown >= limits.max_drawdown)


def _write_report(
    directory: Path,
    bundle_id: str,
    run_id: str,
    daily: pd.DataFrame,
    checks: list[GateCheck],
    reads: int,
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "bundle_id": bundle_id,
        "run_id": run_id,
        "days": len(daily),
        "first_day": str(daily.index[0]),
        "last_day": str(daily.index[-1]),
        "vault_reads_logged": reads,
        "checks": [
            {
                "key": c.criterion.key,
                "measure": c.criterion.measure,
                "op": c.criterion.op,
                "threshold": c.criterion.threshold,
                "value": c.value if math.isfinite(c.value) else None,
                "passed": c.passed,
            }
            for c in checks
        ],
    }
    path = directory / "vault.json"
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    lines = [
        f"# Vault evaluation of bundle `{bundle_id[:12]}`",
        "",
        f"Run `{run_id}`: {len(daily)} vault trading days, {daily.index[0]} to "
        f"{daily.index[-1]}; {reads} vault read(s) logged. The vault is opened once per bundle.",
        "",
        *[f"- {c.describe()}" for c in checks],
        "",
    ]
    path.with_suffix(".md").write_text("\n".join(lines), encoding="utf-8")
    return path
