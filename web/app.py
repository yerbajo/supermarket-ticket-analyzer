"""Compras: server-rendered web app over the SQLite store.

One small app: the purchases list, one purchase's breakdown, and the upload
form that parses a ticket PDF with :mod:`tickets.parsing`. No auth, no sessions,
no ORM; the database path comes from ``$DB_PATH`` (project default
``tickets.db``).

Every amount is formatted up front, in Spanish, and a missing value stays
missing (:data:`DASH`) instead of becoming a zero or a guess.
"""

from __future__ import annotations

import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.templating import Jinja2Templates

from reports.report import MONTH_NAMES
from tickets import brands, equivalents, matching, parsing, store

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

#: Hard cap for one upload request. The ASGI middleware enforces it while the
#: body streams; the route also rejects a declared Content-Length over it before
#: reading anything, so a normal browser gets the page instead of a reset.
MAX_UPLOAD_BYTES = 10 * 1024 * 1024

CHAIN_LOGOS = {
    "Mercadona": "mercadona.svg",
    "Dia": "dia.svg",
    "MAS": "mas.svg",
}

#: Chains offered by the product search, plus the "all" option, in the task's order.
CHAIN_OPTIONS = [
    {"value": "", "label": "Todas"},
    {"value": "Mercadona", "label": "Mercadona"},
    {"value": "Dia", "label": "Día"},
    {"value": "MAS", "label": "MAS"},
]

#: Catalogue results shown for one search term.
SEARCH_LIMIT = 20

#: Products shown in each of the main page's two ranked lists.
RANK_LIMIT = 3

#: How many products the most-bought list shows.
TOP_LIMIT = 5

#: Units whose printed price is a price per unit of measure, not per item.
WEIGHT_UNITS = {"kg", "l"}

#: Shown wherever a value is unknown: never a guess, never a zero.
DASH = "—"

#: The shop's clock: the current month is read here, not off the server's UTC.
SHOP_TZ = ZoneInfo("Europe/Madrid")

#: A month key the selector understands: ``YYYY-MM``.
MONTH_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

app = FastAPI(
    title="Compras",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=TEMPLATES_DIR)


# --------------------------------------------------------------------------- #
# Formatting: Spanish decimal comma, thousands dot, tabular figures in CSS.
# --------------------------------------------------------------------------- #


def db_path() -> Path:
    """The configured database path (``$DB_PATH``, project default last)."""
    return Path(os.environ.get(store.DB_PATH_ENV, store.DEFAULT_DB_PATH))


def current_month() -> str:
    """The shop's current month (``Europe/Madrid``), e.g. ``2026-10``."""
    return datetime.now(SHOP_TZ).strftime("%Y-%m")


def month_label(month_key: str) -> str:
    """``2026-10`` -> ``octubre 2026``; ``todos`` -> ``Todo el histórico``.

    The Spanish month names come from :mod:`reports.report`, the one place
    that already names them, so there is never a second spelling.
    """
    if month_key == "todos":
        return "Todo el histórico"
    year, month = month_key.split("-")
    return f"{MONTH_NAMES[int(month) - 1]} {year}"


def selected_month(raw: str | None) -> str:
    """The month to show: a valid ``YYYY-MM``/``todos``, else the current one.

    A well-formed month with no data still passes through, so the page can say
    honestly that it has none instead of silently jumping to another month.
    """
    if raw == "todos" or (raw and MONTH_PATTERN.match(raw)):
        return raw
    return current_month()


def upload_limit_label() -> str:
    """Human label for :data:`MAX_UPLOAD_BYTES`, e.g. ``10 MB``."""
    megabytes = MAX_UPLOAD_BYTES / (1024 * 1024)
    if megabytes >= 1:
        return f"{megabytes:g} MB"
    return f"{MAX_UPLOAD_BYTES // 1024} KB"


def _es_number(value, decimals: int = 2) -> str:
    """``1234.5`` -> ``1.234,50`` (Spanish separators)."""
    text = f"{float(value):,.{decimals}f}"
    return text.replace(",", "\x00").replace(".", ",").replace("\x00", ".")


def format_amount(value) -> str | None:
    """``1234.56`` -> ``1.234,56 €``; ``None`` stays ``None``."""
    return None if value is None else f"{_es_number(value)} €"


def format_quantity(qty, unit) -> str | None:
    """``(1.149, 'kg')`` -> ``1,149 kg``; ``(2, 'ud')`` -> ``2 ud``."""
    if qty is None:
        return None
    quantity = float(qty)
    text = (
        _es_number(quantity, 0)
        if quantity.is_integer()
        else f"{quantity:g}".replace(".", ",")
    )
    return f"{text} {unit}" if unit else text


def format_unit_price(price, unit) -> str | None:
    """``(1.65, 'kg')`` -> ``1,65 €/kg``; ``(3.58, 'ud')`` -> ``3,58 €``."""
    if price is None:
        return None
    if unit in WEIGHT_UNITS:
        return f"{_es_number(price)} €/{unit}"
    return f"{_es_number(price)} €"


def format_datetime(value) -> str | None:
    """``2026-10-12T11:22:15`` -> ``12/10/2026, 11:22``."""
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError:
        return str(value)
    return f"{moment:%d/%m/%Y}, {moment:%H:%M}"


