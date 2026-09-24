# NSE Data MCP server

An MCP server for NSE equity research. It provides equity-segment securities,
Nifty 50 constituents and latest prices, stock and index history, financial
results, shareholding patterns, corporate actions, announcements and
earnings-call transcripts, bulk/block deals, and short-selling disclosures.
Filing documents and detailed XBRL facts can also be downloaded or read.

Data is retrieved from NSE's public website, not an official API, and is
subject to NSE's terms of use.

## Install

Python 3.12 or newer is required. With `uv`:

```bash
uv sync
```

Or with a virtual environment and pip:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

## Run

The default transport is stdio, which is suitable for local MCP clients:

```bash
uv run nse-data-mcp
```

For Streamable HTTP (served at `http://localhost:8000/mcp` by default):

```bash
uv run nse-data-mcp --transport streamable-http
```

## Inspect and connect

The MCP Inspector requires the `dev` extra (which provides the `mcp` CLI) and
Node.js (`npx`):

```bash
uv sync --extra dev
uv run mcp dev mcp_server.py
```

Example stdio client configuration:

```json
{
  "mcpServers": {
    "nse-data": {
      "command": "uv",
      "args": [
        "--directory",
        "/absolute/path/to/NSE-data-MCP",
        "run",
        "nse-data-mcp"
      ]
    }
  }
}
```

Replace `/absolute/path/to/nse-data-mcp` with this repository's absolute path.

## Tools

All tools are available over both stdio and Streamable HTTP.

### Market data

| Tool | Purpose |
| --- | --- |
| `get_nifty50_stocks` | Get Nifty 50 company names and symbols |
| `get_nifty50_stocks_ltp` | Get the latest Nifty 50 constituent snapshot |
| `get_equity_segment_securities` | Find a stock's NSE symbol from its company name in the full equity-segment securities list |
| `get_stock_historical_price_volume_data` | Get stock price, volume and delivery history |
| `get_nse_index_symbol_names` | Discover valid NSE index symbols and names |
| `get_nse_index_historical_ohlc_volume_data` | Get index OHLC, shares and turnover history |

These tools return JSON-safe `columns`, `records`, and `pagination` fields
(the symbol/name lookups return a plain mapping). Use `offset` and `limit`
(1–5000; default 1,000, or 100 for the Nifty 50 snapshot) when a result is too
large for the model context. Historical dates use `DD-MM-YYYY`. NSE accepts at
most one year per request, so longer stock and index ranges are fetched in
one-year chunks and merged in ascending date order. Missing numeric values in
stock history and the Nifty 50 snapshot are null, distinct from reported zeros.

`get_equity_segment_securities` reads NSE's [equity-segment CSV](https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv)
and returns its original columns and values. It includes all series in that file
(such as `EQ` and `BE`). Match `NAME OF COMPANY` to find the corresponding
`SYMBOL`.

### Corporate filings and deals

| Tool | Data and filters |
| --- | --- |
| `get_financial_results` | Financial filings routed by filing date between legacy and Integrated Filing feeds; requires an NSE symbol and supports consolidated/standalone basis |
| `get_shareholding_patterns` | Promoter, public and employee-trust ownership summaries, reporting dates, revisions and detailed XBRL links; requires an NSE symbol and a date range of at most two calendar years |
| `get_corporate_actions` | Dividend, split, bonus, rights and other action terms, ex-dates, record dates and book closures; requires an NSE symbol |
| `get_corporate_announcements` | Announcements, descriptions, submission/publication timestamps and document links; requires an NSE symbol and a date range of at most one calendar year; optional subject or transcript filter |
| `get_earnings_call_transcripts` | Transcript discovery using announcement descriptions and attachment filenames; requires an NSE symbol and a date range of at most one calendar year |
| `get_bulk_block_deals` | Historical bulk/block disclosures with client, buy/sell side, quantity and price; filter by symbol and deal type |
| `get_short_selling_data` | Reported short-sold share quantities by trade date and security; optionally filter by symbol |

### Documents and XBRL facts

| Tool | Purpose |
| --- | --- |
| `get_corporate_document` | Read PDF/HTML/XML text or download original NSE attachments in base64 chunks |
| `get_filing_facts` | Detailed financial or ownership XBRL facts, including units, accounting periods and dimensions; optional concept search |

### Helpers

| Tool | Purpose |
| --- | --- |
| `validate_start_end_date_str` | Validate an inclusive DD-MM-YYYY date range |
| `split_date_range` | Divide a date range into one-year NSE API chunks |
| `get_useragent` | Get one User-Agent used by the NSE request utilities |

## Querying filings and deals

The corporate filing and deal tools require `start_date_str` and `end_date_str`
in `DD-MM-YYYY` format. `get_financial_results`, `get_shareholding_patterns`,
`get_corporate_actions`, `get_corporate_announcements`, and
`get_earnings_call_transcripts` also require an NSE `symbol`. Shareholding queries
accept at most two calendar years, inclusive of the second anniversary.
Announcement and transcript queries accept at most one calendar year, inclusive
of the first anniversary. For bulk/block deals and short-selling data, `symbol`
is optional; omitting it requests all equities.
Dates filter **filing/submission dates** for financials, ownership and
announcements, **ex-dates** for corporate actions, and **trade dates** for deals
and short-selling disclosures.
Reporting-period end dates are separate fields. Timestamps without an explicit
source timezone are interpreted as India time (`+05:30`).

Requests are split into nonoverlapping 31-day windows. Financial results use the
legacy feed for filing dates before 01-04-2025 and Integrated Filing from that
date; results retain `source_feed`. The financial tool also follows Integrated
Filing's upstream pagination. Exact duplicates are removed;
distinct revisions and buyer/seller disclosures are retained. Results are sorted
by date descending. Client pagination is applied after retrieval and filtering;
requesting the next page fetches the source again, so recently changing data can
shift page boundaries. Narrow symbol/date filters reduce the request volume.

