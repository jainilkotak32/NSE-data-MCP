"""Opt-in live BSE checks: BSE_LIVE_TESTS=1 uv run pytest tests/test_live_bse.py -v.

The default suite is offline. Failures are not silently skipped when enabled:
they indicate that a BSE endpoint or payload differs from what the server expects.
"""
import asyncio
import os

import pytest

from bse_data_mcp import server

pytestmark = pytest.mark.skipif(os.environ.get('BSE_LIVE_TESTS') != '1', reason='opt-in BSE network tests')

INFY = '500209'


def run(coroutine):
    return asyncio.run(coroutine)


def test_live_securities_lookup():
    result = run(server.get_equity_securities(group='A', limit=5000))
    assert any(row['SCRIP_CD'] == INFY for row in result['records'])


def test_live_sensex_snapshot():
    result = run(server.get_sensex_stocks_ltp())
    assert len(result['records']) == 30
    assert all(row['LTP'] for row in result['records'])


def test_live_stock_history():
    result = run(server.get_stock_historical_price_volume_data(INFY, '01-01-2024', '31-01-2024'))
    rows = result['records']
    assert 15 <= len(rows) <= 23
    assert all('2024-01-01' <= row['Date'][:10] <= '2024-01-31' for row in rows)
    assert all(row['Close Price'] for row in rows)


def test_live_index_names_and_history():
    names = run(server.get_bse_index_names())
    assert names['records']
    history = run(server.get_bse_index_historical_data('SENSEX', '01-01-2024', '31-01-2024'))
    assert 15 <= len(history['records']) <= 23


def test_live_corporate_actions():
    result = run(server.get_corporate_actions('01-10-2023', '31-10-2023', INFY))
    assert any(row['ex_date'] == '2023-10-25' and 'Dividend' in row['purpose']
               for row in result['records'])


def test_live_result_calendar_and_snapshot():
    assert 'records' in run(server.get_result_calendar('01-10-2023', '31-10-2023'))
    snapshot = run(server.get_financial_results_snapshot(INFY))
    assert any(row['metric'] == 'Revenue' for row in snapshot['records'])


def test_live_results_transcripts_and_document_text():
    async def exercise():
        results = await server.get_financial_results('01-07-2024', '31-07-2024', INFY)
        assert results['records']
        transcripts = await server.get_earnings_call_transcripts('01-07-2024', '31-08-2024', INFY)
        assert transcripts['records']
        url = transcripts['records'][0]['documents'][0]['url']
        document = await server.get_corporate_document(url, limit=100000)
        assert document['extraction_status'] == 'text'
        assert 'Infosys' in document['text']
    run(exercise())
