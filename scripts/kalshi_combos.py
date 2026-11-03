#!/usr/bin/env python3
"""Snapshot Kalshi combo (multivariate) markets for one sport and their legs.

Usage:
  KALSHI_KEY_ID=... KALSHI_KEY_FILE=/path/key.pem python3 scripts/kalshi_combos.py --sport NBA --out out/

Writes:
  out/collections.json   every open collection (raw)
  out/combos_raw.json    raw combo events + nested markets for the chosen sport
  out/legs_raw.json      raw leg markets referenced by those combos
  out/combos.csv         one row per combo: quotes, legs, product of leg mids, R

Credentials never go into the repo: pass them via env vars.
Public GET endpoints do not need a signature, but we sign everything so the
same client works for the (authenticated) create-market endpoint later.
"""
import argparse, base64, csv, json, os, sys, time, math
from pathlib import Path
import requests
from cryptography.hazmat.primitives import serialization

BASES = [
    "https://external-api.kalshi.com/trade-api/v2",   # current SDK default (v3.35)
    "https://api.elections.kalshi.com/trade-api/v2",  # older host, still documented widely
]

class Kalshi:
    def __init__(self, key_id, key_pem, base=None):
        self.key_id = key_id
        self.key = serialization.load_pem_private_key(key_pem, None) if key_pem else None
        self.base = base
        self.s = requests.Session()
        ca = os.environ.get("REQUESTS_CA_BUNDLE") or "/root/.ccr/ca-bundle.crt"
        if os.path.exists(ca):
            self.s.verify = ca

    def _headers(self, method, path):
        if not self.key:
            return {}
        ts = str(int(time.time() * 1000))
        msg = (ts + method.upper() + path).encode()
        kt = type(self.key).__name__
        if "Ed25519" in kt:
            sig = self.key.sign(msg)
        else:  # RSA-PSS SHA256, salt = digest length
            from cryptography.hazmat.primitives import hashes
            from cryptography.hazmat.primitives.asymmetric import padding
            sig = self.key.sign(msg, padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                                                 salt_length=hashes.SHA256.digest_size), hashes.SHA256())
        return {"KALSHI-ACCESS-KEY": self.key_id,
                "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
                "KALSHI-ACCESS-TIMESTAMP": ts}

    def get(self, path, **params):
        bases = [self.base] if self.base else BASES
        last = None
        for b in bases:
            url = b + path
            full_path = "/trade-api/v2" + path          # signed path = URL path, no query string
            try:
                r = self.s.get(url, params=params, headers=self._headers("GET", full_path), timeout=30)
            except requests.RequestException as e:
                last = e; continue
            tries = 0
            while r.status_code == 429 and tries < 8:
                tries += 1; time.sleep(1.5 * tries)
                r = self.s.get(url, params=params, headers=self._headers("GET", full_path), timeout=30)
            time.sleep(0.35)  # stay under the rate limit on long pages
            if r.ok:
                self.base = b
                return r.json()
            last = RuntimeError(f"{r.status_code} {url} {r.text[:300]}")
        raise last

    def paged(self, path, key, **params):
        out, cursor = [], None
        while True:
            d = self.get(path, **({**params, "cursor": cursor} if cursor else params))
            out.extend(d.get(key, []))
            cursor = d.get("cursor")
            if len(out) and len(out) % 5000 < len(d.get(key, [])):
                print(f"  ...{len(out)} {key} so far", flush=True)
            if not cursor:
                return out

def f(x):
    try: return float(x)
    except (TypeError, ValueError): return None

