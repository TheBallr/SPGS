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
            if r.status_code == 429:
                time.sleep(1.0); r = self.s.get(url, params=params, headers=self._headers("GET", full_path), timeout=30)
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
    a = ap.parse_args()
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)

    key_id = os.environ.get("KALSHI_KEY_ID")
    key_pem = Path(os.environ["KALSHI_KEY_FILE"]).read_bytes() if os.environ.get("KALSHI_KEY_FILE") else None
    k = Kalshi(key_id, key_pem, a.base)

    # 1. collections
    cols = k.paged("/multivariate_event_collections", "multivariate_event_collections", status="open", limit=200)
    (out / "collections.json").write_text(json.dumps(cols, indent=1))
    sport = a.sport.upper()
    mine = [c for c in cols if sport in (c.get("title","") + " " + c.get("series_ticker","") + " " + c.get("collection_ticker","")).upper()]
    print(f"{len(cols)} open collections; {len(mine)} match {sport}:")
    for c in mine:
        print("  ", c["collection_ticker"], "|", c.get("series_ticker"), "|", c.get("title"), "| legs", c.get("size_min"), "-", c.get("size_max"))

    # 2. collection detail (quoters, associated events, price ranges)
    details = {}
    for c in mine:
        try:
            details[c["collection_ticker"]] = k.get(f"/multivariate_event_collections/{c['collection_ticker']}")
        except Exception as e:
            print("detail failed", c["collection_ticker"], e)
    (out / "collection_details.json").write_text(json.dumps(details, indent=1))

    # 3. existing combo events with nested markets
    combos = []
    for c in mine:
        evs = k.paged("/events/multivariate", "events", collection_ticker=c["collection_ticker"], with_nested_markets="true", limit=200)
        for e in evs:
            e["_collection_ticker"] = c["collection_ticker"]
        combos.extend(evs)
    (out / "combos_raw.json").write_text(json.dumps(combos, indent=1))
    combo_mkts = [(e, m) for e in combos for m in (e.get("markets") or [])]
    print(f"{len(combos)} combo events, {len(combo_mkts)} combo markets")

    # 4. legs
    leg_tickers = sorted({l["market_ticker"] for _, m in combo_mkts for l in (m.get("mve_selected_legs") or []) if l.get("market_ticker")})
    legs = {}
    for i in range(0, len(leg_tickers), 100):
        chunk = leg_tickers[i:i+100]
        for m in k.get("/markets", tickers=",".join(chunk), limit=1000).get("markets", []):
            legs[m["ticker"]] = m
    (out / "legs_raw.json").write_text(json.dumps(legs, indent=1))
    print(f"{len(leg_tickers)} distinct legs, {len(legs)} fetched")

    # 5. table
    snap = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    maxlegs = max([len(m.get("mve_selected_legs") or []) for _, m in combo_mkts] + [2])
    hdr = ["snapshot_utc","collection","combo_event","combo_ticker","combo_title","status","n_legs",
           "combo_yes_bid","combo_yes_ask","combo_yes_bid_size","combo_yes_ask_size","combo_last","combo_volume","combo_open_interest","combo_close_time"]
    for i in range(1, maxlegs+1):
        hdr += [f"leg{i}_ticker", f"leg{i}_side", f"leg{i}_title", f"leg{i}_yes_bid", f"leg{i}_yes_ask", f"leg{i}_event"]
    rows = []
    for e, m in combo_mkts:
        sel = m.get("mve_selected_legs") or []
        yb, ya = price_fields(m)
        r = [snap, e["_collection_ticker"], e.get("event_ticker"), m.get("ticker"), e.get("title") or m.get("title"), m.get("status"), len(sel),
             yb, ya, f(m.get("yes_bid_size_fp")), f(m.get("yes_ask_size_fp")), f(m.get("last_price_dollars")),
             f(m.get("volume_fp")), f(m.get("open_interest_fp")), m.get("close_time")]
        for l in sel:
            lm = legs.get(l.get("market_ticker"), {})
            lb, la = price_fields(lm)
            r += [l.get("market_ticker"), l.get("side"), (lm.get("yes_sub_title") or lm.get("title") or ""), lb, la, l.get("event_ticker")]
        r += [None] * (len(hdr) - len(r))
        rows.append(r)
    with open(out / "combos.csv", "w", newline="") as fh:
        w = csv.writer(fh); w.writerow(hdr); w.writerows(rows)
    print("wrote", out / "combos.csv", len(rows), "rows; max legs", maxlegs)

if __name__ == "__main__":
    main()
