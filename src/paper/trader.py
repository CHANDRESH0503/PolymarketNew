"""Paper-trading daemon.

Each tick:
  1. discover markets + generate signals (same engine the live bot will use)
  2. paper-fill new signals
  3. mark all open positions to live prices, settle resolved ones
  4. snapshot equity, record signals for the dashboard

    python -m src.paper.trader              # one tick
    python -m src.paper.trader --loop 900   # every 15 min for the week-long run
"""
from __future__ import annotations

import argparse
import time

from ..config import MIN_EDGE, PEER_SIGNAL, ARB_SCAN, ARB_MIN_PROFIT
from ..polymarket.gamma import fetch_open_temperature_events, parse_event
from ..strategy.edge import generate_signals
from ..strategy import peer_signal, arbitrage
from .engine import PaperBroker
from .forecast_cache import refresh_forecast_cache, cache_scorer
from . import store


def _scan_arb(broker: PaperBroker, events: list[dict]) -> int:
    """Record coherence-arb opportunities (Σ best-ask(YES) < 1) for the dashboard.
    Detection only — real execution is ARB_EXECUTE-gated in arbitrage.py."""
    ops = []
    for ev in events:
        try:
            ops += arbitrage.scan_event(ev, min_edge=ARB_MIN_PROFIT)
        except Exception:  # noqa: BLE001 — a flaky book must not break the tick
            continue
    ops.sort(key=lambda o: o.est_profit, reverse=True)
    store.record_arb_ops(broker.con, ops)
    return len(ops)


def tick(broker: PaperBroker) -> None:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    events = fetch_open_temperature_events()
    markets = [m for ev in events for m in parse_event(ev)]
    # Single upstream call site: fetch + persist forecasts here, then score the
    # signals from those same objects (the dashboard reads the persisted copies).
    scorers = refresh_forecast_cache(broker.con, markets)
    peer_book = peer_signal.fetch_peer_book() if PEER_SIGNAL else None
    signals = generate_signals(markets, scorer_for=cache_scorer(scorers),
                               peer_book=peer_book)
    n_arb = _scan_arb(broker, events) if ARB_SCAN else 0

    broker.prefetch_books([s.token_id for s in signals])   # for depth-aware fills
    taken = set()
    for s in signals:
        broker.log_forecast(s)          # record every forecast for calibration
        if broker.execute(s):
            taken.add(s.token_id)
            print(f"  FILLED {s}")
    broker.record_signals(signals, taken)
    broker.mark_and_settle()
    filled_actuals = broker.backfill_actuals()
    if filled_actuals:
        print(f"  backfilled {filled_actuals} actual max-temp(s)")

    eq = broker.con.execute(
        "SELECT equity, realized, unrealized FROM equity ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    opens = broker.con.execute(
        "SELECT COUNT(*) c FROM fills WHERE status='open'").fetchone()["c"]
    nh = sum(1 for s in signals if s.sleeve == "no_harvest")
    print(f"[{stamp}] signals={len(signals)} (no_harvest={nh}) filled={len(taken)} "
          f"arb={n_arb} open={opens} equity=${eq['equity']:.2f} "
          f"(real ${eq['realized']:.2f} / unreal ${eq['unrealized']:.2f})")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", type=int, default=0)
    args = ap.parse_args()
    broker = PaperBroker()
    if args.loop <= 0:
        tick(broker)
        return
    while True:
        try:
            tick(broker)
        except Exception as e:  # noqa: BLE001
            print(f"tick error: {e}")
        time.sleep(args.loop)


if __name__ == "__main__":
    main()
