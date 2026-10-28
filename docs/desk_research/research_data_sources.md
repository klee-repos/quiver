# Research: primary-document data sources for a fact-driven Quiver

Date of research: 2026-10-27. Method: live calls from the laptop (curl / the repo venv
`yfinance 1.5.1` / the Robinhood MCP read tools), plus official SEC pages. Every number below
comes from a call made today unless it is marked **UNVERIFIED**. Scratch artifacts (downloaded
filings, parsers, the valuation demo) are in
`/private/tmp/claude-501/-Users-kvnlee-dev-quiver/b1816101-e244-433e-93d8-4e8a1539970a/scratchpad/sec/`
and `.../scratchpad/valuation_demo.py`.

---

## 0. Bottom line

| Data type | Primary source | Fallback | Why |
|---|---|---|---|
| "Did anything new get filed?" | `data.sec.gov/submissions/CIK##########.json` (diff `accessionNumber`, read `form` + `items`) | EDGAR Atom company feed (`browse-edgar ... output=atom`) | No key, no auth expiry, ~20 KB gzip per name, carries 8-K item codes. |
| Reported numbers (revenue, margins, cash flow, shares, cash) | `data.sec.gov/api/xbrl/companyfacts/CIK##########.json` | RH MCP `get_sec_filing_facts` → yfinance statements | Tagged by the filer; the LLM never transcribes a number. |
| Filing text (Item 1, 1A, 7; 10-Q MD&A; S-1 "Market Opportunity") | EDGAR Archives primary document (`primaryDocument` from submissions) + Python section splitter | RH MCP `get_sec_filing` (coverage gaps, see §9) | RH text access failed for 3/3 ANET filings today. |
| Earnings release + guidance | 8-K Item 2.02 → Exhibit 99.x (EDGAR Archives) | company IR site | Guidance is text only (no XBRL). Arrives 0–8 days before the 10-Q. |
| Earnings dates | RH `get_earnings_results` (date, am/pm, `verified`) | yfinance `Ticker.calendar` | Both agreed today (ANET 2026-11-03, pm). |
| Price / market cap | RH `get_equity_quotes` (already the one price source) × XBRL `dei:EntityCommonStockSharesOutstanding` | RH `get_equity_fundamentals.market_cap`, yfinance `fast_info` | Shares agree across all three sources to 7 digits. |
| Consensus estimates | yfinance `revenue_estimate` / `growth_estimates` / `eps_revisions` | Alpha Vantage `EARNINGS_ESTIMATES` (key) | Only needed for "guide vs street". |
| Call transcripts | Alpha Vantage `EARNINGS_CALL_TRANSCRIPT` (free key, 25 req/day) | FMP Ultimate ($139/mo) / API Ninjas ($39–59/mo) / EarningsCall ($60+/mo) | SEC does not host transcripts (Form 8-K Item 2.02(b) exempts the call). |
| ETF holdings | EDGAR NPORT-P (series level, quarterly public, ~55–60 day lag) | yfinance `funds_data.top_holdings` (top 10, current); issuer CSV (Global X verified) | N-PORT is primary and free; yfinance is fresher but only top 10. |
| ETF expense ratio | yfinance `funds_data.fund_operations` | Alpha Vantage `ETF_PROFILE`; closed-end fund: XBRL `cef:TotalAnnualExpensesPercent` | Open-end ETF trusts have EMPTY companyfacts. |

**Change-detection mechanism (recommended):** poll each book name's submissions JSON once per
tick, store `(cik, accessionNumber)` in a new ledger table, classify each new accession by
`form` + 8-K `items` into tiers (§3), and only re-decide a ticker when a Tier-A item appears or a
derived fact moves (guidance delta, growth delta, share-count delta, risk-factor sentence diff).
For a 10-Q/10-K, gate the numeric re-valuation on companyfacts containing facts with
`accn == <new accession>`. This loop needs no key and no human re-auth.

---

## 1. SEC EDGAR direct APIs (verified)

### 1.1 Endpoints (all returned HTTP 200 today with a declared User-Agent)

