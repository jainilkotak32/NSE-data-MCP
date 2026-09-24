"""Tests for the MCP-facing adapters without making calls to NSE."""

from __future__ import annotations

import asyncio
import inspect

import pytest
from mcp import Client

from nse_data_mcp.data_utils import Table
import nse_data_mcp.server as mcp_server


def test_all_mcp_tools_are_async() -> None:
    tools = (
        mcp_server.get_useragent,
        mcp_server.validate_start_end_date_str,
        mcp_server.split_date_range,
        mcp_server.get_nifty50_stocks,
        mcp_server.get_equity_segment_securities,
        mcp_server.get_stock_historical_price_volume_data,
        mcp_server.get_nse_index_symbol_names,
        mcp_server.get_nse_index_historical_ohlc_volume_data,
        mcp_server.get_nifty50_stocks_ltp,
    )

    assert all(inspect.iscoroutinefunction(tool) for tool in tools)


def test_mcp_protocol_lists_and_calls_tools() -> None:
    async def exercise_server() -> tuple[set[str], object]:
        async with Client(mcp_server.build_server()) as client:
            listed_tools = await client.list_tools()
            result = await client.call_tool(
                "validate_start_end_date_str",
                {
                    "start_date_str": "01-01-2026",
                    "end_date_str": "02-01-2026",
                },
            )
            assert result.is_error is False
            return {tool.name for tool in listed_tools.tools}, result.structured_content

    tool_names, structured_content = asyncio.run(exercise_server())

    assert tool_names == {
        "get_useragent",
        "validate_start_end_date_str",
        "split_date_range",
        "get_nifty50_stocks",
        "get_equity_segment_securities",
        "get_stock_historical_price_volume_data",
        "get_nse_index_symbol_names",
        "get_nse_index_historical_ohlc_volume_data",
        "get_nifty50_stocks_ltp",
        "get_financial_results",
        "get_shareholding_patterns",
        "get_corporate_actions",
        "get_corporate_announcements",
        "get_earnings_call_transcripts",
        "get_bulk_block_deals",
        "get_short_selling_data",
        "get_corporate_document",
        "get_filing_facts",
    }
    assert structured_content == {
        "valid": True,
        "start_date": "01-01-2026",
        "end_date": "02-01-2026",
    }


def test_equity_securities_description_explains_symbol_lookup() -> None:
    async def get_description() -> str:
        async with Client(mcp_server.build_server()) as client:
            tools = await client.list_tools()
            return next(
                tool.description for tool in tools.tools
                if tool.name == "get_equity_segment_securities"
            )

    description = asyncio.run(get_description())

    assert "NAME OF COMPANY" in description
    assert "SYMBOL" in description


def test_table_response_is_json_safe_and_paginated() -> None:
    table = Table(
        columns=["TIMESTAMP", "CLOSE"],
        records=[
            {"TIMESTAMP": "2026-01-01T00:00:00.000", "CLOSE": 10.5},
            {"TIMESTAMP": "2026-01-02T00:00:00.000", "CLOSE": 0.0},
        ],
    )

    result = mcp_server._table_response(
        table, offset=0, limit=1, metadata={"symbol": "TEST"}
    )

    assert result["columns"] == ["TIMESTAMP", "CLOSE"]
    assert result["records"] == [
        {"TIMESTAMP": "2026-01-01T00:00:00.000", "CLOSE": 10.5}
    ]
    assert result["pagination"] == {
        "offset": 0,
        "limit": 1,
        "returned_rows": 1,
        "total_rows": 2,
        "has_more": True,
        "next_offset": 1,
    }
    assert result["metadata"] == {"symbol": "TEST"}


