"""Download the product pictures once, so the app serves them from the LAN.

The catalogue connectors already store each product's ``image_url``. This module
turns those URLs into files under one root, records the file in
``products.image_file`` and never repeats work: a product that already has a file
is skipped. That makes a second pass cheap after a catalogue refresh, which is
what lets it run as part of the daily update.

What each shop needs (measured 2026-10-12):

* **Mercadona** — plain HTTP works: JPEG 300×300, about 11 KB each.
* **Día** — its image host is behind the same Akamai wall as the rest of the site:
  a plain request gets ``403``. The pictures are fetched from inside a real
  Chromium page instead (the same session the account download uses).
* **MAS** — it has **no real pictures**: every product's ``thumbnail`` is the same
  generic placeholder, so the connector stores no ``image_url`` for MAS at all.

Bytes are validated before being written: an HTML error page saved as ``.jpg`` is
worse than no picture, so anything that does not start like an image is refused.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import requests

from tickets import store

#: Extensions by content type, best effort; the URL suffix is the fallback.
EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/avif": ".avif",
    "image/gif": ".gif",
}

#: A picture bigger than this is not a product thumbnail and is not stored.
MAX_BYTES = 4 * 1024 * 1024

#: Pictures above this size are re-encoded before being stored. Dia serves full
#: resolution photos (about 355 KB each, 102 MB for 288 products measured
#: 2026-10-12); a page never needs that, and a 400 px JPEG is about 30 KB.
SHRINK_ABOVE_BYTES = 60 * 1024
MAX_SIDE = 400
JPEG_QUALITY = 82

BROWSER_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)


def _looks_like_image(content: bytes) -> bool:
    """Magic numbers of the formats a shop actually serves."""
    if content[:3] == b"\xff\xd8\xff":  # JPEG
        return True
    if content[:8] == b"\x89PNG\r\n\x1a\n":
        return True
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return True
    if content[:6] in (b"GIF87a", b"GIF89a"):
        return True
    if content[4:12] == b"ftypavif":
        return True
    return False


def extension_for(url, content_type) -> str:
    """The file extension to use, from the content type or the URL."""
    limpio = (content_type or "").split(";")[0].strip().lower()
    if limpio in EXTENSIONS:
        return EXTENSIONS[limpio]
    sufijo = Path(str(url).split("?")[0]).suffix.lower()
    return sufijo if sufijo in {".jpg", ".jpeg", ".png", ".webp", ".avif", ".gif"} else ".jpg"


def shrink(content, max_side=MAX_SIDE, quality=JPEG_QUALITY):
    """Re-encode a picture down to something a web page can serve.

    Returns the small JPEG bytes, or ``None`` when Pillow cannot read the picture
    (in which case the caller keeps the original, which is already validated).
    """
    import io

    from PIL import Image

    try:
        with Image.open(io.BytesIO(content)) as imagen:
            imagen = imagen.convert("RGB")
            imagen.thumbnail((max_side, max_side))
            buffer = io.BytesIO()
            imagen.save(buffer, format="JPEG", quality=quality, optimize=True)
            return buffer.getvalue()
    except Exception:  # an unreadable picture is not worth losing the download
        return None


def http_fetcher(session=None, timeout=30):
    """A ``fetch_bytes`` that uses plain HTTP (Mercadona and MAS work this way)."""

    def fetch_bytes(url):
        respuesta = (session or requests).get(
            url, timeout=timeout, headers={"User-Agent": BROWSER_USER_AGENT, "Accept": "image/*"}
        )
        if respuesta.status_code != 200:
            return None, None
        return respuesta.content, respuesta.headers.get("content-type")

    return fetch_bytes


def download_images(
    conn,
    root,
    fetchers,
    chains=None,
    only_bought=False,
    limit=None,
    pause=0.15,
    redo=False,
) -> dict:
    """Download every product picture that is missing, one root per catalogue.

    ``fetchers`` maps a chain to a ``fetch_bytes(url) -> (bytes, content_type)``.
    A chain without a fetcher is skipped and counted, never guessed at. The file
    is written only after the bytes pass the image check, and ``image_file`` is
    recorded as a path relative to ``root`` so the web app can serve it.
    """
    raiz = Path(root)
    raiz.mkdir(parents=True, exist_ok=True)
    counts = {"pending": 0, "downloaded": 0, "skipped": 0, "failed": 0, "bytes": 0, "no_fetcher": 0}

    if redo:
        # Start over: the pictures already on disk may be too heavy or from a bad pass.
        store.clear_image_files(conn, chains=chains)

    pendientes = store.products_missing_images(
        conn, chains=chains, only_bought=only_bought, limit=limit
    )
    counts["pending"] = len(pendientes)

    for producto in pendientes:
        chain, external_id, url = producto["chain"], producto["external_id"], producto["image_url"]
        fetch_bytes = fetchers.get(chain)
        if fetch_bytes is None:
            counts["no_fetcher"] += 1
            continue

        carpeta = raiz / chain
        carpeta.mkdir(parents=True, exist_ok=True)
        try:
            content, content_type = fetch_bytes(url)
        except Exception:  # a broken picture must not stop the pass
            content, content_type = None, None

        if not content or len(content) > MAX_BYTES or not _looks_like_image(content):
            counts["failed"] += 1
            continue

        nombre = f"{external_id}{extension_for(url, content_type)}"
        if len(content) > SHRINK_ABOVE_BYTES:
            reducida = shrink(content)
            if reducida and len(reducida) < len(content):
                content, nombre = reducida, f"{external_id}.jpg"

        (carpeta / nombre).write_bytes(content)
        store.set_image_file(conn, producto["id"], f"{chain}/{nombre}")
        counts["downloaded"] += 1
        counts["bytes"] += len(content)

        if pause:
            time.sleep(pause)

    return counts


def images_root(db_path=None) -> Path:
    """Where pictures live: ``$IMAGES_DIR``, else an ``images`` folder beside the database."""
    from_env = os.environ.get("IMAGES_DIR")
    if from_env:
        return Path(from_env)
    base = Path(db_path).parent if db_path else Path(".")
    return base / "images"
