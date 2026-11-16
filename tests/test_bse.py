"""Offline contract tests for the BSE server, using hand-built payloads.

See tests/fixtures/bse/README.md: these fixtures follow publicly documented
response shapes but were not captured from BSE.
"""
import asyncio
import copy
import csv
import inspect
import io
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest
from mcp import Client as SDKClient

from bse_data_mcp import corporate as corp
from bse_data_mcp import data_utils as bse
from bse_data_mcp import documents, server
from test_documents import XBRL, pdf_bytes

FIXTURES = Path(__file__).parent / 'fixtures' / 'bse'
ENDPOINT_FIXTURES = {
    'AnnSubCategoryGetData': 'announcements.json',
    'DefaultData': 'actions.json',
    'ListofScripData': 'securities.json',
    'Corpforthresults': 'result_calendar.json',
    'TabResults_PAR': 'results_snapshot.json',
    'GetMktData': 'sensex.json',
    'FillddlIndex': 'index_names.json',
    'StockPriceCSVDownload': 'stock_history.csv',
    'ProduceCSVForDate': 'index_history.csv',
}
DATES = ('01-07-2026', '31-07-2026')
PDF_URL = 'https://www.bseindia.com/xml-data/corpfiling/AttachLive/doc.pdf'


def run(coroutine):
    return asyncio.run(coroutine)


@pytest.fixture
def feed(monkeypatch):
    """Serve fixtures by endpoint; tests may override entries with text or callables."""
    calls = []
    overrides = {}

    async def response(url):
        parts = urlsplit(url)
        assert parts.scheme == 'https' and parts.hostname == 'api.bseindia.com'
        assert parts.path.startswith('/BseIndiaAPI/api/') and parts.path.endswith('/w')
        endpoint = parts.path.split('/')[-2]
        query = parse_qs(parts.query, keep_blank_values=True)
        calls.append((endpoint, query))
        body = overrides.get(endpoint)
        if callable(body):
            body = body(query)
        if body is None:
            body = (FIXTURES / ENDPOINT_FIXTURES[endpoint]).read_text()
        return httpx2.Response(200, text=body if isinstance(body, str) else json.dumps(body))

    monkeypatch.setattr(bse, '_get_bse_response', response)
    response.calls = calls
    response.overrides = overrides
    return response


# --- transport -------------------------------------------------------------

def test_requests_send_site_headers_and_retry(monkeypatch):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx2.Response(503) if len(seen) == 1 else httpx2.Response(200, text='[]')

    async def no_sleep(_):
        return None

    monkeypatch.setattr(bse.asyncio, 'sleep', no_sleep)
    monkeypatch.setattr(bse, '_create_http_client', lambda: httpx2.AsyncClient(
        transport=httpx2.MockTransport(handler)))
    payload, url = run(bse.get_json('ListofScripData', {'segment': 'Equity'}))
    assert payload == []
    assert url == 'https://api.bseindia.com/BseIndiaAPI/api/ListofScripData/w?segment=Equity'
    assert len(seen) == 2
    assert seen[-1].headers['referer'] == 'https://www.bseindia.com/'
    assert seen[-1].headers['origin'] == 'https://www.bseindia.com'


def test_persistent_http_failure_raises(monkeypatch):
    async def no_sleep(_):
        return None

    monkeypatch.setattr(bse.asyncio, 'sleep', no_sleep)
    monkeypatch.setattr(bse, '_create_http_client', lambda: httpx2.AsyncClient(
        transport=httpx2.MockTransport(lambda request: httpx2.Response(403))))
    with pytest.raises(RuntimeError, match='failed after 3 attempts'):
        run(bse.get_json('ListofScripData', {}))


def test_html_error_page_is_not_an_empty_result(feed):
    feed.overrides['DefaultData'] = '<html><body>Access Denied</body></html>'
    with pytest.raises(RuntimeError, match='invalid JSON'):
        run(corp.get_corporate_actions(*DATES))


