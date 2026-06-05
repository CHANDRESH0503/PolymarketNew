# Competitor wallet analysis — Polymarket daily-temperature traders

Peer traders worth learning from, profiled the same way this project reverse-
engineered `@lactesting`. **Everything here is public on-chain data**, not X
scraping: candidates came from Polymarket's own **weather leaderboard**
(`GET https://data-api.polymarket.com/v1/leaderboard?category=WEATHER&orderBy=PNL`),
and each strategy below is computed from the wallet's real `activity`/`positions`
via `src/polymarket/data_api.py`. X handles are the ones the trader themselves
linked on their Polymarket profile.

> Snapshot: 2026-06-05. PnL/volume are leaderboard all-time; the strategy
> metrics (side bias, entry price, cities, timing) are from each wallet's most
> recent ~500 temperature trades. Re-run any row with
> `python scripts/analyze_trader.py <wallet>`.

## At a glance

| Wallet (name) | X | All-time PnL / Vol | Edge style | Entry px | Side | Enters | Cities |
|---|---|---|---|---|---|---|---|
| `0x594edb…1c11` (ColdMath) | — | $135k / $10.8M | HFT cheap-longshot scatter, both tails | ~0.04 | all BUY (Yes+No) | ~14h before | **NYC, London** |
| `0xd8f8c1…0f11` (automatedAItradingbot) | — | $65k / $2.6M | cheap-Yes longshots, **our exact Asian set** | ~0.02 | Yes-heavy | ~14h before | Taipei, Shanghai, Seoul, Moscow |
| `0x1f6679…4c8d` (ShyGuy1) | @Mask4che | $65k / $5.3M | cheap-Yes Asian longshots (top recent PnL) | ~0.15 | Yes-heavy | ~16h before | Seoul, Shenzhen, HK, NYC |
| `0x15ceff…d5fa` (HondaCivic) | @0xMarchyel | $59k / $7.4M | **No-favorite harvesting** (sell tails) | ~1.00 | No-heavy | ~5h before | Moscow, Chicago, NYC, Toronto |
| `0x6011655c…b31e` (HighTempTation) | — | $57k / $1.5M | No-favorite harvesting, **wide city set** | ~1.00 | No-heavy (lots of SELL) | mixed | Taipei, Cape Town, Wellington, Jeddah |
| `0x0f37cb…1410` (Hans323) | @Hans323 | $81k / $7.2M | buy near-certain favorites, low risk | ~0.96 | No-heavy | late | NYC, London, Paris, Shenzhen |

---

## The wallets

### 1. ColdMath — `0x594edb9112f526fa6a80b8f858a6379c8a2c1c11`
- **Leaderboard:** $135k PnL on **$10.8M** volume (the highest-turnover weather bot on the board).
- **Observed:** ~2,800 temp trades/day, **all BUY**, median entry **0.04**, split across **Yes (268) and No (213)**. Concentrated almost entirely in **New York City (407)** + **London (55)**. Enters ~14.5h before resolution. Currently ~450 open positions, ~$46k value, ~-$6.6k unrealized.
- **Read:** a high-frequency **scatter-the-cheap-tails / two-sided maker** on the two deepest weather books. It isn't forecasting a winner so much as buying every out-of-the-money bucket cheaply on both sides and harvesting the ones that drift.
- **Helps us:** (a) **Liquidity concentration** — NYC and London are where size lives; our universe is Asian-heavy, so adding NYC/London lets us actually deploy capital. (b) Validates **two-sided cheap-bucket** entries, which our coherence/arb module (`arbitrage.py`) is built for but underuses. (c) A live benchmark for how thin our depth-aware fills assumption is on the deep books.

### 2. automatedAItradingbot — `0xd8f8c13644ea84d62e1ec88c5d1215e436eb0f11`
- **Leaderboard:** $65k PnL / $2.6M vol.
- **Observed:** ~10 temp trades/day, median entry **0.02**, **Yes-heavy (205 Yes / 26 No)**, cities are **Taipei, Shanghai, Seoul, Moscow, London** — essentially **our STATIONS universe**. Enters ~13.6h before. 49 open positions, ~$2.2k value, **+$74 unrealized (in the green)**.
- **Read:** the closest peer to us — a disciplined bot buying **cheap Yes longshots** on the same Asian daily-high markets, modest size, currently profitable.
- **Helps us:** direct **head-to-head benchmark**. Same markets, same horizon. Track this wallet's entries vs our model's bucket probs: where it buys a 0.02 Yes that our ensemble says is ~0, we're likely right; where it buys one our model also likes, that's a calibration confirmation. Good candidate for an automated "are we agreeing with the smart money?" check.

### 3. ShyGuy1 — `0x1f66796b45581868376365aef54b51eb84184c8d`  ·  X: @Mask4che
- **Leaderboard:** $65k all-time / $5.3M vol, and **#1 weather trader this week and month** — the hottest recent hand.
- **Observed:** ~92 temp trades/day, median entry **0.15**, **Yes-heavy (400/63)**, cities **Seoul (188), Shenzhen (114), NYC, Hong Kong** — heavy overlap with us. Enters ~15.6h before. But: **475 open positions, only $4.2k value, ~-$23.9k unrealized**.
- **Read:** aggressive **cheap-Yes longshot** accumulation in Asian cities. Top *realized* recent PnL but a large *open* drawdown — i.e. high variance: realizes winners fast, carries a long tail of losers.
- **Helps us:** (a) confirms Seoul/Shenzhen/HK as the productive Asian books right now. (b) A **cautionary sizing case** — exactly the over-betting our correlation-aware Kelly + per-day cap are meant to prevent. Their -$24k open mark is what independent longshot stacking looks like when a heat pattern goes against you.

