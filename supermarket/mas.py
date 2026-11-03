"""Supermercados MAS catalogue connector.

The online shop behind https://www.supermercadosmas.com runs Magento 2 with the
Hyva theme, behind Cloudflare. Observed live shape (2026):

* Magento GraphQL answers **only as GET**, with the query in the URL query
  string (``/graphql?query={...}``). The same query sent as a POST with a JSON
  body returns HTTP 500 ``Syntax Error: Unexpected <EOF>``.
* One request returns the whole category tree: ``{ categories { items { … children { … } } } }``
  with no filter. The single root entry is "Default Category" and its children are
  the **202 departments** of the shop. Do **not** filter by ``parent_id``: with
  ``parent_id: "2"`` the same store answers with only 20 of those departments
  (the seasonal ones), and filtering by the ``parent_id`` of an inner category
  returns nothing at all. That mistake silently left the whole fresh-food side of
  the catalogue out.
* Department categories are **anchors**: ``products(filter: {category_uid:
  {eq: "Alimentación"}})` already includes every descendant (2.994 products while
  its child ``Arroz`` has 57). The walk therefore stays at level 1, which costs
  ~200 requests instead of thousands.
* ``products(filter: {category_uid: {eq: "<uid>"}}, pageSize: N, currentPage: P)``
  walks a category and ``total_count`` drives the paging.
* Magento's ``ProductInterface`` exposes no weight or measure field, so the unit
  price is derived from the size already written in the product name through
  ``tickets.units.normalize_unit``; it is never guessed.
* Product URLs are ``/<url_key>/`` with a trailing slash and no ``.html``.
* robots.txt disallows ``/catalogsearch/`` and ``/catalog/product/view/``; this
  connector uses neither.
"""

from __future__ import annotations

import time

import requests

from tickets import store
from tickets.units import normalize_unit

CHAIN = "MAS"
GRAPHQL_URL = "https://www.supermercadosmas.com/graphql"
SITE_ROOT = "https://www.supermercadosmas.com"
ROOT_PARENT_ID = "2"  # Magento's "Default Category" (informational only)
PAGE_SIZE = 100
REQUEST_TIMEOUT = 20.0


class MasError(RuntimeError):
    """Raised when the MAS GraphQL endpoint does not answer with usable data."""


CATEGORY_TREE_QUERY = (
    "{ categories { items { uid name url_path"
    " children { uid name url_path"
    " children { uid name url_path } } } } }"
)

PRODUCTS_QUERY = (
    '{ products(filter: {category_uid: {eq: "%s"}}, pageSize: %d, currentPage: %d)'
    " { total_count items { sku name url_key thumbnail { url }"
    " price_range { minimum_price { final_price { value } regular_price { value } } } } } }"
)

#: MAS answers every product's ``thumbnail`` with this generic placeholder
#: (verified 30/30 across five departments, 2026-10-12), so it is treated as
#: "no image" instead of being stored as a picture of nothing.
PLACEHOLDER_MARKER = "placeholder"


def graphql(session, query, timeout=REQUEST_TIMEOUT):
    """Run a GraphQL query over GET and return its ``data`` payload."""
    response = session.get(GRAPHQL_URL, params={"query": query}, timeout=timeout)
    if response.status_code != 200:
        raise MasError(f"MAS GraphQL request failed: {GRAPHQL_URL} -> HTTP {response.status_code}")
    payload = response.json()
    errors = payload.get("errors") or []
    if errors:
        raise MasError(f"MAS GraphQL error: {errors[0].get('message')}")
    return payload.get("data") or {}


def parse_category_tree(payload):
    """Flatten the department tree into ``uid/name/url_path/level`` rows.

    The store's root ("Default Category") is skipped, so the departments come out
    at ``level`` 1 and their subcategories at 2 and 3.
    """
    rows = []

    def walk(nodes, level):
        for node in nodes or []:
            rows.append(
                {
                    "uid": node.get("uid"),
                    "name": node.get("name"),
                    "url_path": node.get("url_path"),
                    "level": level,
                }
            )
            walk(node.get("children"), level + 1)

    roots = (payload.get("categories") or {}).get("items") or []
    for root in roots:
        walk(root.get("children"), 1)
    return rows


def fetch_category_tree(session, timeout=REQUEST_TIMEOUT):
    """Return every category of the shop in one request."""
    return parse_category_tree(graphql(session, CATEGORY_TREE_QUERY, timeout=timeout))


def parse_products(payload, category_name, observed_at=None):
    """Map a products payload to store rows, dropping anything unusable."""
    observed_at = observed_at or store.utcnow()
    rows = []
    for product in (payload.get("products") or {}).get("items") or []:
        sku = product.get("sku")
        name = product.get("name")
        final_price = (
            ((product.get("price_range") or {}).get("minimum_price") or {}).get("final_price") or {}
        )
        price = final_price.get("value")
        url_key = product.get("url_key")
        if not sku or not name or price is None:
            continue
        thumbnail = (product.get("thumbnail") or {}).get("url")
        if thumbnail and PLACEHOLDER_MARKER in thumbnail:
            thumbnail = None  # a generic placeholder is not this product's picture
        unit_price, unit_label = normalize_unit(price, name)
        rows.append(
            {
                "chain": CHAIN,
                "external_id": str(sku),
                "name": name,
                "format": None,  # the size travels inside the name on this shop
                "price": float(price),
                "unit_price": unit_price,
                "unit_label": unit_label,
                "category": category_name,
                "url": f"{SITE_ROOT}/{url_key}/" if url_key else None,
                "image_url": thumbnail,
                "observed_at": observed_at,
            }
        )
    return rows


def _items(payload):
    return (payload.get("products") or {}).get("items") or []


def ingest_mas(
    conn,
    sleep_seconds=0.5,
    session=None,
    max_requests=400,
    timeout=REQUEST_TIMEOUT,
):
    """Walk every category, upsert its products and return counts.

    Returns ``{"categories", "products", "skipped", "requests", "stopped"}``.
    ``stopped`` is True when ``max_requests`` cut the walk short, so a partial
    run is never mistaken for a complete one.
    """
    session = session or requests.Session()
    observed_at = store.utcnow()
    counts = {"categories": 0, "products": 0, "skipped": 0, "requests": 0, "stopped": False}

    categories = fetch_category_tree(session, timeout=timeout)
    # Only the departments: they are anchor categories, so each one already
    # brings its whole subtree. Walking every subcategory would multiply the
    # requests for the same products.
    departments = [row for row in categories if row["level"] == 1]
    counts["categories"] = len(departments)
    counts["requests"] = 1

    for category in departments:
        page = 1
        while True:
            if counts["requests"] >= max_requests:
                counts["stopped"] = True
                return counts
            if sleep_seconds:
                time.sleep(sleep_seconds)
            payload = graphql(
                session,
                PRODUCTS_QUERY % (category["uid"], PAGE_SIZE, page),
                timeout=timeout,
            )
            counts["requests"] += 1

            items = _items(payload)
            rows = parse_products(payload, category["name"], observed_at)
            counts["skipped"] += len(items) - len(rows)
            for row in rows:
                store.upsert_product(conn, row)
                counts["products"] += 1

            total = (payload.get("products") or {}).get("total_count") or 0
            if not items or page * PAGE_SIZE >= total:
                break
            page += 1

    return counts