| Purpose | URL | Notes from the live call |
|---|---|---|
| Ticker → CIK (operating cos.) | `https://www.sec.gov/files/company_tickers.json` | dict of 10,430 rows `{cik_str, ticker, title}`; `Last-Modified: Mon, 26 Oct 2026 21:11:03 GMT` (conditional GET works here). |
| Ticker → CIK + exchange | `https://www.sec.gov/files/company_tickers_exchange.json` | `fields: [cik, name, ticker, exchange]`. |
| Ticker → fund CIK/series/class | `https://www.sec.gov/files/company_tickers_mf.json` | 28,616 rows `[cik, seriesId, classId, symbol]`. SMH→`S000034411`, SOXX→`S000004354`, SGOV→`S000068768`, URA→`S000029442`, QQQ→`S000101292`. |
| Filing history | `https://data.sec.gov/submissions/CIK0001596532.json` | `filings.recent` = columnar arrays: `accessionNumber, filingDate, reportDate, acceptanceDateTime, act, form, fileNumber, filmNumber, items, core_type, size, isXBRL, isInlineXBRL, isXBRLNumeric, primaryDocument, primaryDocDescription`. ANET: 1,000 recent rows + 1 overflow file (`CIK0001596532-submissions-001.json`). 157 KB raw / ~20 KB gzip. |
| All XBRL facts | `https://data.sec.gov/api/xbrl/companyfacts/CIK0001596532.json` | 3.0 MB raw / ~200 KB gzip; taxonomies `dei`, `us-gaap` (449 concepts), `ecd`, `srt`. Each fact: `start, end, val, accn, fy, fp, form, filed, frame`. |
| One concept | `https://data.sec.gov/api/xbrl/companyconcept/CIK0001596532/us-gaap/GrossProfit.json` | Same fact shape for one tag. |
| Cross-section | `https://data.sec.gov/api/xbrl/frames/us-gaap/RevenueFromContractWithCustomerExcludingAssessedTax/USD/CY2026Q2.json` | 2,306 companies, one fact each (`pts: 2306`). Useful for peer growth/margin percentiles. |
| Filing folder | `https://www.sec.gov/Archives/edgar/data/1596532/000159653226000174/index.json` | Lists every file (`ex991q226-earningsrelease.htm`, `FilingSummary.xml`, `R1.htm`, `*_htm.xml`, …). |
| Filing index page | `https://www.sec.gov/Archives/edgar/data/<cik>/<acc-no-dashes>/<acc>-index.htm` | The only place that labels each document's **type** (`8-K`, `EX-99.1`, `EX-99`, `GRAPHIC`, `EX-101.*`). |
| Company Atom feed | `https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=0001181412&type=8-K&dateb=&owner=include&count=10&output=atom` | `application/atom+xml`, ~2 KB. Also accepts a **series ID** as `CIK=` (used for ETFs, §5). |
| Latest filings (all filers) | `https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&company=&dateb=&owner=include&start=0&count=40&output=atom` | Newest entry was 6 min old at query time. |
| XBRL RSS | `https://www.sec.gov/Archives/edgar/usgaap.rss.xml`; monthly `https://www.sec.gov/Archives/edgar/monthly/xbrlrss-2026-10.xml` (2.7 MB) | SEC: "updated every ten minutes Monday through Friday, 6am – 10pm EST". |
| Full-text search (JSON) | `https://efts.sec.gov/LATEST/search-index?q="..."&ciks=0001181412&forms=10-K&dateRange=custom&startdt=2026-10-01&enddt=2026-10-27` | Works (Elasticsearch-shaped: `hits.total`, `_source.form`, `file_date`, `file_type`). This JSON backend is **not documented as a public API**; the documented UI is `https://www.sec.gov/edgar/search/`. Coverage per SEC FAQ: "all EDGAR filings submitted electronically since 2001". |
| Bulk | `https://www.sec.gov/Archives/edgar/daily-index/xbrl/companyfacts.zip`, `.../bulkdata/submissions.zip` | SEC: "recompiled and republished nightly at approximately 3:00 a.m. ET". Not needed for 13 names. |

### 1.2 Fair-access policy (verified)

- SEC page text: "Current max request rate: 10 requests/second." and "Please declare your user
  agent in request headers." Sample format: `Sample Company Name AdminContact@<domain>.com`.
- **Enforced today:** curl's default UA → **403**; empty UA → **403**; bare `Mozilla/5.0` → **403**;
  `QuiverResearch admin@example.org` → **200**. Use a real operator contact on the box (set it in
  `.env`, do not hard-code it).
- No key, no OAuth. `data.sec.gov` does not support CORS (irrelevant for a server).
- Block duration after a rate breach: **UNVERIFIED** (the SEC pages say only that access is
  "carefully monitored").
- Filing window: "Monday through Friday, from 6:00 a.m. to 10:00 p.m., ET". Filings that start
  after 5:30 p.m. ET get the next business day's `filingDate`. Observed: ANET 10-K
  `acceptanceDateTime 2026-02-14T00:12:46Z` (Feb 13, 7:12 pm ET) has `filingDate 2026-02-17`.
  **Use `acceptanceDateTime` (UTC, verified against index timestamps) as the event time, not
  `filingDate`.**

### 1.3 Latency (filing → API)

- SEC states: submissions "typical processing delay of less than a second"; XBRL APIs "typical
  processing delay of under a minute"; longer at peak times.
- **Observed today (n=2, 07:30 ET, a busy earnings morning):** Boeing 8-K accepted 11:30:23Z →
  in submissions JSON by 11:31:49Z (≤ 86 s). Weatherford 8-K accepted 11:30:54Z → Archives
  `index.json` live by 11:32:15Z, submissions JSON by 11:32:23Z (≈ 89 s). The getcurrent Atom
  showed Weatherford within 32 s. **Plan for ~1–2 minutes**, which is irrelevant for an hourly tick.
- `data.sec.gov` sends **no `ETag` / `Last-Modified`**, so conditional GET does not work; diff
  accession numbers instead. `www.sec.gov/files/*.json` does send `Last-Modified`.

### 1.4 Can we detect "new filing for ticker X since last check" cheaply? Yes.

Per tick: 1 GET per CIK (~20 KB gzip, ~0.5 s). 8 operating/trust CIKs + 5 ETF series feeds = 13
requests per tick, far below 10 req/s. Throttle to ≤ 5 req/s in code to stay polite.

---

## 2. Entity resolution: book-specific traps (verified)

