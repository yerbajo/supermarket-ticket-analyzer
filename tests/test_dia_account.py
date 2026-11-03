"""Dia account tests: offline, against payloads with the shape the API returned."""

import pytest

from supermarket import dia_account
from tickets import store

# Shape taken from a real response (2026-10-12); the products and amounts are
# invented so the repository carries no purchase of the owner's.
LIST_PAYLOAD = {
    "total_tickets": 3,
    "pagination": {"offset": 50, "page_size": 50},
    "tickets": [
        {
            "detail_params": {"begin": 1791546220000, "business": 1, "country": "ES", "pos": 2, "store": 2344, "ticket": 295869},
            "store_info": {"address": "AV RAMON Y CAJAL 12", "city": "Sevilla", "postal_code": "41005"},
            "submitted_date": "2026-10-09T11:43:40Z",
            "total_amount": 12.34,
        },
        {
            "detail_params": {"begin": 1791546220000, "business": 1, "country": "ES", "pos": 2, "store": 2344, "ticket": 295870},
            "submitted_date": "2026-10-08T09:00:00Z",
            "total_amount": 5.0,
        },
    ],
}

DETAIL_PAYLOAD = {
    "header": {
        "code": 295869,
        "display_code": "0234402-00295869",
        "store_code": 2344,
        "store_address": "AV RAMON Y CAJAL 12",
        "submitted_date": "2026-10-09T11:43:40Z",
    },
    "amount": {"total_amount": 12.34, "total_discounts": 0},
    "corporate": {
        "items": [
            {"code": 1, "name": "PAN DE MOLDE", "quantity": 2, "price_per_unit": 1.79, "amount": 3.58, "variable_weight": False, "weight": 0, "tax": "A", "cancel": False},
            {"code": 2, "name": "PECHUGA ENTERA", "quantity": 1, "price_per_unit": 6.49, "amount": 7.46, "variable_weight": True, "weight": 1.149, "tax": "B", "cancel": False},
            {"code": 3, "name": "LINEA ANULADA", "quantity": 1, "price_per_unit": 1.3, "amount": 1.3, "variable_weight": False, "weight": 0, "tax": "A", "cancel": True},
        ]
    },
}


def _fetch(payloads, seen=None):
    def fetch_json(url):
        if seen is not None:
            seen.append(url)
        for fragmento, payload in payloads.items():
            if fragmento in url:
                return payload
        raise dia_account.DiaAccountError(f"sin datos para {url}")

    return fetch_json


# --------------------------------------------------------------------------- #
# Timestamps: the API speaks UTC, the ticket prints local time
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "entrada, esperado",
    [
        ("2026-10-09T11:43:40Z", "2026-10-09T13:43:00"),  # summer time: +2
        ("2026-01-15T10:00:00Z", "2026-01-15T11:00:00"),  # winter time: +1
        (None, None),
        ("", None),
        ("no es una fecha", None),
    ],
)
def test_local_timestamp_truncates_to_the_minute_in_shop_time(entrada, esperado):
    assert dia_account.local_timestamp(entrada) == esperado


# --------------------------------------------------------------------------- #
# Listing
# --------------------------------------------------------------------------- #


def test_list_tickets_walks_the_offsets():
    siguiente = dict(LIST_PAYLOAD, total_tickets=3, tickets=[LIST_PAYLOAD["tickets"][1]])
    vistos = []
    fetch = _fetch({"offset=0": LIST_PAYLOAD, "offset=2": siguiente}, seen=vistos)

    tickets = dia_account.list_tickets(fetch)

    assert len(tickets) == 3
    assert "offset=0" in vistos[0] and "page_size=50" in vistos[0]
    assert "offset=2" in vistos[1]


def test_list_tickets_without_a_session_names_the_fix():
    fetch = _fetch({"": {"total_tickets": 0, "tickets": []}})

    with pytest.raises(dia_account.DiaAccountError) as excinfo:
        dia_account.list_tickets(fetch)

    assert "sesión" in str(excinfo.value) or "caducado" in str(excinfo.value)


