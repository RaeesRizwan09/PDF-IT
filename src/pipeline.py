# -*- coding: utf-8 -*-
"""
Universal HTML-to-PDF rendering pipeline.

Cleaned for local (non-Colab) use from the original ``~/Downloads/playwright.py``.

Capabilities (inherited from the original pipeline):
- KaTeX / MathJax equations  (waits for MathJax typesetPromise)
- Google Fonts & custom web fonts (waits for document.fonts.ready)
- Local & remote images (broken / 404 images get a clean fallback box)
- Anti-bot / CDN header simulation (desktop UA + Accept headers)
- Optional layout scaling & CSS margin customization via ``layout_config``
- File-based conversion navigates via ``file://`` so relative assets
  (e.g. ``<img src="images/logo.png">``) actually resolve.

Logging goes to stderr only. Never stdout: stdout is the MCP protocol channel.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from urllib.request import pathname2url

import playwright
from playwright.async_api import async_playwright

log = logging.getLogger("html2pdf")

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0.0.0 Safari/537.36"
)

_EXTRA_HEADERS = {
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
}

# Wait for fonts, sanitize broken images with a fallback box, then typeset MathJax.
# Substantially the same in-page script as the original pipeline.
_WAIT_AND_SANITIZE_JS = """
    async () => {
        // A. Wait for all fonts (Google Fonts, KaTeX fonts, system fonts)
        await document.fonts.ready;

        // B. Handle all images (valid images render; 404s/broken URLs get clean fallbacks)
        const imgPromises = Array.from(document.images).map(img => {
            return new Promise(resolve => {
                const checkWidth = () => {
                    if (img.naturalWidth === 0) sanitize(img);
                    resolve();
                };
                const sanitize = (el) => {
                    if (!el.parentNode) { resolve(); return; }
                    el.style.display = 'none';
                    const box = document.createElement('div');
                    box.style.cssText = 'background:#f1f5f9;color:#64748b;border:1px dashed #cbd5e1;padding:12px;border-radius:6px;text-align:center;font-size:9pt;margin:10px 0;';
                    box.innerHTML = '📷 <i>Asset unavailable: ' + (el.alt || 'Image') + '</i>';
                    el.parentNode.insertBefore(box, el);
                };

                if (img.complete) {
                    checkWidth();
                } else {
                    img.onload = checkWidth;
                    img.onerror = () => { sanitize(img); resolve(); };
                    setTimeout(resolve, 4000);
                }
            });
        });
        await Promise.all(imgPromises);

        // C. Handle MathJax async typesetting (if present)
        if (window.MathJax && window.MathJax.typesetPromise) {
            try {
                await window.MathJax.typesetPromise();
            } catch (e) {
                console.error('MathJax typesetting failed: ' + e);
            }
        }
    }
"""


def _path_to_file_url(path: Path) -> str:
    """Convert a local filesystem path to a file:// URL (handles spaces etc.)."""
    return "file://" + pathname2url(str(path.resolve()))


def _dynamic_css(layout_config: dict | None, paper_size: str, landscape: bool) -> str:
    """Build the injected CSS block from the user's layout_config.

    Same behavior as the original pipeline's layout injection, with the
    @page size now following the requested paper size.
    """
    cfg = layout_config or {}
    size = f"{paper_size} landscape" if landscape else paper_size
    return f"""
    <style>
        :root {{
            --font-size-base: {cfg.get('font_size_base', '10.5pt')};
            --line-height:    {cfg.get('line_height', '1.6')};
            --section-gap:    {cfg.get('section_gap', '20px')};
            --element-gap:    {cfg.get('element_gap', '12px')};
            --card-padding:   {cfg.get('card_padding', '14px')};
        }}
        @page {{
            size: {size};
            margin: {cfg.get('content_margin_v', '18mm')} {cfg.get('content_margin_h', '15mm')};
        }}
    </style>
    """


def _inject_style(html: str, css: str) -> str:
    """Inject CSS into HTML respecting the original DYNAMIC_STYLE_SLOT contract."""
    if "<!--DYNAMIC_STYLE_SLOT-->" in html:
        return html.replace("<!--DYNAMIC_STYLE_SLOT-->", css)
    if "</head>" in html:
        return html.replace("</head>", f"{css}\n</head>", 1)
    # Headless fragment (no <head> tag): prepend so it still applies.
    return css + "\n" + html