@pytest.mark.parametrize('value', ['INFY', '', '50 0209', '1234567890', '５００２０９'])
def test_scrip_code_must_be_numeric(value):
    with pytest.raises(ValueError, match='scrip_code'):
        bse.scrip_code(value)


def test_scrip_code_accepts_integers():
    assert bse.scrip_code(500209) == '500209'
    assert bse.scrip_code(' 500209 ') == '500209'


# --- market data -----------------------------------------------------------

def test_equity_securities_keep_source_columns(feed):
    table = run(bse.get_equity_securities(group='a'))
    assert table.columns[:3] == ['SCRIP_CD', 'Scrip_Name', 'Status']
    assert [row['scrip_id'] for row in table.records] == ['ABB', 'INFY']
    endpoint, query = feed.calls[0]
    assert endpoint == 'ListofScripData'
    assert query['Group'] == ['A'] and query['segment'] == ['Equity'] and query['status'] == ['Active']


def test_equity_securities_reject_unexpected_shape(feed):
    feed.overrides['ListofScripData'] = [{'unexpected': 1}]
    with pytest.raises(RuntimeError, match='SCRIP_CD'):
        run(bse.get_equity_securities())
    feed.overrides['ListofScripData'] = {'error': 'x'}
    with pytest.raises(RuntimeError, match='list of records'):
        run(bse.get_equity_securities())


def test_stock_history_parses_types_and_sorts_ascending(feed):
    table = run(bse.get_stock_historical_price_volume_data('500209', '01-01-2024', '31-01-2024'))
    assert table.columns[0] == 'Date' and '' not in table.columns
    assert [row['Date'] for row in table.records] == [
        '2024-01-02T00:00:00.000', '2024-01-03T00:00:00.000']
    latest = table.records[1]
    assert latest['Close Price'] == 1558.35
    assert latest['No.of Shares'] == 123456 and isinstance(latest['No.of Shares'], int)
    # A blank cell is missing, not zero.
    assert table.records[0]['Deliverable Quantity'] is None
    assert table.records[0]['% Deli. Qty to Traded Qty'] is None
    endpoint, query = feed.calls[0]
    assert endpoint == 'StockPriceCSVDownload'
    assert query['Scode'] == ['500209']
    assert query['FDates'] == ['01/01/2024'] and query['TDates'] == ['31/01/2024']


def test_stock_history_splits_long_ranges(feed):
    run(bse.get_stock_historical_price_volume_data('500209', '01-01-2023', '15-02-2024'))
    assert [(q['FDates'][0], q['TDates'][0]) for _, q in feed.calls] == [
        ('01/01/2023', '31/12/2023'), ('01/01/2024', '15/02/2024')]


@pytest.mark.parametrize('body,match', [
    ('', 'no stock history data'),
    ('<html><body>Error</body></html>', 'CSV columns'),
    ('Date,Open Price,High Price,Low Price,Close Price\n1-January-2024,1,2\n', 'row width'),
])
def test_stock_history_rejects_bad_downloads(feed, body, match):
    feed.overrides['StockPriceCSVDownload'] = body
    with pytest.raises(RuntimeError, match=match):
        run(bse.get_stock_historical_price_volume_data('500209', '01-01-2024', '31-01-2024'))


def test_stock_history_validates_before_fetching(feed):
    with pytest.raises(ValueError):
        run(bse.get_stock_historical_price_volume_data('500209', '31-01-2024', '01-01-2024'))
    with pytest.raises(ValueError, match='scrip_code'):
        run(bse.get_stock_historical_price_volume_data('INFY', '01-01-2024', '31-01-2024'))
    assert feed.calls == []


