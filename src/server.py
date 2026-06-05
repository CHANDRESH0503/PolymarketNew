"""Dashboard API + static server.

    python -m src.server            # http://127.0.0.1:8000

Read-only over the paper-trading SQLite, plus one live endpoint (/api/forecast)
that recomputes a city's ensemble distribution vs market prices on demand.
"""
from __future__ import annotations

import datetime as dt
import time
from collections import defaultdict
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

from .config import (ROOT, STATIONS, BANKROLL, CALIBRATION, DRY_RUN, NOWCAST,
                     CORR_KELLY, CORR_KELLY_RHO, MIN_EDGE, KELLY_FRACTION,
                     MAX_STAKE_PER_MARKET, MIN_PRICE, MAX_PRICE,
                     MIN_HOURS_TO_RESOLVE, ARB_EXECUTE, LP_EXECUTE)
from .paper import store
from .polymarket.gamma import fetch_open_temperature_events, parse_event
from .forecast.openmeteo import fetch_max_temp_distribution, MODELS
from .forecast.model import yes_probability, apply_calibration
from .forecast import nowcast as nowcast_mod

WEB = ROOT / "web"
app = Flask(__name__, static_folder=None)

_fc_cache: dict[tuple, tuple[float, dict]] = {}


def db():
    return store.connect()


@app.get("/api/summary")
def summary():
    con = db()
    start = store.get_meta(con, "starting_cash", BANKROLL)
    cash = store.get_meta(con, "cash", BANKROLL)
    eq = con.execute("SELECT * FROM equity ORDER BY ts DESC LIMIT 1").fetchone()
    equity = eq["equity"] if eq else cash
    realized = eq["realized"] if eq else 0.0
    unrealized = eq["unrealized"] if eq else 0.0
    settled = con.execute(
        "SELECT pnl, side, resolved_yes FROM fills WHERE status='settled'").fetchall()
    wins = sum(1 for s in settled if s["pnl"] > 0)
    n = len(settled)
    # Brier score: model P(Yes) at entry vs whether Yes actually resolved.
    brier = None
    rows = con.execute(
        "SELECT model_prob, resolved_yes FROM fills WHERE status='settled' "
        "AND resolved_yes IS NOT NULL").fetchall()
    if rows:
        brier = sum((r["model_prob"] - r["resolved_yes"]) ** 2 for r in rows) / len(rows)
    open_n = con.execute("SELECT COUNT(*) c FROM fills WHERE status='open'").fetchone()["c"]
    created = store.get_meta(con, "created_at", time.time())
    return jsonify({
        "starting_cash": start, "cash": cash, "equity": equity,
        "total_pnl": equity - start, "roi": (equity - start) / start if start else 0,
        "realized": realized, "unrealized": unrealized,
        "open_positions": open_n, "settled": n,
        "wins": wins, "win_rate": wins / n if n else None,
        "brier": brier, "last_update": eq["ts"] if eq else None,
        "running_since": created,
        "is_live": bool(eq) and (time.time() - eq["ts"] < 3600),
    })


@app.get("/api/equity")
def equity_series():
    con = db()
    rows = con.execute("SELECT ts, equity, realized, unrealized FROM equity "
                       "ORDER BY ts ASC").fetchall()
    return jsonify([dict(r) for r in rows])


@app.get("/api/positions")
def positions():
    con = db()
    rows = con.execute(
        "SELECT * FROM fills WHERE status='open' ORDER BY ts DESC").fetchall()
    return jsonify([dict(r) for r in rows])


@app.get("/api/fills")
def fills():
    con = db()
    rows = con.execute(
        "SELECT * FROM fills ORDER BY ts DESC LIMIT 200").fetchall()
    return jsonify([dict(r) for r in rows])


@app.get("/api/signals")
def signals():
    con = db()
    rows = con.execute(
        "SELECT * FROM signals ORDER BY edge DESC").fetchall()
    return jsonify([dict(r) for r in rows])