def format_date(value) -> str | None:
    """``2026-10-12T09:47:03Z`` -> ``12/10/2026``."""
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value)
    return f"{moment:%d/%m/%Y}"


def format_signed_unit_price(delta, unit) -> str | None:
    """``(0.35, 'kg')`` -> ``+0,35 €/kg``; ``None`` stays ``None``.

    The sign reads against the product on the page: ``+`` means the other
    chain costs more, ``-`` means it costs less.
    """
    if delta is None:
        return None
    sign = "+" if delta > 0 else ("-" if delta < 0 else "")
    value = _es_number(abs(delta))
    if unit in WEIGHT_UNITS:
        return f"{sign}{value} €/{unit}"
    return f"{sign}{value} €"


# --------------------------------------------------------------------------- #
# Queries and view models.
# --------------------------------------------------------------------------- #


def _month_filter(month: str) -> tuple[str, tuple]:
    """The ``WHERE`` clause and params that keep one month, or all history."""
    if month == "todos":
        return "", ()
    return " WHERE substr(purchased_at, 1, 7) = ?", (month,)


def ticket_rows(conn, month: str = "todos") -> list:
    """Tickets in ``month`` (or all), newest first, ready to render."""
    where, params = _month_filter(month)
    return [
        {
            "id": row["id"],
            "chain": row["chain"],
            "logo": CHAIN_LOGOS.get(row["chain"]),
            "iso": row["purchased_at"],
            "datetime": format_datetime(row["purchased_at"]),
            "amount": format_amount(row["total"]) or DASH,
        }
        for row in conn.execute(
            f"SELECT * FROM tickets{where} ORDER BY purchased_at DESC, id DESC",
            params,
        )
    ]


def spend_summary(conn, month: str = "todos") -> dict:
    """Total, count, average and per chain for one month (or all history)."""
    where, params = _month_filter(month)
    total, count = conn.execute(
        f"SELECT COALESCE(SUM(total), 0), COUNT(*) FROM tickets{where}", params
    ).fetchone()
    # No purchases means no average: a dash, never a divide-by-zero guess.
    average = total / count if count else None
    by_chain = [
        {
            "chain": row["chain"],
            "logo": CHAIN_LOGOS.get(row["chain"]),
            "amount": format_amount(row["spend"]) or DASH,
        }
        for row in conn.execute(
            f"SELECT chain, SUM(total) AS spend FROM tickets{where} "
            "GROUP BY chain ORDER BY spend DESC",
            params,
        )
    ]
    return {
        "total": format_amount(total) or DASH,
        "count": count,
        "average": format_amount(average) or DASH,
        "by_chain": by_chain,
    }


def available_months(conn) -> list[str]:
    """Months with at least one ticket, newest first."""
    return [
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT substr(purchased_at, 1, 7) AS month FROM tickets "
            "ORDER BY month DESC"
        )
    ]


def _frequency_text(days) -> str | None:
    """``12`` -> ``cada 12 días``; ``None`` when there is nothing to average.

    Only whole days and months are named: inventing "cada 1,7 semanas" helps nobody.
    """
    if days is None:
        return None
    dias = max(1, round(days))
    if dias < 45:
        return f"cada {dias} día" if dias == 1 else f"cada {dias} días"
    meses = max(2, round(dias / 30))
    return f"cada {meses} meses"


def top_products(conn, limit=TOP_LIMIT) -> list:
    """The products bought most often, with how often they come round.

    Over the whole history on purpose: how often a product comes round is a
    property of all the tickets, not of one month, and the section says so.
    ``veces`` counts distinct tickets, ``units_text`` joins one total per unit
    (never a mixed sum) and ``frequency`` is the mean gap between purchases in
    days, which needs two purchases to exist at all.
    """
    rows = conn.execute(
        "SELECT l.product_id AS product_id, p.name AS name, p.chain AS chain, "
        "l.purchase_unit AS unit, l.qty AS qty, t.id AS ticket_id, "
        "t.purchased_at AS purchased_at "
        "FROM ticket_lines l "
        "JOIN tickets t ON t.id = l.ticket_id "
        "JOIN products p ON p.id = l.product_id "
        "WHERE l.product_id IS NOT NULL "
        "ORDER BY l.product_id, t.purchased_at, t.id, l.id"
    ).fetchall()

    por_producto: dict = {}
    for row in rows:
        por_producto.setdefault(row["product_id"], []).append(row)

    listado = []
    for product_id, lines in por_producto.items():
        tickets = {line["ticket_id"] for line in lines}
        fechas = sorted({line["purchased_at"] for line in lines})
        frecuencia = None
        if len(fechas) >= 2:
            momentos = [datetime.fromisoformat(fecha) for fecha in fechas]
            huecos = [
                (momentos[i] - momentos[i - 1]).total_seconds() / 86400
                for i in range(1, len(momentos))
            ]
            frecuencia = sum(huecos) / len(huecos)

        por_unidad: dict = {}
        for line in lines:
            if line["qty"] is None:
                continue
            por_unidad[line["unit"]] = por_unidad.get(line["unit"], 0) + line["qty"]
        totales = [
            format_quantity(cantidad, unidad)
            for unidad, cantidad in sorted(por_unidad.items(), key=lambda par: -par[1])
        ]

        listado.append(
            {
                "product_id": product_id,
                "name": lines[0]["name"],
                "chain": lines[0]["chain"],
                "times": len(tickets),
                "units_text": " + ".join(t for t in totales if t) or DASH,
                "frequency": _frequency_text(frecuencia),
                "last_date": format_date(fechas[-1]),
                "last_iso": fechas[-1],
            }
        )

    listado.sort(key=lambda item: (-item["times"], item["name"]))
    return listado[:limit]


