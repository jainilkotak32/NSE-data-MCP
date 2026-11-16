"""Utilities to collect market data for BSE stocks and indices.

BSE's website reads from ``api.bseindia.com``, which is not a versioned public
API. Unexpected payloads raise errors rather than being mistaken for an empty
trading day.
"""

import asyncio
import csv
import datetime
import io
import json
from typing import Any
from urllib.parse import urlencode

import httpx2

from nse_data_mcp.data_utils import (
    Table,
    get_useragent,
    split_date_range,
    validate_start_end_date_str,
)

__all__ = [
    "Table",
    "get_useragent",
    "split_date_range",
    "validate_start_end_date_str",
    "get_equity_securities",
    "get_stock_historical_price_volume_data",
    "get_bse_index_names",
    "get_bse_index_historical_data",
    "get_sensex_stocks",
    "get_sensex_stocks_ltp",
]

BASE_URL = "https://www.bseindia.com"
API_URL = "https://api.bseindia.com/BseIndiaAPI/api"
# The index filter BSE's market-watch page sends for the Sensex constituents.
SENSEX_INDEX_NAME = "S&P BSE SENSEX"
BSE_REQUEST_ATTEMPTS = 3
BSE_REQUEST_TIMEOUT = httpx2.Timeout(15.0, connect=5.0)

# Date formats seen across BSE's JSON and CSV endpoints.
_DATE_FORMATS = (
    "%d-%B-%Y", "%d-%b-%Y", "%d %b %Y", "%d %B %Y", "%d/%m/%Y", "%d-%m-%Y",
    "%Y-%m-%d", "%Y%m%d", "%d-%b-%y", "%a %b %d %Y %H:%M:%S",
)
_MISSING_VALUES = ("", "-", "--", "NA", "N/A", "null", "None")


def _parse_date(value: Any) -> datetime.datetime:
    """Parse a BSE date using the formats its endpoints are known to emit."""
    text = str(value).strip()
    try:
        return datetime.datetime.fromisoformat(text)
    except ValueError:
        pass
    for date_format in _DATE_FORMATS:
        try:
            return datetime.datetime.strptime(text, date_format)
        except ValueError:
            continue
    raise ValueError(f"Unrecognized BSE date value: {text!r}")


def _isoformat(value: datetime.datetime) -> str:
    return f"{value.strftime('%Y-%m-%dT%H:%M:%S')}.{value.microsecond // 1000:03d}"


def _to_float(value: Any) -> float | None:
    """Coerce a BSE cell to float, keeping missing-value placeholders null."""
    if value is None:
        return None
    text = str(value).replace(",", "").strip()
    if text in _MISSING_VALUES:
        return None
    return float(text)


def _to_int(value: Any) -> int | None:
    number = _to_float(value)
    if number is None:
        return None
    if not number.is_integer():
        raise ValueError(f"Expected a whole number from BSE, got {value!r}")
    return int(number)


def scrip_code(value: Any) -> str:
    """Normalize a BSE scrip code such as 500325; symbols are not accepted."""
    text = str(value).strip()
    if not text.isascii() or not text.isdigit() or not 1 <= len(text) <= 9:
        raise ValueError(
            "scrip_code must be a numeric BSE scrip code (e.g. 500325); "
            "call get_equity_securities to look one up"
        )
    return text


def _to_api_date(date_str: str, date_format: str) -> str:
    return datetime.datetime.strptime(date_str, "%d-%m-%Y").strftime(date_format)


def _create_http_client() -> httpx2.AsyncClient:
    return httpx2.AsyncClient(follow_redirects=True, timeout=BSE_REQUEST_TIMEOUT)


def api_url(endpoint: str, params: dict[str, Any]) -> str:
    return f"{API_URL}/{endpoint}/w?{urlencode(params)}"


async def _get_bse_response(url: str) -> httpx2.Response:
    """Fetch a BSE API URL with the site headers it expects and bounded retries."""
    last_error: httpx2.HTTPError | None = None
    async with _create_http_client() as client:
        for attempt in range(BSE_REQUEST_ATTEMPTS):
            try:
                # The API rejects requests that do not appear to come from the site.
                headers = {
                    "Accept": "application/json, text/plain, */*",
                    "Accept-Language": "en-US,en;q=0.9",
                    "Origin": BASE_URL,
                    "Referer": BASE_URL + "/",
                    "User-Agent": get_useragent(),
                }
                response = await client.get(url, headers=headers)
                response.raise_for_status()
                return response
            except httpx2.HTTPError as error:
                last_error = error
                if attempt + 1 < BSE_REQUEST_ATTEMPTS:
                    await asyncio.sleep(0.25 * (2**attempt))
    raise RuntimeError(
        f"BSE request failed after {BSE_REQUEST_ATTEMPTS} attempts"
    ) from last_error