@app.get("/api/daily")
def daily():
    """Realized PnL grouped by settlement day + end-of-day equity."""
    con = db()
    rows = con.execute(
        "SELECT ts, pnl FROM fills WHERE status='settled'").fetchall()
    def day_of(ts):
        return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%d")
    by_day = defaultdict(float)
    for r in rows:
        by_day[day_of(r["ts"])] += r["pnl"]
    eq = con.execute("SELECT ts, equity FROM equity ORDER BY ts ASC").fetchall()
    eq_by_day = {}
    for r in eq:
        eq_by_day[day_of(r["ts"])] = r["equity"]
    return jsonify({"realized": [{"day": d, "pnl": p} for d, p in sorted(by_day.items())],
                    "equity": [{"day": d, "equity": e} for d, e in sorted(eq_by_day.items())]})


@app.get("/api/forecast")
def forecast():
    """Live ensemble distribution vs market-implied bucket probabilities for one event."""
    slug = request.args.get("event", "")
    events = fetch_open_temperature_events()
    ev = next((e for e in events if e.get("slug") == slug), None)
    if ev is None:
        return jsonify({"error": "event not found", "available":
                        [e["slug"] for e in events[:60]]}), 404
    markets = parse_event(ev)
    station = next((m.station_code for m in markets if m.station_code in STATIONS), None)
    if not station:
        return jsonify({"error": "unknown station"}), 404
    date = markets[0].end_date[:10]
    key = (station, date)
    now = time.time()
    if key in _fc_cache and now - _fc_cache[key][0] < 600:
        fc = _fc_cache[key][1]
    else:
        s = STATIONS[station]
        f = fetch_max_temp_distribution(s["lat"], s["lon"], date, s["tz"], station)
        fc = {"mean": f.mean, "std": f.std, "members": f.members_max_c.tolist(), "_obj": f}
        _fc_cache[key] = (now, fc)
    buckets = []
    for m in sorted(markets, key=lambda x: x.threshold_c):
        p = yes_probability(fc["_obj"], m.bucket_kind, m.threshold_c)
        buckets.append({"label": f"{m.threshold_c}°", "kind": m.bucket_kind,
                        "degree": m.threshold_c, "model": p, "market": m.yes_price})
    s = STATIONS[station]
    return jsonify({"event": slug, "city": s["city"], "station": station,
                    "date": date, "mean": fc["mean"], "std": fc["std"],
                    "members": fc["members"], "buckets": buckets})


@app.get("/api/status")
def status():
    """What the system is configured to do right now — feature flags + knobs.
    Drives the dashboard's 'System Almanac' strip so it reflects the live build."""
    con = db()
    n_fc = con.execute("SELECT COUNT(*) c FROM forecasts").fetchone()["c"]
    n_emos = sum(1 for v in CALIBRATION.values() if v.get("emos"))
    return jsonify({
        "dry_run": DRY_RUN,
        "strategies": {
            "forecast_edge": True,
            "nowcast": NOWCAST,
            "corr_kelly": CORR_KELLY,
            "arb_execute": ARB_EXECUTE,
            "lp_execute": LP_EXECUTE,
        },
        "knobs": {
            "min_edge": MIN_EDGE, "kelly_fraction": KELLY_FRACTION,
            "max_stake": MAX_STAKE_PER_MARKET, "bankroll": BANKROLL,
            "min_price": MIN_PRICE, "max_price": MAX_PRICE,
            "min_hours_to_resolve": MIN_HOURS_TO_RESOLVE,
            "corr_rho": CORR_KELLY_RHO,
        },
        "models": MODELS.split(","),
        "stations": len(STATIONS),
        "calibrated": len(CALIBRATION),
        "emos_fitted": n_emos,
        "forecasts_logged": n_fc,
    })


@app.get("/api/calibration")
def calibration():
    """Tier-2 EMOS/NGR + bias calibration per station (μ=a+b·mean, σ²=c+d·var)."""
    out = []
    for code, cal in sorted(CALIBRATION.items()):
        s = STATIONS.get(code, {})
        e = cal.get("emos") or {}
        out.append({
            "station": code, "city": s.get("city", code),
            "bias": cal.get("bias"), "sigma": cal.get("sigma"),
            "emos": bool(e),
            "a": e.get("a"), "b": e.get("b"), "c": e.get("c"), "d": e.get("d"),
        })
    return jsonify(out)


