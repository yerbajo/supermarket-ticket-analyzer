"""Ticket (PDF) parsing: get the text, detect the chain, read its lines.

Two chains are supported. Supermercados MAS prints a text layer, so pypdf plus a
few regexes are enough. Dia hands the ticket out as a picture (a phone
screenshot exported with iOS Quartz), so the page is rendered and read with OCR
first and the same kind of parsing then runs on the result.

This module never guesses. An unknown chain, an unreadable PDF or a ticket
whose product lines cannot be found all raise :class:`ParseError`. OCR drops
characters, so every amount is normalised and then checked against the line
arithmetic (``quantity x unit price == line total``); a line that does not check
is counted in ``unverified``, and a line sum that disagrees with the printed
total is reported through ``mismatch`` instead of being silently accepted.
"""

from __future__ import annotations

import re
import unicodedata

from pypdf import PdfReader

MAS_MARKER = "SUPERMERCADOS MAS"
DIA_MARKER = "productos vendidos por dia"


class ParseError(RuntimeError):
    """A ticket could not be read: unknown chain, unreadable PDF or no lines."""


class ImageOnlyPdf(ParseError):
    """The PDF has no text layer: it is a picture and needs OCR."""


def _plain(text) -> str:
    """Lower-case, accent-free text, used only for marker matching."""
    decomposed = unicodedata.normalize("NFKD", str(text))
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).lower()


# The header line carries ``dd/mm/yyyy hh:mm:ss`` next to ``TLF``.
_DATE_RE = re.compile(r"(\d{2})/(\d{2})/(\d{4})\s+(\d{2}:\d{2}:\d{2})")
_FACTURA_RE = re.compile(r"FACTURA SIMPLIFICADA\s+(\S+)")
_OPERACION_RE = re.compile(r"OPERACI\S*\s+(\d[\d/]*)")
_TOTAL_RE = re.compile(r"TOTAL COMPRA.*?(\d+\.\d{2})")

# Fixed-width product tail: name, ``<qty>x``, unit price, line total, VAT letter.
# The name is truncated by the printer, so only the tail is reliable.
_LINE_RE = re.compile(
    r"^(?P<name>.+?)\s+(?P<qty>\d+(?:\.\d+)?)x\s+"
    r"(?P<unit>\d+\.\d{2})\s+(?P<total>\d+\.\d{2})\s+(?P<vat>[A-Z])\s*$"
)

# ``mismatch`` tolerance: the printed total and the line sum may differ by one
# cent per line due to rounding without being a real inconsistency.
_TOLERANCE = 0.05


def extract_pdf_text(path) -> str:
    """Return the text of every page, or raise on a file with no text layer.

    A PDF exported from a phone screenshot has only images: pypdf extracts zero
    characters. That needs OCR, which this project does not implement, so the
    error says so instead of returning an empty string.
    """
    try:
        reader = PdfReader(str(path))
        parts = [page.extract_text() or "" for page in reader.pages]
    except ParseError:
        raise
    except Exception as exc:  # pypdf raises a variety of reader errors
        raise ParseError(f"{path}: no se pudo leer el PDF ({exc})") from exc

    text = "\n".join(parts)
    if not text.strip():
        raise ImageOnlyPdf(
            f"{path}: el PDF no tiene capa de texto (parece un ticket en imagen); "
            "necesita OCR"
        )
    return text


def detect_chain(text) -> str | None:
    """Return the chain key for ``text``, or ``None`` when unknown.

    Matching is by marker, never by the printer's mangled accented words.
    """
    plain = _plain(text)
    if MAS_MARKER in str(text):
        return "MAS"
    if DIA_MARKER in plain:
        return "Dia"
    return None


def _parse_purchased_at(text):
    """The ``dd/mm/yyyy hh:mm:ss`` from the header line that carries ``TLF``."""
    for line in str(text).splitlines():
        if "TLF" not in line:
            continue
        match = _DATE_RE.search(line)
        if match:
            day, month, year, clock = match.groups()
            return f"{year}-{month}-{day}T{clock}"
    return None


def parse_ticket(text) -> dict:
    """Parse a ticket ``text`` into chain-agnostic fields.

    Dispatches on :func:`detect_chain`. Every parser returns the same keys:
    ``chain``, ``ticket_number``, ``purchased_at``, ``total``, ``lines_total``,
    ``mismatch``, ``unverified``, ``skipped`` and ``lines``.
    """
    chain = detect_chain(text)
    if chain == "MAS":
        return parse_mas(text)
    if chain == "Dia":
        return parse_dia(text)
    raise ParseError("cadena no reconocida: no se encontró ninguna marca conocida")


