"""Contract tests using captured NSE responses plus failure/edge-case payloads."""
import asyncio
import csv
import io
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest

from nse_data_mcp import corporate as corp

FIXTURES = Path(__file__).parent / 'fixtures' / 'nse'


def fixture(name):
    return json.loads((FIXTURES / (name + '.json')).read_text())


def short_csv(rows):
    """Match NSE's download headers, BOM, quoting and original string values."""
    stream = io.StringIO(newline='')
    writer = csv.writer(stream, quoting=csv.QUOTE_ALL)
    writer.writerow(['Date ', 'Symbol ', 'Security Name ', 'Quantity '])
    writer.writerows(rows)
    return '\ufeff' + stream.getvalue()


def deal_csv(rows):
    stream = io.StringIO(newline='')
    writer = csv.writer(stream, quoting=csv.QUOTE_ALL)
    writer.writerow(['Date ', 'Symbol ', 'Security Name ', 'Client Name ', 'Buy / Sell ',
                     'Quantity Traded ', 'Trade Price / Wght. Avg. Price ', 'Remarks '])
    writer.writerows(rows)
    return '\ufeff' + stream.getvalue()


@pytest.fixture
def feed(monkeypatch):
    calls = []
    mapping = {
        'corporates-financial-results': 'financial_legacy',
        'integrated-filing-results': 'financial_integrated',
        'corporate-share-holdings-master': 'shareholding',
        'corporates-corporateActions': 'actions',
        'corporate-announcements': 'announcements',
    }

    async def response(landing, url):
        parts = urlsplit(url)
        query = parse_qs(parts.query)
        calls.append((parts.path, query))
        endpoint = parts.path.rsplit('/', 1)[-1]
        if endpoint == 'bulk-block-short-deals':
            if query['optionType'] == ['short_selling']:
                assert query.get('csv') == ['true']
                return httpx2.Response(200, text=(FIXTURES / 'short.csv').read_text(),
                                       headers={'Content-Type': 'text/csv'})
            assert query.get('csv') == ['true']
            kind = query['optionType'][0].split('_')[0]
            return httpx2.Response(200, content=(FIXTURES / (kind + '.csv')).read_bytes(),
                                   headers={'Content-Type': 'text/csv'})
        elif endpoint == 'corporates-financial-results' and query['period'] != ['Quarterly']:
            data = []
        else:
            data = fixture(mapping[endpoint])
        return httpx2.Response(200, json=data)

    monkeypatch.setattr(corp.nse, '_get_nse_response', response)
    return calls


def test_financial_results_route_by_filing_date_and_preserve_source(feed):
    legacy = asyncio.run(corp.get_financial_results('01-07-2024', '31-07-2024', 'infy'))
    integrated = asyncio.run(corp.get_financial_results('01-07-2026', '31-07-2026', 'infy'))
    assert len(legacy) == len(integrated) == 2  # governance is excluded
    assert {r['basis'] for r in legacy.records} == {'standalone', 'consolidated'}
    assert {r['basis'] for r in integrated.records} == {'standalone', 'consolidated'}
    assert {r['source_feed'] for r in legacy.records} == {'legacy'}
    assert {r['source_feed'] for r in integrated.records} == {'integrated'}
    latest = integrated.records[0]
    assert latest['period_end'] == '2026-06-30'
    assert latest['published_at'].endswith('+05:30')
    assert latest['revision'] == 'Original'
    assert latest['period'] is None  # never guess from the quarter-end date
    assert len(latest['documents']) == 2  # no /corporate/null attachment
    assert latest['raw']['seq_Id'] == latest['filing_id']
    assert {q['period'][0] for path, q in feed if 'corporates-financial-results' in path} == {
        'Quarterly', 'Half-Yearly', 'Annual', 'Others'
    }
    assert len([path for path, _ in feed if 'integrated-filing-results' in path]) == 1


def test_financial_results_split_at_integrated_cutover(monkeypatch):
    calls = []

    async def fetch(kind, endpoint, start, end, symbol, *, paged=False, extra=None):
        calls.append((endpoint, start, end, symbol, paged, extra))
        return []

    monkeypatch.setattr(corp, '_fetch', fetch)
    asyncio.run(corp.get_financial_results('31-03-2025', '01-04-2025', 'infy'))
    assert calls[:4] == [
        ('corporates-financial-results', '31-03-2025', '31-03-2025', 'INFY', False, {'period': p})
        for p in ('Quarterly', 'Half-Yearly', 'Annual', 'Others')
    ]
    assert calls[4:] == [
        ('integrated-filing-results', '01-04-2025', '01-04-2025', 'INFY', True, {})
    ]


