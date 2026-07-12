"""Tests for the three competitor-inspired additions: peer agreement signal,
the No-harvest sleeve, and the US-station regex fix."""
import datetime as dt

import numpy as np

from src.forecast.openmeteo import MaxTempForecast
from src.polymarket.gamma import TempMarket, _parse_station
from src.strategy import edge, peer_signal


def _market(yes_tok="Y", no_tok="N", kind="exact", deg=35,
            yes=0.01, no=0.95, days_ahead=3):
    end = (dt.datetime.now(dt.timezone.utc)
           + dt.timedelta(days=days_ahead)).isoformat()
    return TempMarket(
        event_slug="e", market_slug="m", question="Will the highest temperature "
        f"in Tokyo be {deg}°C?", condition_id="c", yes_token_id=yes_tok,
        no_token_id=no_tok, yes_price=yes, no_price=no, bucket_kind=kind,
        threshold_c=deg, station_code="RJTT", end_date=end)


def _forecast(mean=25.0, n=30, spread=1.0):
    members = np.linspace(mean - spread, mean + spread, n)
    return MaxTempForecast(station_code="RJTT", date="2026-06-08",
                           members_max_c=members)


# ---- peer agreement -------------------------------------------------------
def test_peer_agreement_labels():
    m = _market(yes_tok="Y", no_tok="N")
    confirm_yes = {"Y": {"net": 100.0}}
    assert peer_signal.agreement(confirm_yes, m, "Yes") == "confirm"
    assert peer_signal.agreement(confirm_yes, m, "No") == "against"
    peer_no = {"N": {"net": 50.0}}
    assert peer_signal.agreement(peer_no, m, "No") == "confirm"
    both = {"Y": {"net": 50.0}, "N": {"net": 50.0}}
    assert peer_signal.agreement(both, m, "Yes") == "mixed"
    assert peer_signal.agreement({}, m, "Yes") == "-"
    # dust below MIN_PEER_USDC is ignored
    assert peer_signal.agreement({"Y": {"net": 1.0}}, m, "Yes") == "-"


def test_peer_size_multiplier():
    assert peer_signal.size_multiplier("confirm") > 1.0
    assert peer_signal.size_multiplier("against") < 1.0
    assert peer_signal.size_multiplier("-") == 1.0
    assert peer_signal.size_multiplier("mixed") == 1.0


# ---- No-harvest sleeve ----------------------------------------------------
def test_no_harvest_takes_high_confidence_no(monkeypatch):
    # A 35°C bucket when the forecast says ~25°C: P(Yes) ~ 0. Yes price (0.01) is
    # below MIN_PRICE, so the normal lane rejects it; the sleeve should take No.
    monkeypatch.setattr(edge, "NO_HARVEST", True)
    m = _market(deg=35, yes=0.01, no=0.95)
    fc = _forecast(mean=25.0)
    sig = edge.evaluate_market(m, fc)
    assert sig is not None
    assert sig.side == "No" and sig.sleeve == "no_harvest"
    assert sig.stake <= edge.NO_HARVEST_STAKE
    assert sig.model_prob < edge.NO_HARVEST_MAX_P


def test_no_harvest_off_by_default(monkeypatch):
    monkeypatch.setattr(edge, "NO_HARVEST", False)
    m = _market(deg=35, yes=0.01, no=0.95)
    assert edge.evaluate_market(m, _forecast(mean=25.0)) is None


def test_no_harvest_skips_when_model_not_confident(monkeypatch):
    # Forecast centered ON the bucket -> P(Yes) high -> not a "favorite No".
    monkeypatch.setattr(edge, "NO_HARVEST", True)
    m = _market(deg=25, yes=0.30, no=0.70)
    sig = edge.evaluate_market(m, _forecast(mean=25.0))
    assert sig is None or sig.sleeve != "no_harvest"


# ---- tail-confidence gate on No entries -----------------------------------
def test_no_entry_blocked_in_miscalibrated_midband():
    # Model P(Yes) ≈ 0.24 for the bucket one degree above the mean: the No side
    # shows a nominal edge (0.76 vs a 0.68 price) but the mid-band is where the
    # model's calibration historically failed — the gate must reject it.
    m = _market(deg=26, yes=0.30, no=0.68)
    assert edge.evaluate_market(m, _forecast(mean=25.0)) is None


def test_no_entry_allowed_in_tail():
    # Far-tail bucket (P(Yes) ~ 0) with room in the No price: gate passes.
    m = _market(deg=29, yes=0.10, no=0.85)
    sig = edge.evaluate_market(m, _forecast(mean=25.0))
    assert sig is not None and sig.side == "No"
    assert sig.model_prob <= edge.MAX_PYES_FOR_NO


# ---- US station regex -----------------------------------------------------
def test_station_regex_handles_us_and_intl():
    assert _parse_station(
        "wunderground.com/history/daily/us/ny/new-york-city/KLGA.") == "KLGA"
    assert _parse_station("wunderground.com/history/daily/jp/tokyo/RJTT") == "RJTT"
    assert _parse_station(
        "https://www.wunderground.com/history/daily/kr/seoul/RKSI") == "RKSI"