| Ticker | What SEC says | Consequence |
|---|---|---|
| ANET, CEG, GEV, VRT, DLR | Operating companies (`entityType: operating`). DLR `tickers` also lists preferreds `DLR-PK/PJ/PL`. | Normal 10-K/10-Q/8-K path. |
| **SPCX** | `company_tickers.json` → **SpaceX, CIK 1181412** (IPO: S-1 2026-05-20, 424B4 2026-06-12, 10-Qs since). `company_tickers_mf.json` **also** maps SPCX → CIK 1719812 series `S000070261` (Collaborative Investment Series Trust, the old SPAC ETF series, still filing NPORT-P). | **Ticker collision.** Rule: prefer `company_tickers.json`; confirm with the submissions `tickers` + `exchanges` fields. strategy.yaml means SpaceX. |
| **BOT** | CIK 2081119 "RoboStrategy, Inc.", `entityType: other`; forms N-2, 424B3, NPORT-P, N-CSRS. A **closed-end fund** of private robotics preferred stock. | Not an ETF and not an operating company. Facts = NAV, premium/discount, expenses (§5.4). |
| BSOL | CIK 2045872 "Bitwise Solana Staking ETF", files 10-K/10-Q (a grantor trust), has `us-gaap` facts (`InvestmentOwnedAtFairValue`, `AssetsNet`). | No company fundamentals; NAV + fee + staking only. |
| SMH, SOXX, URA, QQQ, SGOV | Series inside fund trusts (VanEck ETF Trust 1137360, iShares Trust 1100663, Global X Funds 1432353, Invesco QQQ Trust 1067839). | Use the **series ID**, not the trust CIK (iShares Trust filed 1,524 NPORT-P in 12 months). |

---

## 3. 8-K item codes (verified from the SEC Form 8-K PDF) and the `items` field

The submissions JSON `items` column holds a comma string, e.g. `'2.02,9.01'` (ANET 2026-08-04),
`'5.02,5.03,8.01,9.01'` (ANET 2025-09-08), `'2.01,3.02,9.01'` (SpaceX 2026-08-14). Non-8-K rows
have `''`. The RH MCP index does **not** return item codes (§9).

Full item list from `https://www.sec.gov/files/form8-k.pdf`: 1.01 Entry into a Material Definitive
Agreement; 1.02 Termination of a Material Definitive Agreement; 1.03 Bankruptcy or Receivership;
1.04 Mine Safety; 1.05 Material Cybersecurity Incidents; 2.01 Completion of Acquisition or
Disposition of Assets; 2.02 Results of Operations and Financial Condition; 2.03 Creation of a
Direct Financial Obligation …; 2.04 Triggering Events That Accelerate … an Obligation; 2.05 Costs
Associated with Exit or Disposal Activities; 2.06 Material Impairments; 3.01 Notice of Delisting
…; 3.02 Unregistered Sales of Equity Securities; 3.03 Material Modification to Rights of Security
Holders; 4.01 Changes in Registrant's Certifying Accountant; 4.02 Non-Reliance on Previously
Issued Financial Statements …; 5.01 Changes in Control of Registrant; 5.02 Departure of Directors
or Certain Officers …; 5.03 Amendments to Articles … Change in Fiscal Year; 5.04 Temporary
Suspension of Trading Under … Benefit Plans; 5.05 Amendments to the Code of Ethics …; 5.06 Change
in Shell Company Status; 5.07 Submission of Matters to a Vote of Security Holders; 5.08
Shareholder Director Nominations; 6.01–6.06 (ABS only); 7.01 Regulation FD Disclosure; 8.01 Other
Events; 9.01 Financial Statements and Exhibits.

Form rules (same PDF): a report is due "within four business days" unless stated otherwise;
information under Items 2.02 and 7.01 "will be deemed furnished, and not filed". Item 2.02(b): no
8-K is required for an oral/webcast call that occurs "within 48 hours after" a furnished release
and is broadly accessible — **this is why EDGAR has no call transcripts.**

**Proposed tiers (a design choice, not an SEC rule):**

- **Tier A — re-decide the ticker:** 10-K, 10-K/A, 10-Q, 8-K 2.02, S-1/424B4 (IPO), 8-K 1.01,
  1.02, 1.03, 1.05, 2.01, 2.03, 2.04, 2.05, 2.06, 3.01, 4.01, 4.02, 5.01; 8-K 7.01/8.01 **with an
  EX-99 exhibit** (mid-quarter guidance updates and pre-announcements arrive here — send the
  exhibit to a cheap LLM triage that answers "does this change guidance/capital/demand: yes/no").
- **Tier B — record + triage only:** 5.02, 5.03, 3.02, 3.03, 5.07, SC 13D/13G, DEF 14A.
- **Tier C — ignore for decisions:** Form 4, 144, S-8, 424B2/3, FWP, CORRESP, 8-A12B, D. (ANET:
  651 of its 1,000 recent rows are Form 4; 241 are Form 144.)

Exhibit naming varies: GEV files its release as `EX-99`, others `EX-99.1`. Match the type prefix
`EX-99`, read the type from the `-index.htm` table (index.json has no type column).

---

## 4. Earnings materials

### 4.1 Release and presentation (EDGAR, free) — verified

