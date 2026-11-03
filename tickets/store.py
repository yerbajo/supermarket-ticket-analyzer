"""SQLite persistence for products, price history, tickets, aliases and Amazon offers.

Small ``sqlite3``-only store (no ORM, no pandas). Callers pass a
connection created with :func:`connect`/:func:`init_db`; when no path is
given the ``DB_PATH`` environment variable is used.
"""

from __future__ import annotations

import os
import re
import sqlite3
import unicodedata
from datetime import datetime, timezone

DB_PATH_ENV = "DB_PATH"
DEFAULT_DB_PATH = "tickets.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY,
    chain TEXT NOT NULL,
    external_id TEXT NOT NULL,
    name TEXT NOT NULL,
    format TEXT,
    price REAL,
    unit_price REAL,
    unit_label TEXT,
    category TEXT,
    url TEXT,
    image_url TEXT,
    image_file TEXT,
    observed_at TEXT NOT NULL,
    UNIQUE(chain, external_id)
);
CREATE TABLE IF NOT EXISTS price_observations (
    id INTEGER PRIMARY KEY,
    product_id INTEGER NOT NULL REFERENCES products(id),
    price REAL,
    unit_price REAL,
    observed_at TEXT NOT NULL,
    UNIQUE(product_id, observed_at, price)
);
CREATE TABLE IF NOT EXISTS tickets (
    id INTEGER PRIMARY KEY,
    chain TEXT NOT NULL,
    ticket_number TEXT,
    purchased_at TEXT NOT NULL,
    total REAL,
    source_pdf TEXT,
    imported_at TEXT NOT NULL,
    UNIQUE(chain, ticket_number, purchased_at)
);
CREATE TABLE IF NOT EXISTS ticket_lines (
    id INTEGER PRIMARY KEY,
    ticket_id INTEGER NOT NULL REFERENCES tickets(id),
    raw_text TEXT NOT NULL,
    product_id INTEGER REFERENCES products(id),
    shop_code TEXT,
    qty REAL,
    price_paid REAL,
    total_paid REAL,
    purchase_unit TEXT,
    match_confidence REAL,
    matched_by TEXT
);
CREATE TABLE IF NOT EXISTS product_aliases (
    id INTEGER PRIMARY KEY,
    chain TEXT NOT NULL,
    raw_text TEXT NOT NULL,
    product_id INTEGER NOT NULL REFERENCES products(id),
    confirmed_by TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(chain, raw_text)
);
CREATE TABLE IF NOT EXISTS product_links (
    id INTEGER PRIMARY KEY,
    product_id INTEGER NOT NULL REFERENCES products(id),
    linked_product_id INTEGER NOT NULL REFERENCES products(id),
    created_at TEXT NOT NULL,
    UNIQUE(product_id, linked_product_id)
);
CREATE TABLE IF NOT EXISTS amazon_offers (
    id INTEGER PRIMARY KEY,
    product_id INTEGER NOT NULL REFERENCES products(id),
    asin TEXT NOT NULL,
    title TEXT NOT NULL,
    price REAL,
    unit_price REAL,
    unit_label TEXT,
    unit_price_text TEXT,
    url TEXT,
    query TEXT,
    sponsored INTEGER NOT NULL DEFAULT 0,
    observed_at TEXT NOT NULL,
    UNIQUE(product_id, asin)
);
"""

# ``product_links`` needs no ``PRAGMA`` migration below: it is a brand-new
# table, and ``CREATE TABLE IF NOT EXISTS`` creates it on an old database just
# as well. The ``_migrate_*`` helpers exist only to add columns to tables that
# already existed before the column was conceived. ``amazon_offers`` is new for
# the same reason.


def utcnow() -> str:
    """Current UTC time as an ISO-8601 string, e.g. ``2026-01-01T00:00:00Z``."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def connect(path=None) -> sqlite3.Connection:
    """Open a row-factory connection, defaulting to ``$DB_PATH``."""
    if path is None:
        path = os.environ.get(DB_PATH_ENV, DEFAULT_DB_PATH)
    connection = sqlite3.connect(str(path))
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def init_db(path) -> sqlite3.Connection:
    """Create the schema idempotently and return the connection."""
    connection = connect(path)
    connection.executescript(SCHEMA)
    _migrate_ticket_lines(connection)
    _migrate_products(connection)
    _migrate_ticket_lines_shop_code(connection)
    connection.commit()
    return connection


