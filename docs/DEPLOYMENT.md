# Deployment guide

Everything below assumes the repo layout: `deploy.sh` is the single
idempotent script that stands up the full stack.

## Prerequisites

1. **Azure account** — a free/student tier works. The script warns before
   each billable resource unless `--yes` is passed.
2. **`az` CLI** — installed and `az login` performed once (MFA tenants:
   `az login --tenant <tenant-id>`).
3. **Docker** — for local image builds. Rootless Docker works (this is how
   the reference deployment builds, without sudo past `apt-get install
   uidmap`). If you prefer, you can adapt the script's build branch.
4. **Python 3.10+** for the test scripts.

## One-shot deploy

```bash
pip install -r requirements.txt   # local tools only need python for tests
./deploy.sh --yes                 # create everything (default: suffix defaults)
# customize:
./deploy.sh --suffix 4821 --location eastasia --yes
```

The script, in order:

| Step | What it does |
|---|---|
| 0 | Verifies `az login`; picks subscription |
| 1 | Creates resource group `rg-html2pdf-<suffix>` |
| 2 | Creates Storage account `sthtml2pdf<suffix>` + `generated-pdfs` blob container (key auth) and ACR `acrhtml2pdf<suffix>` |
| 3 | Builds the Docker image (tagged `YYYYMMDD-HHMMSS` — snapshot tags so `containerapp update` always re-pulls the digest) and pushes to ACR |
| 4 | Creates the Container Apps environment (full Consumption: `--enable-workload-profiles true`; not the flaky "Express" class) and the app |
| 5 | Prints the **MCP endpoint URL** |

### Endpoint + token

- Path-token secret is stored in `.deploy-secrets` (chmod 600) and **reused
  on redeploy** so the connector URL you gave Claude/ChatGPT never breaks.
- Rotate explicitly any time: `./deploy.sh --rotate`.
- The endpoint's path looks like:

```
https://html2pdf-mcp-<suffix>.<random>.<region>.azurecontainerapps.io/<token>/mcp
```

## Configuring connectors

**Claude:** Settings → Connectors → *Add custom connector* → paste the full
URL. No auth prompt appears (the token is in the path).

**ChatGPT:** Settings → Apps & Connectors → Developer mode ON → *Create* →
paste the URL → authentication: **No authentication**.

## Updating**

Any source change:

```bash
./deploy.sh --yes     # rebuild image → new timestamped tag → revision redeploy
```

Note the `update` path re-wires registry creds via
`az containerapp registry set` (the `update` subcommand itself does not
accept registry args) and uses `--env-vars` on `create`.

## Scaling / cost

| Mode | Behavior | Ballpark cost |
|---|---|---|
| `min-replicas 0` (default) | scale-to-zero; first request pays a cold start (tens of seconds — LibreOffice + Chromium warm, a little more after the office feature) | near-$0 idle |
| `min-replicas 1` | always warm 1 vCPU/2 GiB | ~$65 + ~$16 ≈ $80/mo region-dependent |

Blob traffic is pennies at modest volume; consider a lifecycle rule on
`generated-pdfs` to delete blobs older than N days (the signed URLs expire
at 24 h regardless).

## Env vars the server reads

| Var | Meaning | Default |
|---|---|---|
| `MCP_PATH_PREFIX` | secret path segment | required in prod |
| `AZURE_STORAGE_CONNECTION_STRING` | storage account | required |
| `AZURE_BLOB_CONTAINER` | target container | `generated-pdfs` |
| `PDF_URL_TTL_HOURS` | pre-signed TTL | 24 |
| `HTML2PDF_MAX_HTML_BYTES` | HTML input cap | 2 MB |
| `PDF_MAX_INPUT_BYTES` | toolkit input cap | 25 MB |
| `PDF_MAX_PAGES` | page cap for ops | 500 |
| `UPLOAD_MAX_BYTES` | /upload cap | 50 MB |

## Known deployment gotchas (learned the hard way)

- **ACR Tasks are blocked** on some Student subscriptions → build locally.
- **Express-class Container App environments** lack managed identity and
  have flaky ACR pulls → the script always uses full Consumption envs.
- `az containerapp update` ignores mutable-tag-only changes → timestamped
  image tags make redeploys real.
- Blob SAS/CS: container operations are done with the **connection string**
  (key auth); AD-auth-only paths were flaky on this subscription.
- Re-running the script without `--suffix` on a fork with a new number will
  create a **second, separate** billable stack. The default suffix is pinned
  so bare re-runs update in place.
- If your disk is small: the built image is ~1.5 GB unpacked (LibreOffice
  adds ~500 MB over base). Building locally needs a few GB free — prune old
  image tags aggressively (`docker image prune`, delete previous tags).
