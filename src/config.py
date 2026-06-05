"""Central configuration: env vars + station registry."""
from __future__ import annotations

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _f(key: str, default: float) -> float:
    return float(os.getenv(key, default))


# Strategy knobs
MIN_EDGE = _f("MIN_EDGE", 0.07)
KELLY_FRACTION = _f("KELLY_FRACTION", 0.25)
MAX_STAKE_PER_MARKET = _f("MAX_STAKE_PER_MARKET", 100)
BANKROLL = _f("BANKROLL", 2000)
DRY_RUN = os.getenv("DRY_RUN", "1") == "1"

# Fraction of bankroll always kept in cash (never deployed). A reserve buffer so
# the book can't drift to 100%-invested with no dry powder.
CASH_BUFFER = _f("CASH_BUFFER", 0.10)
# Max fraction of bankroll committed to any single resolution-day's markets, so
# one day can't eat the whole book and starve the next day (which is already
# trading). Positions recycle daily, so this keeps dry powder for each new date.
MAX_DAY_FRACTION = _f("MAX_DAY_FRACTION", 0.5)
# Paper fills walk the live order book (depth + slippage) instead of magically
# filling the whole size at the quoted price. Falls back to quoted-price fills if
# the book is unavailable.
PAPER_DEPTH = os.getenv("PAPER_DEPTH", "1") == "1"

# Tradability filters. Markets within a few hours of resolution are effectively
# decided (prices pinned to 0.001/0.999, no liquidity), so forecast "edge" there
# is fake. Only trade a sane price band and a minimum horizon.
MIN_PRICE = _f("MIN_PRICE", 0.03)
MAX_PRICE = _f("MAX_PRICE", 0.97)
MIN_HOURS_TO_RESOLVE = _f("MIN_HOURS_TO_RESOLVE", 8)

# Auto-execution gates. Each is an EXTRA opt-in on top of DRY_RUN+PK: even with
# DRY_RUN=0 and a funded wallet, the arb / LP bots only send orders when their
# own flag is set. Default off.
ARB_EXECUTE = os.getenv("ARB_EXECUTE", "0") == "1"
ARB_MIN_PROFIT = _f("ARB_MIN_PROFIT", 0.02)        # min basket edge to act on
ARB_MAX_CAPITAL = _f("ARB_MAX_CAPITAL", 200)       # max USDC deployed per basket
LP_EXECUTE = os.getenv("LP_EXECUTE", "0") == "1"
LP_SIZE = _f("LP_SIZE", 50)                        # shares per maker quote
LP_EVENTS = os.getenv("LP_EVENTS", "")             # comma-sep slugs; blank = held

# Tier 3 — intraday nowcasting. When on, same-day markets are scored from a
# nowcast that folds the running observed station max (the resolution source) in
# as a hard floor and only forecasts the remaining hours, sharpening as the
# afternoon peak passes. Off by default (it's the unproven "is the market slow?"
# alpha hunt; ensemble-only is the safe baseline).
NOWCAST = os.getenv("NOWCAST", "0") == "1"
NOWCAST_RESID_SIGMA = _f("NOWCAST_RESID_SIGMA", 0.6)   # °C residual on remaining-hours max
NOWCAST_SAMPLES = int(_f("NOWCAST_SAMPLES", 4000))     # MC samples for the daily-max dist
CORR_KELLY = os.getenv("CORR_KELLY", "0") == "1"       # covariance-shrink simultaneous sizing
CORR_KELLY_RHO = _f("CORR_KELLY_RHO", 0.3)             # assumed cross-bet outcome correlation

# Smart-money agreement signal. Cross-check our signals against proven weather
# traders' live entries (matched by Polymarket token id). Advisory by default —
# it tags each signal confirm/against and nudges size — never a blind copy.
PEER_SIGNAL = os.getenv("PEER_SIGNAL", "0") == "1"
# Default peer: automatedAItradingbot (0xd8f8…0f11) — trades our exact Asian
# universe on our ~14h horizon and is currently profitable (see WALLETS.md).
PEER_WALLETS = [w.strip() for w in os.getenv(
    "PEER_WALLETS", "0xd8f8c13644ea84d62e1ec88c5d1215e436eb0f11").split(",") if w.strip()]
PEER_LOOKBACK_HOURS = _f("PEER_LOOKBACK_HOURS", 36)    # how far back to read peer trades

# No-favorite harvesting sleeve (low-variance lane, modeled on HondaCivic but
# entered the day before, not at T-1h). Take No on buckets our CALIBRATED model
# says are near-impossible (P(Yes) <= NO_HARVEST_MAX_P) yet the No price still has
# room — small, capped tickets. Deliberately re-includes the cheap-Yes buckets the
# normal MIN_PRICE band excludes, gated on model confidence instead.
NO_HARVEST = os.getenv("NO_HARVEST", "0") == "1"
NO_HARVEST_MAX_P = _f("NO_HARVEST_MAX_P", 0.03)        # max model P(Yes) to call it a "favorite No"
NO_HARVEST_STAKE = _f("NO_HARVEST_STAKE", 25)          # hard cap per sleeve ticket (USDC)

# Coherence arbitrage surfacing in the paper loop: scan events for Σ best-ask(YES)
# < 1 baskets and record them for the dashboard. Real execution stays gated by
# ARB_EXECUTE (live only); this just flags the opportunities.
ARB_SCAN = os.getenv("ARB_SCAN", "1") == "1"

# Forecast freshness: the daemon re-fetches a station's ensemble from Open-Meteo
# only when the cached copy is older than this. The underlying models update
# ~every 6h, so re-pulling every 15-min tick is wasteful — a 90-min TTL keeps the
# daemon's API footprint well under the free-tier caps while staying current.
# (The intraday nowcast still refreshes every tick — it must fold in new obs.)
FORECAST_TTL = _f("FORECAST_TTL", 5400)   # seconds (default 90 min)

# Wallet / CLOB
PK = os.getenv("PK", "")
POLY_PROXY_ADDRESS = os.getenv("POLY_PROXY_ADDRESS", "")
CLOB_API_KEY = os.getenv("CLOB_API_KEY", "")
CLOB_API_SECRET = os.getenv("CLOB_API_SECRET", "")
CLOB_API_PASSPHRASE = os.getenv("CLOB_API_PASSPHRASE", "")

# Telegram notifications
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Endpoints
GAMMA_API = "https://gamma-api.polymarket.com"
DATA_API = "https://data-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"


def load_stations() -> dict:
    with open(ROOT / "config" / "cities.yaml") as fh:
        return yaml.safe_load(fh)["stations"]


def load_calibration() -> dict:
    """Per-station {bias, sigma} learned by scripts/calibrate.py. Empty if not
    yet calibrated."""
    path = ROOT / "config" / "calibration.yaml"
    if not path.exists():
        return {}
    with open(path) as fh:
        return yaml.safe_load(fh) or {}


STATIONS = load_stations()
CALIBRATION = load_calibration()
