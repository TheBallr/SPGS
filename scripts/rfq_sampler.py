#!/usr/bin/env python3
"""Sample market-maker quotes on same-game NBA combos via Kalshi RFQs. Never trades.

Flow per combo:  create (or look up) the combo market in the cross-category collection
  -> POST an RFQ with obscure_creator_id -> wait -> read every quote -> DELETE the RFQ.
Accept/confirm endpoints are never called.

Usage:
  python3 scripts/rfq_sampler.py --env demo --date 26NOV04 --dry-run      # build panel only
  KALSHI_KEY_ID=... KALSHI_KEY_FILE=... python3 scripts/rfq_sampler.py --env demo --date 26NOV04

Outputs (appended): <out>/rfq_runs.csv (one row per combo per run) and <out>/rfq_quotes.csv
(one row per quote), plus <out>/rfq_raw_<ts>.json.
"""
import argparse, csv, json, math, os, sys, time, uuid
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from kalshi_combos import Kalshi, f, price_fields  # noqa: E402

BASES = {"demo": "https://demo-api.kalshi.co/trade-api/v2",
         "prod": "https://api.elections.kalshi.com/trade-api/v2"}
COLLECTION = "KXMVECROSSCATEGORY-R"


class KalshiRW(Kalshi):
    """Adds signed POST/DELETE to the read-only client."""
    def _req(self, method, path, body=None):
        url = self.base + path
        full = "/trade-api/v2" + path.split("?")[0]
        for i in range(6):
            r = self.s.request(method, url, json=body, headers=self._headers(method, full), timeout=30)
            if r.status_code == 429:
                time.sleep(1.5 * (i + 1)); continue
            break
        time.sleep(0.15)
        return r

    def post(self, path, body):
        return self._req("POST", path, body)

    def delete(self, path):
        return self._req("DELETE", path)


def mid(m):
    b, a = price_fields(m)
    if b is None or a is None or b <= 0 or a >= 1:
        return None
    return (a + b) / 2


def pick_main(markets, key=lambda m: abs((mid(m) or 9) - 0.5)):
    ms = [m for m in markets if mid(m) is not None]
    return min(ms, key=key) if ms else None


def build_panel(k, date, max_games):
    """Return list of (game, pair_type, [leg, leg]) where leg = dict(event, ticker, side, title, bid, ask)."""
    by = {}
    for st in ("KXNBAGAME", "KXNBASPREAD", "KXNBATOTAL", "KXNBATEAMTOTAL"):
        for m in k.paged("/markets", "markets", series_ticker=st, status="open", limit=1000):
            ev = m["event_ticker"]; g = ev.split("-")[1]
            if g.startswith(date):
                by.setdefault(g, {}).setdefault(st, []).append(m)
    panel = []
    eligible = [g for g in sorted(by) if len(by[g].get("KXNBAGAME", [])) >= 2 and by[g].get("KXNBASPREAD") and by[g].get("KXNBATOTAL")]
    print(f"  {len(by)} games on {date}, {len(eligible)} with spread and total ladders: {eligible}")
    for g in eligible[:max_games]:
        d = by[g]
        ml = d["KXNBAGAME"]
        fav = max(ml, key=lambda m: mid(m) or 0); dog = min(ml, key=lambda m: mid(m) or 1)
        fav_code = fav["ticker"].split("-")[-1]
        fav_spreads = [m for m in d["KXNBASPREAD"] if m["ticker"].split("-")[-1].startswith(fav_code)]
        spread = pick_main(fav_spreads) or pick_main(d["KXNBASPREAD"])
        total = pick_main(d["KXNBATOTAL"])
        if spread is None or total is None:
            print(f"  skip {g}: no two-sided spread/total quotes"); continue
        tt = {}
        for m in d.get("KXNBATEAMTOTAL", []):
            code = m["ticker"].split("-")[-1].rstrip("0123456789")
            tt.setdefault(code, []).append(m)
        fav_tt = pick_main(tt.get(fav_code, [])); dog_code = dog["ticker"].split("-")[-1]; dog_tt = pick_main(tt.get(dog_code, []))

        def leg(m, side):
            b, a = price_fields(m)
            return {"event": m["event_ticker"], "ticker": m["ticker"], "side": side, "title": m.get("title"), "bid": b, "ask": a}
        # Kalshi rejects pairs where one leg implies the other (e.g. fav ML + fav spread yes,
        # dog ML + fav spread no), so those are left out.
        pairs = [
            ("favML_yes+favSpread_no", [leg(fav, "yes"), leg(spread, "no")]),   # fav wins but fails to cover
            ("favML_yes+over", [leg(fav, "yes"), leg(total, "yes")]),
            ("favML_yes+under", [leg(fav, "yes"), leg(total, "no")]),
            ("dogML_yes+over", [leg(dog, "yes"), leg(total, "yes")]),
            ("dogML_yes+under", [leg(dog, "yes"), leg(total, "no")]),
            ("favSpread_yes+over", [leg(spread, "yes"), leg(total, "yes")]),
            ("favSpread_yes+under", [leg(spread, "yes"), leg(total, "no")]),
            ("favSpread_no+under", [leg(spread, "no"), leg(total, "no")]),
        ]
        if fav_tt:
            pairs += [("favTT_over+over", [leg(fav_tt, "yes"), leg(total, "yes")]),
                      ("favTT_over+favML_yes", [leg(fav_tt, "yes"), leg(fav, "yes")])]
        if fav_tt and dog_tt:
            pairs += [("favTT_over+dogTT_under", [leg(fav_tt, "yes"), leg(dog_tt, "no")])]
        for pt, legs in pairs:
            if all(l["ticker"] for l in legs):
                panel.append((g, pt, legs))
    return panel


