"""
Hyperliquid daily 5:25pm ET snapshot collector.
Runs on GitHub Actions (see collect.yml). Appends one row per live perp coin
to data/snapshot.csv and backfills outcomes for earlier rows once their
observation window has passed.

Fields per row (features observable AT snapshot time only - no lookahead):
  date, ts_ms, coin,
  ret12, ret24, ret7d, day2_ret24 (yesterday's 24h ret),
  pump_max5m (biggest single 5m move in 12h),
  mins_since_high12 (minutes since the 12h high),
  retrace_from_high (drop from 12h high to now, as frac of pump),
  vwap_dist (close vs 12h VWAP),
  volr_15m, volr_1h, volr_2h (5m notional vs 12h avg, per window),
  vol24_usd (24h notional), oi_usd, funding_1h, funding_8h_sum, premium,
  spread_bps, depth_bid_05, depth_ask_05 (book within 0.5%, top-20 gainers only),
  btc_ret24, eth_ret24, breadth_up5 (count of coins +5% on 24h),
  rank24 (rank by ret24, 1 = top gainer)
Outcomes (backfilled next run): r30m, r1h, r2h, r3h, r6h, r24h,
  mae3h (max high in 3h vs snapshot close - worst case for a short),
  fund_paid_3h (cum funding over 3h; positive = short RECEIVES).
"""

import json
import time
import csv
import os
import sys
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

API = "https://api.hyperliquid.xyz/info"
NY = ZoneInfo("America/New_York")
CSV_PATH = "data/snapshot.csv"
FIELDS = ("date,ts_ms,coin,ret12,ret24,ret7d,day2_ret24,pump_max5m,"
          "mins_since_high12,retrace_from_high,vwap_dist,volr_15m,volr_1h,"
          "volr_2h,vol24_usd,oi_usd,funding_1h,funding_8h_sum,premium,"
          "spread_bps,depth_bid_05,depth_ask_05,btc_ret24,eth_ret24,"
          "breadth_up5,rank24,r30m,r1h,r2h,r3h,r6h,r24h,mae3h,fund_paid_3h"
          ).split(",")