async def get_json(endpoint: str, params: dict[str, Any]) -> tuple[Any, str]:
    """Return a decoded BSE API payload together with the URL it came from."""
    url = api_url(endpoint, params)
    response = await _get_bse_response(url)
    try:
        return json.loads(response.text), url
    except ValueError as exc:
        raise RuntimeError(
            "BSE returned invalid JSON (possibly an access challenge or error page)"
        ) from exc


def rows(payload: Any, key: str = "Table") -> list[dict]:
    """Return the record list from a bare-list or ``{"Table": [...]}`` payload."""
    data = payload.get(key) if isinstance(payload, dict) else payload
    if not isinstance(data, list) or any(not isinstance(row, dict) for row in data):
        raise RuntimeError("Unexpected BSE response: expected a list of records")
    return data


def _columns(records: list[dict]) -> list[str]:
    columns: list[str] = []
    for record in records:
        for key in record:
            if key not in columns:
                columns.append(key)
    return columns


def _read_csv(text: str, required: set[str], what: str) -> tuple[list[str], list[dict[str, str]]]:
    """Read a BSE CSV download, rejecting error pages and malformed rows."""
    text = text.lstrip("﻿")
    if not text.strip():
        raise RuntimeError(f"BSE returned no {what} data; check the identifier and date range")
    try:
        parsed = [row for row in csv.reader(io.StringIO(text)) if any(c.strip() for c in row)]
    except csv.Error as exc:
        raise RuntimeError(f"BSE returned malformed {what} CSV") from exc
    header = [column.strip() for column in parsed[0]]
    # BSE ends each line with a trailing comma, which yields an unnamed column.
    while header and not header[-1]:
        header.pop()
    if not required <= set(header) or len(header) != len(set(header)):
        raise RuntimeError(f"Unexpected BSE {what} CSV columns (possibly an error page)")
    records = []
    for values in parsed[1:]:
        if len(values) < len(header) or any(v.strip() for v in values[len(header):]):
            raise RuntimeError(f"Unexpected BSE {what} CSV row width")
        records.append({column: value.strip() for column, value in zip(header, values)})
    return header, records


async def get_equity_securities(group: str = "", status: str = "Active") -> Table:
    """List BSE equity-segment securities with scrip codes, symbols and ISINs."""
    payload, _ = await get_json("ListofScripData", {
        "Group": group.strip().upper(), "Scripcode": "", "industry": "",
        "segment": "Equity", "status": status,
    })
    records = rows(payload)
    if records and "SCRIP_CD" not in records[0]:
        raise RuntimeError("Unexpected BSE securities list: missing SCRIP_CD")
    return Table(columns=_columns(records), records=records)


_HISTORY_INT_COLUMNS = ("No.of Shares", "No. of Trades", "Deliverable Quantity")


async def get_stock_historical_price_volume_data(
    stock_scrip_code: str, start_date_str: str, end_date_str: str
) -> Table:
    """Daily price, volume and delivery history for a BSE scrip code.

    Dates use DD-MM-YYYY. Longer ranges are fetched in one-year chunks and
    merged in ascending date order.
    """
    validate_start_end_date_str(start_date_str, end_date_str)
    code = scrip_code(stock_scrip_code)
    columns: list[str] = []
    records: list[dict[str, Any]] = []
    for chunk_start, chunk_end in split_date_range(start_date_str, end_date_str):
        url = api_url("StockPriceCSVDownload", {
            "pageType": 0, "rbType": "D", "Scode": code,
            "FDates": _to_api_date(chunk_start, "%d/%m/%Y"),
            "TDates": _to_api_date(chunk_end, "%d/%m/%Y"), "Seriesid": "",
        })
        response = await _get_bse_response(url)
        header, chunk = _read_csv(
            response.text, {"Date", "Open Price", "High Price", "Low Price", "Close Price"},
            "stock history")
        columns = columns or header
        for raw in chunk:
            record: dict[str, Any] = {"Date": _isoformat(_parse_date(raw["Date"]))}
            for column, value in raw.items():
                if column != "Date":
                    record[column] = (_to_int if column in _HISTORY_INT_COLUMNS else _to_float)(value)
            records.append(record)
    records.sort(key=lambda record: record["Date"])
    return Table(columns=columns, records=records)


