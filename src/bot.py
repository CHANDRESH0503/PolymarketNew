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

from .config import (MIN_EDGE, DRY_RUN, ROOT, MIN_STAKE_PER_MARKET, BANKROLL,
                    PK, POLY_PROXY_ADDRESS, SIGNATURE_TYPE, CLOB_API,
                    CLOB_API_KEY, CLOB_API_SECRET, CLOB_API_PASSPHRASE,
                    STATIONS, CALIBRATION, NOWCAST,
                    PIN_EXIT, EXIT_PIN_PRICE, MIN_ORDER_SHARES)
from .polymarket.gamma import fetch_open_temperature_events, parse_event
from .polymarket.clob import place_order, sell_position, get_books, best_bid
from .polymarket import data_api
from .strategy import edge as edge_mod
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


# Pin-exit ledger: token_ids we have already sent a SELL for, so a resting or
# partially-filled exit isn't re-sent every scan. Mirrors the placed ledger.
_EXITED_PATH = ROOT / "data" / "exited_tokens.json"


def _load_exited() -> set[str]:
    try:
        return set(json.loads(_EXITED_PATH.read_text()))
    except Exception:  # noqa: BLE001 — missing/corrupt ledger => start empty
        return set()


def _record_exited(token_id: str) -> None:
    exited = _load_exited()
    exited.add(token_id)
    _EXITED_PATH.parent.mkdir(parents=True, exist_ok=True)
    _EXITED_PATH.write_text(json.dumps(sorted(exited)))


def harvest_pins() -> None:
    """Recycle capital out of decided markets. A winning ticket held to
    resolution stays frozen as conditional tokens (this bot has no on-chain
    redemption), so the live wallet's tradable USDC only ever shrinks. Once a
    held market pins — best bid >= EXIT_PIN_PRICE — the last cents of terminal
    value are not worth a day of frozen capital: sell into the bid and let the
    cash fund the next resolution day's entries. Runs before the buy scan so
    the freed balance is available to the same tick's orders."""
    if not (PIN_EXIT and POLY_PROXY_ADDRESS):
        return
    try:
        positions = data_api.get_positions(POLY_PROXY_ADDRESS)
    except Exception as e:  # noqa: BLE001
        print(f"  ! pin-exit: positions fetch failed: {e}")
        return
    exited = _load_exited()
    candidates = []
    for p in positions:
        token = str(p.get("asset") or "")
        size = float(p.get("size") or 0.0)
        cur = float(p.get("curPrice") or 0.0)
        # redeemable == already resolved: the CLOB book is closed, nothing to
        # sell into. (Recovering those needs on-chain redemption, out of scope.)
        if (not token or token in exited or p.get("redeemable")
                or cur < EXIT_PIN_PRICE or size < MIN_ORDER_SHARES):
            continue
        candidates.append((token, size, str(p.get("title") or "")))
    if not candidates:
        return
    try:
        books = get_books([t for t, _, _ in candidates])
    except Exception as e:  # noqa: BLE001
        print(f"  ! pin-exit: book fetch failed: {e}")
        return
    freed = 0.0
    for token, size, title in candidates:
        bid = best_bid(books.get(token))
        if not bid or bid[0] < EXIT_PIN_PRICE:
            continue          # marked at the pin but no real bid to sell into
        try:
            resp = sell_position(token, bid[0], size)
        except Exception as e:  # noqa: BLE001 — one bad exit must not stop the rest
            print(f"  ! pin-exit failed {token[:10]}…: {e}")
            continue
        if resp.get("skipped"):
            continue
        if not DRY_RUN:
            _record_exited(token)
        freed += size * bid[0]
        print(f"  pin-exit SELL {size:.2f} @ {bid[0]:.3f} — {title[:60]}")
    if freed:
        print(f"pin-exit: ~${freed:.2f} recycled back to cash")


def _live_equity() -> float | None:
    """Real wallet equity = collateral cash + open-position value.

    The live path must size Kelly against *actual capital*, exactly as the paper
    daemon sizes against paper equity — otherwise the two diverge. With the static
    config BANKROLL (100) the corr-Kelly book fragments across ~50 correlated legs
    and the marginal ones fall below MIN_STAKE_PER_MARKET, so live places almost
    nothing while paper (higher equity) fills them: the "paper trades but live
    doesn't" symptom. Sizing on the real ~$200 wallet lifts those legs over the
    floor. Returns None (→ caller falls back to BANKROLL) when DRY_RUN, creds are
    missing, or the balance call fails. Mirrors server._live_snapshot."""
    if DRY_RUN or not (PK and POLY_PROXY_ADDRESS):
        return None
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import (ApiCreds, BalanceAllowanceParams,
                                               AssetType)
        cl = ClobClient(CLOB_API, key=PK, chain_id=137,
                        signature_type=SIGNATURE_TYPE, funder=POLY_PROXY_ADDRESS)
        cl.set_api_creds(ApiCreds(CLOB_API_KEY, CLOB_API_SECRET, CLOB_API_PASSPHRASE))
        ba = cl.get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
        cash = int(ba["balance"]) / 1e6                 # USDC has 6 decimals
    except Exception as e:  # noqa: BLE001
        print(f"  ! live balance fetch failed ({e}); sizing on static BANKROLL")
        return None
    pos_value = 0.0
    try:
        for p in data_api.get_positions(POLY_PROXY_ADDRESS):
            cur = float(p.get("curPrice") or 0.0)
            val = float(p.get("currentValue") or 0.0)
            if cur <= 0.0 and val <= 0.005:             # skip resolved-to-zero dust
                continue
            pos_value += val
    except Exception as e:  # noqa: BLE001
        print(f"  ! live positions fetch failed ({e}); cash-only bankroll")
    return round(cash + pos_value, 2)


