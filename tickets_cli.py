"""Command line entry point: chain catalogues and ticket PDFs in, report out.

Usage::

    python tickets_cli.py precios --cadena todas
    python tickets_cli.py tickets --inbox samples/inbox
    python tickets_cli.py amazon --limite 20
    python tickets_cli.py informe --salida report/informe.html
    python tickets_cli.py estado
    python tickets_cli.py actualizar

``actualizar`` is the cron entry point: every chain plus the report. On a
machine without a desktop every command that opens a browser (Día and Amazon)
must run under a display, i.e. ``xvfb-run -a python tickets_cli.py actualizar``.

``tickets`` imports the ticket PDFs found in an inbox directory and then links
what it imported to the catalogue. It reads the Supermercados MAS text layer and
Día tickets, which are pictures (a phone screenshot exported as a PDF) and go
through OCR first: that needs ``tesseract-ocr`` and ``poppler-utils`` installed.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from reports import report
from supermarket import amazon, dia, dia_account, firefox, mas, mas_account, mercadona
from tickets import images, matching, parsing, store

CHAIN_CHOICES = ("mercadona", "dia", "mas")
ALL_CHAINS = ("mercadona", "dia", "mas")
CHAIN_LABELS = {"mercadona": "Mercadona", "dia": "Día", "mas": "MAS"}

DEFAULT_REPORT_PATH = os.path.join("report", "informe.html")
DEFAULT_INBOX = os.path.join("samples", "inbox")
TICKETS_INBOX_ENV = "TICKETS_INBOX"

DISPLAY_HINT = (
    "Día necesita un navegador con ventana: en un servidor ejecuta el comando "
    "bajo 'xvfb-run -a'."
)

AMAZON_HINT = (
    "Amazon se abre con un navegador real: en un servidor ejecuta el comando "
    "bajo 'xvfb-run -a'."
)


def _default_db():
    """``$DB_PATH`` when set, else the store's default file."""
    return os.environ.get(store.DB_PATH_ENV, store.DEFAULT_DB_PATH)


def _default_inbox():
    """``$TICKETS_INBOX`` when set, else ``samples/inbox``."""
    return os.environ.get(TICKETS_INBOX_ENV, DEFAULT_INBOX)


def _category_ids(value):
    """Parse ``"L2033,L2007"`` into ``["L2033", "L2007"]``, or ``None``."""
    if not value:
        return None
    return [item.strip() for item in value.split(",") if item.strip()]


def _needs_display(message):
    text = str(message).lower()
    return "chromium" in text or "xvfb" in text


def _run_dia(conn, args):
    """Run the Día ingest with a single browser for the whole walk.

    ``ExitStack`` keeps that one browser alive until the walk ends, while a test
    can replace ``dia.ingest_dia`` with a fake that never needs a window.
    """
    with contextlib.ExitStack() as stack:
        opened = []

        def fetch_json(path):
            # One Chromium for the whole walk: launching it per request would cost
            # seconds of startup on every page.
            if not opened:
                opened.append(stack.enter_context(dia.DiaBrowser()))
            return opened[0].fetch_json(path)

        return dia.ingest_dia(
            conn,
            fetch_json,
            category_ids=_category_ids(args.categorias),
            max_pages=args.max_peticiones,
            sleep_seconds=args.pausa,
        )

def _ingest_chain(conn, chain, args) -> bool:
    """Ingest one chain, print its summary line; ``False`` when it failed."""
    label = CHAIN_LABELS[chain]
    try:
        if chain == "mercadona":
            counts = mercadona.ingest_mercadona(conn, sleep_seconds=args.pausa)
            print(
                f"{label}: {counts['products']} productos, "
                f"{counts['categories']} categorías, {counts['leaves']} hojas"
            )
        elif chain == "dia":
            counts = _run_dia(conn, args)
            print(
                f"{label}: {counts['products']} productos, "
                f"{counts['categories']} categorías, {counts['pages']} páginas"
                + (
                    f", {counts['failed_pages']} páginas con error"
                    if counts.get("failed_pages")
                    else ""
                )
            )
        else:
            counts = mas.ingest_mas(
                conn, sleep_seconds=args.pausa, max_requests=args.max_peticiones
            )
            line = (
                f"{label}: {counts['products']} productos, "
                f"{counts['categories']} categorías, {counts['requests']} peticiones"
            )
            if counts.get("stopped"):
                line += " (incompleto: se alcanzó el límite de peticiones)"
            print(line)
        return True
    except dia.DiaError as exc:
        print(f"{label}: error: {exc}", file=sys.stderr)
        if _needs_display(exc):
            print(f"  {DISPLAY_HINT}", file=sys.stderr)
        return False
    except Exception as exc:  # one broken chain must not stop the others
        print(f"{label}: error: {exc}", file=sys.stderr)
        return False


