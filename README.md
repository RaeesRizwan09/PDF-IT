# PDF-IT — HTML→PDF MCP + Full PDF Toolkit, hosted on Azure

A **remote MCP (Model Context Protocol) server** that web LLMs — Claude custom
connectors, ChatGPT developer-mode connectors, or any MCP client — can call
directly over HTTP. It turns HTML, live web pages, Office documents and text
into **real, downloadable PDFs and files**, and ships a complete
**server-side PDF manipulation toolkit** on top.

**Endpoint shape:** `https://<app>.azurecontainerapps.io/<secret-token>/mcp`
—the token is a secret path segment (Claude/ChatGPT connector UIs accept full
URLs, but not header-based auth; the path token is that workaround).

---

## Highlights

- **Zero-UI rendering** — headless Chromium (Playwright, *headless-shell*
  build) renders HTML or captures live pages, with KaTeX/MathJax, web fonts
  and image-fallback handling baked in.
- **A complete PDF toolkit — 14 tools** (merge, split, page-range extracts,
  rotate, watermark, page numbers, AES-256 encryption, metadata, text
  extraction) positioned for *chaining*: every tool takes URLs and returns
  URLs, so LLMs can compose flows like
  *merge → watermark → encrypt* without touching file bytes.
- **Preset-based compression** — `light / medium / strong`.
  (image-heavy PDF saw 94% / 99.5% mid/strong in the smoke suite).
- **Office → PDF** — PPTX/DOCX/XLSX/ODP/ODS/ODT/RTF/TXT/CSV via
  **LibreOffice headless**, full fidelity.
- **URL-based ingestion everywhere** — no multipart, no base64 in LLM
  contexts (which are neither possible nor sane at binary sizes). Files that
  aren’t public yet get one dragged onto the built-in **/upload** page.
- **Delivered as links, not files**: every output lands in Azure Blob Storage
  behind **time-limited pre-signed URLs** (default 24 h) — chat clients stay
  token-light.

---

## The tools (18)

| Tool | What it does |
|---|---|
| `html_content_to_pdf` | Render authored HTML → PDF (fonts/KaTeX/images OK) |
| `url_to_pdf` | Capture any public web page as PDF |
| `pdf_merge` | Concatenate PDFs |
| `pdf_split` | Split → one ZIP of single-page PDFs |
| `pdf_extract_pages` / `pdf_delete_pages` | Keep/remove ranges (`1-3,7,9-`) |
| `pdf_rotate` | 90/180/270° pages |
| `pdf_watermark` | Diagonal text stamp or overlay another PDF |
| `pdf_page_numbers` | `4 / 12`-style numbering, 6 positions |
| `pdf_compress` | light / medium / strong presets (image re-encoding + structure opts) |
| `pdf_encrypt` / `pdf_decrypt` | AES-256, user & owner passwords |
| `pdf_metadata` | View / edit title, author, subject, keywords, creator, producer |
| `pdf_extract_text` | Pull raw text per range for LLM summarizing |
| `office_to_pdf` | PPTX/DOCX/XLSX/ODP/… → PDF (LibreOffice) |
| `save_text_as_file` | Store model-authored text as a real downloadable file |
| `upload_base64` | ≤64 KB binaries with exact bytes → Blob link |
| `check_environment` | Diagnostics (Chromium/Playwright + Blob config) |

*(The local-only `html_file_to_pdf` tool existed pre-fork; remote users have
no shared filesystem, so it stays stdio-only.)*

## Getting files in (and *why*, honestly)

Every tool calls require **public `http(s)://` URLs** — LLM chat sandboxes (e.g.,
Claude.ai) cannot fetch arbitrary hosts (a platform DNS allowlist), and the
chat model never receives attached binary bytes in the first place. Three
bridges, covered by the server’s own instructions (the LLM will quote them):

1. **`/upload` page** *(the workhorse)* — open
   `<endpoint>/upload` (replace trailing `/mcp` with `/upload`) in a regular
   browser, drag the file in, get a 24 h signed link, paste it into chat.
2. **Any public host** — Dropbox “anyone with link”, Drive direct-download,
   GitHub — `github.com/.../blob/...` and Gist links are auto-rewritten to
   raw downloads before fetching.
3. **For text the model authored** — pass it straight to
   `html_content_to_pdf` (or `save_text_as_file`). No file dance needed.

## Architecture (one page version)

```
 Chat LLM (Claude / ChatGPT / any MCP client)
     │  streamable-HTTP + JSON-RPC (stateless)
     ▼
 Azure Container App  ──  FastAPI-style MCP server (mcp 2.3.0 SDK)
   ├─ /<token>/mcp        MCP tool endpooint
   ├─ /<token>/upload     browser upload page (drag & drop → Blob)
   ├─ Chromium headless-shell (Playwright)         rendering
   ├─ pypdf + reportlab     PDF toolkit operations
   ├─ pikepdf + Pillow      compression (image re-encode/swap)
   └─ LibreOffice headless  office conversion
            │
            ▼  every output
 Azure Blob Storage (generated-pdfs container)
            │  pre-signed GET link, 24 h TTL
            └── returned to the LLM as {ok, pdf_url, bytes, expires_at}
```

**Scale-to-zero** by default (min replicas 0 → cold starts pay a Chromium
warmup); `--min-replicas 1` removes the wait at always-on cost.
See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for details and
[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) for the deployment walkthrough.

## Repository layout

```
├── src/
│   ├── http_server.py    remote streamable-HTTP MCP server + upload page
│   ├── pdftools.py       URL in → PDF out toolkit (pypdf/reportlab/pikepdf)
│   ├── pipeline.py       shared Playwright rendering pipeline
│   └── pdf_server.py     original local stdio server (reference)
├── scripts/
│   ├── smoke_test.py             minimal e2e (list/render/verify)
│   ├── test_pdftools.py          chained-toolkit e2e (live)
│   └── test_compress_office.py   compression + office e2e (live)
├── Dockerfile            python:3.12-slim + LibreOffice + headless-shell
├── requirements.txt      pinned deps
├── deploy.sh             one-shot idempotent Azure deployment
└── docs/                 architecture / deployment / testing deep dives
```

## Testing

`scripts/smoke_test.py <mcp-url>` — tools list + a real render.
`scripts/test_pdftools.py <mcp-url>` — the 9-step toolkit chain.
`scripts/test_compress_office.py <mcp-url>` — compression levels + LibreOffice.
All three passed against the live deployment (see docs/TESTING.md).

## Security model

- The `<secret-token>` in the path **is** the credential — treat the full
  URL as a secret; anyone holding it can spend your Azure compute.
- Rotation: `./deploy.sh --rotate` (endpoint stays after a redeploy).
- Upload endpoint caps at 50 MB/file; blobs auto-expire via the
  pre-signed URL TTL (set a container lifecycle rule for physical deletion).
- LLM-facing instructions include hard “don’t echo content, return the link”
  rules to keep chat tokens tiny.

## Requirements to deploy your own

- An Azure subscription (~$0/mo at scale-to-zero + pennies of blob traffic).
- Docker locally **or** any container build path (script: build+push to ACR
  then `az containerapp`). One sudo command may be needed on Linux for
  rootless Docker (`apt-get install uidmap`).
- Python 3.10+ + `az` CLI for deploys and tests.

Jump to [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).