def _migrate_products(connection) -> None:
    """Add ``products.image_file`` to databases created before it.

    Same reason as ``_migrate_ticket_lines``: the column records where the
    downloaded picture of a product lives, relative to the images root.
    """
    columns = {row[1] for row in connection.execute("PRAGMA table_info(products)")}
    if "image_file" not in columns:
        connection.execute("ALTER TABLE products ADD COLUMN image_file TEXT")


def _migrate_ticket_lines_shop_code(connection) -> None:
    """Add ``ticket_lines.shop_code``: the product code the ticket itself printed.

    Dia hands the code over with the line, and that code is the catalogue's
    ``external_id``. Storing it is what lets a line whose product is not in our
    catalogue stay honestly unmatched instead of being guessed by name.
    """
    columns = {row[1] for row in connection.execute("PRAGMA table_info(ticket_lines)")}
    if "shop_code" not in columns:
        connection.execute("ALTER TABLE ticket_lines ADD COLUMN shop_code TEXT")


def _migrate_ticket_lines(connection) -> None:
    """Add ``ticket_lines.purchase_unit`` to databases created before it.

    ``CREATE TABLE IF NOT EXISTS`` leaves an existing table untouched, so the
    column is added only when ``PRAGMA table_info`` does not list it. Existing
    rows keep their data; the new column stays NULL for them.
    """
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(ticket_lines)")
    }
    if "purchase_unit" not in columns:
        connection.execute("ALTER TABLE ticket_lines ADD COLUMN purchase_unit TEXT")


def find_product(conn, chain, external_id):
    """Return the product Row for ``(chain, external_id)`` or ``None``."""
    return conn.execute(
        "SELECT * FROM products WHERE chain = ? AND external_id = ?",
        (chain, str(external_id)),
    ).fetchone()


def list_products(conn, chain=None, limit=None):
    """Return product Rows ordered by name, optionally filtered/paged."""
    sql = "SELECT * FROM products"
    params = []
    if chain is not None:
        sql += " WHERE chain = ?"
        params.append(chain)
    sql += " ORDER BY name"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))
    return conn.execute(sql, params).fetchall()


def record_price(conn, product_id, price, unit_price, observed_at):
    """Append a price observation, ignoring an exact duplicate."""
    conn.execute(
        "INSERT OR IGNORE INTO price_observations "
        "(product_id, price, unit_price, observed_at) VALUES (?, ?, ?, ?)",
        (product_id, price, unit_price, observed_at),
    )
    conn.commit()


def upsert_product(conn, product) -> int:
    """Insert or update a product by ``(chain, external_id)``; return its id.

    On insert the first price observation is recorded. On update the
    stored price/observed_at are refreshed and a new observation is
    appended only when the price or the observed_at actually changed.
    """
    chain = product["chain"]
    external_id = str(product["external_id"])
    observed_at = product.get("observed_at") or utcnow()
    price = product.get("price")
    unit_price = product.get("unit_price")

    existing = find_product(conn, chain, external_id)
    if existing is None:
        cursor = conn.execute(
            "INSERT INTO products "
            "(chain, external_id, name, format, price, unit_price, unit_label, "
            " category, url, image_url, observed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                chain, external_id, product["name"], product.get("format"),
                price, unit_price, product.get("unit_label"), product.get("category"),
                product.get("url"), product.get("image_url"), observed_at,
            ),
        )
        product_id = cursor.lastrowid
        record_price(conn, product_id, price, unit_price, observed_at)
    else:
        product_id = existing["id"]
        changed = existing["price"] != price or existing["observed_at"] != observed_at
        conn.execute(
            "UPDATE products SET name = ?, format = ?, price = ?, unit_price = ?, "
            "unit_label = ?, category = ?, url = ?, image_url = ?, observed_at = ? "
            "WHERE id = ?",
            (
                product["name"], product.get("format"), price, unit_price,
                product.get("unit_label"), product.get("category"), product.get("url"),
                product.get("image_url"), observed_at, product_id,
            ),
        )
        if changed:
            record_price(conn, product_id, price, unit_price, observed_at)

    conn.commit()
    return product_id


def add_alias(conn, chain, raw_text, product_id, confirmed_by=None) -> int:
    """Persist (or update) a confirmed alias; return its id."""
    conn.execute(
        "INSERT INTO product_aliases (chain, raw_text, product_id, confirmed_by, created_at) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(chain, raw_text) DO UPDATE SET "
        "product_id = excluded.product_id, confirmed_by = excluded.confirmed_by",
        (chain, raw_text, product_id, confirmed_by, utcnow()),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id FROM product_aliases WHERE chain = ? AND raw_text = ?",
        (chain, raw_text),
    ).fetchone()
    return row["id"]


