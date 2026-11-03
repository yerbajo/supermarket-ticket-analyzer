"""OCR for image-only tickets: a phone screenshot exported as a PDF.

Dia's app hands out the ticket as a single PDF built from 148 image strips and
no fonts, so there is nothing to extract with a text reader: the page has to be
rendered and read as a picture.

Two system tools do the work: ``pdftoppm`` (poppler) renders and ``tesseract``
reads with the Spanish model. Neither is a Python package, so this module checks
for them first and says exactly what is missing instead of failing with a stack
trace. Install them with::

    sudo apt install -y tesseract-ocr tesseract-ocr-spa poppler-utils

Measured on the real Dia ticket (2026): 450 dpi with ``--psm 3`` reads every
amount, while ``--psm 6`` mangled the printed totals (36,95 became 0,90). Even
the good settings sometimes drop the decimal comma (``269€`` for ``2,69 €``),
which is why the parser normalises the amounts and then checks
``quantity x unit price == line total`` before trusting them.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from tickets.parsing import ParseError

DEFAULT_DPI = 450
LANG = "spa"
PSM = 3  # a single uniform block of text: measured better than psm 6 on tickets

INSTALL_HINT = "sudo apt install -y tesseract-ocr tesseract-ocr-spa poppler-utils"


def missing_tools() -> list:
    """Names of the system tools that are not on the PATH."""
    return [tool for tool in ("pdftoppm", "tesseract") if shutil.which(tool) is None]


def require_tools():
    """Raise a ``ParseError`` that names what to install, or return quietly."""
    missing = missing_tools()
    if missing:
        raise ParseError(
            "falta " + " y ".join(missing) + " para leer un ticket en imagen (OCR); "
            f"instálalo con: {INSTALL_HINT}"
        )


def render_pdf_pages(path, dpi=DEFAULT_DPI, out_dir=None) -> list:
    """Render every page of ``path`` to PNG and return the files, in page order."""
    require_tools()
    directory = Path(out_dir) if out_dir else Path(tempfile.mkdtemp(prefix="tickets-ocr-"))
    directory.mkdir(parents=True, exist_ok=True)
    prefix = directory / "page"

    result = subprocess.run(
        ["pdftoppm", "-png", "-r", str(dpi), str(path), str(prefix)],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise ParseError(
            f"{path}: pdftoppm no pudo renderizar el PDF "
            f"({result.stderr.decode(errors='replace').strip()[:200]})"
        )

    def page_number(png):
        digits = "".join(character for character in png.stem if character.isdigit())
        return int(digits) if digits else 0

    pages = sorted(directory.glob("page*.png"), key=page_number)
    if not pages:
        raise ParseError(f"{path}: pdftoppm no generó ninguna imagen")
    return pages


def image_text(png, lang=LANG, psm=PSM) -> str:
    """Read one image with tesseract."""
    require_tools()
    import pytesseract
    from PIL import Image

    try:
        with Image.open(png) as image:
            return pytesseract.image_to_string(image, lang=lang, config=f"--psm {psm}")
    except Exception as exc:  # tesseract missing pieces, unreadable image, ...
        raise ParseError(f"{png}: el OCR falló ({exc})") from exc


def pdf_text(path, dpi=DEFAULT_DPI, lang=LANG, psm=PSM) -> str:
    """Render ``path`` and OCR every page, joined by newlines."""
    pages = render_pdf_pages(path, dpi=dpi)
    return "\n".join(image_text(png, lang=lang, psm=psm) for png in pages)
