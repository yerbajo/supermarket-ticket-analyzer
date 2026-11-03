"""Store tests: schema init, upsert semantics, observations and aliases."""

import pytest

from tickets import store


def _product(**overrides):
    row = {
        "chain": "Mercadona",
        "external_id": "4241",
        "name": "Aceite de oliva 0,4º Hacendado",
        "format": "Garrafa 5 l",
        "price": 17.25,
        "unit_price": 3.45,
        "unit_label": "l",
        "category": "Aceite de oliva",
        "url": "https://tienda.mercadona.es/product/4241/x",
        "image_url": "https://img/4241.jpg",
        "observed_at": "2026-01-01T00:00:00Z",
    }
    row.update(overrides)
    return row


def test_init_db_is_idempotent(tmp_path):
    path = tmp_path / "twice.db"
    first = store.init_db(path)
    first.close()
    second = store.init_db(path)  # must not raise on the second call

    names = {row[0] for row in second.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"products", "price_observations", "tickets", "ticket_lines", "product_aliases", "product_links"} <= names
    second.close()


def test_upsert_product_does_not_duplicate(conn):
    store.upsert_product(conn, _product())
    store.upsert_product(conn, _product())

    assert len(store.list_products(conn, chain="Mercadona")) == 1
    found = store.find_product(conn, "Mercadona", "4241")
    assert found["name"] == "Aceite de oliva 0,4º Hacendado"


def test_observation_appended_only_on_change(conn):
    product_id = store.upsert_product(conn, _product())

    def observations():
        return conn.execute(
            "SELECT COUNT(*) FROM price_observations WHERE product_id = ?", (product_id,)
        ).fetchone()[0]

    assert observations() == 1  # initial observation
    store.upsert_product(conn, _product())  # identical -> no new observation
    assert observations() == 1

    store.upsert_product(conn, _product(price=18.00, observed_at="2026-02-01T00:00:00Z"))
    assert observations() == 2
    assert store.find_product(conn, "Mercadona", "4241")["price"] == 18.00


def test_list_products_limit_and_chain_filter(conn):
    store.upsert_product(conn, _product(external_id="1", name="A"))
    store.upsert_product(conn, _product(external_id="2", name="B"))
    store.upsert_product(conn, _product(chain="Dia", external_id="3", name="C"))

    assert len(store.list_products(conn)) == 3
    assert len(store.list_products(conn, chain="Mercadona")) == 2
    assert [row["name"] for row in store.list_products(conn, limit=1)] == ["A"]


def test_alias_add_resolve_round_trip(conn):
    product_id = store.upsert_product(conn, _product())

    store.add_alias(conn, "Mercadona", "ACEITE OLIVA 5L HACENDADO", product_id, confirmed_by="test")
    assert store.resolve_alias(conn, "Mercadona", "ACEITE OLIVA 5L HACENDADO") == product_id
    assert store.resolve_alias(conn, "Mercadona", "SIN ALIAS") is None

    # Re-adding the same raw text updates in place instead of duplicating.
    store.add_alias(conn, "Mercadona", "ACEITE OLIVA 5L HACENDADO", product_id, confirmed_by="test")
    assert conn.execute("SELECT COUNT(*) FROM product_aliases").fetchone()[0] == 1


def test_link_products_is_symmetric_and_idempotent(conn):
    a = store.upsert_product(conn, _product(external_id="A", name="A"))
    b = store.upsert_product(conn, _product(chain="Dia", external_id="B", name="B"))

    first = store.link_products(conn, a, b)
    again = store.link_products(conn, b, a)  # the other direction, same row

    assert first == again
    assert conn.execute("SELECT COUNT(*) FROM product_links").fetchone()[0] == 1
    assert store.linked_products(conn, a) == [b]
    assert store.linked_products(conn, b) == [a]


def test_link_products_refuses_a_product_linked_to_itself(conn):
    product_id = store.upsert_product(conn, _product())

    with pytest.raises(ValueError):
        store.link_products(conn, product_id, product_id)

    assert conn.execute("SELECT COUNT(*) FROM product_links").fetchone()[0] == 0


def test_unlink_products_removes_the_link_from_either_side(conn):
    a = store.upsert_product(conn, _product(external_id="A", name="A"))
    b = store.upsert_product(conn, _product(external_id="B", name="B"))
    store.link_products(conn, a, b)

    assert store.unlink_products(conn, b, a) == 1
    assert store.linked_products(conn, a) == []
    assert store.linked_products(conn, b) == []
    assert store.unlink_products(conn, a, b) == 0


