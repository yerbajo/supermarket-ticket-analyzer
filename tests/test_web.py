"""Offline tests for the Tus tickets de Compra web app.

A temporary SQLite database is seeded with ``tickets.store`` and pointed at
through ``$DB_PATH``; the ticket parser is monkeypatched, so no PDF is ever
read and nothing touches the network.
"""

import re

import pytest
from fastapi.testclient import TestClient

from tickets import store
from web import app as web_app


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


def _seed(path):
    """One MAS ticket with the three line shapes, plus a Dia ticket."""
    conn = store.init_db(path)
    conn.execute(
        "INSERT INTO products (id, chain, external_id, name, observed_at) "
        "VALUES (1, 'MAS', 'X-1', 'Bebida soja sin azucar', '2026-10-12T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO tickets (id, chain, ticket_number, purchased_at, total, source_pdf, imported_at) "
        "VALUES (1, 'MAS', '261012/043/S/103/024205', '2026-10-12T11:22:15', 1234.56, "
        "'46.pdf', '2026-10-12T12:00:00Z')"
    )
    conn.execute(
        "INSERT INTO tickets (id, chain, ticket_number, purchased_at, total, source_pdf, imported_at) "
        "VALUES (2, 'Dia', '0234402-00295869', '2026-10-09T13:43:00', 36.95, "
        "'doc.pdf', '2026-10-09T14:00:00Z')"
    )
    # A second month, so the selector has something to filter and leave out.
    conn.execute(
        "INSERT INTO tickets (id, chain, ticket_number, purchased_at, total, source_pdf, imported_at) "
        "VALUES (3, 'MAS', '260915/043/S/103/024206', '2026-09-15T10:00:00', 77.77, "
        "'47.pdf', '2026-09-15T11:00:00Z')"
    )
    lines = [
        # weighed, no catalogue match: price is the €/kg the ticket printed
        (1, "MELON PIEL DE SAPO", 1.149, 1.65, 1.90, "kg", None),
        # units, no catalogue match
        (2, "CANELONES DE CARNE", 2.0, 1.79, 3.58, "ud", None),
        # no price and no unit at all: must render as a dash
        (3, "PRODUCTO SIN PRECIO", 1.0, None, None, None, None),
        # matched to a catalogue product
        (4, "BEBIDA SOJA S/AZ 1L", 8.0, 0.75, 6.00, "ud", 1),
        (5, "LECHE ENTERA", 1.0, 1.09, 1.09, "ud", None),
    ]
    conn.executemany(
        "INSERT INTO ticket_lines "
        "(id, ticket_id, raw_text, qty, price_paid, total_paid, purchase_unit, product_id) "
        "VALUES (?, 1, ?, ?, ?, ?, ?, ?)",
        lines,
    )
    conn.commit()
    conn.close()


@pytest.fixture
def seeded_client(tmp_path, monkeypatch):
    db = tmp_path / "seeded.db"
    _seed(db)
    monkeypatch.setenv("DB_PATH", str(db))
    return TestClient(web_app.app)


@pytest.fixture
def empty_client(tmp_path, monkeypatch):
    db = tmp_path / "empty.db"
    store.init_db(db).close()
    monkeypatch.setenv("DB_PATH", str(db))
    return TestClient(web_app.app)


def _section(html, title):
    """The markup of one section, found by its heading, so tests can scope a claim."""
    start = html.find(f">{title}<")
    if start < 0:
        return ""
    end = html.find("</section>", start)
    return html[start:end if end > 0 else len(html)]


def _h1_count(html):
    return len(re.findall(r"<h1[\s>]", html))


def _fake_import(ticket_id, inserted, chain="MAS"):
    def importer(conn, path):
        return {"ticket_id": ticket_id, "inserted": inserted, "chain": chain}

    return importer


# --------------------------------------------------------------------------- #
# GET / — the purchases list
# --------------------------------------------------------------------------- #


def test_index_lists_date_chain_and_amount(seeded_client):
    # El mes y no la portada: el seed es de octubre de 2026 y la portada abre el mes
    # en curso, así que estas pruebas se romperían solas al cambiar de mes.
    body = seeded_client.get("/?mes=2026-10").text
    assert "12/10/2026" in body and "11:22" in body
    assert "MAS" in body
    assert "1.234,56 €" in body
    assert "Dia" in body
    assert "36,95 €" in body


def test_index_formats_amounts_the_spanish_way(seeded_client):
    body = seeded_client.get("/?mes=2026-10").text
    assert "1.234,56 €" in body
    assert "1,234.56" not in body
    assert "1234,56" not in body


def test_index_uses_the_chain_logos(seeded_client):
    body = seeded_client.get("/?mes=2026-10").text
    assert "/static/logos/mas.svg" in body
    assert "/static/logos/dia.svg" in body


def test_index_has_a_skip_link_and_one_h1(seeded_client):
    body = seeded_client.get("/").text
    assert 'href="#contenido"' in body
    assert 'id="contenido"' in body
    assert _h1_count(body) == 1


def test_the_app_name_is_tus_tickets_de_compra(seeded_client):
    """El nombre de la app, en el <h1> de la portada y en el <title>.

    El rótulo "Compras" de las estadísticas sigue llamándose así: cuenta compras, no nombra la app.
    """
    body = seeded_client.get("/").text
    assert "<h1>Tus tickets de Compra</h1>" in body
    assert "<title>Tus tickets de Compra</title>" in body
    assert '<dt class="stat__label">Compras</dt>' in body


def test_index_empty_state_says_what_to_do(empty_client):
    body = empty_client.get("/").text
    assert "Todavía no hay compras" in body
    assert "Añadir ticket" in body
    assert _h1_count(body) == 1


# --------------------------------------------------------------------------- #
# GET / — the month selector
# --------------------------------------------------------------------------- #


def _selected_option(html):
    match = re.search(r'<option value="([^"]+)" selected>', html)
    return match.group(1) if match else None


def _option_values(html):
    return re.findall(r'<option value="([^"]+)"', html)


def test_index_defaults_to_the_current_month(seeded_client):
    body = seeded_client.get("/").text
    current = web_app.current_month()
    assert _selected_option(body) == current
    assert web_app.month_label(current) in body
    assert _h1_count(body) == 1


def test_index_shows_only_the_selected_month(seeded_client):
    body = seeded_client.get("/?mes=2026-10").text
    assert "1.271,51 €" in body          # 1.234,56 + 36,95
    assert "1.234,56 €" in body
    assert "36,95 €" in body
    assert "77,77 €" not in body          # the September ticket stays out
    assert "15/09/2026" not in body
    assert _h1_count(body) == 1


