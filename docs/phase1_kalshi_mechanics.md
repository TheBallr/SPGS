# Phase 1 brief: how Kalshi combos work (API view)

Source: Kalshi's official TypeScript SDK v3.35.0 (npm `kalshi-typescript`), which is generated
from Kalshi's OpenAPI spec. Items marked VERIFY need confirming against docs.kalshi.com or the
live API once network access is open.

## The instrument
- A combo is an ordinary binary Kalshi market (yes/no, settles $1 or $0) that is created on
  demand inside a "multivariate event collection". One collection per product family
  (expect one or more NBA collections).
- A combo market carries `mve_selected_legs`: a list of {event_ticker, market_ticker, side}.
  Side can be yes or no per leg, unless the collection's event is flagged `is_yes_only`.
- Because it is a binary market it has yes_bid / yes_ask / no_bid / no_ask. Taking the No
  side of a combo is possible by construction. FanDuel never offers this.
- Collections declare `size_min` / `size_max` (leg count limits), `is_ordered`, and per-event
  `size_max` (how many legs may come from the same game, which tells us whether same-game
  combos are allowed and how many legs they can have).
- Each associated event lists `active_quoters`. That is the market-maker list, per game.
- Combos have their own `price_level_structure` and `price_ranges` (tick sizes by price band),
  which may differ from single markets. VERIFY the tick table for NBA combos.

## How combos get created and quoted
- POST /multivariate_event_collections/{collection_ticker} with `selected_markets` creates (or
  returns) the combo market for that exact leg set. Limit 5000 creations per week per user.
  Signed request required.
- Once created, the market is public: anyone can see it under GET /events/multivariate
  (filter by collection_ticker, with_nested_markets=true) or GET /markets?mve_filter=only.
  So every combo any user has ever built is a free dataset of market-maker quotes.
- Quotes come from the quoters named on the collection. Whether they quote a full book or
  one level each side: VERIFY via GET /markets/{ticker}/orderbook on a few combos.

## Fees
- Series carry `fee_type` and `fee_multiplier`. Types: quadratic, quadratic_with_maker_fees,
  quadratic_with_combo_maker_fees, flat. Events can override with `fee_type_override`.
- quadratic = general trading fee table: taker fee = 0.07 x contracts x P x (1 - P), rounded
  up to the next cent per contract (fee schedule PDF; VERIFY current coefficient).
- Maker fee multiplier is 0.25 of taker for normal series and 0.50 for combo series
  (SDK docstring on the Series model). So on combos: taker ~7% x P(1-P), maker ~3.5% x P(1-P).
- `fee_waiver_expiration_time` on a market means fees are waived until then. Record it.
- Fee at P = 0.20 (typical 2-leg combo): 0.07 x 0.20 x 0.80 = 1.12 cents per contract,
  i.e. about 5.6% of the stake. Fee is a much bigger share of edge on low-probability combos.

## Reading quotes
- Price fields are fixed-point dollar strings (`yes_bid_dollars`, etc.); sizes are
  `*_size_fp` contract counts with two decimals.
- GET /markets accepts `tickers=` (comma list, up to 1000 results) so legs can be fetched in
  one call per 100 tickers.
- Default rate-limit cost is 10 tokens per request; GET /account/endpoint_costs lists
  exceptions. VERIFY the per-second budget for the basic tier.

## Authentication
- Headers: KALSHI-ACCESS-KEY (key id), KALSHI-ACCESS-TIMESTAMP (ms since epoch),
  KALSHI-ACCESS-SIGNATURE (base64).
- Signed text = timestamp + METHOD + path, where path is the URL path including
  /trade-api/v2 and excluding the query string.
- Ed25519 keys sign the raw text; RSA keys use RSA-PSS SHA-256 with salt = digest length.
- Base URL in the current SDK: https://external-api.kalshi.com/trade-api/v2. Older material
  uses https://api.elections.kalshi.com/trade-api/v2. The script tries both.

## Settlement
- Combo rules live in each market's `rules_primary` / `rules_secondary`. Pull them for a
  few NBA combos and compare with FanDuel SGP rules for pushes, voids, postponements.
  Not yet done: needs live API.