def price_fields(m):
    """Return yes_bid, yes_ask in dollars from either the new *_dollars or old cent fields."""
    yb = f(m.get("yes_bid_dollars")); ya = f(m.get("yes_ask_dollars"))
    if yb is None and m.get("yes_bid") is not None: yb = m["yes_bid"] / 100
    if ya is None and m.get("yes_ask") is not None: ya = m["yes_ask"] / 100
    return yb, ya

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sport", default="NBA")
    ap.add_argument("--out", default="out")
    ap.add_argument("--base", default=None)
    ap.add_argument("--create-hours", type=float, default=1, help="scan combos created in the last N hours (Kalshi creates ~100+ combos per second exchange-wide)")
    ap.add_argument("--max-pages", type=int, default=600, help="safety cap on 1000-row pages scanned")
    a = ap.parse_args()
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)

    key_id = os.environ.get("KALSHI_KEY_ID")
    key_pem = Path(os.environ["KALSHI_KEY_FILE"]).read_bytes() if os.environ.get("KALSHI_KEY_FILE") else None
    k = Kalshi(key_id, key_pem, a.base)

    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    sport = a.sport.upper()
    pref = "KX" + sport  # leg event tickers look like KXNBAGAME-26NOV04PHIDET

    # 1. collections (response key is multivariate_contracts, not the SDK's name)
    cols = k.paged("/multivariate_event_collections", "multivariate_contracts", limit=200)
    (out / "collections.json").write_text(json.dumps(cols, indent=1))
    live = [c for c in cols if (c.get("close_date") or "") > now]
    print(f"{len(cols)} collections, {len(live)} live")

    # 2. detail for live collections; keep the sport's events and their quoters
    details, sport_events = {}, {}
    for c in live:
        d = k.get(f"/multivariate_event_collections/{c['collection_ticker']}").get("multivariate_contract", {})
        evs = [e for e in d.get("associated_events", []) if e.get("ticker", "").startswith(pref)]
        if evs:
            d["associated_events"] = evs
            details[c["collection_ticker"]] = d
            for e in evs:
                sport_events[e["ticker"]] = e
            print(f"  {c['collection_ticker']}: {len(evs)} {sport} events, legs {d.get('size_min')}-{d.get('size_max') or 'no max'}, "
                  f"quoted events {sum(1 for e in evs if e.get('active_quoters'))}")
    (out / "collection_details.json").write_text(json.dumps(details, indent=1))

    # 3. find this sport's combos two ways, streaming so memory stays small:
    #    (a) combos that traded in the last --trade-hours (executed RFQ prices = real MM quotes)
    #    (b) combos created in the last --create-hours (what users are building right now)
    def legs_of(m):
        sel = m.get("mve_selected_legs")
        if sel:
            return [{"event_ticker": l.get("event_ticker"), "market_ticker": l.get("market_ticker"), "side": l.get("side")} for l in sel]
        cs = m.get("custom_strike") or {}
        ev = (cs.get("Associated Events") or "").split(","); mk = (cs.get("Associated Markets") or "").split(","); sd = (cs.get("Associated Market Sides") or "").split(",")
        return [{"event_ticker": e, "market_ticker": t, "side": sdx} for e, t, sdx in zip(ev, mk, sd) if t]
    def is_sport_only(m):
        l = legs_of(m)
        return bool(l) and all((x["event_ticker"] or "").startswith(pref) for x in l)

    nowts = int(time.time())
    # (b) scan recent creations first: this is the cheap way to find the sport's combos
    combos, cursor, pages, seen = {}, None, 0, 0
    while True:
        q = {"mve_filter": "only", "status": "open", "min_created_ts": nowts - int(a.create_hours * 3600), "limit": 1000}
        if cursor: q["cursor"] = cursor
        d = k.get("/markets", **q); pages += 1
        for m in d.get("markets", []):
            seen += 1
            if is_sport_only(m):
                m["_legs"] = legs_of(m); m["_source"] = "created"; combos[m["ticker"]] = m
        cursor = d.get("cursor")
        if pages % 10 == 0: print(f"  created: {pages} pages, {seen} combos scanned, {len(combos)} {sport} kept", flush=True)
        if not cursor or pages >= a.max_pages: break
    print(f"{seen} combos created in last {a.create_hours}h scanned; {len(combos)} {sport}-only")
    # (a) then pull the public trades for each of those combos (executed RFQ prices = real MM quotes)
    for i, (tkr, m) in enumerate(combos.items()):
        if f(m.get("volume_fp")) in (None, 0.0):
            m["_trades"] = None; continue
        d = k.get("/markets/trades", ticker=tkr, limit=1000)
        r = {"n": 0, "contracts": 0.0, "notional": 0.0, "last_price": None, "last_time": None, "taker_yes": 0}
        for t in d.get("trades", []):
            c = f(t.get("count_fp")) or 0.0; px = f(t.get("yes_price_dollars")) or 0.0
            r["n"] += 1; r["contracts"] += c; r["notional"] += c * px
            if r["last_time"] is None or t.get("created_time", "") > r["last_time"]:
                r["last_time"] = t.get("created_time"); r["last_price"] = px
            if t.get("taker_side") == "yes": r["taker_yes"] += 1
        m["_trades"] = r
        if (i + 1) % 100 == 0: print(f"  trades fetched for {i+1}/{len(combos)}", flush=True)
    print(f"{sum(1 for m in combos.values() if m.get('_trades'))} {sport} combos have trades")
    combos = list(combos.values())
    (out / "combos_raw.json").write_text(json.dumps(combos, indent=1))
    combo_mkts = [(None, m) for m in combos]

    # 4. legs
    leg_tickers = sorted({l["market_ticker"] for m in combos for l in m["_legs"]})
    legs = {}
    for i in range(0, len(leg_tickers), 100):
        chunk = leg_tickers[i:i+100]
        for m in k.get("/markets", tickers=",".join(chunk), limit=1000).get("markets", []):
            legs[m["ticker"]] = m
    (out / "legs_raw.json").write_text(json.dumps(legs, indent=1))
    print(f"{len(leg_tickers)} distinct legs, {len(legs)} fetched")

    # fee info for the combo series and the leg series
    fees = {}
    for st in sorted({m.get("event_ticker", "").split("-")[0] for m in combos} | {t.split("-")[0] for t in leg_tickers}):
        try:
            fees[st] = {kk: v for kk, v in k.get(f"/series/{st}").get("series", {}).items() if "fee" in kk or kk in ("ticker", "title")}
        except Exception as e:
            fees[st] = {"error": str(e)[:200]}
    (out / "series_fees.json").write_text(json.dumps(fees, indent=1))

    # 5. table
    snap = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    maxlegs = max([len(m["_legs"]) for _, m in combo_mkts] + [2])
    hdr = ["snapshot_utc","collection","combo_event","combo_ticker","combo_title","status","n_legs",
           "combo_yes_bid","combo_yes_ask","combo_yes_bid_size","combo_yes_ask_size","combo_last","combo_volume","combo_open_interest","combo_close_time",
           "source","trades_in_window","contracts_traded","trade_vwap","last_trade_price","last_trade_time","fee_waiver_until"]
    for i in range(1, maxlegs+1):
        hdr += [f"leg{i}_ticker", f"leg{i}_side", f"leg{i}_title", f"leg{i}_yes_bid", f"leg{i}_yes_ask", f"leg{i}_event"]
    rows = []
    for e, m in combo_mkts:
        sel = m["_legs"]
        yb, ya = price_fields(m)
        r = [snap, m.get("mve_collection_ticker"), m.get("event_ticker"), m.get("ticker"), m.get("title") or m.get("yes_sub_title"), m.get("status"), len(sel),
             yb, ya, f(m.get("yes_bid_size_fp")), f(m.get("yes_ask_size_fp")), f(m.get("last_price_dollars")),
             f(m.get("volume_fp")), f(m.get("open_interest_fp")), m.get("close_time")]
        tr = m.get("_trades") or {}
        r += [m.get("_source"), tr.get("n"), tr.get("contracts"),
              (tr["notional"] / tr["contracts"]) if tr.get("contracts") else None, tr.get("last_price"), tr.get("last_time"), m.get("fee_waiver_expiration_time")]
        for l in sel:
            lm = legs.get(l.get("market_ticker"), {})
            lb, la = price_fields(lm)
            r += [l.get("market_ticker"), l.get("side"), (lm.get("title") or lm.get("yes_sub_title") or ""), lb, la, l.get("event_ticker")]
        r += [None] * (len(hdr) - len(r))
        rows.append(r)
    with open(out / "combos.csv", "w", newline="") as fh:
        w = csv.writer(fh); w.writerow(hdr); w.writerows(rows)
    print("wrote", out / "combos.csv", len(rows), "rows; max legs", maxlegs)

if __name__ == "__main__":
    main()