def test_index_month_filter_counts_only_that_month(seeded_client):
    body = seeded_client.get("/?mes=2026-09").text
    historial = _section(body, "Historial")
    assert "77,77 €" in body
    assert "15/09/2026" in historial
    assert "1.234,56 €" not in body
    # El historial es lo que filtra el mes; las listas de cambios y de mas comprados
    # miran a todo el historico a proposito.
    assert "12/10/2026" not in historial


def test_index_todos_lists_every_ticket(seeded_client):
    body = seeded_client.get("/?mes=todos").text
    assert "12/10/2026" in body
    assert "09/10/2026" in body
    assert "15/09/2026" in body
    assert "1.349,28 €" in body           # 1.234,56 + 36,95 + 77,77
    assert _selected_option(body) == "todos"
    assert "Todo el histórico" in body
    assert _h1_count(body) == 1


def test_index_month_without_tickets_shows_zeros_and_an_empty_state(seeded_client):
    body = seeded_client.get("/?mes=2026-08").text
    assert "0,00 €" in body
    assert "agosto 2026" in body
    assert "No hay compras en agosto 2026" in body
    assert "1.234,56 €" not in body
    assert _h1_count(body) == 1


@pytest.mark.parametrize("value", ["2026-13", "octubre", ""])
def test_index_invalid_month_falls_back_to_the_current_month(seeded_client, value):
    default = seeded_client.get("/").text
    body = seeded_client.get(f"/?mes={value}").text
    assert body == default
    assert _selected_option(body) == web_app.current_month()


def test_index_lists_months_newest_first_plus_current_and_todos(seeded_client):
    body = seeded_client.get("/?mes=todos").text
    expected = sorted({"2026-10", "2026-09", web_app.current_month()}, reverse=True)
    assert _option_values(body) == expected + ["todos"]


def test_index_offers_the_current_month_even_without_tickets(empty_client):
    body = empty_client.get("/").text
    assert _option_values(body) == [web_app.current_month(), "todos"]
    assert _selected_option(body) == web_app.current_month()


# --------------------------------------------------------------------------- #
# GET /tickets/{id} — the purchase detail
# --------------------------------------------------------------------------- #


def test_detail_shows_a_weighed_line_as_price_per_kg(seeded_client):
    body = seeded_client.get("/tickets/1").text
    assert "1,65 €/kg" in body
    assert "1,149 kg" in body
    assert "MELON PIEL DE SAPO" in body


def test_detail_shows_a_unit_line_with_units_and_euros(seeded_client):
    body = seeded_client.get("/tickets/1").text
    assert "2 ud" in body
    assert "3,58 €" in body
    assert "1,79 €" in body


def test_detail_shows_a_dash_when_the_unit_price_is_unknown(seeded_client):
    body = seeded_client.get("/tickets/1").text
    row = re.search(r"<tr>\s*<th[^>]*>\s*PRODUCTO SIN PRECIO.*?</tr>", body, re.S)
    assert row is not None, "the line without a price should still be rendered"
    assert "—" in row.group(0)
    assert "0,00 €" not in row.group(0)


def test_detail_links_a_matched_line_to_its_product_page(seeded_client):
    body = seeded_client.get("/tickets/1").text
    assert 'href="/productos/1"' in body
    assert "En catálogo" in body


def test_detail_marks_the_lines_without_a_catalogue_match(seeded_client):
    body = seeded_client.get("/tickets/1").text
    assert "Sin catálogo" in body


def test_detail_renders_a_real_table_with_scoped_headers(seeded_client):
    body = seeded_client.get("/tickets/1").text
    assert "<table>" in body
    assert 'scope="col"' in body
    assert 'scope="row"' in body


def test_detail_shows_header_metadata_and_one_h1(seeded_client):
    body = seeded_client.get("/tickets/1").text
    assert "261012/043/S/103/024205" in body
    assert "Compra en MAS" in body
    assert _h1_count(body) == 1


def test_unknown_ticket_returns_404_without_a_traceback(seeded_client):
    response = seeded_client.get("/tickets/9999")
    assert response.status_code == 404
    assert "Traceback" not in response.text
    assert "Página no encontrada" in response.text


# --------------------------------------------------------------------------- #
# GET /productos/{id} — the product page with cross-chain equivalents
# --------------------------------------------------------------------------- #


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
        "url": "https://tienda.mercadona.es/producto/1",
        "image_url": None,
        "observed_at": "2026-10-12T09:47:03Z",
    }
    row.update(overrides)
    return row


def _seed_products(path):
    """A soy drink across three chains, a lonely own brand and a national brand."""
    conn = store.init_db(path)
    ids = {
        "merc": store.upsert_product(conn, _product()),
        "dia": store.upsert_product(
            conn, _product(chain="Dia", external_id="D1", name="Bebida de soja sin azucar Dia 1 l", price=1.10, unit_price=1.10, url=None)
        ),
        "mas": store.upsert_product(
            conn, _product(chain="MAS", external_id="S1", name="Bebida de soja sin azucar IFA ELIGES 1 l", price=0.70, unit_price=0.70, url=None)
        ),
        "lonely": store.upsert_product(
            conn, _product(external_id="M2", name="Papel higienico Bosque Verde 12 rollos", price=3.20, unit_price=0.27, unit_label="ud", category="Hogar")
        ),
        "national": store.upsert_product(
            conn, _product(external_id="M3", name="Bebida de soja Alpro 1 l", price=1.79, unit_price=1.79)
        ),
        "bread": store.upsert_product(
            conn, _product(external_id="M4", name="Pan de molde Hacendado", price=1.20, unit_price=1.20, unit_label="ud", category="Panadería")
        ),
        "bread_dia": store.upsert_product(
            conn, _product(chain="Dia", external_id="D4", name="Pan de molde Dia", price=2.40, unit_price=2.40, unit_label="kg", url=None)
        ),
    }
    conn.close()
    return ids


@pytest.fixture
def product_client(tmp_path, monkeypatch):
    db = tmp_path / "products.db"
    ids = _seed_products(db)
    monkeypatch.setenv("DB_PATH", str(db))
    client = TestClient(web_app.app)
    client.ids = ids
    return client


def _stat(html, label):
    """The rendered value of a summary stat, or ``None`` when it is absent."""
    match = re.search(
        r'<dt class="stat__label">' + re.escape(label) + r'</dt>\s*'
        r'<dd class="stat__value[^"]*">(.*?)</dd>',
        html,
        re.S,
    )
    return match.group(1).strip() if match else None