## Open questions for the first data pull
1. How many NBA collections exist and what leg types do they allow (ML, spread, total, props)?
2. Same-game combos: allowed? Max legs from one game?
3. Who are the active quoters, and is it one firm or several?
4. Is R = combo_mid / product(leg mids) clustered at 1.0 (independence pricing)?
5. Spread on combos vs the sum of leg spreads.
6. Size at the touch.

## What the live API showed (2026-11-03 snapshot)

Corrections to the SDK-based notes above:
- The collections list comes back under the key `multivariate_contracts`, and a single
  collection under `multivariate_contract`.
- Per-game NBA collections (`KXMVENBASINGLEGAME-<game>`) were used through preseason
  (last one closed 2026-10-08). Live NBA combos now sit in one exchange-wide collection,
  `KXMVECROSSCATEGORY-R` (also `KXMVESPORTSMULTIGAMEEXTENDED-R`), which lists 55 NBA
  events (game, spread, total, team total, 1H, 1Q, race-to-points) with `size_max: 1`
  per event. Same-game combos are allowed by mixing events of the same game.
- `active_quoters` was empty on every NBA event in the snapshot. Quoters are not
  discoverable from this field right now.
- Combo legs are in `mve_selected_legs` and also in `custom_strike` as comma lists
  ("Associated Events", "Associated Markets", "Associated Market Sides").
- Combo markets carry no `rules_primary`; settlement is inherited from the legs.
- Combos price via RFQ (docs.kalshi.com/getting_started/rfqs), not a resting book.
  The app sends an RFQ when a user builds a parlay; makers answer with private
  yes_bid / no_bid quotes for the full size; the requester accepts one side; the maker
  confirms within 3 seconds (combos are "High Volatility Markets"); the fill prints to
  the public tape 1 second later. Quotes are private to requester and maker. A quote
  with both sides must satisfy yes_bid + no_bid <= 1.
- Consequence: of 4,504 NBA-only combos created in a one-hour window, zero had a
  resting two-sided quote. The observable record of market-maker pricing is (a) the
  public trade tape on combo markets and (b) quotes returned to our own RFQs.
- Kalshi creates roughly 100+ combo markets per second exchange-wide (over one million
  open combo markets). Full scans are not practical; the script scans by creation time
  and filters to the sport.
- Fee types confirmed from the API: the combo series use `quadratic_with_combo_maker_fees`
  (multiplier 1); NBA game, spread and total series use `quadratic_with_maker_fees`;
  NBA 1H, 1Q, team total and race series use plain `quadratic`.
- Rate limits: Basic tier is 200 read tokens/s and 100 write tokens/s at 10 tokens per
  call; unauthenticated calls hit 429 fast. Advanced tier is a free upgrade via
  POST /account/api_usage_level/upgrade.
- Demo environment exists (external-api.demo.kalshi.co) for testing the RFQ flow.

First-look numbers (305 traded NBA combos, one hour, 2026-11-03 15:44 to 16:48 UTC):
- Same-game combos are rare: 147 of 4,504 built, 10 of 305 traded.
- Most traded combos are multi-game moneyline parlays of 4 to 10 legs.
- R = trade VWAP / product of side-adjusted leg midpoints:
  2-leg cross-game median 1.04 (n=11); 3-leg cross-game 1.04 (n=21);
  4-leg cross-game 1.11 (n=36); 2-leg same-game 1.09 (n=5); 3-leg same-game 2.0 (n=3).
  Cross-game R above 1 is the maker's margin on taker executions. Same-game R far
  above 1 means positive correlation is being priced in, not independence.
- Sample sizes are tiny and notional is small (about $3,100 across 305 combos).

## Revised Phase 1 to-do
1. Create RFQs ourselves on a fixed panel of 2-leg same-game combos (ML+spread, ML+total,
   spread+total, team total pairs) and record the quotes without trading. Test whether
   deleting unexecuted RFQs draws any maker penalty (docs penalize makers, not requesters).
2. Keep logging the public trade tape for NBA combos on a schedule (every 15 minutes).
3. Verify the 0.07 fee coefficient on the fee PDF from a normal browser.
4. Request the Advanced rate-limit tier.
