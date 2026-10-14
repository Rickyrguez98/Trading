# Step 3 — Connecting a broker: options A, B and C

**Status:** all three options are implemented and selectable; **B is the
default**. Investing itself stays manual. Researched 2026-10-13. Nothing here
places orders: this repository never executes trades, never logs in with a
password, and never stores credentials.

The question: can the monitor (`trading-monitor`, step 4) read your portfolio
straight from your broker instead of from a file?

## Choosing an option

```bash
trading-monitor --list-brokers        # shows A, B, C, the selected one, and whether each is ready
```

Set the default in `configs/us_equities/monitoring.yaml`:

```yaml
broker:
  option: B          # A = statement PDF, B = holdings CSV, C = IBKR Flex
```

or override one run with `trading-monitor --broker A|B|C`. Passing
`--holdings file.pdf` / `file.csv` implies A / B.

| Option | Set up once | Each time |
|---|---|---|
| **A** statement PDF | `pip install -e ".[pdf]"` | Download the month's estado de cuenta, then `trading-monitor --broker A --holdings estado.pdf` (or set `broker.statement_pdf`). Audit `data/us_equities/processed/monitoring/extracted_*.csv`. |
| **B** holdings CSV | `trading-monitor --init-holdings` creates `data/us_equities/holdings/my_holdings.csv` from the template | Add one row per purchase (`ticker,shares,avg_cost,currency,market,purchase_date`); run `trading-monitor`. |
| **C** IBKR Flex | In Client Portal → **Performance & Reports → Flex Queries**: the gear next to *Flex Web Service Configuration* enables the service and generates a token (choose 1-year validity); the **+** next to *Activity Flex Query* creates a query — add *Open Positions*, level of detail *Lot*, date format `yyyyMMdd` — and its "i" icon shows the query ID. Put `IBKR_FLEX_TOKEN` and `IBKR_FLEX_QUERY_ID` in `.env`. | `trading-monitor --broker C` |

How option C protects the token: it is read from the environment only, never
written anywhere, never logged; IBKR requires it in the URL, so request URLs
are never logged and error messages are scrubbed of it; it is only ever sent
to `https://*.interactivebrokers.com`. The raw statement (which contains your
account ID) is never saved — only the extracted positions. Shorts, options and
non-USD/MXN positions are skipped and listed, never guessed. Option C has been
tested against synthetic IBKR responses built on the Flex XML schema, not yet
against a live account.

## Findings

### GBM (GBM+) — no API for your account

- **No public retail API.** GBM publishes no developer documentation for
  client accounts; the GBM+ app is consumer-only.
- **"Open Banking" does not cover your positions.** GBM's Open Banking page
  implements Article 76 of the Ley Fintech. Only the *open data* tier of that
  article is in force (rules from June 2020: product information, branches).
  The tiers for **aggregated and transactional (customer) data are still
  unpublished in 2026** — years past the legal deadline, with an amparo filed
  against CNBV, Banxico and SHCP over the delay. Until those rules exist, no
  Mexican broker is obliged to share your holdings by API.
- **Statements are PDFs.** In the app: *Estados de cuenta y constancias* →
  strategy → year → month. They appear after the first five business days of
  the month.