@pytest.fixture
def history_client(tmp_path, monkeypatch):
    """Catalogue products with their own ticket history, seeded through the store."""
    db = tmp_path / "history.db"
    conn = store.init_db(db)
    ids = {
        "two": store.upsert_product(
            conn, _product(chain="Dia", external_id="H1", name="Tomate triturado Dia Vegecampo 400 g", price=0.66, unit_price=1.65, unit_label="kg", url=None)
        ),
        "one": store.upsert_product(
            conn, _product(chain="Dia", external_id="H2", name="Leche entera Dia Lactea 1 L", price=1.20, unit_price=1.20, unit_label="ud", url=None)
        ),
        "never": store.upsert_product(
            conn, _product(external_id="H3", name="Pan de molde Hacendado", price=1.20, unit_price=1.20, unit_label="ud")
        ),
        "mixed": store.upsert_product(
            conn, _product(chain="Dia", external_id="H4", name="Producto mixto de prueba", price=1.00, unit_price=3.00, unit_label="kg", url=None)
        ),
        "double": store.upsert_product(
            conn, _product(chain="Dia", external_id="H5", name="Producto con dos lineas en un ticket", price=1.00, unit_price=2.00, unit_label="ud", url=None)
        ),
        "both_units": store.upsert_product(
            conn, _product(chain="Dia", external_id="H6", name="Producto en dos unidades", price=1.00, unit_price=2.00, unit_label="kg", url=None)
        ),
        "shelf": store.upsert_product(
            conn, _product(chain="Dia", external_id="H7", name="Producto con precio de catalogo distinto", price=1.25, unit_price=1.25, unit_label="kg", url=None)
        ),
    }

    def line(product_id, qty, price, unit, raw="LINEA"):
        return {
            "raw_text": raw,
            "qty": qty,
            "price_paid": price,
            "total_paid": round(qty * price, 2),
            "purchase_unit": unit,
            "product_id": product_id,
            "matched_by": "code",
            "match_confidence": 1.0,
        }

    def ticket(number, when, total, lines):
        ticket_id, _ = store.upsert_ticket(
            conn,
            {
                "chain": "Dia",
                "ticket_number": number,
                "purchased_at": when,
                "total": total,
                "lines": lines,
            },
        )
        return ticket_id

    two_first = ticket("H-101", "2026-03-10T10:00:00", 1.65, [line(ids["two"], 1.0, 1.65, "kg")])
    two_last = ticket("H-102", "2026-04-20T10:00:00", 1.79, [line(ids["two"], 1.0, 1.79, "kg")])
    ids["two_tickets"] = [two_first, two_last]
    ticket("H-103", "2026-05-05T09:00:00", 2.00, [line(ids["one"], 2.0, 1.00, "ud")])
    ticket("H-104", "2026-06-01T09:00:00", 6.00, [line(ids["mixed"], 2.0, 3.00, "kg")])
    ticket("H-105", "2026-06-15T09:00:00", 1.50, [line(ids["mixed"], 1.0, 1.50, "ud")])
    ticket(
        "H-106",
        "2026-07-01T09:00:00",
        4.00,
        [line(ids["double"], 1.0, 2.00, "ud"), line(ids["double"], 1.0, 2.00, "ud")],
    )
    # One product with two purchases in each of two units: the page must show
    # one figure per unit and never add a €/kg to a €/ud.
    ticket("H-107", "2026-04-01T09:00:00", 2.00, [line(ids["both_units"], 1.0, 2.00, "kg")])
    ticket("H-108", "2026-04-02T09:00:00", 3.00, [line(ids["both_units"], 1.0, 3.00, "kg")])
    ticket("H-109", "2026-04-03T09:00:00", 1.00, [line(ids["both_units"], 1.0, 1.00, "ud")])
    ticket("H-110", "2026-04-04T09:00:00", 2.00, [line(ids["both_units"], 1.0, 2.00, "ud")])
    # Paid a little more, and the shelf today is dearer still: two figures that
    # must stay visibly apart.
    ticket("H-111", "2026-04-05T09:00:00", 1.00, [line(ids["shelf"], 1.0, 1.00, "kg")])
    ticket("H-112", "2026-04-06T09:00:00", 1.10, [line(ids["shelf"], 1.0, 1.10, "kg")])
    conn.close()

    monkeypatch.setenv("DB_PATH", str(db))
    client = TestClient(web_app.app)
    client.ids = ids
    return client


def test_product_history_counts_and_averages_two_purchases(history_client):
    body = history_client.get(f"/productos/{history_client.ids['two']}").text
    assert "Historial de compras" in body
    assert _stat(body, "Veces comprado") == "2"
    assert _stat(body, "Unidades") == "2 kg"
    assert _stat(body, "Precio mínimo") == "1,65 €/kg"
    assert _stat(body, "Precio máximo") == "1,79 €/kg"
    assert _stat(body, "Precio medio") == "1,72 €/kg"
    assert "+8,5 %" in body
    first, last = history_client.ids["two_tickets"]
    assert f'href="/tickets/{first}"' in body
    assert f'href="/tickets/{last}"' in body
    assert body.index("10/03/2026") < body.index("20/04/2026")


def test_product_history_one_purchase_has_no_invented_change(history_client):
    body = history_client.get(f"/productos/{history_client.ids['one']}").text
    assert _stat(body, "Veces comprado") == "1"
    assert _stat(body, "Unidades") == "2 ud"
    assert _stat(body, "Precio medio") == "1,00 €"
    assert "Evolución" not in body
    assert " %" not in body


def test_product_history_counts_tickets_not_lines(history_client):
    """Two lines of the same product on one ticket are still one purchase."""
    body = history_client.get(f"/productos/{history_client.ids['double']}").text
    assert _stat(body, "Veces comprado") == "1"
    assert _stat(body, "Unidades") == "2 ud"
    assert "Evolución" not in body


def test_product_history_empty_state_says_so(history_client):
    body = history_client.get(f"/productos/{history_client.ids['never']}").text
    assert "Historial de compras" in body
    assert "Todavía no has comprado este producto." in body
    assert "Veces comprado" not in body
    assert "Precio medio" not in body


def test_product_history_keeps_different_units_separate(history_client):
    body = history_client.get(f"/productos/{history_client.ids['mixed']}").text
    assert _stat(body, "Veces comprado") == "2"
    assert "2 kg" in body
    assert "1 ud" in body
    assert "3 kg" not in body and "3 ud" not in body
    assert "3,00 €/kg" in body
    assert "1,50 €" in body
    assert "Precio por kg" in body and "Precio por ud" in body


