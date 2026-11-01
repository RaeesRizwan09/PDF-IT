# -*- coding: utf-8 -*-
"""
PDF-IT local stdio MCP server — the FULL toolkit for filesystem-capable agents
(Claude Code, OpenCode, Gemini CLI, …).

Local agents differ from web LLMs in one decisive way: they read and write
real files. So this server removes the URL-land requirement the remote one
lives by:

- inputs: LOCAL PATHS or http(s) URLs, your choice per call
- outputs: files written to disk (default `~/PDF-IT outputs/`) — no Blob,
  no upload page, no expired links
- secrets: none; urls only if you opt into remote sources

Same core code as the remote server (pdftools.py / pipeline.py), so behavior
matches 1:1. Rendering tools need Playwright's Chromium (set
PLAYWRIGHT_BROWSERS_PATH or run `playwright install chromium`); the PDF
manipulation tools need only pypdf/reportlab/pikepdf/Pillow; office_to_pdf
needs LibreOffice installed locally.

Register it (stdio):
  Claude Code : claude mcp add pdf-it -- python3 <repo>/src/local_server.py
                (or .mcp.json — see docs/LOCAL-AGENTS.md)
  OpenCode    : "pdf-it": {"type": "local",
                           "command": ["python3", "<repo>/src/local_server.py"]}
  Gemini CLI  : {"mcpServers": {"pdf-it": {"command": "python3",
                                           "args": ["<repo>/src/local_server.py"]}}}
"""

from __future__ import annotations

import io
import logging
import os
import subprocess
import sys
import uuid
from pathlib import Path

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import pdftools  # noqa: E402
from mcp.server.mcpserver import MCPServer  # noqa: E402
from pypdf import PdfReader, PdfWriter  # noqa: E402

