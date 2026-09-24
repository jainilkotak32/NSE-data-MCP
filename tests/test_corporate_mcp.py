"""Exercise every new tool through the MCP protocol, including CSV export."""
import asyncio
import csv
import inspect
import io
import json

import httpx2
import pytest
from mcp import Client as SDKClient

from nse_data_mcp import documents, server
from test_corporate import feed, short_csv, deal_csv  # register the captured-response fixture
from test_documents import XBRL, URL, pdf_bytes, unit_measures

DATES = {'start_date_str': '01-07-2026', 'end_date_str': '31-07-2026'}
SHORT_DATES = {'start_date_str': '17-09-2026', 'end_date_str': '17-09-2026'}
TOOLS = [
    ('get_financial_results', {**DATES, 'symbol': 'INFY', 'basis': 'consolidated'}, 'records'),
    ('get_shareholding_patterns', {**DATES, 'symbol': 'INFY'}, 'records'),
    ('get_corporate_actions', {**DATES, 'symbol': 'INFY'}, 'records'),
    ('get_corporate_announcements', {**DATES, 'symbol': 'INFY'}, 'records'),
    ('get_earnings_call_transcripts', {**DATES, 'symbol': 'INFY'}, 'records'),
    ('get_bulk_block_deals', DATES, 'records'),
    ('get_short_selling_data', SHORT_DATES, 'records'),
    ('get_corporate_document', {'url': URL}, 'text'),
    ('get_filing_facts', {'url': URL.replace('.pdf', '.xml'), 'concept': 'Pledged'}, 'records'),
]


def test_all_new_tools_via_mcp_protocol(feed, monkeypatch):
    def handler(request):
        return httpx2.Response(200, content=XBRL if request.url.path.endswith('.xml') else pdf_bytes())
    monkeypatch.setattr(documents.nse, '_create_http_client', lambda: httpx2.AsyncClient(
        transport=httpx2.MockTransport(handler)))

    async def exercise():
        async with SDKClient(server.build_server()) as client:
            for name, params, field in TOOLS:
                response = await client.call_tool(name, params)
                assert response.is_error is False, (name, response)
                content = response.structured_content
                assert content[field], (name, content)
                if field == 'records':
                    assert content['pagination']['returned_rows'] > 0
    asyncio.run(exercise())


def test_all_registered_tools_are_async():
    assert all(inspect.iscoroutinefunction(func) for func in server._TOOLS)


@pytest.mark.parametrize('output_format', ['json', 'csv'])
def test_short_selling_full_download_survives_mcp_pagination(monkeypatch, output_format):
    rows = [('17-SEP-2026', f'STOCK{i:03}', f'Company {i}', '1,234') for i in range(135)]

    async def response(landing, url):
        assert 'csv=true' in url
        return httpx2.Response(200, text=short_csv(rows))

    monkeypatch.setattr(server.nse, '_get_nse_response', response)

    async def exercise():
        async with SDKClient(server.build_server()) as client:
            first = await client.call_tool('get_short_selling_data', {
                **SHORT_DATES, 'output_format': output_format})
            assert first.is_error is False
            first = first.structured_content
            assert first['pagination'] == {
                'offset': 0, 'limit': 100, 'returned_rows': 100, 'total_rows': 135,
                'has_more': True, 'next_offset': 100}
            second = await client.call_tool('get_short_selling_data', {
                **SHORT_DATES, 'offset': first['pagination']['next_offset'],
                'output_format': output_format})
            assert second.is_error is False
            second = second.structured_content
            assert second['pagination'] == {
                'offset': 100, 'limit': 100, 'returned_rows': 35, 'total_rows': 135,
                'has_more': False, 'next_offset': None}
            if output_format == 'csv':
                records = list(csv.DictReader(io.StringIO(first['csv']))) + list(
                    csv.DictReader(io.StringIO(second['csv'])))
                for row in records:
                    row['raw'] = json.loads(row['raw'])
                    row['short_sold_quantity'] = int(row['short_sold_quantity'])
            else:
                records = first['records'] + second['records']
            assert len(records) == 135
            assert {row['symbol'] for row in records} == {row[1] for row in rows}
            assert all(row['trade_date'] == '2026-09-17' for row in records)
            assert all(row['short_sold_quantity'] == 1234 for row in records)
            assert all(row['raw']['Quantity'] == '1,234' for row in records)
            assert all('csv=true' in row['source_url'] for row in records)

    asyncio.run(exercise())


def test_short_selling_invalid_download_is_an_mcp_error(monkeypatch):
    async def response(*args):
        return httpx2.Response(200, text='<html>Access denied</html>')

    monkeypatch.setattr(server.nse, '_get_nse_response', response)

    async def exercise():
        async with SDKClient(server.build_server()) as client:
            result = await client.call_tool('get_short_selling_data', SHORT_DATES)
            assert result.is_error is True

    asyncio.run(exercise())