def _amazon_price(value, unit=None) -> str:
    """``(1.86, 'l')`` -> ``1,86 €/l``; a missing value stays a dash."""
    if value is None:
        return "—"
    text = ("%g" % float(value)).replace(".", ",")
    if unit in ("kg", "l"):
        return f"{text} €/{unit}"
    return f"{text} €"


def _amazon_line(product, offers, best) -> str:
    """One Spanish line for a searched product and its best stored offer."""
    if best is None or best["unit_price"] is None:
        return f"{product['name']}: {len(offers)} ofertas, ninguna con precio por unidad"
    flag = " [Patrocinado]" if best["sponsored"] else ""
    printed = best["unit_price_text"] or "—"
    return (
        f"{product['name']}: {len(offers)} ofertas, mejor "
        f"{_amazon_price(best['unit_price'], best['unit_label'])} (Amazon: {printed}){flag}"
    )


def _amazon_products(conn, args):
    """The product Rows to search, or ``None`` when ``--producto`` is unknown."""
    if args.producto is not None:
        row = conn.execute(
            "SELECT * FROM products WHERE id = ?", (int(args.producto),)
        ).fetchone()
        if row is None:
            print(f"No existe el producto {args.producto} en la base de datos.", file=sys.stderr)
            return None
        return [row]

    candidates = store.amazon_candidates(conn)
    if args.cadena != "todas":
        label = CHAIN_LABELS[args.cadena]
        candidates = [row for row in candidates if row["chain"] == label]
    if args.limite is not None:
        candidates = candidates[: args.limite]
    return candidates


def _amazon_run(conn, products) -> int:
    """Search every product in **one** browser, save the offers and print the outcome."""
    counts = {"searched": 0, "offers": 0, "with_best": 0, "failed": 0}
    with amazon.open_session() as session:
        for product in products:
            try:
                offers = amazon.search(product["name"], session.fetch_page)
            except amazon.AmazonError as exc:
                counts["failed"] += 1
                print(f"{product['name']}: error: {exc}", file=sys.stderr)
                continue
            counts["searched"] += 1
            counts["offers"] += store.save_offers(conn, product["id"], offers, product["name"])
            best = store.best_offer(conn, product["id"])
            if best is not None and best["unit_price"] is not None:
                counts["with_best"] += 1
            print(_amazon_line(product, offers, best))

    summary = (
        f"Amazon: {counts['searched']} productos buscados, "
        f"{counts['offers']} ofertas guardadas, {counts['with_best']} con precio por unidad"
    )
    if counts["failed"]:
        summary += f", {counts['failed']} con error"
    print(summary)
    return 1 if counts["failed"] else 0


def _amazon_buscar(args) -> int:
    """A one-off search for ``--buscar``: printed to the screen, never stored."""
    with amazon.open_session() as session:
        offers = amazon.search(args.buscar, session.fetch_page)
    print(f'Amazon "{args.buscar}": {len(offers)} ofertas')
    for offer in offers:
        flag = " [Patrocinado]" if offer["sponsored"] else ""
        printed = offer["unit_price_text"] or "sin precio por unidad"
        print(
            f"  {_amazon_price(offer['price'])} · {printed} · "
            f"{offer['asin']}{flag} · {offer['title'][:70]}"
        )
    return 0


