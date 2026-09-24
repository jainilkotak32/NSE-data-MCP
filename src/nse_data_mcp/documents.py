"""Bounded NSE document downloads, PDF/HTML text, and contextual XBRL facts."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit
from xml.etree.ElementTree import Element

import httpx2
from defusedxml import ElementTree as SafeET
from pypdf import PdfReader

from . import data_utils as nse
from .data_utils import Table

ALLOWED_HOSTS = frozenset({"www.nseindia.com", "nseindia.com", "nsearchives.nseindia.com", "archives.nseindia.com"})
MAX_DOCUMENT_BYTES = 25 * 1024 * 1024
MAX_PDF_PAGES = 500
XBRLI = "{http://www.xbrl.org/2003/instance}"
XBRLDI = "{http://xbrl.org/2006/xbrldi}"
XSI = "{http://www.w3.org/2001/XMLSchema-instance}"


def validate_document_url(url: str) -> str:
    """Prevent arbitrary outbound requests, including through redirect targets."""
    parts = urlsplit(url)
    if (parts.scheme != "https" or parts.hostname not in ALLOWED_HOSTS
            or parts.username or parts.password or parts.port not in (None, 443)
            or "\\" in url or any(ord(c) < 32 for c in url)):
        raise ValueError("Document URL must be HTTPS on an NSE website/archive host")
    if not parts.path or parts.path == "/":
        raise ValueError("Document URL must identify a file")
    return url


async def _download(url: str) -> tuple[bytes, str, str]:
    url = validate_document_url(url)
    last_error = None
    async with nse._create_http_client() as client:
        for attempt in range(nse.NSE_REQUEST_ATTEMPTS):
            current = url
            try:
                headers = {"User-Agent": nse.get_useragent(), "Referer": nse.BASE_URL,
                           "Accept": "application/pdf,application/xml,text/html,*/*"}
                for _ in range(6):
                    async with client.stream("GET", current, headers=headers, follow_redirects=False) as response:
                        if response.is_redirect:
                            location = response.headers.get("location")
                            if not location:
                                raise RuntimeError("NSE document redirect has no destination")
                            current = validate_document_url(urljoin(current, location))
                            continue
                        response.raise_for_status()
                        length = response.headers.get("content-length")
                        if length and int(length) > MAX_DOCUMENT_BYTES:
                            raise ValueError("NSE document exceeds the 25 MiB download limit")
                        chunks = []
                        size = 0
                        async for chunk in response.aiter_bytes():
                            size += len(chunk)
                            if size > MAX_DOCUMENT_BYTES:
                                raise ValueError("NSE document exceeds the 25 MiB download limit")
                            chunks.append(chunk)
                        content = b"".join(chunks)
                        if not content:
                            raise RuntimeError("NSE returned an empty document")
                        media_type = response.headers.get("content-type", "").split(";")[0].lower()
                        # A successful HTTP status can still contain an edge challenge.
                        head = content[:4096].lower()
                        if (b"<html" in head or b"<!doctype html" in head) and any(
                            marker in head for marker in (b"access denied", b"request rejected", b"service unavailable")
                        ):
                            raise RuntimeError("NSE returned an access challenge instead of a document")
                        return content, media_type, current
                raise RuntimeError("Too many NSE document redirects")
            except httpx2.HTTPError as exc:
                last_error = exc
                if attempt + 1 < nse.NSE_REQUEST_ATTEMPTS:
                    await asyncio.sleep(0.25 * 2**attempt)
    raise RuntimeError(f"NSE document request failed after {nse.NSE_REQUEST_ATTEMPTS} attempts") from last_error


class _HTMLText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.links: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.hidden += 1
        if not self.hidden:
            if tag in ("p", "div", "br", "tr", "h1", "h2", "h3", "li"):
                self.parts.append("\n")
            if tag in ("td", "th"):
                self.parts.append("\t")
            if tag == "a":
                href = dict(attrs).get("href")
                if href:
                    self.links.append(href)

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.hidden = max(0, self.hidden - 1)
        if tag in ("p", "div", "tr", "li") and not self.hidden:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def _extract_text(content: bytes, media_type: str, url: str) -> tuple[str, dict]:
    if content.lstrip().startswith(b"%PDF"):
        reader = PdfReader(io.BytesIO(content))
        if reader.is_encrypted and not reader.decrypt(""):
            raise ValueError("Cannot extract an encrypted PDF; request base64 to download it")
        if len(reader.pages) > MAX_PDF_PAGES:
            raise ValueError("PDF exceeds the 500-page text extraction limit")
        pages = [page.extract_text() or "" for page in reader.pages]
        text = "\n\n".join(f"--- Page {i + 1} ---\n{part}" for i, part in enumerate(pages))
        links = []
        for page in reader.pages:
            for annotation in page.get("/Annots", []):
                action = annotation.get_object().get("/A")
                if action and action.get("/URI"):
                    links.append(str(action["/URI"]))
        return text, {"media_type": "application/pdf", "page_count": len(pages),
                      "pages_without_text": [i + 1 for i, part in enumerate(pages) if not part.strip()],
                      "extraction_status": "text" if any(p.strip() for p in pages) else "no_text_layer",
                      "links": list(dict.fromkeys(links))}
    text = content.decode("utf-8-sig", errors="replace")
    if "html" in media_type or "<html" in text[:2000].lower() or "<!doctype html" in text[:2000].lower():
        parser = _HTMLText()
        parser.feed(text)
        return "".join(parser.parts).strip(), {"media_type": "text/html", "links": list(dict.fromkeys(
            urljoin(url, link) for link in parser.links)), "extraction_status": "text"}
    if "xml" in media_type or text.lstrip().startswith("<?xml"):
        root = SafeET.fromstring(content)
        return "\n".join(t.strip() for t in root.itertext() if t.strip()), {
            "media_type": "application/xml", "extraction_status": "text"}
    if media_type.startswith("text/plain"):
        return text, {"media_type": media_type, "extraction_status": "text"}
    raise ValueError("Unsupported document format for text extraction; use base64 for the original file")


async def get_corporate_document(url: str, output_format: str = "text",
                                 offset: int = 0, limit: int = 20_000) -> dict:
    """Read text or download original bytes in base64 chunks; no server-side file writes."""
    if output_format not in ("text", "base64"):
        raise ValueError("output_format must be text or base64")
    if offset < 0 or not 1 <= limit <= 100_000:
        raise ValueError("offset must be nonnegative and limit must be between 1 and 100000")
    content, media_type, final_url = await _download(url)
    result = {"source_url": url, "resolved_url": final_url, "media_type": media_type,
              "size_bytes": len(content), "sha256": hashlib.sha256(content).hexdigest(),
              "output_format": output_format}
    if output_format == "text":
        text, metadata = await asyncio.to_thread(_extract_text, content, media_type, final_url)
        result.update(metadata)
        page = text[offset:offset + limit]
        result["text"] = page
        total, returned, unit = len(text), len(page), "characters"
    else:
        page = content[offset:offset + limit]
        result["base64"] = base64.b64encode(page).decode("ascii")
        total, returned, unit = len(content), len(page), "bytes"
    result["pagination"] = {"offset": offset, "limit": limit, "unit": unit,
                            "returned": returned, "total": total, "has_more": offset + returned < total,
                            "next_offset": offset + returned if offset + returned < total else None}
    return result


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _parse_xml_namespaces(content: bytes) -> tuple[Element, dict[Element, dict[str, str]]]:
    """Keep each element's namespace scope, including local prefix rebindings."""
    scopes, stack, pending = {}, [], {}
    parser = SafeET.iterparse(io.BytesIO(content), events=("start-ns", "start", "end"))
    for event, item in parser:
        if event == "start-ns":
            pending[item[0]] = item[1]
        elif event == "start":
            scope = stack[-1] if stack else {"xml": "http://www.w3.org/XML/1998/namespace"}
            if pending:
                scope = {**scope, **pending}
                pending = {}
            scopes[item] = scope
            stack.append(scope)
        else:
            stack.pop()
    return parser.root, scopes


