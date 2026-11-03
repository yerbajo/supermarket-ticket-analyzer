# Tus tickets de Compra — grocery ticket tracker. Image for a single-user host behind a reverse
# proxy (LAN).
#
#   cd /opt/compras && docker compose build && docker compose up -d
#   http://<host>:8130
#
# WHY THIS IMAGE CARRIES A BROWSER AND AN OCR ENGINE. The app does three things that need
# system tools, and all three are load-bearing for the ticket pipeline:
#
#   * chromium — Dia's site sits behind Akamai, which answers `403 Access Denied` to plain
#     HTTP and to headless Chromium alike. What gets through (verified 2026-10-12) is a real
#     Chromium window, so the browser is not optional.
#   * xvfb — that window needs a display. On the workstation the window appears on the
#     desktop; in a container there is none, so `xvfb-run -a` supplies a virtual one. The
#     scraping command is therefore run as `xvfb-run -a …` (see the runbook). `xauth` comes
#     with it: without it xvfb-run stops with "xauth command not found" — measured on this
#     host on the first deploy, not guessed.
#   * tesseract-ocr + tesseract-ocr-spa and poppler-utils — Dia hands out its ticket as a
#     phone screenshot exported to PDF (148 images, no fonts, zero extractable characters).
#     pdftoppm renders it and tesseract reads it in Spanish.
#
# WHY THE SYSTEM CHROMIUM AND NOT PLAYWRIGHT'S OWN: Playwright's bundled browser adds
# hundreds of megabytes to the image for a browser Debian already ships. The app reads
# CHROMIUM_PATH and drives that binary instead (supermarket/dia.py, DiaBrowser.launch_options).
#
# WHY ROOT AND `--no-sandbox`: Chromium cannot sandbox itself as root, and inside a container
# it usually cannot use the sandbox anyway. The app adds the flag only when running as root,
# so the workstation path keeps its sandbox.
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      chromium \
      xvfb \
      xauth \
      tesseract-ocr \
      tesseract-ocr-spa \
      poppler-utils \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Requirements first: the dependency layer is cached across application edits.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV DB_PATH=/data/compras.db \
    CHROMIUM_PATH=/usr/bin/chromium \
    TICKETS_INBOX=/data/inbox \
    PYTHONUNBUFFERED=1

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/salud')" || exit 1

CMD ["python", "-m", "uvicorn", "web.app:app", "--host", "0.0.0.0", "--port", "8000"]