def _cmd_amazon(args) -> int:
    """Search Amazon.es for the products where buying there could pay off.

    One browser for the whole run. A search that cannot be read is reported with a
    Spanish message and exits 1: it never looks like a search that found nothing.
    """
    conn = store.init_db(args.db)
    try:
        if args.buscar:
            products = None
        else:
            products = _amazon_products(conn, args)
            if products is None:
                return 1
            if not products:
                print("No hay productos que buscar en Amazon en esta base de datos.")
                return 0
        try:
            return _amazon_buscar(args) if args.buscar else _amazon_run(conn, products)
        except amazon.AmazonError as exc:
            print(f"Amazon: error: {exc}", file=sys.stderr)
            return 1
        except Exception as exc:  # a missing browser lands here
            print(f"Amazon: error: {exc}", file=sys.stderr)
            if _needs_display(exc):
                print(f"  {DISPLAY_HINT}", file=sys.stderr)
            return 1
    finally:
        conn.close()


def _cmd_tickets(args) -> int:
    """Import every ``*.pdf`` in the inbox; print one Spanish line per file."""
    inbox = Path(args.inbox)
    if not inbox.is_dir():
        print(f"No existe la carpeta de entrada: {inbox}", file=sys.stderr)
        return 1
    paths = sorted(inbox.glob("*.pdf"))
    descarga = bool(
        getattr(args, "descargar_mas", False) or getattr(args, "descargar_dia", False)
    )
    if not paths and not descarga:
        print(f"No hay ficheros PDF en {inbox}")
        return 0

    conn = store.init_db(args.db)
    failed = False
    chains = []
    try:
        if getattr(args, "descargar_mas", False):
            failed = not _download_mas(conn, inbox) or failed
        if getattr(args, "descargar_dia", False):
            failed = not _download_dia(
                conn,
                con_imagenes=getattr(args, "imagenes", False),
                solo_comprados=getattr(args, "solo_comprados", False),
                raiz=images.images_root(getattr(args, "db", None)),
            ) or failed
        for path in paths:
            try:
                result = parsing.import_ticket(conn, path)
            except parsing.ParseError as exc:
                print(f"{path.name}: error: {exc}", file=sys.stderr)
                failed = True
                continue
            total = result["total"]
            total_text = f"{total:.2f}".replace(".", ",") if total is not None else "?"
            chains.append(result["chain"])
            if result["inserted"]:
                print(
                    f"{path.name}: {result['chain']}, ticket {result['ticket_number']}, "
                    f"{len(result['lines'])} líneas, total {total_text} €"
                )
                if result["mismatch"] or result["unverified"] or result["skipped"]:
                    print(
                        f"  aviso: {result['unverified']} líneas sin verificar y "
                        f"{result['skipped']} descartadas"
                        + (
                            "; la suma de líneas no cuadra con el total impreso"
                            if result["mismatch"]
                            else ""
                        )
                    )
            else:
                print(
                    f"{path.name}: ya importado ({result['chain']}, "
                    f"ticket {result['ticket_number']})"
                )
        _match_chains(conn, sorted(set(chains)))
    finally:
        conn.close()
    return 1 if failed else 0


