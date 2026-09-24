"""Utilities to collect historical data for NSE stocks and indices."""

import asyncio
import csv
import dataclasses
import datetime
import io
import json
import random
import re
import urllib.parse
from typing import Any

import httpx2

BASE_URL = "https://www.nseindia.com"
NIFTY50_STOCK_LIST_URL = "https://nsearchives.nseindia.com/content/indices/ind_nifty50list.csv"
EQUITY_SECURITIES_PAGE_URL = BASE_URL + "/static/market-data/securities-available-for-trading"
EQUITY_SECURITIES_CSV_URL = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
STOCK_HIST_PRICE_VOL_DATA_BASE_URL = "https://www.nseindia.com/report-detail/eq_security"
NSE_INDICES_DATA_BASE_URL = "https://www.nseindia.com/reports-indices-historical-index-data"
NSE_INDICES_SYMBOL_NAMES_URL = "https://www.nseindia.com/api/index-names"
NSE_STOCK_LTP_DATA_BASE_URL = "https://www.nseindia.com/market-data/live-equity-market"
NSE_REQUEST_ATTEMPTS = 3
NSE_REQUEST_TIMEOUT = httpx2.Timeout(10.0, connect=5.0)

# Formats seen on the NSE date columns. Pandas inferred these per value with
# ``format="mixed"``; the stdlib parser needs them listed explicitly.
_DATE_FORMATS = ("%d-%b-%Y", "%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y")


@dataclasses.dataclass(frozen=True)
class Table:
    """An ordered set of column names with their JSON-safe row dictionaries.

    This replaces the Pandas DataFrame these utilities used to return. Pandas
    and NumPy added tens of megabytes to the install while only being used to
    reshape NSE payloads that are serialized straight back to JSON.
    """

    columns: list[str]
    records: list[dict[str, Any]]

    def __len__(self) -> int:
        return len(self.records)


def _read_csv(text: str) -> tuple[list[str], list[list[str]]]:
    """Split CSV text into stripped column names and their raw rows."""
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return [], []
    return [column.strip() for column in rows[0]], [row for row in rows[1:] if row]


def _parse_date(value: str) -> datetime.datetime:
    """Parse an NSE date using the formats its endpoints are known to emit."""
    value = value.strip()
    for date_format in _DATE_FORMATS:
        try:
            return datetime.datetime.strptime(value, date_format)
        except ValueError:
            continue
    raise ValueError(f"Unrecognized NSE date value: {value!r}")


def _isoformat(value: datetime.datetime) -> str:
    """Render a timestamp the way ``DataFrame.to_json(date_format="iso")`` did."""
    return f"{value.strftime('%Y-%m-%dT%H:%M:%S')}.{value.microsecond // 1000:03d}"


# Placeholders NSE uses for a missing cell. They map to None rather than 0 so a
# missing value is not mistaken for a reported zero.
_MISSING_VALUES = ("", "-", "NA", "N/A")


def _to_int(value: Any) -> int | None:
    """Coerce an NSE cell to int, keeping its missing-value placeholders null."""
    if value is None:
        return None
    text = str(value).strip()
    if text in _MISSING_VALUES:
        return None
    return int(text)


def _to_float(value: Any) -> float | None:
    """Coerce an NSE cell to float, keeping its missing-value placeholders null."""
    if value is None:
        return None
    text = str(value).strip()
    if text in _MISSING_VALUES:
        return None
    return float(text)


def _create_http_client() -> httpx2.AsyncClient:
    """Create a cookie-preserving async client for the NSE endpoints."""
    return httpx2.AsyncClient(
        follow_redirects=True,
        timeout=NSE_REQUEST_TIMEOUT,
    )