@app.get("/api/exposure")
def exposure():
    """Live breakdown of open capital: by city, by side (Yes/No longshot mix),
    and the edge/horizon profile — i.e. what the book currently looks like."""
    con = db()
    rows = con.execute(
        "SELECT city, side, cost, shares, mark_price, edge, model_prob, pnl, "
        "end_date FROM fills WHERE status='open'").fetchall()
    by_city: dict[str, float] = defaultdict(float)
    by_side = {"Yes": {"n": 0, "cost": 0.0}, "No": {"n": 0, "cost": 0.0}}
    deployed = value = edge_sum = 0.0
    for r in rows:
        by_city[r["city"] or "—"] += r["cost"]
        side = r["side"] if r["side"] in by_side else "No"
        by_side[side]["n"] += 1
        by_side[side]["cost"] += r["cost"]
        deployed += r["cost"]
        value += r["shares"] * r["mark_price"]
        edge_sum += r["edge"] or 0.0
    n = len(rows)
    return jsonify({
        "n": n, "deployed": round(deployed, 2), "value": round(value, 2),
        "avg_edge": (edge_sum / n) if n else None,
        "by_city": [{"city": c, "cost": round(v, 2)}
                    for c, v in sorted(by_city.items(), key=lambda x: -x[1])],
        "by_side": by_side,
    })


_nc_cache: dict[tuple, tuple[float, dict]] = {}


@app.get("/api/nowcast")
def nowcast():
    """Tier-3 intraday nowcast for one event: observed station floor + remaining
    hours, with the collapse meter and ENS-vs-NOW-vs-MKT bucket probabilities."""
    slug = request.args.get("event", "")
    events_ = fetch_open_temperature_events()
    ev = next((e for e in events_ if e.get("slug") == slug), None)
    if ev is None:
        return jsonify({"error": "event not found"}), 404
    markets = parse_event(ev)
    station = next((m.station_code for m in markets if m.station_code in STATIONS), None)
    if not station:
        return jsonify({"error": "unknown station"}), 404
    date = markets[0].end_date[:10]
    key = (station, date)
    now = time.time()
    if key in _nc_cache and now - _nc_cache[key][0] < 300:
        nc, fc = _nc_cache[key][1]["_nc"], _nc_cache[key][1]["_fc"]
    else:
        try:
            nc = nowcast_mod.build_nowcast(station, date)
        except Exception as e:  # noqa: BLE001
            return jsonify({"error": f"nowcast failed: {e}"}), 502
        s = STATIONS[station]
        try:
            fc = apply_calibration(fetch_max_temp_distribution(
                s["lat"], s["lon"], date, s["tz"], station), CALIBRATION)
        except Exception:  # noqa: BLE001
            fc = None
        _nc_cache[key] = (now, {"_nc": nc, "_fc": fc})

    buckets = []
    for m in sorted(markets, key=lambda x: x.threshold_c):
        buckets.append({
            "label": f"{m.threshold_c}°", "kind": m.bucket_kind, "degree": m.threshold_c,
            "now": nowcast_mod.yes_probability(nc, m.bucket_kind, m.threshold_c),
            "ens": (yes_probability(fc, m.bucket_kind, m.threshold_c)
                    if fc is not None else None),
            "market": m.yes_price,
        })
    s = STATIONS[station]
    return jsonify({
        "event": slug, "city": s["city"], "station": station, "date": date,
        "observed_max": nc.observed_max_c, "latest_ob": nc.latest_ob,
        "remaining_hours": nc.n_remaining_hours, "floor_locked": nc.floor_locked,
        "mean": nc.mean, "std": nc.std, "buckets": buckets,
    })


@app.get("/api/events")
def events():
    evs = fetch_open_temperature_events()
    out = []
    for e in evs:
        ms = parse_event(e)
        if any(m.station_code in STATIONS for m in ms):
            out.append({"slug": e["slug"], "title": e.get("title", "")})
    return jsonify(out)


@app.get("/")
def index():
    return send_from_directory(WEB, "index.html")


@app.get("/<path:p>")
def static_files(p):
    return send_from_directory(WEB, p)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8010, debug=False)
