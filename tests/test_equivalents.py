"""Equivalence tests: seeded catalogue, offline, no network.

The catalogue is seeded directly with :func:`tickets.store.upsert_product`; the
fuzzy engine is the same :mod:`tickets.matching` the app uses, so the tests pin
the behaviour the page depends on: a counterpart per chain, an explicit ``None``
when there is none, no cross-unit delta, and own-brand alternatives that never
include a national brand.
"""

import pytest

from tickets import equivalents, store


def _product(**overrides):
    row = {
        "chain": "Mercadona",
        "external_id": "M1",
        "name": "Bebida de soja sin azucar Hacendado 1 l",
        "format": None,
        "price": 0.85,
        "unit_price": 0.85,
        "unit_label": "l",
        "category": "Bebidas",
        "url": None,
        "image_url": None,
        "observed_at": "2026-10-12T00:00:00Z",
    }
    row.update(overrides)
    return row


def _seed_three_chains(conn):
    """The same soy drink as a Mercadona, a Dia and a MAS own brand."""
    hacendado = store.upsert_product(conn, _product())
    dia = store.upsert_product(
        conn,
        _product(
            chain="Dia",
            external_id="D1",
            name="Bebida de soja sin azucar Dia 1 l",
            price=1.10,
            unit_price=1.10,
        ),
    )
    mas = store.upsert_product(
        conn,
        _product(
            chain="MAS",
            external_id="S1",
            name="Bebida de soja sin azucar IFA ELIGES 1 l",
            price=0.70,
            unit_price=0.70,
        ),
    )
    return hacendado, dia, mas


def _load(conn, product_id):
    return conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()


# --------------------------------------------------------------------------- #
# same_product_in_other_chains
# --------------------------------------------------------------------------- #


def test_finds_the_comparable_in_each_other_chain(conn):
    hacendado, dia, mas = _seed_three_chains(conn)

    result = equivalents.same_product_in_other_chains(conn, _load(conn, hacendado))

    by_chain = {item["chain"]: item for item in result}
    assert set(by_chain) == {"Dia", "MAS"}  # never the product's own chain
    assert by_chain["Dia"]["product_id"] == dia
    assert by_chain["MAS"]["product_id"] == mas
    assert by_chain["Dia"]["name"] == "Bebida de soja sin azucar Dia 1 l"
    assert by_chain["Dia"]["unit_price"] == pytest.approx(1.10)
    assert by_chain["Dia"]["unit_label"] == "l"
    assert by_chain["Dia"]["score"] >= equivalents.DEFAULT_THRESHOLD


def test_a_chain_without_a_comparable_is_returned_with_none(conn):
    mine = store.upsert_product(conn, _product())
    store.upsert_product(
        conn,
        _product(
            chain="Dia",
            external_id="D1",
            name="Bebida de soja sin azucar Dia 1 l",
            price=1.10,
            unit_price=1.10,
        ),
    )
    # A catalogue with nothing comparable, not a missing chain: it must render.
    store.upsert_product(
        conn,
        _product(
            chain="MAS",
            external_id="S1",
            name="Detergente Ariel 30 lavados",
            price=12.0,
            unit_price=12.0,
            unit_label="ud",
        ),
    )

    result = equivalents.same_product_in_other_chains(conn, _load(conn, mine))

    by_chain = {item["chain"]: item for item in result}
    assert by_chain["MAS"]["product_id"] is None
    assert by_chain["MAS"]["name"] is None
    assert by_chain["MAS"]["price"] is None
    assert by_chain["MAS"]["delta_unit"] is None
    assert by_chain["MAS"]["source"] is None  # nothing was found there
    assert by_chain["Dia"]["product_id"] is not None
    assert by_chain["Dia"]["source"] == "auto"


def test_delta_is_signed_against_the_product(conn):
    hacendado, _, _ = _seed_three_chains(conn)

    by_chain = {
        item["chain"]: item
        for item in equivalents.same_product_in_other_chains(conn, _load(conn, hacendado))
    }

    assert by_chain["Dia"]["delta_unit"] == pytest.approx(0.25)  # costlier there
    assert by_chain["MAS"]["delta_unit"] == pytest.approx(-0.15)  # cheaper there


def test_no_delta_when_the_units_do_not_match(conn):
    mine = store.upsert_product(
        conn, _product(name="Pan de molde Hacendado", unit_price=1.20, unit_label="ud")
    )
    store.upsert_product(
        conn,
        _product(
            chain="Dia",
            external_id="D1",
            name="Pan de molde Dia",
            unit_price=2.40,
            unit_label="kg",
        ),
    )

    result = {i["chain"]: i for i in equivalents.same_product_in_other_chains(conn, _load(conn, mine))}

    assert result["Dia"]["product_id"] is not None
    assert result["Dia"]["unit_label"] == "kg"
    assert result["Dia"]["delta_unit"] is None  # never €/ud against €/kg


def test_no_delta_when_a_unit_price_is_missing(conn):
    mine = store.upsert_product(conn, _product(name="Pan de molde Hacendado", unit_price=None, unit_label="ud"))
    store.upsert_product(
        conn,
        _product(chain="Dia", external_id="D1", name="Pan de molde Dia", unit_price=2.40, unit_label="ud"),
    )

    result = {i["chain"]: i for i in equivalents.same_product_in_other_chains(conn, _load(conn, mine))}

    assert result["Dia"]["product_id"] is not None
    assert result["Dia"]["delta_unit"] is None