- **Trading USA positions are in dollars**, and DriveWealth (GBM's US partner)
  issues its own statements for at least part of that activity, so you may
  have two statements: GBM's (MXN; SIC and BMV) and DriveWealth's (USD).
- **The unofficial `gbmplus` library is not an option.** It logs in with your
  email and password (plus a client ID), has not been updated since its only
  releases on 2021-12-20, and would break this repository's rules: no broker
  login automation, no credential storage. It may also breach GBM's terms.

### Other brokers

| Broker | Official API to read your portfolio? | For a Mexican resident |
|---|---|---|
| **Interactive Brokers** | **Yes — Flex Web Service**: read-only token + query ID, returns positions with cost basis. Two calls (`SendRequest`, `GetStatement`), paced at 1 request/second, 10/minute. | Accounts open to Mexican residents (per broker reviews — confirm on IBKR's country list). Not a CNBV-supervised casa de bolsa; US tax forms (W-8BEN) apply. |
| Charles Schwab | Trader API (OAuth), requires a Schwab retail brokerage account. | Eligibility from Mexico not confirmed. |
| Alpaca | Trading API. | Individual non-US accounts historically unsupported; grows through partner apps (e.g. trii in Chile). Not confirmed for Mexico. |
| Webull México (ex-Flink) | None found for Mexican accounts. | — |
| Hapi, Kuspit, Bursanet | None found. | — |

## Options

| | Option | Effort | What you get | Trade-offs |
|---|---|---|---|---|
| **A** | **GBM + monthly statement PDF** (built) | None | Run `trading-monitor --holdings estado.pdf` each month | Monthly cadence. The parser is tested only on *synthetic* GBM-style statements; one real (redacted) statement is needed to calibrate it. Purchase dates attach only when that month's buys explain the whole position. |
| **B** | **GBM + holdings CSV** (built) | ~2 minutes per trade | Exact lots, prices and purchase dates; run any day | Manual upkeep. The most accurate input today. |
| **C** | **Move some or all investing to IBKR + Flex Web Service** | Open an account; ~1 day to build the read-only connector | Automatic daily positions with cost basis | A broker change: different fees, FX costs, taxes, and no CNBV supervision. |
| **D** | **Ask GBM** (`plus@gbm.com`) whether any client data API exists | One email | Certainty | Given the Article 76 situation, the answer is most likely no. |
| ✗ | Unofficial GBM login (`gbmplus`, scraping) | — | — | Not offered: password automation, unmaintained, likely against GBM's terms. |

Option D from the original research (asking GBM by email whether a client API exists) needs no code; it remains worth one email.

**Recommendation:** **A + B now** — keep a CSV as the source of truth and use
the PDF to cross-check monthly — and **D** in parallel, since it costs one
email. Choose **C** only if automatic updates are worth changing broker; the
connector would be a read-only adapter producing the same lots the CSV does.

## Sources

- GBM Open Banking: <https://gbm.com/open-banking/>
- GBM trading products (Trading USA in USD, DriveWealth statements): <https://gbm.com/trading/>
- GBM help on statements and returns: <https://gbm.com/faqs/donde-puedo-consultar-mis-rendimientos-perdidas-y-ganancias/>
- Article 76 secondary rules still pending (2026): <https://www.elfinanciero.com.mx/opinion/colaborador-invitado/2026/01/07/el-emprendimiento-demanda-open-finance-lleva-a-la-cnbv-ante-el-juez/>, <https://armor-aml.com/que-viene-para-la-ley-fintech-en-el-2026-cuando-open-finance-y-el-sandbox-siguen-pendientes/>
- `gbmplus` package metadata: <https://pypi.org/project/gbmplus/>
- IBKR Flex Web Service: <https://www.interactivebrokers.com/campus/ibkr-api-page/flex-web-service/>
- Generating the Flex token and query ID (third-party walkthrough): <https://help.tradezella.com/en/articles/6063403-interactive-broker-how-to-sync-your-interactive-broker-ibkr-account-with-tradezella>
- Flex XML `OpenPosition` attribute names (open-source parser `ibflex`): <https://github.com/csingley/ibflex>
- IBKR for Mexican residents (third-party review): <https://brokerchooser.com/es/broker-reviews/interactive-brokers-review/interactive-brokers-mexico>
- Alpaca and non-US individuals: <https://forum.alpaca.markets/t/live-trading-as-a-non-us-citizen/115>
- Webull's entry into Mexico via Flink: <https://www.fintechfutures.com/m-a/us-stock-trading-app-webull-enters-mexico-with-flink-acquisition>
