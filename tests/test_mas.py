"""Supermercados MAS connector: offline fixture tests plus one live smoke test."""

import json
import pathlib

import pytest
import requests

from supermarket import mas
from tickets import store

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload


class FakeSession:
    """Answers the tree query with one payload and every product page with another."""

    def __init__(self, tree, products, status=200):
        self.tree = tree
        self.products = products
        self.status = status
        self.calls = []

    def get(self, url, params=None, timeout=None):
        query = (params or {}).get("query", "")
        self.calls.append(query)
        if "products(" in query:
            return FakeResponse(self.products, self.status)
        return FakeResponse(self.tree, self.status)


def test_parse_category_tree_flattens_every_level():
    rows = mas.parse_category_tree(load("mas_categories_tree.json")["data"])
    levels = {}
    for row in rows:
        levels[row["level"]] = levels.get(row["level"], 0) + 1

    # The real tree: one root entry ("Default Category") skipped, 202 departments
    # and their 221 subcategories.
    assert levels == {1: 202, 2: 221}
    assert len(rows) == 423
    names = {row["name"] for row in rows if row["level"] == 1}
    for department in ("Alimentación", "Lácteos y sustitutivos", "Carnes y aves", "Frutas", "Pescados y mariscos"):
        assert department in names


def test_parse_category_tree_skips_the_store_root():
    rows = mas.parse_category_tree(load("mas_categories_root.json")["data"])

    assert [row["name"] for row in rows if row["level"] == 1] == [
        "Alimentación",
        "Lácteos y sustitutivos",
        "Carnes y aves",
        "Sherry month",
    ]
    assert all(row["name"] != "Default Category" for row in rows)
    assert [row["name"] for row in rows if row["level"] == 2] == ["Arroz"]


def test_parse_products_maps_real_rows_with_unit_price():
    payload = load("mas_products_leaf.json")
    rows = mas.parse_products(
        payload["data"], "Fresas, cerezas y frutos del bosque", observed_at="2026-01-01T00:00:00Z"
    )

    assert len(rows) == 6
    first = rows[0]
    assert first["chain"] == "MAS"
    assert first["external_id"] == "349380"
    assert first["name"] == "Arandanos cristina blue dulce vasito 225 gr"
    assert first["price"] == pytest.approx(3.45)
    assert first["unit_label"] == "kg"
    assert first["unit_price"] == pytest.approx(3.45 / 0.225, rel=1e-4)
    assert first["url"] == (
        "https://www.supermercadosmas.com/frutas/arandanos-cristina-blue-dulce-vasito-225-gr/"
    )
    assert first["category"] == "Fresas, cerezas y frutos del bosque"
    assert first["observed_at"] == "2026-01-01T00:00:00Z"


def test_parse_products_skips_incomplete_products():
    payload = {
        "products": {
            "total_count": 3,
            "items": [
                {"sku": "1", "name": "Leche entera 1 l", "price_range": {"minimum_price": {"final_price": {"value": 1.0}}}},
                {"sku": None, "name": "Sin sku", "price_range": {"minimum_price": {"final_price": {"value": 1.0}}}},
                {"sku": "3", "name": "Sin precio", "price_range": {"minimum_price": {}}},
            ],
        }
    }
    rows = mas.parse_products(payload, "Lacteos")
    assert [row["external_id"] for row in rows] == ["1"]
    assert rows[0]["unit_price"] == pytest.approx(1.0)
    assert rows[0]["unit_label"] == "l"


def test_ingest_walks_the_departments_and_is_idempotent(conn):
    tree = load("mas_categories_tree.json")
    products = load("mas_products_leaf.json")
    payload_for_tree_query = mas.graphql(FakeSession(tree, products), mas.CATEGORY_TREE_QUERY)
    assert payload_for_tree_query  # the fake session is wired correctly

    counts = mas.ingest_mas(conn, sleep_seconds=0, session=FakeSession(tree, products))

    # One tree request plus one page per department: the 202 departments are
    # anchors, so their subtrees come along without extra requests.
    assert counts["categories"] == 202
    assert counts["requests"] == 203
    assert counts["stopped"] is False
    assert counts["products"] == 202 * 6  # every department returns the same six in the fake

    stored = store.list_products(conn, chain="MAS")
    assert len(stored) == 6  # upsert keyed on (chain, external_id)

    again = mas.ingest_mas(conn, sleep_seconds=0, session=FakeSession(tree, products))
    assert again["products"] == 202 * 6
    assert len(store.list_products(conn, chain="MAS")) == 6


def test_ingest_stops_at_the_request_budget(conn):
    tree = load("mas_categories_tree.json")
    products = load("mas_products_leaf.json")
    counts = mas.ingest_mas(
        conn, sleep_seconds=0, session=FakeSession(tree, products), max_requests=3
    )
    assert counts["stopped"] is True
    assert counts["requests"] == 3


def test_graphql_error_names_the_url_and_status():
    session = FakeSession({}, {}, status=503)
    with pytest.raises(mas.MasError) as excinfo:
        mas.graphql(session, mas.CATEGORY_TREE_QUERY)
    assert mas.GRAPHQL_URL in str(excinfo.value)
    assert "503" in str(excinfo.value)


def test_graphql_error_surfaces_payload_errors():
    class ErrorSession:
        def get(self, url, params=None, timeout=None):
            return FakeResponse({"errors": [{"message": "Internal server error"}]})

    with pytest.raises(mas.MasError) as excinfo:
        mas.graphql(ErrorSession(), "{ storeConfig { store_name } }")
    assert "Internal server error" in str(excinfo.value)


@pytest.mark.network
def test_live_store_config_answers():
    data = mas.graphql(requests.Session(), "{ storeConfig { store_name } }")
    assert data["storeConfig"]["store_name"]