def _cmd_imagenes(args) -> int:
    """Download the product pictures that are missing, per chain.

    Mercadona serves them over plain HTTP; Dia's image host is behind the same
    Akamai wall as the rest of the site, so its pictures are fetched from inside a
    real Chromium window (hence ``xvfb-run -a`` on a machine with no desktop).
    """
    # The option is lowercase (mercadona/dia/mas) and the catalogue stores the
    # display name (Mercadona/Dia/MAS): without this mapping the filter matches
    # nothing and the pass silently does nothing.
    cadenas = None if args.cadena == "todas" else [CHAIN_LABELS[args.cadena]]
    raiz = args.imagenes or images.images_root(args.db)
    conn = store.init_db(args.db)
    fallo = False
    try:
        fetchers = {"Mercadona": images.http_fetcher(), "MAS": images.http_fetcher()}
        quiere_dia = cadenas is None or "Dia" in cadenas
        error_dia = None
        if quiere_dia:
            try:
                with dia_account.browser_session() as sesion:
                    fetchers["Dia"] = sesion.fetch_bytes
                    counts = images.download_images(
                        conn, raiz, fetchers, chains=cadenas,
                        only_bought=args.solo_comprados, limit=args.limite, redo=args.rehacer,
                    )
            except dia_account.DiaAccountError as exc:
                print(f"Día (imágenes): error: {exc}", file=sys.stderr)
                fallo = True
                error_dia = exc
        if not quiere_dia or error_dia is not None:
            # Sin Día: el resto de cadenas, con una lista explícita (una lista
            # vacía significa "todas" y volvería a incluir Día).
            resto = [c for c in (cadenas or list(CHAIN_LABELS)) if c != "Dia"]
            counts = images.download_images(
                conn, raiz, fetchers, chains=resto,
                only_bought=args.solo_comprados, limit=args.limite, redo=args.rehacer,
            )
    finally:
        conn.close()

    megas = counts["bytes"] / 1024 / 1024
    print(
        f"Imágenes: {counts['downloaded']} descargadas de {counts['pending']} pendientes "
        f"({megas:.1f} MB), {counts['failed']} con error"
        + (f", {counts['no_fetcher']} sin descargador para su cadena" if counts["no_fetcher"] else "")
    )
    print(f"Carpeta: {raiz}")
    return 1 if (fallo or counts["failed"]) else 0


def _cmd_sesiones(args) -> int:
    """Report whether each shop's session is available in the Firefox profile.

    Read-only and cheap: it only opens the cookie database. The account downloads
    need these sessions, and a session expires eventually, so this says which one
    has to be renewed instead of failing halfway through a download.
    """
    cuentas = (
        ("Supermercados MAS", mas_account.TICKET_HOST),
        ("Día", dia_account.HOST_SUFFIX),
    )
    todas = True
    for nombre, sufijo in cuentas:
        try:
            cookies = firefox.firefox_cookies(sufijo)
        except firefox.FirefoxSessionError as exc:
            print(f"{nombre}: sin sesión — {exc}", file=sys.stderr)
            todas = False
        else:
            print(f"{nombre}: sesión disponible en Firefox ({len(cookies)} cookies)")

    if getattr(args, "navegador", False):
        # Que las cookies existan no significa que la sesión valga: Día puede
        # entrar en un bucle de redirecciones con la cookie caducada. Esto lo
        # comprueba de verdad, a cambio de abrir el navegador.
        print("\nComprobación real contra las tiendas:")
        try:
            vistas = mas_account.ticket_urls(mas_account.session_from_firefox())
            print(f"  Supermercados MAS: {len(vistas)} tickets a la vista")
        except Exception as exc:
            print(f"  Supermercados MAS: la sesión no funciona — {exc}", file=sys.stderr)
            todas = False
        try:
            with dia_account.browser_session() as sesion:
                entradas = dia_account.list_tickets(sesion.fetch_json)
            print(f"  Día: {len(entradas)} tickets a la vista")
        except Exception as exc:
            print(f"  Día: la sesión no funciona — {exc}", file=sys.stderr)
            todas = False

    if not todas:
        print(
            "\nAbre Firefox, inicia sesión en la tienda que falte y vuelve a intentarlo.",
            file=sys.stderr,
        )
    return 0 if todas else 1


