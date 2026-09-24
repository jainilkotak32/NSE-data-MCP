"""Opt-in live NSE checks: NSE_LIVE_TESTS=1 uv run pytest tests/test_live_nse.py -v.

Dates deliberately select known historical filings; the default suite is offline.
Failures are not silently skipped when explicitly enabled.
"""
import asyncio
import os

import pytest

from nse_data_mcp import server

pytestmark = pytest.mark.skipif(os.environ.get('NSE_LIVE_TESTS') != '1', reason='opt-in NSE network tests')


@pytest.fixture(autouse=True)
def live_network_transport(monkeypatch):
    if os.environ.get('NSE_LIVE_TRANSPORT') == 'curl':
        import httpx2
        from live_transport import CurlTransport
        monkeypatch.setattr(server.nse, '_create_http_client', lambda: httpx2.AsyncClient(
            transport=CurlTransport(), follow_redirects=True, timeout=server.nse.NSE_REQUEST_TIMEOUT))


def test_live_financial_results_and_actual_facts():
    async def exercise():
        # Integrated-era filings have no period classification, so 2026 must use
        # period='all'; the legacy feed still supports a period filter.
        for year, period, source_feed in (('2024', 'Quarterly', 'legacy'), ('2026', 'all', 'integrated')):
            response = await server.get_financial_results(
                f'01-07-{year}', f'31-07-{year}', 'INFY', basis='consolidated', period=period)
            assert response['records']
            record = response['records'][0]
            assert record['source_feed'] == source_feed
            assert record['period_end'] == f'{year}-06-30'
            url = next(doc['url'] for doc in record['documents'] if doc['kind'] == 'xbrl')
            facts = await server.get_filing_facts(url, concept='Revenue')
            assert facts['records']
            assert any(r['unit_id'] for r in facts['records'])
    asyncio.run(exercise())


def test_live_shareholding_and_detailed_pledge_facts():
    async def exercise():
        response = await server.get_shareholding_patterns('01-07-2026', '31-07-2026', 'INFY')
        assert response['records']
        record = response['records'][0]
        assert record['as_of_date'] == '2026-06-30'
        assert record['promoter_percent'] is not None
        url = next(doc['url'] for doc in record['documents'] if doc['kind'] == 'xbrl')
        facts = await server.get_filing_facts(url, concept='Pledged')
        assert facts['records']
    asyncio.run(exercise())


def test_live_actions():
    result = asyncio.run(server.get_corporate_actions('01-06-2026', '30-06-2026', 'INFY'))
    assert result['records']
    assert result['records'][0]['ex_date'] == '2026-06-10'


def test_live_transcript_discovery_and_pdf_text():
    async def exercise():
        results = await server.get_earnings_call_transcripts('01-07-2026', '31-07-2026', 'INFY')
        assert results['records']
        url = results['records'][0]['documents'][0]['url']
        document = await server.get_corporate_document(url, limit=100000)
        assert document['page_count'] > 1
        assert 'Infosys' in document['text']
        assert 'transcript' in document['text'].lower()
        assert document['extraction_status'] == 'text'
    asyncio.run(exercise())


@pytest.mark.parametrize('kind,start,end', [
    ('bulk', '17-09-2026', '18-09-2026'), ('block', '01-08-2026', '31-08-2026')])
def test_live_large_deals(kind, start, end):
    async def exercise():
        result = await server.get_bulk_block_deals(start, end, deal_type=kind, limit=5000)
        rows = result['records']
        assert len(rows) > 70
        assert result['pagination']['total_rows'] == len(rows)
        assert result['pagination']['has_more'] is False
        assert all(r['deal_type'] == kind for r in rows)
        assert all(r['quantity'] and r['price'] for r in rows)
        assert all('csv=true' in r['source_url'] for r in rows)
        symbol = 'ABH' if kind == 'bulk' else 'ADANIGREEN'
        filtered = await server.get_bulk_block_deals(
            start, end, symbol=symbol.lower(), deal_type=kind, limit=5000)
        expected = [row['raw'] for row in rows if row['symbol'] == symbol]
        assert expected
        assert [row['raw'] for row in filtered['records']] == expected

    asyncio.run(exercise())


def test_live_short_selling_download_exceeds_json_preview_limit():
    async def exercise():
        result = await server.get_short_selling_data('01-09-2026', '24-09-2026', limit=5000)
        rows = result['records']
        assert len(rows) > 70
        assert result['pagination']['total_rows'] == len(rows)
        assert result['pagination']['has_more'] is False
        assert all('csv=true' in row['source_url'] for row in rows)
        assert all('2026-09-01' <= row['trade_date'] <= '2026-09-24' for row in rows)
        filtered = await server.get_short_selling_data(
            '01-09-2026', '24-09-2026', symbol='reliance', limit=5000)
        expected = [row['raw'] for row in rows if row['symbol'] == 'RELIANCE']
        assert expected
        assert [row['raw'] for row in filtered['records']] == expected

    asyncio.run(exercise())