def _cached_scorer():
    """A `scorer_for(station, date)` that reuses the paper daemon's persisted
    forecast cache (paper.db `forecast_cache`), which the co-located trader
    refreshes every ~15 min.

    Two reasons the live path must NOT fetch its own forecasts fresh every run:
    (1) it double-hits Open-Meteo (paper already fetched the same distributions),
    and (2) a transient 429 then drops *every* station from the run — exactly what
    a fresh-fetch live run does, leaving live idle while paper keeps trading from
    its cache. Reading paper's cache makes live see the same forecasts paper does.
    Read-only on paper.db; falls back to a live fetch per (station, date) only when
    the cache has no entry, and returns None (→ caller uses the default live
    fetcher) when the paper DB isn't present at all."""
    try:
        from .paper import store as paper_store
        from .forecast import dist_cache
    except Exception:  # noqa: BLE001 — paper deps absent => default live fetch
        return None
    try:
        con = paper_store.connect()
    except Exception:  # noqa: BLE001 — no paper DB on this host
        return None

    def scorer_for(station: str, date: str):
        s = STATIONS.get(station)
        if not s:
            return None
        # Same-day + NOWCAST: fold in fresh obs with a live nowcast; on failure
        # (e.g. 429) fall through to the cached ensemble rather than dropping it.
        if NOWCAST and edge_mod._is_today(date, s["tz"]):
            try:
                from .forecast import nowcast as nowcast_mod
                return nowcast_mod.build_nowcast(station, date)
            except Exception:  # noqa: BLE001
                pass
        cached = paper_store.load_forecast_dist(con, station, date, "ensemble")
        if cached:
            return dist_cache.ensemble_from_payload(station, date, cached, CALIBRATION)
        try:
            return edge_mod._build_scorer(station, date)   # not cached: fetch live
        except Exception as e:  # noqa: BLE001
            print(f"  ! forecast unavailable for {station} {date}: {e}")
            return None

    return scorer_for


def run_once() -> None:
    print(f"\n=== scan @ {time.strftime('%Y-%m-%d %H:%M:%S')} "
          f"(DRY_RUN={DRY_RUN}, MIN_EDGE={MIN_EDGE}) ===")
    harvest_pins()
    events = fetch_open_temperature_events()
    markets = [m for ev in events for m in parse_event(ev)]
    print(f"discovered {len(events)} temperature events / {len(markets)} bucket markets")

    # Exclude markets we've already bought BEFORE sizing. Correlation-Kelly sizes
    # the whole book together (f* = Σ⁻¹μ) and concentrates the budget on the
    # highest-edge legs; if those are ones we already hold, they keep winning the
    # allocation while the un-held legs (e.g. a fresh resolution day's buckets) get
    # shrunk below MIN_STAKE_PER_MARKET and are never placed — the live book
    # freezes on its first fills. The paper engine dodges this only because it
    # entered those legs on an earlier tick when they were top-ranked and un-held.
    # Dropping already-placed markets here lets corr-Kelly's budget flow to the
    # un-held opportunities, so live progressively deploys across ticks like paper.
    placed = _load_placed()
    n_before = len(markets)
    markets = [m for m in markets
               if m.yes_token_id not in placed and m.no_token_id not in placed]
    print(f"{len(markets)} markets after excluding {n_before - len(markets)} "
          f"already-held (sizing pool)")

    eq = _live_equity()
    bankroll = eq if eq is not None else BANKROLL
    print(f"sizing bankroll = ${bankroll:.2f} "
          f"({'live wallet equity' if eq is not None else 'static BANKROLL'})")
    signals = generate_signals(markets, scorer_for=_cached_scorer(),
                               bankroll=bankroll)
    print(f"\n{len(signals)} actionable signal(s):")
    for s in signals:
        print(" ", s)

    n_placed = n_small = 0
    for s in signals:
        if s.token_id in placed:
            print(f"  skip (already ordered): {s.token_id[:10]}… {s.side}")
            continue
        # Mirror the paper engine (engine.execute): drop stakes below the per-market
        # floor. Correlation-Kelly re-sizing shrinks most correlated legs toward ~0,
        # and a $0 stake would build a 0-share order the CLOB rejects ("Invalid order
        # inputs") — which, unguarded, used to crash the whole batch on the first leg.
        if s.stake < MIN_STAKE_PER_MARKET:
            n_small += 1
            continue
        try:
            place_order(s.token_id, "BUY", s.price, s.stake)
        except Exception as e:  # noqa: BLE001 — one bad order must not stop the batch
            print(f"  ! order failed {s.token_id[:10]}… {s.side} ${s.stake:.2f}: {e}")
            continue
        n_placed += 1
        if not DRY_RUN:
            _record_placed(s.token_id)
            placed.add(s.token_id)
    print(f"\nplaced {n_placed} order(s); skipped {n_small} below "
          f"${MIN_STAKE_PER_MARKET:.2f} min stake")


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
