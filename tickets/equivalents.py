"""Equivalent products across supermarket chains.

Two small, pure queries over an already-populated catalogue:

* :func:`same_product_in_other_chains` looks for the same product in every other
  chain through :func:`tickets.matching.match_line` (same fuzzy engine a ticket
  line uses, so the comparison and the ticket matching agree). A hand-made link
  stored in ``product_links`` beats the fuzzy match for its chain.
* :func:`own_brand_alternatives` proposes the own-brand products of the other
  chains that can plausibly substitute an own-brand product.

Both are honest. A chain where nothing plausible was found is **returned**, not
dropped: its ``product_id`` is ``None`` so the page can say "no equivalent". The
unit-price delta is computed only when both products share the same unit label
and both have a unit price, so a ``€/kg`` is never compared against a ``€/ud``.
"""

from __future__ import annotations

from tickets import brands, matching, store

#: Minimum blended score (0-100) for ``same_product_in_other_chains``. It is the
#: same threshold :func:`tickets.matching.match_line` defaults to, so a product
#: and a ticket line resolve to the same counterpart.
DEFAULT_THRESHOLD = 82

#: Minimum blended score (0-100) for an own-brand candidate. The brand tokens are
#: stripped before scoring — a Hacendado and a Dia product are *expected* to
#: differ in brand — so the score only weighs the description. A moderate value:
#: higher misses real equivalents, lower starts pairing unrelated products.
OWN_BRAND_THRESHOLD = 72

#: Own-brand alternatives kept per chain.
OWN_BRAND_LIMIT = 4


def _chains(conn) -> list:
    """Every chain present in the catalogue, in a deterministic order."""
    return [
        row["chain"]
        for row in conn.execute("SELECT DISTINCT chain FROM products ORDER BY chain")
    ]


def _row(conn, product_id):
    """The product Row for ``product_id`` or ``None``."""
    return conn.execute(
        "SELECT * FROM products WHERE id = ?", (product_id,)
    ).fetchone()


def _delta_unit(mine, other):
    """Unit-price difference ``other - mine``, or ``None`` when not comparable.

    Two unit prices are only comparable when both exist, both name a unit and
    the unit labels match. ``None`` is the honest answer for mismatched units
    (``€/kg`` against ``€/ud``) and for a missing price.
    """
    if mine["unit_price"] is None or other["unit_price"] is None:
        return None
    if not mine["unit_label"] or mine["unit_label"] != other["unit_label"]:
        return None
    return round(other["unit_price"] - mine["unit_price"], 4)


def _linked_by_chain(conn, product) -> dict:
    """Manual links of ``product`` keyed by the chain of the linked product.

    A hand-made link is one product per chain: if a chain somehow holds more
    than one link, the linked product with the smallest id wins, because
    :func:`tickets.store.linked_products` returns ids in that order. A link to a
    product of ``product``'s own chain is dropped: the comparison has one entry
    per *other* chain and no place to show it.
    """
    by_chain = {}
    for linked_id in store.linked_products(conn, product["id"]):
        row = _row(conn, linked_id)
        if row is None or row["chain"] == product["chain"]:
            continue
        by_chain.setdefault(row["chain"], row)
    return by_chain


def same_product_in_other_chains(conn, product, threshold=DEFAULT_THRESHOLD) -> list:
    """The counterpart of ``product`` in every other chain.

    One entry per other chain present in the catalogue, in chain order. A chain
    whose catalogue holds no match above ``threshold`` comes back with
    ``product_id`` and the product fields set to ``None`` instead of being
    dropped. ``delta_unit`` is ``None`` when the units are not comparable.

    A hand-made link wins over the fuzzy match for its chain: that entry is the
    linked product, with ``score`` ``None`` and ``source`` ``"manual"``. The
    rest carry ``source`` ``"auto"`` when the fuzzy engine matched and ``None``
    when nothing was found.
    """
    results = []
    linked_by_chain = _linked_by_chain(conn, product)
    for chain in _chains(conn):
        if chain == product["chain"]:
            continue
        linked = linked_by_chain.get(chain)
        if linked is not None:
            results.append(
                {
                    "chain": chain,
                    "product_id": linked["id"],
                    "name": linked["name"],
                    "price": linked["price"],
                    "unit_price": linked["unit_price"],
                    "unit_label": linked["unit_label"],
                    "score": None,
                    "source": "manual",
                    "delta_unit": _delta_unit(product, linked),
                    "image_file": linked["image_file"],
                }
            )
            continue
        match = matching.match_line(conn, chain, product["name"], threshold=threshold)
        if match is None:
            results.append(
                {
                    "chain": chain,
                    "product_id": None,
                    "name": None,
                    "price": None,
                    "unit_price": None,
                    "unit_label": None,
                    "score": None,
                    "source": None,
                    "delta_unit": None,
                    "image_file": None,
                }
            )
            continue
        row = _row(conn, match["product_id"])
        results.append(
            {
                "chain": chain,
                "product_id": row["id"],
                "name": row["name"],
                "price": row["price"],
                "unit_price": row["unit_price"],
                "unit_label": row["unit_label"],
                "score": match["score"],
                "source": "auto",
                "delta_unit": _delta_unit(product, row),
                "image_file": row["image_file"],
            }
        )
    return results