def _expand_qname(value: str | None, scope: dict[str, str]) -> str | None:
    """Resolve a QName value into the same {namespace}name form as element tags."""
    if value is None:
        return None
    value = value.strip()
    if ":" in value:
        prefix, name = value.split(":", 1)
        if not scope.get(prefix):
            raise ValueError(f"XBRL QName uses undeclared namespace prefix {prefix!r}")
        return f"{{{scope[prefix]}}}{name}"
    namespace = scope.get("")
    return f"{{{namespace}}}{value}" if namespace else value


def _unit_xml(unit: Element, scopes: dict[Element, dict[str, str]]) -> str:
    """Serialize a standalone unit without changing the meaning of QName text.

    ElementTree normally invents prefixes for tags and drops declarations used
    only in text. Copy with lexical names and explicit declarations instead;
    no process-global namespace registry is changed by concurrent downloads.
    """
    def copy(node: Element, parent_scope: dict[str, str]) -> Element:
        scope = scopes[node]

        def lexical_name(name: str, *, attribute: bool = False) -> str:
            if not name.startswith("{"):
                return name
            namespace, local = name[1:].split("}", 1)
            prefix = next(prefix for prefix, uri in scope.items()
                          if uri == namespace and (prefix or not attribute))
            return f"{prefix}:{local}" if prefix else local

        attributes = {lexical_name(key, attribute=True): value for key, value in node.attrib.items()}
        for prefix, uri in scope.items():
            if prefix != "xml" and parent_scope.get(prefix) != uri:
                attributes[f"xmlns:{prefix}" if prefix else "xmlns"] = uri
        result = Element(lexical_name(node.tag), attributes)
        result.text, result.tail = node.text, node.tail
        result.extend(copy(child, scope) for child in node)
        return result

    return SafeET.tostring(copy(unit, {}), encoding="unicode")