@pytest.mark.parametrize(('period', 'expected_rows'), [
    ('Quarterly', 2),
    ('Annual', 0),
])
def test_specific_financial_period_filters_legacy_rows(
    feed, period, expected_rows
):
    table = asyncio.run(corp.get_financial_results(
        '01-07-2024', '31-07-2024', 'INFY', period=period
    ))

    assert len(table) == expected_rows
    assert all(row['source_feed'] == 'legacy' for row in table.records)
    assert all(row['period'] == period for row in table.records)
    assert not any('integrated-filing-results' in path for path, _ in feed)


@pytest.mark.parametrize('start,end', [
    ('01-07-2026', '31-07-2026'),
    ('31-03-2025', '01-04-2025'),
])
def test_specific_financial_period_rejects_integrated_dates_before_network(feed, start, end):
    with pytest.raises(ValueError, match='period-specific filtering is unavailable'):
        asyncio.run(corp.get_financial_results(start, end, 'INFY', period='Quarterly'))
    assert not feed


@pytest.mark.parametrize('basis', ['consolidated', 'standalone'])
@pytest.mark.parametrize('year', [2024, 2026])
def test_financial_basis_filter(feed, basis, year):
    rows = asyncio.run(corp.get_financial_results(
        f'01-07-{year}', f'31-07-{year}', 'INFY', basis=basis)).records
    assert len(rows) == 1
    assert all(row['basis'] == basis for row in rows)


def test_financial_results_reject_missing_symbol_before_network(feed):
    with pytest.raises(ValueError, match='symbol is required'):
        asyncio.run(corp.get_financial_results('01-07-2026', '31-07-2026', None))
    assert not feed


def test_ownership_summary_preserves_missing_and_zero(feed, monkeypatch):
    row = fixture('shareholding')[0]
    row.update(pr_and_prgrp='0', public_val=None, employeeTrusts='-')

    async def response(*args):
        return httpx2.Response(200, json=[row])

    monkeypatch.setattr(corp.nse, '_get_nse_response', response)
    result = asyncio.run(corp.get_shareholding_patterns('01-07-2026', '31-07-2026', 'INFY')).records[0]
    assert result['promoter_percent'] == 0
    assert result['public_percent'] is None
    assert result['employee_trust_percent'] is None
    assert result['as_of_date'] == '2026-06-30'
    assert result['filing_id'] == '211349'
    assert result['documents'][0]['kind'] == 'xbrl'


@pytest.mark.parametrize('start,end', [
    ('01-07-2024', '01-07-2026'),
    ('29-02-2024', '28-02-2026'),
])
def test_shareholding_accepts_two_calendar_years_without_network(monkeypatch, start, end):
    calls = []

    async def fetch(*args):
        calls.append(args)
        return []

    monkeypatch.setattr(corp, '_fetch', fetch)
    table = asyncio.run(corp.get_shareholding_patterns(start, end, 'infy'))
    assert table.records == []
    assert calls == [('shareholding_patterns', 'corporate-share-holdings-master',
                      start, end, 'INFY')]


@pytest.mark.parametrize('start,end', [
    ('01-07-2024', '02-07-2026'),
    ('29-02-2024', '01-03-2026'),
])
def test_shareholding_rejects_more_than_two_calendar_years_before_network(feed, start, end):
    with pytest.raises(ValueError, match='two calendar years'):
        asyncio.run(corp.get_shareholding_patterns(start, end, 'INFY'))
    assert not feed


def test_shareholding_rejects_missing_symbol_before_network(feed):
    with pytest.raises(ValueError, match='symbol is required'):
        asyncio.run(corp.get_shareholding_patterns('01-07-2026', '31-07-2026', None))
    assert not feed


def test_corporate_actions_dates_and_terms(feed):
    row = asyncio.run(corp.get_corporate_actions('01-06-2026', '30-06-2026', 'INFY')).records[0]
    assert row['purpose'] == 'Dividend - Rs 25 Per Share'
    assert row['ex_date'] == row['record_date'] == '2026-06-10'
    assert row['face_value'] == 5
    assert row['book_closure_end'] is None


