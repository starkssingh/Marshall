"""Parameter perturbation and plateau metrics (ROB-001).

A strategy whose performance is a *plateau* around its chosen parameters survives the estimation
error in those parameters; one that sits on a *peak* (a single-point optimum) is curve-fitted,
whatever its Sharpe ratio. `perturb` re-evaluates a strategy around its nominal parameters and
measures the plateau:

- **Values.** At each level p (default 10, 20 and 30 %), a parameter moves to
  ``nominal +/- p * scale`` (``scale`` is |nominal| unless given, so a parameter whose nominal is
  zero needs one). An integer parameter moves to the nearest integer (halves round up), at least
  one step away from the nominal. A parameter with a grid of allowed values (``choices``) moves to
  the allowed value nearest the target on each side: the neighbouring discrete values. A value
  below ``minimum`` is dropped.
- **Designs.** *One at a time*: each parameter down and up, the others at nominal. *Jointly*:
  every combination of {down, nominal, up} across the parameters, except the nominal point
  itself. *Heat maps*: for each pair of parameters, the grid of both parameters' values at every
  level, the others at nominal.
- **Metrics.** Every point's annualized net Sharpe ratio, from the per-period net returns the
  strategy's ``evaluate`` returns. The joint neighbourhood at level p gives the *profitable share*
  (the share of points with net Sharpe > 0; a point without variance, such as one that never
  trades, is not profitable) and the *median-to-nominal ratio* (the median neighbourhood Sharpe
  over the nominal Sharpe; NaN when the nominal is not positive).
- **Gate.** R2's ``parameter_neighbourhood``: the profitable share of the joint neighbourhood at
  ``perturbation`` (20 %) must be at least ``profitable_share_min`` (ADR 0032, ADR 0054).

Each distinct point is evaluated once.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field

import numpy as np
import numpy.typing as npt
import pandas as pd

from xq.core.config import GateCheck, GatesConfig
from xq.validation.sharpe import sharpe_ratio

DEFAULT_LEVELS = (0.10, 0.20, 0.30)
Point = tuple[float, ...]
Evaluate = Callable[[Mapping[str, float]], npt.ArrayLike]


@dataclass(frozen=True)
class Parameter:
    """One tunable parameter of a strategy and how it is perturbed (module docstring)."""

    name: str
    nominal: float
    integer: bool = False
    scale: float | None = None
    minimum: float | None = None
    choices: tuple[float, ...] | None = None

    def __post_init__(self) -> None:
        if not math.isfinite(self.nominal):
            raise ValueError(f"{self.name}: the nominal value must be finite")
        if self.integer and self.nominal != math.floor(self.nominal):
            raise ValueError(f"{self.name}: an integer parameter needs an integer nominal")
        if self.choices is not None and self.nominal not in self.choices:
            raise ValueError(f"{self.name}: the nominal value must be one of the choices")
        if self.choices is None and self.unit <= 0:
            raise ValueError(f"{self.name}: a zero nominal needs a positive scale")
        if self.minimum is not None and self.nominal < self.minimum:
            raise ValueError(f"{self.name}: the nominal value is below the minimum")

    @property
    def unit(self) -> float:
        """What a perturbation level is a share of."""
        return abs(self.nominal) if self.scale is None else self.scale

    def neighbours(self, level: float) -> tuple[float, ...]:
        """The values at `level`, down then up; a side without a valid value is left out."""
        values = []
        for direction in (-1, 1):
            value = self._snap(self.nominal + direction * level * self.unit, direction)
            if value is not None and (self.minimum is None or value >= self.minimum):
                values.append(value)
        return tuple(values)

    def cast(self, value: float) -> float:
        """`value` as the strategy receives it (an int for an integer parameter)."""
        return int(value) if self.integer else value

    def _snap(self, target: float, direction: int) -> float | None:
        if self.choices is not None:
            side = [float(c) for c in self.choices if (c - self.nominal) * direction > 0]
            if not side:
                return None
            return min(side, key=lambda c: (abs(c - target), abs(c - self.nominal)))
        if self.integer:
            value = float(math.floor(target + 0.5))
            return self.nominal + direction if value == self.nominal else value
        return target


@dataclass(frozen=True)
class PerturbationResult:
    """Net Sharpe ratios around a strategy's nominal parameters (module docstring)."""

    parameters: tuple[Parameter, ...]
    levels: tuple[float, ...]
    periods_per_year: int
    #: One row per distinct evaluated point: a column per parameter, ``sharpe`` (annualized net)
    #: and ``net_return`` (the sum of the per-period net returns).
    points: pd.DataFrame
    _sharpe: dict[Point, float] = field(repr=False, compare=False)

    @property
    def names(self) -> tuple[str, ...]:
        """The parameters' names."""
        return tuple(p.name for p in self.parameters)

    @property
    def nominal(self) -> Point:
        """The nominal point."""
        return tuple(float(p.nominal) for p in self.parameters)

    @property
    def nominal_sharpe(self) -> float:
        """Annualized net Sharpe ratio at the nominal parameters."""
        return self._sharpe[self.nominal]

    def sharpe(self, values: Mapping[str, float]) -> float:
        """The net Sharpe ratio at an evaluated point (parameters missing take their nominal)."""
        point = tuple(float(values.get(p.name, p.nominal)) for p in self.parameters)
        return self._sharpe[point]

    def one_at_a_time(self) -> pd.DataFrame:
        """Each parameter moved down and up at each level, the others at nominal."""
        rows = []
        for level in self.levels:
            for i, parameter in enumerate(self.parameters):
                for value in parameter.neighbours(level):
                    point = _replace(self.nominal, i, value)
                    rows.append(
                        {
                            "parameter": parameter.name,
                            "level": level,
                            "side": "down" if value < parameter.nominal else "up",
                            "value": value,
                            "sharpe": self._sharpe[point],
                        }
                    )
        return pd.DataFrame(rows, columns=["parameter", "level", "side", "value", "sharpe"])

    def neighbourhood(self, level: float) -> pd.DataFrame:
        """The joint neighbourhood at `level`: one row per point, the nominal point excluded."""
        level = self._level(level)
        points = _joint(self.parameters, level)
        frame = pd.DataFrame(points, columns=list(self.names))
        frame["sharpe"] = [self._sharpe[p] for p in points]
        return frame

    def profitable_share(self, level: float) -> float:
        """Share of the joint neighbourhood at `level` with a positive net Sharpe ratio."""
        sharpe = self.neighbourhood(level)["sharpe"].to_numpy(np.float64)
        return float(np.mean(np.nan_to_num(sharpe, nan=0.0) > 0)) if len(sharpe) else math.nan

    def median_to_nominal(self, level: float) -> float:
        """Median neighbourhood Sharpe over the nominal Sharpe (NaN if that is not positive)."""
        nominal = self.nominal_sharpe
        if not nominal > 0:
            return math.nan
        return float(np.nanmedian(self.neighbourhood(level)["sharpe"])) / nominal

    def summary(self) -> pd.DataFrame:
        """Per level: the joint neighbourhood's size, profitable share, median, worst and
        median-to-nominal ratio, and the one-at-a-time profitable share."""
        single = self.one_at_a_time()
        rows = []
        for level in self.levels:
            hood = self.neighbourhood(level)["sharpe"]
            alone = single.loc[np.isclose(single["level"], level), "sharpe"]
            rows.append(
                {
                    "level": level,
                    "points": len(hood),
                    "profitable_share": self.profitable_share(level),
                    "median_sharpe": float(hood.median()),
                    "worst_sharpe": float(hood.min()),
                    "median_to_nominal": self.median_to_nominal(level),
                    "one_at_a_time_profitable_share": float((alone.fillna(0.0) > 0).mean()),
                }
            )
        return pd.DataFrame(rows).set_index("level")

    def heatmap(self, x: str, y: str) -> pd.DataFrame:
        """Net Sharpe over the grid of `x` (columns) and `y` (rows), the others at nominal."""
        ix, iy = self.names.index(x), self.names.index(y)
        xs, ys = _axis(self.parameters[ix], self.levels), _axis(self.parameters[iy], self.levels)
        grid = [
            [self._sharpe[_replace(_replace(self.nominal, ix, vx), iy, vy)] for vx in xs]
            for vy in ys
        ]
        return pd.DataFrame(grid, index=pd.Index(ys, name=y), columns=pd.Index(xs, name=x))

    def gate_check(self, gates: GatesConfig) -> GateCheck:
        """R2 ``parameter_neighbourhood``: the profitable share at the gate's perturbation."""
        hood = gates.r2_validated.parameter_neighbourhood
        criterion = gates.criterion("R2", "parameter_neighbourhood.profitable_share_min")
        return criterion.check(self.profitable_share(hood.perturbation))

    def _level(self, level: float) -> float:
        for known in self.levels:
            if math.isclose(known, level):
                return known
        raise ValueError(f"level {level} was not evaluated; levels are {list(self.levels)}")