def _resolve_output_path(raw_output: str | None, input_file: Path | None) -> Path:
    """Normalize an output path: expand ~, enforce .pdf suffix, create parent dir."""
    if raw_output is None:
        if input_file is None:
            raise ValueError("output_pdf_path is required for content-based conversion")
        out_path = input_file.with_suffix(".pdf")
    else:
        out_path = Path(raw_output).expanduser()
        if out_path.exists() and out_path.is_dir():
            stem = input_file.stem if input_file else "document"
            out_path = out_path / f"{stem}.pdf"
        elif out_path.suffix.lower() != ".pdf":
            out_path = out_path.with_name(out_path.name + ".pdf")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    return out_path


async def _navigate(page, url: str | None, html: str, timeout_ms: int) -> None:
    """Load content into the page, tolerating slow 'networkidle' pages.

    networkidle can hang forever on pages with long-polling sockets; in that
    case we keep whatever has already rendered instead of failing the job.
    """
    try:
        if url is not None:
            await page.goto(url, wait_until="networkidle", timeout=timeout_ms)
        else:
            await page.set_content(html, wait_until="networkidle", timeout=timeout_ms)
    except Exception as exc:  # noqa: BLE001
        log.warning("networkidle wait aborted after %sms, continuing with loaded state: %s", timeout_ms, exc)


async def _finalize_page(page, buffer_ms: int = 500) -> None:
    """Run the in-page waiter/sanitizer script, then a small paint buffer."""
    try:
        await page.evaluate(_WAIT_AND_SANITIZE_JS)
    except Exception as exc:  # noqa: BLE001
        log.warning("finalize step failed (printing anyway): %s", exc)
    await page.wait_for_timeout(buffer_ms)


async def _print_pdf(
    page,
    out_path: Path,
    paper_size: str,
    landscape: bool,
) -> int:
    """Sprint the page to PDF. Returns the file size in bytes."""
    await page.pdf(
        path=str(out_path),
        format=paper_size,
        landscape=landscape,
        print_background=True,
        margin={"top": "0mm", "bottom": "0mm", "left": "0mm", "right": "0mm"},
    )
    return out_path.stat().st_size


# Serialize renders: one browser at a time keeps memory bounded and avoids
# flaky parallel rendering headless Chromium can exhibit.
_RENDER_LOCK = asyncio.Lock()


async def _launch(p) -> "playwright.async_api.Browser":
    """Launch headless Chromium, falling back to the full binary when the
    dedicated headless-shell download is missing."""
    try:
        return await p.chromium.launch(headless=True)
    except Exception as exc:  # noqa: BLE001
        marker = "headless_shell" in str(exc) or "headless-shell" in str(exc)
        if marker:
            log.warning("headless shell missing; retrying with full chromium (channel='chromium')")
            return await p.chromium.launch(headless=True, channel="chromium")
        raise


async def file_to_pdf(
    input_html_path: str,
    output_pdf_path: str | None = None,
    layout_config: dict | None = None,
    paper_size: str = "A4",
    landscape: bool = False,
    timeout_ms: int = 30000,
) -> dict:
    """
    Universal HTML-file → PDF converter (async).

    Renders the file with headless Chromium (playwright), waits for
    fonts/MathJax, sanitizes broken images, and prints to PDF. Relative
    assets resolve against the file's own directory via file:// navigation.
    """
    template_file = Path(input_html_path).expanduser().resolve()
    if not template_file.is_file():
        raise FileNotFoundError(f"HTML file not found: {template_file}")

    out_path = _resolve_output_path(output_pdf_path, template_file)
    log.info("Rendering %s -> %s", template_file.name, out_path.name)

    async with _RENDER_LOCK:
        async with async_playwright() as p:
            browser = await _launch(p)
            try:
                context = await browser.new_context(
                    user_agent=_USER_AGENT,
                    extra_http_headers=_EXTRA_HEADERS,
                )
                page = await context.new_page()
                await _navigate(page, _path_to_file_url(template_file), "", timeout_ms)
                if layout_config:
                    await page.add_style_tag(
                        content=_dynamic_css(layout_config, paper_size, landscape)
                    )
                await _finalize_page(page)
                size_bytes = await _print_pdf(page, out_path, paper_size, landscape)
            finally:
                await browser.close()

    log.info("Generated: %s", out_path)
    return {
        "ok": True,
        "pdf_path": str(out_path),
        "source": str(template_file),
        "bytes": size_bytes,
        "paper_size": paper_size,
        "landscape": landscape,
    }


