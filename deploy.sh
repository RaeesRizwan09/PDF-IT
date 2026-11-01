#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# One-shot deploy: builds the html2pdf MCP image in Azure Container Registry
# (remote build — no local Docker needed) and hosts it on Azure Container
# Apps with Blob Storage PDF delivery and a secret-path-protected endpoint.
#
# Usage:
#   ./deploy.sh [--suffix SUFFIX] [--location REGION] [--prefix NAME]
#               [--scale-to-zero] [--yes]
#
# Idempotent-ish: re-running with the same --suffix updates the existing
# resources (rebuild + app revision redeploy).
# ---------------------------------------------------------------------------
set -euo pipefail

# Parse args BEFORE computing resource names.
# Defaults match the first (live) deployment so a bare re-run updates the
# existing resources instead of creating a second set. Override freely.
SUFFIX="${HTML2PDF_SUFFIX:-4821}"
LOCATION="${AZURE_LOCATION:-eastasia}"
MIN_REPLICAS="${MIN_REPLICAS:-0}"    # 0 = scale to zero (cold start when idle)
MAX_REPLICAS=3
ASSUME_YES=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --suffix)       SUFFIX="$2"; shift 2 ;;
    --location)     LOCATION="$2"; shift 2 ;;
    --min-replicas) MIN_REPLICAS="$2"; shift 2 ;;
    --rotate)       ROTATE=1; shift ;;
    --yes)          ASSUME_YES=1; shift ;;
    *) echo "Unknown arg $1"; exit 1 ;;
  esac
done

# --- resource names (derived from parsed suffix) ---------------------------
ACR_NAME="acrhtml2pdf${SUFFIX}"      # must be alphanumeric only
STG_NAME="sthtml2pdf${SUFFIX}"       # must be alphanumeric only (3-24)
RG_NAME="rg-html2pdf-${SUFFIX}"
CAE_NAME="cae-html2pdf-${SUFFIX}"
APP_NAME="html2pdf-mcp-${SUFFIX}"
BLOB_CONTAINER="generated-pdfs"
# Immutable-ish tag per deploy: redeploys get a fresh tag so Container Apps
# actually re-pulls the new digest (mutable tags get no-op'd by `update`).
TAG="${HTML2PDF_TAG:-$(date +%Y%m%d-%H%M%S)}"
IMAGE="${ACR_NAME}.azurecr.io/html2pdf/html2pdf-mcp:${TAG}"
# --------------------------------------------------------------------------

say()  { printf '\n\033[1;36m>> %s\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m✔\033[0m %s\n' "$*"; }

# Warn before each billable resource, unless --yes passed.
confirm() {
  [[ "${ASSUME_YES}" = "1" ]] && return 0
  printf '\033[1;33m?\033[0m Create/verify %s? [Enter=go, Ctrl-C=abort] ' "$*"
  read -r
}

say "0. Login & subscription"
az account show >/dev/null 2>&1 || { az login; }
az account show -o tsv --query "[name, id]"

confirm "Resource group: $RG_NAME ($LOCATION)"
az group create -n "$RG_NAME" -l "$LOCATION" -o none
ok "resource group ready"

say "1. Storage account + PDF blob container ($STG_NAME)"
confirm "storage account: $STG_NAME"
if ! az storage account show -n "$STG_NAME" -g "$RG_NAME" >/dev/null 2>&1; then
  az storage account create -n "$STG_NAME" -g "$RG_NAME" \
    --sku Standard_LRS --kind StorageV2 --https-only true \
    --allow-blob-public-access false -o none
fi
BLOB_EP=$(az storage account show -n "$STG_NAME" -g "$RG_NAME" \
             --query "primaryEndpoints.blob" -o tsv)
ok "storage ready: $BLOB_EP"

STG_CONN=$(az storage account show-connection-string -n "$STG_NAME" \
                -g "$RG_NAME" --query "connectionString" -o tsv)

if ! az storage container show --account-name "$STG_NAME" \
        --connection-string "$STG_CONN" \
        --name "$BLOB_CONTAINER" >/dev/null 2>&1; then
  # Key auth: the deploying user's Owner role doesn't grant data-plane
  # (blob) operations, and the app itself uses the same connection string.
  az storage container create --account-name "$STG_NAME" \
    --connection-string "$STG_CONN" \
    --name "$BLOB_CONTAINER" -o none
fi
ok "blob container '$BLOB_CONTAINER' ready"

say "2. Container Registry ($ACR_NAME) + remote image build"
confirm "acr: $ACR_NAME"
if ! az acr show -n "$ACR_NAME" -g "$RG_NAME" >/dev/null 2>&1; then
  az acr create -n "$ACR_NAME" -g "$RG_NAME" --sku Basic \
    --admin-enabled true -o none
fi
ok "registry ready"
# Admin credentials (used for the local docker push AND the app's pull auth).
ACR_USER=$(az acr credential show -n "$ACR_NAME" --query "username" -o tsv)
ACR_PASS=$(az acr credential show -n "$ACR_NAME" --query "passwords[0].value" -o tsv)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Build the image. Azure for Students/trial subscriptions can't run ACR Tasks
# (remote builds), so default to a local rootless-docker build + push; set
# HTML2PDF_BUILD_METHOD=acr to build remotely instead (needs a paid sub).
BUILD_METHOD="${HTML2PDF_BUILD_METHOD:-auto}"
if [[ "$BUILD_METHOD" == "auto" ]]; then
  if docker version >/dev/null 2>&1; then
    BUILD_METHOD=local
    ok "docker daemon reachable — building locally (rootless)"
  else
    BUILD_METHOD=acr
    ok "no local docker — building remotely via ACR Tasks"
  fi
