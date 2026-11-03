"""Download the supermarket tickets from a Supermercados MAS account.

The shop's customer area lists every recent ticket and links each one to a plain
PDF download::

    /clubmascustomers/customer/getticketpdf/centre/043/date/12%2F10%2F2026/caja/103/numticket/46/

Verified 2026-10-12 against a real account: 18 tickets, 01/09/2026 to 12/10/2026,
each a PDF with a text layer — the same file the shop hands out by hand, so
:func:`tickets.parsing.import_ticket` reads it with no OCR.

**The session comes from the browser, not from the user.** Firefox stores its
cookies unencrypted in ``cookies.sqlite`` (``moz_cookies`` table), so the session
is read from the profile already logged in. Nothing is printed, copied into the
repository or written outside a temporary directory: a cookie is a credential.
Snap Firefox keeps its profile under ``~/snap/firefox`` instead of ``~/.mozilla``,
and both are searched.

Limits worth knowing:

* The list is only what the account page shows (two pages, ~18 tickets); how far
  back it goes is the shop's decision, not ours.
* A session expires. When it does, this raises :class:`MasAccountError` naming the
  fix (log in again in Firefox) instead of silently returning nothing.
"""

from __future__ import annotations

import html
import os
import re
from pathlib import Path
from urllib.parse import unquote

import requests

from supermarket.firefox import FirefoxSessionError
from supermarket.firefox import find_profile as _find_profile
from supermarket.firefox import firefox_cookies as _firefox_cookies

CHAIN = "MAS"
HISTORY_URL = "https://www.supermercadosmas.com/sales/order/history/"
TICKET_HOST = "supermercadosmas.com"
TICKET_LINK_RE = re.compile(
    r'href="(https://www\.supermercadosmas\.com/clubmascustomers/customer/getticketpdf[^"]+)"'
)
TICKET_PARTS_RE = re.compile(r"/date/(?P<date>[^/]+)/caja/(?P<caja>[^/]+)/numticket/(?P<number>[^/]+)")
FIREFOX_USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64; rv:141.0) Gecko/20100101 Firefox/141.0"

#: Pages of the history to walk before giving up. The site showed two.
MAX_PAGES = 6


class MasAccountError(RuntimeError):
    """The MAS account could not be read: no session, expired or unexpected page."""


def find_profile(root=None) -> Path:
    """The Firefox profile with a cookie database, or :class:`MasAccountError`."""
    try:
        return _find_profile(root)
    except FirefoxSessionError as exc:
        raise MasAccountError(str(exc)) from exc


def firefox_cookies(host_suffix=TICKET_HOST, profile=None) -> dict:
    """The MAS cookies of the logged-in Firefox profile, as ``{name: value}``."""
    try:
        return _firefox_cookies(host_suffix, profile)
    except FirefoxSessionError as exc:
        raise MasAccountError(str(exc)) from exc


def session_from_firefox(host_suffix=TICKET_HOST, profile=None) -> requests.Session:
    """A ``requests`` session carrying the browser's session cookies."""
    cookies = firefox_cookies(host_suffix=host_suffix, profile=profile)
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": FIREFOX_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
        }
    )
    for name, value in cookies.items():
        # The account cookie lives on .www.supermercadosmas.com; the domain keeps
        # the session valid for the whole site.
        session.cookies.set(name, value, domain=f".{host_suffix}", path="/")
    return session


def ticket_urls(session, max_pages=MAX_PAGES, history_url=HISTORY_URL) -> list:
    """Every ticket download URL the account page offers, newest first, deduped."""
    urls = []
    vistos = set()
    for pagina in range(1, max_pages + 1):
        url = history_url if pagina == 1 else f"{history_url}?p={pagina}"
        respuesta = session.get(url, timeout=40)
        if respuesta.status_code != 200:
            # The first page must answer: anything else means the session is gone.
            # A later page may simply not exist, and that ends the walk.
            if pagina == 1:
                raise MasAccountError(f"{url}: HTTP {respuesta.status_code}")
            break
        encontrados = [
            html.unescape(match) for match in TICKET_LINK_RE.findall(respuesta.text)
        ]
        nuevos = [enlace for enlace in encontrados if enlace not in vistos]
        if not nuevos:
            break
        for enlace in nuevos:
            vistos.add(enlace)
            urls.append(enlace)
    if not urls:
        raise MasAccountError(
            f"{history_url} no ofreció ningún ticket: la sesión puede haber caducado "
            "(inicia sesión de nuevo en Firefox)"
        )
    return urls


def filename_for(url) -> str:
    """A stable, sortable file name for a ticket URL, e.g. ``mas-20261012-103-046.pdf``."""
    match = TICKET_PARTS_RE.search(url)
    if not match:
        return f"mas-{abs(hash(url))}.pdf"
    fecha = unquote(html.unescape(match.group("date")))
    dia, mes, anio = (fecha.split("/") + ["", "", ""])[:3]
    caja = str(match.group("caja")).zfill(3)
    numero = str(match.group("number")).zfill(3)
    return f"mas-{anio}{mes}{dia}-{caja}-{numero}.pdf"


def download_tickets(conn, dest_dir, session=None, max_pages=MAX_PAGES) -> dict:
    """Download every ticket of the account, importing and matching each one.

    Returns ``{"found", "downloaded", "imported", "already", "failed"}``. A ticket
    that fails to download or parse is counted and the walk continues: one broken
    PDF must not cost the rest of the history.
    """
    from tickets import matching, parsing, store  # local: keep this module importable alone

    session = session or session_from_firefox()
    destino = Path(dest_dir)
    destino.mkdir(parents=True, exist_ok=True)

    urls = ticket_urls(session, max_pages=max_pages)
    counts = {"found": len(urls), "downloaded": 0, "imported": 0, "already": 0, "failed": 0}

    for url in urls:
        ruta = destino / filename_for(url)
        try:
            respuesta = session.get(url, timeout=60)
            if respuesta.status_code != 200 or not respuesta.content.startswith(b"%PDF"):
                counts["failed"] += 1
                continue
            ruta.write_bytes(respuesta.content)
            counts["downloaded"] += 1

            resultado = parsing.import_ticket(conn, ruta)
            if resultado["inserted"]:
                counts["imported"] += 1
            else:
                counts["already"] += 1
        except (parsing.ParseError, requests.RequestException, OSError):
            counts["failed"] += 1

    # Matching once, after every ticket is stored: one pass over the unresolved
    # lines instead of one pass per downloaded ticket.
    matching.match_ticket_lines(conn, CHAIN)
    return counts


def default_inbox() -> Path:
    """Where downloads go when nothing else is said."""
    return Path(os.environ.get("TICKETS_INBOX", "samples/inbox"))