def test_index_names_and_history(feed):
    names = run(bse.get_bse_index_names())
    assert names.records[0] == {'Indx_cd': '16', 'IndexName': 'SENSEX'}
    table = run(bse.get_bse_index_historical_data('SENSEX', '01-01-2024', '31-01-2024'))
    assert table.columns == ['Date', 'Open', 'High', 'Low', 'Close']
    assert [row['Date'] for row in table.records] == [
        '2024-01-01T00:00:00.000', '2024-01-02T00:00:00.000']
    assert table.records[1]['Close'] == 71892.48
    endpoint, query = feed.calls[-1]
    assert endpoint == 'ProduceCSVForDate'
    assert query == {'strIndex': ['SENSEX'], 'dtFromDate': ['01/01/2024'],
                     'dtToDate': ['31/01/2024'], 'period': ['D']}


def test_index_history_rejects_bad_input_and_empty_downloads(feed):
    with pytest.raises(ValueError, match='period'):
        run(bse.get_bse_index_historical_data('SENSEX', '01-01-2024', '31-01-2024', 'W'))
    with pytest.raises(ValueError, match='index_name'):
        run(bse.get_bse_index_historical_data(' ', '01-01-2024', '31-01-2024'))
    assert feed.calls == []
    feed.overrides['ProduceCSVForDate'] = ''
    with pytest.raises(RuntimeError, match='no index history data'):
        run(bse.get_bse_index_historical_data('NOPE', '01-01-2024', '31-01-2024'))
    feed.overrides['ProduceCSVForDate'] = 'Something,Else\n1,2\n'
    with pytest.raises(RuntimeError, match='CSV columns'):
        run(bse.get_bse_index_historical_data('SENSEX', '01-01-2024', '31-01-2024'))


def test_sensex_constituents_and_snapshot(feed):
    assert run(bse.get_sensex_stocks()) == {
        'INFOSYS LTD.': '500209', 'RELIANCE INDUSTRIES LTD.': '500325'}
    table = run(bse.get_sensex_stocks_ltp())
    first = table.records[0]
    assert first['SCRIP CODE'] == '500209' and first['SYMBOL'] == 'INFY'
    assert first['LTP'] == 1523.05 and first['%CHNG'] == 9.61
    assert table.records[1]['VALUE (Rs. Lakhs)'] is None
    assert set(first) == set(table.columns)
    assert feed.calls[0][1]['strfilter'] == ['S&P BSE SENSEX']


def test_sensex_rejects_empty_or_unexpected_payload(feed):
    for payload in ({'Table': []}, {'Table': [{'foo': 1}]}, {}):
        feed.overrides['GetMktData'] = payload
        with pytest.raises(RuntimeError):
            run(bse.get_sensex_stocks_ltp())


# --- corporate -------------------------------------------------------------

def test_announcements_normalise_records_and_documents(feed):
    table = run(corp.get_corporate_announcements(*DATES, '500209'))
    transcript, result = table.records  # newest first
    assert transcript['published_at'] == '2026-07-28T18:02:11.450000+05:30'
    assert transcript['submitted_at'] == '2026-07-28T18:02:05+05:30'
    assert transcript['document_type'] == 'earnings_call_transcript'
    assert transcript['subcategory'] == 'Earnings Call Transcript'
    assert transcript['documents'] == [{'kind': 'attachment', 'url': (
        'https://www.bseindia.com/xml-data/corpfiling/AttachLive/'
        '0a0a0a0a-1111-2222-3333-444444444444.pdf')}]
    assert result['document_type'] == 'announcement'
    assert result['documents'][0]['url'].startswith(
        'https://www.bseindia.com/xml-data/corpfiling/AttachHis/')
    assert result['raw']['FILESTATUS'] == 'N    '  # source record is untouched
    assert set(table.columns) == set(transcript)
    endpoint, query = feed.calls[0]
    assert endpoint == 'AnnSubCategoryGetData'
    assert query['strPrevDate'] == ['20260701'] and query['strToDate'] == ['20260731']
    assert query['strScrip'] == ['500209'] and query['strCat'] == ['-1']
    assert query['strType'] == ['C'] and query['pageno'] == ['1']