def _better(score, row, previous) -> bool:
    """True when ``(score, row)`` is the better substitute of the same name.

    A chain repeats one own-brand product in several pack sizes under the same
    name, so the cheapest unit price is the most actionable suggestion; the
    score only decides between equal prices.
    """
    previous_score, previous_row = previous
    new_price = row["unit_price"] if row["unit_price"] is not None else float("inf")
    old_price = (
        previous_row["unit_price"] if previous_row["unit_price"] is not None else float("inf")
    )
    if new_price != old_price:
        return new_price < old_price
    return score > previous_score


def _description(chain, name) -> str:
    """Normalised name with the chain's own-brand tokens removed.

    Two own brands never carry the same tokens, so leaving them in only drags
    every cross-brand score down. Stripping them compares the product itself
    (``Batido de chocolate Hacendado`` against ``Batido de chocolate Dia``).
    """
    tokens = matching.normalize_text(name).split()
    brand = brands.detect_own_brand(chain, name)
    if not brand:
        return " ".join(tokens)
    brand_tokens = set(matching.normalize_text(brand).split())
    return " ".join(token for token in tokens if token not in brand_tokens)


def own_brand_alternatives(conn, product, limit=OWN_BRAND_LIMIT) -> list:
    """Own-brand substitutes of an own-brand ``product``, per other chain.

    Returns an empty list when ``product`` is not own-brand: the concept does not
    apply to a national brand. Otherwise, for each other chain, the own-brand
    products with the same unit label are scored on their brand-stripped names
    and the best ``limit`` at or above :data:`OWN_BRAND_THRESHOLD` are kept.
    Every returned entry carries ``chain``, ``brand``, ``product_id``, ``name``,
    ``price``, ``unit_price``, ``unit_label`` and ``score``.
    """
    if brands.detect_own_brand(product["chain"], product["name"]) is None:
        return []
    unit = product["unit_label"]
    if not unit:
        return []

    query = _description(product["chain"], product["name"])
    results = []
    for chain in _chains(conn):
        if chain == product["chain"]:
            continue
        scored = []
        for row in conn.execute(
            "SELECT * FROM products WHERE chain = ? AND unit_label = ?", (chain, unit)
        ):
            if not brands.is_own_brand(chain, row["name"]):
                continue
            score = matching.blended_score(query, _description(chain, row["name"]))
            if score >= OWN_BRAND_THRESHOLD:
                scored.append((score, row))
        scored.sort(key=lambda item: item[0], reverse=True)
        # One entry per product name: the same own-brand product otherwise shows
        # up once per pack size, and four identical rows read as a bug.
        by_name = {}
        for score, row in scored:
            key = matching.normalize_text(row["name"])
            previous = by_name.get(key)
            if previous is None or _better(score, row, previous):
                by_name[key] = (score, row)
        ordered = sorted(by_name.values(), key=lambda item: item[0], reverse=True)
        for score, row in ordered[:limit]:
            results.append(
                {
                    "chain": chain,
                    "brand": brands.detect_own_brand(chain, row["name"]),
                    "product_id": row["id"],
                    "name": row["name"],
                    "price": row["price"],
                    "unit_price": row["unit_price"],
                    "unit_label": row["unit_label"],
                    "score": round(score, 2),
                    "image_file": row["image_file"],
                }
            )
    return results