`get_short_selling_data` returns the reported short-sold share quantity from
[NSE's short-selling archive](https://www.nseindia.com/report-detail/display-bulk-and-block-deals)
for each security and trade date. This measures short-selling activity, not an
outstanding short position. It has no client, trade side, or price fields.
Bulk/block deals and short-selling records use NSE's full CSV downloads with
standard pagination; `raw` preserves the source CSV fields.

### Pagination and export

The corporate filing, deal and `get_filing_facts` tools support `offset`,
`limit` (1–5000; default 100), and `output_format="json"` or `"csv"`. JSON
output returns `columns`, `records`, `pagination` and `metadata`. CSV output
replaces `records` with a `csv` string for the requested page and keeps the
pagination metadata; nested fields such as `raw` and `documents` are JSON inside
CSV cells. Follow `next_offset` to export further pages. Missing numeric values
remain null, distinct from reported zeros. Original source records and URLs are
retained for verification.

### Financial results example

```json
{
  "tool": "get_financial_results",
  "arguments": {
    "symbol": "INFY",
    "start_date_str": "01-07-2026",
    "end_date_str": "31-07-2026",
    "basis": "consolidated",
    "period": "all",
    "limit": 100
  }
}
```

`basis` accepts `all`, `consolidated`, or `standalone`. `period` accepts `all`
(default), `Quarterly`, `Half-Yearly`, `Annual`, or `Others`. Period-specific
filters are available only for date ranges entirely before 01-04-2025.
Integrated listing rows do not classify their reporting period, so requests
touching the integrated era must use `period="all"`. Use XBRL contexts to select
the actual accounting duration. For legacy dates, `period="all"` queries all
four period types because NSE's default feed does not include them all.

### Detailed financial and ownership data

Listings expose document URLs; they do not flatten entire financial statements.
Call `get_filing_facts` with a document whose `kind` is `xbrl` to obtain revenue,
profit, EPS, balance-sheet/cash-flow or ownership/pledge facts where reported.
For example, use `concept="Revenue"` or `concept="Pledged"`, or omit `concept`
to retrieve every fact. The filter searches taxonomy concept names, not synonyms.

Each fact retains its exact string value, qualified concept, entity, period,
unit definition, dimensions, decimals/precision and explicit nil flag. This
preserves financial precision and avoids combining different periods, units,
segments or shareholders. Each `unit_xml` includes the namespace declarations
needed to interpret its measures independently. Dimensions retain their original
`dimension` and `value` strings and add `qualified_dimension` and `qualified_value`
in `{namespace-uri}local-name` form. `qualified_value` is null for typed dimensions,
whose values remain as extracted text. Local namespace overrides are respected.
Ratios, price adjustments and cross-company accounting
normalisation are not calculated. Use XML `xbrl` links, not rendered `ixbrl` HTML,
for fact extraction. Legacy HTML results can instead be read with
`get_corporate_document`.

### Earnings-call transcripts and documents

1. Call `get_earnings_call_transcripts` with a symbol and filing-date range (or
   `get_corporate_announcements` with `document_type="earnings_call_transcript"`).
2. Pass a returned attachment URL to `get_corporate_document`.
3. Follow its `pagination.next_offset` to read the remaining text.

Transcript discovery matches descriptions and filenames, including common
spelling variants; it does not inspect every PDF. Generic call invitations and
audio announcements without transcript references are excluded. Metadata can be
ambiguous, so discovery is not guaranteed complete. Some filings contain only a
cover letter linking to an issuer-hosted transcript; extracted document links
are returned but external issuer websites are not downloaded by this tool.

For document text, `offset`/`limit` count characters (default limit 20000;
maximum 100000). PDF text includes page markers and reports pages without text.
Scanned documents require OCR, which is not included.

For original-file downloads, set `output_format="base64"`; offsets and limits
then count **original bytes**. Decode each chunk separately and concatenate the
bytes in order. `size_bytes` and `sha256` allow verification of the assembled
file. Downloads are capped at 25 MiB; PDF text extraction at 500 pages. The server
returns content without writing files. Only HTTPS NSE and archive hosts are
accepted, including after redirects.

## Test

```bash
uv sync --extra dev
uv run pytest
```

The default suite makes no network calls. It uses small captured NSE responses,
generated PDFs and HTTP mocks, and covers every tool through the MCP protocol,
CSV/JSON pagination, both financial feeds, source pagination, revisions,
transcript classification, XBRL context/units, malformed responses, retries and
bounded attachment downloads.

Live tests are opt-in, require network access to NSE, and use known historical
filings and deals. They exercise old and current financial results, detailed
financial/ownership XBRL, corporate actions, an actual transcript PDF, and both
deal types:

```bash
NSE_LIVE_TESTS=1 uv run pytest tests/test_live_nse.py -v
```

For a diagnostic run in environments where only system curl has network access,
set `NSE_LIVE_TRANSPORT=curl` as well. This replaces the transport only in the
live tests; it does not add a curl dependency or fallback to the production server.

NSE endpoints may change or return access challenges. HTTP failures, unexpected
payloads and incomplete upstream pagination raise errors instead of returning a
misleading empty dataset. Live tests fail visibly if upstream access is blocked.

## License

Licensed under the [PolyForm Noncommercial License 1.0.0](LICENSE). You may use,
modify and share this software for noncommercial purposes such as personal
projects, research, education and use by nonprofit or government organizations.
Commercial use is not permitted without a separate license from the author.
