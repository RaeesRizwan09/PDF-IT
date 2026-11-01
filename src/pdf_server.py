# -*- coding: utf-8 -*-
"""
html2pdf MCP server.

Exposes the universal HTML→PDF pipeline (see pipeline.py) as MCP tools over
stdio, so coding agents (e.g. OpenCode) can generate PDFs on demand.

Tools
-----
- html_file_to_pdf     : convert an .html file on disk to PDF
- html_content_to_pdf  : convert raw HTML text to PDF
- check_environment    : report playwright/chromium availability

OpenCode registration (~/.config/opencode/opencode.json):

    "html2pdf": {
        "type": "local",
        "command": ["/usr/bin/python3", "/home/himayat/.local/share/opencode-mcp/html2pdf/pdf_server.py"]
    }

Set HTML2PDF_LOGLEVEL=DEBUG (env) for verbose logs on stderr.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import pipeline  # noqa: E402
from mcp.server.mcpserver import MCPServer  # noqa: E402

# stderr only — stdout carries the MCP protocol.
logging.basicConfig(
    stream=sys.stderr,
    level=os.environ.get("HTML2PDF_LOGLEVEL", "INFO"),
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
log = logging.getLogger("html2pdf.server")

server = MCPServer(
    name="html2pdf",
    version="0.1.0",
    instructions=(
        "Convert HTML files or raw HTML content into PDFs using headless Chromium. "
        "Handles KaTeX/MathJax equations, Google/any web fonts, and remote images "
        "(with clean fallback boxes for broken assets). "
        "Use html_file_to_pdf for .html files on disk; use html_content_to_pdf for "
        "HTML you just authored. layout_config keys: font_size_base, line_height, "
        "section_gap, element_gap, card_padding, content_margin_v, content_margin_h."
    ),
)


def _error_result(exc: Exception) -> dict:
    return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


@server.tool()
async def html_file_to_pdf(
    input_html_path: str,
    output_pdf_path: str | None = None,
    layout_config: dict | None = None,
    paper_size: str = "A4",
    landscape: bool = False,
    timeout_ms: int = 30000,
) -> dict:
    """Convert an HTML file on disk into a PDF using headless Chromium.

    Args:
        input_html_path: Path to the source .html file.
        output_pdf_path: Target .pdf path. Defaults to the input path with a
            .pdf suffix. Relative paths are allowed and made absolute.
        layout_config: Optional CSS layout overrides (keys: font_size_base,
            line_height, section_gap, element_gap, card_padding,
            content_margin_v, content_margin_h — values are raw CSS sizes,
            e.g. "10.5pt", "1.6", "18mm").
        paper_size: PDF paper format, e.g. "A4" or "Letter". Default "A4".
        landscape: Landscape orientation. Default False.
        timeout_ms: Wait timeout (ms) for page load and network idle.

    Returns:
        {"ok": True, "pdf_path": ..., "bytes": n, ...} on success,
        {"ok": False, "error": "..."} on failure.
    """
    try:
        return await pipeline.file_to_pdf(
            input_html_path=input_html_path,
            output_pdf_path=output_pdf_path,
            layout_config=layout_config,
            paper_size=paper_size,
            landscape=landscape,
            timeout_ms=timeout_ms,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("html_file_to_pdf failed")
        return _error_result(exc)


@server.tool()
async def html_content_to_pdf(
    html: str,
    output_pdf_path: str,
    layout_config: dict | None = None,
    paper_size: str = "A4",
    landscape: bool = False,
    timeout_ms: int = 30000,
) -> dict:
    """Convert raw HTML text (typically agent-authored) into a PDF.

    Remote assets (CDN links for fonts, MathJax/KaTeX, images) are fetched
    with desktop-browser identity headers. A ``<!--DYNAMIC_STYLE_SLOT-->`` or
    ``</head>`` marker receives the injected layout CSS.
    """
    try:
        return await pipeline.content_to_pdf(
            html_content=html,
            output_pdf_path=output_pdf_path,
            layout_config=layout_config,
            paper_size=paper_size,
            landscape=landscape,
            timeout_ms=timeout_ms,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("html_content_to_pdf failed")
        return _error_result(exc)


@server.tool()
async def check_environment() -> dict:
    """Report whether the playwright/Chromium rendering stack is operational."""
    return await pipeline.check_environment()


def main() -> None:
    server.run("stdio")


if __name__ == "__main__":
    main()