def _download_dia(conn, con_imagenes=False, solo_comprados=False, raiz=None) -> bool:
    """Pull the Dia tickets, and optionally its missing pictures, in **one** browser session.

    Dia sits behind Akamai, so this opens a real Chromium window carrying the
    cookies of the Firefox profile already logged in: on a machine without a
    desktop the command must run under ``xvfb-run -a``. One session for both jobs
    on purpose: its session is short lived, and opening a second browser for the
    pictures doubles the chances of losing it halfway through.
    """
    try:
        with dia_account.browser_session() as sesion:
            try:
                counts = dia_account.import_tickets(conn, sesion.fetch_json)
                print(
                    f"Día (cuenta): {counts['imported']} tickets nuevos de {counts['found']} "
                    f"disponibles, {counts['already']} ya estaban, {counts['lines']} líneas"
                    + (f", {counts['failed']} con error" if counts["failed"] else "")
                )
                tickets_ok = counts["failed"] == 0
            except dia_account.DiaAccountError as exc:
                print(f"Día (cuenta): error: {exc}", file=sys.stderr)
                tickets_ok = False

            fotos_ok = True
            if con_imagenes:
                counts_img = images.download_images(
                    conn,
                    raiz or images.images_root(),
                    {"Dia": sesion.fetch_bytes},
                    chains=["Dia"],
                    only_bought=solo_comprados,
                )
                print(
                    f"Día (fotos): {counts_img['downloaded']} descargadas de "
                    f"{counts_img['pending']} pendientes, {counts_img['failed']} con error"
                )
                fotos_ok = counts_img["failed"] == 0
    except dia_account.DiaAccountError as exc:
        print(f"Día: error: {exc}", file=sys.stderr)
        return False
    return tickets_ok and fotos_ok


def _download_mas(conn, inbox) -> bool:
    """Pull every ticket from the Supermercados MAS account; ``False`` on failure.

    The session is read from the Firefox profile that is already logged in, so
    nothing has to be pasted anywhere. Downloads land in the inbox and are
    imported and matched on the spot.
    """
    try:
        counts = mas_account.download_tickets(conn, inbox)
    except mas_account.MasAccountError as exc:
        print(f"MAS (cuenta): error: {exc}", file=sys.stderr)
        return False
    print(
        f"MAS (cuenta): {counts['downloaded']} tickets descargados de {counts['found']} "
        f"disponibles, {counts['imported']} nuevos, {counts['already']} ya estaban"
        + (f", {counts['failed']} con error" if counts["failed"] else "")
    )
    return counts["failed"] == 0


def _match_chains(conn, chains) -> None:
    """Link the imported ticket lines to the catalogue and report the outcome.

    Runs right after importing, so a ticket is never left silently unmatched
    when the catalogue is already there. Matching is per chain (a line only
    matches products of its own chain); the comparison against the other chains
    is what the report shows.
    """
    for chain in chains:
        counts = matching.match_ticket_lines(conn, chain)
        if not counts["lines"]:
            continue
        if counts["matched"]:
            print(
                f"{chain}: {counts['matched']} líneas emparejadas en esta pasada; "
                f"quedan {counts['unmatched']} sin emparejar"
            )
        else:
            # Only the still-unresolved lines are walked, so an honest summary
            # says what is left, not "0 of N matched".
            print(f"{chain}: {counts['unmatched']} líneas siguen sin emparejar con el catálogo")
        if counts["unmatched"] and not _has_catalogue(conn, chain):
            print(
                f"  aviso: no hay catálogo de {chain} en la base; ejecuta antes "
                "'precios --cadena ...'"
            )


def _has_catalogue(conn, chain) -> bool:
    row = conn.execute(
        "SELECT COUNT(*) FROM products WHERE chain = ?", (chain,)
    ).fetchone()
    return bool(row[0])


def _cmd_precios(args) -> int:
    chains = ALL_CHAINS if args.cadena == "todas" else (args.cadena,)
    conn = store.init_db(args.db)
    try:
        results = [_ingest_chain(conn, chain, args) for chain in chains]
    finally:
        conn.close()
    return 0 if all(results) else 1


def _cmd_informe(args) -> int:
    conn = store.init_db(args.db)
    try:
        summary = report.build_report(conn, args.salida)
    finally:
        conn.close()
    print("Resumen:", summary)
    print(f"Informe escrito en {args.salida}")
    return 0


