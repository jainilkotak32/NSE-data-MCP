"""NSE equity filings and large trades, retaining original records for auditability.

The website's JSON endpoints are not a versioned public API. Unexpected payloads
raise errors rather than being mistaken for an empty trading/filing day.
"""

from __future__ import annotations

import datetime as dt
import csv
import io
import json
import math
import re
from typing import Any
from urllib.parse import urlencode, urljoin, urlsplit, unquote

from . import data_utils as nse
from .data_utils import Table

PAGES = {
    "financial_results": "/companies-listing/corporate-filings-financial-results",
    "shareholding_patterns": "/companies-listing/corporate-filings-shareholding-pattern",
    "corporate_actions": "/companies-listing/corporate-filings-actions",
    "corporate_announcements": "/companies-listing/corporate-filings-announcements",
    "bulk_block_deals": "/report-detail/display-bulk-and-block-deals",
    "short_selling": "/report-detail/display-bulk-and-block-deals",
}
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
# NSE discontinued its legacy financial-results XBRL filing utilities from this date.
FINANCIAL_INTEGRATED_START = dt.date(2025, 4, 1)
COMMON_COLUMNS = ["symbol", "company_name", "source_url", "raw"]
DOCUMENT_KEYS = (
    "xbrl", "ixbrl", "resultDetailedDataLink", "pdf_attach", "attchmntFile",
    "attachment", "vrXbrlFile", "vrxbrlfilename",
)