def parse_xbrl(content: bytes) -> Table:
    """Extract raw instance facts with units, periods and dimensional context.

    Values remain strings to preserve decimal precision and reported units.
    No cross-taxonomy financial ratios or totals are guessed.
    """
    root, scopes = _parse_xml_namespaces(content)
    if root.tag != XBRLI + "xbrl":
        raise ValueError("Expected an XBRL instance XML file; use the filing's xbrl URL, not ixbrl HTML")
    contexts = {}
    for context in root.findall(XBRLI + "context"):
        period = context.find(XBRLI + "period")
        entity = context.find(".//" + XBRLI + "identifier")
        dimensions = []
        for member in context.iter():
            if member.tag in (XBRLDI + "explicitMember", XBRLDI + "typedMember"):
                value = "".join(member.itertext()).strip()
                dimensions.append({
                    "dimension": member.get("dimension"), "value": value,
                    "qualified_dimension": _expand_qname(member.get("dimension"), scopes[member]),
                    "qualified_value": _expand_qname(value, scopes[member])
                    if member.tag == XBRLDI + "explicitMember" else None,
                })
        contexts[context.get("id")] = {
            "entity": entity.text if entity is not None else None,
            "period": {_local(child.tag): child.text for child in period} if period is not None else {},
            "dimensions": dimensions,
        }
    units = {unit.get("id"): _unit_xml(unit, scopes)
             for unit in root.findall(XBRLI + "unit")}
    records = []
    for fact in root.iter():
        context_ref = fact.get("contextRef")
        if context_ref is None:
            continue
        if context_ref not in contexts:
            raise ValueError(f"XBRL fact refers to missing context {context_ref}")
        unit_ref = fact.get("unitRef")
        if unit_ref is not None and unit_ref not in units:
            raise ValueError(f"XBRL fact refers to missing unit {unit_ref}")
        nil = fact.get(XSI + "nil") in ("true", "1")
        records.append({"concept": _local(fact.tag), "qualified_concept": fact.tag,
                        "value": None if nil else "".join(fact.itertext()).strip(), "is_nil": nil,
                        "context_id": context_ref, **contexts[context_ref],
                        "unit_id": unit_ref, "unit_xml": units.get(unit_ref),
                        "decimals": fact.get("decimals"), "precision": fact.get("precision")})
    return Table(["concept", "qualified_concept", "value", "is_nil", "context_id", "entity", "period",
                  "dimensions", "unit_id", "unit_xml", "decimals", "precision"], records)


async def get_filing_facts(url: str, concept: str | None = None) -> Table:
    """Download financial/shareholding XBRL and optionally filter concept names."""
    content, _, _ = await _download(url)
    table = await asyncio.to_thread(parse_xbrl, content)
    if concept:
        return Table(table.columns, [row for row in table.records if concept.casefold() in row["concept"].casefold()])
    return table
