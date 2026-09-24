"""Tests for async NSE HTTP utilities."""

from __future__ import annotations

import asyncio
import datetime
import json
import urllib.parse

import httpx2
import pytest

import nse_data_mcp.data_utils as nse


def test_get_nse_response_primes_cookies_and_fetches_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested_urls: list[str] = []

    async def handler(request: httpx2.Request) -> httpx2.Response:
        requested_urls.append(str(request.url))
        if request.url.path == "/landing":
            return httpx2.Response(
                200,
                headers={"set-cookie": "nse-session=test; Path=/"},
            )
        assert request.headers["cookie"] == "nse-session=test"
        return httpx2.Response(200, json={"ok": True})

    transport = httpx2.MockTransport(handler)
    monkeypatch.setattr(
        nse,
        "_create_http_client",
        lambda: httpx2.AsyncClient(transport=transport),
    )

    response = asyncio.run(
        nse._get_nse_response(
            "https://example.test/landing", "https://example.test/data"
        )
    )

    assert response.json() == {"ok": True}
    assert requested_urls == [
        "https://example.test/landing",
        "https://example.test/data",
    ]


def test_get_nse_response_still_fetches_data_after_landing_challenge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/landing":
            return httpx2.Response(
                403,
                headers={"set-cookie": "nse-session=test; Path=/"},
            )
        assert request.headers["cookie"] == "nse-session=test"
        assert request.headers["referer"] == "https://example.test/landing"
        return httpx2.Response(200, json={"ok": True})

    transport = httpx2.MockTransport(handler)
    monkeypatch.setattr(
        nse,
        "_create_http_client",
        lambda: httpx2.AsyncClient(transport=transport),
    )

    response = asyncio.run(
        nse._get_nse_response(
            "https://example.test/landing", "https://example.test/data"
        )
    )

    assert response.json() == {"ok": True}


def test_get_nse_response_has_bounded_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_count = 0

    async def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal request_count
        request_count += 1
        return httpx2.Response(503, request=request)

    async def no_sleep(_: float) -> None:
        return None

    transport = httpx2.MockTransport(handler)
    monkeypatch.setattr(
        nse,
        "_create_http_client",
        lambda: httpx2.AsyncClient(transport=transport),
    )
    monkeypatch.setattr(nse.asyncio, "sleep", no_sleep)

    with pytest.raises(RuntimeError, match="failed after 3 attempts"):
        asyncio.run(
            nse._get_nse_response(
                "https://example.test/landing", "https://example.test/data"
            )
        )

    assert request_count == nse.NSE_REQUEST_ATTEMPTS * 2


def test_split_date_range_handles_leap_day() -> None:
    assert nse.split_date_range("29-02-2024", "02-03-2025") == [
        ("29-02-2024", "28-02-2025"),
        ("01-03-2025", "02-03-2025"),
    ]


STOCK_HISTORY_CSV = (
    '"Symbol ","Series","Date","Prev Close","Open Price","High Price",'
    '"Low Price","Last Price","Close Price","Average Price",'
    '"Total Traded Quantity","Turnover ₹","No. of Trades",'
    '"Deliverable Qty","% Dly Qt to Traded Qty"\n'
    '"INFY","EQ","02-Jan-2024","1,500.50","1,510.00","1,520.00","1,495.00",'
    '"1,505.00","1,508.25","1,499.75","1,234,567","1,850,000,000.50",'
    '"45,678","900,000","72.9"\n'
    '"INFY","BE","03-Jan-2024","-","-","-","-","-","-","-","-","-","-","-","-"\n'
)


def _stub_nse_response(monkeypatch: pytest.MonkeyPatch, responses: dict) -> None:
    """Route ``_get_nse_response`` to canned payloads keyed by data URL prefix."""

    async def fake_response(landing_url: str, data_url: str) -> httpx2.Response:
        for prefix, text in responses.items():
            if prefix in data_url:
                return httpx2.Response(200, text=text)
        raise AssertionError(f"Unexpected NSE request: {data_url}")

    monkeypatch.setattr(nse, "_get_nse_response", fake_response)