def test_announcement_filters(feed):
    transcripts = run(corp.get_corporate_announcements(
        *DATES, '500209', document_type='earnings_call_transcript'))
    assert [r['filing_id'][-1] for r in transcripts.records] == ['1']
    by_subject = run(corp.get_corporate_announcements(*DATES, '500209', subject='AUDITED'))
    assert [r['filing_id'][-1] for r in by_subject.records] == ['2']
    run(corp.get_financial_results(*DATES, '500209'))
    assert feed.calls[-1][1]['strCat'] == ['Result']


def test_announcement_attachment_in_dated_folder_and_media_link(feed):
    payload = json.loads((FIXTURES / 'announcements.json').read_text())
    payload['Table'][0].update(PDFFLAG=2, AUDIO_VIDEO_FILE='https://example.com/call.mp3',
                               ATTACHMENTNAME='a b.pdf')
    payload['Table'][1].update(ATTACHMENTNAME='', AUDIO_VIDEO_FILE='not a url')
    feed.overrides['AnnSubCategoryGetData'] = payload
    first, second = run(corp.get_corporate_announcements(*DATES, '500209')).records
    assert first['documents'] == [
        {'kind': 'attachment',
         'url': 'https://www.bseindia.com/xml-data/corpfiling/CorpAttachment/2026/7/a%20b.pdf'},
        {'kind': 'audio_video', 'url': 'https://example.com/call.mp3'}]
    assert second['documents'] == []


def test_announcements_follow_pages_and_drop_repeats(feed):
    base = json.loads((FIXTURES / 'announcements.json').read_text())['Table'][0]

    def page(query):
        # The last row of page 1 is repeated on page 2, as happens when new
        # announcements arrive between page requests.
        ids = {'1': ['id-0', 'id-1'], '2': ['id-1', 'id-2']}[query['pageno'][0]]
        return {'Table': [{**copy.deepcopy(base), 'NEWSID': i} for i in ids],
                'Table1': [{'ROWCNT': 4}]}

    feed.overrides['AnnSubCategoryGetData'] = page
    table = run(corp.get_corporate_announcements(*DATES, '500209'))
    assert sorted(r['filing_id'] for r in table.records) == ['id-0', 'id-1', 'id-2']
    assert [q['pageno'][0] for _, q in feed.calls] == ['1', '2']


def test_announcements_fail_on_truncated_pagination(feed):
    base = json.loads((FIXTURES / 'announcements.json').read_text())

    def page(query):
        return {'Table': base['Table'] if query['pageno'] == ['1'] else [],
                'Table1': [{'ROWCNT': 99}]}

    feed.overrides['AnnSubCategoryGetData'] = page
    with pytest.raises(RuntimeError, match='pagination ended'):
        run(corp.get_corporate_announcements(*DATES, '500209'))


def test_announcements_empty_and_malformed_payloads(feed):
    feed.overrides['AnnSubCategoryGetData'] = {'Table': [], 'Table1': [{'ROWCNT': 0}]}
    assert run(corp.get_corporate_announcements(*DATES, '500209')).records == []
    feed.overrides['AnnSubCategoryGetData'] = {'Table': [], 'Table1': []}
    assert run(corp.get_corporate_announcements(*DATES, '500209')).records == []
    feed.overrides['AnnSubCategoryGetData'] = {'Message': 'error'}
    with pytest.raises(RuntimeError, match='missing Table'):
        run(corp.get_corporate_announcements(*DATES, '500209'))
    rows = json.loads((FIXTURES / 'announcements.json').read_text())['Table']
    feed.overrides['AnnSubCategoryGetData'] = {'Table': rows}
    with pytest.raises(RuntimeError, match='row count'):
        run(corp.get_corporate_announcements(*DATES, '500209'))


