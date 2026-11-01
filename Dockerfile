# Slim remote html2pdf MCP server.
# python:3.12-slim + Playwright's Chromium *headless-shell* only (~110MB,
# purpose-built for PDF printing) instead of the full ~1.5GB playwright
# base image — smaller pulls, faster cold starts, fits small disks.
# pdf_server.py's _launch() already prefers the headless shell.
FROM python:3.12-slim

WORKDIR /app

# Certificates + LibreOffice (headless Office→PDF conversion for the
# office_to_pdf tool). Writer/Calc/Impress cover docx/xlsx/pptx/odf;
# Carlito/Caladea give metric-compatible Calibri/Cambria substitution.
# Acquire::Retries makes flaky mirrors survivable.
RUN apt-get update && apt-get install -y --no-install-recommends \
        -o Acquire::Retries=8 \
        ca-certificates \
        libreoffice-writer \
        libreoffice-calc \
        libreoffice-impress \
        fonts-crosextra-carlito \
        fonts-crosextra-caladea \
    && rm -rf /var/lib/apt/lists/*

# Python deps (single source of truth: requirements.txt). Generous read
# timeouts + retries — PyPI CDN can be very slow from some networks.
ENV PIP_DEFAULT_TIMEOUT=300 \
    PIP_RETRIES=10
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Browser path must be set BEFORE `playwright install` so the binaries land
# where the runtime looks for them (same env at build and run time).
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# Chromium headless-shell + its system libraries/fonts. Fixed to the
# playwright version in requirements.txt so the browser build matches.
RUN python -m playwright install --with-deps --only-shell chromium

COPY src/ ./

ENV PYTHONUNBUFFERED=1 \
    HTML2PDF_LOGLEVEL=INFO \
    PORT=8000

EXPOSE 8000
CMD ["python3", "http_server.py"]