def _ranked(changes: list, direction: str) -> list:
    """At most :data:`RANK_LIMIT` changes of one direction, biggest first."""
    picked = [item for item in changes if item["change"]["direction"] == direction]
    picked.sort(key=lambda item: item["change"]["value"], reverse=direction == "up")
    return picked[:RANK_LIMIT]


def monthly_price_changes(conn, months=None) -> dict:
    """The main page's two ranked lists: risers and fallers, newest month first.

    Only products with a line in the newest month that has tickets take part,
    so a product bought only earlier never shows up. Each is ranked by the
    change between the first and the most recent price actually paid for the
    same unit (``kg``, ``l`` or ``ud``); a product sold in two units gets one
    entry each and a ``€/kg`` is never set against a ``€/ud``. ``compared``
    tells an empty list apart from a month with nothing to compare at all.
    """
    months = available_months(conn) if months is None else months
    month = months[0] if months else None
    changes: list[dict] = []
    compared = False
    if month is not None:
        bought = [
            row[0]
            for row in conn.execute(
                "SELECT DISTINCT l.product_id FROM ticket_lines l "
                "JOIN tickets t ON t.id = l.ticket_id "
                "WHERE substr(t.purchased_at, 1, 7) = ? AND l.product_id IS NOT NULL",
                (month,),
            )
        ]
        if bought:
            marks = ",".join("?" * len(bought))
            rows = conn.execute(
                "SELECT l.product_id AS product_id, p.name AS name, "
                "p.chain AS chain, l.purchase_unit AS unit, l.price_paid AS price_paid, "
                "t.purchased_at AS purchased_at "
                "FROM ticket_lines l "
                "JOIN tickets t ON t.id = l.ticket_id "
                "JOIN products p ON p.id = l.product_id "
                f"WHERE l.product_id IN ({marks}) "
                "ORDER BY l.product_id, t.purchased_at, t.id, l.id",
                bought,
            ).fetchall()
            per_unit: dict = {}
            for row in rows:
                per_unit.setdefault((row["product_id"], row["unit"]), []).append(row)
            for (product_id, unit), lines in per_unit.items():
                # Each price keeps its own date: the comparison has to be able to
                # say which purchase it is comparing against.
                pagadas = [
                    (line["purchased_at"], line["price_paid"])
                    for line in lines
                    if line["price_paid"] is not None
                ]
                if len(pagadas) < 2:
                    continue
                compared = True
                (fecha_primera, precio_primero), (fecha_ultima, precio_ultimo) = pagadas[0], pagadas[-1]
                change = _percent_change(precio_primero, precio_ultimo)
                if change is None or change["direction"] == "same":
                    continue
                changes.append(
                    {
                        "product_id": product_id,
                        "name": lines[0]["name"],
                        "chain": lines[0]["chain"],
                        "first_text": format_unit_price(precio_primero, unit),
                        "last_text": format_unit_price(precio_ultimo, unit),
                        "first_date": format_date(fecha_primera),
                        "last_date": format_date(fecha_ultima),
                        "first_iso": fecha_primera,
                        "last_iso": fecha_ultima,
                        "change": change,
                        "verdict": _change_verdict(change["direction"]),
                    }
                )
    return {
        "month": month,
        "month_label": month_label(month) if month else None,
        "compared": compared,
        "risers": _ranked(changes, "up"),
        "fallers": _ranked(changes, "down"),
    }


def month_options(months) -> list[dict]:
    """``YYYY-MM`` options newest first, current month always, plus ``Todos``."""
    present = set(months) | {current_month()}
    options = [
        {"value": key, "label": month_label(key)}
        for key in sorted(present, reverse=True)
    ]
    options.append({"value": "todos", "label": "Todos"})
    return options


def ticket_detail(conn, ticket_id) -> dict | None:
    """One ticket with its lines ready to render, or ``None`` when absent."""
    ticket = conn.execute(
        "SELECT * FROM tickets WHERE id = ?", (ticket_id,)
    ).fetchone()
    if ticket is None:
        return None
    lines = conn.execute(
        "SELECT * FROM ticket_lines WHERE ticket_id = ? ORDER BY id", (ticket_id,)
    ).fetchall()
    return {
        "id": ticket["id"],
        "chain": ticket["chain"],
        "logo": CHAIN_LOGOS.get(ticket["chain"]),
        "iso": ticket["purchased_at"],
        "datetime": format_datetime(ticket["purchased_at"]),
        "ticket_number": ticket["ticket_number"],
        "amount": format_amount(ticket["total"]) or DASH,
        "lines": [
            {
                "raw_text": line["raw_text"],
                "quantity": format_quantity(line["qty"], line["purchase_unit"]) or DASH,
                "unit_price": format_unit_price(
                    line["price_paid"], line["purchase_unit"]
                )
                or DASH,
                "total": format_amount(line["total_paid"]) or DASH,
                "product_id": line["product_id"],
                "matched": line["product_id"] is not None,
            }
            for line in lines
        ],
    }


