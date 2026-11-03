"""Build a synthetic demo database: three chains, three months, repeated products.

Nothing here comes from a real ticket: the numbers, the shop names and the
purchase dates are invented. It exists so the README's screenshots and anyone's
first run can show every screen (resumen, historial, ficha con histórico,
comparativa entre cadenas, oferta de Amazon) without publishing a single real
purchase.

    python3 scripts/demo.py            # writes report/demo.db
    python3 scripts/demo.py --db /tmp/x.db
"""

from __future__ import annotations

import argparse
import pathlib
import sys

RUTA = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RUTA))

from tickets import store  # noqa: E402

#: Catalogue rows: (chain, external_id, name, format, category, [(observed_at, price, unit_price)]).
#: Two or three observations per product are what the price-change lists read.
CATALOGO = [
    ("Mercadona", "M-100", "Leche entera Hacendado 1 l", "Brick 1 l", "Leche", [("2026-08-01", 0.95, 0.95), ("2026-10-01", 1.05, 1.05)]),
    ("Mercadona", "M-101", "Aceite de oliva virgen extra Hacendado 1 l", "Botella 1 l", "Aceites", [("2026-08-01", 8.50, 8.50), ("2026-10-01", 9.90, 9.90)]),
    ("Mercadona", "M-102", "Arroz redondo Hacendado 1 kg", "Paquete 1 kg", "Arroz", [("2026-08-01", 1.35, 1.35)]),
    ("Mercadona", "M-103", "Bebida de soja 0% azúcar Hacendado 1 l", "Brick 1 l", "Bebidas vegetales", [("2026-08-01", 0.75, 0.75), ("2026-10-01", 0.79, 0.79)]),
    ("Dia", "D-200", "Leche entera Dia Láctea 1 l", "Brick 1 l", "Leche", [("2026-08-01", 0.92, 0.92), ("2026-10-01", 0.99, 0.99)]),
    ("Dia", "D-201", "Aceite de oliva virgen extra Dia 1 l", "Botella 1 l", "Aceites", [("2026-08-01", 8.20, 8.20), ("2026-10-01", 9.60, 9.60)]),
    ("Dia", "D-202", "Arroz redondo Dia 1 kg", "Paquete 1 kg", "Arroz", [("2026-08-01", 1.25, 1.25)]),
    ("MAS", "S-300", "Leche entera Covap 1 l", "Brick 1 l", "Leche", [("2026-08-01", 1.09, 1.09), ("2026-10-01", 1.15, 1.15)]),
    ("MAS", "S-301", "Aceite oliva 0,4º Garrafal 1 l", "Botella 1 l", "Aceites", [("2026-08-01", 9.40, 9.40)]),
    ("MAS", "S-302", "Arroz redondo SOS 1 kg", "Paquete 1 kg", "Arroz", [("2026-08-01", 1.45, 1.45)]),
]

