"""Static, self-contained HTML purchase report.

Reads the SQLite store (``tickets``, ``ticket_lines``, ``products``) and writes
a single HTML file with inline CSS and no external asset, so it opens in any
browser with no network. All user-visible text is neutral Spanish and every
value coming from the database is HTML-escaped because product names are
written by third parties.

Sections, in order: summary, spend per chain, spend per month, spend per
category, what I paid over time for repeated products, and a cross-chain
comparison of comparable catalogue products against what I actually paid.
The comparison never invents a match: a chain without a comparable product is
rendered as an em dash.
"""

from __future__ import annotations

import html
from collections import defaultdict
from pathlib import Path

from tickets import matching, store, units

MONTH_NAMES = (
    "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
)
UNCLASSIFIED = "Sin clasificar"
DASH = "—"

STYLE = """
:root { color-scheme: light dark; }
body { font-family: system-ui, sans-serif; margin: 2rem auto; max-width: 64rem;
       padding: 0 1rem; line-height: 1.45; }
h1 { font-size: 1.6rem; }
h2 { font-size: 1.2rem; margin-top: 2rem; border-bottom: 1px solid #8884;
     padding-bottom: .3rem; }
table { border-collapse: collapse; width: 100%; margin-top: .75rem; }
th, td { text-align: left; padding: .35rem .5rem; border-bottom: 1px solid #8883; }
th { background: #8881; }
td.num, th.num { text-align: right; white-space: nowrap; }
svg.bar rect { fill: #4a7; }
.note { color: #777; font-size: .9rem; }
"""


def _esc(value) -> str:
    """HTML-escape any database value, attributes included."""
    return html.escape(str(value), quote=True)


def _number(value, decimals=2) -> str:
    """Format a number the Spanish way (``1234.5`` -> ``1.234,50``)."""
    text = f"{float(value):,.{decimals}f}"
    return text.replace(",", "\x00").replace(".", ",").replace("\x00", ".")


def _money(value) -> str:
    return DASH if value is None else f"{_number(value)} €"


def _unit_money(value, label) -> str:
    if value is None:
        return DASH
    return f"{_number(value)} €/{label}" if label else _money(value)


def _signed_unit(value, label) -> str:
    sign = "+" if value >= 0 else "-"
    return f"{sign}{_unit_money(abs(value), label)}"


def _signed_pct(value) -> str:
    sign = "+" if value >= 0 else "-"
    return f"{sign}{_number(abs(value))} %"


def _spanish_date(iso) -> str:
    """``2026-01-05T10:00:00Z`` -> ``05/01/2026`` (never guesses)."""
    text = str(iso or "")
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        return f"{text[8:10]}/{text[5:7]}/{text[0:4]}"
    return text or DASH


def _month_label(month_key) -> str:
    """``2026-02`` -> ``febrero 2026``; anything else is returned as is."""
    parts = str(month_key).split("-")
    if len(parts) >= 2 and parts[1].isdigit() and 1 <= int(parts[1]) <= 12:
        return f"{MONTH_NAMES[int(parts[1]) - 1]} {parts[0]}"
    return str(month_key)


def _line_amount(line) -> float:
    """What the line cost, preferring the line total over the unit price."""
    if line["total_paid"] is not None:
        return float(line["total_paid"])
    if line["price_paid"] is not None:
        return float(line["price_paid"]) * float(line["qty"] or 1)
    return 0.0


def _paid_unit_price(line):
    """What I paid per base unit, preferring the ticket's own unit price.

    A parsed line carries ``purchase_unit`` (``kg``, ``l`` or ``ud``) and
    ``price_paid`` is already the price for that unit, so it is returned as is.
    Older rows have no ``purchase_unit``: for those the size is read from the
    product name in the raw text and the line amount is divided by it. When
    neither works the result is ``(None, None)`` rather than a guess.
    """
    purchase_unit = line["purchase_unit"] if "purchase_unit" in line.keys() else None
    if purchase_unit and line["price_paid"] is not None:
        return float(line["price_paid"]), purchase_unit

    amount = line["total_paid"]
    if amount is None:
        amount = line["price_paid"]
    if amount is None:
        return None, None
    size, label = units.extract_size(line["raw_text"])
    if not size:
        return None, None
    return float(amount) / size, label


