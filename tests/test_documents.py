"""Document download bounds, readable PDFs, and auditable XBRL extraction."""
import asyncio
import base64
import hashlib
import io
from pathlib import Path

import httpx2
import pytest
from defusedxml.common import DefusedXmlException
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject

from nse_data_mcp import documents as docs

URL = 'https://nsearchives.nseindia.com/corporate/test.pdf'
FIXTURES = Path(__file__).parent / 'fixtures' / 'nse'


def pdf_bytes(text='Earnings call transcript: revenue grew 15%.'):
    writer = PdfWriter()
    page = writer.add_blank_page(width=600, height=800)
    font = DictionaryObject({NameObject('/Type'): NameObject('/Font'),
                             NameObject('/Subtype'): NameObject('/Type1'),
                             NameObject('/BaseFont'): NameObject('/Helvetica')})
    page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({
        NameObject('/F1'): writer._add_object(font)})})
    stream = DecodedStreamObject()
    stream.set_data(f'BT /F1 12 Tf 50 750 Td ({text}) Tj ET'.encode())
    page[NameObject('/Contents')] = writer._add_object(stream)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def mock_transport(monkeypatch, handler):
    monkeypatch.setattr(docs.nse, '_create_http_client', lambda: httpx2.AsyncClient(
        transport=httpx2.MockTransport(handler), follow_redirects=True))


def test_pdf_reading_and_character_pagination(monkeypatch):
    content = pdf_bytes()
    mock_transport(monkeypatch, lambda r: httpx2.Response(200, content=content,
        headers={'content-type': 'application/pdf'}))
    first = asyncio.run(docs.get_corporate_document(URL, limit=20))
    second = asyncio.run(docs.get_corporate_document(URL, offset=20, limit=1000))
    assert 'Earnings call transcript: revenue grew 15%.' in first['text'] + second['text']
    assert first['page_count'] == 1
    assert first['pagination']['unit'] == 'characters'
    assert first['pagination']['next_offset'] == 20
    assert second['pagination']['has_more'] is False
    assert first['sha256'] == hashlib.sha256(content).hexdigest()
    assert first['pages_without_text'] == []


def test_original_download_chunks_reconstruct_bytes(monkeypatch):
    content = pdf_bytes()
    mock_transport(monkeypatch, lambda r: httpx2.Response(200, content=content))
    offset, chunks = 0, []
    while True:
        response = asyncio.run(docs.get_corporate_document(URL, 'base64', offset, 37))
        chunks.append(base64.b64decode(response['base64']))
        assert response['pagination']['unit'] == 'bytes'
        if not response['pagination']['has_more']:
            break
        offset = response['pagination']['next_offset']
    assert b''.join(chunks) == content
    assert asyncio.run(docs.get_corporate_document(URL, 'base64', len(content) + 1))['base64'] == ''


def test_blank_pdf_reports_no_text_layer():
    out = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(100, 100)
    writer.write(out)
    _, metadata = docs._extract_text(out.getvalue(), 'application/pdf', URL)
    assert metadata['extraction_status'] == 'no_text_layer'
    assert metadata['pages_without_text'] == [1]


def test_encrypted_pdf_and_page_limit(monkeypatch):
    writer = PdfWriter()
    writer.add_blank_page(100, 100)
    writer.encrypt('secret')
    out = io.BytesIO()
    writer.write(out)
    with pytest.raises(ValueError, match='encrypted'):
        docs._extract_text(out.getvalue(), 'application/pdf', URL)
    monkeypatch.setattr(docs, 'MAX_PDF_PAGES', 0)
    with pytest.raises(ValueError, match='page'):
        docs._extract_text(pdf_bytes(), 'application/pdf', URL)


def test_html_preserves_table_text_and_links_but_skips_scripts():
    content = b'<html><script>SECRET</script><style>HIDDEN</style><p>Revenue &amp; profit</p><table><tr><td>Revenue</td><td>100</td></tr></table><a href="/transcript.pdf">Full transcript</a></html>'
    text, meta = docs._extract_text(content, 'text/html', URL)
    assert 'Revenue & profit' in text
    assert 'Revenue\t100' in text
    assert 'SECRET' not in text and 'HIDDEN' not in text
    assert meta['links'] == ['https://nsearchives.nseindia.com/transcript.pdf']


@pytest.mark.parametrize('url', [
    'http://nsearchives.nseindia.com/test.pdf', 'https://example.com/test.pdf',
    'https://127.0.0.1/test.pdf', 'https://www.nseindia.com.evil.com/test.pdf',
    'https://www.nseindia.com@evil.com/test.pdf', 'file:///etc/passwd',
    'https://user:password@www.nseindia.com/test.pdf', 'https://www.nseindia.com:444/test.pdf',
    'https://www.nseindia.com/', 'https://www.nseindia.com/test\n.pdf',
    'https://www.nseindia.com\\@evil.com/file.pdf',
])
def test_unsafe_urls_rejected_before_request(monkeypatch, url):
    def no_network(*args):
        pytest.fail('must not make an HTTP request')
    mock_transport(monkeypatch, no_network)
    with pytest.raises(ValueError):
        asyncio.run(docs.get_corporate_document(url))