def perturb(
    evaluate: Evaluate,
    parameters: Sequence[Parameter],
    *,
    periods_per_year: int,
    levels: Iterable[float] = DEFAULT_LEVELS,
    heatmaps: bool = True,
) -> PerturbationResult:
    """Evaluate a strategy around its nominal parameters (module docstring).

    Args:
        evaluate: Maps parameter values (by name) to the strategy's per-period net returns.
        parameters: The perturbed parameters with their nominal values.
        periods_per_year: Annualization of the Sharpe ratio (``backtest.periods_per_year``).
        levels: Perturbation levels, shares of each parameter's scale, in (0, 1).
        heatmaps: Also evaluate every pair of parameters over all levels.

    Raises:
        ValueError: for no or duplicate parameters, a level outside (0, 1), or returns with
            missing values.
    """
    params = tuple(parameters)
    names = [p.name for p in params]
    if not params or len(set(names)) != len(names):
        raise ValueError("perturb needs parameters with distinct names")
    grid_levels = tuple(sorted({float(level) for level in levels}))
    if not grid_levels or not all(0 < level < 1 for level in grid_levels):
        raise ValueError("perturbation levels must lie in (0, 1)")
    nominal = tuple(float(p.nominal) for p in params)

    wanted: dict[Point, None] = {nominal: None}
    for level in grid_levels:
        for i, parameter in enumerate(params):
            for value in parameter.neighbours(level):
                wanted[_replace(nominal, i, value)] = None
        for point in _joint(params, level):
            wanted[point] = None
    if heatmaps:
        for i, j in itertools.combinations(range(len(params)), 2):
            for vi in _axis(params[i], grid_levels):
                for vj in _axis(params[j], grid_levels):
                    wanted[_replace(_replace(nominal, i, vi), j, vj)] = None

    root = math.sqrt(periods_per_year)
    sharpe: dict[Point, float] = {}
    rows = []
    for point in wanted:
        values = {p.name: p.cast(v) for p, v in zip(params, point, strict=True)}
        returns = np.asarray(evaluate(values), dtype=np.float64)
        sharpe[point] = sharpe_ratio(returns) * root
        rows.append((*point, sharpe[point], float(np.sum(returns))))
    points = pd.DataFrame(rows, columns=[*names, "sharpe", "net_return"])
    return PerturbationResult(params, grid_levels, periods_per_year, points, sharpe)


def _replace(point: Point, index: int, value: float) -> Point:
    return (*point[:index], float(value), *point[index + 1 :])


def _joint(parameters: tuple[Parameter, ...], level: float) -> list[Point]:
    """Every combination of {down, nominal, up} at `level`, the nominal point excluded."""
    options = [(float(p.nominal), *p.neighbours(level)) for p in parameters]
    nominal = tuple(o[0] for o in options)
    return [point for point in itertools.product(*options) if point != nominal]


def _axis(parameter: Parameter, levels: tuple[float, ...]) -> list[float]:
    values = {float(parameter.nominal)}
    for level in levels:
        values.update(parameter.neighbours(level))
    return sorted(values)