@pytest.mark.parametrize('kind', ['bulk', 'block', 'all'])
@pytest.mark.parametrize('output_format', ['json', 'csv'])
def test_bulk_block_full_download_survives_mcp_pagination(monkeypatch, kind, output_format):
    rows = [('17-SEP-2026', f'STOCK{i:03}', 'Company', f'Client {i}',
             'BUY' if i % 2 else 'SELL', '1,23,456', '1,234.50', '-') for i in range(135)]

    async def response(landing, url):
        assert 'csv=true' in url
        return httpx2.Response(200, text=deal_csv(rows))

    monkeypatch.setattr(server.nse, '_get_nse_response', response)

    async def exercise():
        total = 270 if kind == 'all' else 135
        records, counts, offset = [], [], 0
        async with SDKClient(server.build_server()) as client:
            while True:
                result = await client.call_tool('get_bulk_block_deals', {
                    **SHORT_DATES, 'deal_type': kind, 'output_format': output_format, 'offset': offset})
                assert result.is_error is False
                result = result.structured_content
                page = result['pagination']
                assert page['total_rows'] == total
                counts.append(page['returned_rows'])
                if output_format == 'csv':
                    chunk = list(csv.DictReader(io.StringIO(result['csv'])))
                    for row in chunk:
                        row['raw'] = json.loads(row['raw'])
                        row['quantity'] = int(row['quantity'])
                        row['price'] = float(row['price'])
                else:
                    chunk = result['records']
                records.extend(chunk)
                if not page['has_more']:
                    assert page['next_offset'] is None
                    break
                assert page['next_offset'] > offset
                offset = page['next_offset']
        assert counts == ([100, 100, 70] if kind == 'all' else [100, 35])
        assert len({(r['deal_type'], r['symbol'], r['client_name'], r['side']) for r in records}) == total
        assert {r['deal_type'] for r in records} == ({'bulk', 'block'} if kind == 'all' else {kind})
        assert {r['side'] for r in records} == {'BUY', 'SELL'}
        assert all(r['quantity'] == 123456 and r['price'] == 1234.5 for r in records)
        assert all(r['raw']['Quantity Traded'] == '1,23,456' for r in records)
        assert all('csv=true' in r['source_url'] for r in records)

    asyncio.run(exercise())


@pytest.mark.parametrize('output_format', ['json', 'csv'])
def test_filing_namespace_details_survive_mcp_export(monkeypatch, output_format):
    monkeypatch.setattr(documents.nse, '_create_http_client', lambda: httpx2.AsyncClient(
        transport=httpx2.MockTransport(lambda request: httpx2.Response(200, content=XBRL))))

    async def exercise():
        async with SDKClient(server.build_server()) as client:
            response = await client.call_tool('get_filing_facts', {
                'url': URL.replace('.pdf', '.xml'), 'concept': 'Pledged',
                'output_format': output_format,
            })
            assert response.is_error is False
            result = response.structured_content
            if output_format == 'csv':
                row = next(csv.DictReader(io.StringIO(result['csv'])))
                row['dimensions'] = json.loads(row['dimensions'])
            else:
                row = result['records'][0]
            assert unit_measures(row['unit_xml']) == [
                ('http://www.xbrl.org/2003/instance', 'shares')]
            assert row['dimensions'][0]['qualified_dimension'] == '{urn:test}Holder'
            assert row['dimensions'][0]['value'] == 'Mutual fund'

    asyncio.run(exercise())


@pytest.mark.parametrize('name,params,field', [x for x in TOOLS if x[2] == 'records' and x[0] != 'get_filing_facts'])
def test_csv_export_contains_only_requested_page_and_roundtrips_nested_records(feed, name, params, field):
    response = asyncio.run(getattr(server, name)(**params, limit=1, output_format='csv'))
    assert 'records' not in response
    rows = list(csv.DictReader(io.StringIO(response['csv'])))
    assert len(rows) == 1
    assert isinstance(json.loads(rows[0]['raw']), dict)
    assert response['pagination']['returned_rows'] == 1
    assert response['metadata']['source_page'].startswith('https://www.nseindia.com/')
    if 'documents' in rows[0]:
        assert isinstance(json.loads(rows[0]['documents']), list)


@pytest.mark.parametrize('name,params,field', [x for x in TOOLS if x[2] == 'records'])
@pytest.mark.parametrize('options', [{'offset': -1}, {'limit': 0}, {'limit': 5001}, {'output_format': 'bad'}])
def test_invalid_export_arguments_fail_before_network(feed, name, params, field, options):
    with pytest.raises(ValueError):
        asyncio.run(getattr(server, name)(**params, **options))
    assert not feed


def test_pagination_empty_page_keeps_total(feed):
    result = asyncio.run(server.get_bulk_block_deals(**DATES, offset=100))
    assert result['records'] == []
    assert result['pagination']['total_rows'] == 6
    assert result['pagination']['next_offset'] is None


def test_mcp_returns_tool_error_for_invalid_filter(feed):
    async def exercise():
        async with SDKClient(server.build_server()) as client:
            response = await client.call_tool('get_bulk_block_deals', {**DATES, 'deal_type': 'wrong'})
            assert response.is_error is True
    asyncio.run(exercise())
    assert not feed


@pytest.mark.parametrize('name', ['get_financial_results', 'get_shareholding_patterns',
                                  'get_corporate_actions', 'get_corporate_announcements',
                                  'get_earnings_call_transcripts'])
def test_symbol_required_tools_via_mcp(feed, name):
    async def exercise():
        async with SDKClient(server.build_server()) as client:
            response = await client.call_tool(name, DATES)
            assert response.is_error is True

    asyncio.run(exercise())
    assert not feed


@pytest.mark.parametrize('name', ['get_corporate_announcements', 'get_earnings_call_transcripts'])
def test_announcement_tools_reject_more_than_one_year_before_network(feed, name):
    with pytest.raises(ValueError, match='one calendar year'):
        asyncio.run(getattr(server, name)(
            '01-07-2025', '02-07-2026', 'INFY'
        ))
    assert not feed
