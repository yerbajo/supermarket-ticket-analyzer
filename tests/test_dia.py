"""Dia connector tests: offline, against payloads captured from the live shop."""

import json
import pathlib
import re

import pytest

from supermarket import dia
from tickets import store

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FakeFetcher:
    """Stands in for ``DiaBrowser.fetch_json``: answers the tree and the pages."""

    def __init__(self, tree, pages=None):
        self.tree = tree
        self.pages = pages or {}
        self.calls = []

    def __call__(self, path):
        self.calls.append(path)
        if "initial_analytics" in path:
            return self.tree
        page = int(re.search(r"page=(\d+)", path).group(1))
        return self.pages.get(page, {"items": [], "total_items": 0, "pagination": {"page_size": 20}})

    def product_calls(self):
        return [call for call in self.calls if "plp-back" in call]


def test_parse_category_tree_flattens_the_live_tree():
    rows = dia.parse_category_tree(load("dia_tree.json"))

    assert len(rows) == 326
    assert sum(1 for row in rows if row["level"] == 1) == 31
    assert sum(1 for row in rows if row["level"] == 2) == 295

    platanos = next(row for row in rows if row["id"] == "L2033")
    assert platanos == {
        "id": "L2033",
        "name": "platanos y bananas",
        "path": "/frutas/platanos-y-bananas/c/L2033",
        "level": 2,
        "top_id": "L105",
    }
    assert next(row for row in rows if row["id"] == "L105")["top_id"] == "L105"


def test_parse_category_tree_raises_when_the_shape_changed():
    with pytest.raises(dia.DiaError) as excinfo:
        dia.parse_category_tree({"unexpected": {}})
    assert "menu_analytics" in str(excinfo.value)


@pytest.mark.parametrize(
    "measure_unit, expected",
    [("KILO", "kg"), ("kilo", "kg"), ("LITRO", "l"), ("UNIDAD", "ud"), ("UD", "ud"), ("", None), (None, None), ("CAJA", None)],
)
def test_measure_label_maps_known_units_only(measure_unit, expected):
    assert dia.measure_label(measure_unit) == expected


def test_parse_products_maps_the_live_payload():
    payload = load("dia_plp_frutas.json")
    rows = dia.parse_products(payload, "platanos y bananas", observed_at="2026-01-01T00:00:00Z")

    assert len(rows) == 5
    first = rows[0]
    assert first["chain"] == "Dia"
    assert first["external_id"] == "42070"
    assert first["name"] == "Banana granel 900 g aprox."
    assert first["price"] == pytest.approx(1.0)
    assert first["unit_price"] == pytest.approx(1.11)
    assert first["unit_label"] == "kg"
    assert first["format"] == "900 g"
    assert first["url"] == "https://www.dia.es/frutas/platanos-y-bananas/p/42070"
    assert first["image_url"].startswith("https://www.dia.es/product_images/42070/")
    assert first["observed_at"] == "2026-01-01T00:00:00Z"
    assert all(row["unit_label"] == "kg" for row in rows)


def test_parse_products_falls_back_to_the_size_in_the_name():
    payload = {
        "items": [
            {
                "object_id": "1",
                "display_name": "Leche entera PASCUAL brick 1 l",
                "prices": {"price": 1.41},
            },
            {
                "object_id": "2",
                "display_name": "Sin formato reconocible",
                "prices": {"price": 2.0},
            },
            {"object_id": None, "display_name": "Sin id", "prices": {"price": 3.0}},
        ]
    }
    rows = dia.parse_products(payload, "leche")

    assert [row["external_id"] for row in rows] == ["1", "2"]
    assert rows[0]["unit_price"] == pytest.approx(1.41)
    assert rows[0]["unit_label"] == "l"
    # No unit anywhere: it stays unknown instead of being invented.
    assert rows[1]["unit_price"] is None
    assert rows[1]["unit_label"] is None


def test_parse_products_raises_when_items_is_missing():
    with pytest.raises(dia.DiaError) as excinfo:
        dia.parse_products({"plp_items": []}, "leche")
    assert "items" in str(excinfo.value)


def test_ingest_dedupes_repeated_category_ids(conn):
    tree = {
        "menu_analytics": {
            "L101": {
                "parameter": "L1_quesos",
                "path": "/quesos/c/L101",
                "children": {
                    "L101": {"parameter": "L2_todo quesos", "path": "/quesos/c/L101", "children": {}},
                    "L2007": {"parameter": "L2_curado", "path": "/quesos/curado/c/L2007", "children": {}},
                },
            }
        }
    }
    pages = {1: {"items": [{"object_id": "1", "display_name": "Queso 1 kg", "prices": {"price": 5.0, "price_per_unit": 5.0, "measure_unit": "KILO"}}], "total_items": 1, "pagination": {"page_size": 20}}}
    fetcher = FakeFetcher(tree, pages)

    counts = dia.ingest_dia(conn, fetcher, sleep_seconds=0)

    assert counts["categories"] == 2  # L101 (todo quesos) is the same listing as L101
    assert len(fetcher.product_calls()) == 2
    assert all("category_id=L101" in call or "category_id=L2007" in call for call in fetcher.product_calls())