def test_announcements_validate_before_fetching(feed):
    for args, kwargs, match in [
        ((*DATES, 'INFY'), {}, 'scrip_code'),
        ((*DATES, '500209'), {'document_type': 'pdf'}, 'document_type'),
        ((*DATES, '500209'), {'category': 'Results'}, 'category'),
        (('01-07-2025', '02-07-2026', '500209'), {}, 'one calendar year'),
    ]:
        with pytest.raises(ValueError, match=match):
            run(corp.get_corporate_announcements(*args, **kwargs))
    assert feed.calls == []
    # Exactly one year, inclusive of the anniversary, is allowed.
    run(corp.get_corporate_announcements('01-07-2025', '01-07-2026', '500209'))


@pytest.mark.parametrize('text,expected', [
    ('Earnings Call Transcript', True),
    ('Transcript of Analyst / Investor Meet', True),
    ('Transcript of the Annual General Meeting', False),
    ('Intimation of earnings conference call', False),
    ('Audio recording of earnings call', False),
])
def test_transcript_classification(text, expected):
    assert corp._is_transcript({'NEWSSUB': text}) is expected


def test_corporate_actions(feed):
    table = run(corp.get_corporate_actions('01-10-2023', '31-10-2023'))
    split, dividend = table.records  # ex-date descending
    assert dividend == {
        'scrip_code': '500209', 'symbol': 'INFY', 'company_name': 'INFOSYS LTD.',
        'source_url': dividend['source_url'], 'raw': dividend['raw'],
        'purpose': 'Interim Dividend - Rs. - 18.0000', 'ex_date': '2023-10-25',
        'record_date': '2023-10-25', 'book_closure_start': None, 'book_closure_end': None,
        'no_delivery_start': '2023-10-18', 'no_delivery_end': '2023-10-25', 'payment_date': None}
    assert split['purpose'] == 'Stock  Split From Rs.5/- to Rs.2/-'
    endpoint, query = feed.calls[0]
    assert endpoint == 'DefaultData'
    assert query['Fdate'] == ['20231001'] and query['TDate'] == ['20231031']
    assert query['ddlcategorys'] == ['E'] and query['scripcode'] == [''] and query['segment'] == ['0']


def test_corporate_actions_filter_by_scrip_and_date_basis(feed):
    table = run(corp.get_corporate_actions('01-10-2023', '31-10-2023', '500209', by_date='record'))
    assert [r['symbol'] for r in table.records] == ['INFY']
    assert feed.calls[0][1]['ddlcategorys'] == ['R'] and feed.calls[0][1]['scripcode'] == ['500209']
    with pytest.raises(ValueError, match='by_date'):
        run(corp.get_corporate_actions('01-10-2023', '31-10-2023', by_date='payment'))


def test_corporate_actions_dedupe_across_year_chunks(feed):
    table = run(corp.get_corporate_actions('01-01-2023', '31-01-2024'))
    assert len(feed.calls) == 2
    assert len(table.records) == 2


def test_corporate_actions_reject_record_without_scrip_code(feed):
    feed.overrides['DefaultData'] = [{'short_name': 'X', 'Purpose': 'Dividend'}]
    with pytest.raises(RuntimeError, match='missing scrip code'):
        run(corp.get_corporate_actions('01-10-2023', '31-10-2023'))


def test_result_calendar(feed):
    table = run(corp.get_result_calendar('01-10-2023', '31-10-2023'))
    assert [(r['meeting_date'], r['symbol']) for r in table.records] == [
        ('2023-10-12', 'INFY'), ('2023-10-23', 'ALOKTEXT')]
    assert table.records[0]['company_name'] == 'INFOSYS LTD.'
    assert feed.calls[0] == ('Corpforthresults', {
        'fromdate': ['20231001'], 'scripcode': [''], 'todate': ['20231031']})
    only = run(corp.get_result_calendar('01-10-2023', '31-10-2023', '521070'))
    assert [r['scrip_code'] for r in only.records] == ['521070']