logging.basicConfig(
    stream=sys.stderr,
    level=os.environ.get("HTML2PDF_LOGLEVEL", "INFO"),
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
log = logging.getLogger("pdfit.local")

try:
    import pipeline  # noqa: E402  (Playwright — optional for rendering tools)
    _HAS_PIPELINE = True
except Exception as _exc:  # noqa: BLE001
    pipeline = None
    _HAS_PIPELINE = False
    log.warning("pipeline (Playwright) unavailable: %s — render tools disabled", _exc)

DEFAULT_OUTPUT_DIR = os.environ.get("PDFIT_OUTPUT_DIR", "~/PDF-IT outputs")
WORK_DIR = Path("/tmp/pdfit-local")
WORK_DIR.mkdir(parents=True, exist_ok=True)

server = MCPServer(
    name="pdf-it",
    version="1.0.0",
    instructions=(
        "Full PDF toolkit running LOCALLY on this machine. Give file paths "
        "(preferred) or http(s) URLs. Outputs are written to your disk — "
        "default folder is ~/PDF-IT outputs (override PDFIT_OUTPUT_DIR). "
        "Rendering (html/url → pdf) needs Playwright Chromium; manipulation "
        "tools (compress/merge/split/rotate/…) are pure Python; office "
        "conversion needs LibreOffice installed. Chain freely: every tool "
        "also accepts output files from other tools via path."
    ),
)


# ---------------------------------------------------------------------------
# helpers

def _error_result(exc: Exception) -> dict:
    return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _safe_name(raw: str | None, default_ext: str = ".pdf") -> str:
    import re as _re
    name = (raw or "output" + default_ext).strip().split("/")[-1].split("\\")[-1]
    name = _re.sub(r"[^\w.\- ]+", "", name).strip() or "output" + default_ext
    lower = name.lower()
    allowed = (".pdf", ".zip", ".txt", ".md", ".csv", ".tex", ".html", ".json")
    return name if lower.endswith(allowed) else name + default_ext


def _out_path(filename: str) -> Path:
    outdir = Path(DEFAULT_OUTPUT_DIR).expanduser()
    outdir.mkdir(parents=True, exist_ok=True)
    return outdir / _safe_name(filename)


def get_reader(source: str, password: str | None = None):
    """Read a PDF from a local path OR an http(s) URL → pypdf reader."""
    s = (source or "").strip()
    if s.lower().startswith(("http://", "https://")):
        return pdftools.fetch_pdf(s, password=password)
    p = Path(s).expanduser()
    if not p.exists():
        raise FileNotFoundError(f"no such file: {p} (or pass an http(s):// URL)")
    reader = PdfReader(str(p))
    if reader.is_encrypted:
        if not password:
            raise RuntimeError("PDF is password-protected; pass 'password'")
        if not reader.decrypt(password):
            raise RuntimeError("wrong password")
    return reader


def _deliver_bytes(data: bytes, name: str, ctype: str = "application/pdf") -> dict:
    out = _out_path(name)
    out.write_bytes(data)
    return {"ok": True, "path": str(out.resolve()), "bytes": len(data)}


def _wrap(data: bytes, name: str, ctype: str) -> dict:
    return _deliver_bytes(data, name, ctype)


def _read_pdf_bytes(source: str, password: str | None = None) -> bytes:
    s = (source or "").strip()
    if s.lower().startswith(("http://", "https://")):
        return pdftools.download_bytes(s)
    p = Path(s).expanduser()
    if not p.exists():
        raise FileNotFoundError(f"no such file: {p}")
    data = p.read_bytes()
    if len(data) > pdftools.MAX_PDF_BYTES:
        raise ValueError(f"file exceeds {pdftools.MAX_PDF_BYTES//1024//1024} MB cap")
    return data


# ---------------------------------------------------------------------------
# rendering tools (Playwright)

def _no_pipeline() -> dict:
    return _error_result(RuntimeError(
        "rendering unavailable — install playwright + chromium "
        "(pip install playwright && playwright install chromium)"))


@server.tool()
async def html_content_to_pdf(
    html: str,
    filename: str = "document.pdf",
    layout_config: dict | None = None,
    paper_size: str = "A4",
    landscape: bool = False,
    timeout_ms: int = 45000,
) -> dict:
    """Render HTML (authored here) to a PDF on local disk.

    Args as in the remote server. Returns {ok, path, bytes}.
    """
    if not _HAS_PIPELINE:
        return _no_pipeline()
    try:
        out = _out_path(filename)
        if not html or not html.strip():
            raise ValueError("html is empty")
        await pipeline.content_to_pdf(
            html_content=html, output_pdf_path=str(out),
            layout_config=layout_config, paper_size=paper_size,
            landscape=landscape, timeout_ms=timeout_ms)
        return {"ok": True, "path": str(out.resolve()),
                "bytes": out.stat().st_size}
    except Exception as exc:  # noqa: BLE001
        log.exception("html_content_to_pdf failed")
        return _error_result(exc)


@server.tool()
async def url_to_pdf(
    url: str,
    filename: str = "page.pdf",
    layout_config: dict | None = None,
    paper_size: str = "A4",
    landscape: bool = False,
    timeout_ms: int = 45000,
) -> dict:
    """Capture a live web page (http(s)) as a local PDF."""
    if not _HAS_PIPELINE:
        return _no_pipeline()
    try:
        if not url.startswith(("http://", "https://")):
            raise ValueError("url must be an http(s):// address")
        out = _out_path(filename)
        await pipeline.url_to_pdf(
            url=url, output_pdf_path=str(out), layout_config=layout_config,
            paper_size=paper_size, landscape=landscape, timeout_ms=timeout_ms)
        return {"ok": True, "path": str(out.resolve()),
                "bytes": out.stat().st_size}
    except Exception as exc:  # noqa: BLE001
        log.exception("url_to_pdf failed")
        return _error_result(exc)


@server.tool()
async def office_to_pdf(
    input_path_or_url: str,
    filename: str | None = None,
) -> dict:
    """Convert an Office document (pptx/docx/xlsx/odp/…) to a local PDF via
    LibreOffice (must be installed: `sudo apt install libreoffice`).

    Args: a local file path or http(s) URL; optional output filename.
    """
    try:
        name = (filename or "").strip() or (
            pdftools.infer_filename(input_path_or_url, "")
            or "converted" + Path(input_path_or_url).suffix)
        ext = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""
        if ext not in pdftools.OFFICE_EXTS:
            src = Path(input_path_or_url)
            ext = src.suffix.lower()
            name = src.name or "converted" + ext
        data = _read_pdf_bytes(input_path_or_url)
        out, ctype, stats = pdftools.op_office_to_pdf(data, name)
        result = _wrap(out, Path(name).with_suffix(".pdf").name, ctype)
        result["input_type"] = stats["input_type"]
        return result
    except Exception as exc:  # noqa: BLE001
        log.exception("office_to_pdf failed")
        return _error_result(exc)


@server.tool()
async def check_environment() -> dict:
    """Report what this local install can do (Chromium / toolkit / LibreOffice)."""
    soffice = "missing"
    try:
        subprocess.run(["soffice", "--version"], capture_output=True,
                       timeout=15)
        soffice = "available"
    except Exception:  # noqa: BLE001
        pass
    envinfo = None
    if _HAS_PIPELINE:
        try:
            envinfo = await pipeline.check_environment()
        except Exception:  # noqa: BLE001
            envinfo = None
    return {"ok": True,
            "rendering": (envinfo if envinfo is not None
                          else "pipeline not importable (Playwright missing?)"),
            "pipeline_installed": _HAS_PIPELINE,
            "pdf_toolkit": "available (pypdf/reportlab/pikepdf/Pillow)",
            "office_conversion": f"soffice: {soffice}",
            "output_dir": str(Path(DEFAULT_OUTPUT_DIR).expanduser())}


# ---------------------------------------------------------------------------
# PDF manipulation toolkit (identical semantics to the remote MCP server;
# sources are local paths or URLs, outputs are local files)

@server.tool()
async def pdf_merge(
    pdf_paths_or_urls: list[str],
    filename: str = "merged.pdf",
) -> dict:
    """Merge multiple PDFs (local paths or URLs) into one, on disk.

    Args:
        pdf_paths_or_urls: ordered list of documents; pages concatenated in order.
        filename: output name, e.g. "combined-report.pdf".
    """
    try:
        readers = [await pdftools.run_sync(get_reader, s) for s in pdf_paths_or_urls]
        writer = PdfWriter()
        for r in readers:
            writer.append(r)
        return _wrap(pdf_bytes(writer), filename)
    except Exception as exc:  # noqa: BLE001
        log.exception("pdf_merge failed")
        return _error_result(exc)


def pdf_bytes(writer) -> bytes:
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


@server.tool()
async def pdf_split(
    pdf_path_or_url: str,
    password: str | None = None,
    filename: str = "pages.zip",
) -> dict:
    """Split a PDF (local path or URL) into single-page PDFs → one ZIP on disk."""
    try:
        reader = await pdftools.run_sync(get_reader, pdf_path_or_url, password)
        data, ctype = await pdftools.run_sync(pdftools.op_split_zip, reader)
        return _wrap(data, filename, ctype)
    except Exception as exc:  # noqa: BLE001
        log.exception("pdf_split failed")
        return _error_result(exc)


@server.tool()
async def pdf_extract_pages(
    pdf_path_or_url: str,
    page_ranges: str,
    password: str | None = None,
    filename: str = "extracted.pdf",
) -> dict:
    """Build a new PDF containing chosen pages, e.g. page_ranges "1-3,7,9-"."""
    try:
        reader = await pdftools.run_sync(get_reader, pdf_path_or_url, password)
        data, ctype = await pdftools.run_sync(pdftools.op_extract, reader, page_ranges)
        return _wrap(data, filename, ctype)
    except Exception as exc:  # noqa: BLE001
        log.exception("pdf_extract_pages failed")
        return _error_result(exc)


@server.tool()
async def pdf_delete_pages(
    pdf_path_or_url: str,
    page_ranges: str,
    password: str | None = None,
    filename: str = "trimmed.pdf",
) -> dict:
    """Delete the chosen pages from a PDF (all others are kept)."""
    try:
        reader = await pdftools.run_sync(get_reader, pdf_path_or_url, password)
        data, ctype = await pdftools.run_sync(pdftools.op_delete, reader, page_ranges)
        return _wrap(data, filename, ctype)
    except Exception as exc:  # noqa: BLE001
        log.exception("pdf_delete_pages failed")
        return _error_result(exc)


@server.tool()
async def pdf_rotate(
    pdf_path_or_url: str,
    degrees: int = 90,
    page_ranges: str = "all",
    password: str | None = None,
    filename: str = "rotated.pdf",
) -> dict:
    """Rotate pages by 90/180/270 degrees (clockwise)."""
    try:
        reader = await pdftools.run_sync(get_reader, pdf_path_or_url, password)
        data, ctype = await pdftools.run_sync(
            pdftools.op_rotate, reader, degrees, page_ranges)
        return _wrap(data, filename, ctype)
    except Exception as exc:  # noqa: BLE001
        log.exception("pdf_rotate failed")
        return _error_result(exc)


@server.tool()
async def pdf_watermark(
    pdf_path_or_url: str,
    text: str = "",
    opacity: float = 0.15,
    watermark_pdf_path_or_url: str | None = None,
    password: str | None = None,
    filename: str = "watermarked.pdf",
) -> dict:
    """Stamp every page with a diagonal grey text watermark (DRAFT, …), or
    overlay page 1 of another PDF via watermark_pdf_path_or_url."""
    try:
        reader = await pdftools.run_sync(get_reader, pdf_path_or_url, password)
        ov_reader = (await pdftools.run_sync(
            get_reader, watermark_pdf_path_or_url, None)
            if watermark_pdf_path_or_url else None)
        data, ctype = await pdftools.run_sync(
            pdftools.op_watermark, reader, text, opacity, ov_reader)
        return _wrap(data, filename, ctype)
    except Exception as exc:  # noqa: BLE001
        log.exception("pdf_watermark failed")
        return _error_result(exc)


@server.tool()
async def pdf_page_numbers(
    pdf_path_or_url: str,
    position: str = "bottom-center",
    start: int = 1,
    label: str = "",
    password: str | None = None,
    filename: str = "numbered.pdf",
) -> dict:
    """Add "4 / 12"-style page numbers. position: (top|bottom)-(left|center|right)."""
    try:
        reader = await pdftools.run_sync(get_reader, pdf_path_or_url, password)
        data, ctype = await pdftools.run_sync(
            pdftools.op_page_numbers, reader, position, start, label)
        return _wrap(data, filename, ctype)
    except Exception as exc:  # noqa: BLE001
        log.exception("pdf_page_numbers failed")
        return _error_result(exc)


@server.tool()
async def pdf_compress(
    pdf_path_or_url: str,
    level: str = "medium",
    password: str | None = None,
    filename: str = "compressed.pdf",
) -> dict:
    """Compress a PDF. level: light (structure only) / medium (~150 dpi,
    q72) / strong (~72 dpi, q42). Image-heavy files shrink dramatically;
    text-only files generally don't.

    Returns {ok, path, bytes, original_bytes, saved_pct, level}.
    """
    try:
        data = _read_pdf_bytes(pdf_path_or_url)
        out, ctype, stats = await pdftools.run_sync(
            pdftools.op_compress, data, level)
        result = _wrap(out, filename, ctype)
        result.update(original_bytes=stats["original_bytes"],
                      saved_pct=stats["saved_pct"], level=stats["level"])
        return result
    except Exception as exc:  # noqa: BLE001
        log.exception("pdf_compress failed")
        return _error_result(exc)


@server.tool()
async def pdf_encrypt(
    pdf_path_or_url: str,
    password: str,
    owner_password: str | None = None,
    filename: str = "protected.pdf",
) -> dict:
    """AES-256 encrypt a PDF (user password required to open)."""
    try:
        reader = await pdftools.run_sync(get_reader, pdf_path_or_url, None)
        data, ctype = await pdftools.run_sync(
            pdftools.op_encrypt, reader, password, owner_password)
        return _wrap(data, filename, ctype)
    except Exception as exc:  # noqa: BLE001
        log.exception("pdf_encrypt failed")
        return _error_result(exc)


@server.tool()
async def pdf_decrypt(
    pdf_path_or_url: str,
    password: str,
    filename: str = "unlocked.pdf",
) -> dict:
    """Remove password protection from an encrypted PDF (password must open it)."""
    try:
        reader = await pdftools.run_sync(get_reader, pdf_path_or_url, password)
        data, ctype = await pdftools.run_sync(pdftools.op_decrypt, reader, password)
        return _wrap(data, filename, ctype)
    except Exception as exc:  # noqa: BLE001
        log.exception("pdf_decrypt failed")
        return _error_result(exc)


@server.tool()
async def pdf_metadata(
    pdf_path_or_url: str,
    set_metadata: dict | None = None,
    password: str | None = None,
    filename: str = "updated.pdf",
) -> dict:
    """View or edit metadata (title, author, subject, keywords, creator, producer).
    With set_metadata omitted → info only; otherwise writes a new file too."""
    try:
        reader = await pdftools.run_sync(get_reader, pdf_path_or_url, password)
        info, out = await pdftools.run_sync(pdftools.op_metadata, reader, set_metadata)
        result = {"ok": True, **info}
        if out is not None:
            result.update(_wrap(out, filename))
        return result
    except Exception as exc:  # noqa: BLE001
        log.exception("pdf_metadata failed")
        return _error_result(exc)


@server.tool()
async def pdf_extract_text(
    pdf_path_or_url: str,
    page_ranges: str = "all",
    password: str | None = None,
) -> dict:
    """Extract raw text (per-page) from a local PDF — great for quoting/summarizing."""
    try:
        reader = await pdftools.run_sync(get_reader, pdf_path_or_url, password)
        info, _ = await pdftools.run_sync(pdftools.op_text, reader, page_ranges)
        return {"ok": True, **info}
    except Exception as exc:  # noqa: BLE001
        log.exception("pdf_extract_text failed")
        return _error_result(exc)


def main() -> None:
    server.run("stdio")


if __name__ == "__main__":
    main()