def _cmd_estado(args) -> int:
    conn = store.init_db(args.db)
    try:
        chains = conn.execute(
            "SELECT chain, COUNT(*) AS products, "
            "SUM(CASE WHEN unit_price IS NOT NULL THEN 1 ELSE 0 END) AS with_unit, "
            "MAX(observed_at) AS newest "
            "FROM products GROUP BY chain ORDER BY chain"
        ).fetchall()
        tickets = conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
        lines, matched = conn.execute(
            "SELECT COUNT(*), SUM(CASE WHEN product_id IS NOT NULL THEN 1 ELSE 0 END) "
            "FROM ticket_lines"
        ).fetchone()
    finally:
        conn.close()

    print("Productos por cadena:")
    if not chains:
        print("  Sin productos en la base de datos.")
    for row in chains:
        print(
            f"  {row['chain']}: {row['products']} productos, "
            f"{row['with_unit']} con precio por unidad, último {row['newest']}"
        )
    matched = matched or 0
    print(f"Tickets: {tickets}")
    print(f"Líneas de ticket: {lines}")
    print(f"  Emparejadas: {matched}")
    print(f"  Sin emparejar: {lines - matched}")
    return 0


def _cmd_actualizar(args) -> int:
    """Cron entry point: every chain, then the tickets, then the report."""
    code = _cmd_precios(args)
    _cmd_tickets(
        argparse.Namespace(
            db=args.db,
            inbox=_default_inbox(),
            descargar_mas=True,
            descargar_dia=True,
        )
    )
    _cmd_informe(args)
    return code


def _add_db_option(parser):
    parser.add_argument(
        "--db", default=_default_db(), metavar="RUTA",
        help=f"Base de datos SQLite (por defecto: {_default_db()}).",
    )