def _value(row: dict, *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if value is not None and str(value).strip().lower() not in ("", "-", "null", "none"):
            return value
    return None


def _number(value: Any) -> int | float | None:
    if value is None or str(value).strip() in ("", "-", "NA", "N/A"):
        return None
    number = float(str(value).replace(",", "").strip())
    if not math.isfinite(number):
        raise ValueError("NSE returned a non-finite number")
    return int(number) if number.is_integer() else number


def _date(value: Any, timestamp: bool = False) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    formats = ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%Y-%m-%d %H:%M:%S")
    for fmt in formats:
        try:
            parsed = dt.datetime.strptime(text, fmt)
            return parsed.replace(tzinfo=IST).isoformat() if timestamp else parsed.date().isoformat()
        except ValueError:
            pass
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        parsed = nse._parse_date(text)
    if timestamp:
        return (parsed if parsed.tzinfo else parsed.replace(tzinfo=IST)).isoformat()
    return parsed.date().isoformat()


def _documents(row: dict) -> list[dict[str, str]]:
    documents = []
    seen = set()
    for key in DOCUMENT_KEYS:
        value = _value(row, key)
        if not isinstance(value, str):
            continue
        url = urljoin(nse.BASE_URL, value.strip())
        parts = urlsplit(url)
        if parts.scheme not in ("https", "http") or not parts.hostname:
            continue
        if parts.path.rstrip("/").split("/")[-1].lower() in ("-", "null", "none"):
            continue
        if url not in seen:
            documents.append({"kind": key, "url": url})
            seen.add(url)
    return documents


def _rows(payload: Any) -> list[dict]:
    rows = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise RuntimeError("Unexpected NSE response: expected a list of records")
    return rows


async def _json(page: str, endpoint: str, params: dict) -> tuple[Any, str]:
    url = nse.BASE_URL + "/api/" + endpoint + "?" + urlencode(params)
    response = await nse._get_nse_response(nse.BASE_URL + page, url)
    try:
        payload = json.loads(response.text)
    except ValueError as exc:
        raise RuntimeError("NSE returned invalid JSON (possibly an access challenge)") from exc
    return payload, url


def _symbol(symbol: str | None) -> str | None:
    if symbol is None:
        return None
    symbol = symbol.strip().upper()
    if not symbol or len(symbol) > 64 or any(ord(c) < 32 for c in symbol):
        raise ValueError("symbol must be a nonempty NSE symbol")
    return symbol


def _chunks(start: str, end: str):
    nse.validate_start_end_date_str(start, end)
    first = dt.datetime.strptime(start, "%d-%m-%Y").date()
    last = dt.datetime.strptime(end, "%d-%m-%Y").date()
    # Small windows avoid the website's differing maximum date spans.
    while first <= last:
        # Avoid adding past date.max before min() can select ``last``. Once the
        # final chunk is yielded, stop instead of incrementing date.max again.
        stop = last if (last - first).days <= 30 else first + dt.timedelta(days=30)
        yield first.strftime("%d-%m-%Y"), stop.strftime("%d-%m-%Y")
        if stop == last:
            break
        first = stop + dt.timedelta(days=1)


async def _fetch(kind: str, endpoint: str, start: str, end: str,
                 symbol: str | None, *, paged: bool = False,
                 extra: dict | None = None) -> list[tuple[dict, str]]:
    symbol = _symbol(symbol)
    result = []
    seen = set()
    for first, last in _chunks(start, end):
        params = {"from_date": first, "to_date": last, "index": "equities"}
        if symbol:
            params["symbol"] = symbol
        params.update(extra or {})
        page = 1
        fetched = 0
        previous_pages = set()
        while True:
            if paged:
                params.update(page=page, size=100)
            payload, url = await _json(PAGES[kind], endpoint, params)
            rows = _rows(payload)
            page_key = json.dumps(rows, sort_keys=True)
            if paged and rows and page_key in previous_pages:
                raise RuntimeError("NSE repeated a page; refusing to return incomplete results")
            previous_pages.add(page_key)
            for row in rows:
                # NSE may return duplicates across windows. Distinct revisions survive.
                key = json.dumps(row, sort_keys=True)
                row_symbol = _value(row, "symbol", "BD_SYMBOL", "SS_SYMBOL")
                if row_symbol is None:
                    raise RuntimeError("Unexpected NSE record: missing symbol")
                if symbol and str(row_symbol).upper() != symbol:
                    continue
                if key not in seen:
                    result.append((row, url))
                    seen.add(key)
            if not paged:
                break
            if not isinstance(payload, dict) or "totalCount" not in payload:
                raise RuntimeError("NSE paginated response is missing totalCount")
            try:
                total = int(payload["totalCount"])
            except (TypeError, ValueError) as exc:
                raise RuntimeError("Invalid NSE totalCount") from exc
            if total < 0:
                raise RuntimeError("Invalid NSE totalCount")
            fetched += len(rows)
            if fetched >= total:
                break
            if not rows or page >= 1000:
                raise RuntimeError("NSE pagination ended before all records were received")
            page += 1
    return result


def _base(row: dict, url: str) -> dict:
    return {"symbol": _value(row, "symbol", "BD_SYMBOL", "SS_SYMBOL"),
            "company_name": _value(row, "companyName", "sm_name", "cmName", "name", "comp", "BD_SCRIP_NAME", "SS_NAME"),
            "source_url": url, "raw": row}


def _table(records: list[dict], fields: list[str], date_key: str) -> Table:
    records.sort(key=lambda r: (r.get(date_key) or "", r.get("symbol") or "",
                               str(r.get("filing_id") or "")), reverse=True)
    return Table(fields + COMMON_COLUMNS, records)


async def get_financial_results(start_date_str: str, end_date_str: str,
                                symbol: str, basis: str = "all",
                                period: str = "all") -> Table:
    """List legacy and integrated financial filings by filing date (not period end)."""
    symbol = _symbol(symbol)
    if symbol is None:
        raise ValueError("symbol is required for financial results")
    if basis not in ("all", "consolidated", "standalone"):
        raise ValueError("basis must be all, consolidated, or standalone")
    periods = ("Quarterly", "Half-Yearly", "Annual", "Others")
    if period not in ("all", *periods):
        raise ValueError("period must be all, Quarterly, Half-Yearly, Annual, or Others")
    nse.validate_start_end_date_str(start_date_str, end_date_str)
    start_date = dt.datetime.strptime(start_date_str, "%d-%m-%Y").date()
    end_date = dt.datetime.strptime(end_date_str, "%d-%m-%Y").date()
    if end_date >= FINANCIAL_INTEGRATED_START and period != "all":
        raise ValueError("period-specific filtering is unavailable for integrated filings; use period=all")
    records = []
    sources = []
    if start_date < FINANCIAL_INTEGRATED_START:
        legacy_end = min(end_date, FINANCIAL_INTEGRATED_START - dt.timedelta(days=1))
        sources.extend(("corporates-financial-results", False, {"period": p}, start_date, legacy_end)
                       for p in (periods if period == "all" else (period,)))
    if end_date >= FINANCIAL_INTEGRATED_START:
        integrated_start = max(start_date, FINANCIAL_INTEGRATED_START)
        sources.append(("integrated-filing-results", True, {}, integrated_start, end_date))
    seen = set()
    for endpoint, paged, extra, first, last in sources:
        rows = await _fetch("financial_results", endpoint, first.strftime("%d-%m-%Y"),
                            last.strftime("%d-%m-%Y"), symbol, paged=paged, extra=extra)
        for row, url in rows:
            if paged and "financial" not in str(row.get("type", "")).lower():
                continue
            # Integrated filings do not declare Annual/Quarterly in their listing.
            # Do not infer a period from the quarter-end date; facts expose exact durations.
            key = (endpoint, json.dumps(row, sort_keys=True))
            if key in seen:
                continue
            seen.add(key)
            raw_basis = str(_value(row, "consolidated") or "").lower()
            normalized_basis = {"consolidated": "consolidated", "non-consolidated": "standalone",
                                "standalone": "standalone", "non consolidated": "standalone"}.get(raw_basis)
            if basis != "all" and basis != normalized_basis:
                continue
            record = _base(row, url)
            record.update(
                filing_id=_value(row, "seq_Id", "seqNumber"),
                source_feed="integrated" if paged else "legacy",
                period_start=_date(_value(row, "fromDate")),
                period_end=_date(_value(row, "qe_Date", "toDate")),
                period=_value(row, "period"), basis=normalized_basis,
                audited=_value(row, "audited"),
                published_at=_date(_value(row, "broadcast_Date", "broadCastDate", "filingDate"), True),
                revised_at=_date(_value(row, "revised_Date"), True),
                revision=_value(row, "type_Sub", "revision_Remark"),
                documents=_documents(row),
            )
            records.append(record)
    return _table(records, ["filing_id", "source_feed", "period_start", "period_end", "period", "basis",
                            "audited", "published_at", "revised_at", "revision", "documents"], "published_at")


async def get_shareholding_patterns(start_date_str: str, end_date_str: str,
                                    symbol: str) -> Table:
    """List up to two years of a symbol's ownership filings by submission date."""
    symbol = _symbol(symbol)
    if symbol is None:
        raise ValueError("symbol is required for shareholding patterns")
    nse.validate_start_end_date_str(start_date_str, end_date_str)
    start_date = dt.datetime.strptime(start_date_str, "%d-%m-%Y").date()
    end_date = dt.datetime.strptime(end_date_str, "%d-%m-%Y").date()
    if (end_date.year, end_date.month, end_date.day) > (
        start_date.year + 2, start_date.month, start_date.day
    ):
        raise ValueError("shareholding patterns date range must not exceed two calendar years")
    rows = await _fetch("shareholding_patterns", "corporate-share-holdings-master",
                        start_date_str, end_date_str, symbol)
    records = []
    for row, url in rows:
        record = _base(row, url)
        record.update(filing_id=_value(row, "recordId"), as_of_date=_date(_value(row, "date")),
                      promoter_percent=_number(_value(row, "pr_and_prgrp")),
                      public_percent=_number(_value(row, "public_val")),
                      employee_trust_percent=_number(_value(row, "employeeTrusts")),
                      published_at=_date(_value(row, "broadcastDate", "submissionDate"), True),
                      revised_at=_date(_value(row, "revisionDate", "revisedDate"), True),
                      revision=_value(row, "revisedStatus", "revisedData"), documents=_documents(row))
        records.append(record)
    return _table(records, ["filing_id", "as_of_date", "promoter_percent", "public_percent",
                            "employee_trust_percent", "published_at", "revised_at", "revision", "documents"], "published_at")


async def get_corporate_actions(start_date_str: str, end_date_str: str,
                                symbol: str) -> Table:
    """Corporate actions by ex-date; terms are retained verbatim, not inferred."""
    symbol = _symbol(symbol)
    if symbol is None:
        raise ValueError("symbol is required for corporate actions")
    rows = await _fetch("corporate_actions", "corporates-corporateActions",
                        start_date_str, end_date_str, symbol)
    records = []
    for row, url in rows:
        subject = _value(row, "subject")
        record = _base(row, url)
        record.update(series=_value(row, "series"), purpose=subject,
                      face_value=_number(_value(row, "faceVal")), ex_date=_date(_value(row, "exDate")),
                      record_date=_date(_value(row, "recDate")),
                      book_closure_start=_date(_value(row, "bcStartDate")),
                      book_closure_end=_date(_value(row, "bcEndDate")))
        records.append(record)
    return _table(records, ["series", "purpose", "face_value", "ex_date", "record_date",
                            "book_closure_start", "book_closure_end"], "ex_date")


def _is_transcript(row: dict) -> bool:
    text = unquote(" ".join(str(row.get(key) or "") for key in
                           ("desc", "attchmntText", "attchmntFile")))
    # NSE uses both 'Transcript' and the misspelling 'Transcipt', sometimes only
    # in the filename; 'Analyst/Investor Meet' alone also covers invitations/audio.
    if not re.search(r"transcri?pts?|transcipts?", text, re.IGNORECASE):
        return False
    call = re.search(r"earnings?|concall|conference|call|analyst|investor", text, re.IGNORECASE)
    shareholder_meeting = re.search(r"\b(?:agm|egm)\b|general.meeting", text, re.IGNORECASE)
    return bool(call) or not shareholder_meeting


async def get_corporate_announcements(start_date_str: str, end_date_str: str,
                                     symbol: str, subject: str | None = None,
                                     document_type: str = "all") -> Table:
    """List up to one year of a symbol's announcements by filing date."""
    symbol = _symbol(symbol)
    if symbol is None:
        raise ValueError("symbol is required for corporate announcements")
    if document_type not in ("all", "earnings_call_transcript"):
        raise ValueError("document_type must be all or earnings_call_transcript")
    nse.validate_start_end_date_str(start_date_str, end_date_str)
    start_date = dt.datetime.strptime(start_date_str, "%d-%m-%Y").date()
    end_date = dt.datetime.strptime(end_date_str, "%d-%m-%Y").date()
    if (end_date.year, end_date.month, end_date.day) > (
        start_date.year + 1, start_date.month, start_date.day
    ):
        raise ValueError("corporate announcements date range must not exceed one calendar year")
    rows = await _fetch("corporate_announcements", "corporate-announcements",
                        start_date_str, end_date_str, symbol)
    records = []
    for row, url in rows:
        text = " ".join(str(row.get(k) or "") for k in ("desc", "attchmntText"))
        if subject and subject.casefold() not in text.casefold():
            continue
        is_transcript = _is_transcript(row)
        if document_type == "earnings_call_transcript" and not is_transcript:
            continue
        record = _base(row, url)
        record.update(filing_id=_value(row, "seq_id"), subject=_value(row, "desc"),
                      description=_value(row, "attchmntText"),
                      published_at=_date(_value(row, "exchdisstime", "an_dt", "sort_date"), True),
                      submitted_at=_date(_value(row, "an_dt"), True),
                      document_type="earnings_call_transcript" if is_transcript else "announcement",
                      documents=_documents(row))
        records.append(record)
    return _table(records, ["filing_id", "subject", "description", "published_at", "submitted_at",
                            "document_type", "documents"], "published_at")


async def get_bulk_block_deals(start_date_str: str, end_date_str: str,
                              symbol: str | None = None, deal_type: str = "all") -> Table:
    """Full CSV bulk/block disclosures; each buyer/seller remains a separate row."""
    if deal_type not in ("all", "bulk", "block"):
        raise ValueError("deal_type must be all, bulk, or block")
    records = []
    for kind in ("bulk", "block") if deal_type == "all" else (deal_type,):
        rows = await _fetch_deal_csv(kind + "_deals", start_date_str, end_date_str, symbol)
        for row, url in rows:
            side = _value(row, "Buy / Sell")
            quantity = nse._to_int(row["Quantity Traded"].replace(",", ""))
            if quantity is not None and quantity < 0:
                raise ValueError("NSE returned a negative deal quantity")
            records.append({
                "symbol": row["Symbol"].strip().upper(),
                "company_name": _value(row, "Security Name"), "source_url": url, "raw": row,
                "deal_type": kind, "trade_date": _date(row["Date"]),
                "client_name": _value(row, "Client Name"),
                "side": str(side).strip().upper() if side else None,
                "quantity": quantity, "price": _number(_value(row, "Trade Price / Wght. Avg. Price")),
                "remarks": _value(row, "Remarks"),
            })
    return _table(records, ["deal_type", "trade_date", "client_name", "side", "quantity", "price", "remarks"], "trade_date")


def _deal_csv_rows(text: str, option_type: str) -> list[dict[str, str]]:
    """Read NSE's full CSV download, rejecting challenges and malformed rows."""
    reader = csv.reader(io.StringIO(text.lstrip("\ufeff")), strict=True)
    try:
        columns = [column.strip() for column in next(reader, [])]
        required = {"Date", "Symbol", "Security Name"}
        if option_type == "short_selling":
            required.add("Quantity")
        else:
            required.update({"Client Name", "Buy / Sell", "Quantity Traded",
                             "Trade Price / Wght. Avg. Price", "Remarks"})
        if (not required <= set(columns) or len(columns) != len(set(columns))
                or any(not column for column in columns)):
            raise RuntimeError(f"Unexpected NSE {option_type} CSV columns (possibly an access challenge)")
        rows = []
        for values in reader:
            if not values:
                continue
            if len(values) != len(columns):
                raise RuntimeError(f"Unexpected NSE {option_type} CSV row width")
            row = dict(zip(columns, values))
            if _value(row, "Date") is None or _value(row, "Symbol") is None:
                raise RuntimeError(f"Unexpected NSE {option_type} CSV row: missing date or symbol")
            rows.append(row)
    except csv.Error as exc:
        raise RuntimeError(f"NSE returned malformed {option_type} CSV") from exc
    return rows


async def _fetch_deal_csv(option_type: str, start: str, end: str,
                          symbol: str | None) -> list[tuple[dict[str, str], str]]:
    """Fetch full deal downloads, then filter and deduplicate before client paging."""
    symbol = _symbol(symbol)
    rows = []
    seen = set()
    for first, last in _chunks(start, end):
        params = {"optionType": option_type, "from": first, "to": last, "csv": "true"}
        if symbol:
            params["symbol"] = symbol
        url = nse.BASE_URL + "/api/historicalOR/bulk-block-short-deals?" + urlencode(params)
        response = await nse._get_nse_response(nse.BASE_URL + PAGES["bulk_block_deals"], url)
        for row in _deal_csv_rows(response.text, option_type):
            row_symbol = row["Symbol"].strip().upper()
            if symbol and row_symbol != symbol:
                continue
            key = json.dumps(row, sort_keys=True)
            if key in seen:
                continue
            rows.append((row, url))
            seen.add(key)
    return rows


async def get_short_selling_data(start_date_str: str, end_date_str: str,
                                 symbol: str | None = None) -> Table:
    """Fetch all short-selling disclosures via the website's CSV download."""
    rows = await _fetch_deal_csv("short_selling", start_date_str, end_date_str, symbol)
    records = []
    for row, url in rows:
        quantity = nse._to_int(row["Quantity"].replace(",", ""))
        if quantity is not None and quantity < 0:
            raise ValueError("NSE returned a negative short-sold quantity")
        records.append({
            "symbol": row["Symbol"].strip().upper(), "company_name": _value(row, "Security Name"),
            "source_url": url, "raw": row,
            "trade_date": _date(row["Date"]), "short_sold_quantity": quantity,
        })
    return _table(records, ["trade_date", "short_sold_quantity"], "trade_date")