def leg_prob(l):
    if l["bid"] is None or l["ask"] is None:
        return None
    p = (l["bid"] + l["ask"]) / 2
    return 1 - p if l["side"] == "no" else p


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", choices=BASES, default="demo")
    ap.add_argument("--date", required=True, help="game date code in tickers, e.g. 26NOV04")
    ap.add_argument("--max-games", type=int, default=6)
    ap.add_argument("--contracts", default="10", help="RFQ size in contracts (fixed-point string)")
    ap.add_argument("--wait", type=float, default=25, help="seconds to wait for quotes")
    ap.add_argument("--out", default="out/rfq")
    ap.add_argument("--dry-run", action="store_true", help="build the panel and stop before any signed write")
    ap.add_argument("--limit", type=int, default=0, help="only run the first N panel entries")
    a = ap.parse_args()

    key_id = os.environ.get("KALSHI_KEY_ID"); key_file = os.environ.get("KALSHI_KEY_FILE")
    pem = Path(key_file).read_bytes() if key_file else None
    if not a.dry_run and not (key_id and pem):
        sys.exit("set KALSHI_KEY_ID and KALSHI_KEY_FILE (a key for the chosen env)")
    k = KalshiRW(key_id, pem, BASES[a.env])
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    run_ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    panel = build_panel(k, a.date, a.max_games)
    if a.limit:
        panel = panel[:a.limit]
    print(f"[{a.env}] panel: {len(panel)} combos across {len({g for g,_,_ in panel})} games")
    for g, pt, legs in panel:
        ps = [leg_prob(l) for l in legs]
        prod = (ps[0] * ps[1]) if None not in ps else None
        print(f"  {g} {pt:28s} " + " | ".join(f"{l['side']} {l['title']} [{l['bid']},{l['ask']}]" for l in legs) + f"  product={prod and round(prod,4)}")
    if a.dry_run:
        return

    # 1. create combo markets and RFQs
    active = []; my_user_id = None
    for g, pt, legs in panel:
        body = {"selected_markets": [{"event_ticker": l["event"], "market_ticker": l["ticker"], "side": l["side"]} for l in legs]}
        r = k.post(f"/multivariate_event_collections/{COLLECTION}", body)
        if not r.ok:
            print("  create market failed", g, pt, r.status_code, r.text[:200]); continue
        mt = r.json().get("market_ticker")
        r2 = k.post("/communications/rfqs", {"market_ticker": mt, "contracts_fp": a.contracts, "rest_remainder": False,
                                             "obscure_creator_id": True, "replace_existing": True})
        if not r2.ok:
            print("  rfq failed", g, pt, mt, r2.status_code, r2.text[:200]); continue
        rj = r2.json(); rfq_id = rj.get("id") or rj.get("rfq", {}).get("id")
        my_user_id = rj.get("creator_user_id") or rj.get("rfq", {}).get("creator_user_id") or my_user_id
        if not my_user_id:  # the create response omits it; the single-RFQ read has it
            try:
                rr = k.get(f"/communications/rfqs/{rfq_id}"); my_user_id = (rr.get("rfq") or rr).get("creator_user_id")
            except Exception as e:
                print("  could not read back rfq", str(e)[:120])
        active.append({"game": g, "pair_type": pt, "legs": legs, "combo_ticker": mt, "rfq_id": rfq_id, "created": time.time(), "raw_create": r2.json()})
        print(f"  RFQ {rfq_id} on {mt} ({pt})")
    print(f"{len(active)} RFQs open; polling quotes for {a.wait}s (rfq creator user id {my_user_id})")
    # 2. poll quotes every few seconds (the quotes endpoint needs the RFQ creator's user id), then delete RFQs
    seen = {x["rfq_id"]: {} for x in active}
    t0 = time.time()
    while time.time() - t0 < a.wait:
        for x in active:
            try:
                for q in k.get("/communications/quotes", rfq_id=x["rfq_id"], rfq_creator_user_id=my_user_id, limit=500).get("quotes", []):
                    if q.get("id") not in seen[x["rfq_id"]]:
                        q["_first_seen_s"] = round(time.time() - x["created"], 1)
                        seen[x["rfq_id"]][q["id"]] = q
            except Exception as e:
                print("  quotes fetch failed", x["rfq_id"], str(e)[:160]); break
        time.sleep(4)
    quotes_rows = []
    for x in active:
        qs = list(seen[x["rfq_id"]].values())
        x["quotes"] = qs
        for q in qs:
            quotes_rows.append({"run_ts": run_ts, "env": a.env, "rfq_id": x["rfq_id"], "combo_ticker": x["combo_ticker"], "game": x["game"], "pair_type": x["pair_type"],
                                "quote_id": q.get("id"), "maker_id": q.get("creator_id"), "yes_bid": f(q.get("yes_bid_dollars")), "no_bid": f(q.get("no_bid_dollars")),
                                "status": q.get("status"), "quote_created_ts": q.get("created_ts"), "seconds_after_rfq": q.get("_first_seen_s")})
        rd = k.delete(f"/communications/rfqs/{x['rfq_id']}")
        x["delete_status"] = rd.status_code
    # 3. summarise
    rows = []
    for x in active:
        ps = [leg_prob(l) for l in x["legs"]]
        prod = ps[0] * ps[1] if None not in ps else None
        live = [q for q in x["quotes"] if f(q.get("yes_bid_dollars")) is not None]
        buy_yes = min([1 - f(q["no_bid_dollars"]) for q in live if f(q.get("no_bid_dollars"))], default=None)   # cheapest way for us to buy YES
        sell_yes = max([f(q["yes_bid_dollars"]) for q in live if f(q.get("yes_bid_dollars"))], default=None)   # best price to sell YES
        m = (buy_yes + sell_yes) / 2 if buy_yes is not None and sell_yes is not None else None
        rows.append({"run_ts": run_ts, "env": a.env, "game": x["game"], "pair_type": x["pair_type"], "combo_ticker": x["combo_ticker"], "rfq_id": x["rfq_id"],
                     "leg1": x["legs"][0]["ticker"], "leg1_side": x["legs"][0]["side"], "leg1_bid": x["legs"][0]["bid"], "leg1_ask": x["legs"][0]["ask"],
                     "leg2": x["legs"][1]["ticker"], "leg2_side": x["legs"][1]["side"], "leg2_bid": x["legs"][1]["bid"], "leg2_ask": x["legs"][1]["ask"],
                     "product_of_leg_mids": prod, "n_quotes": len(live), "n_makers": len({q.get("creator_id") for q in live}),
                     "best_buy_yes": buy_yes, "best_sell_yes": sell_yes, "quote_mid": m, "maker_spread": (buy_yes - sell_yes) if m is not None else None,
                     "R_mid": (m / prod) if m and prod else None, "R_buy": (buy_yes / prod) if buy_yes and prod else None, "delete_status": x["delete_status"]})
    for name, data in (("rfq_runs.csv", rows), ("rfq_quotes.csv", quotes_rows)):
        p = out / name; new = not p.exists()
        if data:
            with open(p, "a", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=list(data[0].keys())); 
                if new: w.writeheader()
                w.writerows(data)
    (out / f"rfq_raw_{run_ts.replace(':','')}.json").write_text(json.dumps(active, indent=1, default=str))
    print(f"done: {len(rows)} combos, {len(quotes_rows)} quotes; {sum(1 for r in rows if r['n_quotes'])} combos got at least one quote")
    for r in rows:
        print(f"  {r['game']} {r['pair_type']:28s} quotes={r['n_quotes']} buy_yes={r['best_buy_yes']} sell_yes={r['best_sell_yes']} product={r['product_of_leg_mids'] and round(r['product_of_leg_mids'],4)} R_mid={r['R_mid'] and round(r['R_mid'],3)}")


if __name__ == "__main__":
    main()