async def _get_nse_response(landing_url: str, data_url: str) -> httpx2.Response:
    """Prime NSE cookies and fetch data with bounded retries."""
    last_error: httpx2.HTTPError | None = None

    async with _create_http_client() as client:
        for attempt in range(NSE_REQUEST_ATTEMPTS):
            try:
                user_agent = get_useragent()
                landing_headers = {
                    "Accept": (
                        "text/html,application/xhtml+xml,application/xml;q=0.9,"
                        "image/avif,image/webp,*/*;q=0.8"
                    ),
                    "Accept-Language": "en-US,en;q=0.9",
                    "Upgrade-Insecure-Requests": "1",
                    "User-Agent": user_agent,
                }
                data_headers = {
                    "Accept": "application/json,text/csv,text/plain,*/*",
                    "Accept-Language": "en-US,en;q=0.9",
                    "Referer": landing_url,
                    "User-Agent": user_agent,
                }

                # NSE uses the landing request to establish cookies. Its edge can
                # return a challenge status while still setting useful cookies;
                # the original requests-based implementation therefore did not
                # reject this response before attempting the data URL.
                await client.get(landing_url, headers=landing_headers)
                response = await client.get(data_url, headers=data_headers)
                response.raise_for_status()
                return response
            except httpx2.HTTPError as error:
                last_error = error
                if attempt + 1 < NSE_REQUEST_ATTEMPTS:
                    await asyncio.sleep(0.25 * (2**attempt))

    raise RuntimeError(
        f"NSE request failed after {NSE_REQUEST_ATTEMPTS} attempts"
    ) from last_error


def get_useragent() -> str:
    _useragent_list = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:66.0) Gecko/20100101 Firefox/66.0",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/109.0.0.0 Safari/537.36",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36 Edg/123.0.1661.62",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/123.0",
    ]
    return random.choice(_useragent_list)


def validate_start_end_date_str(
    start_date_str: str, end_date_str: str
) -> None:
    """Validates the start and end date strings.

    Args:
        start_date_str: Start Date string in DD-MM-YYYY format.
        end_date_str: End Date string in DD-MM-YYYY format.
    """
    start_date = datetime.datetime.strptime(start_date_str, r"%d-%m-%Y").date()
    end_date = datetime.datetime.strptime(end_date_str, r"%d-%m-%Y").date()
    if end_date < start_date:
        raise ValueError("End Date should be after Start Date")


def split_date_range(start_date_str: str, end_date_str: str) -> list:
    """Divide the time range in 1 year chunks.

    NSE api does not support time range of more than 1 year.
    """
    date_chunks = []
    start_date = datetime.datetime.strptime(start_date_str, r"%d-%m-%Y").date()
    end_date = datetime.datetime.strptime(end_date_str, r"%d-%m-%Y").date()
    current_date = start_date
    while current_date <= end_date:
        try:
            next_year_start = current_date.replace(year=current_date.year + 1)
        except ValueError:
            # 29 February has no direct anniversary in a non-leap year.
            next_year_start = current_date.replace(
                year=current_date.year + 1, month=3, day=1
            )
        next_year_end = next_year_start - datetime.timedelta(days=1)
        chunk_end = min(next_year_end, end_date)
        date_chunks.append(
            (current_date.strftime(r"%d-%m-%Y"), chunk_end.strftime(r"%d-%m-%Y"))
        )
        current_date = next_year_start
    return date_chunks


async def get_nifty50_stocks() -> dict:
    """Gets latest stocks included in Nifty50 Index along with their symbol."""
    response = await _get_nse_response(BASE_URL, NIFTY50_STOCK_LIST_URL)
    columns, rows = _read_csv(response.text)
    name_index = columns.index("Company Name")
    symbol_index = columns.index("Symbol")
    return {row[name_index]: row[symbol_index] for row in rows}


async def get_equity_segment_securities() -> Table:
    """List every security in NSE's equity-segment CSV, including non-EQ series."""
    response = await _get_nse_response(
        EQUITY_SECURITIES_PAGE_URL, EQUITY_SECURITIES_CSV_URL
    )
    columns, rows = _read_csv(response.text)
    if "SYMBOL" not in columns or "SERIES" not in columns:
        raise ValueError("Unexpected NSE equity securities CSV columns")
    return Table(
        columns=columns,
        records=[dict(zip(columns, row)) for row in rows],
    )


