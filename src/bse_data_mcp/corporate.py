"""BSE corporate announcements, results and actions, retaining original records.

Unexpected payloads raise errors rather than being mistaken for an empty
filing period.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import re
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from . import data_utils as bse
from .data_utils import Table

PAGES = {
    "corporate_announcements": "/corporates/ann.html",
    "financial_results": "/corporates/ann.html",
    "corporate_actions": "/corporates/corporates_act.html",
    "result_calendar": "/corporates/Forth_Results.html",
    "financial_results_snapshot": "/stock-share-price/",
}
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
COMMON_COLUMNS = ["scrip_code", "symbol", "company_name", "source_url", "raw"]
ATTACHMENT_URL = bse.BASE_URL + "/xml-data/corpfiling/"
# Announcement categories accepted by BSE's ``strCat`` filter.
CATEGORIES = (
    "AGM/EGM", "Board Meeting", "Company Update", "Corp. Action",
    "Insider Trading / SAST", "New Listing", "Result", "Integrated Filing", "Others",
)
ACTION_DATE_BASIS = {"ex": "E", "record": "R", "bc_start": "B"}
MAX_PAGES = 1000


def _value(row: dict, *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if value is not None and str(value).strip().lower() not in ("", "-", "null", "none"):
            return value.strip() if isinstance(value, str) else value
    return None


def _number(value: Any) -> int | float | None:
    number = bse._to_float(value)
    if number is None:
        return None
    if not math.isfinite(number):
        raise ValueError("BSE returned a non-finite number")
    return int(number) if number.is_integer() else number


def _date(value: Any, timestamp: bool = False) -> str | None:
    """ISO date, or an ISO timestamp interpreted as India time when it has no zone."""
    if value is None:
        return None
    parsed = bse._parse_date(value)
    if timestamp:
        return (parsed if parsed.tzinfo else parsed.replace(tzinfo=IST)).isoformat()
    return parsed.date().isoformat()


def _one_year(start: str, end: str, what: str) -> None:
    bse.validate_start_end_date_str(start, end)
    first = dt.datetime.strptime(start, "%d-%m-%Y").date()
    last = dt.datetime.strptime(end, "%d-%m-%Y").date()
    if (last.year, last.month, last.day) > (first.year + 1, first.month, first.day):
        raise ValueError(f"{what} date range must not exceed one calendar year")


def _documents(row: dict) -> list[dict[str, str]]:
    """Build attachment links the way BSE's announcements page does."""
    documents = []
    name = _value(row, "ATTACHMENTNAME")
    if isinstance(name, str) and "/" not in name and "\\" not in name:
        flag = str(_value(row, "PDFFLAG") or "0")
        folder = "AttachLive/"
        if flag == "1":
            folder = "AttachHis/"
        elif flag == "2":
            stamp = _value(row, "NEWS_DT", "DT_TM")
            if stamp is not None:
                when = bse._parse_date(stamp)
                folder = f"CorpAttachment/{when.year}/{when.month}/"
        documents.append({"kind": "attachment", "url": ATTACHMENT_URL + folder + quote(name)})
    media = _value(row, "AUDIO_VIDEO_FILE")
    if isinstance(media, str) and urlsplit(media).scheme in ("http", "https"):
        documents.append({"kind": "audio_video", "url": media})
    return documents


def _is_transcript(row: dict) -> bool:
    text = unquote(" ".join(str(row.get(key) or "") for key in
                           ("NEWSSUB", "HEADLINE", "SUBCATNAME", "MORE")))
    if not re.search(r"transcri?pts?|transcipts?", text, re.IGNORECASE):
        return False
    call = re.search(r"earnings?|concall|conference|call|analyst|investor", text, re.IGNORECASE)
    shareholder_meeting = re.search(r"\b(?:agm|egm)\b|general.meeting", text, re.IGNORECASE)
    return bool(call) or not shareholder_meeting