### 4. HondaCivic — `0x15ceffed7bf820cd2d90f90ea24ae9909f5cd5fa`  ·  X: @0xMarchyel
- **Leaderboard:** $59k PnL / **$7.4M** vol.
- **Observed:** ~205 temp trades/day, median entry **1.00 (0.90–1.00)**, overwhelmingly **No (398) / Yes (13)**, **Western** cities (Moscow, Chicago, NYC, Toronto, Houston, Buenos Aires). Enters **late — median ~5h before, p10 ~0.1h**. Currently **-$5.6k unrealized**.
- **Read:** the scaled-up version of `@lactesting`'s bias — **buy No on unlikely buckets at ~0.97–1.00 near resolution**, collecting the last few cents of premium on near-certain outcomes. High volume, thin margins, fat-tail risk.
- **Helps us:** defines the **No-favorite harvesting** lane and its failure mode. We can do this *better* by entering on a calibrated forecast the day before (more edge, less crowding) instead of scalping pennies at T-1h. Their current red mark is the argument for our `MIN_HOURS_TO_RESOLVE` filter and tail-aware sizing.

### 5. HighTempTation — `0x6011655c4afb76f36dd1b08a137a1ba73466b31e`
- **Leaderboard:** $57k PnL / $1.5M vol.
- **Observed:** ~100 temp trades/day, median entry **1.00**, **No-heavy (476/24)** with a lot of **SELL (349)**, across a **wide, less-crowded city set**: Taipei, Cape Town, Miami, Wellington, Jeddah, Karachi, Warsaw, Tel Aviv.
- **Read:** No-favorite harvesting like HondaCivic, but spread across **newer/exotic markets** rather than the deep majors — chasing softer pricing where fewer bots compete.
- **Helps us:** a **city-expansion map**. Cape Town, Wellington, Jeddah, Karachi, Tel Aviv are markets our station registry may not cover yet; if the resolution source is auditable (`resolution_audit.py`), these are less-contested books to extend into.

### 6. Hans323 — `0x0f37cb80dee49d55b5f6d9e595d52591d6371410`  ·  X: @Hans323
- **Leaderboard:** $81k PnL / $7.2M vol.
- **Observed:** ~162 temp trades/day, median entry **0.96**, **No-heavy (334/107)**, mix of BUY/SELL, cities **NYC (354), London, Paris, Shenzhen**. Small median ticket (~$17).
- **Read:** a steady **buy-near-certain favorites** grinder on the deep books — low edge per trade, high count, low variance. The "boring but green" archetype.
- **Helps us:** the low-variance counterweight to ShyGuy1's longshot stacking. Useful as a model for a **safe sub-allocation**: a slice of bankroll on high-confidence near-1.0 buckets our forecast strongly agrees with, to stabilize the equity curve.

---

## What we should actually take from this

1. **Expand the city universe toward liquidity.** Every top bot is heavy in **NYC + London** (ColdMath, Hans323) — far deeper books than our Asian-centric set. Add them (and audit their resolution stations) so corr-Kelly has real depth to size into.
2. **Two distinct, profitable lanes exist** — *cheap-Yes longshots day-before* (automatedAItradingbot, ShyGuy1) and *No-favorite harvesting* (HondaCivic, HighTempTation, Hans323). We currently lean on the first; a calibrated, day-before version of the second is a cleaner edge than their near-resolution penny-scalping.
3. **Variance discipline is the differentiator.** ShyGuy1 (-$24k open) and HondaCivic (-$5.6k open) are top earners *carrying large drawdowns* from independent longshot stacking — precisely what our correlation-aware Kelly + cash buffer + per-day cap are designed to avoid. Sizing, not forecast, is where they're beatable.
4. **`automatedAItradingbot` is our benchmark.** Same markets, same ~14h horizon, currently green. Wire a periodic compare of its entries against our model probabilities as a live calibration signal.
5. **`HighTempTation` is a scouting list** for less-contested markets (Cape Town, Wellington, Jeddah, Karachi, Tel Aviv).

## Excluded (looked promising, weren't useful)
- **gopfan2** (`0xf2f6…5817`) and **aenews2** (`0x44c1…ebc1`) — top of the all-time weather PnL board, but **zero temperature trades in their last 500 activities**; their weather profit is stale and they've rotated into other categories. Not a current weather strategy to copy.
- **WeatherTraderBot** (`0xacc8…7d08`) — despite the name, **~$167 total volume**, effectively a dormant test wallet.

## Reproduce
```bash
# leaderboard of weather traders (names + X handles)
curl -s "https://data-api.polymarket.com/v1/leaderboard?category=WEATHER&orderBy=PNL&timePeriod=ALL&limit=25"
# deep-dive any wallet
python scripts/analyze_trader.py 0x594edb9112f526fa6a80b8f858a6379c8a2c1c11
```