def _add_ingest_options(parser):
    _add_db_option(parser)
    parser.add_argument(
        "--cadena", choices=CHAIN_CHOICES + ("todas",), default="todas",
        help="Cadena a descargar (por defecto: todas).",
    )
    parser.add_argument(
        "--pausa", type=float, default=0.5, metavar="SEGUNDOS",
        help="Pausa entre peticiones (por defecto: 0,5).",
    )
    parser.add_argument(
        "--max-peticiones", type=int, default=400, dest="max_peticiones", metavar="N",
        help="Tope de peticiones para MAS y de páginas por categoría para Día "
             "(por defecto: 400).",
    )
    parser.add_argument(
        "--categorias", default=None, metavar="L2033,L2007",
        help="Solo para Día: ids de categoría separados por comas "
             "(por defecto: todas).",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tickets_cli.py",
        description="Catálogos de supermercados e informe de compras.",
    )
    subparsers = parser.add_subparsers(dest="comando", required=True)

    precios = subparsers.add_parser(
        "precios",
        help="Descarga los catálogos de las cadenas y los guarda en la base de datos.",
        epilog=DISPLAY_HINT,
    )
    _add_ingest_options(precios)
    precios.set_defaults(func=_cmd_precios)

    informe = subparsers.add_parser(
        "informe", help="Genera el informe HTML estático."
    )
    _add_db_option(informe)
    informe.add_argument(
        "--salida", default=DEFAULT_REPORT_PATH, metavar="RUTA",
        help=f"Fichero HTML de salida (por defecto: {DEFAULT_REPORT_PATH}).",
    )
    informe.set_defaults(func=_cmd_informe)

    estado = subparsers.add_parser(
        "estado", help="Muestra el contenido de la base de datos sin escribir nada."
    )
    _add_db_option(estado)
    estado.set_defaults(func=_cmd_estado)

    sesiones = subparsers.add_parser(
        "sesiones",
        help="Comprueba si las sesiones de las tiendas están disponibles en Firefox.",
    )
    sesiones.add_argument(
        "--navegador", action="store_true",
        help="Además de mirar las cookies, comprueba de verdad contra las tiendas "
             "(más lento: Día necesita el navegador).",
    )
    sesiones.set_defaults(func=_cmd_sesiones)

    imagenes = subparsers.add_parser(
        "imagenes",
        help="Descarga las fotos de los productos (Mercadona por HTTP; Día necesita el navegador).",
    )
    _add_db_option(imagenes)
    imagenes.add_argument(
        "--cadena", choices=CHAIN_CHOICES + ("todas",), default="todas",
        help="Cadena cuyas fotos se descargan (por defecto: todas).",
    )
    imagenes.add_argument(
        "--solo-comprados", action="store_true", dest="solo_comprados",
        help="Sólo las fotos de productos que aparecen en algún ticket (la primera pasada barata).",
    )
    imagenes.add_argument(
        "--limite", type=int, default=None, metavar="N",
        help="Máximo de fotos en esta pasada (por defecto: todas las que falten).",
    )
    imagenes.add_argument(
        "--rehacer", action="store_true",
        help="Vuelve a descargar las fotos aunque ya estuvieran (para rehacer una pasada).",
    )
    imagenes.add_argument(
        "--imagenes", default=None, metavar="RUTA",
        help="Carpeta de fotos (por defecto: $IMAGES_DIR o 'images' junto a la base).",
    )
    imagenes.set_defaults(func=_cmd_imagenes)

    tickets = subparsers.add_parser(
        "tickets",
        help="Importa los tickets PDF de una carpeta (MAS y Día) y los empareja "
             "con el catálogo.",
    )
    _add_db_option(tickets)
    tickets.add_argument(
        "--inbox", default=_default_inbox(), metavar="RUTA",
        help=f"Carpeta con los PDF (por defecto: $TICKETS_INBOX o {DEFAULT_INBOX}).",
    )
    tickets.add_argument(
        "--descargar-mas",
        action="store_true",
        dest="descargar_mas",
        help="Antes de importar, descarga los tickets de tu cuenta de Supermercados MAS "
             "usando la sesión de Firefox (no hay que pegar ninguna cookie).",
    )
    tickets.add_argument(
        "--descargar-dia",
        action="store_true",
        dest="descargar_dia",
        help="Descarga los tickets de tu cuenta de Día (datos exactos, sin OCR). En un "
             "servidor hay que ejecutarlo bajo 'xvfb-run -a' porque Día bloquea el HTTP plano.",
    )
    tickets.add_argument(
        "--imagenes",
        action="store_true",
        help="Con --descargar-dia, trae también las fotos que falten usando la MISMA "
             "sesión de navegador (la de Día caduca enseguida: mejor una sola).",
    )
    tickets.add_argument(
        "--solo-comprados",
        action="store_true",
        dest="solo_comprados",
        help="Con --imagenes: sólo las fotos de productos que aparecen en algún ticket.",
    )
    tickets.set_defaults(func=_cmd_tickets)

    amazon_cmd = subparsers.add_parser(
        "amazon",
        help="Busca en Amazon los productos (marca nacional, higiene, pañales, cápsulas) "
             "donde comprar allí podría salir a cuenta, y guarda las ofertas.",
        epilog=AMAZON_HINT,
    )
    _add_db_option(amazon_cmd)
    amazon_cmd.add_argument(
        "--limite", type=int, default=None, metavar="N",
        help="Cuántos productos buscar en esta pasada (por defecto: todos los candidatos).",
    )
    amazon_cmd.add_argument(
        "--cadena", choices=CHAIN_CHOICES + ("todas",), default="todas",
        help="Cadena de los productos a buscar (por defecto: todas).",
    )
    amazon_cmd.add_argument(
        "--producto", type=int, default=None, metavar="ID",
        help="Buscar sólo este producto del catálogo (comprobación a mano).",
    )
    amazon_cmd.add_argument(
        "--buscar", default=None, metavar="TEXTO",
        help="Hacer una búsqueda suelta y mostrarla por pantalla, sin guardar nada.",
    )
    amazon_cmd.set_defaults(func=_cmd_amazon)

    actualizar = subparsers.add_parser(
        "actualizar",
        help="Entrada de cron: todas las cadenas y después el informe.",
        epilog=DISPLAY_HINT,
    )
    _add_ingest_options(actualizar)
    actualizar.add_argument(
        "--salida", default=DEFAULT_REPORT_PATH, metavar="RUTA",
        help=f"Fichero HTML de salida (por defecto: {DEFAULT_REPORT_PATH}).",
    )
    actualizar.set_defaults(func=_cmd_actualizar)

    return parser


def main(argv=None) -> int:
    load_dotenv()  # .env is optional: DB_PATH and TICKETS_INBOX can live there.
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