def test_get_nifty50_stocks_maps_company_names_to_symbols(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_nse_response(
        monkeypatch,
        {
            "ind_nifty50list.csv": (
                "Company Name,Industry,Symbol,Series,ISIN Code\n"
                "Infosys Ltd.,Information Technology,INFY,EQ,INE009A01021\n"
            )
        },
    )

    assert asyncio.run(nse.get_nifty50_stocks()) == {"Infosys Ltd.": "INFY"}


def test_get_equity_segment_securities_keeps_all_series(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_response(landing_url: str, data_url: str) -> httpx2.Response:
        assert landing_url == nse.EQUITY_SECURITIES_PAGE_URL
        assert data_url == nse.EQUITY_SECURITIES_CSV_URL
        return httpx2.Response(
            200,
            text=(
                "SYMBOL,NAME OF COMPANY, SERIES, DATE OF LISTING, PAID UP VALUE, MARKET LOT, ISIN NUMBER, FACE VALUE\n"
                "INFY,Infosys Limited,EQ,08-FEB-1995,5,1,INE009A01021,5\n"
                "EXAMPLE,Example Limited,BE,01-JAN-2024,10,1,INE000A01000,10\n"
            ),
        )

    monkeypatch.setattr(nse, "_get_nse_response", fake_response)

    table = asyncio.run(nse.get_equity_segment_securities())

    assert len(table) == 2
    assert table.columns == [
        "SYMBOL", "NAME OF COMPANY", "SERIES", "DATE OF LISTING",
        "PAID UP VALUE", "MARKET LOT", "ISIN NUMBER", "FACE VALUE",
    ]
    assert [row["SERIES"] for row in table.records] == ["EQ", "BE"]
    assert table.records[0]["ISIN NUMBER"] == "INE009A01021"


def test_stock_history_normalizes_columns_and_numbers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_nse_response(
        monkeypatch,
        {"generateSecurityWiseHistoricalData": STOCK_HISTORY_CSV},
    )

    table = asyncio.run(
        nse.get_stock_historical_price_volume_data(
            "INFY", "01-01-2024", "31-01-2024"
        )
    )

    assert table.columns[0] == "Symbol"
    assert "Turnover(Rupees)" in table.columns
    # Only the EQ series row survives; the BE row is dropped.
    assert len(table) == 1
    record = table.records[0]
    assert record["Date"] == "2024-01-02T00:00:00.000"
    assert record["Prev Close"] == 1500.50
    assert record["Turnover(Rupees)"] == 1850000000.50
    assert record["Total Traded Quantity"] == 1234567
    assert record["No. of Trades"] == 45678
    assert record["Deliverable Qty"] == 900000
    assert isinstance(record["Total Traded Quantity"], int)


def test_stock_history_keeps_placeholder_cells_null(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    csv_text = STOCK_HISTORY_CSV.replace('"INFY","BE"', '"INFY","EQ"')
    csv_text += '"INFY","EQ","04-Jan-2024","0","NA","N/A","","-","0","0","0","0","0","0","0","0"\n'
    _stub_nse_response(
        monkeypatch, {"generateSecurityWiseHistoricalData": csv_text}
    )

    table = asyncio.run(
        nse.get_stock_historical_price_volume_data(
            "INFY", "01-01-2024", "31-01-2024"
        )
    )

    placeholder_row = table.records[1]
    assert placeholder_row["Close Price"] is None
    assert placeholder_row["Total Traded Quantity"] is None
    assert placeholder_row["Deliverable Qty"] is None
    mixed_row = table.records[2]
    assert [mixed_row[column] for column in (
        "Open Price", "High Price", "Low Price", "Last Price"
    )] == [None, None, None, None]
    # A reported zero is still distinct from a missing value.
    assert mixed_row["Prev Close"] == 0.0
    assert mixed_row["Total Traded Quantity"] == 0


def test_index_history_renames_drops_and_sorts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_nse_response(
        monkeypatch,
        {
            "index-names": json.dumps({"stn": [["NIFTY 50", "NIFTY 50"]]}),
            "indicesHistory": json.dumps(
                {
                    "data": [
                        {
                            "_id": "b",
                            "HI_TIMESTAMP": "ignored",
                            "EOD_TIMESTAMP": "03-Jan-2024",
                            "EOD_CLOSE_INDEX_VAL": 110.0,
                            "HIT_TRADED_QTY": 5,
                            "EOD_INDEX_NAME_UPPER": "NIFTY 50",
                        },
                        {
                            "_id": "a",
                            "EOD_TIMESTAMP": "02-Jan-2024",
                            "EOD_CLOSE_INDEX_VAL": 95.0,
                            "HIT_TRADED_QTY": 4,
                            "EOD_INDEX_NAME_UPPER": "NIFTY 50",
                        },
                    ]
                }
            ),
        },
    )

    table = asyncio.run(
        nse.get_nse_index_historical_ohlc_volume_data(
            "NIFTY 50", "01-01-2024", "31-01-2024"
        )
    )

    assert table.columns == [
        "TIMESTAMP",
        "CLOSE_INDEX_VAL",
        "TRADED_QTY",
        "INDEX_NAME",
    ]
    assert [record["TIMESTAMP"] for record in table.records] == [
        "2024-01-02T00:00:00.000",
        "2024-01-03T00:00:00.000",
    ]
    assert "_id" not in table.records[0]
    assert "HI_TIMESTAMP" not in table.records[0]


def test_ltp_filters_equities_and_converts_turnover_to_crores(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_nse_response(
        monkeypatch,
        {
            "marketWatchApi": json.dumps(
                {
                    "data": {
                        "data": [
                            {"symbol": "NIFTY 50", "series": ""},
                            {
                                "symbol": "INFY",
                                "series": "EQ",
                                "open": 1500.0,
                                "lastPrice": 1505.0,
                                "totalTradedValue": 20_000_000.0,
                                "stockIndClosePrice": None,
                            },
                        ]
                    }
                }
            )
        },
    )

    table = asyncio.run(nse.get_nifty50_stocks_ltp())

    assert table.columns[0] == "SYMBOL"
    # The leading index record is not an EQ series row.
    assert len(table) == 1
    record = table.records[0]
    assert record["SYMBOL"] == "INFY"
    assert record["VALUE (Rs. Crores)"] == 2.0
    # Missing cells stay null instead of posing as a zero price.
    assert record["INDICATIVE CLOSE"] is None
    assert record["52W H"] is None


def test_ltp_keeps_placeholder_and_missing_values_null(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_nse_response(
        monkeypatch,
        {
            "marketWatchApi": json.dumps(
                {
                    "data": {
                        "data": [
                            {
                                "symbol": "INFY",
                                "series": "EQ",
                                "lastPrice": "NA",
                                "change": "-",
                                "pChange": 0,
                            },
                        ]
                    }
                }
            )
        },
    )

    record = asyncio.run(nse.get_nifty50_stocks_ltp()).records[0]

    assert record["LTP"] is None
    assert record["CHNG"] is None
    assert record["VALUE (Rs. Crores)"] is None
    # A reported zero is still distinct from a missing value.
    assert record["%CHNG"] == 0.0


def _record_nse_requests(
    monkeypatch: pytest.MonkeyPatch, respond
) -> list[dict[str, str]]:
    """Route ``_get_nse_response`` to ``respond`` and log each data URL's query."""
    queries: list[dict[str, str]] = []

    async def fake_response(landing_url: str, data_url: str) -> httpx2.Response:
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(data_url).query))
        queries.append(query)
        return httpx2.Response(200, text=respond(data_url, query))

    monkeypatch.setattr(nse, "_get_nse_response", fake_response)
    return queries


@pytest.mark.parametrize(
    "body",
    [
        "<html><head><title>Access Denied</title></head><body>Denied</body></html>",
        "",
    ],
)
def test_stock_history_rejects_non_csv_response(
    monkeypatch: pytest.MonkeyPatch, body: str
) -> None:
    _stub_nse_response(monkeypatch, {"generateSecurityWiseHistoricalData": body})

    with pytest.raises(RuntimeError, match="Unexpected NSE stock history response"):
        asyncio.run(
            nse.get_stock_historical_price_volume_data(
                "INFY", "01-01-2024", "31-01-2024"
            )
        )


def test_stock_history_header_only_response_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    header = STOCK_HISTORY_CSV.splitlines()[0] + "\n"
    _stub_nse_response(monkeypatch, {"generateSecurityWiseHistoricalData": header})

    table = asyncio.run(
        nse.get_stock_historical_price_volume_data("INFY", "01-01-2024", "31-01-2024")
    )

    assert len(table) == 0
    assert "Symbol" in table.columns


def test_stock_history_strips_padded_cells(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    csv_text = (
        '"Symbol ","Series ","Date ","Close Price "\n'
        '"INFY ","EQ ","02-Jan-2024 "," 1,508.25 "\n'
    )
    _stub_nse_response(monkeypatch, {"generateSecurityWiseHistoricalData": csv_text})

    table = asyncio.run(
        nse.get_stock_historical_price_volume_data("INFY", "01-01-2024", "31-01-2024")
    )

    assert table.records == [
        {
            "Symbol": "INFY",
            "Series": "EQ",
            "Date": "2024-01-02T00:00:00.000",
            "Close Price": 1508.25,
            # Columns absent from the response are missing, not zero.
            "Total Traded Quantity": None,
            "No. of Trades": None,
            "Deliverable Qty": None,
            "Prev Close": None,
            "Open Price": None,
            "High Price": None,
            "Low Price": None,
            "Last Price": None,
            "Average Price": None,
            "Turnover(Rupees)": None,
            "% Dly Qt to Traded Qty": None,
        }
    ]


def test_stock_history_splits_multi_year_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def respond(data_url: str, query: dict[str, str]) -> str:
        # NSE lists rows newest first; the merged result must still ascend.
        chunk_start = datetime.datetime.strptime(query["from"], "%d-%m-%Y")
        dates = [chunk_start + datetime.timedelta(days=1), chunk_start]
        return '"Symbol ","Series","Date"\n' + "".join(
            f'"INFY","EQ","{day:%d-%b-%Y}"\n' for day in dates
        )

    queries = _record_nse_requests(monkeypatch, respond)

    table = asyncio.run(
        nse.get_stock_historical_price_volume_data("INFY", "01-01-2023", "15-03-2024")
    )

    assert [(q["from"], q["to"]) for q in queries] == [
        ("01-01-2023", "31-12-2023"),
        ("01-01-2024", "15-03-2024"),
    ]
    assert all(q["symbol"] == "INFY" for q in queries)
    assert [record["Date"] for record in table.records] == [
        "2023-01-01T00:00:00.000",
        "2023-01-02T00:00:00.000",
        "2024-01-01T00:00:00.000",
        "2024-01-02T00:00:00.000",
    ]


def test_index_history_splits_multi_year_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def respond(data_url: str, query: dict[str, str]) -> str:
        if "index-names" in data_url:
            return json.dumps({"stn": [["NIFTY 50", "NIFTY 50"]]})
        return json.dumps(
            {"data": [{"EOD_TIMESTAMP": query["to"], "EOD_CLOSE_INDEX_VAL": 1.0}]}
        )

    queries = _record_nse_requests(monkeypatch, respond)

    table = asyncio.run(
        nse.get_nse_index_historical_ohlc_volume_data(
            "NIFTY 50", "01-01-2023", "15-03-2024"
        )
    )

    history_queries = [q for q in queries if "indexType" in q]
    # The symbol-name map is fetched once, not per chunk.
    assert len(queries) == len(history_queries) + 1
    assert [(q["from"], q["to"]) for q in history_queries] == [
        ("01-01-2023", "31-12-2023"),
        ("01-01-2024", "15-03-2024"),
    ]
    assert [record["TIMESTAMP"] for record in table.records] == [
        "2023-12-31T00:00:00.000",
        "2024-03-15T00:00:00.000",
    ]


def test_index_history_rejects_unknown_symbol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_nse_response(
        monkeypatch,
        {"index-names": json.dumps({"stn": [["NIFTY 50", "NIFTY 50"]]})},
    )

    with pytest.raises(ValueError, match="get_nse_index_symbol_names"):
        asyncio.run(
            nse.get_nse_index_historical_ohlc_volume_data(
                "NIFTY50", "01-01-2024", "31-01-2024"
            )
        )
