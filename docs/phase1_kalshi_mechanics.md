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
