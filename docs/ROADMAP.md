# Kalshi Correlated Combos: Research Roadmap

Goal: determine whether Kalshi market makers systematically misprice correlation in
multi-leg sports combos, and if so, build a scanner that flags +EV combos net of
fees and spread.

Core measurement: for any combo with legs L1..Ln,

    R = P(combo) / (P(L1) * P(L2) * ... * P(Ln))

R = 1 means the quote treats the legs as independent. R > 1 implies positive
correlation, R < 1 negative. Compute R for Kalshi quotes, for the FanDuel SGP
reference, and for our own model. Edge lives in the gaps between them.

## Phase 0: Infrastructure and logging (start immediately, runs throughout)
- Kalshi API key, read-only first.
- Log every combo quote plus the contemporaneous leg quotes (bid/ask/size/time).
- Market data cannot be backfilled, so logging starts before the theory is finished.
- Store in a simple table (Sheets or CSV) that later phases can read.

## Phase 1: Kalshi combo market mechanics
Purpose: know exactly what we are trading before modeling it.
- Which sports, leg types (game lines only? props?), same-game vs cross-game, leg count.
- Who quotes: order book vs single market-maker quote, how often it updates.
- Can we take the No side of a combo? (Shorting an overpriced parlay is a structural
  edge FanDuel cannot offer.)
- Fee schedule for combos, typical spread, size available, position limits.
- Settlement rules: pushes, voids, OT, postponements, and how they differ from FanDuel.
- First look: 20-30 hand-collected combos with legs, compute R.
Deliverable: a one-page mechanics brief and a Sheet of first R observations.

## Phase 2: Correlation theory and the FanDuel reference method
Purpose: understand what an SGP price encodes and how sharps extract correlation.
- Ratio method: FD_SGP / product(FD vigged legs) isolates FD's correlation
  adjustment with most of the vig cancelling.
- Where FD's own model is known to be shaded or conservative.
- Multiplicative vs additive devig for parlays; why SGP hold is not observable.
- What a "correct" correlation looks like for common leg pairs (ML+spread, ML+total,
  spread+total, player props vs game script).
Deliverable: a written method for turning FD prices into a reference R.

## Phase 3: Data collection at scale
Purpose: build the dataset that every later phase depends on.
- Automate Kalshi combo + leg capture via API.
- Capture FD SGP prices for matched combos (manual, third-party SGP tools, or feed).
- Capture closing lines and results for CLV and settlement.
Deliverable: a growing matched dataset of Kalshi R, FD R, and outcomes.

## Phase 4: Reverse-engineer Kalshi market-maker pricing
Purpose: find the rule the MM is using, not just individual gaps.
- Distribution of Kalshi R by leg-pair type, sport, time to start.
- Hypotheses to test: MM prices independent (R=1); MM copies FD; MM uses a fixed
  multiplier table; MM widens spread instead of adjusting R.
- Does R move when legs move? Does it move after our trades?
Deliverable: a pricing-rule profile of the MM with confidence levels.

## Phase 5: Independent fair value
Purpose: stop relying on FD as truth.
- Empirical correlation from historical results (box scores, play-by-play).
- Simulation model for game-script pairs.
- Reconcile model R vs FD R vs Kalshi R; identify where FD is itself off.
Deliverable: a fair-value R for each combo type with error bars.

## Phase 6: Edge validation
Purpose: prove the edge is real before scaling.
- Paper-trade flagged combos, track CLV on the combo and on each leg.
- Realized P&L has too much variance for parlays; CLV and quote-reaction are the
  primary validation metrics.
- Net out fees, spread, and fill rate.
Deliverable: go / no-go by combo category.

## Phase 7: Scanner and execution
Purpose: automate what Phase 6 validated.
- Scan combos, compute fair R, flag EV above a threshold net of fees.
- Alerts, sizing, logging, and MM-reaction monitoring.

## Assumptions to challenge
- MMs may already price correlation, or copy FD. Measure, do not assume.
- FanDuel shades SGPs in its own favor; the FD-Kalshi gap can overstate edge.
- The real edge may be spread and stale quotes rather than correlation.
- Size limits and MM reaction cap scale; edge decays once hit.
- No official FD API; scraping is brittle and against ToS.
- Settlement-rule mismatches can turn a modeled edge into a loss.