def test_corporate_actions_reject_missing_symbol_before_network(feed):
    with pytest.raises(ValueError, match='symbol is required'):
        asyncio.run(corp.get_corporate_actions('01-06-2026', '30-06-2026', None))
    assert not feed


def test_transcript_discovery_uses_description_and_filename(feed):
    rows = asyncio.run(corp.get_corporate_announcements(
        '01-07-2026', '31-07-2026', 'INFY', document_type='earnings_call_transcript')).records
    assert len(rows) == 1
    assert rows[0]['subject'] == 'Updates'  # subject alone would miss this
    assert rows[0]['filing_id'] == '106714324'
    assert rows[0]['published_at'] == '2026-07-28T20:24:54+05:30'
    assert rows[0]['submitted_at'] == '2026-07-28T20:24:53+05:30'
    assert rows[0]['documents'][0]['url'].endswith('.pdf')
    assert len(asyncio.run(corp.get_corporate_announcements(
        '01-07-2026', '31-07-2026', 'INFY', subject='newspaper')).records) == 1


@pytest.mark.parametrize('start,end', [
    ('01-07-2025', '01-07-2026'),
    ('29-02-2024', '28-02-2025'),
])
def test_announcements_accept_one_calendar_year_without_network(monkeypatch, start, end):
    calls = []

    async def fetch(*args):
        calls.append(args)
        return []

    monkeypatch.setattr(corp, '_fetch', fetch)
    table = asyncio.run(corp.get_corporate_announcements(start, end, 'infy'))
    assert table.records == []
    assert calls == [('corporate_announcements', 'corporate-announcements', start, end, 'INFY')]


@pytest.mark.parametrize('start,end', [
    ('01-07-2025', '02-07-2026'),
    ('29-02-2024', '01-03-2025'),
])
def test_announcements_reject_more_than_one_calendar_year_before_network(feed, start, end):
    with pytest.raises(ValueError, match='one calendar year'):
        asyncio.run(corp.get_corporate_announcements(start, end, 'INFY'))
    assert not feed


def test_announcements_reject_missing_symbol_before_network(feed):
    with pytest.raises(ValueError, match='symbol is required'):
        asyncio.run(corp.get_corporate_announcements('01-07-2026', '31-07-2026', None))
    assert not feed


@pytest.mark.parametrize('row,expected', [
    ({'desc': 'Analyst/Investor Meet - Intimation', 'attchmntText': 'Conference call scheduled for tomorrow'}, False),
    ({'desc': 'Audio recording of earnings call'}, False),
    ({'desc': 'Transcript of earnings call'}, True),
    ({'attchmntFile': 'https://nsearchives.nseindia.com/corporate/Investor_call_transcipt_signed.pdf'}, True),
    ({'attchmntFile': 'https://nsearchives.nseindia.com/corporate/Q2_Transcript.pdf'}, True),
    ({'desc': 'Transcript of Annual General Meeting'}, False),
    ({'desc': 'AGM Transcript'}, False),
    ({'attchmntText': None, 'desc': None}, False),
])
def test_transcript_classification(row, expected):
    assert corp._is_transcript(row) is expected


@pytest.mark.parametrize('kind,count', [('bulk', 3), ('block', 3), ('all', 6)])
def test_bulk_block_current_endpoint_and_distinct_sides(feed, kind, count):
    table = asyncio.run(corp.get_bulk_block_deals('01-08-2026', '31-08-2026', deal_type=kind))
    assert len(table) == count
    assert all(r['trade_date'].startswith('2026-') for r in table.records)
    assert all(isinstance(r['quantity'], int) for r in table.records)
    assert {r['side'] for r in table.records} == {'BUY', 'SELL'}
    assert all('historicalOR/bulk-block-short-deals' in path for path, _ in feed)
    assert all('from' in q and 'from_date' not in q for _, q in feed)
    assert all(q['csv'] == ['true'] for _, q in feed)
    assert all(r['raw']['Buy / Sell'] == r['side'] for r in table.records)
    if kind == 'block':
        adani = next(r for r in table.records if r['symbol'] == 'ADANIGREEN')
        assert adani['quantity'] == 17000000
        assert adani['price'] == 1400
        assert adani['raw']['Quantity Traded'] == '1,70,00,000'
        assert adani['remarks'] is None