def test_redirect_destination_checked_before_fetch(monkeypatch):
    requests = []
    def handler(request):
        requests.append(str(request.url))
        return httpx2.Response(302, headers={'location': 'https://localhost/private'})
    mock_transport(monkeypatch, handler)
    with pytest.raises(ValueError, match='NSE'):
        asyncio.run(docs.get_corporate_document(URL))
    assert requests == [URL]


def test_valid_archive_redirect(monkeypatch):
    def handler(request):
        if str(request.url) == URL:
            return httpx2.Response(302, headers={'location': 'https://archives.nseindia.com/final.pdf'})
        return httpx2.Response(200, content=pdf_bytes())
    mock_transport(monkeypatch, handler)
    result = asyncio.run(docs.get_corporate_document(URL))
    assert result['resolved_url'] == 'https://archives.nseindia.com/final.pdf'


def test_redirect_loop_is_bounded(monkeypatch):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx2.Response(302, headers={'location': URL})
    mock_transport(monkeypatch, handler)
    with pytest.raises(RuntimeError, match='redirects'):
        asyncio.run(docs.get_corporate_document(URL))
    assert len(calls) == 6


@pytest.mark.parametrize('declared', [True, False])
def test_size_limit_enforced_with_or_without_content_length(monkeypatch, declared):
    monkeypatch.setattr(docs, 'MAX_DOCUMENT_BYTES', 5)
    headers = {'content-length': '10'} if declared else {}
    def handler(request):
        response = httpx2.Response(200, content=b'0123456789', headers=headers)
        if not declared:
            del response.headers['content-length']
        return response
    mock_transport(monkeypatch, handler)
    with pytest.raises(ValueError, match='download limit'):
        asyncio.run(docs.get_corporate_document(URL))


def test_http_errors_retry_and_never_return_error_page(monkeypatch):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx2.Response(503)
    async def no_sleep(*args):
        pass
    monkeypatch.setattr(docs.asyncio, 'sleep', no_sleep)
    mock_transport(monkeypatch, handler)
    with pytest.raises(RuntimeError, match='3 attempts'):
        asyncio.run(docs.get_corporate_document(URL))
    assert len(calls) == 3


@pytest.mark.parametrize('content,message', [(b'', 'empty document'),
    (b'<html><h1>Access Denied</h1></html>', 'access challenge')])
def test_invalid_document_response(monkeypatch, content, message):
    mock_transport(monkeypatch, lambda r: httpx2.Response(200, content=content))
    with pytest.raises(RuntimeError, match=message):
        asyncio.run(docs.get_corporate_document(URL))


@pytest.mark.parametrize('kwargs', [{'output_format': 'bad'}, {'offset': -1}, {'limit': 0}, {'limit': 100001}])
def test_document_arguments_validated_before_network(monkeypatch, kwargs):
    mock_transport(monkeypatch, lambda r: pytest.fail('unexpected network'))
    with pytest.raises(ValueError):
        asyncio.run(docs.get_corporate_document(URL, **kwargs))


def test_real_nse_financial_xbrl_contexts_units_and_concepts():
    table = docs.parse_xbrl((FIXTURES / 'financial.xml').read_bytes())
    assert len(table) > 100
    revenues = [r for r in table.records if 'Revenue' in r['concept']]
    assert revenues
    assert all('qualified_concept' in r for r in revenues)
    assert any(r['period'].get('endDate') == '2026-06-30' for r in revenues)
    assert any(r['unit_xml'] and 'INR' in r['unit_xml'] for r in revenues)
    assert any(r['dimensions'] for r in table.records)
    assert all(isinstance(r['value'], str) or r['value'] is None for r in table.records)


def unit_measures(xml):
    """Resolve measure QNames from a standalone exported unit, as a client would."""
    scopes, stack, pending, measures = {}, [], {}, []
    for event, item in docs.SafeET.iterparse(io.StringIO(xml), events=('start-ns', 'start', 'end')):
        if event == 'start-ns':
            pending[item[0]] = item[1]
        elif event == 'start':
            scopes[item] = {**(stack[-1] if stack else {}), **pending}
            stack.append(scopes[item])
            pending = {}
        else:
            if item.tag == docs.XBRLI + 'measure':
                prefix, separator, name = item.text.strip().partition(':')
                if not separator:
                    prefix, name = '', prefix
                measures.append((scopes[item][prefix], name))
            stack.pop()
    return measures


def test_real_nse_units_and_dimensions_keep_namespace_identity():
    rows = docs.parse_xbrl((FIXTURES / 'financial.xml').read_bytes()).records
    units = {row['unit_id']: row['unit_xml'] for row in rows if row['unit_id']}
    assert unit_measures(units['INR']) == [('http://www.xbrl.org/2003/iso4217', 'INR')]
    assert unit_measures(units['INRPerShare']) == [
        ('http://www.xbrl.org/2003/iso4217', 'INR'),
        ('http://www.xbrl.org/2003/instance', 'shares'),
    ]
    dimension = next(row['dimensions'][0] for row in rows if row['dimensions'])
    assert dimension['qualified_dimension'] == (
        '{http://www.sebi.gov.in/xbrl/2026-01-31/in-capmkt}DetailsOfOtherExpensesAxis')
    assert dimension['qualified_value'] == (
        '{http://www.sebi.gov.in/xbrl/2026-01-31/in-capmkt}OtherExpenses1Member')