async def url_to_pdf(
    url: str,
    output_pdf_path: str | None = None,
    layout_config: dict | None = None,
    paper_size: str = "A4",
    landscape: bool = False,
    timeout_ms: int = 45000,
) -> dict:
    """
    Public-URL → PDF converter (async). Navigates a headless Chromium to the
    given http(s) URL, waits for fonts/images/MathJax, and prints the page
    to PDF. Only http/https URLs are accepted.
    """
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"url must use http(s) (got scheme {parsed.scheme!r})")

    out_path = _resolve_output_path(output_pdf_path, None)
    log.info("Rendering %s -> %s", url, out_path.name)

    async with _RENDER_LOCK:
        async with async_playwright() as p:
            browser = await _launch(p)
            try:
                context = await browser.new_context(
                    user_agent=_USER_AGENT,
                    extra_http_headers=_EXTRA_HEADERS,
                )
                page = await context.new_page()
                await _navigate(page, url, "", timeout_ms)
                if layout_config:
                    await page.add_style_tag(
                        content=_dynamic_css(layout_config, paper_size, landscape)
                    )
                await _finalize_page(page)
                size_bytes = await _print_pdf(page, out_path, paper_size, landscape)
            finally:
                await browser.close()

    log.info("Generated: %s", out_path)
    return {
        "ok": True,
        "pdf_path": str(out_path),
        "source": url,
        "bytes": size_bytes,
        "paper_size": paper_size,
        "landscape": landscape,
    }


async def content_to_pdf(
    html_content: str,
    output_pdf_path: str,
    layout_config: dict | None = None,
    paper_size: str = "A4",
    landscape: bool = False,
    timeout_ms: int = 30000,
) -> dict:
    """
    Raw-HTML-content → PDF converter (async). Remote assets are fetched over
    the network with browser-identity headers; content layout_config CSS is
    string-injected before rendering (respects DYNAMIC_STYLE_SLOT).
    """
    if not html_content or not html_content.strip():
        raise ValueError("html_content is empty")

    html = html_content
    if layout_config:
        html = _inject_style(html, _dynamic_css(layout_config, paper_size, landscape))

    out_path = _resolve_output_path(output_pdf_path, None)
    log.info("Rendering inline HTML -> %s", out_path.name)

    async with _RENDER_LOCK:
        async with async_playwright() as p:
            browser = await _launch(p)
            try:
                context = await browser.new_context(
                    user_agent=_USER_AGENT,
                    extra_http_headers=_EXTRA_HEADERS,
                )
                page = await context.new_page()
                await _navigate(page, None, html, timeout_ms)
                await _finalize_page(page)
                size_bytes = await _print_pdf(page, out_path, paper_size, landscape)
            finally:
                await browser.close()

    log.info("Generated: %s", out_path)
    return {
        "ok": True,
        "pdf_path": str(out_path),
        "source": "(inline html)",
        "bytes": size_bytes,
        "paper_size": paper_size,
        "landscape": landscape,
    }


async def check_environment() -> dict:
    """Report on the playwright + chromium rendering stack."""
    import importlib.metadata as md

    info: dict = {"playwright_package_version": md.version("playwright")}
    info["playwright_module_version"] = getattr(playwright, "__version__", "unknown")
    try:
        async with async_playwright() as p:
            exe = p.chromium.executable_path
            info["chromium_path"] = exe
            if Path(exe).exists():
                info["chromium_available"] = True
            else:
                # headless-shell-only installs: executable_path points at the
                # full chromium build; detect the headless shell instead.
                shells = sorted(Path(exe).parent.parent.parent.glob(
                    "chromium_headless_shell-*/chrome-linux/headless_shell"))
                info["headless_shell_path"] = str(shells[-1]) if shells else None
                info["chromium_available"] = bool(shells)
    except Exception as exc:  # noqa: BLE001
        info["chromium_available"] = False
        info["chromium_error"] = f"{type(exc).__name__}: {exc}"
    return info
