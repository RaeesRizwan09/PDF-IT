# -*- coding: utf-8 -*-
"""
Server-side PDF manipulation toolkit (pure Python, permissively licensed).

Takes PDFs as *URLs* (preferably the pre-signed links returned by our own
html_content_to_pdf / url_to_pdf tools — so LLM chat sessions can chain
operations without file uploads) and returns processed PDF bytes, which the
server uploads to Azure Blob Storage as a fresh pre-signed URL.

Uses pypdf + reportlab only (BSD-3 licenses; no AGPL code).
"""

from __future__ import annotations

import io
import logging
import os
import zipfile
from concurrent.futures import ThreadPoolExecutor

from pypdf import PdfReader, PdfWriter
from reportlab.lib.colors import Color
from reportlab.pdfgen import canvas as rl_canvas

log = logging.getLogger("html2pdf.pdftools")

MAX_PDF_BYTES = int(os.environ.get("PDF_MAX_INPUT_BYTES", str(25 * 1024 * 1024)))
MAX_PAGES = int(os.environ.get("PDF_MAX_PAGES", "500"))

_THREAD_POOL = None  # created lazily in start_thread_pool()


def start_thread_pool() -> ThreadPoolExecutor:
    global _THREAD_POOL
    if _THREAD_POOL is None:
        _THREAD_POOL = ThreadPoolExecutor(max_workers=4)
    return _THREAD_POOL


def pdf_bytes(writer) -> bytes:
    """Serialize a PdfWriter to bytes (pypdf 6.x requires an explicit stream)."""
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


# --- fetch + range parsing ---------------------------------------------------

def public_url(url: str) -> str:
    """Normalize human GitHub URLs into ones a plain GET can download.

    github.com/user/repo/blob/<ref>/path → raw.githubusercontent.com
    gist.github.com/<user>/<id>          → gist raw alias
    Anything else is returned unchanged.
    """
    from urllib.parse import urlparse
    p = urlparse(url)
    if p.scheme not in ("http", "https"):
        return url
    host = (p.netloc or "").lower()
    if host in ("github.com", "www.github.com") and "/blob/" in p.path:
        return "https://raw.githubusercontent.com" + p.path.replace("/blob/", "/")
    if host in ("gist.github.com", "www.gist.github.com") \
            and p.path and not p.path.endswith("/raw"):
        return ("https://gist.githubusercontent.com" + p.path
                + "/raw" + ("?" + p.query if p.query else ""))
    return url


def download_bytes(url: str, timeout_s: float = 60.0) -> bytes:
    """Download a resource from a URL (http/https, pre-signed links welcome)."""
    import httpx2  # provided transitively by the mcp/validation stack

    url = public_url(url)
    if not url.lower().startswith(("http://", "https://")):
        raise ValueError(
            "input must be an http(s):// PUBLIC link (empty or bare filenames "
            "won't work)")
    resp = httpx2.get(url, timeout=timeout_s, follow_redirects=True,
                      headers={"User-Agent": "html2pdf-mcp/1.0"})
    if resp.status_code >= 400:
        raise RuntimeError(f"download failed: HTTP {resp.status_code} for {url}")
    data = resp.content
    if len(data) > MAX_PDF_BYTES:
        raise ValueError(f"file exceeds the {MAX_PDF_BYTES // (1024 * 1024)} MB limit")
    return data


def infer_filename(url: str, default: str = "document") -> str:
    """Best-effort basename from a URL path (no query), unescaped."""
    from urllib.parse import unquote, urlparse
    path = urlparse(url).path
    name = unquote(path.split("/")[-1]) if "/" in path or path else ""
    return name.strip() or default


def fetch_pdf(url: str, password: str | None = None, timeout_s: float = 60.0):
    """Download a PDF from a URL and return a pypdf reader (decrypts if needed)."""
    data = download_bytes(url, timeout_s)
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            if not password:
                raise RuntimeError("PDF is password-protected; pass 'password'")
            if not reader.decrypt(password):
                raise RuntimeError("wrong password")
        return reader
    except RuntimeError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"not a readable PDF: {type(exc).__name__}: {exc}") from exc


def parse_page_ranges(raw: str | None, total: int) -> list[int]:
    """Parse '1-3,5,9-' or 'all' into a 0-based, sorted, de-duplicated list."""
    raw = (raw or "").strip()
    if not raw or raw.lower() == "all":
        return list(range(total))
    picked: set[int] = set()
    try:
        for chunk in raw.replace(" ", "").split(","):
            if not chunk:
                continue
            if "-" in chunk:
                a, b = chunk.split("-", 1)
                begin = int(a) if a else 1
                finish = int(b) if b else total
            else:
                begin = finish = int(chunk)
            begin, finish = max(1, begin), min(total, finish)
            if begin > finish:
                raise ValueError(f"empty range '{chunk}'")
            picked.update(range(begin - 1, finish))
    except ValueError as exc:
        msg = str(exc)
        if "empty range" not in msg and "range" not in msg:
            raise ValueError(
                f"bad page range '{raw}' (use e.g. '1-3,7' or 'all')") from exc
        raise
    if not picked:
        raise ValueError("page range matched no pages")
    return sorted(picked)