def _table(headers, rows, empty_note="No hay datos."):
    """One table; ``rows`` are pre-rendered ``<tr>`` strings."""
    head = "".join(f"<th>{header}</th>" for header in headers)
    if not rows:
        return (
            f'<table><thead><tr>{head}</tr></thead></table>'
            f'<p class="note">{_esc(empty_note)}</p>'
        )
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(rows)}</tbody></table>"


def _collect(conn):
    tickets = conn.execute(
        "SELECT id, chain, purchased_at, total FROM tickets ORDER BY purchased_at, id"
    ).fetchall()
    lines = conn.execute(
        "SELECT tl.id AS line_id, tl.raw_text, tl.product_id, tl.qty, tl.price_paid, "
        "tl.total_paid, tl.purchase_unit, t.chain AS chain, t.purchased_at AS purchased_at, "
        "p.name AS product_name, p.category AS category "
        "FROM ticket_lines tl "
        "JOIN tickets t ON t.id = tl.ticket_id "
        "LEFT JOIN products p ON p.id = tl.product_id "
        "ORDER BY t.purchased_at, tl.id"
    ).fetchall()
    product_chains = [
        row["chain"]
        for row in conn.execute("SELECT DISTINCT chain FROM products ORDER BY chain")
    ]
    return tickets, lines, product_chains


def _section_summary(tickets, lines, matched_lines, total_spent):
    if tickets:
        first = _spanish_date(tickets[0]["purchased_at"])
        last = _spanish_date(tickets[-1]["purchased_at"])
        period = first if first == last else f"{first} – {last}"
    else:
        period = DASH

    rows = [
        ("Tickets", str(len(tickets))),
        ("Periodo cubierto", period),
        ("Gasto total", _money(total_spent)),
        ("Líneas de ticket", str(len(lines))),
        ("Líneas emparejadas", str(matched_lines)),
        ("Líneas sin emparejar", str(len(lines) - matched_lines)),
    ]
    body = "".join(
        f'<tr><th scope="row">{_esc(label)}</th><td class="num">{_esc(value)}</td></tr>'
        for label, value in rows
    )
    return f"<h2>Resumen</h2><table><tbody>{body}</tbody></table>"


def _section_chains(tickets, total_spent):
    stats = {}
    for ticket in tickets:
        entry = stats.setdefault(ticket["chain"], {"tickets": 0, "total": 0.0})
        entry["tickets"] += 1
        entry["total"] += float(ticket["total"] or 0)

    rows = []
    for chain in sorted(stats):
        entry = stats[chain]
        share = entry["total"] / total_spent * 100 if total_spent else 0.0
        rows.append(
            f"<tr><td>{_esc(chain)}</td>"
            f'<td class="num">{entry["tickets"]}</td>'
            f'<td class="num">{_money(entry["total"])}</td>'
            f'<td class="num">{_number(share, 1)} %</td></tr>'
        )
    return (
        "<h2>Gasto por cadena</h2>"
        + _table(("Cadena", "Tickets", "Gasto", "Cuota"), rows)
    )


def _section_months(tickets):
    totals = defaultdict(float)
    for ticket in tickets:
        totals[str(ticket["purchased_at"])[:7]] += float(ticket["total"] or 0)

    peak = max(totals.values()) if totals else 0.0
    rows = []
    for month in sorted(totals):
        width = round(200 * totals[month] / peak) if peak else 0
        bar = (
            f'<svg class="bar" width="200" height="14" role="img" '
            f'aria-label="{_esc(_money(totals[month]))}">'
            f'<rect x="0" y="0" width="{width}" height="14" rx="2"></rect></svg>'
        )
        rows.append(
            f"<tr><td>{_esc(_month_label(month))}</td>"
            f'<td class="num">{_money(totals[month])}</td><td>{bar}</td></tr>'
        )
    return (
        "<h2>Gasto por mes</h2>"
        + _table(("Mes", "Gasto", "Reparto"), rows)
    )