# --------------------------------------------------------------------------- #
# Product page: own purchase history, then the cross-chain comparison.
# --------------------------------------------------------------------------- #


def _percent_change(first, last) -> dict | None:
    """Signed change ``first`` -> ``last`` in percent, with a word and an arrow.

    ``None`` when there is nothing to compare. A change that rounds to ``0,0 %``
    is reported as "Sin cambios" rather than a signed zero dressed as data.
    ``value`` carries the unrounded change so callers can rank without parsing
    the Spanish text back out of it.
    """
    if first is None or last is None or first == 0:
        return None
    change = (last - first) / first * 100
    if abs(change) < 0.05:  # rounds to 0,0 %
        return {"text": "Sin cambios", "direction": "same", "arrow": "=", "value": 0.0}
    return {
        "text": f"{'+' if change > 0 else '-'}{_es_number(abs(change), 1)} %",
        "direction": "up" if change > 0 else "down",
        "arrow": "↑" if change > 0 else "↓",
        "value": change,
    }


def _change_verdict(direction: str, against: str = "ahora") -> str:
    """The word beside the arrow: colour never carries the meaning alone.

    ``against`` names what the change is measured against, so the paid change
    ("más caro ahora") and the catalogue one ("más caro que tu primera compra")
    never read as the same statement.
    """
    if direction == "up":
        return f"más caro {against}"
    if direction == "down":
        return f"más barato {against}"
    return "al mismo precio"


def _history_group(unit, rows) -> dict:
    """One unit's slice of a product's history: totals, range, average, change.

    A unit price is the average actually paid: weighted by the quantity when the
    ticket prints one, and a plain mean otherwise. The change only exists once
    the product was bought on more than one ticket, so a single purchase never
    invents a percentage.
    """
    prices = [row["price_paid"] for row in rows if row["price_paid"] is not None]
    quantities = [row["qty"] for row in rows if row["qty"] is not None]
    weighted = [
        (row["qty"], row["price_paid"])
        for row in rows
        if row["qty"] is not None and row["qty"] > 0 and row["price_paid"] is not None
    ]
    if weighted:
        average = sum(q * p for q, p in weighted) / sum(q for q, _ in weighted)
    elif prices:
        average = sum(prices) / len(prices)
    else:
        average = None

    total_qty = sum(quantities) if quantities else None
    first_price = prices[0] if prices else None
    last_price = prices[-1] if prices else None
    con_precio = [row for row in rows if row["price_paid"] is not None]
    first_date = format_date(con_precio[0]["purchased_at"]) if con_precio else None
    last_date = format_date(con_precio[-1]["purchased_at"]) if con_precio else None
    first_iso = con_precio[0]["purchased_at"] if con_precio else None
    last_iso = con_precio[-1]["purchased_at"] if con_precio else None
    change = None
    if len({row["ticket_id"] for row in rows}) >= 2 and len(prices) >= 2:
        change = _percent_change(first_price, last_price)
    return {
        "unit": unit,
        "title": f"Precio por {unit}" if unit else "Precio",
        "units_text": format_quantity(total_qty, unit) or DASH,
        "min_text": format_unit_price(min(prices), unit) if prices else DASH,
        "max_text": format_unit_price(max(prices), unit) if prices else DASH,
        "average_text": format_unit_price(average, unit) if average is not None else DASH,
        "average_value": average,
        "first_value": first_price,
        "first_text": (
            format_unit_price(first_price, unit) if first_price is not None else DASH
        ),
        "last_text": (
            format_unit_price(last_price, unit) if last_price is not None else DASH
        ),
        # Which purchase each price comes from: the comparison has to say it.
        "first_date": first_date,
        "last_date": last_date,
        "first_iso": first_iso,
        "last_iso": last_iso,
        "change": change,
        "verdict": _change_verdict(change["direction"]) if change else None,
    }


def _price_comparison(product, groups) -> dict | None:
    """What the product costs now against what was paid on average.

    Only compared when the catalogue unit and a ticket unit agree, so a ``€/kg``
    is never set against a ``€/ud``. With no comparable unit the page says so and
    keeps the current price, instead of inventing a difference.
    """
    current_price = product["unit_price"]
    if current_price is None:
        return None
    unit = product["unit_label"]
    current = format_unit_price(current_price, unit) or DASH
    matching = next((group for group in groups if group["unit"] == unit), None)
    if matching is None or matching["average_value"] is None:
        return {
            "comparable": False,
            "current": current,
            "note": (
                "En los tickets lo pagaste en otra unidad, así que no se puede "
                "comparar con el precio actual."
            ),
        }

    delta = current_price - matching["average_value"]
    if delta < 0:
        direction, arrow, verdict = "down", "↓", "más barato ahora"
    elif delta > 0:
        direction, arrow, verdict = "up", "↑", "más caro ahora"
    else:
        direction, arrow, verdict = "same", "=", "al mismo precio"
    comparison = {
        "comparable": True,
        "current": current,
        "paid": matching["average_text"],
        "delta_text": format_signed_unit_price(delta, unit),
        "direction": direction,
        "arrow": arrow,
        "verdict": verdict,
        "first_text": matching["first_text"],
        "change": None,
        "change_verdict": None,
    }
    # The same change measured against the shelf, never against a paid price
    # without saying which one is which. Only when that unit has a paid change
    # to sit beside: with fewer than two purchases there is no percentage at all.
    if matching["change"] is not None and matching["first_value"] is not None:
        change = _percent_change(matching["first_value"], current_price)
        if change is not None:
            comparison["change"] = change
            comparison["change_verdict"] = _change_verdict(
                change["direction"], against="que tu primera compra"
            )
    return comparison


