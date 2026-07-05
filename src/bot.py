"""Main bot loop.

  1. Discover open daily high-temperature markets on Polymarket.
  2. For each station/date, pull an ensemble max-temp forecast.
  3. Convert the forecast into bucket probabilities matching the resolution rule.
  4. Compare to market prices, rank by edge, size with fractional Kelly.
  5. Place orders (DRY_RUN by default — logs instead of sending).

Run once:   python -m src.bot
Loop:       python -m src.bot --loop 600    # every 10 min
"""
from __future__ import annotations

import argparse
import json
import time

from .config import MIN_EDGE, DRY_RUN, ROOT
from .polymarket.gamma import fetch_open_temperature_events, parse_event
from .polymarket.clob import place_order
from .strategy.edge import generate_signals

# Idempotency ledger of token_ids we have already sent a live order for. The
# live path (unlike the paper engine) has no position state, so without this a
# repeated scan — a loop tick or a daily timer that sees the same still-open
# market twice — would re-buy it every time and stack far past the per-market
# cap. Keyed by token_id (unique per bucket/day), so a token is bought at most
# once, ever. Only real (non-DRY_RUN) placements are recorded.
_PLACED_PATH = ROOT / "data" / "placed_tokens.json"


def _load_placed() -> set[str]:
    try:
        return set(json.loads(_PLACED_PATH.read_text()))
    except Exception:  # noqa: BLE001 — missing/corrupt ledger => start empty
        return set()


def _record_placed(token_id: str) -> None:
    placed = _load_placed()
    placed.add(token_id)
    _PLACED_PATH.parent.mkdir(parents=True, exist_ok=True)
    _PLACED_PATH.write_text(json.dumps(sorted(placed)))


def run_once() -> None:
    print(f"\n=== scan @ {time.strftime('%Y-%m-%d %H:%M:%S')} "
          f"(DRY_RUN={DRY_RUN}, MIN_EDGE={MIN_EDGE}) ===")
    events = fetch_open_temperature_events()
    markets = [m for ev in events for m in parse_event(ev)]
    print(f"discovered {len(events)} temperature events / {len(markets)} bucket markets")

    signals = generate_signals(markets)
    print(f"\n{len(signals)} actionable signal(s):")
    for s in signals:
        print(" ", s)

    placed = _load_placed()
    for s in signals:
        if s.token_id in placed:
            print(f"  skip (already ordered): {s.token_id[:10]}… {s.side}")
            continue
        place_order(s.token_id, "BUY", s.price, s.stake)
        if not DRY_RUN:
            _record_placed(s.token_id)
            placed.add(s.token_id)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", type=int, default=0,
                    help="seconds between scans; 0 = run once")
    args = ap.parse_args()

    if args.loop <= 0:
        run_once()
        return
    while True:
        try:
            run_once()
        except Exception as e:  # noqa: BLE001
            print(f"scan error: {e}")
        time.sleep(args.loop)


if __name__ == "__main__":
    main()