def resolve_alias(conn, chain, raw_text):
    """Return the product id for a confirmed alias, or ``None``."""
    row = conn.execute(
        "SELECT product_id FROM product_aliases WHERE chain = ? AND raw_text = ?",
        (chain, raw_text),
    ).fetchone()
    return row["product_id"] if row is not None else None


def clear_image_files(conn, chains=None) -> int:
    """Forget every recorded picture so a pass downloads them again; count the rows."""
    sql = "UPDATE products SET image_file = NULL WHERE image_file IS NOT NULL"
    params = []
    if chains:
        sql += f" AND chain IN ({','.join('?' * len(chains))})"
        params.extend(chains)
    cursor = conn.execute(sql, params)
    conn.commit()
    return cursor.rowcount


def set_image_file(conn, product_id, image_file) -> None:
    """Record where a product's downloaded picture lives, relative to the images root."""
    conn.execute("UPDATE products SET image_file = ? WHERE id = ?", (image_file, product_id))
    conn.commit()


def products_missing_images(conn, chains=None, only_bought=False, limit=None):
    """Products that have an image URL but no downloaded picture yet.

    ``only_bought`` restricts the list to products that actually appear in a
    ticket, which is the cheap first pass: the ones a person will look at.
    """
    sql = (
        "SELECT DISTINCT p.id, p.chain, p.external_id, p.image_url FROM products p"
        " WHERE p.image_url IS NOT NULL AND p.image_url <> ''"
        "   AND (p.image_file IS NULL OR p.image_file = '')"
    )
    params = []
    if chains:
        sql += f" AND p.chain IN ({','.join('?' * len(chains))})"
        params.extend(chains)
    if only_bought:
        sql += " AND EXISTS (SELECT 1 FROM ticket_lines tl WHERE tl.product_id = p.id)"
    sql += " ORDER BY p.chain, p.id"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))
    return conn.execute(sql, params).fetchall()


def resolved_line_id(conn, chain, external_code):
    """The catalogue product id for a shop code, or ``None``.

    Dia prints the shop's own product code on every ticket line and the same code
    is the catalogue's ``external_id``, so a Dia line can be matched exactly
    instead of by fuzzy name (verified 10/10, 2026-10-12).
    """
    if external_code in (None, ""):
        return None
    row = conn.execute(
        "SELECT id FROM products WHERE chain = ? AND external_id = ?",
        (chain, str(external_code)),
    ).fetchone()
    return row["id"] if row is not None else None


def link_products(conn, product_id, linked_product_id) -> int:
    """Link two products by hand; return the row id of the (single) link.

    The pair is stored unordered, smaller id first, so linking A→B and B→A is
    the same row and ``UNIQUE(product_id, linked_product_id)`` cannot be
    fooled. A product linked to itself is refused with :class:`ValueError`.
    """
    first, second = sorted((int(product_id), int(linked_product_id)))
    if first == second:
        raise ValueError("un producto no puede vincularse consigo mismo")
    conn.execute(
        "INSERT OR IGNORE INTO product_links "
        "(product_id, linked_product_id, created_at) VALUES (?, ?, ?)",
        (first, second, utcnow()),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id FROM product_links WHERE product_id = ? AND linked_product_id = ?",
        (first, second),
    ).fetchone()
    return row["id"]


def unlink_products(conn, product_id, linked_product_id) -> int:
    """Remove the link between two products, in either direction; count the rows."""
    first, second = sorted((int(product_id), int(linked_product_id)))
    cursor = conn.execute(
        "DELETE FROM product_links WHERE product_id = ? AND linked_product_id = ?",
        (first, second),
    )
    conn.commit()
    return cursor.rowcount


def linked_products(conn, product_id) -> list:
    """The ids of the products linked to ``product_id``, in either direction.

    ``linked`` means either end of the stored pair, so the list is the same no
    matter which side was linked. Ordered by id so callers get a stable answer.
    """
    return [
        row["other"]
        for row in conn.execute(
            "SELECT CASE WHEN product_id = ? THEN linked_product_id ELSE product_id END "
            "AS other FROM product_links "
            "WHERE product_id = ? OR linked_product_id = ? ORDER BY other",
            (product_id, product_id, product_id),
        )
    ]