| Name | Latest 2.02 8-K exhibits (type : size) |
|---|---|
| ANET | EX-99.1 release, 243 KB html → 19.5 K chars (~4.9 K tokens) |
| CEG | EX-99.1 release 465 KB; **EX-99.2 "Earnings Conference Call Second Quarter 2026"** presentation, ~9.2 K tokens |
| GEV | EX-99 (single exhibit) 645 KB |
| VRT | EX-99.1 693 KB |
| DLR | EX-99.1 = 4.4 MB **Financial Supplement** (~38.6 K tokens); EX-99.2 = earnings presentation (~10 K tokens) |
| SPCX | EX-99.1 849 KB html → ~10.7 K tokens |

- Guidance is **text**, e.g. ANET EX-99.1 "Financial Outlook — For the third quarter of 2026, we
  expect: Revenue of approximately $3.3 billion; Non-GAAP operating margin of 48 - 49%; and
  Non-GAAP diluted net income per share between $1.06 and $1.08." Compare to consensus: yfinance
  `revenue_estimate` 0q avg $3.334 B (23 analysts) — guide midpoint is ~1% below street.
- The 8-K's own XBRL is **cover-page `dei` only** (ANET `anet-20260804_htm.xml`: 22 `dei:*`
  tags, zero `us-gaap`). Release numbers are not tagged. Tagged numbers arrive with the 10-Q/10-K.

**Gap from release (8-K 2.02) to the tagged 10-Q/10-K (acceptance to acceptance, last 4 quarters):**

| ANET | CEG | GEV | VRT | DLR | SPCX |
|---|---|---|---|---|---|
| 0.1 d (10-K 1.1 d) | 0.2 d | 0.0 d (10-K 1.4 d) | 0.4 d (10-K 2.4 d) | **8.0 d** every quarter | 2.0 d / 0.0 d |

Design consequence: on release day, extract guidance + headline numbers from the exhibit as
**provisional** (LLM extraction + regex cross-check), and re-run the numeric valuation when
companyfacts shows the new `accn`. RH `get_financials` matched XBRL for ANET Q2-26 exactly
(revenue 3,035,700,000; GP 1,910,300,000; NI 1,212,900,000) — its post-release latency is
**UNVERIFIED**.

### 4.2 Earnings-call transcripts — no free primary source

| Source | Access | Price / limit (from the vendor page today) | Verified? |
|---|---|---|---|
| SEC EDGAR | — | Not hosted (Item 2.02(b)) | Yes |
| **Alpha Vantage** `function=EARNINGS_CALL_TRANSCRIPT&symbol=&quarter=YYYYQn&apikey=` | free key | Docs do not mark it Premium; free tier "25 API requests per day"; "over 15 years of history", turn-by-turn `sentiment` | Demo key call for IBM 2024Q1 returned 37 turns, 51 K chars (~13 K tokens). **Free-key access for non-demo tickers and lag after the call: UNVERIFIED.** |
| Financial Modeling Prep | key | Transcripts only in **Ultimate $139/mo ($99/mo annual)**; free plan 250 calls/day without transcripts | Pricing page, yes |
| API Ninjas `/v1/earningstranscript` | key | "Premium only"; Developer $59/mo ($39 annual), 5 years; commercial use needs paid tier | Docs page, yes |
| EarningsCall (earningscall.biz) | key | Starter "$60 /Month", Premium $69, Ultimate $129 | Pricing page, yes |
| Finnhub | key | **UNVERIFIED** (JS-rendered pages; not retrieved) | No |
| Company IR sites / news sites (Motley Fool etc.) | web | Scraping terms not checked | **UNVERIFIED**, not recommended |

Recommendation: make transcripts optional. The release + presentation (both on EDGAR) carry the
guidance and KPIs; the transcript adds tone and Q&A. If wanted, Alpha Vantage's free tier covers
5 single stocks × 4 quarters = 20 calls/year.

### 4.3 What yfinance gives (verified, yfinance 1.5.1 in `.venv`)

- `Ticker.calendar`: next earnings date + EPS/revenue high/low/avg (ANET: 2026-11-03; revenue avg $3.334 B).
- `revenue_estimate`, `earnings_estimate`, `growth_estimates` (0q/+1q/0y/+1y/LTG), `eps_trend`,
  `eps_revisions`, `analyst_price_targets`, `upgrades_downgrades`, `recommendations`.
- `info`: `marketCap`, `enterpriseValue`, `sharesOutstanding`, `totalRevenue`, `forwardPE`,
  `pegRatio`, `enterpriseToRevenue`, `totalCash`, `totalDebt`, `freeCashflow`.
  **Caution:** `info.freeCashflow` = $3.89 B for ANET vs OCF − capex from XBRL = $5.16 B TTM;
  the vendor definition is **UNVERIFIED** (likely "levered FCF"). Compute FCF from XBRL.
- `get_earnings_dates()` **fails in the repo venv**: `ImportError: Import lxml failed` (lxml is
  not installed; adding it needs `requirements.lock.txt`, per the deploy-dependency memory).
- `sec_filings`: list with Yahoo CDN mirrors of EDGAR docs (labels can be wrong: it titled ANET's
  earnings 8-K "Corporate Changes & Voting Matters").
- `funds_data`: `top_holdings` (top 10), `fund_operations` (expense ratio, turnover, net assets),
  `sector_weightings` (§5).
- yfinance is unofficial (Yahoo endpoints); it can break without notice.

