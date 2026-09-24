These are small samples captured from NSE on 2026-09-20 for parser contract tests.
They are historical company disclosures/trade records, not investment conclusions.

- `financial_legacy.json`: `/api/corporates-financial-results`, INFY, July 2024, `period=Quarterly`.
- `financial_integrated.json`: `/api/integrated-filing-results`, INFY, July–August 2026, `page=1&size=100`.
- `financial.xml`: the consolidated June 2026 XBRL linked from that integrated filing, `INTEGRATED_FILING_INDAS_1700136_23072026054446_WEB.xml` on nsearchives.nseindia.com.
- `shareholding.json`: `/api/corporate-share-holdings-master`, INFY, July–August 2026.
- `actions.json`: `/api/corporates-corporateActions`, INFY, January–August 2026.
- `announcements.json`: two records from `/api/corporate-announcements`, INFY, July–August 2026, including the earnings-call transcript.
- `bulk.csv`, `block.csv`: first three rows of `/api/historicalOR/bulk-block-short-deals`
  with `csv=true` and `optionType=bulk_deals` (September 17–18, 2026) or
  `block_deals` (August 2026), captured on 2026-09-25. The full downloads
  contained 437 bulk and 383 block disclosures, respectively, versus 70 rows
  each in their JSON previews.
- `short.csv`: sample rows from the full short-selling download captured on
  2026-09-25 using `optionType=short_selling&from=01-09-2026&to=24-09-2026&csv=true`.
  The source has a UTF-8 BOM, padded headers and comma-separated quantities.
  For that range the JSON preview contained 70 rows and the CSV contained 1,793.

The financial, ownership, action and announcement requests use `index=equities`
and `from_date`/`to_date`; deal and short-selling requests use `from`/`to`.
Full request URLs are
constructed and checked in tests. Test mutations and synthetic edge cases are
separate from these captured payloads.