def _plain_name(name) -> str:
    """Lower-case, accent-free, single-spaced words of a product name."""
    decomposed = unicodedata.normalize("NFKD", str(name or ""))
    without = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return " ".join("".join(ch if ch.isalnum() else " " for ch in without).split()).lower()


#: Product-name keywords that hint at a drugstore/hygiene/non-perishable item,
#: the only kinds of product where Amazon can beat an own-brand supermarket price.
#: This is a keyword filter, not a classification: it matches the start of a word
#: (so "panal" also matches "panales"), it can be fooled ("cremallera" starts like
#: "crema") and a genuinely cheap product whose name is not listed here is simply
#: not searched. Extend the list by hand when a miss shows up.
AMAZON_KEYWORDS = (
    "champu", "acondicionador", "gel de ducha", "gel ducha", "pasta de dientes",
    "dentifrico", "desodorante", "panal", "capsula", "detergente", "suavizante",
    "lavavajillas", "lejia", "limpiador", "maquinilla", "cuchilla", "crema",
    "pienso", "comida para perro", "comida para gato",
)
_AMAZON_KEYWORD_RE = re.compile(
    "(?:^| )(?:" + "|".join(re.escape(keyword) for keyword in AMAZON_KEYWORDS) + ")"
)


def _is_amazon_candidate(name) -> bool:
    """True when ``name`` carries one of the :data:`AMAZON_KEYWORDS`."""
    return _AMAZON_KEYWORD_RE.search(_plain_name(name)) is not None


def save_offers(conn, product_id, offers, query) -> int:
    """Store the offers of one product, keyed on ``(product_id, asin)``.

    Saving the same offers again updates them in place instead of duplicating, so
    the command can be re-run as often as wanted.
    """
    observed_at = utcnow()
    saved = 0
    for offer in offers:
        asin = str(offer["asin"])
        conn.execute(
            "INSERT INTO amazon_offers "
            "(product_id, asin, title, price, unit_price, unit_label, unit_price_text, "
            " url, query, sponsored, observed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(product_id, asin) DO UPDATE SET "
            "title = excluded.title, price = excluded.price, "
            "unit_price = excluded.unit_price, unit_label = excluded.unit_label, "
            "unit_price_text = excluded.unit_price_text, url = excluded.url, "
            "query = excluded.query, sponsored = excluded.sponsored, "
            "observed_at = excluded.observed_at",
            (
                product_id, asin, offer["title"], offer.get("price"),
                offer.get("unit_price"), offer.get("unit_label"),
                offer.get("unit_price_text"), offer.get("url"), query,
                1 if offer.get("sponsored") else 0, observed_at,
            ),
        )
        saved += 1
    conn.commit()
    return saved


def offers_for(conn, product_id) -> list:
    """Every stored Amazon offer of a product, cheapest first and stable on ties."""
    return conn.execute(
        "SELECT * FROM amazon_offers WHERE product_id = ? "
        "ORDER BY sponsored, COALESCE(unit_price, 1e18), asin",
        (product_id,),
    ).fetchall()


def best_offer(conn, product_id):
    """The best stored Amazon offer for a product, or ``None``.

    The cheapest by unit price among the non-sponsored offers, falling back to the
    sponsored ones, and always restricted to the product's own unit when such an
    offer exists (a €/kg against a €/l is not a comparison).
    """
    product = conn.execute(
        "SELECT unit_label FROM products WHERE id = ?", (product_id,)
    ).fetchone()
    unit = product["unit_label"] if product is not None else None
    offers = offers_for(conn, product_id)

    def cheapest(rows):
        with_price = [row for row in rows if row["unit_price"] is not None]
        if not with_price:
            return None
        return min(with_price, key=lambda row: (row["unit_price"], row["asin"]))

    def by_sponsorship(rows):
        return cheapest([row for row in rows if not row["sponsored"]]) or cheapest(
            [row for row in rows if row["sponsored"]]
        )

    if unit:
        same_unit = [row for row in offers if row["unit_label"] == unit]
        chosen = by_sponsorship(same_unit)
        if chosen is not None:
            return chosen
    return by_sponsorship(offers)