---

## 5. ETFs

### 5.1 N-PORT (EDGAR, free) — verified

- Rule (SEC 2019 interim final rule, effective 2019-03-06): funds file all three monthly reports
  for a fiscal quarter "not later than 60 days after the end of that fiscal quarter"; only the
  **third-month report is public**. The 2024 monthly-public amendments were pushed to
  **2027-11-17** (≥ $1 B funds) / 2028-05-18 (smaller); a Feb-2026 SEC proposal would revise them
  again (final status **UNVERIFIED**).
- Observed lag (period → public): SOXX 2026-06-30 → 2026-08-25 (56 d); SMH 06-30 → 08-26 (57 d);
  SGOV latest 2026-10-22; URA 2026-09-29; QQQ 06-30 → 08-28. Holdings are **2–5 months old** when read.
- Find a series' filings: `browse-edgar?action=getcompany&CIK=S000004354&type=NPORT-P&...&output=atom`.
  **Trap:** for SMH (`S000034411`) the `type=NPORT-P` filter returned only 2019–2021 entries;
  with `type=` empty the 2026-08-26 NPORT-P appears. Fetch unfiltered and filter client-side.
- Raw XML: `https://www.sec.gov/Archives/edgar/data/<trust-cik>/<acc>/primary_doc.xml`
  (`xslFormNPORT-P_X01/primary_doc.xml` is the rendered view). SOXX file = 53 KB.
  Fields: `seriesId`, `seriesName`, `repPdDate`, `netAssets`; per holding `<invstOrSec>` with
  `name`, `lei`, `cusip`, `isin`, `balance`, `valUSD`, `pctVal`, `assetCat`, `fairValLevel`,
  `isRestrictedSec`. **No ticker** — map via LEI/ISIN/CUSIP or name → `company_tickers.json`.

### 5.2 Look-through feasibility per book ETF

| ETF | N-PORT holdings | Top-10 weight | Look-through verdict |
|---|---|---|---|
| SMH | 26 (06-30) | 70.2% (NVDA 17.5, TSM 9.3, AMAT 5.7) | **Feasible**: top 10 = 70%; most file 10-K/10-Q. TSM files 20-F + 6-K (verified: 637 6-Ks in recent); its companyfacts is `ifrs-full` in **TWD**, annual (20-F) periods only — quarterly TSM numbers need the 6-K text or a vendor. |
| SOXX | 33 (06-30) | 61.6% | **Feasible** (US-heavy). |
| QQQ | ~100 (AV) | 46.7% (yfinance) | Feasible for the top 10–15; the tail is diffuse. Treat as a "market" sleeve. |
| URA | Global X daily CSV: Cameco (`CCO CN`) 23.4%, Sprott Physical Uranium 8.1%, NexGen, Kazatomprom GDR, Oklo, UEC… | 54% (yfinance top 9) | **Partial**: many holdings are Canada/Kazakhstan listings or a physical trust. Verified: Cameco (CCJ, CIK 1009001) files 40-F + 6-K; NexGen (NXE), UEC, Oklo are in `company_tickers.json`; Kazatomprom is **not** in the SEC ticker map. |
| SGOV | T-bills | — | Not meaningful; the fact is the yield (RH `thirty_day_sec_yield`) and expense 0.09%. |
| BSOL | Solana + staking | — | Not meaningful; NAV, fee 0.20%, staking yield. |
| BOT | 14 private, Level-3, restricted holdings (Figure AI 25.4%, Dyna Robotics 25.4%, Apptronik 13.3+12.1%, …), net assets $146.9 M (05-31) | — | **No public look-through.** Facts: NAV, premium/discount (XBRL `cef:HighestPriceOrBidPremiumDiscountToNavPercent` 2.68%), total expense **3.98%**, management fee 2.5%. |
| SPCX | Not an ETF (SpaceX) | — | Use the operating-company path. |

### 5.3 Other holdings/expense sources

- yfinance `funds_data` (verified): SMH expense 0.35%, SOXX 0.33%, URA 0.69%, QQQ 0.18%,
  SGOV 0.09%, BSOL 0.20%; top-10 for SMH/SOXX/QQQ/URA; empty holdings for SGOV/BSOL;
  `No Fund data found` for BOT.
- Alpha Vantage `ETF_PROFILE` (demo, QQQ): `net_expense_ratio 0.0018`, 103 holdings with weights,
  `last_updated 2026-10-23`. Needs a key for other tickers (free tier 25/day).
- Issuer files: Global X `https://assets.globalxetfs.com/funds/holdings/ura_full-holdings_20261026.csv`
  → 200, dated "as of 10/26/2026" (daily). iShares `.../1467271812596.ajax?fileType=csv...` returned an
  HTML page, not CSV (**not working as tried**). VanEck/Invesco not tested.
- Open-end ETF trusts have **empty** companyfacts (`facts: {}` for VanEck and Global X trusts), so
  there is no XBRL expense ratio via that API for SMH/SOXX/URA/QQQ/SGOV. Fund XBRL in N-CSR
  tailored shareholder reports: **UNVERIFIED** (iShares N-CSR submissions are 10–243 MB bundles).

---

## 6. XBRL pitfalls found in real data (verified on ANET)