async def _fetch_announcements(start: str, end: str, code: str,
                               category: str | None) -> list[tuple[dict, str]]:
    """Follow BSE's page numbers until its reported row count is reached."""
    params = {
        "pageno": 1, "strCat": category or "-1", "strPrevDate": bse._to_api_date(start, "%Y%m%d"),
        "strScrip": code, "strSearch": "P", "strToDate": bse._to_api_date(end, "%Y%m%d"),
        "strType": "C", "subcategory": "-1",
    }
    result: list[tuple[dict, str]] = []
    seen = set()
    fetched = 0
    for page in range(1, MAX_PAGES + 1):
        params["pageno"] = page
        payload, url = await bse.get_json("AnnSubCategoryGetData", params)
        if not isinstance(payload, dict) or "Table" not in payload:
            raise RuntimeError("Unexpected BSE announcements response: missing Table")
        page_rows = bse.rows(payload)
        try:
            total = int(bse.rows(payload, "Table1")[0]["ROWCNT"])
        except (RuntimeError, IndexError, KeyError, TypeError, ValueError) as exc:
            if page == 1 and not page_rows:
                return []
            raise RuntimeError("BSE announcements response is missing its row count") from exc
        for row in page_rows:
            if str(_value(row, "SCRIP_CD")) != code:
                continue
            # NEWSID identifies an announcement; RN is only its position in this page set.
            key = _value(row, "NEWSID") or json.dumps(row, sort_keys=True, default=str)
            if key not in seen:
                seen.add(key)
                result.append((row, url))
        fetched += len(page_rows)
        if fetched >= total:
            return result
        if not page_rows:
            break
    raise RuntimeError("BSE pagination ended before all announcements were received")


async def get_corporate_announcements(start_date_str: str, end_date_str: str, scrip_code: str,
                                      subject: str | None = None, category: str | None = None,
                                      document_type: str = "all") -> Table:
    """List up to one year of a company's announcements by dissemination date."""
    code = bse.scrip_code(scrip_code)
    if document_type not in ("all", "earnings_call_transcript"):
        raise ValueError("document_type must be all or earnings_call_transcript")
    if category is not None and category not in CATEGORIES:
        raise ValueError("category must be one of: " + ", ".join(CATEGORIES))
    _one_year(start_date_str, end_date_str, "corporate announcements")
    records = []
    for row, url in await _fetch_announcements(start_date_str, end_date_str, code, category):
        text = " ".join(str(row.get(k) or "") for k in ("NEWSSUB", "HEADLINE", "MORE", "SUBCATNAME"))
        if subject and subject.casefold() not in text.casefold():
            continue
        is_transcript = _is_transcript(row)
        if document_type == "earnings_call_transcript" and not is_transcript:
            continue
        records.append({
            "scrip_code": code, "symbol": None, "company_name": _value(row, "SLONGNAME"),
            "source_url": url, "raw": row,
            "filing_id": _value(row, "NEWSID"), "subject": _value(row, "NEWSSUB"),
            "headline": _value(row, "HEADLINE"), "details": _value(row, "MORE"),
            "category": _value(row, "CATEGORYNAME"), "subcategory": _value(row, "SUBCATNAME"),
            "published_at": _date(_value(row, "DissemDT", "DT_TM", "NEWS_DT"), True),
            "submitted_at": _date(_value(row, "News_submission_dt"), True),
            "document_type": "earnings_call_transcript" if is_transcript else "announcement",
            "documents": _documents(row),
        })
    return _table(records, ["filing_id", "subject", "headline", "details", "category", "subcategory",
                            "published_at", "submitted_at", "document_type", "documents"], "published_at")


async def get_financial_results(start_date_str: str, end_date_str: str, scrip_code: str) -> Table:
    """Result filings (BSE's ``Result`` announcement category) with their attachments."""
    return await get_corporate_announcements(start_date_str, end_date_str, scrip_code, category="Result")


def _table(records: list[dict], fields: list[str], date_key: str) -> Table:
    records.sort(key=lambda r: (r.get(date_key) or "", r.get("scrip_code") or "",
                               str(r.get("filing_id") or "")), reverse=True)
    return Table(fields + COMMON_COLUMNS, records)


def _base(row: dict, url: str) -> dict:
    code = _value(row, "scrip_code", "scrip_Code", "SCRIP_CD")
    if code is None:
        raise RuntimeError("Unexpected BSE record: missing scrip code")
    return {"scrip_code": str(code), "symbol": _value(row, "short_name"),
            "company_name": _value(row, "long_name", "Long_Name"), "source_url": url, "raw": row}


def _dedupe(rows: list[tuple[dict, str]], code: str | None) -> list[tuple[dict, str]]:
    result, seen = [], set()
    for row, url in rows:
        key = json.dumps(row, sort_keys=True, default=str)
        row_code = _value(row, "scrip_code", "scrip_Code", "SCRIP_CD")
        if key in seen or (code and str(row_code) != code):
            continue
        seen.add(key)
        result.append((row, url))
    return result