def purchase_history(conn, product) -> dict:
    """One product's purchases, oldest first, grouped by the unit it was sold in."""
    rows = conn.execute(
        "SELECT t.id AS ticket_id, t.chain AS chain, t.purchased_at AS purchased_at, "
        "l.qty AS qty, l.price_paid AS price_paid, l.total_paid AS total_paid, "
        "l.purchase_unit AS purchase_unit "
        "FROM ticket_lines l JOIN tickets t ON t.id = l.ticket_id "
        "WHERE l.product_id = ? "
        "ORDER BY t.purchased_at ASC, t.id ASC, l.id ASC",
        (product["id"],),
    ).fetchall()
    if not rows:
        return {"has": False, "count": 0, "groups": [], "rows": [], "compare": None}

    by_unit: dict = {}
    for row in rows:
        by_unit.setdefault(row["purchase_unit"], []).append(row)
    groups = [
        _history_group(unit, by_unit[unit])
        for unit in sorted(by_unit, key=lambda u: (u is None, u or ""))
    ]
    return {
        "has": True,
        "count": len({row["ticket_id"] for row in rows}),
        "groups": groups,
        "rows": [
            {
                "ticket_id": row["ticket_id"],
                "chain": row["chain"],
                "logo": CHAIN_LOGOS.get(row["chain"]),
                "iso": row["purchased_at"],
                "date": format_date(row["purchased_at"]),
                "units_text": format_quantity(row["qty"], row["purchase_unit"]) or DASH,
                "unit_price": format_unit_price(row["price_paid"], row["purchase_unit"])
                or DASH,
                "total": format_amount(row["total_paid"]) or DASH,
            }
            for row in rows
        ],
        "compare": _price_comparison(product, groups),
    }


# --------------------------------------------------------------------------- #
# Product page: the same product in the other chains, plus own-brand swaps.
# --------------------------------------------------------------------------- #


def _selected_chain(raw: str | None) -> str:
    """The chain to search in: a known chain, else all of them (``""``)."""
    return raw if raw in CHAIN_LOGOS else ""


def search_products(conn, term: str, chain: str = "", exclude_id=None) -> list:
    """Catalogue products whose name contains ``term``, ready to render.

    ``chain`` (``""`` for all) narrows the search; ``exclude_id`` drops the
    product showing the form, which must never be offered as its own candidate.
    A blank term searches nothing instead of listing the whole catalogue.
    """
    term = (term or "").strip()
    if not term:
        return []
    sql = "SELECT * FROM products WHERE name LIKE ?"
    params = [f"%{term}%"]
    if chain:
        sql += " AND chain = ?"
        params.append(chain)
    if exclude_id is not None:
        sql += " AND id <> ?"
        params.append(int(exclude_id))
    sql += " ORDER BY chain, name LIMIT ?"
    params.append(SEARCH_LIMIT)
    return [
        {
            "product_id": row["id"],
            "chain": row["chain"],
            "logo": CHAIN_LOGOS.get(row["chain"]),
            "name": row["name"],
            "price": format_amount(row["price"]) or DASH,
            "unit_price": format_unit_price(row["unit_price"], row["unit_label"])
            or DASH,
        }
        for row in conn.execute(sql, params)
    ]


def _comparable(item) -> dict:
    """One other-chain entry, formatted; a missing counterpart stays a dash."""
    base = {
        "chain": item["chain"],
        "logo": CHAIN_LOGOS.get(item["chain"]),
        "matched": item["product_id"] is not None,
        "product_id": item["product_id"],
        "name": item["name"],
        "price": DASH,
        "unit_price": DASH,
        "delta_text": None,
        "verdict": None,
        "direction": None,
        "arrow": None,
        "source": item.get("source"),
        "manual": item.get("source") == "manual",
        "image_file": item.get("image_file"),
    }
    if not base["matched"]:
        return base

    base["price"] = format_amount(item["price"]) or DASH
    base["unit_price"] = (
        format_unit_price(item["unit_price"], item["unit_label"]) or DASH
    )
    delta = item["delta_unit"]
    if delta is None:
        # Different units: the page says so instead of inventing a comparison.
        base["verdict"] = "Unidades no comparables"
        base["direction"] = "none"
        return base
    base["delta_text"] = format_signed_unit_price(delta, item["unit_label"])
    if delta < 0:
        base["direction"], base["arrow"], base["verdict"] = "down", "↓", "Más barato"
    elif delta > 0:
        base["direction"], base["arrow"], base["verdict"] = "up", "↑", "Más caro"
    else:
        base["direction"], base["arrow"], base["verdict"] = "same", "=", "Mismo precio"
    return base