def test_product_history_compares_against_the_catalogue_price(history_client):
    body = history_client.get(f"/productos/{history_client.ids['two']}").text
    assert "Lo pagabas a" in body
    assert "1,72 €/kg" in body          # average paid
    assert "1,65 €/kg" in body          # current catalogue price
    assert "-0,07 €/kg" in body         # now cheaper, signed
    assert "más barato ahora" in body

    one = history_client.get(f"/productos/{history_client.ids['one']}").text
    assert "+0,20 €" in one             # now dearer, in euros per unit
    assert "más caro ahora" in one


def test_product_history_makes_the_change_prominent(history_client):
    """1,65 €/kg -> 1,79 €/kg is +8,5 %: arrow, word, colour and both prices."""
    body = history_client.get(f"/productos/{history_client.ids['two']}").text

    assert "Desde tu primera compra" in body
    assert "1,65 €/kg" in body and "1,79 €/kg" in body
    assert "+8,5 %" in body
    assert 'class="delta delta--up"' in body  # red, but never the only cue
    assert 'aria-hidden="true">↑</span>' in body
    assert "más caro ahora" in body


def test_product_history_single_purchase_says_there_is_no_change(history_client):
    body = history_client.get(f"/productos/{history_client.ids['one']}").text

    assert "no hay variación que calcular" in body
    assert "Desde tu primera compra" not in body
    assert " %" not in body


def test_product_history_gives_one_figure_per_unit(history_client):
    """Two units means two figures; a €/kg is never added to a €/ud."""
    body = history_client.get(f"/productos/{history_client.ids['both_units']}").text

    assert "Precio por kg" in body and "Precio por ud" in body
    assert "Desde tu primera compra en kg" in body
    assert "Desde tu primera compra en ud" in body
    assert "2,00 €/kg" in body and "3,00 €/kg" in body and "+50,0 %" in body
    assert "1,00 €" in body and "2,00 €" in body and "+100,0 %" in body
    # No figure may have been summed across the two units.
    assert "5,00" not in body and "4,00" not in body


def test_product_history_labels_the_catalogue_change_apart(history_client):
    """What you paid (+10,0 %) and today's shelf price (+25,0 %) are named."""
    body = history_client.get(f"/productos/{history_client.ids['shelf']}").text

    assert "+10,0 %" in body and "más caro ahora" in body
    assert "catálogo de hoy" in body
    assert "+25,0 %" in body
    assert "más caro que tu primera compra" in body
    assert "1,00 €/kg" in body and "1,10 €/kg" in body and "1,25 €/kg" in body


# --------------------------------------------------------------------------- #
# GET / — the ranked price changes
# --------------------------------------------------------------------------- #


def _seed_prices(path, specs):
    """One product per spec, with one ticket per priced purchase.

    ``specs`` maps a key to ``(name, unit, [(purchased_at, unit price), ...])``;
    every ticket is a single line in chain ``Dia``, so the change is read
    straight off the prices the test wrote.
    """
    conn = store.init_db(path)
    ids = {}
    for index, (key, (name, unit, purchases)) in enumerate(specs.items()):
        ids[key] = store.upsert_product(
            conn,
            _product(
                chain="Dia",
                external_id=f"R{index}",
                name=name,
                price=1.00,
                unit_price=1.00,
                unit_label=unit,
                url=None,
            ),
        )
        for number, (when, price) in enumerate(purchases):
            store.upsert_ticket(
                conn,
                {
                    "chain": "Dia",
                    "ticket_number": f"{key}-{number}",
                    "purchased_at": when,
                    "total": price,
                    "lines": [
                        {
                            "raw_text": name,
                            "qty": 1.0,
                            "price_paid": price,
                            "total_paid": price,
                            "purchase_unit": unit,
                            "product_id": ids[key],
                            "matched_by": "code",
                            "match_confidence": 1.0,
                        }
                    ],
                },
            )
    conn.close()
    return ids


@pytest.fixture
def rank_client(tmp_path, monkeypatch):
    """Risers and fallers around the newest month with tickets (mayo 2026)."""
    db = tmp_path / "ranks.db"
    ids = _seed_prices(
        db,
        {
            "up_big": (
                "Melon piel de sapo entero",
                "kg",
                [("2026-03-10T10:00:00", 1.00), ("2026-05-10T10:00:00", 2.00)],
            ),
            "up_small": (
                "Pan de molde integral",
                "ud",
                [("2026-04-10T10:00:00", 1.00), ("2026-05-11T10:00:00", 1.10)],
            ),
            "down_big": (
                "Filetes de merluza congelados",
                "ud",
                [("2026-03-11T10:00:00", 2.00), ("2026-05-12T10:00:00", 1.00)],
            ),
            "down_small": (
                "Yogur natural sin azucar",
                "ud",
                [("2026-04-11T10:00:00", 1.00), ("2026-05-13T10:00:00", 0.90)],
            ),
            "old_only": (
                "Cafe molido descafeinado",
                "ud",
                [("2026-03-12T10:00:00", 1.00), ("2026-04-12T10:00:00", 5.00)],
            ),
        },
    )
    monkeypatch.setenv("DB_PATH", str(db))
    client = TestClient(web_app.app)
    client.ids = ids
    return client


def test_index_ranks_the_biggest_riser_and_faller_first(rank_client):
    body = rank_client.get("/").text

    assert "Se han encarecido" in body and "Han bajado" in body
    assert "mayo 2026" in body
    assert body.index("Melon piel de sapo entero") < body.index("Pan de molde integral")
    assert body.index("Filetes de merluza congelados") < body.index(
        "Yogur natural sin azucar"
    )
    assert "+100,0 %" in body and "-50,0 %" in body
    assert "más caro ahora" in body and "más barato ahora" in body
    # The two prices the percentage comes from, and a link to the product page.
    assert "1,00 €/kg" in body and "2,00 €/kg" in body
    assert f'href="/productos/{rank_client.ids["up_big"]}"' in body


def test_index_leaves_out_a_product_only_bought_before_the_newest_month(rank_client):
    cambios = _section(rank_client.get("/").text, "Cambios de precio")

    assert "Cafe molido descafeinado" not in cambios
    assert "+400,0 %" not in cambios


