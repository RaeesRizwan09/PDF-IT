# Testing

Three layers. Run tests against the **deployed** endpoint URL (the MCP
protocol is the interesting surface; local logic is tested implicitly too).

## 1. Smoke test (bottom line: "does it serve")

```bash
python3 scripts/smoke_test.py "https://.../<token>/mcp"
```

- lists tools, asserts the basics exist
- calls `check_environment`
- renders an HTML hello-doc `html_content_to_pdf`
- downloads the returned `pdf_url` and asserts real `%PDF` bytes

## 2. Toolkit chain test (the manipulation suite)

```bash
python3 scripts/test_pdftools.py "https://.../<token>/mcp"
```

Runs the full chained flow **live**:

render A + B → `pdf_merge` → `pdf_extract_text` → `pdf_extract_pages` →
`pdf_page_numbers` → `pdf_metadata` (edit, asserts title written) →
`pdf_watermark` → `pdf_encrypt` (AES-256, then **verifies the returned PDF
actually asks for the password and opens with it**) → `pdf_decrypt` →
`pdf_split` (asserts ZIP contents) → `pdf_rotate` + `pdf_delete_pages`.
Downloads every returned link and re-parses with local pypdf.

## 3. Compression + Office test

```bash
python3 scripts/test_compress_office.py "https://.../<token>/mcp"
```

- builds a 1.15 MB *image-heavy* PDF locally (reportlab + noise JPEG) and a
  ~36 KB real `.pptx` (python-pptx), uploads both to the Blob container via
  `az` (needs `az storage` + account key auth), then:
- `pdf_compress` at all three levels — asserts sizes shrink monotonically
  and per-level stats are surfaced,
- `office_to_pdf` — asserts the returned PDF has **exactly 3 pages** with
  the slide-1 text readable,
- a rejection case — PDFs fed to `office_to_pdf` must fail cleanly.

## What "passing" proved in the reference run

- 14 toolkit ops + 2 new tools all chained live on the deployed app
- compression: `light` 0 % (text structural, expected tiny), `medium`
  94.4 %, `strong` 99.5 % on the image-heavy fixture
- PPTX: 3-slide deck → 24 KB PDF, text extractable per slide
- password-protected outputs open exactly as `pypdf` says they should

## Local syntax-level check

```bash
python3 -m py_compile src/*.py
python3 -c "import http_server"      # from src/
```

(Live tests need python-pptx + pypdf + reportlab locally — installed with
`pip install python-pptx pypdf reportlab pikepdf Pillow`.)