def _own_brand_groups(conn, product, comparables) -> list:
    """Per-other-chain own-brand alternatives, formatted; empty groups say so."""
    alternatives = equivalents.own_brand_alternatives(conn, product)
    by_chain = {}
    for alt in alternatives:
        by_chain.setdefault(alt["chain"], []).append(
            {
                "product_id": alt["product_id"],
                "name": alt["name"],
                "brand": alt["brand"] or DASH,
                "image_file": alt["image_file"],
                "price": format_amount(alt["price"]) or DASH,
                "unit_price": format_unit_price(alt["unit_price"], alt["unit_label"])
                or DASH,
            }
        )
    return [
        {
            "chain": item["chain"],
            "logo": CHAIN_LOGOS.get(item["chain"]),
            "alternatives": by_chain.get(item["chain"], []),
        }
        for item in comparables
    ]


def _amazon_view(conn, product) -> dict:
    """The best Amazon offer for a product, formatted, or an honest empty state.

    The signed difference is only shown when both sides share a unit: a €/kg against
    a €/l gets a word, never a made-up number.
    """
    offers = store.offers_for(conn, product["id"])
    best = store.best_offer(conn, product["id"])
    if best is None:
        # No offer with a unit price: either not searched, or searched without a
        # usable unit price. The template tells those two apart by ``count``.
        return {"offer": None, "count": len(offers)}

    delta = None
    if (
        best["unit_price"] is not None
        and product["unit_price"] is not None
        and best["unit_label"] == product["unit_label"]
    ):
        delta = best["unit_price"] - product["unit_price"]

    view = {
        "title": best["title"],
        "url": best["url"] or f"https://www.amazon.es/dp/{best['asin']}",
        "price": format_amount(best["price"]) or DASH,
        "unit_price_text": best["unit_price_text"] or DASH,
        "unit_price": format_unit_price(best["unit_price"], best["unit_label"]) or DASH,
        "sponsored": bool(best["sponsored"]),
        "observed_at": format_date(best["observed_at"]),
        "delta_text": None,
        "direction": None,
        "arrow": None,
        "verdict": None,
    }
    if delta is None:
        view["direction"] = "none"
        view["verdict"] = "Sin comparación por unidad"
    elif delta < 0:
        view["delta_text"] = format_signed_unit_price(delta, product["unit_label"])
        view["direction"], view["arrow"], view["verdict"] = "down", "↓", "Más barato en Amazon"
    elif delta > 0:
        view["delta_text"] = format_signed_unit_price(delta, product["unit_label"])
        view["direction"], view["arrow"], view["verdict"] = "up", "↑", "Más caro en Amazon"
    else:
        view["delta_text"] = format_signed_unit_price(0, product["unit_label"])
        view["direction"], view["arrow"], view["verdict"] = "same", "=", "Mismo precio"
    return {"offer": view, "count": len(offers)}


def product_detail(conn, product_id, buscar=None, cadena=None) -> dict | None:
    """One catalogue product with its cross-chain view, or ``None``.

    ``buscar``/``cadena`` carry the link-search form: a term re-renders the
    page with the candidates the user can link by hand.
    """
    product = conn.execute(
        "SELECT * FROM products WHERE id = ?", (product_id,)
    ).fetchone()
    if product is None:
        return None

    comparables = [
        _comparable(item)
        for item in equivalents.same_product_in_other_chains(conn, product)
    ]
    own_brand = brands.detect_own_brand(product["chain"], product["name"])
    term = (buscar or "").strip()
    chain = _selected_chain(cadena)
    return {
        "id": product["id"],
        "name": product["name"],
        "chain": product["chain"],
        "logo": CHAIN_LOGOS.get(product["chain"]),
        "image_file": product["image_file"],
        "category": product["category"] or DASH,
        "price": format_amount(product["price"]) or DASH,
        "unit_price": format_unit_price(product["unit_price"], product["unit_label"])
        or DASH,
        "observed_at": format_date(product["observed_at"]),
        "observed_at_iso": product["observed_at"],
        "url": product["url"],
        "own_brand": own_brand,
        "comparables": comparables,
        "own_groups": _own_brand_groups(conn, product, comparables) if own_brand else [],
        "history": purchase_history(conn, product),
        "amazon": _amazon_view(conn, product),
        "search": {
            "term": term,
            "chain": chain,
            "chains": CHAIN_OPTIONS,
            "results": search_products(conn, term, chain, exclude_id=product["id"]),
        },
    }


# --------------------------------------------------------------------------- #
# Upload plumbing.
# --------------------------------------------------------------------------- #


class RequestTooLarge(Exception):
    """The request body ran past :data:`MAX_UPLOAD_BYTES` while streaming."""


