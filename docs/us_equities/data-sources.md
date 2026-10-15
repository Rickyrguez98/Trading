# Data Sources

This project uses only free / public / freemium data. Each source has known
gaps; we document them rather than paper over them.

## Tickers / universe

### Primary: NASDAQ Trader FTP
- URL: `https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt`
       `https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt`
- Coverage: All NASDAQ-listed (`nasdaqlisted`) and NYSE / NYSE American / NYSE
  Arca / BATS / IEX (`otherlisted`) securities.
- Limitations: Includes ETFs, ADRs, warrants, units, preferreds, rights, and
  test issues. We filter these in `universe.py`. Updated daily, but the FTP
  endpoint occasionally returns stale or empty payloads — hence the fallback.

### Fallback: SEC company tickers
- URL: `https://www.sec.gov/files/company_tickers.json`
- Coverage: Companies with current SEC filings (CIK-mapped). Reliable but no
  exchange info and no asset-type classification.

## Fundamentals

### Default: yfinance
- Unofficial wrapper around Yahoo Finance. Free, keyless, broad coverage.
- Known limitations:
  - No SLA. Yahoo may change endpoints without notice.
  - Some fields (forward P/E, PEG, FCF) are missing for many small caps.
  - Trailing fundamentals can lag by a quarter or two.
- We treat every yfinance field as **best-effort** and explicitly track
  `missing_fields` per ticker.

### Alternatives (provider stubs, future)
- **Finnhub** (free tier, key required) — better-structured fundamentals.
- **Alpha Vantage** (free tier, key required) — slower rate limit, good
  historical depth.
- **Financial Modeling Prep** (free tier, key required) — convenient ratios.
- **SEC EDGAR direct** — authoritative, but requires XBRL parsing.

## Prices

### Default: yfinance
- Used only for liquidity filters (avg dollar volume), recent return, and a
  vol proxy. **Not** used for execution decisions in this milestone.

### Backup (implemented): Stooq
- Keyless CSV download (`https://stooq.com/q/d/l/`). Registered as the `stooq`
  prices provider and used as a live fallback when yfinance fails — enable with
  `--provider prices=yfinance,stooq` or
  `robustness.provider_priority_by_data_type.prices: [yfinance, stooq]`.
- Daily OHLCV only; class shares use the same dot→hyphen normalization as
  yfinance (`BRK.B` → `BRK-B`). Coverage is U.S.-broad but not guaranteed for
  every micro-cap.

### Alternative (future stub)
- **Alpha Vantage** TIME_SERIES_DAILY_ADJUSTED — keyed, rate-limited.

## News

Three backends, in descending order of reliability. The shipped config chains
the first two: `news: [finnhub, yahoo_rss]`.