def test_results_snapshot(feed):
    table = run(corp.get_financial_results_snapshot('500209'))
    assert table.columns == ['metric', 'unit', 'Dec-25', 'Sep-25', 'FY24-25',
                             'scrip_code', 'source_url', 'raw']
    revenue = table.records[0]
    assert (revenue['metric'], revenue['unit'], revenue['Dec-25'], revenue['FY24-25']) == (
        'Revenue', 'crore', 2883, 8617)
    pe = next(r for r in table.records if r['metric'] == 'PE')
    assert pe['Dec-25'] == 97.09 and pe['FY24-25'] is None
    assert table.records[-1]['unit'] == 'million' and table.records[-1]['Dec-25'] == 28830
    assert feed.calls[0] == ('TabResults_PAR', {'scripcode': ['500209'], 'tabtype': ['RESULTS']})


def test_results_snapshot_handles_odd_labels_and_bad_payloads(feed):
    feed.overrides['TabResults_PAR'] = {
        'col2': 'Q1', 'col3': 'Q1', 'col4': '',
        'resultinCr': [{'title': 'Note', 'v1': 'n.m.', 'v2': '1', 'v3': None}]}
    table = run(corp.get_financial_results_snapshot('500209'))
    assert table.columns[2:5] == ['Q1', 'Q1 (2)', 'period_3']
    assert table.records[0]['Q1'] == 'n.m.' and table.records[0]['period_3'] is None
    feed.overrides['TabResults_PAR'] = {}
    with pytest.raises(RuntimeError, match='results snapshot'):
        run(corp.get_financial_results_snapshot('500209'))


# --- documents -------------------------------------------------------------

@pytest.mark.parametrize('url', [
    'http://www.bseindia.com/a.pdf',
    'https://www.nseindia.com/a.pdf',
    'https://www.bseindia.com.evil.example/a.pdf',
    'https://user@www.bseindia.com/a.pdf',
    'https://www.bseindia.com:8443/a.pdf',
    'https://www.bseindia.com/',
])
def test_document_urls_are_restricted_to_bse_hosts(url):
    with pytest.raises(ValueError):
        documents.validate_document_url(url)


def mock_documents(monkeypatch, handler):
    monkeypatch.setattr(bse, '_create_http_client', lambda: httpx2.AsyncClient(
        transport=httpx2.MockTransport(handler)))


def test_document_text_and_base64(monkeypatch):
    content = pdf_bytes()
    mock_documents(monkeypatch, lambda request: httpx2.Response(
        200, content=content, headers={'content-type': 'application/pdf'}))
    text = run(documents.get_corporate_document(PDF_URL))
    assert text['extraction_status'] == 'text' and text['text']
    assert text['size_bytes'] == len(content)
    raw = run(documents.get_corporate_document(PDF_URL, 'base64', limit=10))
    assert raw['pagination'] == {'offset': 0, 'limit': 10, 'unit': 'bytes', 'returned': 10,
                                 'total': len(content), 'has_more': True, 'next_offset': 10}


def test_document_redirect_cannot_leave_bse(monkeypatch):
    mock_documents(monkeypatch, lambda request: httpx2.Response(
        302, headers={'location': 'https://example.com/a.pdf'}))
    with pytest.raises(ValueError, match='BSE website host'):
        run(documents.get_corporate_document(PDF_URL))


def test_document_challenge_page_is_an_error(monkeypatch):
    mock_documents(monkeypatch, lambda request: httpx2.Response(
        200, content=b'<html><body>Access Denied</body></html>'))
    with pytest.raises(RuntimeError, match='access challenge'):
        run(documents.get_corporate_document(PDF_URL))


def test_filing_facts_from_bse_hosted_xbrl(monkeypatch):
    mock_documents(monkeypatch, lambda request: httpx2.Response(200, content=XBRL))
    url = 'https://www.bseindia.com/XBRLFILES/filing.xml'
    everything = run(documents.get_filing_facts(url))
    pledged = run(documents.get_filing_facts(url, 'pledged'))
    assert pledged.records and len(pledged.records) < len(everything.records)
    assert all('pledged' in row['concept'].lower() for row in pledged.records)


# --- MCP protocol ----------------------------------------------------------

