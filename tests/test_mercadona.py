"""Mercadona parsing tests. No network: the fixture is a captured real response."""

import json
import pathlib

import pytest

from supermarket import mercadona

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "mercadona_category_112.json"


class _FakeResponse:
    def __init__(self, body, status_code=200):
        self._body = body
        self.status_code = status_code

    def json(self):
        return self._body


class _FakeSession:
    """Maps exact URLs to fixture payloads so ingest can run offline."""

    def __init__(self, responses, status_code=200):
        self._responses = responses
        self._status_code = status_code
        self.requested = []

    def get(self, url, timeout=None):
        self.requested.append(url)
        return _FakeResponse(self._responses.get(url), status_code=self._status_code)


def _payload():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_parse_category_extracts_expected_rows():
    rows = mercadona.parse_category(_payload(), observed_at="2026-01-01T00:00:00Z")

    assert len(rows) == 35  # 11 + 8 + 3 + 13 products in the captured category
    by_id = {row["external_id"]: row for row in rows}
    oil = by_id["4241"]

    assert oil["chain"] == "Mercadona"
    assert oil["name"] == "Aceite de oliva 0,4º Hacendado"
    assert oil["format"] == "Garrafa 5 l"
    assert oil["price"] == pytest.approx(17.25)
    assert oil["unit_price"] == pytest.approx(3.45)
    assert oil["unit_label"] == "l"
    assert oil["category"] == "Aceite de oliva"
    assert oil["url"].startswith("https://tienda.mercadona.es/product/4241/")
    assert oil["observed_at"] == "2026-01-01T00:00:00Z"


def test_parse_category_skips_incomplete_products():
    payload = {
        "categories": [
            {
                "name": "X",
                "products": [
                    {"id": "1", "display_name": "ok", "price_instructions": {"unit_price": "1.0", "bulk_price": "1.0", "size_format": "ud"}},
                    {"display_name": "no id", "price_instructions": {"unit_price": "1.0"}},
                    {"id": "3", "display_name": "no price", "price_instructions": {}},
                ],
            }
        ]
    }
    rows = mercadona.parse_category(payload, observed_at="t")
    assert [row["external_id"] for row in rows] == ["1"]


def test_ingest_mercadona_walks_leaves_and_writes_products(conn):
    from tickets import store

    top = {
        "results": [
            {"id": 12, "name": "Aceite, especias y salsas", "categories": [{"id": 112, "name": "Aceite, vinagre y sal"}]}
        ]
    }
    session = _FakeSession(
        {
            mercadona.CATEGORIES_URL: top,
            f"{mercadona.CATEGORIES_URL}112/": _payload(),
        }
    )

    counts = mercadona.ingest_mercadona(conn, sleep_seconds=0, session=session)

    assert counts == {"categories": 1, "leaves": 4, "products": 35, "skipped": 0}
    assert len(store.list_products(conn, chain="Mercadona")) == 35
    assert store.find_product(conn, "Mercadona", "4241")["unit_price"] == pytest.approx(3.45)


def test_fetch_json_error_names_url_and_status():
    session = _FakeSession({}, status_code=503)
    with pytest.raises(mercadona.MercadonaError) as excinfo:
        mercadona.fetch_json(session, "https://tienda.mercadona.es/api/categories/")
    message = str(excinfo.value)
    assert "https://tienda.mercadona.es/api/categories/" in message
    assert "503" in message