# --------------------------------------------------------------------------- #
# Manual links beat the fuzzy match
# --------------------------------------------------------------------------- #


def test_a_manual_link_beats_the_fuzzy_match_for_that_chain(conn):
    hacendado, dia, mas = _seed_three_chains(conn)
    # A Dia product the fuzzy engine would never pick for a soy drink, linked by
    # hand because the person knows the equivalence the system cannot see.
    manual = store.upsert_product(
        conn,
        _product(
            chain="Dia",
            external_id="D-HAND",
            name="Agua mineral Dia 1,5 l",
            price=0.45,
            unit_price=0.30,
        ),
    )
    store.link_products(conn, hacendado, manual)

    by_chain = {
        item["chain"]: item
        for item in equivalents.same_product_in_other_chains(conn, _load(conn, hacendado))
    }

    assert by_chain["Dia"]["product_id"] == manual
    assert by_chain["Dia"]["name"] == "Agua mineral Dia 1,5 l"
    assert by_chain["Dia"]["source"] == "manual"
    assert by_chain["Dia"]["score"] is None
    assert by_chain["Dia"]["delta_unit"] == pytest.approx(0.30 - 0.85)  # units respected
    # The chains without a link keep the fuzzy result untouched.
    assert by_chain["MAS"]["product_id"] == mas
    assert by_chain["MAS"]["source"] == "auto"


def test_a_link_to_a_product_of_its_own_chain_is_ignored(conn):
    hacendado, dia, mas = _seed_three_chains(conn)
    sibling = store.upsert_product(
        conn, _product(external_id="M-OTHER", name="Otro producto Hacendado")
    )
    store.link_products(conn, hacendado, sibling)

    by_chain = {
        item["chain"]: item
        for item in equivalents.same_product_in_other_chains(conn, _load(conn, hacendado))
    }

    # One entry per other chain: a same-chain link has no place and is dropped.
    assert set(by_chain) == {"Dia", "MAS"}
    assert by_chain["Dia"]["product_id"] == dia
    assert by_chain["Dia"]["source"] == "auto"


# --------------------------------------------------------------------------- #
# own_brand_alternatives
# --------------------------------------------------------------------------- #


def test_own_brand_alternatives_are_only_own_brands(conn):
    hacendado, dia, mas = _seed_three_chains(conn)
    store.upsert_product(
        conn,
        _product(
            chain="Dia",
            external_id="D2",
            name="Bebida de soja Alpro 1 l",
            price=1.79,
            unit_price=1.79,
        ),
    )

    alternatives = equivalents.own_brand_alternatives(conn, _load(conn, hacendado))

    ids = {item["product_id"] for item in alternatives}
    assert dia in ids and mas in ids
    assert all(item["brand"] for item in alternatives)
    assert "Alpro" not in " ".join(item["name"] for item in alternatives)


def test_a_national_brand_has_no_own_brand_alternatives(conn):
    mine = store.upsert_product(
        conn, _product(name="Bebida de soja Alpro 1 l", price=1.79, unit_price=1.79)
    )
    store.upsert_product(
        conn,
        _product(chain="Dia", external_id="D1", name="Bebida de soja Dia 1 l", price=0.75, unit_price=0.75),
    )

    assert equivalents.own_brand_alternatives(conn, _load(conn, mine)) == []


def test_own_brand_alternatives_require_the_same_unit(conn):
    mine = store.upsert_product(conn, _product())
    store.upsert_product(
        conn,
        _product(
            chain="Dia",
            external_id="D1",
            name="Bebida de soja sin azucar Dia 6 ud",
            price=3.00,
            unit_price=3.00,
            unit_label="ud",
        ),
    )

    assert equivalents.own_brand_alternatives(conn, _load(conn, mine)) == []


def test_own_brand_alternatives_are_capped_per_chain(conn):
    mine = store.upsert_product(conn, _product())
    for index in range(6):
        store.upsert_product(
            conn,
            _product(
                chain="Dia",
                external_id=f"D{index}",
                name=f"Bebida de soja sin azucar Dia variante {index} 1 l",
                price=0.75 + index / 100,
                unit_price=0.75 + index / 100,
            ),
        )

    alternatives = equivalents.own_brand_alternatives(conn, _load(conn, mine), limit=2)

    assert len(alternatives) == 2
    assert all(item["chain"] == "Dia" for item in alternatives)


def test_own_brand_alternatives_collapse_pack_sizes(conn):
    """A chain repeats one own-brand product in several packs under one name."""
    mine = store.upsert_product(
        conn,
        _product(name="Chocolate a la taza Hacendado", price=2.45, unit_price=2.45),
    )
    for price in (2.45, 1.95, 3.10):
        store.upsert_product(
            conn,
            _product(
                chain="MAS",
                external_id=f"M-{price}",
                name="Chocolate a la taza IFA ELIGES brick 1l",
                price=price,
                unit_price=price,
            ),
        )

    alternatives = equivalents.own_brand_alternatives(conn, _load(conn, mine))
    mas = [item for item in alternatives if item["chain"] == "MAS"]

    assert len(mas) == 1  # one row per name, not one per pack size
    assert mas[0]["unit_price"] == pytest.approx(1.95)  # the cheapest per unit
