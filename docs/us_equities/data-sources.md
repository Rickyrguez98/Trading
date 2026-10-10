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

### Default: yfinance news
- Returns recent headlines per ticker with publisher, URL, and publish time.
- Limitations: depth varies per ticker (sometimes < 5 items), and it lacks
  full article text.

### Alternatives (provider stubs, future)
- **NewsAPI.org** (free tier) — broad coverage, headlines + descriptions.
- **MarketAux** (free tier) — finance-focused.
- **Finnhub company-news** — finance-focused, decent free tier.

## Sentiment

### Default: VADER (`vaderSentiment`)
- Lexicon-based, general-purpose. Strong on conversational text, weaker on
  financial jargon. We use it as a baseline.

### Alternative: FinBERT (optional install)
- `transformers` + `torch` (heavy). Domain-tuned for financial text.
- Plug-in point: see `sentiment/sentiment_model.py`'s `SentimentModel`
  interface.

## Rate limits & terms of service

We are **guests** on every one of these services. The pipeline:

- caches every external response on disk;
- enforces a minimum gap between calls via `utils.rate_limiter.RateLimiter`;
- retries transient failures with exponential backoff (`tenacity`);
- never scrapes HTML or bypasses terms of service.

If you publish or share results, attribute the data sources and respect their
licenses.