async def get_stock_historical_price_volume_data(
    stock_symbol: str, start_date_str: str, end_date_str: str
) -> Table:
    """Gets Price Volume data for a stock in given date range.

    Ranges longer than one year are fetched in one-year chunks, since the NSE
    api rejects longer spans, and merged in ascending date order.

    Args:
        stock_symbol: Unique symbol of the stock used in the market.
        start_date_str: Start Date string in DD-MM-YYYY format.
        end_date_str: End Date string in DD-MM-YYYY format.

    Returns:
        Table of historical price volume data for the stock.
    """
    validate_start_end_date_str(
        start_date_str=start_date_str, end_date_str=end_date_str
    )
    stock_symbol_processed = urllib.parse.quote(stock_symbol)

    columns: list[str] = []
    raw_records: list[dict[str, str]] = []
    for chunk_start, chunk_end in split_date_range(start_date_str, end_date_str):
        path = "/api/historicalOR/generateSecurityWiseHistoricalData"
        path += f"?from={chunk_start}"
        path += f"&to={chunk_end}"
        path += f"&symbol={stock_symbol_processed}"
        path += "&type=priceVolumeDeliverable&series=ALL&csv=true"
        final_url = BASE_URL + path
        response = await _get_nse_response(
            STOCK_HIST_PRICE_VOL_DATA_BASE_URL, final_url
        )

        chunk_columns, chunk_rows = _read_csv(response.text)
        chunk_columns = [
            re.sub(r'[^a-zA-Z0-9]*Symbol[^a-zA-Z0-9]*', 'Symbol', column)
            for column in chunk_columns
        ]
        chunk_columns = [
            re.sub(r'[^a-zA-Z0-9]*Turnover[^a-zA-Z0-9]*', 'Turnover(Rupees)', column)
            for column in chunk_columns
        ]
        # An access challenge or error page can arrive with a 200 status. Without
        # this check it would parse as a CSV with no EQ rows, i.e. no trading.
        if not {"Symbol", "Series", "Date"} <= set(chunk_columns):
            raise RuntimeError(
                "Unexpected NSE stock history response (possibly an access challenge)"
            )
        if not columns:
            columns = chunk_columns
        raw_records.extend(dict(zip(chunk_columns, row)) for row in chunk_rows)

    int_cols = ["Total Traded Quantity", "No. of Trades", "Deliverable Qty"]
    float_cols = [
        "Prev Close",
        "Open Price",
        "High Price",
        "Low Price",
        "Last Price",
        "Close Price",
        "Average Price",
        "Turnover(Rupees)",
        "% Dly Qt to Traded Qty",
    ]

    records = []
    for raw_record in raw_records:
        # NSE quotes thousands separators inside its numeric cells, and pads
        # its CSV cells with whitespace as it does the column names.
        record = {
            column: value.replace(",", "").strip()
            for column, value in raw_record.items()
        }
        if record.get("Series") != "EQ":
            continue
        record["Date"] = _isoformat(_parse_date(record["Date"]))
        for int_col in int_cols:
            record[int_col] = _to_int(record.get(int_col))
        for float_col in float_cols:
            record[float_col] = _to_float(record.get(float_col))
        records.append(record)
    # ISO timestamps sort chronologically, keeping merged chunks in order.
    records.sort(key=lambda record: record["Date"])

    return Table(columns=columns, records=records)


async def get_nse_index_symbol_names() -> dict:
    """Gets the ids and names of NSE Indices."""
    response = await _get_nse_response(
        NSE_INDICES_DATA_BASE_URL, NSE_INDICES_SYMBOL_NAMES_URL
    )
    stn_list = json.loads(response.text)["stn"]
    return {sym: name for sym, name in stn_list}