### Default: `finnhub` — documented company-news API (recommended)
- Endpoint: `GET https://finnhub.io/api/v1/company-news?symbol=<SYM>&from=&to=`
- **Requires a free key in `FINNHUB_API_KEY`** (https://finnhub.io/register).
  The key is sent in the `X-Finnhub-Token` **header**, never as a query
  parameter, so it cannot leak into URLs, logs, or cache keys.
- This is the only news backend here whose terms actually permit automated
  access, which makes it the right choice for any unattended or shared run.
- It returns a genuine per-article `source`, so the source-diversity term in
  the sentiment-confidence model is meaningful rather than inflated.
- Free tier covers US-listed companies with roughly a year of history.
- A missing key raises `NewsProviderNotConfigured` **before any network call**
  (one warning per run, no wasted requests). That is a configuration fault, not
  "no news", so it can never quietly become neutral sentiment — in a chain the
  next provider takes over; as the sole provider the run reports news
  unavailable.

### Fallback: `yahoo_rss` — keyless RSS headline feed (best-effort)
- URL shape: `https://feeds.finance.yahoo.com/rss/2.0/headline?s=<SYMBOL>&region=US&lang=en-US`
- **Keyless**, a feed in a standard format rather than a private JSON API, and
  parsed with the standard library — no extra dependency, no HTML scraping.
- **Access-policy caveat, stated plainly.** `feeds.finance.yahoo.com/robots.txt`
  serves `User-agent: * / Disallow: /`, and this provider sends a browser-like
  `User-Agent` because the feed answers 403 to a default client UA. The same is
  true of `query1.finance.yahoo.com`, the host behind every `yfinance` price and
  fundamentals call this project already makes — so this adds no exposure the
  pipeline did not already have, but it is not a sanctioned integration either.
  Prefer `finnhub` when that matters to you.
- **Observed throttling.** From datacenter and VPN IPs this host returns a
  persistent HTTP 429 (verified repeatedly while price calls on
  `query1.finance.yahoo.com` kept succeeding, so it is host-specific, not
  general contention). If your sentiment columns come back empty, this is the
  usual cause and a Finnhub key is the fix.
- Limitations: headlines plus a short description only (never full article
  text); depth varies per ticker; the feed carries no reliable per-article
  publisher, so `source` is left **empty** unless a `<source>` element is
  present. That is deliberate — inventing a publisher would inflate the
  source-diversity term and overstate confidence.

### `yfinance` news — endpoint retired by Yahoo
Still selectable and chainable, but it yields nothing. It calls Yahoo's private
`xhr/ncp?queryRef=latestNews` endpoint, which now answers **HTTP 404**;
`Ticker.news` returns an empty list for every `tab` option. The provider
therefore produces no articles and the run reports `NO_NEWS_DATA` with provider
health `degraded` — honest behavior, but no sentiment.

### Changing the chain

```yaml
providers:
  news: finnhub
robustness:
  provider_priority_by_data_type:
    news: [finnhub, yahoo_rss]     # the shipped default
```

### Deliberately not implemented
- **Google News RSS** — rejected on policy grounds, not capability. It is
  reachable and returns real headlines, but `news.google.com/robots.txt`
  disallows `/rss/` for all agents and names automated crawlers explicitly.
  Using it would mean overriding a stated access policy, which this project
  does not do.
- **MarketAux**, **NewsAPI.org**, **Alpha Vantage news** — viable free tiers;
  each needs a key. Not implemented because the repository does not claim
  support for a provider path it has not exercised. `.env.example` lists the
  key names if you want to add one.

### Verification status (be precise about this)
- The Finnhub provider's parsing, header handling, error contract, and the
  keyless fall-through to `yahoo_rss` are covered by offline tests with a
  stubbed HTTP layer, and sentiment has been confirmed to populate end-to-end
  through the stage-4 path (positive and negative headline sets producing
  scores of 60.3 and 17.1 against a neutral baseline of 50.0, with non-zero
  article counts and source diversity).
- **Live Finnhub responses have not been exercised** in this repository,
  because doing so needs a personal API key. The contract is tested; real
  coverage and latency are not claimed.
- `yahoo_rss` has not been verified live either: every attempt from this
  environment returned HTTP 429.

> **A news outage never invalidates a ranking.** Sentiment is a bounded,
> confidence-adjusted input; with no articles a name scores neutral with low
> confidence and the `NO_NEWS` flag, and `min_news_coverage_ratio` only
> downgrades sentiment confidence rather than blocking the run.

## Sentiment

### Default: VADER (`vaderSentiment`)
- Lexicon-based, general-purpose. Strong on conversational text, weaker on
  financial jargon. We use it as a baseline.

### Alternative: FinBERT (optional install)
- `transformers` + `torch` (heavy). Domain-tuned for financial text.
- Plug-in point: see `sentiment/sentiment_model.py`'s `SentimentModel`
  interface.

## Rate limits & terms of service

### How "429 Too Many Requests" is handled

Every provider call goes through a shared throttle
(`src/trading_common/throttle.py`), one per provider name:

| Provider | Spacing between calls | On a 429 | After repeated 429s |
|---|---|---|---|
| Yahoo prices + fundamentals (`yfinance`, shared) | 0.4 s | pause 30 s, 60 s, 120 s, 240 s, 300 s, then continue | wait 10 min, then continue with the same ticker (none is skipped); after 3 such waits in a row with no success (~1 h), stop waiting and report the rest as rate-limited |
| Yahoo news feed (`yahoo_rss`) | 2 s | pause 5 s | after 2 in a row: paused 10 min, its calls skipped |
| Finnhub | 1.1 s (free plan: 60/min) | pause 15–60 s | after 4 in a row: paused 5 min |

A `Retry-After` header is always honoured when the server sends one. Pauses
are shared, so the health check and every pipeline stage wait out the same
pause instead of each retrying on its own. The quick automatic retries
(transient network faults) no longer retry a 429 at all.

Measured on 2026-10-15: the Yahoo news feed answers 429 to every request from
this network, with no `Retry-After`, and has done so for days -- a block, not
a rate limit, so no amount of waiting gets past it. With the throttle it costs
one 5-second pause and two warning lines per run, instead of ~7 seconds of
retries for each of 150 tickers (16 minutes) and 150 warnings. Yahoo prices and
fundamentals had **zero** rate-limit failures across all 4,757 stocks in the
same run at 0.4 s spacing; the longer pauses are there so a temporary throttle
ends in a completed run rather than in failed tickers.


We are **guests** on every one of these services. The pipeline:

- caches every external response on disk;
- enforces a minimum gap between calls via `utils.rate_limiter.RateLimiter`;
- retries transient failures with exponential backoff (`tenacity`);
- never scrapes HTML, and never defeats a bot check, paywall, or login.

**Where that guest status is uncomfortable, stated plainly.** The Yahoo-backed
providers — `yfinance` prices and fundamentals, and the `yahoo_rss` news feed —
talk to hosts whose `robots.txt` is `User-agent: * / Disallow: /`, using a
browser-like `User-Agent`. That is how essentially every free Python finance
library works, and Yahoo serves these endpoints publicly without
authentication, but it is not a sanctioned integration and it can stop working
without notice (the retired `yfinance` news endpoint is exactly that happening).
For anything beyond personal research, move to a provider whose terms permit
programmatic access: `finnhub` for news today, and the Finnhub/FMP/Alpha Vantage
stubs for fundamentals and prices.

A provider was rejected on these grounds during development: Google News RSS
works and returns real headlines, but its `robots.txt` disallows the feed path
for all agents, so it is not implemented. Reachability is not permission.

If you publish or share results, attribute the data sources and respect their
licenses.
