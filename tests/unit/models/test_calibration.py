"""ML-002: calibration on validation rows improves ECE on miscalibrated scores; isotonic above
the plan's 1,000 rows, Platt otherwise; weights shrink a slope fitted on few independent rows."""

import numpy as np
import pytest

from xq.models.calibration import CalibrationError, Calibrator, fit_calibrator
from xq.validation.forecast_eval import ece, log_loss


def miscalibrated(n: int, seed: int) -> tuple[np.ndarray, np.ndarray]:
    """Outcomes drawn from true probabilities, and overconfident scores of them."""
    rng = np.random.default_rng(seed)
    truth = rng.uniform(0.1, 0.9, n)
    y = (rng.uniform(size=n) < truth).astype(float)
    logit = np.log(truth / (1 - truth))
    return 1 / (1 + np.exp(-3 * logit)), y  # three times too confident


@pytest.mark.parametrize(("n", "method"), [(500, "platt"), (5000, "isotonic")])
def test_calibration_on_validation_improves_ece_on_test(n: int, method: str) -> None:
    p_val, y_val = miscalibrated(n, 1)
    p_test, y_test = miscalibrated(20_000, 2)
    calibrator = fit_calibrator(p_val, y_val, "auto", isotonic_min_samples=1000)
    assert calibrator.method == method
    cal = calibrator.transform(p_test)
    assert ece(y_test, cal) < ece(y_test, p_test) / 3
    assert log_loss(y_test, cal) < log_loss(y_test, p_test)
    assert ((cal > 0) & (cal < 1)).all()


def test_uniqueness_weights_shrink_a_slope_fitted_on_few_independent_rows() -> None:
    p_val, y_val = miscalibrated(200, 3)
    full = fit_calibrator(p_val, y_val, "platt", isotonic_min_samples=1000)
    light = fit_calibrator(
        p_val, y_val, "platt", isotonic_min_samples=1000, sample_weight=np.full(200, 1 / 48)
    )
    probe = np.array([0.05, 0.95])
    spread_full = np.ptp(full.transform(probe))
    spread_light = np.ptp(light.transform(probe))
    assert spread_light < 0.95 * spread_full  # ~4 independent outcomes: towards the base rate


def test_calibrators_refuse_unusable_rows() -> None:
    with pytest.raises(CalibrationError, match="fit the calibrator"):
        Calibrator("platt").transform([0.5])
    with pytest.raises(CalibrationError, match="both classes"):
        Calibrator("platt").fit([0.2, 0.4], [1.0, 1.0])
    with pytest.raises(CalibrationError, match="one known probability"):
        Calibrator("isotonic").fit([0.2, np.nan], [0.0, 1.0])
    with pytest.raises(CalibrationError, match="non-negative"):
        Calibrator("platt").fit([0.2, 0.4], [0.0, 1.0], sample_weight=[1.0, -1.0])
    unchanged = Calibrator("none").fit([0.2, 0.4], [0.0, 1.0]).transform([0.3, np.nan])
    assert unchanged[0] == pytest.approx(0.3)
    assert np.isnan(unchanged[1])