def parse_mas(text) -> dict:
    """Parse the Supermercados MAS fixed-width text layer.

    ``purchase_unit`` rule: an integer quantity is a count of units (``"ud"``),
    a fractional quantity is a weight in kilograms (``"kg"``). Supermarket
    weighed lines are always priced per kg, so the unit price column is the
    €/kg. Limit: a genuinely fractional *unit* count (for example ``1.5x`` of a
    pack) would be misread as kilograms; the printed layout gives no way to
    tell them apart.
    """
    text = str(text)
    purchased_at = _parse_purchased_at(text)

    factura = _FACTURA_RE.search(text)
    operacion = _OPERACION_RE.search(text)
    if factura:
        ticket_number = factura.group(1)
    elif operacion:
        ticket_number = operacion.group(1)
    else:
        ticket_number = None

    total_match = _TOTAL_RE.search(text)
    total = float(total_match.group(1)) if total_match else None

    lines = []
    for raw in text.splitlines():
        match = _LINE_RE.match(raw.strip())
        if not match:
            continue
        qty = float(match.group("qty"))
        purchase_unit = "ud" if qty.is_integer() else "kg"
        lines.append(
            {
                "raw_text": match.group("name").strip(),
                "qty": qty,
                "price_paid": float(match.group("unit")),
                "total_paid": float(match.group("total")),
                "purchase_unit": purchase_unit,
            }
        )

    lines_total = round(sum(line["total_paid"] for line in lines), 2)
    mismatch = total is not None and abs(lines_total - total) > _TOLERANCE

    return {
        "chain": "MAS",
        "ticket_number": ticket_number,
        "purchased_at": purchased_at,
        "total": total,
        "lines_total": lines_total,
        "mismatch": mismatch,
        "unverified": 0,  # a text layer has no OCR to doubt
        "skipped": 0,
        "lines": lines,
    }


# --------------------------------------------------------------------------- #
# Dia: the text arrives from OCR, so the amounts are read defensively.
# --------------------------------------------------------------------------- #

# Units are matched in lower case on purpose: the OCR text of a Dia ticket has
# ``1 ud`` and ``1,149 kg``, while a bare ``G`` is part of a product name
# (``150 G``) and must not be read as grams.
_DIA_LINE_RE = re.compile(
    r"^(?P<name>.*?)\s*(?P<qty>\d+(?:,\d+)?)\s*(?P<unit>ud|uds|kg|l)\s+"
    r"(?P<price>[\d.,]+)\s*€?\s+(?P<total>[\d.,]+)\s*€?\s*(?P<vat>[A-Z])?\s*$"
)
_DIA_DATE_RE = re.compile(r"(\d{2})/(\d{2})/(\d{4})\s+(\d{2}):(\d{2})")
_DIA_FACTURA_RE = re.compile(r"[Ff]actura simplificada[^\d]*(\d[\d-]*)")
_DIA_END_MARKERS = ("total venta", "desglose", "tipo iva")
_DIA_UNITS = {"ud": "ud", "uds": "ud", "kg": "kg", "l": "l"}
_DIA_TOLERANCE = 0.03


def amount_candidates(token) -> list:
    """Readings of a money token, most likely first; empty when unreadable.

    OCR drops the decimal comma (``269€`` is ``2,69 €``) and sometimes adds a
    stray digit (``1,779`` is really ``1,79``). A token with an ambiguous shape
    yields ``[]`` rather than a guess.
    """
    text = str(token).replace(" ", "").replace("€", "").strip()
    if not re.fullmatch(r"\d[\d.,]*", text):
        return []

    candidates = []
    if "," in text:
        whole, decimals = text.split(",", 1)
        if not whole.isdigit() or not decimals.isdigit():
            return []
        if len(decimals) == 2:
            candidates.append(float(f"{whole}.{decimals}"))
        elif len(decimals) == 3:
            for index in range(len(decimals)):
                value = float(f"{whole}.{decimals[:index]}{decimals[index + 1:]}")
                if value not in candidates:
                    candidates.append(value)
        else:
            return []
    elif len(text) == 3:
        candidates = [float(f"{text[0]}.{text[1:]}"), float(f"{text[:2]}.{text[2:]}")]
    elif len(text) == 4:
        candidates = [float(f"{text[:2]}.{text[2:]}")]
    return candidates


def read_amount(token):
    """Canonical reading of a money token, or ``None`` when it is ambiguous."""
    candidates = amount_candidates(token)
    return candidates[0] if candidates else None


