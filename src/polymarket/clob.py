"""Order execution via the Polymarket CLOB.

This is intentionally a thin, GUARDED wrapper. Live trading is OFF unless you
(1) fill in wallet creds in .env and (2) set DRY_RUN=0. By default every order
is logged, not sent.

Setup once:
    python -m src.polymarket.clob --create-api-key
This derives CLOB_API_KEY/SECRET/PASSPHRASE from your PK; paste them into .env.
"""
from __future__ import annotations

import requests

from ..config import (CLOB_API, PK, POLY_PROXY_ADDRESS, DRY_RUN, SIGNATURE_TYPE,
                      CLOB_API_KEY, CLOB_API_SECRET, CLOB_API_PASSPHRASE,
                      MIN_ORDER_SHARES)


# ---- read-only order book (no auth needed) --------------------------------
def get_books(token_ids: list[str]) -> dict[str, dict]:
    """Fetch live order books for many tokens in one batched call.
    Returns {token_id: book}, where book has 'bids' and 'asks' (price/size)."""
    if not token_ids:
        return {}
    r = requests.post(f"{CLOB_API}/books",
                      json=[{"token_id": t} for t in token_ids], timeout=20)
    r.raise_for_status()
    return {b.get("asset_id"): b for b in r.json()}


def best_ask(book: dict | None) -> tuple[float, float] | None:
    """(price, size) of the lowest ask — the price/size we could BUY at."""
    asks = (book or {}).get("asks") or []
    if not asks:
        return None
    a = min(asks, key=lambda x: float(x["price"]))
    return float(a["price"]), float(a["size"])


def best_bid(book: dict | None) -> tuple[float, float] | None:
    """(price, size) of the highest bid — the price/size we could SELL at."""
    bids = (book or {}).get("bids") or []
    if not bids:
        return None
    b = max(bids, key=lambda x: float(x["price"]))
    return float(b["price"]), float(b["size"])


def walk_asks(book: dict | None, limit_price: float, budget_usdc: float
              ) -> tuple[float, float, float]:
    """Simulate a marketable BUY: spend up to `budget_usdc`, taking asks priced
    at or below `limit_price`, cheapest first. Returns (shares, avg_price, cost).

    This is what makes a paper fill realistic — you cross the spread and eat depth
    rather than magically filling the whole size at the top-of-book quote. Returns
    (0, 0, 0) if nothing is takeable within the limit."""
    asks = sorted(((float(a["price"]), float(a["size"]))
                   for a in (book or {}).get("asks") or []), key=lambda x: x[0])
    shares = cost = 0.0
    remaining = budget_usdc
    for price, size in asks:
        if price > limit_price + 1e-9 or remaining <= 1e-9:
            break
        take = min(size, remaining / price)       # shares we can afford at this level
        if take <= 0:
            break
        shares += take
        cost += take * price
        remaining -= take * price
    avg = cost / shares if shares > 0 else 0.0
    return round(shares, 4), round(avg, 5), round(cost, 4)


def _client():
    """Lazily build a py-clob-client-v2 client. Imported here so the rest of the
    bot runs without the trading dependency installed.

    We use the **v2** client (py_clob_client_v2), not the legacy py_clob_client:
    Polymarket's order-service rejects orders built by the old client with
    "invalid order version, please use the latest clob-client", and only v2
    supports this wallet's signature_type=3. This mirrors the working polybot on
    the same host (identical funder wallet + sig type + v2 client)."""
    from py_clob_client_v2 import ClobClient
    from py_clob_client_v2.clob_types import ApiCreds

    creds = None
    if CLOB_API_KEY:
        creds = ApiCreds(api_key=CLOB_API_KEY, api_secret=CLOB_API_SECRET,
                         api_passphrase=CLOB_API_PASSPHRASE)
    return ClobClient(
        host=CLOB_API, chain_id=137, key=PK, creds=creds,
        signature_type=SIGNATURE_TYPE, funder=POLY_PROXY_ADDRESS,
    )