def post(body, retries=3):
    for i in range(retries):
        try:
            req = urllib.request.Request(
                API, data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except Exception:
            if i == retries - 1:
                raise
            time.sleep(2 * (i + 1))


def candles(coin, interval, start, end):
    return post({"type": "candleSnapshot",
                 "req": {"coin": coin, "interval": interval,
                         "startTime": start, "endTime": end}}) or []


def wait_until_525pm_ny():
    now = datetime.now(NY)
    target = now.replace(hour=17, minute=25, second=0, microsecond=0)
    if now > target + timedelta(minutes=30):
        print("outside window, exiting (probably the wrong DST cron)")
        sys.exit(0)
    if now < target - timedelta(minutes=45):
        print("too early, exiting (probably the wrong DST cron)")
        sys.exit(0)
    wait = (target - now).total_seconds()
    if wait > 0:
        print(f"waiting {wait:.0f}s until 17:25 NY")
        time.sleep(wait)


def f(x, nd=6):
    return "" if x is None else f"{x:.{nd}f}"


def collect_snapshot():
    meta, ctxs = post({"type": "metaAndAssetCtxs"})
    uni = meta["universe"]
    now_ms = int(time.time() * 1000)
    coins = []
    for u, c in zip(uni, ctxs):
        if u.get("isDelisted"):
            continue
        mark = float(c.get("markPx") or 0)
        coins.append(dict(
            coin=u["name"], mark=mark,
            vol24=float(c.get("dayNtlVlm") or 0),
            oi=float(c.get("openInterest") or 0) * mark,
            funding=float(c.get("funding") or 0),
            premium=float(c.get("premium") or 0)))

    rows = {}
    H = 3600 * 1000
    for cd in coins:
        coin = cd["coin"]
        try:
            c5 = candles(coin, "5m", now_ms - 13 * H, now_ms)
            c1h = candles(coin, "1h", now_ms - 8 * 24 * H, now_ms)
        except Exception:
            continue
        if len(c5) < 24 or len(c1h) < 30:
            continue
        time.sleep(0.06)
        closes5 = [float(k["c"]) for k in c5]
        highs5 = [float(k["h"]) for k in c5]
        nvol5 = [float(k["v"]) * float(k["c"]) for k in c5]
        px = closes5[-1]

        def ret_h(hours):
            tgt = now_ms - hours * H
            best = min(c1h, key=lambda k: abs(k["t"] - tgt))
            if abs(best["t"] - tgt) > 2 * H:
                return None
            p = float(best["c"])
            return px / p - 1 if p > 0 else None

        ret24 = ret_h(24)
        ret24_y = ret_h(48)
        day2 = (None if (ret24 is None or ret24_y is None)
                else (1 + ret24_y) / (1 + ret24) * 0 + ret24_y)  # yesterday-ending 24h ret ~ ret48->24
        # simpler and correct: yesterday's 24h return
        c48 = ret_h(48)
        day2 = (None if (c48 is None or ret24 is None)
                else (1 + c48) / (1 + ret24) - 1)
        i_hi = max(range(len(highs5)), key=lambda i: highs5[i])
        hi = highs5[i_hi]
        pump5 = max((closes5[i] / closes5[i - 1] - 1)
                    for i in range(1, len(closes5)))
        tot_nv = sum(nvol5)
        vwap = (sum(float(k["c"]) * float(k["v"]) * float(k["c"]) for k in c5)
                / tot_nv) if tot_nv > 0 else px
        avg5 = tot_nv / len(nvol5)

        def volr(nbars):
            if avg5 <= 0:
                return None
            return (sum(nvol5[-nbars:]) / nbars) / avg5

        rows[coin] = dict(
            coin=coin, ret12=ret_h(12), ret24=ret24, ret7d=ret_h(168),
            day2_ret24=day2, pump_max5m=pump5,
            mins_since_high12=(len(c5) - 1 - i_hi) * 5,
            retrace_from_high=(hi - px) / hi if hi > 0 else None,
            vwap_dist=px / vwap - 1 if vwap > 0 else None,
            volr_15m=volr(3), volr_1h=volr(12), volr_2h=volr(24),
            vol24_usd=cd["vol24"], oi_usd=cd["oi"], funding_1h=cd["funding"],
            premium=cd["premium"], funding_8h_sum=None,
            spread_bps=None, depth_bid_05=None, depth_ask_05=None)

    # funding 8h sum + order book for top-20 gainers
    ranked = sorted([r for r in rows.values() if r["ret24"] is not None],
                    key=lambda r: -r["ret24"])
    for i, r in enumerate(ranked):
        r["rank24"] = i + 1
    for r in ranked[:20]:
        try:
            fh = post({"type": "fundingHistory", "coin": r["coin"],
                       "startTime": now_ms - 9 * H})
            r["funding_8h_sum"] = sum(float(x["fundingRate"]) for x in fh[-8:])
            book = post({"type": "l2Book", "coin": r["coin"]})
            bids, asks = book["levels"][0], book["levels"][1]
            bb, ba = float(bids[0]["px"]), float(asks[0]["px"])
            mid = (bb + ba) / 2
            r["spread_bps"] = (ba - bb) / mid * 1e4
            r["depth_bid_05"] = sum(float(l["px"]) * float(l["sz"])
                                    for l in bids if float(l["px"]) >= mid * 0.995)
            r["depth_ask_05"] = sum(float(l["px"]) * float(l["sz"])
                                    for l in asks if float(l["px"]) <= mid * 1.005)
            time.sleep(0.1)
        except Exception:
            pass

    btc = rows.get("BTC", {}).get("ret24")
    eth = rows.get("ETH", {}).get("ret24")
    breadth = sum(1 for r in rows.values()
                  if r["ret24"] is not None and r["ret24"] > 0.05)
    date = datetime.now(NY).strftime("%Y-%m-%d")
    out = []
    for r in rows.values():
        out.append({
            "date": date, "ts_ms": now_ms, "coin": r["coin"],
            **{k: f(r.get(k)) for k in
               ("ret12", "ret24", "ret7d", "day2_ret24", "pump_max5m",
                "retrace_from_high", "vwap_dist", "volr_15m", "volr_1h",
                "volr_2h", "funding_1h", "funding_8h_sum", "premium",
                "vwap_dist")},
            "mins_since_high12": r.get("mins_since_high12", ""),
            "vol24_usd": f(r.get("vol24_usd"), 0),
            "oi_usd": f(r.get("oi_usd"), 0),
            "spread_bps": f(r.get("spread_bps"), 2),
            "depth_bid_05": f(r.get("depth_bid_05"), 0),
            "depth_ask_05": f(r.get("depth_ask_05"), 0),
            "btc_ret24": f(btc), "eth_ret24": f(eth),
            "breadth_up5": breadth, "rank24": r.get("rank24", ""),
            "r30m": "", "r1h": "", "r2h": "", "r3h": "", "r6h": "",
            "r24h": "", "mae3h": "", "fund_paid_3h": ""})
    return out


def backfill(rows):
    """Fill outcomes for rows older than 25h that lack them."""
    now_ms = int(time.time() * 1000)
    H = 3600 * 1000
    todo = [r for r in rows if r["r24h"] == "" and
            now_ms - int(r["ts_ms"]) > 25 * H and
            now_ms - int(r["ts_ms"]) < 15 * 24 * H]
    by_coin = {}
    for r in todo:
        by_coin.setdefault(r["coin"], []).append(r)
    for coin, rs in by_coin.items():
        t0 = min(int(r["ts_ms"]) for r in rs) - H
        t1 = max(int(r["ts_ms"]) for r in rs) + 25 * H
        try:
            c5 = candles(coin, "5m", t0, t1)
            fh = post({"type": "fundingHistory", "coin": coin, "startTime": t0})
        except Exception:
            continue
        time.sleep(0.06)
        cmap = {k["t"]: k for k in c5}
        for r in rs:
            ts = int(r["ts_ms"])
            base_t = ts - ts % (5 * 60 * 1000)
            b0 = cmap.get(base_t) or cmap.get(base_t - 5 * 60 * 1000)
            if not b0:
                continue
            px = float(b0["c"])

            def at(mins):
                b = cmap.get(base_t + mins * 60 * 1000)
                return None if not b or px <= 0 else float(b["c"]) / px - 1
            r["r30m"], r["r1h"], r["r2h"] = f(at(30)), f(at(60)), f(at(120))
            r["r3h"], r["r6h"], r["r24h"] = f(at(180)), f(at(360)), f(at(1440))
            mae = None
            for m in range(5, 185, 5):
                b = cmap.get(base_t + m * 60 * 1000)
                if b:
                    hi_r = float(b["h"]) / px - 1
                    mae = hi_r if mae is None else max(mae, hi_r)
            r["mae3h"] = f(mae)
            fp = sum(float(x["fundingRate"]) for x in fh
                     if ts < x["time"] <= ts + 3 * H)
            r["fund_paid_3h"] = f(fp)
    return rows


def main():
    wait_until_525pm_ny()
    os.makedirs("data", exist_ok=True)
    old = []
    if os.path.exists(CSV_PATH):
        with open(CSV_PATH, newline="") as fh:
            old = list(csv.DictReader(fh))
    today = datetime.now(NY).strftime("%Y-%m-%d")
    if any(r["date"] == today for r in old):
        print("already collected today")
    else:
        old += collect_snapshot()
    old = backfill(old)
    with open(CSV_PATH, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(old)
    print(f"total rows: {len(old)}")


if __name__ == "__main__":
    main()
