"""Unit normalisation for price comparison.

Turns a price plus a size/format string into a comparable unit price
(€/kg, €/l or €/ud). Returns ``(None, None)`` when the size cannot be
parsed: this module never guesses.
"""

from __future__ import annotations

import re

# label -> (base label, factor from the written unit to the base unit)
_UNITS = {
    "g": ("kg", 0.001),
    "kg": ("kg", 1.0),
    "ml": ("l", 0.001),
    "cl": ("l", 0.01),
    "l": ("l", 1.0),
    "ud": ("ud", 1.0),
    "uds": ("ud", 1.0),
    "unidad": ("ud", 1.0),
    "unidades": ("ud", 1.0),
    "gr": ("kg", 0.001),
    "grs": ("kg", 0.001),
    "gramo": ("kg", 0.001),
    "gramos": ("kg", 0.001),
    "kgs": ("kg", 1.0),
    "cc": ("l", 0.001),
    "lt": ("l", 1.0),
    "lts": ("l", 1.0),
    "litro": ("l", 1.0),
    "litros": ("l", 1.0),
}

_NUMBER = r"\d+(?:[.,]\d+)?"
# Longest alternatives first so "gr" wins over "g" and "gramos" over "grs".
_UNIT = r"kgs?|gramos?|grs?|g|ml|cl|cc|litros?|lts?|l|unidades?|unidad|uds?"
# A unit right before an apostrophe belongs to a word, not to a size: "5 l'oréal"
# is a brand name, not five litres. Measured on a real MAS catalogue name.
_NOT_APOSTROPHE = r"(?!['\u2019\u02bc])"
_MULTI_RE = re.compile(
    rf"(\d+)\s*[x\u00d7]\s*({_NUMBER})\s*({_UNIT}){_NOT_APOSTROPHE}\b", re.IGNORECASE
)
_SINGLE_RE = re.compile(rf"({_NUMBER})\s*({_UNIT}){_NOT_APOSTROPHE}\b", re.IGNORECASE)
_BARE_UD_RE = re.compile(r"\b(uds?|unidades?|unidad)\b", re.IGNORECASE)


# A pack written the way Spanish tickets write it: "P-16", "P4", "P 6".
# Case-insensitive: the caller lower-cases the text before looking for a size.
_PACK_RE = re.compile(r"\bP-?\s?(\d{1,3})\b", re.IGNORECASE)


def _to_base(value: float, written_unit: str):
    label, factor = _UNITS[written_unit.lower()]
    return value * factor, label


def extract_size(size_text):
    """Return ``(quantity_in_base_unit, base_label)`` or ``(None, None)``.

    The base unit is one of ``kg``, ``l`` or ``ud`` and the quantity is already
    converted to it (``"500 g"`` -> ``(0.5, "kg")``, ``"6 x 33 cl"`` ->
    ``(1.98, "l")``). This is the single source of truth for unit parsing; the
    other helpers must reuse it instead of duplicating the regexes. When the
    text cannot be parsed it returns ``(None, None)`` rather than guessing.
    """
    if size_text is None:
        return None, None

    text = str(size_text).strip().lower().replace(",", ".")
    if not text:
        return None, None

    match = _MULTI_RE.search(text)
    if match:
        count = int(match.group(1))
        size = float(match.group(2))
        total, label = _to_base(count * size, match.group(3))
    else:
        match = _SINGLE_RE.search(text)
        if match:
            total, label = _to_base(float(match.group(1)), match.group(2))
        else:
            match = _PACK_RE.search(text)
            if match:
                # "P-16" is sixteen units of whatever the line is: the ticket's own
                # way of writing a pack size. Without it, a 16-pack matches a 4-pack.
                total, label = float(match.group(1)), "ud"
            else:
                match = _BARE_UD_RE.search(text)
                if not match:
                    return None, None
                total, label = 1.0, "ud"

    if total <= 0:
        return None, None
    return total, label


def normalize_unit(price, size_text):
    """Return ``(unit_price, unit_label)`` or ``(None, None)``.

    ``size_text`` examples: ``"1 l"``, ``"500 g"``, ``"Bandeja 1 kg"``,
    ``"6 x 33 cl"``, ``"Pack 2 x 1,5 l"``, plus the abbreviations Spanish tickets
    and catalogues actually use: ``"500 gr"``, ``"125gr"``, ``"250 gramos"``,
    ``"1,5 lt"``, ``"2 lts"``, ``"3 litros"``, ``"33 cc"``.
    ``unit_label`` is one of ``kg``, ``l`` or ``ud``.
    """
    if price is None or size_text is None:
        return None, None
    try:
        price = float(price)
    except (TypeError, ValueError):
        return None, None
    if price <= 0:
        return None, None

    total, label = extract_size(size_text)
    if total is None:
        return None, None
    return round(price / total, 4), label