def test_list_tickets_reports_a_changed_shape():
    fetch = _fetch({"": {"total_tickets": 3, "resultados": []}})

    with pytest.raises(dia_account.DiaAccountError) as excinfo:
        dia_account.list_tickets(fetch)

    assert "tickets" in str(excinfo.value)


def test_detail_url_moves_the_ticket_into_the_path():
    url = dia_account.detail_url(LIST_PAYLOAD["tickets"][0])

    assert url.startswith(f"{dia_account.API_ROOT}/295869?")
    assert "business=1" in url and "store=2344" in url
    assert "ticket=" not in url  # it belongs in the path, not the query


def test_detail_url_without_a_ticket_id():
    with pytest.raises(dia_account.DiaAccountError):
        dia_account.detail_url({"detail_params": {"business": 1}})


# --------------------------------------------------------------------------- #
# Parsing one ticket
# --------------------------------------------------------------------------- #


def test_parse_ticket_maps_the_lines_and_the_header():
    ticket = dia_account.parse_ticket(DETAIL_PAYLOAD)

    assert ticket["chain"] == "Dia"
    assert ticket["ticket_number"] == "0234402-00295869"
    assert ticket["purchased_at"] == "2026-10-09T13:43:00"
    assert ticket["total"] == pytest.approx(12.34)

    assert len(ticket["lines"]) == 2  # the cancelled line is not a purchase
    pan, pechuga = ticket["lines"]

    assert pan["raw_text"] == "PAN DE MOLDE"
    assert pan["qty"] == pytest.approx(2)
    assert pan["price_paid"] == pytest.approx(1.79)
    assert pan["total_paid"] == pytest.approx(3.58)
    assert pan["purchase_unit"] == "ud"

    # A weighed line carries the weight as quantity and the €/kg as price.
    assert pechuga["qty"] == pytest.approx(1.149)
    assert pechuga["price_paid"] == pytest.approx(6.49)
    assert pechuga["total_paid"] == pytest.approx(7.46)
    assert pechuga["purchase_unit"] == "kg"

    assert ticket["lines_total"] == pytest.approx(11.04)


def test_parse_ticket_reports_a_changed_shape():
    with pytest.raises(dia_account.DiaAccountError) as excinfo:
        dia_account.parse_ticket({"header": {}, "amount": {}, "lines": []})

    assert "corporate.items" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# Importing the whole history
# --------------------------------------------------------------------------- #


def test_import_tickets_imports_and_then_reports_already(conn):
    segunda_pagina = {"total_tickets": 3, "tickets": [LIST_PAYLOAD["tickets"][1]]}
    fetch = _fetch(
        {
            "offset=2": segunda_pagina,
            "/295869?": DETAIL_PAYLOAD,
            "/295870?": dict(
                DETAIL_PAYLOAD,
                header=dict(DETAIL_PAYLOAD["header"], code=295870, display_code="0234402-00295870"),
            ),
            "page_size": LIST_PAYLOAD,
        }
    )

    counts = dia_account.import_tickets(conn, fetch)

    assert counts["found"] == 3
    assert counts["imported"] >= 1
    assert counts["lines"] >= 2
    assert len(store.list_products(conn)) == 0  # no catalogue in this test
    tickets = conn.execute("SELECT chain, ticket_number FROM tickets").fetchall()
    assert any(row["chain"] == "Dia" for row in tickets)

    second = dia_account.import_tickets(conn, fetch)
    assert second["imported"] == 0  # nothing is stored twice


def test_import_tickets_counts_a_broken_ticket_and_continues(conn):
    def fetch_json(url):
        if "page_size" in url:
            return LIST_PAYLOAD
        if "/295869" in url:
            return DETAIL_PAYLOAD
        raise dia_account.DiaAccountError("ticket roto")

    counts = dia_account.import_tickets(conn, fetch_json)

    assert counts["failed"] >= 1
    assert counts["imported"] >= 1


def test_import_tickets_honours_a_limit(conn):
    fetch = _fetch({"/295869?": DETAIL_PAYLOAD, "page_size": LIST_PAYLOAD})

    counts = dia_account.import_tickets(conn, fetch, limit=1)

    assert counts["found"] == 1
    assert counts["imported"] == 1