async def get_bse_index_names() -> Table:
    """Index names accepted by the historical index endpoint, as BSE lists them."""
    payload, _ = await get_json("FillddlIndex", {"fmdt": "", "todt": ""})
    records = rows(payload)
    return Table(columns=_columns(records), records=records)


async def get_bse_index_historical_data(
    index_name: str, start_date_str: str, end_date_str: str, period: str = "D"
) -> Table:
    """Historical values for a BSE index; period is D (daily), M or Y."""
    validate_start_end_date_str(start_date_str, end_date_str)
    index_name = index_name.strip()
    if not index_name or len(index_name) > 100:
        raise ValueError("index_name must be a BSE index name; call get_bse_index_names")
    if period not in ("D", "M", "Y"):
        raise ValueError("period must be D, M, or Y")
    columns: list[str] = []
    date_column = ""
    records: list[dict[str, Any]] = []
    for chunk_start, chunk_end in split_date_range(start_date_str, end_date_str):
        url = api_url("ProduceCSVForDate", {
            "strIndex": index_name,
            "dtFromDate": _to_api_date(chunk_start, "%d/%m/%Y"),
            "dtToDate": _to_api_date(chunk_end, "%d/%m/%Y"), "period": period,
        })
        response = await _get_bse_response(url)
        header, chunk = _read_csv(response.text, set(), "index history")
        date_column = next((c for c in header if c.lower() in ("date", "month", "year")), "")
        if not date_column or len(header) < 2:
            raise RuntimeError("Unexpected BSE index history CSV columns (possibly an error page)")
        columns = columns or header
        for raw in chunk:
            record: dict[str, Any] = {}
            for column, value in raw.items():
                if column != date_column:
                    record[column] = _to_float(value)
                elif period == "D":
                    record[column] = _isoformat(_parse_date(value))
                else:
                    record[column] = value
            records.append(record)
    if period == "D":
        records.sort(key=lambda record: record[date_column])
    return Table(columns=columns, records=records)


_LTP_COLUMN_MAP = {
    "scrip_cd": "SCRIP CODE",
    "scripname": "SYMBOL",
    "LONG_NAME": "COMPANY NAME",
    "openrate": "OPEN",
    "highrate": "HIGH",
    "lowrate": "LOW",
    "prevdayclose": "PREV. CLOSE",
    "ltradert": "LTP",
    "change_val": "CHNG",
    "change_percent": "%CHNG",
    "trd_vol": "VOLUME (shares)",
    "trd_val": "VALUE (Rs. Lakhs)",
    "nooftrd": "NO. OF TRADES",
    "dt_tm": "UPDATED AT",
}
_LTP_TEXT_COLUMNS = ("SCRIP CODE", "SYMBOL", "COMPANY NAME", "UPDATED AT")


async def _sensex_rows() -> list[dict]:
    payload, _ = await get_json("GetMktData", {
        "ordcol": "TT", "strType": "index", "strfilter": SENSEX_INDEX_NAME,
    })
    records = rows(payload)
    if not records or any("scrip_cd" not in row or "ltradert" not in row for row in records):
        raise RuntimeError("Unexpected BSE Sensex snapshot: missing constituent records")
    return records


async def get_sensex_stocks() -> dict[str, str]:
    """Map each Sensex constituent's company name to its BSE scrip code."""
    return {
        str(row.get("LONG_NAME") or row.get("scripname")).strip(): str(row["scrip_cd"]).strip()
        for row in await _sensex_rows()
    }


async def get_sensex_stocks_ltp() -> Table:
    """Latest traded-price snapshot for the Sensex constituents."""
    records = []
    for row in await _sensex_rows():
        record: dict[str, Any] = {}
        for source, column in _LTP_COLUMN_MAP.items():
            value = row.get(source)
            if column in _LTP_TEXT_COLUMNS:
                record[column] = None if value is None else str(value).strip()
            else:
                record[column] = _to_float(value)
        records.append(record)
    return Table(columns=list(_LTP_COLUMN_MAP.values()), records=records)