fi
confirm "build & push image: $IMAGE (method: $BUILD_METHOD)"
if [[ "$BUILD_METHOD" == "acr" ]]; then
  az acr build --registry "$ACR_NAME" --image "${IMAGE}" \
    --file "$SCRIPT_DIR/Dockerfile" "$SCRIPT_DIR" -o none
else
  printf '%s' "$ACR_PASS" | docker login "${ACR_NAME}.azurecr.io" \
    --username "$ACR_USER" --password-stdin >/dev/null
  docker build -t "$IMAGE" -f "$SCRIPT_DIR/Dockerfile" "$SCRIPT_DIR"
  docker push "$IMAGE"
fi
ok "image built & pushed"

say "3. Container Apps env + app ($APP_NAME)"
confirm "container apps environment: $CAE_NAME"
if ! az containerapp env show -n "$CAE_NAME" -g "$RG_NAME" >/dev/null 2>&1; then
  # Full Consumption environment (NOT the new "Express" default): Express
  # envs can't use managed identities and have flaky ACR credential pulls.
  az containerapp env create -n "$CAE_NAME" -g "$RG_NAME" --location "$LOCATION" \
    --enable-workload-profiles true -o none
fi
ok "environment ready"

MCP_TOKEN=""
STATE_FILE="$SCRIPT_DIR/.deploy-secrets"   # keeps the URL stable across redeploys
if [[ -s "$STATE_FILE" ]] && [[ "${ROTATE:-0}" != 1 ]]; then
  MCP_TOKEN=$(sed -n 's/^MCP_TOKEN=//p' "$STATE_FILE" | head -1)
  if [[ -n "$MCP_TOKEN" ]]; then
    ok "reusing saved endpoint token (URL unchanged)"
  fi
fi
if [[ -z "$MCP_TOKEN" ]]; then
  MCP_TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')
  (umask 077; printf 'MCP_TOKEN=%s\n' "$MCP_TOKEN" > "$STATE_FILE")
  [[ -n "$(az containerapp show -n "$APP_NAME" -g "$RG_NAME" >/dev/null 2>&1 && echo x)" ]] \
    && az containerapp secret set -n "$APP_NAME" -g "$RG_NAME" \
         --secrets "mcp-token=${MCP_TOKEN}" -o none
fi
confirm "container app: $APP_NAME"
if az containerapp show -n "$APP_NAME" -g "$RG_NAME" >/dev/null 2>&1; then
  # Registry credentials go through the dedicated sub-command for updates.
  az containerapp registry set -n "$APP_NAME" -g "$RG_NAME" \
    --server "${ACR_NAME}.azurecr.io" \
    --username "$ACR_USER" --password "$ACR_PASS" -o none
  az containerapp update -n "$APP_NAME" -g "$RG_NAME" \
    --image "$IMAGE" \
    --set-env-vars "AZURE_STORAGE_CONNECTION_STRING=secretref:storage-conn" \
                   "AZURE_BLOB_CONTAINER=${BLOB_CONTAINER}" \
                   "MCP_PATH_PREFIX=secretref:mcp-token" \
                   "HTML2PDF_JSON_RESPONSE=1" -o none
  # (no explicit revision restart: `update` provisions a fresh revision)
else
  az containerapp create -n "$APP_NAME" -g "$RG_NAME" \
    --environment "$CAE_NAME" \
    --image "$IMAGE" \
    --registry-server "${ACR_NAME}.azurecr.io" \
    --registry-username "$ACR_USER" --registry-password "$ACR_PASS" \
    --ingress external --target-port 8000 --transport auto \
    --cpu 1.0 --memory 2.0 \
    --min-replicas "$MIN_REPLICAS" --max-replicas "$MAX_REPLICAS" \
    --secrets "storage-conn=${STG_CONN}" "mcp-token=${MCP_TOKEN}" \
    --env-vars "AZURE_STORAGE_CONNECTION_STRING=secretref:storage-conn" \
               "AZURE_BLOB_CONTAINER=${BLOB_CONTAINER}" \
               "MCP_PATH_PREFIX=secretref:mcp-token" \
               "HTML2PDF_JSON_RESPONSE=1" -o none
fi
ok "container app deployed"

FQDN=$(az containerapp show -n "$APP_NAME" -g "$RG_NAME" \
        --query "properties.configuration.ingress.fqdn" -o tsv)
MCP_URL="https://${FQDN}/${MCP_TOKEN}/mcp"

say "4. Done — details"
cat <<EOF

  MCP endpoint   : ${MCP_URL}

  Add to Claude  : Claude → Settings → Connectors → Add custom connector →
                   paste
                   ${MCP_URL}
                   (no auth prompt appears; the token is in the path)

  Add to ChatGPT : Settings → Apps & Connectors → Developer Mode ON →
                   "Create" a connector → paste
                   ${MCP_URL}
                   and choose "No authentication".
  !!!Distribution: treat the full URL as a secret — anyone with it can spend
  !!!your compute. The token is stable across redeploys; rotate it explicitly
  !!!with:  ./deploy.sh --suffix $SUFFIX --rotate

  Scale note     : currently min=${MIN_REPLICAS} replica(s). With min=0, first
                   request after idle pays a cold start (tens of seconds).
                   Always-on costs roughly 1 vCPU × ~\$65/mo + 2 GiB × ~\$16/mo.
                   Run with --min-replicas 1 to keep it warm.
EOF
