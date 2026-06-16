"""Paper-trading broker: executes signals against a virtual cash balance, marks
open positions to live Polymarket prices, and settles them on resolution.

No real orders are sent — this is the week-long paper run before going live.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import time

import requests

from ..config import (GAMMA_API, STATIONS, CASH_BUFFER, PAPER_DEPTH,
                      MAX_DAY_FRACTION, MAX_CITY_FRACTION, EQUITY_SNAPSHOT_INTERVAL,
                      MIN_STAKE_PER_MARKET)
from ..forecast.openmeteo import fetch_actual_max
from ..forecast.metar import fetch_station_daily_max
from ..polymarket import clob
from ..strategy.edge import Signal
from .. import notify
from . import store


def _city(question: str) -> str:
    m = re.search(r"in ([\w ]+?) be ", question)
    return m.group(1).strip() if m else ""


def capped_budget(stake: float, cash: float, cash_floor: float,
                  day_deployed: float, day_cap: float,
                  city_deployed: float = 0.0, city_cap: float = float("inf")) -> float:
    """USDC we may actually spend on one signal, after the independent limits:
    the signal's own Kelly stake, the cash reserve buffer, how much room is left
    under this resolution-day's capital cap, and how much room is left under this
    city's exposure cap (correlated single-name risk). Never negative."""
    return max(0.0, min(stake, cash - cash_floor,
                        day_cap - day_deployed, city_cap - city_deployed))


