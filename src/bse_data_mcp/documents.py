"""Bounded BSE document downloads, with the shared PDF/HTML text and XBRL parsers."""

from __future__ import annotations

import asyncio
import base64
import hashlib
from urllib.parse import urljoin, urlsplit

import httpx2

from nse_data_mcp.documents import (
    MAX_DOCUMENT_BYTES,
    MAX_PDF_PAGES,
    _extract_text,
    parse_xbrl,
)

from . import data_utils as bse
from .data_utils import Table

__all__ = ["MAX_DOCUMENT_BYTES", "MAX_PDF_PAGES", "validate_document_url",
           "get_corporate_document", "get_filing_facts"]

ALLOWED_HOSTS = frozenset({"www.bseindia.com", "bseindia.com", "api.bseindia.com"})


def validate_document_url(url: str) -> str:
    """Prevent arbitrary outbound requests, including through redirect targets."""
    parts = urlsplit(url)
    if (parts.scheme != "https" or parts.hostname not in ALLOWED_HOSTS
            or parts.username or parts.password or parts.port not in (None, 443)
            or "\\" in url or any(ord(c) < 32 for c in url)):
        raise ValueError("Document URL must be HTTPS on a BSE website host")
    if not parts.path or parts.path == "/":
        raise ValueError("Document URL must identify a file")
    return url


async def _download(url: str) -> tuple[bytes, str, str]:
    url = validate_document_url(url)
    last_error = None
    async with bse._create_http_client() as client:
        for attempt in range(bse.BSE_REQUEST_ATTEMPTS):
            current = url
            try:
                headers = {"User-Agent": bse.get_useragent(), "Referer": bse.BASE_URL + "/",
                           "Accept": "application/pdf,application/xml,text/html,*/*"}
                for _ in range(6):
                    async with client.stream("GET", current, headers=headers, follow_redirects=False) as response:
                        if response.is_redirect:
                            location = response.headers.get("location")
                            if not location:
                                raise RuntimeError("BSE document redirect has no destination")
                            current = validate_document_url(urljoin(current, location))
                            continue
                        response.raise_for_status()
                        length = response.headers.get("content-length")
                        if length and int(length) > MAX_DOCUMENT_BYTES:
                            raise ValueError("BSE document exceeds the 25 MiB download limit")
                        chunks = []
                        size = 0
                        async for chunk in response.aiter_bytes():
                            size += len(chunk)
                            if size > MAX_DOCUMENT_BYTES:
                                raise ValueError("BSE document exceeds the 25 MiB download limit")
                            chunks.append(chunk)
                        content = b"".join(chunks)
                        if not content:
                            raise RuntimeError("BSE returned an empty document")
                        media_type = response.headers.get("content-type", "").split(";")[0].lower()
                        # A successful HTTP status can still contain an edge challenge.
                        head = content[:4096].lower()
                        if (b"<html" in head or b"<!doctype html" in head) and any(
                            marker in head for marker in (b"access denied", b"request rejected", b"service unavailable")
                        ):
                            raise RuntimeError("BSE returned an access challenge instead of a document")
                        return content, media_type, current
                raise RuntimeError("Too many BSE document redirects")
            except httpx2.HTTPError as exc:
                last_error = exc
                if attempt + 1 < bse.BSE_REQUEST_ATTEMPTS:
                    await asyncio.sleep(0.25 * 2**attempt)
    raise RuntimeError(f"BSE document request failed after {bse.BSE_REQUEST_ATTEMPTS} attempts") from last_error


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


async def get_filing_facts(url: str, concept: str | None = None) -> Table:
    """Download a BSE-hosted XBRL instance and optionally filter concept names."""
    content, _, _ = await _download(url)
    table = await asyncio.to_thread(parse_xbrl, content)
    if concept:
        return Table(table.columns, [row for row in table.records if concept.casefold() in row["concept"].casefold()])
    return table