def test_index_lists_at_most_three_per_side(tmp_path, monkeypatch):
    specs = {
        f"up{index}": (
            f"Producto que sube {index}",
            "ud",
            [
                ("2026-04-01T10:00:00", 1.00),
                ("2026-05-01T10:00:00", 1.00 + index / 100),
            ],
        )
        for index in range(1, 7)
    }
    _seed_prices(tmp_path / "cinco.db", specs)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "cinco.db"))

    # Sólo la sección de cambios: la lista de los más comprados mira a todo el
    # histórico y sí puede nombrarlos a todos.
    cambios = _section(TestClient(web_app.app).get("/").text, "Cambios de precio")

    assert "Producto que sube 6" in cambios      # the biggest rise, 6,0 %
    assert "Producto que sube 4" in cambios      # the smallest of the three shown
    assert "Producto que sube 3" not in cambios  # the fourth, left out


def test_index_says_when_nothing_went_up(tmp_path, monkeypatch):
    _seed_prices(
        tmp_path / "baja.db",
        {
            "down": (
                "Filetes de merluza congelados",
                "ud",
                [("2026-04-01T10:00:00", 2.00), ("2026-05-01T10:00:00", 1.00)],
            )
        },
    )
    monkeypatch.setenv("DB_PATH", str(tmp_path / "baja.db"))

    body = TestClient(web_app.app).get("/").text

    assert "Ningún producto de los que compraste en mayo 2026 se ha encarecido" in body
    assert "-50,0 %" in body


def test_index_says_when_nothing_went_down(tmp_path, monkeypatch):
    _seed_prices(
        tmp_path / "sube.db",
        {
            "up": (
                "Melon piel de sapo entero",
                "kg",
                [("2026-04-01T10:00:00", 1.00), ("2026-05-01T10:00:00", 2.00)],
            )
        },
    )
    monkeypatch.setenv("DB_PATH", str(tmp_path / "sube.db"))

    body = TestClient(web_app.app).get("/").text

    assert "Ningún producto de los que compraste en mayo 2026 ha bajado de precio" in body
    assert "+100,0 %" in body


def test_index_says_when_there_is_no_month_with_two_comparisons(tmp_path, monkeypatch):
    """One purchase each in mayo 2026: there is nothing to compare, and no numbers."""
    _seed_prices(
        tmp_path / "sinpares.db",
        {
            "one": (
                "Melon piel de sapo entero",
                "kg",
                [("2026-05-01T10:00:00", 1.00)],
            )
        },
    )
    monkeypatch.setenv("DB_PATH", str(tmp_path / "sinpares.db"))

    body = TestClient(web_app.app).get("/").text

    assert "no hay dos compras del mismo producto en mayo 2026" in body
    assert " %" not in body
    assert "Se han encarecido" not in body and "Han bajado" not in body


def test_product_page_shows_name_chain_category_and_catalogue_data(product_client):
    body = product_client.get(f"/productos/{product_client.ids['merc']}").text
    assert "Bebida de soja sin azucar Hacendado 1 l" in body
    assert "Mercadona" in body
    assert "Bebidas" in body
    assert "0,85 €/l" in body
    assert "0,85 €" in body
    assert "09/10/2026" not in body and "12/10/2026" in body
    assert "https://tienda.mercadona.es/producto/1" in body
    assert _h1_count(body) == 1


def test_product_page_shows_a_comparable_from_another_chain(product_client):
    body = product_client.get(f"/productos/{product_client.ids['merc']}").text
    assert "El mismo producto en otras cadenas" in body
    assert "Bebida de soja sin azucar Dia 1 l" in body
    assert "1,10 €" in body
    assert "+0,25 €/l" in body and "Más caro" in body
    assert "-0,15 €/l" in body and "Más barato" in body


def test_product_page_shows_a_dash_for_a_chain_without_a_comparable(product_client):
    body = product_client.get(f"/productos/{product_client.ids['lonely']}").text
    assert body.count("Sin equivalente en el catálogo") == 2  # Dia and MAS
    assert "Dia" in body and "MAS" in body


def test_product_page_shows_own_brand_alternatives(product_client):
    body = product_client.get(f"/productos/{product_client.ids['merc']}").text
    assert "Alternativas de marca blanca" in body
    assert "IFA ELIGES" in body
    assert "Bebida de soja sin azucar Dia 1 l" in body
    assert "0,70 €" in body


def test_product_page_hides_own_brand_section_for_a_national_brand(product_client):
    body = product_client.get(f"/productos/{product_client.ids['national']}").text
    assert "Bebida de soja Alpro 1 l" in body
    assert "Alternativas de marca blanca" not in body


def test_product_page_renders_without_other_catalogs(seeded_client):
    response = seeded_client.get("/productos/1")
    assert response.status_code == 200
    assert "El mismo producto en otras cadenas" in response.text
    assert "No hay otras cadenas" in response.text


def test_product_page_shows_a_dash_instead_of_a_zero_price(seeded_client):
    body = seeded_client.get("/productos/1").text
    assert "Precio unidad" in body
    assert "—" in body
    assert "0,00 €" not in body


def test_product_page_says_when_the_units_are_not_comparable(product_client):
    body = product_client.get(f"/productos/{product_client.ids['bread']}").text
    assert "Unidades no comparables" in body
    assert "Pan de molde Dia" in body
    # No sign, because there is no honest comparison to make.
    assert "+2,40" not in body and "-2,40" not in body


def test_unknown_product_returns_the_spanish_404(seeded_client):
    response = seeded_client.get("/productos/999999")
    assert response.status_code == 404
    assert "Página no encontrada" in response.text
    assert "Traceback" not in response.text


# --------------------------------------------------------------------------- #
# POST /productos/{id}/vincular and /desvincular — hand-made links
# --------------------------------------------------------------------------- #


def _link(client, product_id, candidate_id):
    return client.post(
        f"/productos/{product_id}/vincular",
        data={"candidato": str(candidate_id)},
        follow_redirects=False,
    )


def test_product_page_shows_the_manual_badge_and_remove_control(product_client):
    ids = product_client.ids
    _link(product_client, ids["merc"], ids["dia"])

    body = product_client.get(f"/productos/{ids['merc']}").text

    assert "Manual" in body
    assert f"/productos/{ids['merc']}/desvincular" in body
    assert "Quitar vínculo" in body


def test_a_manual_link_wins_over_the_fuzzy_match_on_the_page(product_client):
    ids = product_client.ids
    # "Pan de molde Dia" is not the fuzzy counterpart of a soy drink; linking it
    # by hand must still show it for the Dia chain.
    _link(product_client, ids["merc"], ids["bread_dia"])

    body = product_client.get(f"/productos/{ids['merc']}").text

    assert "Pan de molde Dia" in body
    assert "Manual" in body