1. **Tag drift inside one company:** capex is `PaymentsToAcquirePropertyPlantAndEquipment` in
   10-Qs but `PaymentsToAcquireProductiveAssets` in the FY2025 10-K. Keep a synonym list per metric.
2. **Cash-flow facts are year-to-date** (`start 2026-01-01, end 2026-06-30`). Derive quarters by
   differencing YTD; derive Q4 = FY − 9M YTD.
3. **10-K has no discrete Q4** for income-statement items either (FY only) — same derivation.
4. **Tags stop being used:** `RevenueRemainingPerformanceObligation` last tagged FY2022; `Revenues`
   absent (ANET uses `RevenueFromContractWithCustomerExcludingAssessedTax`). Test presence; do not assume.
5. **Dimensions:** product/service and geography splits come as separate facts (RH `get_sec_filing_facts`
   returns them with `axises`; companyfacts returns only non-dimensional facts). Segment growth needs
   the dimensional facts (RH tool or the filing's instance XML).
6. **Restatements:** the same `(start,end)` appears in several filings; keep the latest `filed`.
7. Share count: `dei:EntityCommonStockSharesOutstanding` (cover page, latest 1,261,224,648 as of
   2026-07-30) matched RH `shares_outstanding` 1,261,224,573.6 and yfinance 1,261,224,648.

---

## 7. Valuation methods for a deterministic Python layer

All inputs below are reachable without an LLM. "XBRL" = companyfacts; "RH" = MCP quote/fundamentals.

| Method | Formula | Inputs → source |
|---|---|---|
| Revenue growth (YoY, TTM, q/q) | Σ last 4 q / Σ prior 4 q − 1 | Revenue (XBRL, derived quarters) |
| Margins | GP/Rev, OpInc/Rev, NI/Rev, FCF/Rev | XBRL `GrossProfit`, `OperatingIncomeLoss`, `NetIncomeLoss`, OCF, capex |
| FCF | OCF − capex (TTM) | XBRL `NetCashProvidedByUsedInOperatingActivities` − capex synonyms |
| Market cap | price × shares | RH quote × XBRL `dei` shares |
| EV | mcap − cash − marketable securities + debt (+ leases, policy) | XBRL `CashAndCashEquivalentsAtCarryingValue`, `AvailableForSaleSecuritiesDebtSecurities*`, debt tags (`LongTermDebt*`, `DebtCurrent`) |
| EV/Sales vs growth | EV/TTM revenue; compare to growth (and to peers via the frames API) | above + frames |
| FCF yield | TTM FCF / mcap | above |
| Rule of 40 | revenue growth % + FCF margin % | above |
| PEG | forward P/E ÷ expected EPS growth (%) | RH/yfinance P/E; growth from yfinance `growth_estimates` (consensus, not a filing fact) or from guidance |
| Dilution | diluted weighted shares YoY (same quarter); SBC/revenue | XBRL `WeightedAverageNumberOfDilutedSharesOutstanding`, `ShareBasedCompensation` |
| Guidance vs consensus | guide midpoint / consensus − 1 | EX-99.x text (LLM-extracted, regex-verified) vs yfinance `revenue_estimate` |
| Guidance revision | this quarter's guide vs last quarter's guide for the same period | ledger history of extracted guidance |
| **Reverse DCF** | solve g such that Σ FCF₀(1+g)ᵗ/(1+r)ᵗ + TV = EV, TV = FCF_N(1+g_t)/(r−g_t) | EV, TTM FCF; r, N, g_t are **policy constants** in config |
| Implied vs stated growth | implied g (above) vs guidance-implied growth and consensus `+1y` growth | the two rows above |

**Worked example (ANET, computed today by `valuation_demo.py` from companyfacts + RH market cap
$223.49 B):** TTM revenue $10.54 B (matches yfinance `totalRevenue` exactly); TTM growth 32.6%;
TTM FCF $5.16 B, FCF margin 48.9%; cash + securities $13.34 B; EV $210.1 B (yfinance $210.2 B);
EV/Sales 19.9×; FCF yield 2.31%; Rule of 40 = 81; diluted shares +0.38% YoY. Reverse DCF (10 y,
terminal 3%): implied FCF CAGR 11.6% / 14.3% / 16.7% at r = 8% / 9% / 10%. Consensus revenue growth:
+40.8% this year, +29.0% next year; Q3 guide ≈ +43% YoY. Reading: the price implies ~14% for 10
years against ~30–40% near-term growth — the model's job is to judge durability, and Python can
state the gap as a number.

Methods that need the LLM (fact extraction only, never arithmetic): TAM (text), guidance (text),
segment narrative, risk-factor changes. **TAM caveat:** ANET's 10-K contains **no** "addressable"
phrase; TAM figures usually sit in S-1s ("Business—Our Market Opportunity" in SpaceX's 424B4: "We
believe we have identified the largest actionable total addressable market (“TAM”) in human
history") and in investor-day decks, not 10-Ks.

---

## 8. Size and LLM-cost control (measured)

Text = HTML stripped by a simple parser; tokens ≈ chars/4 (numbers-heavy text tokenizes worse;
treat as ±30%).

| Document | Text chars | ≈ tokens |
|---|---|---|
| ANET 10-K FY2025 (whole) | 415,751 | **~104 K** |
| — Item 1 Business | 39,726 | ~9.9 K |
| — Item 1A Risk Factors | 190,395 | **~47.6 K** |
| — Item 7 MD&A | 43,360 | ~10.8 K |
| — Item 8 Financial statements | 90,340 | ~22.6 K |
| ANET 10-Q Q2-2026 (whole) | 277,334 | ~69 K |
| — Part I Item 2 MD&A | 34,024 | ~8.5 K |
| — Part II Item 1A (full risk factors repeated) | 193,612 | ~48.4 K |
| ANET EX-99.1 release | 19,553 | ~4.9 K |
| SpaceX EX-99.1 release | 42,870 | ~10.7 K |
| DLR EX-99.1 financial supplement | 154,325 | ~38.6 K |
| SpaceX 424B4 prospectus (whole) | 1,513,685 | **~378 K** |
| IBM call transcript (AV sample) | 51,420 | ~12.9 K |

**Cost controls that work:**

1. **Numbers from XBRL only.** The LLM never reads Item 8 or the release tables for numbers (saves ~23 K tokens per 10-K and removes transcription risk).
2. **Section-targeted reads:** split by `Item N.` headings (the body heading is the last match; the first is the TOC). 10-K: Item 1 + Item 7 ≈ 21 K tokens; 10-Q: MD&A ≈ 8.5 K; release ≈ 5–11 K.
3. **Diff, do not re-read.** Sentence-level diff of Item 1A (page numbers stripped): ANET 10-Q Q1→Q2 2026 = **32 changed sentences ≈ 1.8 K tokens of 47.8 K** (real changes: purchase commitments, memory/silicon price pass-through, tariffs); 10-K→Q1 10-Q = 93 sentences ≈ 5.3 K tokens. Paragraph-hash diff is noisy (76 "changed" paragraphs) because page breaks split paragraphs.
4. **S-1/424B4:** read only "Prospectus Summary", "Our Market Opportunity", MD&A; never the 378 K whole.
5. **Event-gated:** read only when §3 Tier A fires — about 4 earnings cycles × 5 single names per year plus a few 8-Ks.
6. Price reference (OpenRouter `/api/v1/models` listing today, may vary by route): `z-ai/glm-5.2` prompt $0.034/M, completion $7.00/M; `z-ai/glm-4.7-flash` prompt $0.061/M, completion $0.40/M. At these listed rates input is cheap; output/reasoning tokens and long-context accuracy are the real limits — another reason to keep the context to the changed sections.

---

## 9. Robinhood MCP SEC/fundamental tools — verified behavior today

| Tool | Result today | Notes |
|---|---|---|
| `get_sec_filing_index(ANET, [8-K,10-Q], since 2026-07-01)` | OK | Returns opaque UUID `filing_id`, `form_type`, `description`, `date_filed`. **No accession number** (cannot join to EDGAR), **no 8-K item codes**. `date_filed` = acceptance date (10-Q shows 2026-08-04; EDGAR `filingDate` 2026-08-05). |
| `get_sec_filing(<ANET 8-K / 10-Q / 10-K>)` | **404 "Filing content is not available."** for all 3 | Text access failed for every ANET filing tried. |
| `get_sec_filing(<AAPL 10-K 2025-10-31>)` | OK (TOC) | TOC is heading-derived, has mojibake (`Note 1 â Summary…`), and lists financial statements + notes **but no Item 1, 1A or 7**. |
| `get_sec_filing_facts(ANET 10-Q, 3 concepts)` | OK | Returns dimensional facts (`ProductOrServiceAxis`, `StatementGeographicalAxis`) — richer than companyfacts for segments. Max 3 filings × 10 concepts per call. |
| `get_financials([ANET, SMH])` | ANET OK (matches XBRL); **SMH `null`** | Revenue, GP, NI, net margin only — no cash flow, no shares. |
| `get_earnings_results(ANET)` | OK | 8 quarters, EPS est/actual, date, `am/pm`, `verified`. |
| `get_equity_fundamentals([ANET, SMH])` | OK | market cap, shares, float, P/E, P/B, 52-week range; for SMH also P/E 34.6, `thirty_day_sec_yield`. No expense ratio. |

**Reliability for a monitoring loop:**

- The OAuth token expires every few days (CLAUDE.md: the 2026-10-12 login stored a 7-day expiry;
  earlier ones ~3.8 days), with **no headless refresh** — a human runs `deploy/gcp/rh-reauth.sh`.
  A fact-monitor built on it goes blind for the whole expiry gap.
- Architecture: the MCP lives in the Claude orchestrator. The analysis path (`analyze.py`, EVE
  brain) cannot call it, and the decision wall forbids analysis modules from naming broker symbols.
  A direct-EDGAR fetcher in Python sits on the analysis side of the wall with no new grant needed
  (it takes no limits and touches no broker).
- Direct EDGAR: no key, no OAuth, only a UA + ≤ 10 req/s. It fails only on an SEC outage or a
  rate block. RH stays the price source for orders (already required), so a price outage and a
  trading outage are the same event anyway.

---

## 10. Recommended design (data plane only)

1. **Resolver (daily, cached):** `company_tickers.json` + `company_tickers_mf.json` (conditional GET
   on `Last-Modified`). Precedence: operating map > fund map; confirm via submissions `tickers`.
   Store `(ticker, cik, series_id, kind ∈ {operating, etf_series, cef, grantor_trust})`.
2. **Filing watcher (each tick):** for operating/trust CIKs GET submissions JSON; for ETF series
   GET the unfiltered series Atom feed. New `accessionNumber` → row in a new ledger table
   `filings(cik, accession, form, items, acceptance_utc, primary_doc, tier, processed_at)`.
   Idempotent by accession (same pattern as the `ref_id` reservation).
3. **Fact builder (Python, deterministic):** on Tier-A 10-Q/10-K wait until companyfacts has
   `accn == accession`, then compute §7 metrics into a `facts_snapshot(ticker, accession, metric,
   value, period_end)` table. On 8-K 2.02, fetch EX-99* and have the LLM fill a strict JSON schema
   (guidance metric, low, high, period, basis GAAP/non-GAAP, quote span); Python verifies each
   number appears verbatim in the exhibit text before it is stored.
4. **Materiality gate (Python):** re-decide a ticker only if (a) a Tier-A filing arrived, and
   (b) at least one delta crosses a configured band: guidance midpoint vs prior guide or vs
   consensus, TTM growth change, FCF-margin change, dilution, reverse-DCF gap, or a non-empty
   risk-factor sentence diff. Otherwise carry the prior decision (the "decide only on new data" rule).
5. **ETF path:** new NPORT-P → recompute the look-through weighted growth/valuation from the
   constituents' own fact snapshots (top-N covering ≥ 60% weight); daily: price, premium/discount,
   expense ratio (yfinance), yield.
6. **Earnings calendar:** RH `get_earnings_results` (or yfinance `calendar`) marks expected
   release days; nothing else depends on it, because the watcher sees the 8-K anyway.

---

## 11. UNVERIFIED / open items

- Alpha Vantage transcript endpoint with a real free key (non-IBM ticker), and its lag after a call.
- Finnhub transcript plan gating; transcript-site terms of use (Motley Fool, IR sites).
- SEC rate-limit block duration and response code.
- iShares/VanEck/Invesco holdings CSV URLs (the iShares URL tried returned HTML).
- XBRL expense ratio in N-CSR tailored shareholder reports for open-end ETFs.
- Final status of the Feb-2026 SEC N-PORT proposal.
- Why RH `get_sec_filing` returns 404 for ANET filings (transient vs coverage); its post-release
  latency for `get_financials`.
- OpenRouter per-token prices are a live listing and may differ from the route actually billed.
- The 1–2 minute EDGAR latency figure rests on n = 2 filings at one moment.

---

## 12. Sources (fetched today)

- SEC EDGAR APIs: https://www.sec.gov/search-filings/edgar-application-programming-interfaces
- SEC accessing EDGAR data / fair access: https://www.sec.gov/os/accessing-edgar-data
- SEC webmaster FAQ: https://www.sec.gov/about/webmaster-frequently-asked-questions
- SEC full-text search FAQ: https://www.sec.gov/edgar/search/efts-faq.html
- SEC RSS feeds: https://www.sec.gov/about/rss-feeds ; https://www.sec.gov/structureddata/rss-feeds-submitted-filings
- SEC Form 8-K instructions (PDF): https://www.sec.gov/files/form8-k.pdf
- SEC N-PORT 2019 timing rule: https://www.sec.gov/rules-regulations/2019/02/amendments-timing-requirements-filing-reports-form-n-port (via search result; text summarized from search)
- N-PORT 2025 compliance-date extension: https://www.lexblog.com/2025/04/23/sec-extends-compliance-date-for-amendments-to-form-n-port/ ; Feb-2026 proposal: https://www.stblaw.com/about-us/publications/view/2026/05/11/sec-proposes-amendments-to-form-n-port-and-extends-compliance-dates-for-names-rule-reporting-(registered-funds-regulatory-update)
- Live API calls: `https://www.sec.gov/files/company_tickers.json`, `.../company_tickers_exchange.json`,
  `.../company_tickers_mf.json`, `https://data.sec.gov/submissions/CIK{0001596532,0001181412,0002081119,0002045872,0001868275,0001996810,0001674101,0001297996,0001137360,0001100663,0001067839,0001432353,0001719812}.json`,
  `https://data.sec.gov/api/xbrl/companyfacts/CIK0001596532.json` (+ SpaceX, BSOL, RoboStrategy, VanEck, Global X),
  companyconcept + frames URLs in §1.1, EDGAR Archives docs for ANET 10-K/10-Qs/EX-99.1, SpaceX 8-K/424B4,
  CEG/DLR EX-99.x, SOXX/SMH/RoboStrategy NPORT-P XML, `efts.sec.gov/LATEST/search-index`.
- Alpha Vantage docs: https://www.alphavantage.co/documentation/ ; limits: https://www.alphavantage.co/premium/ , https://www.alphavantage.co/support/
- FMP pricing: https://site.financialmodelingprep.com/pricing-plans
- API Ninjas: https://api-ninjas.com/api/earningscalltranscript , https://api-ninjas.com/pricing
- EarningsCall: https://earningscall.biz/api-pricing
- Global X holdings CSV: https://assets.globalxetfs.com/funds/holdings/ura_full-holdings_20261026.csv
- OpenRouter model listing: https://openrouter.ai/api/v1/models
- Reverse-DCF concept (secondary): https://www.expectationsinvesting.com/