class LimitRequestBody:
    """ASGI middleware: raise once a request body passes ``max_bytes``.

    The multipart parser spools files to disk and would happily accept any
    size, so the cap has to live at the byte stream. Raising here propagates
    out of ``request.form()`` inside the route, where FastAPI's exception
    handler renders the upload page instead of a 500.
    """

    def __init__(self, app, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        seen = 0

        async def limited_receive():
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > self.max_bytes:
                    raise RequestTooLarge
            return message

        await self.app(scope, limited_receive, send)


app.add_middleware(LimitRequestBody, max_bytes=MAX_UPLOAD_BYTES)


def _looks_like_pdf(filename: str, content_type) -> bool:
    """A PDF by extension or by declared type (browsers disagree on both)."""
    if filename.lower().endswith(".pdf"):
        return True
    return (content_type or "").split(";")[0].strip() == "application/pdf"


def _import_bytes(data: bytes, name: str) -> dict:
    """Write the upload to a temp file, import it, match it, then clean up."""
    with tempfile.TemporaryDirectory(prefix="compras-") as tmp:
        path = Path(tmp) / name
        path.write_bytes(data)
        conn = store.connect()
        try:
            result = parsing.import_ticket(conn, path)
            # Same step the CLI does after importing: without it every line of a
            # freshly uploaded ticket would show as unmatched.
            matching.match_ticket_lines(conn, result["chain"])
            return result
        finally:
            conn.close()


async def _import_one(upload) -> dict:
    """Import one upload; a bad file becomes a result row, never a 500."""
    name = Path(upload.filename or "ticket.pdf").name
    if not _looks_like_pdf(name, upload.content_type):
        return {"filename": name, "ok": False, "message": f"«{name}» no es un PDF."}
    data = await upload.read()
    try:
        parsed = _import_bytes(data, name)
    except parsing.ParseError as exc:
        return {"filename": name, "ok": False, "message": str(exc)}
    except Exception as exc:  # a broken file must not take the page down
        return {
            "filename": name,
            "ok": False,
            "message": f"No se pudo leer «{name}»: {exc}",
        }
    return {
        "filename": name,
        "ok": True,
        "ticket_id": parsed["ticket_id"],
        "inserted": parsed["inserted"],
    }


def _render_nuevo(request, *, results=None, error=None, status_code=200):
    return templates.TemplateResponse(
        request,
        "nuevo.html",
        {
            "results": results or [],
            "error": error,
            "limit": upload_limit_label(),
        },
        status_code=status_code,
    )


def _imported_notice(raw):
    """Notice for ``?importados=N`` (only reached when more than one imported)."""
    if not raw or not raw.isdigit() or int(raw) < 1:
        return None
    count = int(raw)
    return "Se ha importado 1 ticket." if count == 1 else f"Se han importado {count} tickets."


# --------------------------------------------------------------------------- #
# Routes (``/tickets/nuevo`` before ``/tickets/{id}`` so it is not read as an id).
# --------------------------------------------------------------------------- #


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    month = selected_month(request.query_params.get("mes"))
    conn = store.connect()
    try:
        months = available_months(conn)
        tickets = ticket_rows(conn, month)
        summary = spend_summary(conn, month)
        changes = monthly_price_changes(conn, months)
        top = top_products(conn)
    finally:
        conn.close()
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "tickets": tickets,
            "summary": summary,
            "month": month,
            "month_label": month_label(month),
            "options": month_options(months),
            "has_any": bool(months),
            "changes": changes,
            "top": top,
            "notice": _imported_notice(request.query_params.get("importados")),
        },
    )


@app.get("/salud")
def health():
    """JSON status: the database path and whether it can be queried."""
    path = db_path()
    readable = False
    if path.exists():
        try:
            conn = store.connect(path)
            try:
                conn.execute("SELECT count(*) FROM tickets").fetchone()
                readable = True
            finally:
                conn.close()
        except Exception:
            readable = False
    return JSONResponse(
        {"db_path": str(path), "readable": readable},
        status_code=200 if readable else 503,
    )


@app.get("/tickets/nuevo", response_class=HTMLResponse)
def nuevo_page(request: Request):
    return _render_nuevo(request)


@app.post("/tickets/nuevo")
async def nuevo_import(request: Request):
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_UPLOAD_BYTES:
        return _render_nuevo(
            request,
            error=(
                f"El archivo supera el tamaño máximo ({upload_limit_label()}); "
                "no se ha importado nada."
            ),
            status_code=413,
        )

    form = await request.form()
    uploads = [
        item
        for item in form.getlist("files")
        if isinstance(item, UploadFile) and item.filename
    ]
    if not uploads:
        return _render_nuevo(
            request, error="Selecciona al menos un archivo PDF.", status_code=400
        )

    results = [await _import_one(upload) for upload in uploads]
    if any(not result["ok"] for result in results):
        return _render_nuevo(request, results=results)

    if len(results) == 1:
        target = f"/tickets/{results[0]['ticket_id']}"
        if not results[0]["inserted"]:
            target += "?ya-importado=1"
        return RedirectResponse(target, status_code=303)
    return RedirectResponse(f"/?importados={len(results)}", status_code=303)


