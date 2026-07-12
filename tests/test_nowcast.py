"""Tier 3 sanity checks: intraday nowcaster + correlation-aware Kelly."""
import datetime as dt
from zoneinfo import ZoneInfo

import numpy as np

from src.forecast import nowcast as nc
from src.strategy.sizing import (correlated_stakes, correlation_kelly,
                                  kelly_fraction)

TZ = "Asia/Seoul"
DATE = "2026-06-05"


def _hourly(values):
    """24 hourly local timestamps + a couple of identical 'members'."""
    times = [f"{DATE}T{h:02d}:00" for h in range(len(values))]
    members = {"temperature_2m": np.array(values, dtype=float),
               "temperature_2m_member01": np.array(values, dtype=float)}
    return times, members


def _build(values, observed_max, now_hour, **kw):
    times, members = _hourly(values)
    now = dt.datetime(2026, 6, 5, now_hour, 30, tzinfo=ZoneInfo(TZ))
    return nc.nowcast_from_parts("RKSI", DATE, times, members,
                                 observed_max, f"{DATE} {now_hour:02d}:00",
                                 tz=TZ, now=now, rng=np.random.default_rng(0),
                                 n_samples=8000, **kw)


def test_buckets_sum_to_one():
    vals = [20 + 6 * np.sin((h - 3) / 24 * 2 * np.pi) for h in range(24)]
    n = _build(vals, observed_max=24.0, now_hour=12)
    total = sum(nc.prob_exact(n, d) for d in range(0, 50))
    assert abs(total - 1.0) < 1e-9


def test_observed_floor_is_hard():
    # Remaining hours are cool (15°C), but 26°C has already been observed:
    # nothing below 26 should carry mass.
    vals = [15.0] * 24
    n = _build(vals, observed_max=26.0, now_hour=14)
    assert n.observed_max_c == 26.0
    assert nc.prob_lte(n, 25) < 1e-6
    assert nc.prob_gte(n, 26) > 0.99


def test_distribution_collapses_as_day_progresses():
    # Same forecast; later in the afternoon the remaining hours can't beat the
    # observed floor, so more mass locks onto it.
    vals = [18.0 if h < 12 else 22.0 - (h - 14) ** 2 * 0.3 for h in range(24)]
    early = _build(vals, observed_max=23.0, now_hour=10)
    late = _build(vals, observed_max=23.0, now_hour=17)
    assert late.floor_locked > early.floor_locked
    assert late.std <= early.std + 1e-9


def test_no_remaining_hours_pins_to_floor():
    n = _build([20.0] * 24, observed_max=25.0, now_hour=23)
    assert n.n_remaining_hours == 0
    assert nc.prob_exact(n, 25) == 1.0


def test_no_obs_falls_back_to_forecast():
    # No observed floor yet (early morning) -> distribution is the remaining-hours
    # forecast, still produces valid probabilities.
    vals = [27.0] * 24
    n = _build(vals, observed_max=None, now_hour=6)
    assert n.observed_max_c is None
    assert abs(sum(nc.prob_exact(n, d) for d in range(0, 50)) - 1.0) < 1e-9
    assert nc.prob_exact(n, 27) > 0.4


def test_bias_shifts_forecast_down():
    vals = [30.0] * 24
    cold = _build(vals, observed_max=None, now_hour=6, bias_c=0.0)
    warm_bias = _build(vals, observed_max=None, now_hour=6, bias_c=3.0)
    assert warm_bias.mean < cold.mean - 2.0


# --- correlation-aware Kelly ---------------------------------------------

def test_correlation_shrinks_vs_independent():
    probs = [0.6, 0.6, 0.6]
    prices = [0.5, 0.5, 0.5]
    indep = correlation_kelly(probs, prices, rho=0.0)
    corr = correlation_kelly(probs, prices, rho=0.8)
    # Positive correlation should shrink each correlated leg.
    assert np.all(corr < indep + 1e-9)
    assert corr.sum() < indep.sum()


def test_corr_kelly_independent_is_mu_over_var():
    # The continuous (log-normal) approximation f*=Σ⁻¹μ reduces, with no
    # correlation, to per-leg μ/σ²  (μ=(p-q)/q, σ²=p(1-p)/q²). It is an
    # approximation of — not identical to — exact discrete Kelly, but agrees in
    # sign and grows with edge.
    p, q = 0.7, 0.5
    expected = ((p - q) / q) / (p * (1 - p) / q ** 2)
    f = correlation_kelly([p], [q], rho=0.0)[0]
    assert abs(f - expected) < 1e-6
    assert f > 0 and kelly_fraction(p, q) > 0   # both flag the same positive edge


def test_no_edge_legs_clamp_to_zero():
    f = correlation_kelly([0.4, 0.7], [0.5, 0.5], rho=0.0)
    assert f[0] == 0.0 and f[1] > 0.0


def test_correlated_stakes_respect_bankroll_and_cap():
    stakes = correlated_stakes([0.9, 0.9], [0.2, 0.2],
                               bankroll=1000, fraction=1.0, cap=100, rho=0.0)
    assert all(s <= 100 for s in stakes)
    assert sum(stakes) <= 1000


def test_groups_isolate_unrelated_cities_from_crowding():
    """A cheap, high-edge Yes leg in its own (station, date) group must not be
    crushed just because many unrelated No legs (other cities/dates) are also
    in the book. Without `groups`, the flat equicorrelation pools every leg
    together — a cheap Yes bet has much higher implied sigma (scales ~1/price)
    than the many expensive near-certain No bets, so the shared linear solve
    drives it toward zero regardless of its own edge. Grouping by (station,
    date) confines the covariance shrinkage to legs that share one real
    weather realization."""
    # Many No legs on unrelated (station, date) pairs, all high price / low
    # implied variance, plus one isolated cheap Yes leg with a real edge.
    no_legs = [(0.9, 0.7)] * 10          # p=0.9 win, price=0.7 -> edge=0.2
    probs = [p for p, _ in no_legs] + [0.34]     # Yes leg: p=0.34
    prices = [q for _, q in no_legs] + [0.135]   # priced at 0.135 -> edge~0.2
    groups = [(f"CITY{i}", "2026-07-04") for i in range(10)] + [("CITYX", "2026-07-04")]

    ungrouped = correlated_stakes(probs, prices, bankroll=113, rho=0.3)
    grouped = correlated_stakes(probs, prices, bankroll=113, rho=0.3, groups=groups)

    assert ungrouped[-1] < 1.0            # reproduces the historical bug
    assert grouped[-1] > ungrouped[-1] * 3   # grouping meaningfully frees the Yes leg
    assert grouped[-1] >= 1.5              # clears MIN_STAKE_PER_MARKET, i.e. actually tradable