def _section_categories(lines):
    totals = defaultdict(float)
    for line in lines:
        if line["product_id"] is None:
            key = UNCLASSIFIED
        else:
            key = line["category"] or UNCLASSIFIED
        totals[key] += _line_amount(line)

    overall = sum(totals.values())
    rows = []
    for category, total in sorted(totals.items(), key=lambda item: (-item[1], item[0])):
        share = total / overall * 100 if overall else 0.0
        rows.append(
            f"<tr><td>{_esc(category)}</td>"
            f'<td class="num">{_money(total)}</td>'
            f'<td class="num">{_number(share, 1)} %</td></tr>'
        )
    return (
        "<h2>Gasto por categoría</h2>"
        "<p class='note'>La categoría sale del producto de catálogo emparejado; "
        "las líneas sin emparejar se agrupan como «Sin clasificar».</p>"
        + _table(("Categoría", "Gasto", "Cuota"), rows)
    )


def _section_evolution(lines):
    purchases = defaultdict(list)
    for line in lines:
        if line["product_id"] is not None:
            purchases[line["product_id"]].append(line)

    repeated = {pid: rows for pid, rows in purchases.items() if len(rows) >= 2}
    rendered = []
    for pid in sorted(repeated, key=lambda key: repeated[key][0]["product_name"] or ""):
        history = sorted(repeated[pid], key=lambda row: (row["purchased_at"], row["line_id"]))
        amounts = [_line_amount(row) for row in history]
        first, last = amounts[0], amounts[-1]
        change = (last - first) / first * 100 if first else None
        sequence = ", ".join(
            f"{_spanish_date(row['purchased_at'])}: {_money(amount)}"
            for row, amount in zip(history, amounts)
        )
        rendered.append(
            f"<tr><td>{_esc(history[0]['product_name'])}</td>"
            f'<td class="num">{len(history)}</td>'
            f'<td class="num">{_money(first)}</td>'
            f'<td class="num">{_money(last)}</td>'
            f'<td class="num">{_signed_pct(change) if change is not None else DASH}</td>'
            f"<td>{_esc(sequence)}</td></tr>"
        )
    return (
        "<h2>Evolución de lo que pago</h2>"
        "<p class='note'>Importes que pagué en el ticket, no el precio de catálogo. "
        "Solo productos comprados dos o más veces, del más antiguo al más reciente.</p>"
        + _table(("Producto", "Compras", "Primera", "Última", "Variación", "Histórico"), rendered)
    )


def _section_comparison(conn, lines, product_chains):
    matched = [line for line in lines if line["product_id"] is not None]
    cache = {}
    rows = []
    for line in matched:
        paid_unit, paid_label = _paid_unit_price(line)
        for chain in product_chains:
            if chain == line["chain"]:
                continue
            # Keyed by the matched product, not just its name: two products that
            # share a name can carry different manual links.
            key = (chain, line["product_id"])
            if key not in cache:
                cache[key] = _comparable(conn, chain, line["product_name"], line["product_id"])
            comparable = cache[key]
            name = price = per_unit = delta = DASH
            if comparable is not None:
                name = _esc(comparable["name"])
                price = _money(comparable["price"])
                per_unit = _unit_money(comparable["unit_price"], comparable["unit_label"])
                if (
                    paid_unit is not None
                    and comparable["unit_price"] is not None
                    and paid_label
                    and comparable["unit_label"] == paid_label
                ):
                    delta = _signed_unit(paid_unit - comparable["unit_price"], paid_label)
            rows.append(
                f'<tr data-chain="{_esc(chain)}">'
                f"<td>{_esc(line['product_name'])}</td>"
                f'<td class="num">{_unit_money(paid_unit, paid_label)}</td>'
                f"<td>{_esc(chain)}</td>"
                f"<td>{name}</td>"
                f'<td class="num">{price}</td>'
                f'<td class="num">{per_unit}</td>'
                f'<td class="num">{delta}</td></tr>'
            )
    return (
        "<h2>Comparativa con otras cadenas</h2>"
        "<p class='note'>Para cada línea emparejada, el producto comparable más "
        "parecido del catálogo de cada otra cadena y la diferencia por unidad "
        "frente a lo que pagué. Los precios de las otras cadenas son de catálogo, "
        "no lo que pagué. «—» significa que no hay producto comparable.</p>"
        + _table(
            ("Producto", "Pagué", "Cadena", "Comparable", "Precio", "€/unidad", "Diferencia"),
            rows,
            empty_note="No hay líneas emparejadas ni otras cadenas en el catálogo.",
        )
    )