def _select_amounts(qty, price_token, total_token):
    """Pick the price/total reading that satisfies ``qty * price == total``.

    Returns ``(price, total, verified)``. Without a reading that checks
    arithmetically the canonical ones are returned with ``verified`` False, so
    the caller can count them instead of trusting them.
    """
    prices = amount_candidates(price_token)
    totals = amount_candidates(total_token)
    if not prices or not totals:
        return None, None, False
    for price in prices:
        for total in totals:
            if abs(qty * price - total) <= _DIA_TOLERANCE:
                return price, total, True
    return prices[0], totals[0], False


def _dia_total(text):
    """The amount printed on the ``Total a pagar`` line (with fallbacks)."""
    lines = str(text).splitlines()
    for label in ("total a pagar", "total venta", "total sin promociones"):
        for line in lines:
            if not _plain(line).startswith(label):
                continue
            amounts = [read_amount(token) for token in re.findall(r"[\d.,]+\s*€", line)]
            amounts = [amount for amount in amounts if amount is not None]
            if amounts:
                return amounts[-1]
    return None


def _dia_lines(text):
    """Product rows of a Dia ticket, joining names that wrap onto two lines."""
    lines = []
    pending = []
    unverified = 0
    skipped = 0
    inside = False

    for raw in str(text).splitlines():
        stripped = raw.strip()
        if not stripped:
            continue
        plain = _plain(stripped)

        if plain.startswith("productos vendidos"):
            inside = True
            pending = []
            continue
        if not inside:
            continue
        if plain.startswith(_DIA_END_MARKERS):
            break
        if plain.startswith("descripcion"):  # the column header, not a product
            continue

        match = _DIA_LINE_RE.match(stripped)
        if not match:
            pending.append(stripped)  # a name that wrapped onto the next line
            continue

        name = " ".join(pending + [match.group("name").strip()]).strip()
        pending = []
        if not name:
            continue

        qty = float(match.group("qty").replace(",", "."))
        price, total, verified = _select_amounts(
            qty, match.group("price"), match.group("total")
        )
        if price is None or total is None:
            skipped += 1
            continue
        if not verified:
            unverified += 1

        lines.append(
            {
                "raw_text": name,
                "qty": qty,
                "price_paid": price,
                "total_paid": total,
                "purchase_unit": _DIA_UNITS[match.group("unit")],
            }
        )

    return lines, unverified, skipped


def parse_dia(text) -> dict:
    """Parse the OCR text of a Dia ticket (a phone screenshot as PDF)."""
    text = str(text)

    purchased_at = None
    date_match = _DIA_DATE_RE.search(text)
    if date_match:
        day, month, year, hour, minute = date_match.groups()
        purchased_at = f"{year}-{month}-{day}T{hour}:{minute}:00"

    factura = _DIA_FACTURA_RE.search(text)
    total = _dia_total(text)
    lines, unverified, skipped = _dia_lines(text)

    lines_total = round(sum(line["total_paid"] for line in lines), 2)
    mismatch = total is not None and abs(lines_total - total) > 0.05

    return {
        "chain": "Dia",
        "ticket_number": factura.group(1) if factura else None,
        "purchased_at": purchased_at,
        "total": total,
        "lines_total": lines_total,
        "mismatch": mismatch,
        "unverified": unverified,
        "skipped": skipped,
        "lines": lines,
    }


def parse_pdf(path, dpi=None, lang=None) -> dict:
    """Read ``path``, detect its chain and parse it, or raise ``ParseError``.

    A PDF without a text layer is a picture (Dia's app exports those), so it is
    rendered and read with OCR.
    """
    from tickets import ocr  # lazy: importing this module must not need OCR tools

    try:
        text = extract_pdf_text(path)
    except ImageOnlyPdf:
        text = ocr.pdf_text(
            path, dpi=dpi or ocr.DEFAULT_DPI, lang=lang or ocr.LANG
        )

    parsed = parse_ticket(text)
    if not parsed["lines"]:
        raise ParseError(
            f"{path}: el ticket tiene texto pero no se encontró ninguna línea de producto"
        )
    return parsed


def import_ticket(conn, path) -> dict:
    """Parse ``path`` and persist it; return the parsed dict plus ids.

    The returned dict adds ``ticket_id`` and ``inserted``. Re-importing the same
    file is a no-op at the store level (the ticket and its lines are unique by
    ``chain``, ``ticket_number`` and ``purchased_at``).
    """
    from tickets import store

    parsed = parse_pdf(path)
    parsed["source_pdf"] = str(path)
    ticket_id, inserted = store.upsert_ticket(conn, parsed)
    return {**parsed, "ticket_id": ticket_id, "inserted": inserted}