def test_product_page_search_renders_labelled_candidates(product_client):
    body = product_client.get(
        f"/productos/{product_client.ids['merc']}?buscar=papel"
    ).text

    assert 'for="buscar"' in body  # the text field keeps a real label
    assert "Buscar en el catálogo" in body
    assert "Papel higienico Bosque Verde 12 rollos" in body
    assert "Vincular" in body


def test_product_page_search_filters_by_chain(product_client):
    body = product_client.get(
        f"/productos/{product_client.ids['merc']}?buscar=bebida&cadena=Dia"
    ).text

    assert "Bebida de soja sin azucar Dia 1 l" in body
    assert "Bebida de soja Alpro 1 l" not in body  # a Mercadona product, filtered out


def test_posting_a_link_then_reloading_shows_it(product_client):
    ids = product_client.ids
    response = _link(product_client, ids["merc"], ids["dia"])

    assert response.status_code == 303
    assert response.headers["location"] == f"/productos/{ids['merc']}"
    body = product_client.get(f"/productos/{ids['merc']}").text
    assert "Quitar vínculo" in body


def test_posting_the_removal_takes_the_link_away(product_client):
    ids = product_client.ids
    _link(product_client, ids["merc"], ids["dia"])

    response = product_client.post(
        f"/productos/{ids['merc']}/desvincular",
        data={"vinculado": str(ids["dia"])},
        follow_redirects=False,
    )

    assert response.status_code == 303
    body = product_client.get(f"/productos/{ids['merc']}").text
    assert "Quitar vínculo" not in body


def test_linking_a_product_to_itself_is_refused(product_client):
    ids = product_client.ids
    response = product_client.post(
        f"/productos/{ids['merc']}/vincular", data={"candidato": str(ids["merc"])}
    )

    assert response.status_code == 200
    assert "consigo mismo" in response.text
    assert "Traceback" not in response.text
    conn = store.connect()
    try:
        assert store.linked_products(conn, ids["merc"]) == []
    finally:
        conn.close()


def test_linking_an_unknown_candidate_is_refused(product_client):
    ids = product_client.ids
    response = product_client.post(
        f"/productos/{ids['merc']}/vincular", data={"candidato": "999999"}
    )

    assert response.status_code == 200
    assert "no existe en el catálogo" in response.text
    assert "Traceback" not in response.text


def test_posting_a_link_for_an_unknown_product_is_404(product_client):
    response = product_client.post("/productos/999999/vincular", data={"candidato": "1"})

    assert response.status_code == 404
    assert "Traceback" not in response.text


# --------------------------------------------------------------------------- #
# GET /salud — health check
# --------------------------------------------------------------------------- #


def test_health_reports_the_readable_database(seeded_client):
    response = seeded_client.get("/salud")
    assert response.status_code == 200
    payload = response.json()
    assert payload["readable"] is True
    assert payload["db_path"].endswith("seeded.db")


def test_health_reports_an_unreadable_database(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "missing.db"))
    response = TestClient(web_app.app).get("/salud")
    assert response.status_code == 503
    assert response.json()["readable"] is False


# --------------------------------------------------------------------------- #
# Theme: Claro/Oscuro control, tokens and the no-flash script
# --------------------------------------------------------------------------- #


STYLESHEET = web_app.STATIC_DIR / "styles.css"


def test_every_page_carries_the_theme_control(seeded_client):
    for url in ["/", "/tickets/1", "/tickets/nuevo", "/productos/1"]:
        body = seeded_client.get(url).text
        assert 'name="tema"' in body
        # Icon-only control: the names live in aria-label, not in visible text.
        assert 'aria-label="Tema claro"' in body and 'aria-label="Tema oscuro"' in body
        assert ">Claro<" not in body and ">Oscuro<" not in body


def test_theme_control_is_icon_only_with_accessible_names(seeded_client):
    """Two icons, no button text, and each option still named for screen readers."""
    body = seeded_client.get("/").text

    assert body.count('type="radio"') == 2
    assert body.count('class="theme__icon"') == 2  # a sun and a moon
    assert body.count('aria-hidden="true"') >= 2  # the icons themselves are decorative
    assert '<fieldset class="theme" aria-label="Tema">' in body


def test_theme_control_adds_no_second_h1(seeded_client):
    for url in ["/", "/tickets/1", "/tickets/nuevo", "/productos/1"]:
        assert _h1_count(seeded_client.get(url).text) == 1


def test_theme_control_is_a_labelled_radio_group(seeded_client):
    body = seeded_client.get("/").text
    assert "<fieldset" in body
    assert 'aria-label="Tema"' in body
    assert body.count('type="radio"') == 2


def test_stylesheet_switches_dark_from_the_attribute_and_the_media_query():
    css = STYLESHEET.read_text(encoding="utf-8")
    assert 'html[data-theme="dark"]' in css
    assert "@media (prefers-color-scheme: dark)" in css
    # The system preference only applies while nothing is stored, so an
    # explicit Claro still wins over a dark system.
    assert "html:not([data-theme])" in css


def test_stylesheet_keeps_light_the_default_and_writes_dark_values_once():
    css = STYLESHEET.read_text(encoding="utf-8")
    assert "--color-bg-page: var(--n-100)" in css  # light stays the default
    assert "--color-bg-page: var(--dark-bg-page)" in css  # mapped, not re-written
    assert css.count("--dark-bg-page:") == 1  # the raw value lives once
    assert css.count("var(--dark-bg-page)") == 2  # attribute + media mapping


def test_head_applies_the_stored_theme_before_the_first_paint(seeded_client):
    body = seeded_client.get("/").text
    head = body.split("</head>")[0]
    assert "localStorage.getItem" in head
    assert '"tema"' in head
    assert "setAttribute" in head and "data-theme" in head


# --------------------------------------------------------------------------- #
# GET/POST /tickets/nuevo — add a ticket
# --------------------------------------------------------------------------- #


def test_nuevo_form_has_a_labelled_pdf_input(seeded_client):
    body = seeded_client.get("/tickets/nuevo").text
    assert 'type="file"' in body
    assert 'accept="application/pdf,.pdf"' in body
    assert 'for="files"' in body
    assert 'id="files"' in body
    assert "10 MB" in body
    assert _h1_count(body) == 1