def test_ingest_pages_and_is_idempotent(conn):
    tree = {"menu_analytics": {"L105": {"parameter": "L1_frutas", "path": "/frutas/c/L105", "children": {}}}}
    item = {"object_id": "7", "display_name": "Manzana 1 kg", "prices": {"price": 2.0, "price_per_unit": 2.0, "measure_unit": "KILO"}}
    pages = {
        page: {"items": [item], "total_items": 45, "pagination": {"page_size": 20}}
        for page in (1, 2, 3)
    }
    fetcher = FakeFetcher(tree, pages)

    counts = dia.ingest_dia(conn, fetcher, sleep_seconds=0)

    assert counts["pages"] == 3  # ceil(45 / 20)
    assert len(fetcher.product_calls()) == 3
    assert fetcher.product_calls()[0].endswith("/l1/all/L105/reduced?category_id=L105&page=1")
    assert counts["products"] == 3  # the same product three times
    assert len(store.list_products(conn, chain="Dia")) == 1

    again = dia.ingest_dia(conn, FakeFetcher(tree, pages), sleep_seconds=0)
    assert again["products"] == 3
    assert len(store.list_products(conn, chain="Dia")) == 1


def test_browser_fetch_requires_an_open_browser():
    with pytest.raises(dia.DiaError) as excinfo:
        dia.DiaBrowser().fetch_json("/api/v1/whatever")
    assert "context manager" in str(excinfo.value)


def test_browser_fetch_reports_status_and_bad_json():
    class FakePage:
        def __init__(self, status, body):
            self.status, self.body = status, body

        def evaluate(self, script, path):
            return {"status": self.status, "body": self.body}

    browser = dia.DiaBrowser()
    browser._page = FakePage(403, "")
    with pytest.raises(dia.DiaError) as excinfo:
        browser.fetch_json("/api/v1/plp-back")
    assert "HTTP 403" in str(excinfo.value)

    browser._page = FakePage(200, "<html>not json</html>")
    with pytest.raises(dia.DiaError) as excinfo:
        browser.fetch_json("/api/v1/plp-back")
    assert "not JSON" in str(excinfo.value)


def test_launch_options_use_the_installed_browser_when_asked(monkeypatch):
    """The container has no Playwright bundle: it points CHROMIUM_PATH at Chromium."""
    monkeypatch.setenv(dia.CHROMIUM_PATH_ENV, "/usr/bin/chromium")
    monkeypatch.setattr(dia.os, "geteuid", lambda: 1000)

    options = dia.DiaBrowser.launch_options()

    assert options["executable_path"] == "/usr/bin/chromium"
    assert options["headless"] is False
    assert "--no-sandbox" not in options["args"]


def test_launch_options_drop_the_sandbox_only_for_root(monkeypatch):
    """Chromium refuses to sandbox itself as root, which is the container case."""
    monkeypatch.delenv(dia.CHROMIUM_PATH_ENV, raising=False)
    monkeypatch.setattr(dia.os, "geteuid", lambda: 0)
    assert "--no-sandbox" in dia.DiaBrowser.launch_options()["args"]

    monkeypatch.setattr(dia.os, "geteuid", lambda: 1000)
    options = dia.DiaBrowser.launch_options()
    assert "--no-sandbox" not in options["args"]
    assert "executable_path" not in options


def test_a_failing_page_is_retried_and_does_not_kill_the_walk(conn):
    """Hundreds of requests: one 502 must not cost the whole catalogue."""
    tree = {"menu_analytics": {"L101": {"parameter": "L1_quesos", "path": "/quesos/c/L101", "children": {}}}}
    item = {"object_id": "1", "display_name": "Queso 1 kg", "prices": {"price": 5.0, "price_per_unit": 5.0, "measure_unit": "KILO"}}
    intentos = {"n": 0}

    def fetch_json(path):
        if "initial_analytics" in path:
            return tree
        intentos["n"] += 1
        if intentos["n"] == 1:
            raise dia.DiaError("HTTP 502")  # el primer intento de la pagina falla
        return {"items": [item], "total_items": 1, "pagination": {"page_size": 20}}

    counts = dia.ingest_dia(conn, fetch_json, sleep_seconds=0, retries=2)

    assert counts["failed_pages"] == 0
    assert counts["products"] == 1  # el reintento lo salvo


def test_a_page_that_always_fails_is_counted_and_the_walk_carries_on(conn):
    tree = {
        "menu_analytics": {
            "L101": {"parameter": "L1_quesos", "path": "/quesos/c/L101", "children": {}},
            "L102": {"parameter": "L1_carnes", "path": "/carnes/c/L102", "children": {}},
        }
    }
    item = {"object_id": "1", "display_name": "Queso 1 kg", "prices": {"price": 5.0, "price_per_unit": 5.0, "measure_unit": "KILO"}}

    def fetch_json(path):
        if "initial_analytics" in path:
            return tree
        if "L101" in path:
            raise dia.DiaError("HTTP 502")
        return {"items": [item], "total_items": 1, "pagination": {"page_size": 20}}

    counts = dia.ingest_dia(conn, fetch_json, sleep_seconds=0, retries=1)

    assert counts["failed_pages"] == 1        # la que siempre falla
    assert counts["products"] == 1            # la otra cadena si se trajo
    assert counts["categories"] == 2          # y el recorrido no se corto