@app.get("/imagenes/{chain}/{filename}")
def product_image(chain: str, filename: str):
    """Serve a downloaded product picture from the images root.

    Pictures are stored as ``<chain>/<external_id>.<ext>`` by
    :mod:`tickets.images`; nothing else is reachable here, and a name with a
    separator or a ``..`` is refused instead of being resolved.
    """
    from tickets import images as images_module

    if chain not in CHAIN_LOGOS or Path(filename).name != filename or ".." in filename:
        raise HTTPException(status_code=404)
    raiz = images_module.images_root(os.environ.get(store.DB_PATH_ENV))
    ruta = raiz / chain / filename
    if not ruta.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(ruta, headers={"Cache-Control": "public, max-age=86400"})


@app.get("/tickets/{ticket_id}", response_class=HTMLResponse)
def ticket_page(request: Request, ticket_id: int):
    conn = store.connect()
    try:
        detail = ticket_detail(conn, ticket_id)
    finally:
        conn.close()
    if detail is None:
        raise HTTPException(
            status_code=404, detail="No existe ninguna compra con ese identificador."
        )
    notice = None
    if request.query_params.get("ya-importado"):
        notice = "Este ticket ya estaba importado; no se ha vuelto a guardar."
    return templates.TemplateResponse(
        request, "ticket.html", {"ticket": detail, "notice": notice}
    )


@app.get("/productos/{product_id}", response_class=HTMLResponse)
def product_page(request: Request, product_id: int):
    """One catalogue product: its own data plus the other-chain comparison."""
    conn = store.connect()
    try:
        detail = product_detail(
            conn,
            product_id,
            buscar=request.query_params.get("buscar"),
            cadena=request.query_params.get("cadena"),
        )
    finally:
        conn.close()
    if detail is None:
        raise HTTPException(
            status_code=404,
            detail="No existe ningún producto con ese identificador.",
        )
    return templates.TemplateResponse(request, "producto.html", {"producto": detail})


def _product_exists(conn, product_id) -> bool:
    """True when the catalogue holds ``product_id``."""
    return (
        conn.execute("SELECT 1 FROM products WHERE id = ?", (product_id,)).fetchone()
        is not None
    )


def _form_id(value):
    """An ``int`` from a form field, or ``None`` when it is missing or not one."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _link_error(conn, product_id, candidate) -> str | None:
    """Why ``product_id`` cannot link ``candidate``, or ``None`` when it can.

    A missing candidate id, an unknown catalogue id and the product itself are
    all refused with a Spanish sentence the page shows instead of a traceback.
    """
    if candidate is None or not _product_exists(conn, candidate):
        return "Ese producto no existe en el catálogo; no se ha creado ningún vínculo."
    if candidate == product_id:
        return "Un producto no puede vincularse consigo mismo."
    return None


def _product_error_response(request, detail, error):
    """Re-render the product page with an error notice; the ids were invalid."""
    detail["error"] = error
    return templates.TemplateResponse(request, "producto.html", {"producto": detail})


@app.post("/productos/{product_id}/vincular")
async def link_product(request: Request, product_id: int):
    """Link two catalogue products by hand, then go back to the product page."""
    form = await request.form()
    candidate = _form_id(form.get("candidato"))
    conn = store.connect()
    try:
        detail = product_detail(conn, product_id)
        if detail is None:
            raise HTTPException(
                status_code=404,
                detail="No existe ningún producto con ese identificador.",
            )
        error = _link_error(conn, product_id, candidate)
        if error is None:
            store.link_products(conn, product_id, candidate)
    finally:
        conn.close()
    if error is not None:
        return _product_error_response(request, detail, error)
    return RedirectResponse(f"/productos/{product_id}", status_code=303)


@app.post("/productos/{product_id}/desvincular")
async def unlink_product(request: Request, product_id: int):
    """Remove a hand-made link between two products, then go back."""
    form = await request.form()
    linked = _form_id(form.get("vinculado"))
    conn = store.connect()
    try:
        detail = product_detail(conn, product_id)
        if detail is None:
            raise HTTPException(
                status_code=404,
                detail="No existe ningún producto con ese identificador.",
            )
        if linked is None or not _product_exists(conn, linked):
            error = "Ese vínculo no existe; no se ha quitado nada."
        else:
            store.unlink_products(conn, product_id, linked)
            error = None
    finally:
        conn.close()
    if error is not None:
        return _product_error_response(request, detail, error)
    return RedirectResponse(f"/productos/{product_id}", status_code=303)


# --------------------------------------------------------------------------- #
# Error pages: Spanish HTML, never a traceback or a bare JSON blob.
# --------------------------------------------------------------------------- #


@app.exception_handler(RequestTooLarge)
async def too_large_handler(request: Request, exc: RequestTooLarge):
    return _render_nuevo(
        request,
        error=(
            f"El archivo supera el tamaño máximo ({upload_limit_label()}); "
            "no se ha importado nada."
        ),
        status_code=413,
    )


@app.exception_handler(StarletteHTTPException)
async def not_found_handler(request: Request, exc: StarletteHTTPException):
    if exc.status_code == 404:
        message = (
            exc.detail
            if isinstance(exc.detail, str)
            else "No encontramos lo que buscabas."
        )
        return templates.TemplateResponse(
            request,
            "error.html",
            {"title": "Página no encontrada", "message": message},
            status_code=404,
        )
    return await http_exception_handler(request, exc)
