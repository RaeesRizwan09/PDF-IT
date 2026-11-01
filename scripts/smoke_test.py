#!/usr/bin/env python3
"""
Post-deploy smoke test for the remote html2pdf MCP server.

Usage:
  python3 scripts/smoke_test.py https://<app>.azurecontainerapps.io/<token>/mcp

Exercises list_tools, check_environment, and a real html_content_to_pdf
render end-to-end (verifies the returned pdf_url actually downloads a PDF).
"""

from __future__ import annotations

import asyncio
import sys
from urllib.request import urlopen

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def main(server_url: str) -> int:
    ok = True
    async with streamable_http_client(server_url) as streams:
        read, write = streams[0], streams[1]
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            print("Listing tools:")
            for t in tools.tools:
                print(f"  - {t.name}")
            assert {"html_content_to_pdf", "url_to_pdf", "check_environment"} <= {
                t.name for t in tools.tools
            }, "expected tools missing"

            print("\nCalling check_environment...")
            res = await session.call_tool("check_environment", {})
            print("  ", res.content[0].text[:300])

            print("\nCalling html_content_to_pdf (small hello-doc)...")
            html = (
                "<html><head><title>Hello MCP</title></head><body>"
                "<h1>Hello from html2pdf MCP</h1><p>Deployment works ✔</p>"
                "</body></html>"
            )
            res = await session.call_tool(
                "html_content_to_pdf",
                {"html": html, "filename": "smoke-test.pdf"},
            )
            body = res.content[0].text
            print("  ", body[:400])

            if '"ok": true' in body or '"ok":true' in body:
                import json

                data = json.loads(body)
                url = data.get("pdf_url")
                if url:
                    raw = urlopen(url, timeout=30).read()
                    assert raw[:4] == b"%PDF", "downloaded file is not a PDF"
                    print(f"  downloaded {len(raw)} bytes of PDF — delivery OK")
                else:
                    print("  (no pdf_url — Blob Storage not configured?)")
                    ok = False
            else:
                ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(f"usage: {sys.argv[0]} <mcp-endpoint-url>")
    sys.exit(asyncio.run(main(sys.argv[1])))