@pytest.mark.parametrize('kind', ['bulk', 'block', 'all'])
def test_deal_csv_keeps_full_download_sides_and_feed_identity(monkeypatch, kind):
    calls = []
    rows = [('17-SEP-2026', 'M&M', 'Company, Limited', f'Client {i}', 'BUY',
             '1,23,456', '1,234.50', '-') for i in range(135)]
    rows += [('17-SEP-2026', 'M&M', 'Company, Limited', 'Client 0', 'SELL',
              '1,23,456', '1,234.50', '-')]
    rows += [rows[0], ('17-SEP-2026', 'OTHER', 'Other', 'Other Client', 'BUY', '1', '2', '')]

    async def response(landing, url):
        query = parse_qs(urlsplit(url).query)
        calls.append(query)
        assert query['csv'] == ['true']
        assert query['symbol'] == ['M&M']
        return httpx2.Response(200, text=deal_csv(rows))

    monkeypatch.setattr(corp.nse, '_get_nse_response', response)
    table = asyncio.run(corp.get_bulk_block_deals(
        '17-09-2026', '17-09-2026', ' m&m ', deal_type=kind))
    assert len(table) == (272 if kind == 'all' else 136)
    assert {r['side'] for r in table.records} == {'BUY', 'SELL'}
    assert all(r['quantity'] == 123456 and r['price'] == 1234.5 for r in table.records)
    assert all(r['symbol'] == 'M&M' and r['remarks'] is None for r in table.records)
    expected = {'bulk', 'block'} if kind == 'all' else {kind}
    assert {r['deal_type'] for r in table.records} == expected
    assert {q['optionType'][0] for q in calls} == {k + '_deals' for k in expected}


@pytest.mark.parametrize('kind', ['bulk', 'block'])
def test_deal_csv_chunks_and_empty_downloads(monkeypatch, kind):
    calls = []

    async def response(landing, url):
        query = parse_qs(urlsplit(url).query)
        calls.append(query)
        rows = [] if query['from'] == ['01-08-2026'] else [
            ('01-JUL-2026', 'INFY', 'Infosys', 'Client', 'BUY', '0', '-', '')]
        return httpx2.Response(200, text=deal_csv(rows).rstrip('\r\n'))

    monkeypatch.setattr(corp.nse, '_get_nse_response', response)
    table = asyncio.run(corp.get_bulk_block_deals('01-07-2026', '01-08-2026', deal_type=kind))
    assert len(table) == 1
    assert table.records[0]['quantity'] == 0 and table.records[0]['price'] is None
    assert [(q['from'][0], q['to'][0]) for q in calls] == [
        ('01-07-2026', '31-07-2026'), ('01-08-2026', '01-08-2026')]
    empty = asyncio.run(corp.get_bulk_block_deals('01-08-2026', '01-08-2026', deal_type=kind))
    assert empty.records == [] and empty.columns == table.columns


@pytest.mark.parametrize('kind', ['bulk', 'block'])
@pytest.mark.parametrize('payload', [
    '<html>Access Denied</html>', '{"data": []}', short_csv([]),
    deal_csv([('17-SEP-2026', 'INFY')]),
    deal_csv([]) + '"unterminated',
    deal_csv([('invalid', 'INFY', 'Infosys', 'Client', 'BUY', '1', '2', '')]),
    deal_csv([('17-SEP-2026', 'INFY', 'Infosys', 'Client', 'BUY', '1.5', '2', '')]),
    deal_csv([('17-SEP-2026', 'INFY', 'Infosys', 'Client', 'BUY', '1', 'NaN', '')]),
])
def test_deal_csv_rejects_malformed_downloads(monkeypatch, kind, payload):
    async def response(*args):
        return httpx2.Response(200, text=payload)

    monkeypatch.setattr(corp.nse, '_get_nse_response', response)
    with pytest.raises((RuntimeError, ValueError)):
        asyncio.run(corp.get_bulk_block_deals('17-09-2026', '17-09-2026', deal_type=kind))


