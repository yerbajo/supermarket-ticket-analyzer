"""Report tests: seeded SQLite store, offline, no network.

The store is seeded directly (``store.upsert_product`` plus raw INSERTs) so the
report is exercised without any connector or PDF parsing.
"""

import re

import pytest

from reports import report
from tickets import store

SCRIPT_NAME = "Oferta <script>alert(1)</script> 1 l"
ESCAPED_SCRIPT = "&lt;script&gt;alert(1)&lt;/script&gt;"


def _product(**overrides):
    row = {
        "chain": "Mercadona",
        "external_id": "M1",
        "name": "Pechuga de pollo bandeja 1 kg",
        "format": None,
        "price": 6.00,
        "unit_price": 6.00,
        "unit_label": "kg",
        "category": "Carne",
        "url": None,
        "image_url": None,
        "observed_at": "2026-01-01T00:00:00Z",
    }
    row.update(overrides)
    return row


def _ticket(conn, chain, number, purchased_at, total):
    cursor = conn.execute(
        "INSERT INTO tickets (chain, ticket_number, purchased_at, total, source_pdf, imported_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (chain, number, purchased_at, total, None, "2026-01-01T00:00:00Z"),
    )
    conn.commit()
    return cursor.lastrowid


def _line(conn, ticket_id, raw_text, product_id=None, total_paid=None, price_paid=None,
          purchase_unit=None):
    matched_by = "alias" if product_id is not None else None
    confidence = 1.0 if product_id is not None else None
    if price_paid is None:
        price_paid = total_paid
    cursor = conn.execute(
        "INSERT INTO ticket_lines "
        "(ticket_id, raw_text, product_id, qty, price_paid, total_paid, "
        " match_confidence, matched_by, purchase_unit) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (ticket_id, raw_text, product_id, 1.0, price_paid, total_paid,
         confidence, matched_by, purchase_unit),
    )
    conn.commit()
    return cursor.lastrowid


@pytest.fixture
def seeded(conn):
    """Two chains with a real comparable, a third with none, one unmatched line.

    Mercadona is where the tickets come from; Dia holds the comparable product
    at a different price; MAS holds an unrelated product, so it must never be
    presented as a comparable.
    """
    pechuga = store.upsert_product(conn, _product())
    script = store.upsert_product(
        conn,
        _product(
            external_id="M2",
            name=SCRIPT_NAME,
            price=3.20,
            unit_price=3.20,
            unit_label="l",
            category="Bebidas",
        ),
    )
    dia = store.upsert_product(
        conn, _product(chain="Dia", external_id="D1", price=5.50, unit_price=5.50)
    )
    store.upsert_product(
        conn,
        _product(
            chain="Supermercados MAS",
            external_id="S1",
            name="Chorizo artesano dulce",
            category="Embutidos",
            price=3.20,
            unit_price=12.80,
            unit_label="kg",
        ),
    )

    t1 = _ticket(conn, "Mercadona", "A1", "2026-01-05T10:00:00Z", 8.50)
    _line(conn, t1, "PECHUGA POLLO BANDEJA 1 KG", product_id=pechuga, total_paid=6.00)
    _line(conn, t1, "DETERGENTE ARIEL 30 LAVADOS", product_id=None, total_paid=2.50)

    t2 = _ticket(conn, "Mercadona", "A2", "2026-02-10T10:00:00Z", 3.20)
    _line(conn, t2, "OFERTA ESPECIAL 1 L", product_id=script, total_paid=3.20)

    t3 = _ticket(conn, "Dia", "B1", "2026-02-15T10:00:00Z", 5.50)
    _line(conn, t3, "PECHUGA POLLO BANDEJA 1 KG", product_id=dia, total_paid=5.50)

    t4 = _ticket(conn, "Mercadona", "A3", "2026-03-02T10:00:00Z", 6.60)
    _line(conn, t4, "PECHUGA POLLO BANDEJA 1 KG", product_id=pechuga, total_paid=6.60)

    return {"pechuga": pechuga, "dia": dia, "script": script}


def _build(conn, tmp_path, name="informe.html"):
    output = tmp_path / name
    summary = report.build_report(conn, output)
    return summary, output.read_text(encoding="utf-8")


def _rows_for_chain(html, chain):
    pattern = r'<tr[^>]*data-chain="%s"[^>]*>(.*?)</tr>' % re.escape(chain)
    return re.findall(pattern, html, re.S)