async def get_corporate_actions(start_date_str: str, end_date_str: str,
                                scrip_code: str | None = None, by_date: str = "ex") -> Table:
    """Corporate actions in a date range; terms are retained verbatim, not inferred."""
    if by_date not in ACTION_DATE_BASIS:
        raise ValueError("by_date must be ex, record, or bc_start")
    code = bse.scrip_code(scrip_code) if scrip_code not in (None, "") else None
    bse.validate_start_end_date_str(start_date_str, end_date_str)
    fetched = []
    for first, last in bse.split_date_range(start_date_str, end_date_str):
        payload, url = await bse.get_json("DefaultData", {
            "Fdate": bse._to_api_date(first, "%Y%m%d"), "Purposecode": "",
            "TDate": bse._to_api_date(last, "%Y%m%d"), "ddlcategorys": ACTION_DATE_BASIS[by_date],
            "ddlindustrys": "", "scripcode": code or "", "segment": "0", "strSearch": "D",
        })
        fetched.extend((row, url) for row in bse.rows(payload))
    records = []
    for row, url in _dedupe(fetched, code):
        record = _base(row, url)
        record.update(purpose=_value(row, "Purpose"),
                      ex_date=_date(_value(row, "exdate", "Ex_date")),
                      record_date=_date(_value(row, "RD_Date")),
                      book_closure_start=_date(_value(row, "BCRD_FROM")),
                      book_closure_end=_date(_value(row, "BCRD_TO")),
                      no_delivery_start=_date(_value(row, "ND_START_DATE")),
                      no_delivery_end=_date(_value(row, "ND_END_DATE")),
                      payment_date=_date(_value(row, "payment_date")))
        records.append(record)
    return _table(records, ["purpose", "ex_date", "record_date", "book_closure_start",
                            "book_closure_end", "no_delivery_start", "no_delivery_end",
                            "payment_date"], "ex_date")


async def get_result_calendar(start_date_str: str, end_date_str: str,
                              scrip_code: str | None = None) -> Table:
    """Board-meeting dates on which companies are scheduled to announce results."""
    code = bse.scrip_code(scrip_code) if scrip_code not in (None, "") else None
    bse.validate_start_end_date_str(start_date_str, end_date_str)
    payload, url = await bse.get_json("Corpforthresults", {
        "fromdate": bse._to_api_date(start_date_str, "%Y%m%d"),
        "scripcode": code or "", "todate": bse._to_api_date(end_date_str, "%Y%m%d"),
    })
    records = []
    for row, row_url in _dedupe([(row, url) for row in bse.rows(payload)], code):
        record = _base(row, row_url)
        record.update(meeting_date=_date(_value(row, "meeting_date")), company_page=_value(row, "URL"))
        records.append(record)
    records.sort(key=lambda r: (r["meeting_date"] or "", r["scrip_code"]))
    return Table(["meeting_date", "company_page"] + COMMON_COLUMNS, records)


def _plain(value: Any) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]*>", " ", str(value or ""))).strip()


async def get_financial_results_snapshot(scrip_code: str) -> Table:
    """Headline figures for the latest reported periods, as BSE's stock page shows them."""
    code = bse.scrip_code(scrip_code)
    payload, url = await bse.get_json("TabResults_PAR", {"scripcode": code, "tabtype": "RESULTS"})
    if not isinstance(payload, dict) or not any(k in payload for k in ("resultinCr", "resultinM")):
        raise RuntimeError("Unexpected BSE results snapshot response")
    periods = []
    for index, key in enumerate(("col2", "col3", "col4"), start=1):
        label = _plain(payload.get(key)) or f"period_{index}"
        periods.append(f"{label} ({index})" if label in periods else label)
    records = []
    for unit, key in (("crore", "resultinCr"), ("million", "resultinM")):
        for row in bse.rows(payload.get(key) or [], key):
            record = {"metric": _plain(row.get("title")), "unit": unit}
            for period, value_key in zip(periods, ("v1", "v2", "v3")):
                value = row.get(value_key)
                try:
                    record[period] = _number(value)
                except ValueError:
                    record[period] = _plain(value) or None
            record.update(scrip_code=code, source_url=url, raw=row)
            records.append(record)
    return Table(["metric", "unit", *periods, "scrip_code", "source_url", "raw"], records)
