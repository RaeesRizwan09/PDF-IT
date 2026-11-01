# Architecture

## Components

```
        ┌──────────────────────────── Container App (Azure CA) ───────────────┐
LLM ───▶ │ http_server.py                                                     │
        │   ├─ /<MCP_PATH_PREFIX>/mcp    MCP streamable-HTTP (stateless)      │
        │   └─ /<MCP_PATH_PREFIX>/upload  browser drag&drop → Blob           │
        │                                                                    │
        │ pipeline.py ─ Playwright Chromium *headless-shell* rendering        │
        │ pdftools.py ─ toolkit ops (URL→URL)                                 │
        │ soffice ───── office → pdf                                          │
        └───────────────┬─────────────────────────────────────────────────────┘
                        │ every output = temp file
                        ▼
              Azure Blob gen-container  ──  pre-signed GET (TTL 24 h)
                        │
                        └──▶ LLM receives {ok, pdf_url, bytes, expires_at}
```

### Request lifecycle of a typical tool call

1. LLM POSTs JSON-RPC to `/…/mcp` (stateless streamable-HTTP; JSON
   responses by default so clients don't need SSE).
2. The tool (e.g. `pdf_compress`) validates arg caps, `await`s the sync
   pipeline work on a small thread pool (keeps the event loop responsive
   under scale-out: CA runs multiple replicas, each async-capable).
3. Processing happens **against URLs**: download → bytes → transform →
   upload as a *new* blob → return the fresh pre-signed URL in the tool
   result. Nothing large ever flows through the LLM's own context.

### Why URLs, not blobs-in-context

- Chat connectors pass **JSON only**; attached files never reach the model
  as bytes; model-emitted base64 is capped by output-token budgets anyway.
  (`upload_base64` exists deliberately ≤64 KB for the desktop-client edge
  case.)
- Streaming file bytes from sandboxed LLM environments is also blocked at
  their DNS allowlist (observed live from Claude.ai).
- **URL-land is the common substrate**: links the tools return are links the
  tools accept. Chaining is pull-based, race-free, and cache-friendly.

### Rendering pipeline

`pipeline.py` is deliberately a near-copy of the local stdio server's
pipeline plus `url_to_pdf`. Chromium runs as the *headless-shell* flavour
(`playwright install --only-shell`) — ~110 MB instead of the full ~1.5 GB
playwright browser bundle, purpose built for PDF printing: same print engine,
faster cold starts. `PLAYWRIGHT_BROWSERS_PATH=/ms-playwright` must be set
*before* `playwright install` runs in the image so paths agree at runtime.

### Compression detail

- `light`: structure only — object streams, stream recompress, string
  dedup (pikepdf save options). Text PDFs rarely shrink.
- `medium` / `strong`: additionally walk page resources, extract embedded
  images via `pikepdf.PdfImage.as_pil_image()`, downsample (÷2 or ÷4 linear
  pixels), re-encode as JPEG (q72 / q42), and **swap the XObject back into
  the page's resource dict** (pikepdf 10 has no `.replace()` helper).
  Transparency masks are dropped; images too small to bother are skipped;
  per-image failures are skipped individually — a byte-level output is
  always delivered.

### Office conversion detail

`soffice --headless --convert-to pdf` runs with:
- a **per-call `UserInstallation` profile dir** — LibreOffice is
  single-profile-locking; per-call profiles make concurrent calls safe,
- `HOME` pointed at the temp dir, extension validated from the filename.
The tool accepts URL input (any host) and is capped by the shared
download cap (25 MB default).

### Auth model

AuthN is a **secret path prefix** chosen once and injected via env
(`MCP_PATH_PREFIX`); the connector URL embeds it. This exists because
Claude custom connectors and ChatGPT dev-mode don't take API keys, but do
accept full URLs. Consequence: the entire URL is a bearer credential.
AuthZ is effectively scope-free (one shared realm); rate/cost ceilings come
from Azure-side scaling limits.

## Concurrency & scaling

- App is stateless: replicas just do per-request work; autoscaling is Azure
  CA's (`min 0 – max 3` set by deploy.sh).
- Chromium + LibreOffice are memory-hungry: stick near 1 vCPU + 2 GiB per
  replica ratios; heavy parallelism on one replica will OOM before CPU
  saturates (observed: ~2 GiB for office + render simultaneously).
- Blob writes are one S3-key-per-output (`<yyyy>/<mm>/<uuid>_<name>') which
  gives O(1) expiry via URL TTL; physical cleanup is a lifecycle policy
  away if wanted.

## Failure semantics

- Tools never raise to the protocol layer; they return
  `{"ok": false, "error": "Type: detail"}` structures so the LLM can read
  and explain errors.
- Download errors carry **self-healing hints** (e.g., pointing out the
  `/upload` page or whitelisted types) — the LLM relays them verbatim.
- `check_environment` reports Chromium + Blob wiring for quick diagnosis.