def test_summary_totals_match_seeded_amounts(conn, seeded, tmp_path):
    summary, html = _build(conn, tmp_path)

    assert summary["tickets"] == 4
    assert summary["lines"] == 5
    assert summary["matched_lines"] == 4
    assert summary["unmatched_lines"] == 1
    assert summary["chains"] == 2
    assert summary["total_spent"] == pytest.approx(23.80)

    assert "23,80 €" in html
    assert "18,30 €" in html  # Mercadona total
    assert "5,50 €" in html  # Dia total


def test_chain_and_category_tables_contain_expected_keys(conn, seeded, tmp_path):
    _, html = _build(conn, tmp_path)

    assert "Mercadona" in html
    assert "Dia" in html
    assert "Carne" in html
    assert "Bebidas" in html
    assert "Sin clasificar" in html


def test_month_table_contains_every_month(conn, seeded, tmp_path):
    _, html = _build(conn, tmp_path)

    assert "enero 2026" in html
    assert "febrero 2026" in html
    assert "marzo 2026" in html


def test_repeated_product_shows_first_last_and_percentage(conn, seeded, tmp_path):
    _, html = _build(conn, tmp_path)

    assert "Pechuga de pollo bandeja 1 kg" in html
    assert "6,00 €" in html
    assert "6,60 €" in html
    assert "+10,00 %" in html


def test_cross_chain_comparison_and_missing_comparable(conn, seeded, tmp_path):
    _, html = _build(conn, tmp_path)

    dia_rows = _rows_for_chain(html, "Dia")
    assert any("Pechuga de pollo bandeja 1 kg" in row for row in dia_rows)
    assert any("0,50 €/kg" in row for row in dia_rows)

    mas_rows = _rows_for_chain(html, "Supermercados MAS")
    assert mas_rows
    assert all("—" in row for row in mas_rows)


def test_database_values_are_html_escaped(conn, seeded, tmp_path):
    _, html = _build(conn, tmp_path)

    assert "<script>alert(1)</script>" not in html
    assert ESCAPED_SCRIPT in html


def test_empty_store_still_writes_a_valid_report(conn, tmp_path):
    summary, html = _build(conn, tmp_path, name="vacio.html")

    assert summary["tickets"] == 0
    assert summary["lines"] == 0
    assert summary["matched_lines"] == 0
    assert summary["chains"] == 0
    assert summary["total_spent"] == 0
    assert "<html" in html
    assert (tmp_path / "vacio.html").stat().st_size > 0


def test_output_is_deterministic(conn, seeded, tmp_path):
    first = tmp_path / "a.html"
    second = tmp_path / "b.html"

    report.build_report(conn, first)
    report.build_report(conn, second)

    assert first.read_bytes() == second.read_bytes()


def test_weighed_line_uses_the_ticket_unit_price(conn, tmp_path):
    mas_product = store.upsert_product(
        conn,
        _product(chain="MAS", external_id="MAS1", name="Melon piel de sapo",
                 price=None, unit_price=None, unit_label=None, category="Fruta"),
    )
    store.upsert_product(
        conn,
        _product(chain="Dia", external_id="DIA9", name="Melon piel de sapo",
                 price=1.90, unit_price=1.90, unit_label="kg", category="Fruta"),
    )
    ticket_id = _ticket(conn, "MAS", "M1", "2026-04-01T10:00:00Z", 2.52)
    _line(
        conn, ticket_id, "MELON PIEL DE SAPO", product_id=mas_product,
        total_paid=2.52, price_paid=1.65, purchase_unit="kg",
    )

    _, html = _build(conn, tmp_path)

    assert "1,65 €/kg" in html


def test_a_manual_link_drives_the_comparison(conn, seeded, tmp_path):
    """A hand-made link beats the fuzzy match, exactly like the product page."""
    manual = store.upsert_product(
        conn,
        _product(
            chain="Dia",
            external_id="D-MANUAL",
            name="Pechuga de pollo al horno Dia",
            price=7.00,
            unit_price=7.00,
        ),
    )
    store.link_products(conn, seeded["pechuga"], manual)

    _, html = _build(conn, tmp_path)

    dia_rows = _rows_for_chain(html, "Dia")
    assert any(
        "Pechuga de pollo al horno Dia" in row and "7,00 €" in row
        for row in dia_rows
    )
    # The fuzzy Dia product (5,50 €) is no longer the comparable for that line.
    assert not any("5,50 €" in row for row in dia_rows)