def test_short_selling_uses_full_csv_download_and_filters_symbol(feed):
    table = asyncio.run(corp.get_short_selling_data('17-09-2026', '17-09-2026', 'reliance'))
    assert len(table) == 1
    record = table.records[0]
    assert record['trade_date'] == '2026-09-17'
    assert record['symbol'] == 'RELIANCE'
    assert record['company_name'] == 'RELIANCE INDUSTRIES LTD'
    assert record['short_sold_quantity'] == 19229
    assert record['raw'] == {'Date': '17-SEP-2026', 'Symbol': 'RELIANCE',
                             'Security Name': 'RELIANCE INDUSTRIES LTD', 'Quantity': '19,229'}
    assert parse_qs(urlsplit(record['source_url']).query)['csv'] == ['true']
    assert feed[0][1]['optionType'] == ['short_selling']
    assert feed[0][1]['csv'] == ['true']
    assert feed[0][1]['symbol'] == ['RELIANCE']


def test_short_selling_reads_beyond_preview_and_filters_encoded_symbol(monkeypatch):
    calls = []
    rows = [('17-SEP-2026', f'STOCK{i:03}', f'Company {i}', '1') for i in range(135)]
    rows.append(('17-SEP-2026', 'M&M', 'Company, with comma', '1,234'))

    async def response(landing, url):
        calls.append(parse_qs(urlsplit(url).query))
        return httpx2.Response(200, text=short_csv(rows))

    monkeypatch.setattr(corp.nse, '_get_nse_response', response)
    table = asyncio.run(corp.get_short_selling_data('17-09-2026', '17-09-2026'))
    assert len(table) == 136
    filtered = asyncio.run(corp.get_short_selling_data('17-09-2026', '17-09-2026', ' m&m '))
    assert len(filtered) == 1
    assert filtered.records[0]['symbol'] == 'M&M'
    assert filtered.records[0]['company_name'] == 'Company, with comma'
    assert filtered.records[0]['short_sold_quantity'] == 1234
    assert calls[0] == {'from': ['17-09-2026'], 'to': ['17-09-2026'],
                        'optionType': ['short_selling'], 'csv': ['true']}
    assert calls[1]['symbol'] == ['M&M']


def test_short_selling_chunks_and_deduplicates_without_losing_trade_dates(monkeypatch):
    calls = []

    async def response(landing, url):
        query = parse_qs(urlsplit(url).query)
        calls.append(query)
        date = {'01-07-2026': '01-JUL-2026', '01-08-2026': '01-AUG-2026',
                '01-09-2026': '01-SEP-2026'}[query['from'][0]]
        row = (date, 'RELIANCE', 'RELIANCE INDUSTRIES LTD', '1,234')
        return httpx2.Response(200, text=short_csv([row, row]))

    monkeypatch.setattr(corp.nse, '_get_nse_response', response)
    table = asyncio.run(corp.get_short_selling_data('01-07-2026', '01-09-2026'))
    assert [row['trade_date'] for row in table.records] == [
        '2026-09-01', '2026-08-01', '2026-07-01']
    assert [(query['from'][0], query['to'][0]) for query in calls] == [
        ('01-07-2026', '31-07-2026'), ('01-08-2026', '31-08-2026'),
        ('01-09-2026', '01-09-2026')]
    assert all(query['csv'] == ['true'] for query in calls)
    assert [parse_qs(urlsplit(row['source_url']).query)['from'][0] for row in table.records] == [
        '01-09-2026', '01-08-2026', '01-07-2026']


@pytest.mark.parametrize('quantity,expected', [
    ('0', 0), ('-', None), ('', None), ('NA', None), ('N/A', None), ('1,234', 1234)])
def test_short_selling_csv_preserves_missing_and_zero(monkeypatch, quantity, expected):
    async def response(*args):
        return httpx2.Response(200, text=short_csv([
            ('17-SEP-2026', 'RELIANCE', 'RELIANCE INDUSTRIES LTD', quantity)]))

    monkeypatch.setattr(corp.nse, '_get_nse_response', response)
    row = asyncio.run(corp.get_short_selling_data('17-09-2026', '17-09-2026')).records[0]
    assert row['short_sold_quantity'] == expected
    assert row['raw']['Quantity'] == quantity


@pytest.mark.parametrize('suffix', ['', '\r\n\r\n'])
def test_short_selling_header_only_csv_is_empty_with_stable_columns(monkeypatch, suffix):
    async def response(*args):
        return httpx2.Response(200, text=short_csv([]).rstrip('\r\n') + suffix)

    monkeypatch.setattr(corp.nse, '_get_nse_response', response)
    table = asyncio.run(corp.get_short_selling_data('17-09-2026', '17-09-2026'))
    assert table.records == []
    assert table.columns == ['trade_date', 'short_sold_quantity', 'symbol',
                             'company_name', 'source_url', 'raw']