def _manually_linked(conn, product_id, chain):
    """``{"product_id": id}`` of the product linked by hand to ``product_id`` in ``chain``.

    A hand-made link wins over the fuzzy match, exactly as
    :func:`tickets.equivalents.same_product_in_other_chains` decides for the
    product page, so the report and the page never disagree.
    """
    for linked_id in store.linked_products(conn, product_id):
        row = conn.execute(
            "SELECT id FROM products WHERE id = ? AND chain = ?", (linked_id, chain)
        ).fetchone()
        if row is not None:
            return {"product_id": row["id"]}
    return None


def _comparable(conn, chain, name, product_id=None):
    """Best catalogue match for ``name`` in ``chain``, with its stored price.

    A manual link of ``product_id`` in ``chain`` wins over the fuzzy match,
    following :func:`tickets.equivalents.same_product_in_other_chains`. Returns
    ``None`` when there is nothing: this never invents a comparable.
    """
    result = _manually_linked(conn, product_id, chain) if product_id is not None else None
    if result is None:
        result = matching.match_line(conn, chain, name)
    if result is None:
        return None
    row = conn.execute(
        "SELECT name, price, unit_price, unit_label FROM products WHERE id = ?",
        (result["product_id"],),
    ).fetchone()
    if row is None:
        return None
    return {
        "name": row["name"],
        "price": row["price"],
        "unit_price": row["unit_price"],
        "unit_label": row["unit_label"],
    }


def build_report(conn, output_path, title="Informe de compras") -> dict:
    """Write the self-contained HTML report and return a summary dict.

    The summary carries ``tickets``, ``lines``, ``matched_lines``,
    ``unmatched_lines``, ``chains`` and ``total_spent``. The same store always
    produces byte-identical output: no timestamp is written into the body.
    """
    tickets, lines, product_chains = _collect(conn)
    matched_lines = sum(1 for line in lines if line["product_id"] is not None)
    total_spent = sum(float(ticket["total"] or 0) for ticket in tickets)

    sections = "".join(
        (
            _section_summary(tickets, lines, matched_lines, total_spent),
            _section_chains(tickets, total_spent),
            _section_months(tickets),
            _section_categories(lines),
            _section_evolution(lines),
            _section_comparison(conn, lines, product_chains),
        )
    )
    document = (
        "<!DOCTYPE html>\n"
        '<html lang="es">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{_esc(title)}</title>\n"
        f"<style>{STYLE}</style>\n"
        "</head>\n<body>\n"
        f"<h1>{_esc(title)}</h1>\n"
        f"{sections}\n"
        "</body>\n</html>\n"
    )
    path = Path(output_path)
    if path.parent and not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(document, encoding="utf-8")

    return {
        "tickets": len(tickets),
        "lines": len(lines),
        "matched_lines": matched_lines,
        "unmatched_lines": len(lines) - matched_lines,
        "chains": len({ticket["chain"] for ticket in tickets}),
        "total_spent": round(total_spent, 2),
    }