def test_a_link_survives_a_catalogue_re_import(tmp_path):
    path = tmp_path / "reimport.db"
    conn = store.init_db(path)
    a = store.upsert_product(conn, _product(external_id="A", name="A"))
    b = store.upsert_product(conn, _product(external_id="B", name="B"))
    store.link_products(conn, a, b)
    conn.close()

    conn = store.init_db(path)  # same idempotent migration a CLI run does
    assert store.linked_products(conn, a) == [b]
    conn.close()


def _ticket(**overrides):
    row = {
        "chain": "MAS",
        "ticket_number": "261012/043/S/103/024205",
        "purchased_at": "2026-10-12T11:22:15",
        "total": 22.86,
        "source_pdf": "46.pdf",
        "lines": [
            {
                "raw_text": "BEBIDA SOJA S/AZ 1L",
                "qty": 8.0,
                "price_paid": 0.75,
                "total_paid": 6.00,
                "purchase_unit": "ud",
            },
            {
                "raw_text": "MELON PIEL DE SAPO",
                "qty": 1.527,
                "price_paid": 1.65,
                "total_paid": 2.52,
                "purchase_unit": "kg",
            },
        ],
    }
    row.update(overrides)
    return row


def test_migration_adds_purchase_unit_without_losing_rows(tmp_path):
    path = tmp_path / "vieja.db"
    legacy = store.connect(path)
    legacy.executescript(
        "CREATE TABLE ticket_lines ("
        "id INTEGER PRIMARY KEY, ticket_id INTEGER NOT NULL, raw_text TEXT NOT NULL, "
        "product_id INTEGER, qty REAL, price_paid REAL, total_paid REAL, "
        "match_confidence REAL, matched_by TEXT);"
    )
    legacy.execute(
        "INSERT INTO ticket_lines (ticket_id, raw_text, qty, price_paid, total_paid) "
        "VALUES (1, 'LINEA VIEJA', 1.0, 1.50, 1.50)"
    )
    legacy.commit()
    legacy.close()

    migrated = store.init_db(path)
    columns = {row["name"] for row in migrated.execute("PRAGMA table_info(ticket_lines)")}
    assert "purchase_unit" in columns
    assert migrated.execute("SELECT COUNT(*) FROM ticket_lines").fetchone()[0] == 1
    migrated.close()


def test_upsert_ticket_is_idempotent(conn):
    first_id, first_inserted = store.upsert_ticket(conn, _ticket())
    second_id, second_inserted = store.upsert_ticket(conn, _ticket())

    assert first_inserted is True
    assert second_inserted is False
    assert first_id == second_id
    assert conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM ticket_lines").fetchone()[0] == 2


def test_upsert_ticket_keeps_the_purchase_unit(conn):
    store.upsert_ticket(conn, _ticket())

    units = {
        row["raw_text"]: row["purchase_unit"]
        for row in conn.execute("SELECT raw_text, purchase_unit FROM ticket_lines")
    }
    assert units["BEBIDA SOJA S/AZ 1L"] == "ud"
    assert units["MELON PIEL DE SAPO"] == "kg"


def _amazon_offer(
    asin="A1",
    title="Asevi Detergente Marsella 44 lavados",
    price=4.25,
    unit_price=1.86,
    unit_label="l",
    unit_price_text="1,86 €/l",
    sponsored=False,
):
    return {
        "asin": asin,
        "title": title,
        "price": price,
        "unit_price": unit_price,
        "unit_label": unit_label,
        "unit_price_text": unit_price_text,
        "url": f"https://www.amazon.es/dp/{asin}",
        "sponsored": sponsored,
    }