@pytest.mark.parametrize('payload', [
    '',
    '<html>Access denied</html>',
    '{"data": []}',
    'Date,Symbol,Quantity\n17-SEP-2026,RELIANCE,19229\n',
    'Date,Symbol,Security Name,Quantity, Quantity \n17-SEP-2026,RELIANCE,Reliance,1,2\n',
    short_csv([('17-SEP-2026', 'RELIANCE', 'Reliance')]),
    short_csv([('17-SEP-2026', 'RELIANCE', 'Reliance', '1', 'unexpected')]),
    short_csv([('', 'RELIANCE', 'Reliance', '1')]),
    short_csv([('17-SEP-2026', '', 'Reliance', '1')]),
    short_csv([('invalid-date', 'RELIANCE', 'Reliance', '1')]),
    short_csv([('17-SEP-2026', 'RELIANCE', 'Reliance', 'invalid-number')]),
    short_csv([('17-SEP-2026', 'RELIANCE', 'Reliance', 'NaN')]),
    short_csv([('17-SEP-2026', 'RELIANCE', 'Reliance', 'inf')]),
    short_csv([('17-SEP-2026', 'RELIANCE', 'Reliance', '-1')]),
    short_csv([('17-SEP-2026', 'RELIANCE', 'Reliance', '1.5')]),
    short_csv([]) + '"17-SEP-2026","RELIANCE","Reliance","1\n',
])
def test_short_selling_malformed_csv_never_becomes_empty_success(monkeypatch, payload):
    async def response(*args):
        return httpx2.Response(200, text=payload)

    monkeypatch.setattr(corp.nse, '_get_nse_response', response)
    with pytest.raises((RuntimeError, ValueError)):
        asyncio.run(corp.get_short_selling_data('17-09-2026', '17-09-2026'))


def test_symbol_encoding_and_local_filter(feed):
    assert not asyncio.run(corp.get_corporate_actions('01-06-2026', '30-06-2026', 'm&m')).records
    assert feed[0][1]['symbol'] == ['M&M']


@pytest.mark.parametrize('payload', [{}, {'error': 'denied'}, {'data': None}, [1], {'data': {}}])
def test_bad_schema_never_becomes_empty_success(monkeypatch, payload):
    async def response(*args):
        return httpx2.Response(200, json=payload)
    monkeypatch.setattr(corp.nse, '_get_nse_response', response)
    with pytest.raises(RuntimeError, match='Unexpected NSE'):
        asyncio.run(corp.get_corporate_actions('01-01-2026', '02-01-2026', 'INFY'))


def test_bad_json_and_missing_symbol_fail(monkeypatch):
    async def response(*args):
        return httpx2.Response(200, text='<html>denied</html>')
    monkeypatch.setattr(corp.nse, '_get_nse_response', response)
    with pytest.raises(RuntimeError, match='invalid JSON'):
        asyncio.run(corp.get_corporate_actions('01-01-2026', '02-01-2026', 'INFY'))
    async def response(*args):
        return httpx2.Response(200, json=[{'unexpected': 1}])
    monkeypatch.setattr(corp.nse, '_get_nse_response', response)
    with pytest.raises(RuntimeError, match='missing symbol'):
        asyncio.run(corp.get_corporate_actions('01-01-2026', '02-01-2026', 'INFY'))


def test_empty_response_has_stable_columns(monkeypatch):
    async def response(*args):
        return httpx2.Response(200, json=[])
    monkeypatch.setattr(corp.nse, '_get_nse_response', response)
    table = asyncio.run(corp.get_corporate_actions('01-01-2026', '02-01-2026', 'INFY'))
    assert table.records == []
    assert 'ex_date' in table.columns