# --- text overlays (reportlab) ------------------------------------------------

def _watermark_buf(text: str, page_w: float, page_h: float,
                   opacity: float) -> io.BytesIO:
    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=(page_w, page_h))
    c.setFillColor(Color(0.5, 0.5, 0.5, alpha=opacity))
    c.setFont("Helvetica", max(24, page_h // 20))
    c.saveState()
    c.translate(page_w / 2, page_h / 2)
    c.rotate(45)
    c.drawCentredString(0, 0, text)
    c.restoreState()
    c.save()
    buf.seek(0)
    return buf


def _number_buf(n: int, total: int, page_w: float, page_h: float,
                position: str, start: int, label: str) -> io.BytesIO:
    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=(page_w, page_h))
    c.setFont("Helvetica", 10)
    c.setFillColor(Color(0.2, 0.2, 0.2, alpha=0.85))
    txt = f"{label} {n + start} / {total + start - 1}" if label else \
          f"{n + start} / {total + start - 1}"
    y = page_h - 18 if position.endswith("top") else 14
    if position.startswith("center"):
        c.drawCentredString(page_w / 2, y, txt)
    elif position.startswith("right"):
        c.drawRightString(page_w - 24, y, txt)
    else:
        c.drawString(24, y, txt)
    c.save()
    buf.seek(0)
    return buf


# --- core operations (sync; call via run_sync) --------------------------------

def op_merge(urls: list[str]) -> tuple[bytes, str]:
    writer = PdfWriter()
    for u in urls:
        if not isinstance(u, str) or not u.strip():
            raise ValueError("pdf_urls entries must be non-empty URLs")
        writer.append(fetch_pdf(u.strip()))
    if len(writer.pages) > MAX_PAGES:
        raise ValueError(f"merged document exceeds {MAX_PAGES} pages")
    return pdf_bytes(writer), "application/pdf"


def op_extract(reader, ranges: str) -> tuple[bytes, str]:
    writer = PdfWriter()
    for i in parse_page_ranges(ranges, len(reader.pages)):
        writer.add_page(reader.pages[i])
    return pdf_bytes(writer), "application/pdf"


def op_delete(reader, ranges: str) -> tuple[bytes, str]:
    total = len(reader.pages)
    drop = set(parse_page_ranges(ranges, total))
    writer = PdfWriter()
    for i in range(total):
        if i not in drop:
            writer.add_page(reader.pages[i])
    if len(writer.pages) == 0:
        raise ValueError("cannot delete every page")
    return pdf_bytes(writer), "application/pdf"


def op_rotate(reader, degrees: int, pages: str) -> tuple[bytes, str]:
    degrees = int(degrees) % 360
    if degrees % 90 != 0:
        raise ValueError("degrees must be a multiple of 90")
    idx = set(parse_page_ranges(pages, len(reader.pages)))
    writer = PdfWriter()
    for i, page in enumerate(reader.pages):
        if i in idx:
            page.rotate(degrees)
        writer.add_page(page)
    return pdf_bytes(writer), "application/pdf"


def op_watermark(reader, text: str, opacity: float,
                 watermark_pdf_url: str | None) -> tuple[bytes, str]:
    if not text and not watermark_pdf_url:
        raise ValueError("either 'text' or 'watermark_pdf_url' is required")
    overlay_reader = None
    if watermark_pdf_url:
        orl = fetch_pdf(watermark_pdf_url)
        overlay_reader = orl.pages[0]
    writer = PdfWriter()
    for page in reader.pages:
        if overlay_reader is not None:
            page.merge_page(overlay_reader)
        else:
            ov = _watermark_buf(text, float(page.mediabox.width),
                                float(page.mediabox.height), opacity)
            page.merge_page(PdfReader(ov).pages[0])
        writer.add_page(page)
    return pdf_bytes(writer), "application/pdf"


def op_page_numbers(reader, position: str, start: int,
                    label: str) -> tuple[bytes, str]:
    if position not in ("bottom-left", "bottom-center", "bottom-right",
                        "top-left", "top-center", "top-right"):
        raise ValueError("position must be (top|bottom)-(left|center|right)")
    total = len(reader.pages)
    writer = PdfWriter()
    for i, page in enumerate(reader.pages):
        ov = _number_buf(i, total, float(page.mediabox.width),
                         float(page.mediabox.height), position, start, label)
        page.merge_page(PdfReader(ov).pages[0])
        writer.add_page(page)
    return pdf_bytes(writer), "application/pdf"


def op_metadata(reader, updates: dict | None) -> tuple[dict, bytes | None]:
    meta = {str(k).lstrip("/"): str(v) for k, v in (reader.metadata or {}).items()}
    page_sizes = sorted({f"{int(float(p.mediabox.width))}x{int(float(p.mediabox.height))}"
                         for p in reader.pages})
    info: dict = {
        "pages": len(reader.pages),
        "page_sizes": page_sizes,
        "metadata": meta,
        "encrypted": reader.is_encrypted,
    }
    out = None
    if updates:
        allowed = {"title", "author", "subject", "keywords", "creator", "producer"}
        clean = {}
        for k, v in updates.items():
            if k.lower() in allowed:
                clean[f"/{k.lower().capitalize() if k.lower() != 'title' else 'Title'}"] = str(v)
        if not clean:
            raise ValueError(
                f"set_metadata keys allowed: {sorted(allowed)}")
        writer = PdfWriter()
        writer.append(reader)
        merged = {(k if str(k).startswith("/") else f"/{k}"): v
                  for k, v in (reader.metadata or {}).items()
                  if str(k) in {"/Title", "/Author", "/Subject", "/Keywords",
                                "/Creator", "/Producer"}}
        merged.update(clean)
        writer.add_metadata(merged)
        out = pdf_bytes(writer)
        info["metadata"] = {str(k).lstrip("/"): v for k, v in merged.items()}
    return info, out


def op_text(reader, ranges: str) -> tuple[dict, None]:
    idx = parse_page_ranges(ranges, len(reader.pages))
    chunks = []
    for i in idx:
        body = reader.pages[i].extract_text() or ""
        chunks.append(f"--- page {i + 1} ---\n{body}")
    text = "\n".join(chunks)
    return {"pages": len(idx), "characters": len(text), "text": text}, None


def op_split_zip(reader) -> tuple[bytes, str]:
    if len(reader.pages) > 200:
        raise ValueError("splitting is capped at 200 pages per call")
    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w", zipfile.ZIP_DEFLATED) as zf:
        for i, page in enumerate(reader.pages, 1):
            w = PdfWriter()
            w.add_page(page)
            zf.writestr(f"page-{i:03d}.pdf", pdf_bytes(w))
    return zbuf.getvalue(), "application/zip"


def op_encrypt(reader, user_password: str,
               owner_password: str | None) -> tuple[bytes, str]:
    if len(user_password or "") < 3:
        raise ValueError("password too short (min 3 characters)")
    writer = PdfWriter()
    writer.append(reader)
    writer.encrypt(
        user_password=user_password,
        owner_password=owner_password or user_password,
        algorithm="AES-256",
    )
    return pdf_bytes(writer), "application/pdf"


# --- compression --------------------------------------------------------------
# Levels: light = structural optimize only; medium/strong also downsample &
# re-encode embedded raster images (where the bulk of size usually lives).
_COMPRESS_LEVELS = {
    "light":  {"factor": 1, "quality": None, "min_pixels": 0},
    "medium": {"factor": 2, "quality": 72,  "min_pixels": 64 * 64},
    "strong": {"factor": 4, "quality": 42,  "min_pixels": 16 * 16},
}


def _recompress_images(pdf, factor: int, quality: int, min_pixels: int) -> dict:
    """Downsample + JPEG re-encode embedded images; swap them in the resource
    dicts (pikepdf 10 has no PdfImage.replace). Unprocessable images are left
    untouched; transparency masks are dropped when resapping."""
    import pikepdf
    stats = {"images": 0, "resized": 0, "skipped": 0}
    for page in pdf.pages:
        try:
            xobjects = page.Resources.XObject
        except Exception:  # noqa: BLE001 — pages without resources
            continue
        for name, obj in list(xobjects.items()):
            try:
                if obj.get("/Subtype") != pikepdf.Name("/Image"):
                    continue
                if obj.get("/ImageMask", False):
                    continue
                from pikepdf.models.image import PdfImage
                pil = PdfImage(obj).as_pil_image()
                stats["images"] += 1
                if pil.size[0] * pil.size[1] < min_pixels:
                    stats["skipped"] += 1
                    continue
                w, h = max(1, pil.size[0] // factor), max(1, pil.size[1] // factor)
                from PIL import Image
                pil = pil.resize((w, h), Image.Resampling.BILINEAR)
                if pil.mode not in ("RGB", "L"):
                    pil = pil.convert("RGB")
                buf = io.BytesIO()
                pil.save(buf, format="JPEG", quality=quality, optimize=True)
                new_img = pikepdf.Stream(pdf, buf.getvalue())
                new_img["/Type"] = pikepdf.Name("/XObject")
                new_img["/Subtype"] = pikepdf.Name("/Image")
                new_img["/Width"] = w
                new_img["/Height"] = h
                new_img["/BitsPerComponent"] = 8
                new_img["/ColorSpace"] = (pikepdf.Name("/DeviceRGB")
                                          if pil.mode == "RGB"
                                          else pikepdf.Name("/DeviceGray"))
                new_img["/Filter"] = pikepdf.Name("/DCTDecode")
                page.Resources.XObject[name] = new_img
                stats["resized"] += 1
            except Exception:  # noqa: BLE001 — never fail a compress on one image
                stats["skipped"] += 1
    return stats


def op_compress(data: bytes, level: str) -> tuple[bytes, str, dict]:
    """Optimize a PDF (bytes in, bytes out) at the requested level."""
    import pikepdf

    preset = _COMPRESS_LEVELS.get((level or "").lower())
    if preset is None:
        raise ValueError(f"level must be one of {sorted(_COMPRESS_LEVELS)}")
    stats: dict = {"level": level.lower(), "original_bytes": len(data)}
    with pikepdf.Pdf.open(io.BytesIO(data)) as pdf:
        if preset["quality"] is not None:
            try:
                stats["images"] = _recompress_images(
                    pdf, preset["factor"], preset["quality"],
                    preset["min_pixels"])
            except Exception as exc:  # noqa: BLE001
                stats["image_error"] = str(exc)
        out = io.BytesIO()
        try:
            pdf.save(out, compress_streams=True,
                     object_stream_mode=pikepdf.ObjectStreamMode.generate,
                     normalize_content=False,
                     deduplicate_strings=True, deduplicate_objects=True)
        except TypeError:  # older pikepdf without dedup flags
            out = io.BytesIO()
            pdf.save(out, compress_streams=True,
                     object_stream_mode=pikepdf.ObjectStreamMode.generate)
        compressed = out.getvalue()
    stats["compressed_bytes"] = len(compressed)
    stats["saved_pct"] = max(0, round((1 - len(compressed) / len(data)) * 100, 1))
    return compressed, "application/pdf", stats


# --- office → pdf ---------------------------------------------------------------

OFFICE_EXTS = {".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx",
               ".odt", ".ods", ".odp", ".rtf", ".txt", ".csv"}


def op_office_to_pdf(data: bytes, filename: str,
                     timeout_s: float = 120.0) -> tuple[bytes, str, dict]:
    """Convert an Office document to PDF via headless LibreOffice."""
    import subprocess
    import tempfile
    import shutil
    import uuid

    base = filename.strip().split("/")[-1]
    ext = os.path.splitext(base)[1].lower()
    if ext not in OFFICE_EXTS:
        raise ValueError(
            f"unsupported file type '{ext or '(none)'}' — must be one of "
            f"{sorted(OFFICE_EXTS)} (check the URL's filename or pass one)")
    with tempfile.TemporaryDirectory(prefix="lo-", dir="/tmp") as td:
        src = os.path.join(td, "in" + ext)  # sanitized non-user path
        with open(src, "wb") as fh:
            fh.write(data)
        profile = os.path.join(td, "profile")  # per-call profile = concurrency-safe
        os.makedirs(profile)
        cmd = [
            "soffice", "--headless", "--norestore", "--nologo",
            f"-env:UserInstallation=file://{profile}",
            "--convert-to", "pdf", "--outdir", td, src,
        ]
        env = {k: v for k, v in os.environ.items()}
        env["HOME"] = td
        env.setdefault("FONTCONFIG_PATH", "/etc/fonts")
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=timeout_s,
                                  env=env)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"conversion timed out after {int(timeout_s)}s") from exc
        produced = os.path.join(td, "in.pdf")
        if not os.path.exists(produced):
            tail = (proc.stderr or proc.stdout or b"")[-500:].decode(errors="replace")
            raise RuntimeError(f"LibreOffice conversion failed: {tail or 'no output'}")
        with open(produced, "rb") as fh:
            out = fh.read()
    return out, "application/pdf", {
        "input_type": ext.lstrip("."),
        "input_bytes": len(data),
    }


def op_decrypt(reader, password: str) -> tuple[bytes, str]:
    """Decrypts an already-encrypted `reader` (fetch_pdf handled the password);
    returns a clean unencrypted copy."""
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    return pdf_bytes(writer), "application/pdf"


def run_sync(fn, *args):
    """Run a sync op on the worker pool so the event loop stays responsive."""
    start_thread_pool()
    import asyncio
    return asyncio.get_running_loop().run_in_executor(_THREAD_POOL, fn, *args)