def test_amazon_offers_table_is_created_idempotently(tmp_path):
    path = tmp_path / "amazon.db"
    store.init_db(path).close()
    again = store.init_db(path)
    names = {row[0] for row in again.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "amazon_offers" in names
    again.close()


def test_save_offers_does_not_duplicate_and_refreshes_the_price(conn):
    product_id = store.upsert_product(
        conn, _product(external_id="det", name="Detergente Marsella 44 lavados")
    )

    store.save_offers(conn, product_id, [_amazon_offer()], "detergente")
    store.save_offers(conn, product_id, [_amazon_offer(price=4.50)], "detergente")

    rows = store.offers_for(conn, product_id)
    assert len(rows) == 1
    assert rows[0]["price"] == 4.50


def test_best_offer_prefers_the_cheapest_non_sponsored_of_the_right_unit(conn):
    product_id = store.upsert_product(
        conn, _product(external_id="det", name="Detergente Marsella", unit_price=1.20, unit_label="l")
    )
    store.save_offers(
        conn,
        product_id,
        [
            _amazon_offer(asin="S1", unit_price=0.90, unit_label="l", sponsored=True),
            _amazon_offer(asin="N1", unit_price=1.10, unit_label="l"),
            _amazon_offer(asin="N2", unit_price=1.86, unit_label="l"),
            _amazon_offer(asin="KG", unit_price=0.50, unit_label="kg"),
        ],
        "detergente",
    )

    assert store.best_offer(conn, product_id)["asin"] == "N1"


def test_best_offer_falls_back_to_a_sponsored_offer_of_the_right_unit(conn):
    product_id = store.upsert_product(
        conn, _product(external_id="det", name="Detergente Marsella", unit_price=1.20, unit_label="l")
    )
    store.save_offers(
        conn,
        product_id,
        [
            _amazon_offer(asin="S1", unit_price=0.90, unit_label="l", sponsored=True),
            _amazon_offer(asin="KG", unit_price=0.50, unit_label="kg"),
        ],
        "detergente",
    )

    assert store.best_offer(conn, product_id)["asin"] == "S1"


def test_best_offer_is_none_without_offers(conn):
    product_id = store.upsert_product(conn, _product(external_id="det", name="Detergente Marsella"))
    assert store.best_offer(conn, product_id) is None


def test_amazon_candidates_exclude_own_brand_and_pick_a_branded_shampoo(conn):
    branded = store.upsert_product(
        conn,
        _product(external_id="sh", name="Champú H&S Anticaspa 2x1L", unit_price=5.0, unit_label="l"),
    )
    own = store.upsert_product(
        conn,
        _product(external_id="own", name="Champú Deliplus 750 ml", unit_price=3.0, unit_label="l"),
    )
    food = store.upsert_product(
        conn, _product(external_id="pan", name="Pan de molde Hacendado", unit_price=1.2, unit_label="ud")
    )
    no_unit = store.upsert_product(
        conn, _product(external_id="sin", name="Champú sin unidad", unit_price=None, unit_label=None)
    )

    ids = [row["id"] for row in store.amazon_candidates(conn)]

    assert branded in ids
    assert own not in ids             # own brand, on purpose
    assert food not in ids            # not a drugstore keyword
    assert no_unit not in ids         # no unit price to compare against


def test_amazon_candidates_are_ordered_deterministically_and_respect_limit(conn):
    for external_id, name in [("b", "Gel de ducha X"), ("a", "Champú H&S"), ("c", "Detergente Y")]:
        store.upsert_product(conn, _product(external_id=external_id, name=name, unit_price=1.0, unit_label="l"))

    names = [row["name"] for row in store.amazon_candidates(conn)]
    assert names == ["Champú H&S", "Detergente Y", "Gel de ducha X"]
    assert len(store.amazon_candidates(conn, limit=2)) == 2


def test_a_ticket_line_keeps_the_shop_product_code(conn):
    """The code the ticket printed is stored: it is what keeps a guess from happening."""
    store.upsert_ticket(
        conn,
        {
            "chain": "Dia",
            "ticket_number": "T-COD",
            "purchased_at": "2026-10-12T11:22:15",
            "total": 1.0,
            "lines": [
                {"raw_text": "YOGUR SABOR DIA P-16", "qty": 2, "price_paid": 2.59,
                 "total_paid": 5.18, "purchase_unit": "ud", "shop_code": "275250"}
            ],
        },
    )
    fila = conn.execute("SELECT shop_code FROM ticket_lines").fetchone()
    assert fila["shop_code"] == "275250"


def test_reimporting_with_the_shop_code_drops_an_older_guess(conn):
    """With the code in hand a match by name is a different product, so it goes."""
    store.upsert_product(
        conn,
        {
            "chain": "Dia",
            "external_id": "275251",
            "name": "Yogur sabor plátano Dia Láctea 4 x 125 g",
            "price": 0.79,
            "unit_price": 1.58,
            "unit_label": "kg",
            "observed_at": "2026-10-12T00:00:00Z",
        },
    )
    ticket = {
        "chain": "Dia",
        "ticket_number": "T-GUESS",
        "purchased_at": "2026-10-12T11:22:15",
        "total": 5.18,
        "lines": [
            {"raw_text": "YOGUR SABOR DIA P-16", "qty": 2, "price_paid": 2.59,
             "total_paid": 5.18, "purchase_unit": "ud"},
        ],
    }
    store.upsert_ticket(conn, ticket)
    # Un emparejamiento por nombre hecho antes (el que resultó ser otro envase).
    conn.execute("UPDATE ticket_lines SET product_id = 1, matched_by = 'fuzzy', match_confidence = 82.9")
    conn.commit()

    # Ahora el ticket vuelve, esta vez con el código de la tienda.
    ticket["lines"][0]["shop_code"] = "275250"
    store.upsert_ticket(conn, ticket)

    fila = conn.execute("SELECT product_id, matched_by, shop_code FROM ticket_lines").fetchone()
    assert fila["shop_code"] == "275250"
    assert fila["product_id"] is None and fila["matched_by"] is None