RANGE = {'start_date_str': '01-07-2026', 'end_date_str': '31-07-2026'}
TOOLS = [
    ('get_sensex_stocks_ltp', {}, 'records'),
    ('get_equity_securities', {}, 'records'),
    ('get_stock_historical_price_volume_data', {**RANGE, 'scrip_code': '500209'}, 'records'),
    ('get_bse_index_names', {}, 'records'),
    ('get_bse_index_historical_data', {**RANGE, 'index_name': 'SENSEX'}, 'records'),
    ('get_financial_results', {**RANGE, 'scrip_code': '500209'}, 'records'),
    ('get_financial_results_snapshot', {'scrip_code': '500209'}, 'records'),
    ('get_result_calendar', RANGE, 'records'),
    ('get_corporate_actions', RANGE, 'records'),
    ('get_corporate_announcements', {**RANGE, 'scrip_code': '500209'}, 'records'),
    ('get_earnings_call_transcripts', {**RANGE, 'scrip_code': '500209'}, 'records'),
    ('get_corporate_document', {'url': PDF_URL}, 'text'),
    ('get_filing_facts', {'url': PDF_URL.replace('.pdf', '.xml')}, 'records'),
    ('validate_start_end_date_str', RANGE, 'valid'),
]
UNSTRUCTURED_TOOLS = ['get_useragent', 'split_date_range', 'get_sensex_stocks']


def test_every_tool_via_mcp_protocol(feed, monkeypatch):
    mock_documents(monkeypatch, lambda request: httpx2.Response(
        200, content=XBRL if request.url.path.endswith('.xml') else pdf_bytes()))

    async def exercise():
        async with SDKClient(server.build_server()) as client:
            listed = {tool.name for tool in (await client.list_tools()).tools}
            assert listed == {name for name, _, _ in TOOLS} | set(UNSTRUCTURED_TOOLS)
            assert listed == {func.__name__ for func in server._TOOLS}
            for name, params, field in TOOLS:
                response = await client.call_tool(name, params)
                assert response.is_error is False, (name, response)
                content = response.structured_content
                assert content[field], (name, content)
                if field == 'records':
                    assert content['pagination']['returned_rows'] > 0
            for name in UNSTRUCTURED_TOOLS:
                params = RANGE if name == 'split_date_range' else {}
                response = await client.call_tool(name, params)
                assert response.is_error is False, (name, response)
            sensex = await client.call_tool('get_sensex_stocks', {})
            assert '500209' in json.dumps(sensex.structured_content)
    run(exercise())


def test_all_registered_tools_are_async_and_separate_from_nse():
    from nse_data_mcp import server as nse_server
    assert all(inspect.iscoroutinefunction(func) for func in server._TOOLS)
    assert not set(map(id, server._TOOLS)) & set(map(id, nse_server._TOOLS))


def test_csv_export_and_pagination_via_mcp(feed):
    async def exercise():
        async with SDKClient(server.build_server()) as client:
            first = (await client.call_tool('get_corporate_announcements', {
                **RANGE, 'scrip_code': '500209', 'limit': 1, 'output_format': 'csv'})).structured_content
            assert first['pagination'] == {'offset': 0, 'limit': 1, 'returned_rows': 1,
                                           'total_rows': 2, 'has_more': True, 'next_offset': 1}
            row = next(csv.DictReader(io.StringIO(first['csv'])))
            assert json.loads(row['documents'])[0]['kind'] == 'attachment'
            assert json.loads(row['raw'])['SCRIP_CD'] == 500209
            assert first['metadata']['exchange'] == 'BSE'
            bad = await client.call_tool('get_corporate_actions', {**RANGE, 'limit': 0})
            assert bad.is_error is True
            bad = await client.call_tool('get_equity_securities', {'status': 'active'})
            assert bad.is_error is True
    run(exercise())
    # Invalid page requests are rejected before any request reaches BSE.
    assert [endpoint for endpoint, _ in feed.calls] == ['AnnSubCategoryGetData']
