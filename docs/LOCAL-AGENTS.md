# Local agents: Claude Code, Gemini CLI, OpenCode (and friends)

Filesystem-capable agents are PDF-IT's most natural users. Run the **local
stdio server** (same toolkit core as the remote one) and tools accept real
file paths — no upload dance, no cloud links, no expiry.

## 0. Install the (small) Python deps once

```bash
pip install pypdf reportlab pikepdf Pillow mcp
# optional, only for HTML/URL rendering + web capture:
pip install playwright && playwright install chromium
# if a user's PDF is password protected, tools take `password` args
```

(Nothing cloud-related is required. Azure deps are only for the remote server.)

## 1. Register the stdio server

Path placeholder `<PDF-IT>` = where you cloned the repo. Use absolute paths.

### Claude Code

Add to the project-root `.mcp.json` (recommended — versionable, shared):

```json
{
  "mcpServers": {
    "pdf-it": {
      "command": "python3",
      "args": ["/abs/path/to/PDF-IT/src/local_server.py"],
      "env": { "PDFIT_OUTPUT_DIR": "~/PDF-IT outputs" }
    }
  }
}
```

or one-liner:

```bash
claude mcp add pdf-it -- python3 /abs/path/to/PDF-IT/src/local_server.py
```

### Gemini CLI

`~/.gemini/settings.json`:

```json
{
  "mcpServers": {
    "pdf-it": {
      "command": "python3",
      "args": ["/abs/path/to/PDF-IT/src/local_server.py"]
    }
  }
}
```

### OpenCode

`~/.config/opencode/opencode.json` (or per-project `.opencode.json`):

```json
{
  "mcp": {
    "pdf-it": {
      "type": "local",
      "command": ["python3", "/abs/path/to/PDF-IT/src/local_server.py"]
    }
  }
}
```

## 2. What the agent gets (16 tools, disk-native)

- **Render:** `html_content_to_pdf`, `url_to_pdf` (needs Playwright Chromium)
- **Manipulate:** `pdf_merge`, `pdf_split` (page-ZIP), `pdf_extract_pages`,
  `pdf_delete_pages`, `pdf_rotate`, `pdf_watermark`, `pdf_page_numbers`,
  `pdf_compress` (light/medium/strong), `pdf_encrypt`/`pdf_decrypt`
  (AES-256), `pdf_metadata`, `pdf_extract_text`
- **Convert:** `office_to_pdf` (needs LibreOffice; checks `soffice`)
- **Diagnose:** `check_environment`

Inputs are `{*_path_or_url}`: local paths win for private data; URLs work
when the source is public or on GitHub (auto raw-rewritten). Encrypted PDFs
take a `password` argument (decrypting is deliberately explicit).

## 3. Outputs

Every producing tool returns `{ok: true, path: "/abs/…", bytes: n}` — a real
file the agent can reference in subsequent steps (open, git add, send, etc.).
Default folder: `~/PDF-IT outputs` (override `PDFIT_OUTPUT_DIR`).

## 4. Remote alternative (no local install at all)

The hosted Azure server works from internet-connected agents too:

- Claude Code:  `claude mcp add --transport http pdf-it-remote "https://<app>…/<token>/mcp"`
- OpenCode:     `{ "pdf-it-remote": { "type": "remote", "url": "https://<app>…/<token>/mcp" } }`
- Gemini CLI:   `gemini mcp add --transport http pdf-it-remote "<url>"`

Trade-offs: remote = nothing to install but needs public URLs + 24 h links;
local = full filesystem freedom + zero egress, at the cost of `pip install`
and (optionally) Chromium/LibreOffice presence.

## 5. Quick verification

```bash
python3 - <<'EOF'
import asyncio, sys, os
from mcp import ClientSession
from mcp.client.stdio import stdio_client, StdioServerParameters
async def main():
    params = StdioServerParameters(
        command=sys.executable, args=["src/local_server.py"],
        env=dict(os.environ, PYTHONPATH=os.pathsep.join(
            [os.path.abspath("src"), os.environ.get("PYTHONPATH", "")])))
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            print(sorted(t.name for t in (await s.list_tools()).tools))
asyncio.run(main())
EOF
```