#: Tickets and their lines, oldest first. A line is
#: (raw_text, catalogue_external_id or None, qty, price_paid, total_paid, purchase_unit).
TICKETS = [
    ("MAS", "S-2026-08-12", "2026-08-12T11:22:15", [
        ("LECHE ENTERA COVAP 1L", "S-300", 1, 1.09, 1.09, "ud"),
        ("ARROZ REDONDO SOS 1KG", "S-302", 1, 1.45, 1.45, "ud"),
        ("TOMATE CHER BJA 500G", None, 1, 2.70, 2.70, "ud"),
    ]),
    ("Dia", "D-2026-09-08", "2026-09-08T18:40:00", [
        ("LECHE ENTERA DIA LACTEA 1L", "D-200", 2, 0.92, 1.84, "ud"),
        ("ACEITE OLIVA VIRGEN EXTRA DIA 1L", "D-201", 1, 8.20, 8.20, "ud"),
        ("PLATANO DE CANARIAS", None, 1.240, 1.75, 2.17, "kg"),
    ]),
    ("Mercadona", "M-2026-09-29", "2026-09-29T19:20:00", [
        ("ACEITE OLIVA 0,4 HACENDADO GARRAFA 1L", "M-101", 1, 8.50, 8.50, "ud"),
        ("LECHE ENTERA HACENDADO 1L", "M-100", 6, 0.95, 5.70, "ud"),
        ("BEBIDA SOJA S/AZ HACENDADO 1L", "M-103", 4, 0.75, 3.00, "ud"),
    ]),
    ("Mercadona", "M-2026-10-03", "2026-10-03T12:31:00", [
        ("ACEITE OLIVA 0,4 HACENDADO GARRAFA 1L", "M-101", 1, 9.90, 9.90, "ud"),
        ("ARROZ REDONDO HACENDADO 1KG", "M-102", 2, 1.35, 2.70, "ud"),
        ("LECHE ENTERA HACENDADO 1L", "M-100", 6, 1.05, 6.30, "ud"),
        ("MELON PIEL DE SAPO", None, 1.527, 1.65, 2.52, "kg"),
    ]),
    ("MAS", "S-2026-10-12", "2026-10-12T11:22:15", [
        ("LECHE ENTERA COVAP 1L", "S-300", 4, 1.15, 4.60, "ud"),
        ("ROMANILLAS PIEZA", None, 2, 1.10, 2.20, "ud"),
        ("SURTIDO YOGUR 8 SABO", None, 1, 2.50, 2.50, "ud"),
    ]),
    ("Dia", "D-2026-10-20", "2026-10-20T13:43:00", [
        ("ACEITE OLIVA VIRGEN EXTRA DIA 1L", "D-201", 1, 9.60, 9.60, "ud"),
        ("LECHE ENTERA DIA LACTEA 1L", "D-200", 3, 0.99, 2.97, "ud"),
        ("MERLUZA EMPANADA", None, 1, 4.59, 4.59, "ud"),
    ]),
]

#: One Amazon offer, so the product page shows that block as well.
OFERTA = {
    "asin": "B0DEMO0001",
    "title": "Aceite de oliva virgen extra Hacendado, botella de 1 l (pack de 2)",
    "price": 19.40,
    "unit_price": 9.70,
    "unit_label": "l",
    "url": "https://www.amazon.es/dp/B0DEMO0001",
    "sponsored": False,
    "observed_at": "2026-10-21T00:00:00Z",
}


def build(path):
    path = pathlib.Path(path)
    if path.exists():
        path.unlink()
    conn = store.init_db(path)
    try:
        ids = {}
        for chain, external_id, name, size, category, observations in CATALOGO:
            product_id = store.upsert_product(
                conn,
                {
                    "chain": chain,
                    "external_id": external_id,
                    "name": name,
                    "format": size,
                    "price": observations[-1][1],
                    "unit_price": observations[-1][2],
                    "unit_label": "l" if size.endswith("l") else "kg",
                    "category": category,
                    "observed_at": f"{observations[-1][0]}T00:00:00Z",
                },
            )
            for observed_at, price, unit_price in observations:
                store.record_price(conn, product_id, price, unit_price, f"{observed_at}T00:00:00Z")
            ids[external_id] = product_id

        for chain, number, purchased_at, lines in TICKETS:
            rows = [
                {
                    "raw_text": raw_text,
                    "product_id": ids.get(external_id),
                    "qty": qty,
                    "price_paid": price_paid,
                    "total_paid": total_paid,
                    "purchase_unit": unit,
                    "match_confidence": 100.0 if external_id else None,
                    "matched_by": "alias" if external_id else None,
                }
                for raw_text, external_id, qty, price_paid, total_paid, unit in lines
            ]
            store.upsert_ticket(
                conn,
                {
                    "chain": chain,
                    "ticket_number": number,
                    "purchased_at": purchased_at,
                    "total": round(sum(line["total_paid"] for line in rows), 2),
                    "source_pdf": f"{number}.pdf",
                    "imported_at": "2026-10-21T00:00:00Z",
                    "lines": rows,
                },
            )

        store.save_offers(conn, ids["M-101"], [OFERTA], "aceite oliva virgen extra 1 l")
        conn.commit()
    finally:
        conn.close()
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description="Crea una base de datos de demostración, con datos inventados.")
    parser.add_argument("--db", default=str(RUTA / "report" / "demo.db"), help="Fichero SQLite de salida.")
    args = parser.parse_args(argv)
    ruta = build(args.db)
    print(f"Base de demostración: {ruta}")
    print("  tickets: 6 · cadenas: 3 · meses: 2026-08 a 2026-10 · ninguna compra real")


if __name__ == "__main__":
    main()
