"""Combine market data + forecast model into ranked trade signals."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from ..config import (MIN_EDGE, STATIONS, MIN_PRICE, MAX_PRICE,
                      MIN_HOURS_TO_RESOLVE, CALIBRATION, NOWCAST)
from ..forecast.openmeteo import fetch_max_temp_distribution, MaxTempForecast
from ..forecast.model import yes_probability, apply_calibration
from ..forecast import nowcast as nowcast_mod
from ..polymarket.gamma import TempMarket
from .sizing import stake_usdc


@dataclass
class Signal:
    market: TempMarket
    side: str            # "Yes" or "No" — the outcome we BUY
    token_id: str
    model_prob: float    # our P(Yes resolves)
    price: float         # price we'd pay for the side we buy
    edge: float          # model edge on the side we buy
    stake: float         # USDC to deploy
    station: str = ""
    date: str = ""
    fc_mean: float = 0.0  # calibrated ensemble mean max-temp at entry
    fc_std: float = 0.0
    fc_n: int = 0         # ensemble members behind the forecast (0 for nowcast)

    def __str__(self) -> str:
        return (f"BUY {self.side:3} @ {self.price:.3f} "
                f"(model P(Yes)={self.model_prob:.3f}, edge={self.edge:+.3f}, "
                f"${self.stake:>6.2f})  {self.market.question[:60]}")


def _apply_calibration(fc: MaxTempForecast) -> MaxTempForecast:
    return apply_calibration(fc, CALIBRATION)


# A "scorer" is either an ensemble MaxTempForecast or a Tier-3 Nowcast; both can
# answer P(Yes) for a bucket and expose a calibrated mean/std for the signal log.
def _p_yes(scorer, kind: str, deg: int) -> float:
    if isinstance(scorer, MaxTempForecast):
        return yes_probability(scorer, kind, deg)
    return nowcast_mod.yes_probability(scorer, kind, deg)


def _mean_std(scorer) -> tuple[float, float]:
    if isinstance(scorer, MaxTempForecast):
        return scorer.mean - scorer.bias_c, scorer.std
    return scorer.mean, scorer.std


def _n_members(scorer) -> int:
    if isinstance(scorer, MaxTempForecast):
        return int(scorer.members_max_c.size)
    return 0  # nowcast is sample-based, not ensemble-member-based


def _is_today(date: str, tz: str) -> bool:
    try:
        return date == dt.datetime.now(ZoneInfo(tz)).date().isoformat()
    except Exception:  # noqa: BLE001
        return False


def _hours_to_resolve(end_date: str) -> float:
    try:
        end = dt.datetime.fromisoformat(end_date.replace("Z", "+00:00"))
    except ValueError:
        return 1e9
    return (end - dt.datetime.now(dt.timezone.utc)).total_seconds() / 3600


def is_tradable(m: TempMarket) -> bool:
    """Skip markets that are mid-resolution (pinned price / dead liquidity) or
    too close to settlement for a forecast edge to be real."""
    if _hours_to_resolve(m.end_date) < MIN_HOURS_TO_RESOLVE:
        return False
    if not (MIN_PRICE <= m.yes_price <= MAX_PRICE):
        return False
    return True


def evaluate_market(m: TempMarket, fc,
                    min_edge: float = MIN_EDGE) -> Signal | None:
    if not is_tradable(m):
        return None
    p_yes = _p_yes(fc, m.bucket_kind, m.threshold_c)

    # Edge on buying Yes vs buying No; take whichever is positive & bigger.
    yes_edge = p_yes - m.yes_price
    no_edge = (1.0 - p_yes) - m.no_price

    fc_mean, fc_std = _mean_std(fc)
    meta = dict(station=fc.station_code, date=fc.date,
                fc_mean=fc_mean, fc_std=fc_std, fc_n=_n_members(fc))
    if yes_edge >= no_edge and yes_edge >= min_edge:
        return Signal(m, "Yes", m.yes_token_id, p_yes, m.yes_price, yes_edge,
                      stake_usdc(p_yes, m.yes_price), **meta)
    if no_edge > yes_edge and no_edge >= min_edge:
        return Signal(m, "No", m.no_token_id, p_yes, m.no_price, no_edge,
                      stake_usdc(1.0 - p_yes, m.no_price), **meta)
    return None


def _build_scorer(station: str, date: str):
    """Ensemble forecast for (station, date) — or, when NOWCAST is on and the
    market resolves today, a Tier-3 nowcast that folds in live station obs."""
    s = STATIONS[station]
    if NOWCAST and _is_today(date, s["tz"]):
        try:
            return nowcast_mod.build_nowcast(station, date)
        except Exception as e:  # noqa: BLE001
            print(f"  ! nowcast failed for {station} {date}: {e}; using ensemble")
    return _apply_calibration(fetch_max_temp_distribution(
        s["lat"], s["lon"], date, s["tz"], station))


def generate_signals(markets: list[TempMarket],
                     min_edge: float = MIN_EDGE) -> list[Signal]:
    """Fetch one forecast per (station, date) and score every bucket market."""
    cache: dict[tuple[str, str], object] = {}
    signals: list[Signal] = []

    for m in markets:
        station = m.station_code
        if not station or station not in STATIONS:
            continue
        date = m.end_date[:10]
        key = (station, date)
        if key not in cache:
            try:
                cache[key] = _build_scorer(station, date)
            except Exception as e:  # noqa: BLE001
                print(f"  ! forecast failed for {station} {date}: {e}")
                cache[key] = None
        fc = cache[key]
        if fc is None:
            continue
        sig = evaluate_market(m, fc, min_edge)
        if sig and sig.stake > 0:
            signals.append(sig)

    signals.sort(key=lambda s: s.edge, reverse=True)
    return signals
