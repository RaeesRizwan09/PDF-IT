# -*- coding: utf-8 -*-
"""
Remote (streamable-HTTP) html2pdf MCP server.

Runs the shared HTML→PDF pipeline (pipeline.py) as a *hosted* MCP server so
web LLMs (Claude.ai custom connectors, ChatGPT developer-mode connectors)
can use it remotely. Unlike the local stdio server (pdf_server.py):

- Transport is streamable-HTTP (stateless, JSON responses by default).
- PDFs are delivered to the caller instead of written to their disk:
  each result is uploaded to an Azure Blob Storage container and a
  time-limited pre-signed download URL is returned.
- The MCP protocol endpoint lives behind a secret URL path segment
  (e.g. /<token>/mcp) so the public hostname can't be used by strangers —
  the Claude/ChatGPT connector UIs accept full URLs, so this works as
  a lightweight shared secret in place of the API-key/OAuth flows their
  UIs don't support for custom connectors.

Environment
-----------
MCP_PATH_PREFIX                        required — secret path segment; endpoint is
                                       /<MCP_PATH_PREFIX>/mcp
AZURE_STORAGE_CONNECTION_STRING        required — target storage account (must contain
                                       the AZURE_BLOB_CONTAINER container)
AZURE_BLOB_CONTAINER                   default "generated-pdfs"
PDF_URL_TTL_HOURS                      pre-signed URL lifetime, default 24
HTML2PDF_JSON_RESPONSE                 "0" to switch to SSE responses (default JSON)
HTML2PDF_SECURITY_HEADERS              not used (kept for symmetry)
HTML2PDF_LOGLEVEL                      default INFO
PORT                                   listen port, default 8000

Local dev (no Azure available): no connection string is fine — tools then
return ok=true with a "delivered: false" note instead of a download URL, handy
for smoke tests against a laptop.

Run:  python3 http_server.py
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import re
import sys
import uuid
from pathlib import Path

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import pipeline  # noqa: E402
import pdftools  # noqa: E402
from starlette.responses import HTMLResponse, JSONResponse  # noqa: E402
from mcp.server.mcpserver import MCPServer  # noqa: E402

# stderr only — the HTTP response body is the MCP protocol payload.
logging.basicConfig(
    stream=sys.stderr,
    level=os.environ.get("HTML2PDF_LOGLEVEL", "INFO"),
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
log = logging.getLogger("html2pdf.remote")

WORK_DIR = Path("/tmp/html2pdf-render")
WORK_DIR.mkdir(parents=True, exist_ok=True)

PDF_CONTAINER = os.environ.get("AZURE_BLOB_CONTAINER", "generated-pdfs")
URL_TTL_HOURS = max(1, int(os.environ.get("PDF_URL_TTL_HOURS", "24")))
MAX_HTML_BYTES = int(os.environ.get("HTML2PDF_MAX_HTML_BYTES", str(2 * 1024 * 1024)))
LISTEN_PORT = int(os.environ.get("PORT", "8000"))
MAX_UPLOAD_BYTES = int(os.environ.get(
    "UPLOAD_MAX_BYTES", str(50 * 1024 * 1024)))

server = MCPServer(
    name="html2pdf",
    version="0.7.0",
    instructions=(
        "Move documents around as PDFs. Tools: html_content_to_pdf (render HTML "
        "you authored), url_to_pdf (capture a live web page as PDF), "
        "save_text_as_file (store text you authored as a downloadable file), "
        "upload_base64 (≤64 KB binaries with exact bytes available only), and a "
        "PDF toolkit that operates on links returned by these tools: pdf_merge, "
        "pdf_split (single-page ZIP), pdf_extract_pages, pdf_delete_pages, "
        "pdf_rotate, pdf_watermark, pdf_page_numbers, pdf_compress (levels "
        "light/medium/strong), pdf_encrypt (AES-256), pdf_decrypt, "
        "pdf_metadata (view/edit), pdf_extract_text, office_to_pdf (PPTX/"
        "DOCX/XLSX/ODP/…). Returned "
        f"links expire after {URL_TTL_HOURS} hours, so share them promptly. "
        "layout_config keys: font_size_base, line_height, section_gap, "
        "element_gap, card_padding, content_margin_v, content_margin_h (raw "
        "CSS sizes, e.g. \"10.5pt\", \"1.6\", \"18mm\"). Paper sizes like A4 "
        "and Letter; landscape optional. Page ranges look like \"1-3,7,9-\" "
        "(1-based, open end = last page). TOKEN BUDGET RULES — follow "
        "strictly: (1) NEVER echo, quote, or summarize source HTML/PDF "
        "content back to the user; the pdf_url is the deliverable. (2) On "
        "success reply with only a one-line confirmation plus the link. "
        "(3) Chain tools by passing returned pdf_url values as inputs — "
        "never re-download their contents. (4) Compose compact HTML. "
        "(5) Files that only exist in this chat can't be fetched over HTTP: "
        "chat sandboxes may not even reach this host. NEVER try curl/POST "
        "yourself. If a tool complains the input needs a PUBLIC link, give "
        "the user these options: (a) upload at this server's /upload page "
        "(replace the /mcp of this connector's URL with /upload) and paste "
        "the returned link back; (b) host the file anywhere public "
        "(Dropbox/Drive direct links work); (c) if YOU authored the content "
        "(text/markdown/code), skip files entirely and pass it to "
        "html_content_to_pdf as HTML, or publish it via a GitHub Gist — "
        "github.com/gist links are auto-converted to raw downloads. (6) "
        "To give the user a real downloadable file of text YOU authored, "
        "use save_text_as_file instead of dumping the content in chat. "
        "(7) For tiny (≤64 KB) files where you hold exact bytes, "
        "upload_base64 stores them and returns a link other tools accept."
    ),
)


def _error_result(exc: Exception) -> dict:
    msg = f"{type(exc).__name__}: {exc}"
    if "http(s)" in msg and "must be" in msg:
        msg += (" — the file must live behind a PUBLIC link. If it only exists "
                "in this chat or on the user's device, tell the user to upload "
                "it at this server's /upload page: take this connector's URL "
                "and replace the trailing '/mcp' with '/upload'. Then use the "
                "returned pdf_url(esque) link as file input here.")
    return {"ok": False, "error": msg}


def _safe_filename(raw: str | None, keep_any_ext: bool = False) -> str:
    """Sanitize a user-supplied output filename (keep their text, kill paths)."""
    name = (raw or "document.pdf").strip().split("/")[-1].split("\\")[-1]
    name = re.sub(r"[^\w.\- ]+", "", name).strip() or "document.pdf"
    if name.lower().endswith((".pdf", ".zip", ".txt", ".md", ".csv", ".tex",
                              ".html", ".json", ".xml", ".log")):
        return name
    return name + ".pdf"


def _upload_and_get_url(pdf_path: Path, suggested_name: str,
                        content_type: str = "application/pdf") -> tuple[dict | None, Exception | None]:
    """Upload a PDF to Blob Storage and mint a pre-signed read URL.

    Returns (result, None) or (None, exception). If Azure Storage is not
    configured (local dev) returns ({'delivered': False, ...}, None).
    """
    conn = os.environ.get("AZURE_STORAGE_CONNECTION_STRING", "")
    if not conn:
        return (
            {
                "delivered": False,
                "note": (
                    "PDF was rendered but not uploaded: server has no "
                    "AZURE_STORAGE_CONNECTION_STRING. Enable Blob Storage for a "
                    "download URL."
                ),
            },
            None,
        )

    # Keep the user's chosen filename in the blob metadata (nice browser
    # default when saving the file), but use a random unique_key as blob name.
    from azure.storage.blob import (  # imported lazily: local dev may lack it
        BlobSasPermissions,
        BlobServiceClient,
        ContentSettings,
        generate_blob_sas,
    )

    svc = BlobServiceClient.from_connection_string(conn)
    blob_name = f"{dt.date.today():%Y/%m}/{uuid.uuid4().hex}/{suggested_name}"
    client = svc.get_blob_client(container=PDF_CONTAINER, blob=blob_name)
    with pdf_path.open("rb") as fh:
        client.upload_blob(
            fh,
            overwrite=True,
            content_settings=ContentSettings(content_type=content_type),
        )
    client.set_blob_metadata({"filename": suggested_name})

    expiry = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=URL_TTL_HOURS)
    sas = generate_blob_sas(
        account_name=svc.account_name,
        container_name=PDF_CONTAINER,
        blob_name=blob_name,
        account_key=svc.credential.account_key,
        permission=BlobSasPermissions(read=True),
        expiry=expiry,
    )
    return (
        {
            "delivered": True,
            "pdf_url": client.url + "?" + sas,
            "bytes": pdf_path.stat().st_size,
            "expires_at": expiry.strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
        None,
    )


def _deliver(pdf_path: Path, suggested_name: str,
             content_type: str = "application/pdf") -> dict:
    result, exc = _upload_and_get_url(pdf_path, suggested_name, content_type)
    if exc is not None:
        raise RuntimeError(f"upload failed: {exc}") from exc
    return result


def _deliver_bytes(data: bytes, suggested_name: str,
                   content_type: str = "application/pdf") -> dict:
    """Upload in-memory PDF/ZIP bytes and return a pre-signed-URL result."""
    tmp = WORK_DIR / (uuid.uuid4().hex + ("_" + suggested_name))
    try:
        tmp.write_bytes(data)
        return _deliver(tmp, suggested_name, content_type)
    finally:
        tmp.unlink(missing_ok=True)


def _shape(delivered: dict) -> dict:
    """Compact success payload: only what chat LLMs need."""
    return {
        "ok": True,
        "pdf_url": delivered.get("pdf_url"),
        "bytes": delivered.get("bytes"),
        "expires_at": delivered.get("expires_at"),
        **({} if delivered.get("pdf_url") else delivered),
    }


# ---------------------------------------------------------------------------
# Browser upload page (/<token>/upload): lets a USER push any local file
# (theirs, or one sitting in an LLM chat sandbox) into Blob Storage and get
# back a signed link the tools can consume. POST accepts the raw file bytes
# (?name=deck.pptx); GET serves a minimal drag-and-drop page.
# ---------------------------------------------------------------------------

UPLOAD_CONTENT_TYPES = {
    ".pdf": "application/pdf",
    ".zip": "application/zip",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".odt": "application/vnd.oasis.opendocument.text",
    ".ods": "application/vnd.oasis.opendocument.spreadsheet",
    ".odp": "application/vnd.oasis.opendocument.presentation",
    ".rtf": "application/rtf",
    ".txt": "text/plain",
    ".csv": "text/csv",
}


def _upload_ct(filename: str) -> str:
    from pathlib import PurePath
    return UPLOAD_CONTENT_TYPES.get(PurePath(filename).suffix.lower(),
                                    "application/octet-stream")


def _accept_name(raw: str | None) -> str:
    name = (raw or "").strip().split("/")[-1].split("\\")[-1]
    name = re.sub(r"[^\w.\- ]+", "_", name).strip() or "upload.bin"
    return name[:120]


async def _upload_handler(request):
    if request.method == "GET":
        return HTMLResponse(_UPLOAD_PAGE_HTML)
    name = _accept_name(request.query_params.get("name"))
    chunks: list[bytes] = []
    received = 0
    async for chunk in request.stream():
        received += len(chunk)
        if received > MAX_UPLOAD_BYTES:
            return JSONResponse(
                {"ok": False,
                 "error": f"file exceeds the {MAX_UPLOAD_BYTES // (1024*1024)} MB limit"},
                status_code=413)
        chunks.append(chunk)
    data = b"".join(chunks)
    if not data:
        return JSONResponse({"ok": False, "error": "empty body — POST the raw file"},
                            status_code=400)
    tmp = WORK_DIR / (uuid.uuid4().hex + "_" + name)
    out_path = WORK_DIR / (uuid.uuid4().hex + "_stored_" + name)
    try:
        tmp.write_bytes(data)
        tmp.rename(out_path)
        delivered, exc = _upload_and_get_url(out_path, name, _upload_ct(name))
    except Exception as exc2:  # noqa: BLE001
        log.exception("store failed")
        return JSONResponse({"ok": False, "error": f"store failed: {exc2}"},
                            status_code=500)
    finally:
        tmp.unlink(missing_ok=True)
        out_path.unlink(missing_ok=True)
    if exc is not None:
        log.exception("upload deliver failed")
        return JSONResponse({"ok": False, "error": f"store failed: {exc}"},
                            status_code=500)
    return JSONResponse({
        "ok": True,
        "filename": name,
        "pdf_url": delivered.get("pdf_url"),
        "bytes": delivered.get("bytes"),
        "expires_at": delivered.get("expires_at"),
    })


_UPLOAD_PAGE_HTML = """<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Upload a file — html2pdf MCP</title>
<style>
 body{font:16px/1.5 system-ui,sans-serif;max-width:640px;margin:2rem auto;padding:0 1rem}
 #drop{border:2px dashed #889;border-radius:12px;padding:2.5rem 1rem;text-align:center;cursor:pointer}
 #drop.over{border-color:#2a7;background:#eefcf3}
 #link{display:none;margin-top:1rem;word-break:break-all;background:#f4f7ff;padding:.8rem;border-radius:8px}
 button{font:inherit;padding:.4rem 1rem;border-radius:8px;border:1px solid #99a;cursor:pointer}
 code{background:#eee;padding:.1rem .3rem;border-radius:4px}
</style></head>
<body>
<h1>Upload a file</h1>
<p>Drops the file into secure storage and gives you a temporary
(<b>24 h</b>) download link you can paste into your chat.</p>
<div id="drop">Click or drop a file here<br>
<small>PDF, DOCX/DOC, PPTX/PPT, XLSX/XLS, ODT/ODS/ODP, RTF, TXT, CSV, ZIP</small></div>
<input id="f" type="file" hidden>
<div id="link"><b>Link:</b> <span id="u"></span>
 <br><button onclick="navigator.clipboard.writeText(document.getElementById('u').textContent)">Copy</button>
 <button onclick="location.reload()">Upload next file</button></div>
<script>
const drop = document.getElementById('drop'), fi = document.getElementById('f');
drop.onclick = () => fi.click();
drop.ondragover = e => {e.preventDefault(); drop.classList.add('over');};
drop.ondragleave = () => drop.classList.remove('over');
drop.ondrop = e => {e.preventDefault(); drop.classList.remove('over');
  if (e.dataTransfer.files[0]) go(e.dataTransfer.files[0]);};
fi.onchange = () => fi.files[0] && go(fi.files[0]);
async function go(file){
  drop.textContent = 'Uploading "' + file.name + '" (' + file.size + ' bytes)…';
  const r = await fetch('?name=' + encodeURIComponent(file.name),
                        {method:'POST', body: file});
  const j = await r.json();
  if (j.ok){ document.getElementById('u').textContent = j.pdf_url;
    document.getElementById('link').style.display = 'block';
    drop.textContent = '✔ Stored — link below'; }
  else drop.textContent = '✖ ' + (j.error || 'upload failed');
}
</script></body></html>"""


@server.tool()
async def html_content_to_pdf(
    html: str,
    filename: str = "document.pdf",
    layout_config: dict | None = None,
    paper_size: str = "A4",
    landscape: bool = False,
    timeout_ms: int = 45000,
) -> dict:
    """Convert raw HTML text (your own stylesheet-side content) into a PDF.

    Use this to turn an HTML document/report/card you authored into a
    downloadable PDF. Headless Chromium renders it: KaTeX/MathJax equations
    are typeset, web fonts load, broken remote images are replaced with a
    neat placeholder.

    Args:
        html: The HTML document (a full document or a body fragment; a
            <style> block can be embedded or omitted).
        filename: Preferred download name, e.g. "quarterly-report.pdf".
        layout_config: Optional CSS overrides (font_size_base, line_height,
            section_gap, element_gap, card_padding, content_margin_v,
            content_margin_h).
        paper_size: e.g. "A4", "Letter", "Legal". Default "A4".
        landscape: Landscape orientation. Default false.
        timeout_ms: Page/network-idle wait budget. Default 45000.

    Returns:
        {"ok": true, "pdf_url": "<pre-signed download link>", "bytes": n,
         "expires_at": "<iso>", ...} on success (pdf_url present when Blob
        Storage is configured), else {"ok": false, "error": "..."}.
    """
    suggested_name = _safe_filename(filename)
    out_path = WORK_DIR / (uuid.uuid4().hex + ".pdf")
    try:
        if not html or not html.strip():
            raise ValueError("html is empty")
        if len(html.encode("utf-8")) > MAX_HTML_BYTES:
            raise ValueError(
                f"html exceeds the {MAX_HTML_BYTES // 1024} KB limit; trim inline "
                "assets (e.g. base64 images) and reference remote URLs instead"
            )
        await pipeline.content_to_pdf(
            html_content=html,
            output_pdf_path=str(out_path),
            layout_config=layout_config,
            paper_size=paper_size,
            landscape=landscape,
            timeout_ms=timeout_ms,
        )
        delivered = _deliver(out_path, suggested_name)
        return _shape(delivered)
    except Exception as exc:  # noqa: BLE001
        log.exception("html_content_to_pdf failed")
        return _error_result(exc)
    finally:
        out_path.unlink(missing_ok=True)


@server.tool()
async def url_to_pdf(
    url: str,
    filename: str = "page.pdf",
    layout_config: dict | None = None,
    paper_size: str = "A4",
    landscape: bool = False,
    timeout_ms: int = 45000,
) -> dict:
    """Capture a public web page as a PDF.

    Navigate to `url` (https://... only), wait for fonts/images/MathJax to
    settle, and print the page to PDF the way a desktop browser would.

    Args:
        url: Fully-qualified public URL, e.g. "https://example.com/article".
        filename: Preferred download name, e.g. "example-article.pdf".
        layout_config: Optional CSS overrides (same keys as above).
        paper_size: e.g. "A4", "Letter". Default "A4".
        landscape: Landscape orientation. Default false.
        timeout_ms: Page/network-idle wait budget. Default 45000.

    Returns: same shape as html_content_to_pdf.
    """
    suggested_name = _safe_filename(filename)
    out_path = WORK_DIR / (uuid.uuid4().hex + ".pdf")
    try:
        if not url or not url.startswith(("http://", "https://")):
            raise ValueError("url must be an http(s):// address")
        await pipeline.url_to_pdf(
            url=url,
            output_pdf_path=str(out_path),
            layout_config=layout_config,
            paper_size=paper_size,
            landscape=landscape,
            timeout_ms=timeout_ms,
        )
        delivered = _deliver(out_path, suggested_name)
        return _shape(delivered)
    except Exception as exc:  # noqa: BLE001
        log.exception("url_to_pdf failed")
        return _error_result(exc)
    finally:
        out_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# PDF manipulation toolkit (inputs/outputs are URLs; see src/pdftools.py)
# ---------------------------------------------------------------------------

@server.tool()
async def pdf_merge(
    pdf_urls: list[str],
    filename: str = "merged.pdf",
) -> dict:
    """Merge multiple PDFs (given as http(s) links) into one document.

    Args:
        pdf_urls: Ordered list of PDF URLs; pages are concatenated in order.
        filename: Preferred download name, e.g. "combined-report.pdf".

    Returns: {"ok": true, "pdf_url": "<link>", "bytes": n, "expires_at": ...}.
    """
    try:
        data, ctype = await pdftools.run_sync(pdftools.op_merge, pdf_urls)
        return _shape(_deliver_bytes(data, _safe_filename(filename), ctype))
    except Exception as exc:  # noqa: BLE001
        log.exception("pdf_merge failed")
        return _error_result(exc)


@server.tool()
async def pdf_split(
    pdf_url: str,
    filename: str = "pages.zip",
) -> dict:
    """Split a PDF into single-page PDFs, delivered as one ZIP archive.

    Args:
        pdf_url: Link to the source PDF.
        filename: Preferred ZIP name, e.g. "chapter-pages.zip".

    Returns: {"ok": true, "pdf_url": "<zip link>", "pages": n, ...}.
    """
    try:
        reader = await pdftools.run_sync(pdftools.fetch_pdf, pdf_url)
        data, ctype = await pdftools.run_sync(pdftools.op_split_zip, reader)
        return _shape(_deliver_bytes(data, _safe_filename(filename), ctype))
    except Exception as exc:  # noqa: BLE001
        log.exception("pdf_split failed")
        return _error_result(exc)


@server.tool()
async def pdf_extract_pages(
    pdf_url: str,
    page_ranges: str,
    filename: str = "extracted.pdf",
) -> dict:
    """Build a new PDF containing the chosen pages of an existing one.

    Args:
        pdf_url: Link to the source PDF.
        page_ranges: e.g. "1-3,7,9-" (1-based; open end means last page).
        filename: Preferred download name.

    Returns: {"ok": true, "pdf_url": "<link>", ...}.
    """
    try:
        reader = await pdftools.run_sync(pdftools.fetch_pdf, pdf_url)
        data, ctype = await pdftools.run_sync(
            pdftools.op_extract, reader, page_ranges)
        return _shape(_deliver_bytes(data, _safe_filename(filename), ctype))
    except Exception as exc:  # noqa: BLE001
        log.exception("pdf_extract_pages failed")
        return _error_result(exc)


@server.tool()
async def pdf_delete_pages(
    pdf_url: str,
    page_ranges: str,
    filename: str = "trimmed.pdf",
) -> dict:
    """Delete the chosen pages from a PDF (all others are kept).

    Args: same as pdf_extract_pages (page_ranges selects pages to REMOVE).
    """
    try:
        reader = await pdftools.run_sync(pdftools.fetch_pdf, pdf_url)
        data, ctype = await pdftools.run_sync(
            pdftools.op_delete, reader, page_ranges)
        return _shape(_deliver_bytes(data, _safe_filename(filename), ctype))
    except Exception as exc:  # noqa: BLE001
        log.exception("pdf_delete_pages failed")
        return _error_result(exc)


@server.tool()
async def pdf_rotate(
    pdf_url: str,
    degrees: int = 90,
    page_ranges: str = "all",
    filename: str = "rotated.pdf",
) -> dict:
    """Rotate pages by 90/180/270 degrees.

    Args:
        pdf_url: Link to the source PDF.
        degrees: One of 90, 180, 270 (clockwise).
        page_ranges: "all" or e.g. "1,4-6". Preserves form fields/bookmarks.
    """
    try:
        reader = await pdftools.run_sync(pdftools.fetch_pdf, pdf_url)
        data, ctype = await pdftools.run_sync(
            pdftools.op_rotate, reader, degrees, page_ranges)
        return _shape(_deliver_bytes(data, _safe_filename(filename), ctype))
    except Exception as exc:  # noqa: BLE001
        log.exception("pdf_rotate failed")
        return _error_result(exc)


@server.tool()
async def pdf_watermark(
    pdf_url: str,
    text: str = "",
    opacity: float = 0.15,
    watermark_pdf_url: str | None = None,
    filename: str = "watermarked.pdf",
) -> dict:
    """Stamp every page with a diagonal grey text watermark (DRAFT, CONFIDENTIAL...).

    Args:
        pdf_url: Link to the source PDF.
        text: Watermark text (ignored if watermark_pdf_url given).
        opacity: 0..1, default 0.15.
        watermark_pdf_url: Optionally overlay page 1 of another PDF instead
            (for image or styled watermarks).
    """
    try:
        reader = await pdftools.run_sync(pdftools.fetch_pdf, pdf_url)
        data, ctype = await pdftools.run_sync(
            pdftools.op_watermark, reader, text, opacity, watermark_pdf_url)
        return _shape(_deliver_bytes(data, _safe_filename(filename), ctype))
    except Exception as exc:  # noqa: BLE001
        log.exception("pdf_watermark failed")
        return _error_result(exc)


@server.tool()
async def pdf_page_numbers(
    pdf_url: str,
    position: str = "bottom-center",
    start: int = 1,
    label: str = "",
    filename: str = "numbered.pdf",
) -> dict:
    """Add "4 / 12"-style page numbers to every page.

    Args:
        pdf_url: Link to the source PDF.
        position: (top|bottom)-(left|center|right). Default bottom-center.
        start: First page number, default 1.
        label: Optional prefix, e.g. "Page".
    """
    try:
        reader = await pdftools.run_sync(pdftools.fetch_pdf, pdf_url)
        data, ctype = await pdftools.run_sync(
            pdftools.op_page_numbers, reader, position, start, label)
        return _shape(_deliver_bytes(data, _safe_filename(filename), ctype))
    except Exception as exc:  # noqa: BLE001
        log.exception("pdf_page_numbers failed")
        return _error_result(exc)


@server.tool()
async def pdf_encrypt(
    pdf_url: str,
    password: str,
    owner_password: str | None = None,
    filename: str = "protected.pdf",
) -> dict:
    """Encrypt a PDF with AES-256 (user password required to open).

    Args:
        pdf_url: Link to the source PDF.
        password: Password the recipient will need to open the file.
        owner_password: Optional stronger admin password (defaults to `password`).
    """
    try:
        reader = await pdftools.run_sync(pdftools.fetch_pdf, pdf_url)
        data, ctype = await pdftools.run_sync(
            pdftools.op_encrypt, reader, password, owner_password)
        return _shape(_deliver_bytes(data, _safe_filename(filename), ctype))
    except Exception as exc:  # noqa: BLE001
        log.exception("pdf_encrypt failed")
        return _error_result(exc)


@server.tool()
async def pdf_decrypt(
    pdf_url: str,
    password: str,
    filename: str = "unlocked.pdf",
) -> dict:
    """Remove password protection from a PDF (password must authorize it).

    Args:
        pdf_url: Link to the protected PDF.
        password: The PDF's open/owner password.
    """
    try:
        reader = await pdftools.run_sync(
            pdftools.fetch_pdf, pdf_url, password)
        data, ctype = await pdftools.run_sync(pdftools.op_decrypt, reader, password)
        return _shape(_deliver_bytes(data, _safe_filename(filename), ctype))
    except Exception as exc:  # noqa: BLE001
        log.exception("pdf_decrypt failed")
        return _error_result(exc)


@server.tool()
async def pdf_metadata(
    pdf_url: str,
    set_metadata: dict | None = None,
    filename: str = "updated.pdf",
) -> dict:
    """View or edit PDF metadata (title, author, subject, keywords, creator).

    Args:
        pdf_url: Link to the source PDF.
        set_metadata: Omit to only read. Edit with e.g.
            {"title": "Q3 Report", "author": "Ana"}.

    Returns: with no set_metadata → {"ok":true,"pages":n,"page_sizes":[..],
        "metadata":{...}}; otherwise also the new pdf_url.
    """
    try:
        reader = await pdftools.run_sync(pdftools.fetch_pdf, pdf_url)
        info, out = await pdftools.run_sync(
            pdftools.op_metadata, reader, set_metadata)
        if out is None:
            return {"ok": True, **info}
        return {"ok": True, **info, **_shape(_deliver_bytes(out, _safe_filename(filename)))}
    except Exception as exc:  # noqa: BLE001
        log.exception("pdf_metadata failed")
        return _error_result(exc)


@server.tool()
async def pdf_extract_text(
    pdf_url: str,
    page_ranges: str = "all",
) -> dict:
    """Extract text from a PDF — great for summarizing or quoting documents.

    Args:
        pdf_url: Link to the source PDF.
        page_ranges: "all" or e.g. "1-5".

    Returns: {"ok": true, "pages": n, "characters": n, "text": "..."}.
    """
    try:
        reader = await pdftools.run_sync(pdftools.fetch_pdf, pdf_url)
        info, _ = await pdftools.run_sync(pdftools.op_text, reader, page_ranges)
        return {"ok": True, **info}
    except Exception as exc:  # noqa: BLE001
        log.exception("pdf_extract_text failed")
        return _error_result(exc)


MAX_BASE64_UPLOAD_BYTES = 64 * 1024  # decoded cap — LLM output budgets are small


@server.tool()
async def save_text_as_file(
    text: str,
    filename: str = "notes.txt",
) -> dict:
    """Store text the model authored (markdown, CSV, code, .tex, …) as a real
    downloadable file and return its temporary link.

    Args:
        text: The full text content (written verbatim by the model, ≤2 MB).
        filename: e.g. "report.md", "data.csv", "main.tex".

    Returns: {"ok": true, "file_url": "<link>", "bytes": n, "expires_at": …}.
    """
    try:
        if not text:
            raise ValueError("text is empty")
        if len(text.encode("utf-8")) > MAX_HTML_BYTES:
            raise ValueError(
                f"text exceeds the {MAX_HTML_BYTES // 1024} KB limit")
        safe = _safe_filename(filename)
        delivered = _deliver_bytes(text.encode("utf-8"), safe,
                                   _upload_ct(safe))
        result = _shape(delivered)
        result["file_url"] = result.get("pdf_url")
        return result
    except Exception as exc:  # noqa: BLE001
        log.exception("save_text_as_file failed")
        return _error_result(exc)


@server.tool()
async def upload_base64(
    file_base64: str,
    filename: str = "upload.bin",
) -> dict:
    """Store a SMALL file (≤64 KB decoded) provided as base64 and return a
    signed link all other tools can consume.

    Only call this if you have the file's EXACT bytes available (e.g. from a
    desktop client that read the file into context) — chat-attached PDFs are
    NOT available as bytes, so connecting files usually must go through the
    user's /upload page instead.

    Args:
        file_base64: Standard base64 (may include data-URI prefix/whitespace).
        filename: Name for the stored file, e.g. "chart.pdf", "tiny.zip".

    Returns: {"ok": true, "pdf_url": "<link>", "bytes": n, "expires_at": …}.
    """
    try:
        import base64 as _b64
        raw = (file_base64 or "").strip()
        if raw.startswith("data:") and "," in raw:
            raw = raw.split(",", 1)[1]
        raw = re.sub(r"\s+", "", raw)
        try:
            data = _b64.b64decode(raw, validate=False)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"not valid base64: {exc}") from exc
        if len(data) > MAX_BASE64_UPLOAD_BYTES:
            raise ValueError(
                f"decoded size {len(data)} B exceeds the "
                f"{MAX_BASE64_UPLOAD_BYTES // 1024} KB cap for base64 "
                "uploads — have the user use the /upload page instead")
        delivered = _deliver_bytes(data, _safe_filename(filename),
                                   _upload_ct(filename))
        return _shape(delivered)
    except Exception as exc:  # noqa: BLE001
        log.exception("upload_base64 failed")
        return _error_result(exc)


@server.tool()
async def pdf_compress(
    pdf_url: str,
    level: str = "medium",
    filename: str = "compressed.pdf",
) -> dict:
    """Compress a PDF at a chosen level and return the smaller file.

    Args:
        pdf_url: Link to the source PDF.
        level: "light" = structural optimization only (safe, modest);
            "medium" (default) = downsample images to ~150 dpi at JPEG q72;
            "strong" = ~72 dpi, q42 (best for scans/photo-heavy decks).
            Text-only PDFs barely shrink at any level; image-heavy ones often
            shrink 50–95%.
        filename: Preferred download name.

    Returns: {"ok": true, "pdf_url": ..., "bytes": n, "original_bytes": n,
        "saved_pct": n, "level": "..."}.
    """
    try:
        data = await pdftools.run_sync(pdftools.download_bytes, pdf_url)
        out, ctype, stats = await pdftools.run_sync(
            pdftools.op_compress, data, level)
        result = _shape(_deliver_bytes(out, _safe_filename(filename), ctype))
        result["original_bytes"] = stats["original_bytes"]
        result["saved_pct"] = stats["saved_pct"]
        result["level"] = stats["level"]
        return result
    except Exception as exc:  # noqa: BLE001
        log.exception("pdf_compress failed")
        return _error_result(exc)


@server.tool()
async def office_to_pdf(
    input_url: str,
    filename: str | None = None,
) -> dict:
    """Convert an Office document (PPTX/DOCX/XLSX/…) to PDF via LibreOffice.

    Args:
        input_url: Direct http(s) link to the document (any public URL works,
            including OneDrive/Drive/any website directdownload links).
        filename: Optional preferred download name; by default it is taken
            from the URL's last path segment.

    Supported types: .doc .docx .ppt .pptx .xls .xlsx .odt .ods .odp
    .rtf .txt .csv.

    Returns: {"ok": true, "pdf_url": ..., "bytes": n, "input_type": "pptx"}.
    """
    try:
        name = (filename or "").strip() or pdftools.infer_filename(
            input_url, "converted.docx")
        data = await pdftools.run_sync(pdftools.download_bytes, input_url)
        out, ctype, stats = await pdftools.run_sync(
            pdftools.op_office_to_pdf, data, name)
        result = _shape(_deliver_bytes(
            out, _safe_filename(os.path.splitext(name)[0] + ".pdf"), ctype))
        result["input_type"] = stats["input_type"]
        return result
    except Exception as exc:  # noqa: BLE001
        log.exception("office_to_pdf failed")
        return _error_result(exc)


@server.tool()
async def check_environment() -> dict:
    """Report the rendering stack and PDF-delivery configuration."""
    try:
        info = await pipeline.check_environment()
    except Exception as exc:  # noqa: BLE001
        log.exception("check_environment failed")
        return _error_result(exc)
    info["blob_configured"] = bool(os.environ.get("AZURE_STORAGE_CONNECTION_STRING"))
    info["blob_container"] = PDF_CONTAINER if info["blob_configured"] else None
    info["pdf_url_ttl_hours"] = URL_TTL_HOURS
    return info


def main() -> None:
    # Secret path segment: /<token>/mcp. Falls back to plain /mcp for local dev.
    prefix = (os.environ.get("MCP_PATH_PREFIX") or "").strip("/")
    if not prefix:
        log.warning("MCP_PATH_PREFIX not set — endpoint is UNAUTHENTIC /mcp (dev only)")
        http_path = "/mcp"
        upload_path = "/upload"
    else:
        http_path = f"/{prefix}/mcp"
        upload_path = f"/{prefix}/upload"

    json_response = os.environ.get("HTML2PDF_JSON_RESPONSE", "1") != "0"

    # Build the SDK's Starlette app ourselves so we can mount the browser
    # upload page alongside the MCP route (server.run() offers no hooks).
    # host must match the earlier server.run() behavior (0.0.0.0) — the SDK's
    # DNS-rebinding protection derives allowed Host headers from it.
    starlette_app = server.streamable_http_app(
        streamable_http_path=http_path,
        json_response=json_response,
        stateless_http=True,
        host="0.0.0.0",
    )
    from starlette.routing import Route
    starlette_app.routes.append(Route(
        upload_path, _upload_handler, methods=["GET", "POST"]))

    import uvicorn
    log.info("Serving MCP at %s and upload page at %s", http_path, upload_path)
    uvicorn.run(starlette_app, host="0.0.0.0", port=LISTEN_PORT,
                log_level=os.environ.get("HTML2PDF_LOGLEVEL", "info").lower())


if __name__ == "__main__":
    main()