async def get_nse_index_historical_ohlc_volume_data(
    index_symbol: str, start_date_str: str, end_date_str: str
) -> Table:
    """Gets historical OHLC, shares traded and turnover data for an index.

    Ranges longer than one year are fetched in one-year chunks, since the NSE
    api rejects longer spans.

    Args:
        index_symbol: Unique symbol of the NSE index used in the market.
        start_date_str: Start Date string in DD-MM-YYYY format.
        end_date_str: End Date string in DD-MM-YYYY format.

    Returns:
        Table of historical OHLC, shares traded and turnover data for the index.
    """
    validate_start_end_date_str(
        start_date_str=start_date_str, end_date_str=end_date_str
    )
    index_symbol_name_map = await get_nse_index_symbol_names()
    if index_symbol not in index_symbol_name_map:
        raise ValueError(
            f"Unknown NSE index symbol {index_symbol!r}; call "
            "get_nse_index_symbol_names for valid symbols"
        )
    index_name = index_symbol_name_map[index_symbol]
    index_name_processed = urllib.parse.quote(index_name)

    data: list[dict[str, Any]] = []
    for chunk_start, chunk_end in split_date_range(start_date_str, end_date_str):
        path = "/api/historicalOR/indicesHistory"
        path += f"?indexType={index_name_processed}"
        path += f"&from={chunk_start}"
        path += f"&to={chunk_end}"
        final_url = BASE_URL + path
        response = await _get_nse_response(NSE_INDICES_DATA_BASE_URL, final_url)

        # The API now returns one combined list of OHLC and turnover records. It
        # previously returned separate ``indexCloseOnlineRecords`` and
        # ``indexTurnoverRecords`` lists which had to be merged.
        data.extend(json.loads(response.text)["data"])

    source_columns: list[str] = []
    for row in data:
        for key in row:
            if key not in source_columns and key not in ("HI_TIMESTAMP", "_id"):
                source_columns.append(key)
    columns = [
        column.replace("EOD_", "").replace("HIT_", "").replace("_UPPER", "")
        for column in source_columns
    ]

    records = [
        {
            column: row.get(source_column)
            for source_column, column in zip(source_columns, columns)
        }
        for row in data
    ]
    for record in records:
        record["TIMESTAMP"] = _parse_date(record["TIMESTAMP"])
    records.sort(key=lambda record: record["TIMESTAMP"])
    for record in records:
        record["TIMESTAMP"] = _isoformat(record["TIMESTAMP"])

    return Table(columns=columns, records=records)


async def get_nifty50_stocks_ltp() -> Table:
    """Gets the last traded price data for Nifty 50 stocks."""
    path = "/api/NextApi/apiClient/marketWatchApi"
    path += "?functionName=getIndicesData&symbol=NIFTY%2050"
    final_url = BASE_URL + path
    response = await _get_nse_response(NSE_STOCK_LTP_DATA_BASE_URL, final_url)

    column_map = {
        "symbol": "SYMBOL",
        "open": "OPEN",
        "dayHigh": "HIGH",
        "dayLow": "LOW",
        "previousClose": "PREV. CLOSE",
        "lastPrice": "LTP",
        "stockIndClosePrice": "INDICATIVE CLOSE",
        "change": "CHNG",
        "pChange": "%CHNG",
        "totalTradedVolume": "VOLUME (shares)",
        "totalTradedValue": "VALUE (Rs. Crores)",
        "yearHigh": "52W H",
        "yearLow": "52W L",
        "perChange30d": "30 D %CHNG",
        "perChange365d": "365 D % CHNG",
    }
    market_data = json.loads(response.text)["data"]["data"]
    columns = list(column_map.values())

    records = []
    # The first record describes the Nifty 50 index itself; the remaining EQ
    # records are its constituent stocks.
    for row in market_data:
        if row.get("series") != "EQ":
            continue
        record: dict[str, Any] = {"SYMBOL": row.get("symbol")}
        for source_column, column in column_map.items():
            if column == "SYMBOL":
                continue
            record[column] = _to_float(row.get(source_column))
        # The old CSV endpoint returned traded value in crores, whereas the new
        # JSON endpoint returns it in rupees.
        if record["VALUE (Rs. Crores)"] is not None:
            record["VALUE (Rs. Crores)"] /= 10_000_000
        records.append(record)

    return Table(columns=columns, records=records)
