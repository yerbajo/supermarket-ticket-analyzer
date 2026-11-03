"""Own-brand (white-label) detection per chain.

A supermarket's own brands are what make the "same product, other chain"
comparison meaningful: Hacendado is Mercadona's equivalent of IFA ELIGES at MAS
and of Dia at Día.

Where the evidence comes from:

* **MAS** publishes the brand as a product attribute: the GraphQL aggregation
  ``manufacturer`` (label «Marca») lists LA CARLOTEÑA, EL POZO…, and the store
  has brand pages such as ``/charcuteria-quesos/marca-ifa_eliges/``. In our own
  catalogue 1.439 products carry ``IFA`` and 208 carry ``UNNIA``.
* **Día** exposes the ``dia_brand`` facet («Marca Dia») and its own catalogue has
  2.380 products carrying ``Dia`` and 178 carrying ``Vegecampo``.
* **Mercadona** exposes no brand attribute; its own labels (Hacendado, Bosque
  Verde, Deliplus, Compy) are used consistently in the product name.

Detection therefore works on **tokens**, not on substrings: ``DIA`` must not
match ``DIADEMA``, and the comparison is accent- and case-insensitive.
"""

from __future__ import annotations

import re
import unicodedata

# Canonical own brands per chain. Order matters only for reporting.
OWN_BRANDS = {
    "Mercadona": ("Hacendado", "Bosque Verde", "Deliplus", "Compy"),
    "Dia": ("Dia", "Vegecampo"),
    "MAS": ("IFA ELIGES", "IFA", "Unnia"),
}

#: Brands that are too generic to be trusted on a single token.
_MIN_TOKEN_LENGTH = 3


def _plain(text) -> str:
    """Lower-case, accent-free, single-spaced text with word boundaries kept."""
    if text is None:
        return ""
    decomposed = unicodedata.normalize("NFKD", str(text))
    without_accents = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    spaced = "".join(ch if ch.isalnum() else " " for ch in without_accents)
    return " ".join(spaced.split()).lower()


def brand_tokens(name) -> list:
    """Accent-free upper-case tokens of a product name."""
    return [token.upper() for token in _plain(name).split()]


def own_brands(chain) -> tuple:
    """The own brands of ``chain`` (empty when the chain is unknown)."""
    return tuple(OWN_BRANDS.get(chain, ()))


def detect_own_brand(chain, name) -> str | None:
    """Return the chain's own brand carried by ``name``, or ``None``.

    Multi-word brands are matched as a phrase; single-word brands as a whole
    token, so ``DIA`` matches ``Corazones de alcachofas Dia Vegecampo`` but never
    ``Diadema``.
    """
    plain = _plain(name)
    if not plain:
        return None

    candidates = [brand for brand in own_brands(chain) if len(brand) >= _MIN_TOKEN_LENGTH]
    # Longest first: "IFA ELIGES" must win over "IFA".
    for brand in sorted(candidates, key=lambda value: -len(value)):
        needle = _plain(brand)
        if not needle:
            continue
        if " " in needle:
            if re.search(rf"(?:^|\s){re.escape(needle)}(?:\s|$)", plain):
                return brand
        elif re.search(rf"(?:^|\s){re.escape(needle)}(?:\s|$)", plain):
            return brand
    return None


def is_own_brand(chain, name) -> bool:
    """True when ``name`` carries one of the chain's own brands."""
    return detect_own_brand(chain, name) is not None
