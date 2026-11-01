#!/usr/bin/env python3
"""
Live e2e test of the PDF manipulation toolkit over the remote MCP endpoint.

Chains: html_content_to_pdf x2 → pdf_merge → pdf_extract_text → pdf_encrypt →
pdf_decrypt → pdf_page_numbers → pdf_metadata → pdf_extract_pages → pdf_split.
Downloads the returned links and inspects them with pypdf locally.

Usage: python3 scripts/test_pdftools.py <mcp-endpoint-url>
"""

from __future__ import annotations

import asyncio
import io
import json
import sys
import zipfile
from urllib.request import urlopen

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from pypdf import PdfReader


def body_to_dict(text: str) -> dict:
    return json.loads(text)


def download(url: str) -> bytes:
    return urlopen(url, timeout=60).read()


async def call(session, name, args) -> dict:
    res = await session.call_tool(name, args)
    out = body_to_dict(res.content[0].text)
    return out


async def main(server_url: str) -> int:
    async with streamable_http_client(server_url) as streams:
        read, write = streams[0], streams[1]
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = {t.name for t in (await session.list_tools()).tools}
            want = {"html_content_to_pdf", "pdf_merge", "pdf_split",
                    "pdf_extract_pages", "pdf_delete_pages", "pdf_rotate",
                    "pdf_watermark", "pdf_page_numbers", "pdf_encrypt",
                    "pdf_decrypt", "pdf_metadata", "pdf_extract_text"}
            missing = want - tools
            assert not missing, f"missing tools: {missing}"
            print(f"all {len(want)} expected tools exposed")

            def html(word: str) -> str:
                return (f"<html><body><h1>{word} document</h1>"
                        f"<p>The quick brown fox says {word}.</p></body></html>")

            print("rendering two source PDFs...")
            a = await call(session, "html_content_to_pdf",
                           {"html": html("Alpha"), "filename": "a.pdf"})
            b = await call(session, "html_content_to_pdf",
                           {"html": html("Beta"), "filename": "b.pdf"})
            assert a.get("ok") and b.get("ok"), (a, b)
            print("  a:", a["pdf_url"][:80], "...")
            print("  b:", b["pdf_url"][:80], "...")

            print("pdf_merge...")
            m = await call(session, "pdf_merge",
                           {"pdf_urls": [a["pdf_url"], b["pdf_url"]],
                            "filename": "merged.pdf"})
            assert m.get("ok") and m.get("pdf_url"), m
            merged = download(m["pdf_url"])
            r = PdfReader(io.BytesIO(merged))
            assert len(r.pages) == 2
            print(f"  merged PDF has {len(r.pages)} pages "
                  f"({m['bytes']} bytes) ✔")

            print("pdf_extract_text...")
            t = await call(session, "pdf_extract_text",
                           {"pdf_url": m["pdf_url"], "page_ranges": "all"})
            assert t.get("ok") and "Alpha" in t["text"] and "Beta" in t["text"], t
            print(f"  {t['pages']} pages, {t['characters']} chars, text OK ✔")

            print("pdf_extract_pages ('2') ...")
            e = await call(session, "pdf_extract_pages",
                           {"pdf_url": m["pdf_url"], "page_ranges": "2"})
            assert len(PdfReader(io.BytesIO(download(e["pdf_url"]))).pages) == 1
            print("  1 page ✔")

            print("pdf_page_numbers...")
            n = await call(session, "pdf_page_numbers",
                           {"pdf_url": m["pdf_url"], "position": "bottom-center",
                            "start": 1, "label": "Page"})
            assert PdfReader(io.BytesIO(download(n["pdf_url"]))).pages[0] \
                .extract_text().strip()
            print("  numbered ✔")

            print("pdf_metadata (edit)...")
            md = await call(session, "pdf_metadata",
                            {"pdf_url": m["pdf_url"],
                             "set_metadata": {"title": "Chained Report",
                                              "author": "Smoke Test"}})
            md_lower = {k.lower(): v for k, v in md["metadata"].items()}
            assert md_lower.get("title") == "Chained Report" and \
                md.get("pdf_url"), md
            print("  title set, new PDF returned ✔")

            print("pdf_watermark...")
            w = await call(session, "pdf_watermark",
                           {"pdf_url": m["pdf_url"], "text": "DRAFT 2026"})
            assert PdfReader(io.BytesIO(download(w["pdf_url"]))).pages[0] \
                .extract_text()
            print("  stamped ✔")

            print("pdf_encrypt → pdf_decrypt round-trip...")
            enc = await call(session, "pdf_encrypt",
                             {"pdf_url": m["pdf_url"], "password": "pw12345"})
            assert enc.get("ok"), enc
            raw = download(enc["pdf_url"])
            er = PdfReader(io.BytesIO(raw))
            assert er.is_encrypted and er.decrypt("pw12345"), \
                "returned PDF is not password-protected"
            dec = await call(session, "pdf_decrypt",
                             {"pdf_url": enc["pdf_url"], "password": "pw12345"})
            dr = PdfReader(io.BytesIO(download(dec["pdf_url"])))
            assert not dr.is_encrypted and len(dr.pages) == 2
            print("  encrypted (AES-256, opens with pw12345) → decrypted ✔")

            print("pdf_split...")
            s = await call(session, "pdf_split", {"pdf_url": m["pdf_url"]})
            zf = zipfile.ZipFile(io.BytesIO(download(s["pdf_url"])))
            names = zf.namelist()
            assert len(names) == 2 and all(x.endswith(".pdf") for x in names), names
            print(f"  ZIP with {names} ✔")

            print("pdf_rotate + pdf_delete_pages...")
            ro = await call(session, "pdf_rotate",
                            {"pdf_url": m["pdf_url"], "degrees": 90,
                             "page_ranges": "1"})
            rr = PdfReader(io.BytesIO(download(ro["pdf_url"])))
            assert rr.pages[0].rotation == 90 and rr.pages[1].rotation == 0
            dl = await call(session, "pdf_delete_pages",
                            {"pdf_url": m["pdf_url"], "page_ranges": "1"})
            assert len(PdfReader(io.BytesIO(download(dl["pdf_url"]))).pages) == 1
            print("  rotated/deleted ✔")

            print("\nALL PDF TOOLKIT LIVE TESTS PASSED")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(f"usage: {sys.argv[0]} <mcp-endpoint-url>")
    sys.exit(asyncio.run(main(sys.argv[1])))