def _align_to_tick(price: float, tick: float) -> float:
    """Snap a price to the market's tick grid (e.g. 0.01) so the CLOB accepts it.
    Our signal prices can carry sub-tick precision (a 0.855 on a 0.01 grid), which
    the order-service rejects. Round to the nearest valid tick and clamp inside
    (tick, 1-tick)."""
    if tick <= 0:
        return round(price, 3)
    px = round(round(price / tick) * tick, 6)
    return min(max(px, tick), round(1.0 - tick, 6))


def create_api_key() -> None:
    client = _client()
    creds = client.create_or_derive_api_creds()
    print("Add these to .env:")
    print(f"CLOB_API_KEY={creds.api_key}")
    print(f"CLOB_API_SECRET={creds.api_secret}")
    print(f"CLOB_API_PASSPHRASE={creds.api_passphrase}")


def _post(client, token_id: str, price: float, shares: float):
    """Create + post one BUY order via the v2 client, resolving the market's tick
    size and neg-risk flag first (weather buckets are neg-risk multi-outcome, so
    the order must be signed against the neg-risk exchange)."""
    from py_clob_client_v2.clob_types import OrderArgs, PartialCreateOrderOptions, OrderType
    from py_clob_client_v2.order_builder.constants import BUY

    # get_tick_size returns a TickSize (a string like "0.01"); pass it through
    # unchanged — the client's ROUNDING_CONFIG is keyed by that exact string, so
    # float() would KeyError. Only the tick math below needs the numeric value.
    tick = client.get_tick_size(token_id)
    neg_risk = bool(client.get_neg_risk(token_id))
    px = _align_to_tick(price, float(tick))
    options = PartialCreateOrderOptions(tick_size=tick, neg_risk=neg_risk)
    return client.create_and_post_order(
        OrderArgs(token_id=token_id, price=px, size=round(shares, 2), side=BUY),
        options=options, order_type=OrderType.GTC)


def place_order(token_id: str, side: str, price: float, size_usdc: float) -> dict:
    """Buy `size_usdc` worth of `token_id` at limit `price`.

    side is always BUY here (we buy Yes or No tokens directly). Returns the API
    response, or a dry-run stub.
    """
    shares = round(size_usdc / price, 2)
    # Guard against non-positive / sub-tick orders: a ~$0 stake rounds to 0 shares,
    # which the CLOB rejects. Skip rather than send.
    if size_usdc <= 0 or price <= 0 or shares <= 0:
        print(f"   [skip] non-positive order for {token_id[:10]}… "
              f"(${size_usdc:.2f} @ {price:.3f} = {shares} shares)")
        return {"skipped": True, "reason": "non_positive_size", "token_id": token_id}
    # Polymarket enforces a per-order minimum of MIN_ORDER_SHARES shares; a smaller
    # order is rejected ("Size (x) lower than the minimum: 5"). Bump up to the floor
    # — bounded, since floor × price ≤ 5 × 0.97 < the $5 per-market cap.
    if shares < MIN_ORDER_SHARES:
        shares = round(MIN_ORDER_SHARES, 2)
    order = {"token_id": token_id, "side": "BUY", "price": round(price, 3), "size": shares}

    if DRY_RUN or not PK:
        print(f"   [DRY_RUN] would place {order}")
        return {"dry_run": True, **order}

    resp = _post(_client(), token_id, price, shares)
    print(f"   [LIVE] order resp: {resp}")
    return resp


def place_maker(token_id: str, price: float, shares: float) -> dict:
    """Post a resting BUY limit (maker) order for `shares` at `price`.
    DRY_RUN-guarded like place_order. Used by the LP daemon for two-sided quoting
    (bid = buy YES; ask = buy NO at 1-ask)."""
    order = {"token_id": token_id, "side": "BUY", "price": round(price, 3),
             "size": round(shares, 2)}
    if DRY_RUN or not PK:
        print(f"   [DRY_RUN] would quote {order}")
        return {"dry_run": True, **order}

    return _post(_client(), token_id, price, shares)


def cancel_all() -> dict:
    """Cancel all open orders (clean slate before re-quoting). DRY_RUN-guarded."""
    if DRY_RUN or not PK:
        print("   [DRY_RUN] would cancel all open orders")
        return {"dry_run": True}
    return _client().cancel_all()


if __name__ == "__main__":
    import sys
    if "--create-api-key" in sys.argv:
        create_api_key()
    else:
        print(__doc__)