def test_month_windows_deduplicate_exact_rows_but_keep_revisions(monkeypatch):
    calls = []
    row = fixture('shareholding')[0]
    revised = {**row, 'revisedDate': '16-Jul-2026', 'revisedStatus': 'Revised'}
    async def response(landing, url):
        calls.append(parse_qs(urlsplit(url).query))
        return httpx2.Response(200, json=[row, revised])
    monkeypatch.setattr(corp.nse, '_get_nse_response', response)
    table = asyncio.run(corp.get_shareholding_patterns('01-07-2026', '01-09-2026', 'INFY'))
    assert len(table) == 2
    assert [(q['from_date'][0], q['to_date'][0]) for q in calls] == [
        ('01-07-2026', '31-07-2026'), ('01-08-2026', '31-08-2026'), ('01-09-2026', '01-09-2026')]


def test_upstream_pagination_fetches_every_page(monkeypatch):
    calls = []
    async def response(landing, url):
        page = int(parse_qs(urlsplit(url).query)['page'][0])
        calls.append(page)
        return httpx2.Response(200, json={'data': [{'symbol': 'INFY', 'id': page}], 'totalCount': 3})
    monkeypatch.setattr(corp.nse, '_get_nse_response', response)
    rows = asyncio.run(corp._fetch('financial_results', 'integrated-filing-results',
                                  '01-07-2026', '31-07-2026', None, paged=True))
    assert len(rows) == 3
    assert calls == [1, 2, 3]


@pytest.mark.parametrize('mode', ['repeated', 'empty', 'missing_count', 'invalid_count', 'negative_count'])
def test_incomplete_pagination_raises(monkeypatch, mode):
    async def response(landing, url):
        page = int(parse_qs(urlsplit(url).query)['page'][0])
        payload = {'data': [{'symbol': 'INFY'}], 'totalCount': 2}
        if mode == 'empty' and page == 2:
            payload['data'] = []
        if mode == 'missing_count':
            del payload['totalCount']
        if mode == 'invalid_count':
            payload['totalCount'] = 'x'
        if mode == 'negative_count':
            payload['totalCount'] = -1
        return httpx2.Response(200, json=payload)
    monkeypatch.setattr(corp.nse, '_get_nse_response', response)
    with pytest.raises(RuntimeError):
        asyncio.run(corp._fetch('financial_results', 'integrated-filing-results',
                               '01-07-2026', '31-07-2026', None, paged=True))


@pytest.mark.parametrize('name,kwargs', [
    ('get_financial_results', {'symbol': 'INFY', 'basis': 'bad'}),
    ('get_financial_results', {'symbol': 'INFY', 'period': 'bad'}),
    ('get_financial_results', {'symbol': ' '}),
    ('get_corporate_actions', {'symbol': ' '}),
    ('get_bulk_block_deals', {'deal_type': 'bad'}),
    ('get_short_selling_data', {'symbol': ' '}),
    ('get_corporate_announcements', {'symbol': 'INFY', 'document_type': 'bad'}),
    ('get_corporate_announcements', {'symbol': ' '}),
    ('get_shareholding_patterns', {'symbol': ' '}),
])
def test_invalid_filters_fail_before_network(feed, name, kwargs):
    with pytest.raises(ValueError):
        asyncio.run(getattr(corp, name)('01-07-2026', '31-07-2026', **kwargs))
    assert not feed


@pytest.mark.parametrize('name', ['get_financial_results', 'get_shareholding_patterns',
                                  'get_corporate_actions', 'get_corporate_announcements', 'get_bulk_block_deals',
                                  'get_short_selling_data'])
@pytest.mark.parametrize('start,end', [('31-07-2026', '01-07-2026'), ('invalid', '31-07-2026')])
def test_invalid_dates_fail_before_network(feed, name, start, end):
    with pytest.raises(ValueError):
        kwargs = {'symbol': 'INFY'} if name in ('get_financial_results', 'get_shareholding_patterns',
                                               'get_corporate_actions', 'get_corporate_announcements') else {}
        asyncio.run(getattr(corp, name)(start, end, **kwargs))
    assert not feed


@pytest.mark.parametrize(('start', 'end', 'expected'), [
    ('31-12-9999', '31-12-9999', [('31-12-9999', '31-12-9999')]),
    ('01-12-9999', '31-12-9999', [('01-12-9999', '31-12-9999')]),
    ('30-11-9999', '31-12-9999', [
        ('30-11-9999', '30-12-9999'),
        ('31-12-9999', '31-12-9999'),
    ]),
])
def test_chunks_handle_maximum_valid_date(start, end, expected):
    assert list(corp._chunks(start, end)) == expected