def test_parse_ticket_keeps_the_shop_product_code():
    """The code is what lets a Dia line be matched exactly, not fuzzily."""
    ticket = dia_account.parse_ticket(DETAIL_PAYLOAD)

    assert [line["shop_code"] for line in ticket["lines"]] == ["1", "2"]


def test_import_resolves_lines_by_code_and_leaves_the_rest(conn):
    """A code present in the catalogue matches with confidence 1; the rest stay open."""
    product_id = store.upsert_product(
        conn,
        {
            "chain": "Dia",
            "external_id": "1",  # matches PAN DE MOLDE in the fixture
            "name": "Pan de molde Dia 460 g",
            "price": 1.79,
            "unit_price": 3.89,
            "unit_label": "kg",
            "observed_at": "2026-10-12T00:00:00Z",
        },
    )
    fetch = _fetch({"page_size": LIST_PAYLOAD, "/295869?": DETAIL_PAYLOAD})

    counts = dia_account.import_tickets(conn, fetch, limit=1)

    assert counts["by_code"] == 1
    filas = {
        row["raw_text"]: row
        for row in conn.execute("SELECT raw_text, product_id, matched_by FROM ticket_lines")
    }
    assert filas["PAN DE MOLDE"]["product_id"] == product_id
    assert filas["PAN DE MOLDE"]["matched_by"] == "code"
    assert filas["PECHUGA ENTERA"]["product_id"] is None


def test_reimporting_only_fills_unknown_matches(conn):
    """A second run improves matches without touching the purchase data."""
    store.upsert_product(
        conn,
        {
            "chain": "Dia",
            "external_id": "1",
            "name": "Pan de molde Dia 460 g",
            "price": 1.79,
            "unit_price": 3.89,
            "unit_label": "kg",
            "observed_at": "2026-10-12T00:00:00Z",
        },
    )
    fetch = _fetch({"page_size": LIST_PAYLOAD, "/295869?": DETAIL_PAYLOAD})
    dia_account.import_tickets(conn, fetch, limit=1)
    antes = conn.execute(
        "SELECT qty, price_paid, total_paid FROM ticket_lines WHERE raw_text = 'PAN DE MOLDE'"
    ).fetchone()

    dia_account.import_tickets(conn, fetch, limit=1)
    despues = conn.execute(
        "SELECT qty, price_paid, total_paid FROM ticket_lines WHERE raw_text = 'PAN DE MOLDE'"
    ).fetchone()

    assert tuple(antes) == tuple(despues)
    assert conn.execute("SELECT COUNT(*) FROM ticket_lines").fetchone()[0] == 2


def test_a_dead_session_is_explained_not_dumped():
    """A stale Dia session loops on every url; that must read as a session problem."""
    from playwright.sync_api import Error as PlaywrightError

    error = PlaywrightError(
        "Page.goto: net::ERR_TOO_MANY_REDIRECTS at https://www.dia.es/my-account/tickets"
    )

    mensaje = dia_account.session_error_message(error)

    assert mensaje and "sesión" in mensaje and "Firefox" in mensaje


def test_a_timeout_is_explained_as_a_timeout():
    mensaje = dia_account.session_error_message("Page.goto: Timeout 60000ms exceeded")

    assert mensaje and "tiempo" in mensaje


def test_other_navigation_errors_are_not_dressed_up_as_sessions():
    assert dia_account.session_error_message("net::ERR_NAME_NOT_RESOLVED") is None


def test_a_login_page_instead_of_json_reads_as_a_dead_session():
    """With an expired session the API returns the site's HTML, not a parse error."""

    class PaginaFalsa:
        def evaluate(self, script, path):
            return {"status": 200, "body": '<!DOCTYPE html><html lang="es">Acceso</html>'}

    sesion = dia_account.DiaSession(PaginaFalsa())

    with pytest.raises(dia_account.DiaAccountError) as excinfo:
        sesion.fetch_json("/api/v3/eservice-back/customer/current/tickets")

    mensaje = str(excinfo.value)
    assert "sesión" in mensaje and "Firefox" in mensaje