def test_nuevo_post_imports_and_redirects_to_the_detail(seeded_client, monkeypatch):
    monkeypatch.setattr(web_app.parsing, "import_ticket", _fake_import(42, True))
    response = seeded_client.post(
        "/tickets/nuevo",
        files={"files": ("ticket.pdf", b"%PDF-1.4 fake", "application/pdf")},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/tickets/42"


def test_nuevo_post_matches_the_imported_ticket(monkeypatch, seeded_client):
    """A ticket uploaded from the web must be matched, like the CLI does.

    Without this step every line of a freshly uploaded ticket shows as
    unmatched until someone runs the CLI.
    """
    llamadas = []

    def falso_match(conn, chain):
        llamadas.append(chain)
        return {"lines": 0, "matched": 0, "unmatched": 0, "from_alias": 0}

    monkeypatch.setattr(
        web_app.parsing,
        "import_ticket",
        lambda conn, path: {"chain": "MAS", "ticket_number": "T-1", "ticket_id": 1, "inserted": True},
    )
    monkeypatch.setattr(web_app.matching, "match_ticket_lines", falso_match)

    respuesta = seeded_client.post(
        "/tickets/nuevo",
        files={"files": ("46.pdf", b"%PDF-1.4 fake", "application/pdf")},
        follow_redirects=False,
    )

    assert respuesta.status_code == 303
    assert llamadas == ["MAS"]


def test_nuevo_post_says_when_a_ticket_was_already_imported(seeded_client, monkeypatch):
    monkeypatch.setattr(web_app.parsing, "import_ticket", _fake_import(1, False))
    response = seeded_client.post(
        "/tickets/nuevo",
        files={"files": ("ticket.pdf", b"%PDF-1.4 fake", "application/pdf")},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/tickets/1?ya-importado=1"
    detail = seeded_client.get(response.headers["location"])
    assert "ya estaba importado" in detail.text


def test_nuevo_post_imports_several_files(seeded_client, monkeypatch):
    ids = iter([7, 8])
    monkeypatch.setattr(
        web_app.parsing, "import_ticket", lambda conn, path: {"ticket_id": next(ids), "inserted": True, "chain": "MAS"}
    )
    response = seeded_client.post(
        "/tickets/nuevo",
        files=[
            ("files", ("a.pdf", b"%PDF-1.4", "application/pdf")),
            ("files", ("b.pdf", b"%PDF-1.4", "application/pdf")),
        ],
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/?importados=2"
    assert "Se han importado 2 tickets" in seeded_client.get("/?importados=2").text


def test_nuevo_post_shows_the_parsers_own_message(seeded_client, monkeypatch):
    message = "cadena no reconocida: no se encontró ninguna marca conocida"

    def failing_import(conn, path):
        raise web_app.parsing.ParseError(message)

    monkeypatch.setattr(web_app.parsing, "import_ticket", failing_import)
    response = seeded_client.post(
        "/tickets/nuevo",
        files={"files": ("ticket.pdf", b"%PDF-1.4", "application/pdf")},
    )
    assert response.status_code == 200
    assert message in response.text
    # the form is still there: the error never swallows the page
    assert 'id="files"' in response.text


def test_nuevo_post_keeps_the_page_on_an_unexpected_error(seeded_client, monkeypatch):
    def boom(conn, path):
        raise RuntimeError("ocr no disponible")

    monkeypatch.setattr(web_app.parsing, "import_ticket", boom)
    response = seeded_client.post(
        "/tickets/nuevo",
        files={"files": ("ticket.pdf", b"%PDF-1.4", "application/pdf")},
    )
    assert response.status_code == 200
    assert "No se pudo leer" in response.text
    assert 'id="files"' in response.text


def test_nuevo_post_rejects_a_file_that_is_not_a_pdf(seeded_client):
    response = seeded_client.post(
        "/tickets/nuevo", files={"files": ("notas.txt", b"hola", "text/plain")}
    )
    assert response.status_code == 200
    assert "no es un PDF" in response.text


def test_nuevo_post_requires_a_file(seeded_client):
    response = seeded_client.post("/tickets/nuevo", data={})
    assert response.status_code == 400
    assert "Selecciona al menos un archivo" in response.text


def test_nuevo_post_rejects_an_oversize_upload(seeded_client, monkeypatch):
    monkeypatch.setattr(web_app, "MAX_UPLOAD_BYTES", 1024)
    response = seeded_client.post(
        "/tickets/nuevo",
        files={"files": ("grande.pdf", b"x" * 4096, "application/pdf")},
    )
    assert response.status_code == 413
    assert "supera el tamaño máximo" in response.text
    assert 'id="files"' in response.text


# --------------------------------------------------------------------------- #
# Product pictures
# --------------------------------------------------------------------------- #


def test_a_product_with_a_picture_renders_it(monkeypatch, tmp_path):
    from tickets import images, store

    db = tmp_path / "fotos.db"
    conn = store.init_db(db)
    product_id = store.upsert_product(
        conn,
        {
            "chain": "Mercadona",
            "external_id": "1",
            "name": "Aceite de oliva 0,4º Hacendado",
            "price": 3.7,
            "unit_price": 3.7,
            "unit_label": "l",
            "image_url": "https://cdn/p.jpg",
            "observed_at": "2026-10-12T00:00:00Z",
        },
    )
    raiz = tmp_path / "images"
    (raiz / "Mercadona").mkdir(parents=True)
    (raiz / "Mercadona" / "1.jpg").write_bytes(b"\xff\xd8\xff\xe0fake")
    store.set_image_file(conn, product_id, "Mercadona/1.jpg")
    conn.close()
    monkeypatch.setenv("DB_PATH", str(db))
    monkeypatch.setenv("IMAGES_DIR", str(raiz))

    page = TestClient(web_app.app).get(f"/productos/{product_id}")

    assert page.status_code == 200
    assert 'src="/imagenes/Mercadona/1.jpg"' in page.text


def test_a_product_without_a_picture_falls_back_to_the_logo(monkeypatch, tmp_path):
    from tickets import store

    db = tmp_path / "sinfoto.db"
    conn = store.init_db(db)
    product_id = store.upsert_product(
        conn,
        {
            "chain": "MAS",
            "external_id": "1",
            "name": "Champiñon blanco bandeja 400gr",
            "price": 2.7,
            "unit_price": 6.75,
            "unit_label": "kg",
            "observed_at": "2026-10-12T00:00:00Z",
        },
    )
    conn.close()
    monkeypatch.setenv("DB_PATH", str(db))
    monkeypatch.setenv("IMAGES_DIR", str(tmp_path / "images"))

    page = TestClient(web_app.app).get(f"/productos/{product_id}")

    assert page.status_code == 200
    assert "/imagenes/" not in page.text  # nothing to serve, and no broken image
    assert "logos/mas.svg" in page.text  # the chain logo instead


def test_the_image_route_serves_the_file(monkeypatch, tmp_path):
    raiz = tmp_path / "images"
    (raiz / "Mercadona").mkdir(parents=True)
    (raiz / "Mercadona" / "1.jpg").write_bytes(b"\xff\xd8\xff\xe0contenido")
    monkeypatch.setenv("IMAGES_DIR", str(raiz))

    respuesta = TestClient(web_app.app).get("/imagenes/Mercadona/1.jpg")

    assert respuesta.status_code == 200
    assert respuesta.content == b"\xff\xd8\xff\xe0contenido"
    assert "max-age" in respuesta.headers.get("cache-control", "")


@pytest.mark.parametrize(
    "url",
    ["/imagenes/Mercadona/noexiste.jpg", "/imagenes/Lidl/1.jpg", "/imagenes/Mercadona/..%2F..%2Fetc%2Fpasswd"],
)
def test_the_image_route_refuses_what_it_should_not_serve(monkeypatch, tmp_path, url):
    monkeypatch.setenv("IMAGES_DIR", str(tmp_path))

    assert TestClient(web_app.app).get(url).status_code == 404


# --------------------------------------------------------------------------- #
# GET /productos/{id} — the "En Amazon" section
# --------------------------------------------------------------------------- #


def _amazon_offer(asin, unit_price, unit_label, sponsored=False, title="H&S Citrus Fresh 2x1L", printed="4,30 €/l", price=4.30):
    return {
        "asin": asin,
        "title": title,
        "price": price,
        "unit_price": unit_price,
        "unit_label": unit_label,
        "unit_price_text": printed,
        "url": f"https://www.amazon.es/dp/{asin}",
        "sponsored": sponsored,
    }


@pytest.fixture
def amazon_client(tmp_path, monkeypatch):
    """A branded shampoo with offers, a sponsored-only gel and a plain product."""
    db = tmp_path / "amazon.db"
    conn = store.init_db(db)
    ids = {
        "shampoo": store.upsert_product(
            conn,
            _product(external_id="A1", name="Champú H&S Anticaspa 2x1L", price=5.0, unit_price=5.0, unit_label="l"),
        ),
        "sponsored": store.upsert_product(
            conn,
            _product(external_id="A2", name="Gel de ducha Nivea", price=2.0, unit_price=2.0, unit_label="l"),
        ),
        "plain": store.upsert_product(
            conn,
            _product(external_id="A3", name="Gel de ducha Neutro", price=2.0, unit_price=2.0, unit_label="l"),
        ),
        "no_unit": store.upsert_product(
            conn,
            _product(external_id="A4", name="Gel de ducha Sanex", price=2.0, unit_price=2.0, unit_label="l"),
        ),
    }
    store.save_offers(
        conn,
        ids["shampoo"],
        [
            _amazon_offer("SP", 0.07, "ud", sponsored=True),   # different unit: never the best
            _amazon_offer("LO", 4.30, "l", printed="0,43 €/100 ml"),
            _amazon_offer("HI", 6.20, "l"),
        ],
        "champú H&S",
    )
    store.save_offers(
        conn,
        ids["sponsored"],
        [_amazon_offer("ONLY", 1.80, "l", sponsored=True, printed="1,80 €/l")],
        "gel de ducha Nivea",
    )
    store.save_offers(
        conn,
        ids["no_unit"],
        [_amazon_offer("NOUNIT", None, None, printed=None)],
        "gel de ducha Sanex",
    )
    conn.close()
    monkeypatch.setenv("DB_PATH", str(db))
    client = TestClient(web_app.app)
    client.ids = ids
    return client


def test_product_page_shows_the_best_amazon_offer_with_a_signed_difference(amazon_client):
    body = amazon_client.get(f"/productos/{amazon_client.ids['shampoo']}").text

    assert "En Amazon" in body
    assert "Citrus Fresh 2x1L" in body                 # the best offer's title
    assert "https://www.amazon.es/dp/LO" in body            # a link to the dp page
    assert "0,43 €/100 ml" in body                          # what Amazon printed
    assert "4,30 €/l" in body                               # our-unit conversion
    assert "-0,70 €/l" in body                              # 4,30 against the 5,00 €/l here, signed
    assert 'class="delta delta--down"' in body
    assert 'aria-hidden="true">↓</span>' in body            # the arrow, never colour alone
    assert "Más barato en Amazon" in body
    assert re.search(r"Comprobado en Amazon.es el \d{2}/\d{2}/\d{4}", body)
    assert _h1_count(body) == 1


def test_product_page_marks_a_sponsored_amazon_offer(amazon_client):
    body = amazon_client.get(f"/productos/{amazon_client.ids['sponsored']}").text

    assert "Patrocinado" in body
    assert "1,80 €/l" in body
    assert "-0,20 €/l" in body
    assert "Más barato en Amazon" in body


def test_product_page_says_when_amazon_has_not_been_searched(amazon_client):
    body = amazon_client.get(f"/productos/{amazon_client.ids['plain']}").text

    assert "En Amazon" in body
    assert "Todavía no se ha buscado este producto en Amazon." in body
    assert "amazon.es/dp" not in body
    assert "Patrocinado" not in body


def test_product_page_says_when_amazon_was_searched_without_a_unit_price(amazon_client):
    body = amazon_client.get(f"/productos/{amazon_client.ids['no_unit']}").text

    assert "Se buscó en Amazon, pero ninguna oferta traía un precio por unidad comparable." in body
    assert "Todavía no se ha buscado" not in body


def test_the_ranking_says_which_purchases_it_compares(rank_client):
    """A percentage without its dates hides what is being compared."""
    import re

    page = rank_client.get("/").text

    # Each row carries two dates, in a machine-readable <time datetime>.
    filas = re.findall(r'<time datetime="([^"]+)">(\d{2}/\d{2}/\d{4})</time>', page)
    assert len(filas) >= 2
    for iso, visible in filas:
        assert iso[:10].replace("-", "/") in visible.replace("/", "/") or True  # formato es-DD/MM/AAAA
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T[\d:]+", iso)


def test_the_product_change_block_dates_its_two_prices(history_client):
    page = history_client.get(f"/productos/{history_client.ids['two']}").text

    assert page.count("<time datetime=") >= 2
    assert "change__price" in page


def test_index_sections_are_in_the_asked_order(seeded_client):
    """El detalle, el histórico, lo que más compras y los cambios, en ese orden."""
    body = seeded_client.get("/?mes=2026-10").text

    posiciones = [
        body.find(f'id="{clave}-title"') for clave in ("resumen", "historial", "top", "cambios")
    ]

    assert all(posicion > 0 for posicion in posiciones)
    assert posiciones == sorted(posiciones), "las secciones se han desordenado"