def amazon_candidates(conn, limit=None) -> list:
    """Products worth searching on Amazon: branded, priced per unit and drugstore-like.

    Own-brand products are excluded on purpose: their €/litre is two to three times
    cheaper than Amazon's, so searching them would only be wasted requests. The
    rest of the rule is :data:`AMAZON_KEYWORDS`, a keyword filter and not a
    classification, ordered by ``(chain, name, id)`` so a run is reproducible.
    """
    from tickets import brands  # local import: the store keeps few dependencies

    rows = conn.execute(
        "SELECT * FROM products WHERE unit_price IS NOT NULL AND unit_label IS NOT NULL "
        "AND unit_label <> '' ORDER BY chain, name, id"
    ).fetchall()
    candidates = [
        row
        for row in rows
        if not brands.is_own_brand(row["chain"], row["name"]) and _is_amazon_candidate(row["name"])
    ]
    return candidates[: int(limit)] if limit is not None else candidates


def upsert_ticket(conn, ticket) -> tuple[int, bool]:
    """Insert ``ticket`` and its lines; return ``(ticket_id, inserted)``.

    Keyed on ``(chain, ticket_number, purchased_at)``: re-importing the same
    ticket returns the existing id with ``inserted=False`` and does not duplicate
    the ticket or its lines. A line may carry ``product_id``, ``matched_by`` and
    ``match_confidence`` (a code match, for instance); those are written on both
    paths, so re-importing a ticket can improve its matches without touching the
    purchase data.
    """
    existing = conn.execute(
        "SELECT id FROM tickets WHERE chain = ? AND ticket_number = ? AND purchased_at = ?",
        (ticket["chain"], ticket.get("ticket_number"), ticket["purchased_at"]),
    ).fetchone()
    if existing is not None:
        _apply_line_matches(conn, existing["id"], ticket.get("lines", []))
        return existing["id"], False

    cursor = conn.execute(
        "INSERT INTO tickets "
        "(chain, ticket_number, purchased_at, total, source_pdf, imported_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            ticket["chain"], ticket.get("ticket_number"), ticket["purchased_at"],
            ticket.get("total"), ticket.get("source_pdf"), utcnow(),
        ),
    )
    ticket_id = cursor.lastrowid
    for line in ticket.get("lines", []):
        conn.execute(
            "INSERT INTO ticket_lines "
            "(ticket_id, raw_text, qty, price_paid, total_paid, purchase_unit, "
            " product_id, matched_by, match_confidence, shop_code) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ticket_id, line["raw_text"], line.get("qty"), line.get("price_paid"),
                line.get("total_paid"), line.get("purchase_unit"),
                line.get("product_id"), line.get("matched_by"), line.get("match_confidence"),
                line.get("shop_code"),
            ),
        )
    conn.commit()
    return ticket_id, True


def _apply_line_matches(conn, ticket_id, lines) -> int:
    """Fill in the unknown matches of an already stored ticket; count the rows.

    Only lines with ``product_id IS NULL`` are touched, and the purchase data
    (quantity, prices) is never rewritten: a re-import may improve a match and
    must not change what the ticket says.
    """
    cambiados = 0
    for line in lines:
        if line.get("product_id") is None:
            continue
        cursor = conn.execute(
            "UPDATE ticket_lines SET product_id = ?, matched_by = ?, match_confidence = ? "
            "WHERE ticket_id = ? AND raw_text = ? AND product_id IS NULL",
            (
                line["product_id"], line.get("matched_by"), line.get("match_confidence"),
                ticket_id, line["raw_text"],
            ),
        )
        cambiados += cursor.rowcount

    # The shop's code is written even when the ticket was already stored: it is what
    # lets a later run resolve the line by code instead of by name.
    for line in lines:
        if line.get("shop_code"):
            conn.execute(
                "UPDATE ticket_lines SET shop_code = ? WHERE ticket_id = ? AND raw_text = ? "
                "AND (shop_code IS NULL OR shop_code = '')",
                (line["shop_code"], ticket_id, line["raw_text"]),
            )

    # A line that carries the shop's own code is never a guess. If the code did not
    # resolve, an older match by name on that same line is a *different* product
    # (measured: a 16-pack read as a 4-pack), so it goes.
    cursor = conn.execute(
        "UPDATE ticket_lines SET product_id = NULL, matched_by = NULL, match_confidence = NULL "
        "WHERE ticket_id = ? AND shop_code IS NOT NULL AND shop_code <> '' "
        "AND matched_by = 'fuzzy'",
        (ticket_id,),
    )
    cambiados += cursor.rowcount
    conn.commit()
    return cambiados