XBRL = b'''<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance"
 xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:x="urn:test"
 xmlns:xbrldi="http://xbrl.org/2006/xbrldi">
 <xbrli:context id="C"><xbrli:entity><xbrli:identifier>INFY</xbrli:identifier></xbrli:entity>
 <xbrli:period><xbrli:instant>2026-06-30</xbrli:instant></xbrli:period>
 <xbrli:scenario><xbrldi:typedMember dimension="x:Holder"><x:Name>Mutual fund</x:Name></xbrldi:typedMember></xbrli:scenario></xbrli:context>
 <xbrli:unit id="shares"><xbrli:measure>xbrli:shares</xbrli:measure></xbrli:unit>
 <x:PledgedShares contextRef="C" unitRef="shares" decimals="0">0</x:PledgedShares>
 <x:Unknown contextRef="C" xsi:nil="true"/>
 </xbrli:xbrl>'''


def test_ownership_facts_preserve_zero_nil_and_holder_dimensions():
    table = docs.parse_xbrl(XBRL)
    first, second = table.records
    assert first['value'] == '0'
    assert first['unit_id'] == 'shares'
    assert first['dimensions'] == [{'dimension': 'x:Holder', 'value': 'Mutual fund',
                                    'qualified_dimension': '{urn:test}Holder',
                                    'qualified_value': None}]
    assert first['period'] == {'instant': '2026-06-30'}
    assert second['is_nil'] is True and second['value'] is None


def test_unit_namespaces_handle_local_rebinding_default_and_generated_prefix_names():
    content = XBRL.replace(
        b'<xbrli:unit id="shares"><xbrli:measure>xbrli:shares</xbrli:measure></xbrli:unit>',
        b'''<xbrli:unit id="shares" xmlns:ns0="urn:outer" xmlns="urn:default">
        <xbrli:measure>ns0:first</xbrli:measure>
        <xbrli:measure xmlns:ns0="urn:inner">ns0:second</xbrli:measure>
        <xbrli:measure>ns0:third</xbrli:measure>
        <xbrli:measure>fourth</xbrli:measure>
        </xbrli:unit>''')
    unit = docs.parse_xbrl(content).records[0]['unit_xml']
    assert unit_measures(unit) == [
        ('urn:outer', 'first'), ('urn:inner', 'second'),
        ('urn:outer', 'third'), ('urn:default', 'fourth'),
    ]


def test_dimension_namespaces_follow_member_scope():
    content = XBRL.replace(
        b'<xbrldi:typedMember dimension="x:Holder"><x:Name>Mutual fund</x:Name></xbrldi:typedMember>',
        b'''<xbrldi:explicitMember xmlns:x="urn:local" dimension="x:Axis">x:Member</xbrldi:explicitMember>
        <xbrldi:explicitMember dimension="x:Axis">x:Member</xbrldi:explicitMember>
        <xbrldi:explicitMember xmlns="urn:default" dimension="Axis">Member</xbrldi:explicitMember>''')
    dimensions = docs.parse_xbrl(content).records[0]['dimensions']
    assert [(d['qualified_dimension'], d['qualified_value']) for d in dimensions] == [
        ('{urn:local}Axis', '{urn:local}Member'),
        ('{urn:test}Axis', '{urn:test}Member'),
        ('{urn:default}Axis', '{urn:default}Member'),
    ]


def test_undeclared_dimension_prefix_is_rejected():
    with pytest.raises(ValueError, match='namespace prefix'):
        docs.parse_xbrl(XBRL.replace(b'dimension="x:Holder"', b'dimension="missing:Holder"'))


def test_xbrl_filter(monkeypatch):
    mock_transport(monkeypatch, lambda r: httpx2.Response(200, content=XBRL))
    table = asyncio.run(docs.get_filing_facts(URL.replace('.pdf', '.xml'), 'pledged'))
    assert [r['concept'] for r in table.records] == ['PledgedShares']


@pytest.mark.parametrize('content', [XBRL.replace(b'contextRef="C"', b'contextRef="missing"'),
                                     XBRL.replace(b'unitRef="shares"', b'unitRef="missing"'), b'<html/>'])
def test_xbrl_missing_context_units_or_wrong_format_rejected(content):
    with pytest.raises(ValueError):
        docs.parse_xbrl(content)


def test_xml_entities_cannot_read_local_files_or_expand():
    attack = b'<!DOCTYPE x [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><x>&xxe;</x>'
    with pytest.raises(DefusedXmlException):
        docs.parse_xbrl(attack)
    with pytest.raises(DefusedXmlException):
        docs._extract_text(attack, 'application/xml', URL)
