"""Mercadona public catalog connector.

Walks the public catalogue on https://tienda.mercadona.es/api/ and writes
every product into the SQLite store (``chain = "Mercadona"``). Plain
``requests`` only: no cookies, no Selenium, no pandas, no Excel.

Observed API shape (2026): ``GET /api/categories/`` returns the 26
level-1 categories, each with a ``categories`` list of level-2 entries.
``GET /api/categories/{level2_id}/`` returns that level-2 entry whose
``categories`` are the leaves, each carrying its ``products`` inline.
Leaf ids are *not* fetchable on their own (``/api/categories/{leaf_id}/``
returns 404), so products are read from the level-2 response.
"""

from __future__ import annotations

import time

import requests

from tickets import store

BASE_URL = "https://tienda.mercadona.es/api"
CATEGORIES_URL = f"{BASE_URL}/categories/"
REQUEST_TIMEOUT = 30.0

# Mercadona's size_format values that map onto our comparable unit labels.
SIZE_FORMAT_LABELS = {"l": "l", "kg": "kg", "ud": "ud"}


class MercadonaError(RuntimeError):
    """Raised when the Mercadona API returns an unexpected response."""


def fetch_json(session, url, timeout=REQUEST_TIMEOUT):
    """GET ``url`` and return JSON, raising with the URL and status on error."""
    response = session.get(url, timeout=timeout)
    if response.status_code != 200:
        raise MercadonaError(f"Mercadona request failed: {url} -> HTTP {response.status_code}")
    return response.json()


def _format_label(product):
    """Human size label, e.g. ``"Garrafa 5 l"``, from packaging + size."""
    instructions = product.get("price_instructions") or {}
    packaging = (product.get("packaging") or "").strip()
    unit_size = instructions.get("unit_size")
    size_format = (instructions.get("size_format") or "").strip()

    parts = []
    if packaging:
        parts.append(packaging)
    if size_format:
        parts.append(f"{unit_size:g} {size_format}" if unit_size is not None else size_format)
    return " ".join(parts) or None


def _product_to_row(product, category_name, observed_at):
    """Map one API product to a store row, or ``None`` when unusable."""
    instructions = product.get("price_instructions") or {}
    external_id = product.get("id")
    name = product.get("display_name")
    raw_price = instructions.get("unit_price")
    if external_id is None or not name or raw_price is None:
        return None
    try:
        price = float(raw_price)
        raw_bulk = instructions.get("bulk_price")
        unit_price = float(raw_bulk) if raw_bulk is not None else None
    except (TypeError, ValueError):
        return None

    return {
        "chain": "Mercadona",
        "external_id": str(external_id),
        "name": name,
        "format": _format_label(product),
        "price": price,
        "unit_price": unit_price,
        "unit_label": SIZE_FORMAT_LABELS.get((instructions.get("size_format") or "").lower()),
        "category": category_name,
        "url": product.get("share_url"),
        "image_url": product.get("thumbnail"),
        "observed_at": observed_at,
    }


def _iter_leaf_products(payload):
    for leaf in payload.get("categories") or []:
        category_name = leaf.get("name")
        for product in leaf.get("products") or []:
            yield product, category_name


def parse_category(payload, observed_at=None):
    """Parse a level-2 category payload into a list of store rows (no network)."""
    observed_at = observed_at or store.utcnow()
    rows = []
    for product, category_name in _iter_leaf_products(payload):
        row = _product_to_row(product, category_name, observed_at)
        if row is not None:
            rows.append(row)
    return rows


def ingest_mercadona(conn, sleep_seconds=0.5, session=None, timeout=REQUEST_TIMEOUT) -> dict:
    """Walk every leaf, upsert its products and return counts.

    Returns ``{"categories": level-1 count, "leaves": leaf count,
    "products": upserted, "skipped": unusable}``. Sleeps ``sleep_seconds``
    between requests to be polite.
    """
    session = session or requests.Session()
    observed_at = store.utcnow()
    counts = {"categories": 0, "leaves": 0, "products": 0, "skipped": 0}

    top = fetch_json(session, CATEGORIES_URL, timeout=timeout)
    level2_ids = []
    for level1 in top.get("results") or []:
        counts["categories"] += 1
        for level2 in level1.get("categories") or []:
            if level2.get("id") is not None:
                level2_ids.append(level2["id"])

    for index, category_id in enumerate(level2_ids):
        if index and sleep_seconds:
            time.sleep(sleep_seconds)
        payload = fetch_json(session, f"{CATEGORIES_URL}{category_id}/", timeout=timeout)
        counts["leaves"] += len(payload.get("categories") or [])
        for product, category_name in _iter_leaf_products(payload):
            row = _product_to_row(product, category_name, observed_at)
            if row is None:
                counts["skipped"] += 1
                continue
            store.upsert_product(conn, row)
            counts["products"] += 1

    return counts
