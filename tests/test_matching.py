"""Matching-engine tests: normalisation, aliases, fuzzy scoring and the size guard.

All offline: the catalogue is seeded directly with ``store.upsert_product``.
"""

import pytest

from tickets import matching, store


def _product(**overrides):
    row = {
        "chain": "Mercadona",
        "external_id": "4241",
        "name": "Leche entera PASCUAL botella 1,5l",
        "format": None,
        "price": 1.29,
        "unit_price": 0.86,
        "unit_label": "l",
        "category": "Leche",
        "url": None,
        "image_url": None,
        "observed_at": "2026-01-01T00:00:00Z",
    }
    row.update(overrides)
    return row


def _insert_ticket(conn, chain="Mercadona", ticket_number="A1"):
    cursor = conn.execute(
        "INSERT INTO tickets (chain, ticket_number, purchased_at, total, source_pdf, imported_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (chain, ticket_number, "2026-01-02T10:00:00Z", 10.0, None, "2026-01-02T10:00:00Z"),
    )
    conn.commit()
    return cursor.lastrowid


def _insert_line(conn, ticket_id, raw_text):
    cursor = conn.execute(
        "INSERT INTO ticket_lines (ticket_id, raw_text) VALUES (?, ?)",
        (ticket_id, raw_text),
    )
    conn.commit()
    return cursor.lastrowid


def test_normalize_text_is_case_accent_and_format_insensitive():
    # Case folding, accent stripping, punctuation -> space, whitespace collapse.
    assert matching.normalize_text("Café con  LECHE") == matching.normalize_text("cafe con leche")
    assert matching.normalize_text("Leche (entera) 1,5L") == matching.normalize_text(
        "LECHE ENTERA 1 5L"
    )
    assert matching.normalize_text("") == ""
    assert matching.normalize_text(None) == ""


def test_alias_beats_fuzzy_and_wins_outright(conn):
    fuzzy_best = store.upsert_product(conn, _product())
    aliased = store.upsert_product(
        conn, _product(external_id="9999", name="Bebida de soja ALPRO 1 l")
    )
    raw = "LECHE ENTERA PASCUAL BOT 1,5L"
    matching.confirm_alias(conn, "Mercadona", raw, aliased, confirmed_by="tester")

    result = matching.match_line(conn, "Mercadona", raw)

    assert result["matched_by"] == "alias"
    assert result["score"] == 1.0
    assert result["product_id"] == aliased
    assert result["product_id"] != fuzzy_best


def test_case_accent_and_formatting_insensitivity(conn):
    product_id = store.upsert_product(conn, _product())

    result = matching.match_line(conn, "Mercadona", "LECHE ENTERA PASCUAL BOT 1,5L")

    assert result is not None
    assert result["product_id"] == product_id
    assert result["matched_by"] == "fuzzy"
    assert result["name"] == "Leche entera PASCUAL botella 1,5l"
    assert result["unit_label"] == "l"
    assert result["unit_price"] == pytest.approx(0.86)


def test_conflicting_sizes_are_rejected(conn):
    store.upsert_product(conn, _product())

    # 1 l vs 1,5 l: both expose a size and they are materially different.
    assert matching.match_line(conn, "Mercadona", "LECHE ENTERA PASCUAL 1L") is None


def test_no_plausible_candidate_returns_none(conn):
    store.upsert_product(conn, _product(name="Detergente Ariel 30 lavados"))

    assert matching.match_line(conn, "Mercadona", "LECHE ENTERA PASCUAL BOT 1,5L") is None


def test_match_line_never_crosses_chains(conn):
    store.upsert_product(conn, _product(chain="Dia", name="Leche entera PASCUAL botella 1,5l"))

    assert matching.match_line(conn, "Mercadona", "LECHE ENTERA PASCUAL BOT 1,5L") is None


def test_match_ticket_lines_writes_and_is_idempotent(conn):
    product_id = store.upsert_product(conn, _product())
    ticket_id = _insert_ticket(conn)
    matched_line = _insert_line(conn, ticket_id, "LECHE ENTERA PASCUAL BOT 1,5L")
    unmatched_line = _insert_line(conn, ticket_id, "DETERGENTE ARIEL 30 LAVADOS")

    first = matching.match_ticket_lines(conn, "Mercadona")
    assert first == {"lines": 2, "matched": 1, "unmatched": 1, "from_alias": 0}

    row = conn.execute(
        "SELECT product_id, match_confidence, matched_by FROM ticket_lines WHERE id = ?",
        (matched_line,),
    ).fetchone()
    assert row["product_id"] == product_id
    assert row["match_confidence"] is not None
    assert row["matched_by"] == "fuzzy"

    untouched = conn.execute(
        "SELECT product_id FROM ticket_lines WHERE id = ?", (unmatched_line,)
    ).fetchone()
    assert untouched["product_id"] is None

    # Second run only sees the still-unresolved line and changes nothing.
    second = matching.match_ticket_lines(conn, "Mercadona")
    assert second == {"lines": 1, "matched": 0, "unmatched": 1, "from_alias": 0}

    again = conn.execute(
        "SELECT product_id, match_confidence, matched_by FROM ticket_lines WHERE id = ?",
        (matched_line,),
    ).fetchone()
    assert (again["product_id"], again["match_confidence"], again["matched_by"]) == (
        row["product_id"],
        row["match_confidence"],
        row["matched_by"],
    )


def test_confirm_alias_makes_next_match_instant(conn):
    product_id = store.upsert_product(conn, _product())
    raw = "LECHE PASCUAL BOTELLA"

    alias_id = matching.confirm_alias(conn, "Mercadona", raw, product_id)
    assert alias_id == store.resolve_alias(conn, "Mercadona", raw)

    result = matching.match_line(conn, "Mercadona", raw)
    assert result["matched_by"] == "alias"
    assert result["score"] == 1.0
    assert result["product_id"] == product_id


def test_a_line_with_the_shop_code_is_never_guessed_by_name(conn):
    """Dia hands the exact code over: if it did not resolve, the product is not ours.

    Measured on a real ticket: the 16-pack ``YOGUR SABOR DIA P-16`` was matched by name
    to the catalogue's ``Yogur sabor plátano Dia Láctea 4 x 125 g``, a different pack.
    """
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
    store.upsert_ticket(
        conn,
        {
            "chain": "Dia",
            "ticket_number": "T-16",
            "purchased_at": "2026-10-12T11:22:15",
            "total": 5.18,
            "lines": [
                {"raw_text": "YOGUR SABOR DIA P-16", "qty": 2, "price_paid": 2.59,
                 "total_paid": 5.18, "purchase_unit": "ud", "shop_code": "275250"}
            ],
        },
    )

    counts = matching.match_ticket_lines(conn, "Dia")

    assert counts["lines"] == 0  # the line is left alone, not guessed at
    assert conn.execute("SELECT product_id FROM ticket_lines").fetchone()["product_id"] is None