def _gamma_markets(params: dict) -> list | None:
    try:
        r = requests.get(f"{GAMMA_API}/markets", params=params, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception:  # noqa: BLE001
        return None


def _market_state(condition_id: str) -> dict | None:
    """Live price + resolution state for a market, by condition id.

    Gamma's /markets endpoint filters out *closed* markets by default, so a
    market that has resolved returns []. That left settled positions stuck as
    'open' forever (never marked to the 0/1 resolution). We retry with
    closed=true so the resolution is visible and the position can settle."""
    d = _gamma_markets({"condition_ids": condition_id})
    if not d:
        d = _gamma_markets({"condition_ids": condition_id, "closed": "true"})
    if not d:
        return None
    m = d[0]
    prices = m.get("outcomePrices")
    tokens = m.get("clobTokenIds")
    if isinstance(prices, str):
        prices = json.loads(prices)
    if isinstance(tokens, str):
        tokens = json.loads(tokens)
    return {"closed": bool(m.get("closed")),
            "prices": [float(p) for p in prices],
            "tokens": tokens,
            "uma": m.get("umaResolutionStatus")}


class PaperBroker:
    def __init__(self):
        self.con = store.connect()
        self._books: dict[str, dict] = {}
        healed = store.backfill_fill_metadata(self.con)
        if healed:
            print(f"  backfilled forecast metadata on {healed} legacy fill(s)")

    # ---- execution --------------------------------------------------------
    def already_open(self, token_id: str) -> bool:
        row = self.con.execute(
            "SELECT 1 FROM fills WHERE token_id=? AND status='open'", (token_id,)
        ).fetchone()
        return row is not None

    def day_deployed(self, end_date: str) -> float:
        """USDC of open cost already committed to a resolution day (by end_date)."""
        day = (end_date or "")[:10]
        if not day:
            return 0.0
        row = self.con.execute(
            "SELECT COALESCE(SUM(cost),0) c FROM fills "
            "WHERE status='open' AND substr(end_date,1,10)=?", (day,)).fetchone()
        return float(row["c"])

    def city_deployed(self, city: str) -> float:
        """USDC of open cost already committed to a city, across all days."""
        if not city:
            return 0.0
        row = self.con.execute(
            "SELECT COALESCE(SUM(cost),0) c FROM fills "
            "WHERE status='open' AND city=?", (city,)).fetchone()
        return float(row["c"])

    def prefetch_books(self, token_ids: list[str]) -> None:
        """Batch-fetch the order books we're about to fill against (one call)."""
        self._books = {}
        if not (PAPER_DEPTH and token_ids):
            return
        try:
            self._books = clob.get_books(list(set(token_ids)))
        except Exception as e:  # noqa: BLE001
            print(f"  ! book prefetch failed ({e}); using quoted-price fills")

    def _simulate_fill(self, sig: Signal, budget: float) -> tuple[float, float, float]:
        """(shares, avg_price, cost) for spending `budget` on sig.token_id.
        Walks the live book when available; else fills the whole budget at quote."""
        book = self._books.get(sig.token_id)
        if PAPER_DEPTH and book is not None:
            # allow a couple cents of slippage past the quote we sized on
            shares, avg, cost = clob.walk_asks(book, sig.price + 0.02, budget)
            if shares > 0:
                return shares, avg, cost
        # fallback: quoted-price fill (no depth info)
        return round(budget / sig.price, 2), sig.price, budget

    def execute(self, sig: Signal) -> bool:
        """Fill a signal — depth-aware against the live book — subject to the cash
        reserve and the per-resolution-day capital cap, and not already held."""
        if self.already_open(sig.token_id) or sig.stake <= 0:
            return False
        cash = store.get_meta(self.con, "cash")
        start = store.get_meta(self.con, "starting_cash")
        floor = CASH_BUFFER * start                     # never spend below the reserve
        day_cap = MAX_DAY_FRACTION * start              # max committed to one day
        city_cap = MAX_CITY_FRACTION * start            # max committed to one city
        budget = capped_budget(sig.stake, cash, floor,
                               self.day_deployed(sig.market.end_date), day_cap,
                               self.city_deployed(_city(sig.market.question)), city_cap)
        if budget < MIN_STAKE_PER_MARKET:
            return False
        shares, avg_price, cost = self._simulate_fill(sig, budget)
        if shares <= 0 or cost < MIN_STAKE_PER_MARKET:
            return False
        slippage = round(avg_price - sig.price, 5)
        fill_ratio = round(cost / sig.stake, 4) if sig.stake else 1.0
        m = sig.market
        self.con.execute(
            """INSERT INTO fills (ts,event_slug,market_slug,question,city,condition_id,
               token_id,side,entry_price,shares,cost,model_prob,edge,end_date,status,
               mark_price,pnl,station,fc_date,fc_mean,fc_std,quote_price,slippage,fill_ratio,
               sleeve,peer)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'open', ?, 0, ?,?,?,?,?,?,?,?,?)""",
            (time.time(), m.event_slug, m.market_slug, m.question, _city(m.question),
             m.condition_id, sig.token_id, sig.side, avg_price, shares, cost,
             sig.model_prob, sig.edge, m.end_date, avg_price,
             sig.station, sig.date, sig.fc_mean, sig.fc_std,
             sig.price, slippage, fill_ratio, sig.sleeve, sig.peer))
        store.set_meta(self.con, "cash", cash - cost)
        self.con.commit()
        return True

    def log_forecast(self, sig: Signal) -> None:
        """Record the forecast distribution we acted on, for later calibration."""
        if not sig.station:
            return
        self.con.execute(
            """INSERT INTO forecasts (station,date,ts_logged,mean,std,n_members)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(station,date) DO UPDATE SET mean=excluded.mean,
               std=excluded.std, ts_logged=excluded.ts_logged,
               n_members=excluded.n_members""",
            (sig.station, sig.date, time.time(), sig.fc_mean, sig.fc_std, sig.fc_n))
        self.con.commit()

    # ---- marking & settlement --------------------------------------------
    def mark_and_settle(self) -> None:
        rows = self.con.execute("SELECT * FROM fills WHERE status='open'").fetchall()
        seen: dict[str, dict] = {}
        newly_settled: list[dict] = []
        for f in rows:
            cid = f["condition_id"]
            if cid not in seen:
                st = _market_state(cid)
                if st:
                    seen[cid] = st
            st = seen.get(cid)
            if not st:
                continue
            try:
                idx = st["tokens"].index(f["token_id"])
            except ValueError:
                continue
            price = st["prices"][idx]
            resolved = st["closed"] and price in (0.0, 1.0)
            if resolved:
                payout = f["shares"] * price
                pnl = payout - f["cost"]
                # Did the YES outcome win?  outcomes order is [Yes, No].
                yes_price = st["prices"][0]
                resolved_yes = 1 if yes_price >= 0.5 else 0
                cash = store.get_meta(self.con, "cash")
                store.set_meta(self.con, "cash", cash + payout)
                self.con.execute(
                    """UPDATE fills SET status='settled', mark_price=?, exit_price=?,
                       pnl=?, resolved_yes=? WHERE id=?""",
                    (price, price, pnl, resolved_yes, f["id"]))
                settled = dict(f)
                settled.update(pnl=pnl, exit_price=price)
                newly_settled.append(settled)
            else:
                unreal = f["shares"] * price - f["cost"]
                self.con.execute("UPDATE fills SET mark_price=?, pnl=? WHERE id=?",
                                 (price, unreal, f["id"]))
        self.con.commit()
        # Force a snapshot on settlement: a resolution moves P&L from unrealized
        # to realized in one step, and the normal throttle would leave the equity
        # curve showing the stale split (realized=0) for up to EQUITY_SNAPSHOT_
        # INTERVAL — disagreeing with the live summary plates. Settlements are
        # infrequent (next-day), so forcing here costs nothing.
        self.snapshot(force=bool(newly_settled))
        if newly_settled:
            self._notify_settlements(newly_settled)

    def _notify_settlements(self, settled: list[dict]) -> None:
        if not notify.enabled():
            return
        start = store.get_meta(self.con, "starting_cash")
        eq = self.con.execute(
            "SELECT equity, realized FROM equity ORDER BY ts DESC LIMIT 1").fetchone()
        tally = self.con.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(pnl>0),0) w FROM fills "
            "WHERE status='settled'").fetchone()
        balance = {"equity": eq["equity"] if eq else start,
                   "realized": eq["realized"] if eq else 0.0,
                   "wins": tally["w"], "settled": tally["n"]}
        for f in settled:
            notify.notify_settlement(f, balance)

    def backfill_actuals(self) -> int:
        """Fill realized max-temp for past forecast dates (ERA5 archive).

        The authoritative truth for traded markets is the Polymarket resolution
        (captured as resolved_yes on fills); this gives the *temperature-level*
        truth used to learn per-station bias/sigma.
        """
        today = dt.datetime.now(dt.timezone.utc).date().isoformat()
        rows = self.con.execute(
            "SELECT station, date FROM forecasts WHERE actual_max IS NULL "
            "AND date < ?", (today,)).fetchall()
        n = 0
        for r in rows:
            s = STATIONS.get(r["station"])
            if not s:
                continue
            # prefer the real resolution source (station METAR); fall back to ERA5
            actual = fetch_station_daily_max(r["station"], r["date"], s["tz"])
            src = "metar"
            if actual is None:
                actual = fetch_actual_max(s["lat"], s["lon"], r["date"], s["tz"])
                src = "era5"
            if actual is not None:
                self.con.execute(
                    "UPDATE forecasts SET actual_max=?, actual_src=? "
                    "WHERE station=? AND date=?", (actual, src, r["station"], r["date"]))
                n += 1
        self.con.commit()
        return n

    def snapshot(self, force: bool = False) -> None:
        # Throttle equity-curve points: the trader may tick more often than
        # EQUITY_SNAPSHOT_INTERVAL, but we only add a new point once per interval
        # so the dashboard chart updates every ~10 min, not every short tick.
        if not force and EQUITY_SNAPSHOT_INTERVAL > 0:
            last = self.con.execute(
                "SELECT ts FROM equity ORDER BY ts DESC LIMIT 1").fetchone()
            if last and (time.time() - last["ts"]) < EQUITY_SNAPSHOT_INTERVAL:
                return
        cash = store.get_meta(self.con, "cash")
        start = store.get_meta(self.con, "starting_cash")
        opens = self.con.execute(
            "SELECT shares, mark_price, cost FROM fills WHERE status='open'").fetchall()
        pos_value = sum(o["shares"] * o["mark_price"] for o in opens)
        unrealized = sum(o["shares"] * o["mark_price"] - o["cost"] for o in opens)
        realized = self.con.execute(
            "SELECT COALESCE(SUM(pnl),0) s FROM fills WHERE status='settled'"
        ).fetchone()["s"]
        equity = cash + pos_value
        self.con.execute(
            "INSERT OR REPLACE INTO equity VALUES (?,?,?,?,?,?)",
            (round(time.time()), cash, pos_value, realized, unrealized, equity))
        self.con.commit()

    # ---- dashboard feed ---------------------------------------------------
    def record_signals(self, signals: list[Signal], taken_tokens: set[str]) -> None:
        self.con.execute("DELETE FROM signals")
        now = time.time()
        for s in signals:
            self.con.execute(
                "INSERT INTO signals VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (now, s.market.event_slug, s.market.question, _city(s.market.question),
                 s.side, s.price, s.model_prob, s.edge, s.stake,
                 1 if s.token_id in taken_tokens else 0, s.sleeve, s.peer))
        self.con.commit()