@pytest.mark.parametrize(
    ("offset", "limit", "message"),
    [
        (-1, 10, "offset must be greater than or equal to 0"),
        (0, 0, "limit must be between 1 and 5000"),
        (0, 5_001, "limit must be between 1 and 5000"),
    ],
)
def test_table_response_validates_pagination(
    offset: int, limit: int, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        mcp_server._table_response(
            Table(columns=[], records=[]), offset=offset, limit=limit
        )


@pytest.mark.parametrize(
    ("tool_name", "args"),
    [
        ("get_equity_segment_securities", ()),
        ("get_stock_historical_price_volume_data", ("INFY", "01-01-2026", "31-01-2026")),
        ("get_nse_index_historical_ohlc_volume_data", ("NIFTY 50", "01-01-2026", "31-01-2026")),
        ("get_nifty50_stocks_ltp", ()),
    ],
)
@pytest.mark.parametrize(
    ("offset", "limit", "message"),
    [
        (-1, 10, "offset must be greater than or equal to 0"),
        (0, 0, "limit must be between 1 and 5000"),
        (0, 5_001, "limit must be between 1 and 5000"),
    ],
)
def test_market_tools_validate_pagination_before_fetching(
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
    args: tuple[str, ...],
    offset: int,
    limit: int,
    message: str,
) -> None:
    async def fetch(*_: object) -> Table:
        raise AssertionError("NSE was queried despite invalid pagination")

    monkeypatch.setattr(mcp_server.nse, tool_name, fetch)

    with pytest.raises(ValueError, match=message):
        asyncio.run(
            getattr(mcp_server, tool_name)(*args, offset=offset, limit=limit)
        )


def test_validate_date_tool_returns_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        mcp_server.nse,
        "validate_start_end_date_str",
        lambda start, end: calls.append((start, end)),
    )

    result = asyncio.run(
        mcp_server.validate_start_end_date_str("01-01-2026", "02-01-2026")
    )

    assert calls == [("01-01-2026", "02-01-2026")]
    assert result == {
        "valid": True,
        "start_date": "01-01-2026",
        "end_date": "02-01-2026",
    }


def test_split_date_range_tool_returns_named_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        mcp_server.nse, "validate_start_end_date_str", lambda *_: None
    )
    monkeypatch.setattr(
        mcp_server.nse,
        "split_date_range",
        lambda *_: [("01-01-2025", "31-12-2025"), ("01-01-2026", "02-01-2026")],
    )

    result = asyncio.run(
        mcp_server.split_date_range("01-01-2025", "02-01-2026")
    )

    assert result == [
        {"start_date": "01-01-2025", "end_date": "31-12-2025"},
        {"start_date": "01-01-2026", "end_date": "02-01-2026"},
    ]


def test_stock_history_tool_serializes_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def get_history(*_: object) -> Table:
        return Table(
            columns=["Symbol", "Close Price"],
            records=[{"Symbol": "INFY", "Close Price": 1500.0}],
        )

    monkeypatch.setattr(
        mcp_server.nse,
        "get_stock_historical_price_volume_data",
        get_history,
    )

    result = asyncio.run(
        mcp_server.get_stock_historical_price_volume_data(
            "INFY", "01-01-2026", "31-01-2026"
        )
    )

    assert result["records"] == [{"Symbol": "INFY", "Close Price": 1500.0}]
    assert result["metadata"]["stock_symbol"] == "INFY"


def test_equity_segment_securities_tool_pages_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def get_securities() -> Table:
        return Table(
            columns=["SYMBOL", "SERIES"],
            records=[
                {"SYMBOL": "INFY", "SERIES": "EQ"},
                {"SYMBOL": "EXAMPLE", "SERIES": "BE"},
            ],
        )

    monkeypatch.setattr(mcp_server.nse, "get_equity_segment_securities", get_securities)

    result = asyncio.run(mcp_server.get_equity_segment_securities(offset=1, limit=1))

    assert result["records"] == [{"SYMBOL": "EXAMPLE", "SERIES": "BE"}]
    assert result["pagination"]["total_rows"] == 2
    assert result["metadata"]["source_url"] == mcp_server.nse.EQUITY_SECURITIES_CSV_URL


def test_index_history_tool_serializes_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def get_history(*_: object) -> Table:
        return Table(
            columns=["TIMESTAMP"],
            records=[{"TIMESTAMP": "2026-01-01T00:00:00.000"}],
        )

    monkeypatch.setattr(
        mcp_server.nse,
        "get_nse_index_historical_ohlc_volume_data",
        get_history,
    )

    result = asyncio.run(
        mcp_server.get_nse_index_historical_ohlc_volume_data(
            "NIFTY 50", "01-01-2026", "31-01-2026"
        )
    )

    assert result["records"] == [{"TIMESTAMP": "2026-01-01T00:00:00.000"}]
    assert result["metadata"]["index_symbol"] == "NIFTY 50"


def test_ltp_tool_pages_table(monkeypatch: pytest.MonkeyPatch) -> None:
    async def get_ltp() -> Table:
        return Table(
            columns=["SYMBOL", "LTP"],
            records=[{"SYMBOL": "A", "LTP": 1.0}, {"SYMBOL": "B", "LTP": 2.0}],
        )

    monkeypatch.setattr(
        mcp_server.nse,
        "get_nifty50_stocks_ltp",
        get_ltp,
    )

    result = asyncio.run(mcp_server.get_nifty50_stocks_ltp(offset=1, limit=1))

    assert result["records"] == [{"SYMBOL": "B", "LTP": 2.0}]
    assert result["pagination"]["has_more"] is False
