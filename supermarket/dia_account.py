"""Download the Dia tickets from the account — exact JSON, no OCR.

Dia's customer area (``https://www.dia.es/my-account/tickets``) is a React app
over a JSON API, verified 2026-10-12 against a real account:

* ``GET /api/v3/eservice-back/customer/current/tickets?business=1&country=ES&page_size=50``
  lists the tickets, each with its ``detail_params`` (begin/business/country/pos/
  store/ticket), the store address, ``submitted_date`` (UTC) and ``total_amount``.
  ``total_tickets`` says how many there are in total.
* ``GET /api/v3/eservice-back/customer/current/tickets/<ticket>?<detail_params>``
  returns one ticket: ``header`` (code, ``display_code``, store, submitted date),
  ``amount.total_amount`` and the product lines in ``corporate.items``, each with
  ``name``, ``code``, ``quantity``, ``price_per_unit``, ``amount``, ``weight``,
  ``variable_weight`` and ``tax``.

That is a strict improvement over reading the ticket PDF: the numbers are exact,
``variable_weight`` marks a line sold by weight (so ``price_per_unit`` is the
€/kg) and cancelled lines carry ``cancel: true``.

**Dia sits behind Akamai, so plain HTTP does not work.** Verified: the same
cookies that answer ``200`` in a real Chromium get ``403 Access Denied`` from
``requests``. The session is therefore a real Chromium window (under ``xvfb-run``
in a container) carrying the cookies read from the Firefox profile already logged
in — see :mod:`supermarket.firefox`.

``purchased_at`` is stored as the shop's local time truncated to the minute, which
is what the paper ticket prints. Truncating is deliberate: the same ticket read
from the account JSON and from the exported PDF must be one row, not two.
"""

from __future__ import annotations

import contextlib
import os
from datetime import datetime, timezone
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

ACCOUNT_URL = "https://www.dia.es/my-account/tickets"
API_ROOT = "https://www.dia.es/api/v3/eservice-back/customer/current/tickets"
HOST_SUFFIX = "dia.es"
LOCAL_ZONE = "Europe/Madrid"
DEFAULT_PAGE_SIZE = 50

#: Ceiling on the pages walked, so a paging bug cannot loop forever.
MAX_PAGES = 20


class DiaAccountError(RuntimeError):
    """The Dia account could not be read: no session, blocked, or changed shape."""


def local_timestamp(submitted_date) -> str | None:
    """``2026-10-09T11:43:40Z`` -> ``2026-10-09T13:43:00`` (shop local, to the minute).

    The API speaks UTC and the ticket prints local time. Cutting the seconds is
    what makes the account path and the PDF path agree on the same ticket.
    """
    if not submitted_date:
        return None
    texto = str(submitted_date).replace("Z", "+00:00")
    try:
        momento = datetime.fromisoformat(texto)
    except ValueError:
        return None
    if momento.tzinfo is None:
        momento = momento.replace(tzinfo=timezone.utc)
    local = momento.astimezone(ZoneInfo(LOCAL_ZONE))
    return local.strftime("%Y-%m-%dT%H:%M:00")


def list_tickets(fetch_json, page_size=DEFAULT_PAGE_SIZE, max_pages=MAX_PAGES) -> list:
    """Every ticket the account lists, walking ``offset`` until they run out."""
    tickets = []
    offset = 0
    for _ in range(max_pages):
        query = urlencode(
            {"business": 1, "country": "ES", "page_size": page_size, "offset": offset}
        )
        payload = fetch_json(f"{API_ROOT}?{query}")
        pagina = payload.get("tickets")
        if pagina is None:
            claves = ", ".join(sorted(payload)) or "sin claves"
            raise DiaAccountError(f"la lista de tickets no trae 'tickets' (claves: {claves})")
        tickets.extend(pagina)
        total = payload.get("total_tickets")
        offset += len(pagina)
        if not pagina or (total is not None and offset >= total):
            break
    if not tickets:
        raise DiaAccountError(
            f"{ACCOUNT_URL} no ofreció ningún ticket: la sesión puede haber caducado "
            "(inicia sesión de nuevo en Firefox)"
        )
    return tickets


def detail_url(entry) -> str:
    """The detail URL of one ticket, built from its own ``detail_params``."""
    params = dict(entry.get("detail_params") or {})
    ticket = params.pop("ticket", None)
    if ticket is None:
        raise DiaAccountError("la entrada de la lista no trae 'detail_params.ticket'")
    return f"{API_ROOT}/{ticket}?{urlencode(params)}"


def parse_ticket(payload) -> dict:
    """Map one ticket payload to the store's shape (the same one the PDFs use)."""
    header = payload.get("header") or {}
    amount = payload.get("amount") or {}
    items = (payload.get("corporate") or {}).get("items")
    if items is None:
        claves = ", ".join(sorted(payload)) or "sin claves"
        raise DiaAccountError(f"el ticket no trae 'corporate.items' (claves: {claves})")

    lines = []
    for item in items:
        if item.get("cancel"):
            continue  # a cancelled line is not a purchase
        name = item.get("name")
        price = item.get("price_per_unit")
        total = item.get("amount")
        if not name or price is None or total is None:
            continue
        weighed = bool(item.get("variable_weight"))
        weight = item.get("weight") or 0
        quantity = weight if weighed and weight else item.get("quantity")
        if quantity is None:
            continue
        lines.append(
            {
                "raw_text": name,
                "qty": float(quantity),
                "price_paid": float(price),
                "total_paid": float(total),
                "purchase_unit": "kg" if weighed else "ud",
                # Dia prints its own product code and that code is the catalogue's
                # external_id. Stored on the line: a line that carries a code and
                # did not resolve is NOT guessed by name, because the product it
                # names is simply not in our catalogue.
                "shop_code": str(item.get("code")) if item.get("code") is not None else None,
            }
        )

    return {
        "chain": "Dia",
        "ticket_number": header.get("display_code") or str(header.get("code") or ""),
        "purchased_at": local_timestamp(header.get("submitted_date")),
        "total": amount.get("total_amount"),
        "lines_total": round(sum(line["total_paid"] for line in lines), 2),
        "lines": lines,
    }


def session_error_message(error_text) -> str | None:
    """The Spanish message for a failed navigation, or ``None`` if it is not a session problem.

    Measured 2026-10-12: with a stale Dia session the browser ends in
    ``ERR_TOO_MANY_REDIRECTS`` on *every* url, while the same browser without
    cookies loads the site normally — so the loop is the expired session, not the
    site. Saying that is much more useful than a Playwright traceback.
    """
    texto = str(error_text)
    if "ERR_TOO_MANY_REDIRECTS" in texto or "ERR_ABORTED" in texto:
        return (
            "la sesión de Día ya no vale (el sitio entra en un bucle de redirecciones); "
            "inicia sesión otra vez en Firefox y vuelve a intentarlo"
        )
    if "Timeout" in texto or "timeout" in texto:
        return (
            "Día no respondió a tiempo; comprueba la conexión o vuelve a intentarlo en un momento"
        )
    return None


class DiaSession:
    """An open Chromium page carrying the shop's session.

    Two fetchers, because Dia needs both: JSON for the account API and **bytes**
    for the product pictures, whose host answers ``403`` to anything that is not a
    real browser.
    """

    def __init__(self, page):
        self._page = page

    def fetch_json(self, path):
        """Fetch ``path`` with the page's own headers and Akamai cookies."""
        result = self._page.evaluate(
            """async (u) => {
                const r = await fetch(u, {headers: {'accept': 'application/json'}});
                return {status: r.status, body: await r.text()};
            }""",
            path,
        )
        if result["status"] != 200:
            raise DiaAccountError(f"Dia request failed: {path} -> HTTP {result['status']}")
        import json

        cuerpo = (result["body"] or "").lstrip()
        if cuerpo.startswith("<"):
            # With a dead session the API answers with the site's HTML (the login
            # page) instead of JSON. That happens often enough to deserve its own
            # sentence rather than a JSON parse error.
            raise DiaAccountError(
                "la sesión de Día ya no vale (el sitio devolvió su página de acceso en vez de "
                "datos); inicia sesión otra vez en Firefox y vuelve a intentarlo"
            )
        try:
            return json.loads(result["body"])
        except ValueError as exc:
            raise DiaAccountError(f"Dia answered with something that is not JSON: {path}") from exc

    def fetch_bytes(self, url):
        """Fetch a picture from inside the page: ``(bytes, content_type)`` or ``(None, None)``."""
        import base64

        result = self._page.evaluate(
            """async (u) => {
                const r = await fetch(u, {headers: {'accept': 'image/*'}});
                if (!r.ok) return {status: r.status, b64: null, type: null};
                const bytes = new Uint8Array(await r.arrayBuffer());
                let binary = '';
                for (let i = 0; i < bytes.length; i++) binary += String.fromCharCode(bytes[i]);
                return {status: r.status, b64: btoa(binary), type: r.headers.get('content-type')};
            }""",
            url,
        )
        if not result.get("b64"):
            return None, None
        return base64.b64decode(result["b64"]), result.get("type")


@contextlib.contextmanager
def browser_session(profile=None, headless=False, timeout_ms=60000, settle_ms=2000):
    """Yield a :class:`DiaSession`: a real Chromium window with the shop's session.

    The window is not a preference: Akamai answers ``403`` to plain HTTP and to
    headless Chromium alike. Cookies come from the Firefox profile that is already
    logged in.
    """
    from playwright.sync_api import sync_playwright  # lazy: tests need no browser

    from supermarket.firefox import FirefoxSessionError, firefox_cookies

    try:
        cookies = firefox_cookies(HOST_SUFFIX, profile=profile)
    except FirefoxSessionError as exc:
        raise DiaAccountError(str(exc)) from exc

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=headless, args=["--disable-blink-features=AutomationControlled"]
        )
        try:
            context = browser.new_context(locale="es-ES", timezone_id=LOCAL_ZONE)
            context.add_cookies(
                [
                    {"name": name, "value": value, "domain": f".{HOST_SUFFIX}", "path": "/"}
                    for name, value in cookies.items()
                ]
            )
            page = context.new_page()
            try:
                page.goto(ACCOUNT_URL, wait_until="domcontentloaded", timeout=timeout_ms)
            except Exception as exc:  # a dead session and a dead network land here
                mensaje = session_error_message(exc)
                raise DiaAccountError(
                    mensaje or f"no se pudo abrir {ACCOUNT_URL}: {str(exc)[:160]}"
                ) from exc
            page.wait_for_timeout(settle_ms)
            yield DiaSession(page)
        finally:
            browser.close()


def _resolve_codes(conn, ticket) -> int:
    """Fill in ``product_id`` for the lines whose shop code is in the catalogue.

    Exact by construction: the ticket prints the shop's product code and the
    catalogue stores it as ``external_id``. Returns how many lines resolved.
    """
    from tickets import store

    resueltos = 0
    for line in ticket["lines"]:
        product_id = store.resolved_line_id(conn, ticket["chain"], line.get("shop_code"))
        if product_id is None:
            continue
        line["product_id"] = product_id
        line["matched_by"] = "code"
        line["match_confidence"] = 1.0
        resueltos += 1
    return resueltos


def import_tickets(conn, fetch_json, limit=None) -> dict:
    """Import every ticket of the account and match it, counting what happened.

    ``limit`` stops after N tickets, which is what exploring wants; the default
    walks the whole history. One broken ticket is counted and the walk continues.
    """
    from tickets import matching, store

    entries = list_tickets(fetch_json)
    if limit is not None:
        entries = entries[:limit]

    counts = {
        "found": len(entries), "imported": 0, "already": 0, "failed": 0,
        "lines": 0, "by_code": 0,
    }
    for entry in entries:
        try:
            ticket = parse_ticket(fetch_json(detail_url(entry)))
            counts["by_code"] += _resolve_codes(conn, ticket)
            _, inserted = store.upsert_ticket(conn, ticket)
        except DiaAccountError:
            counts["failed"] += 1
            continue
        counts["lines"] += len(ticket["lines"])
        counts["imported" if inserted else "already"] += 1

    # Matching once at the end, not once per ticket: a per-ticket pass would
    # re-score every still-unresolved line of the chain 99 times over.
    matching.match_ticket_lines(conn, "Dia")
    return counts


def default_profile():
    """The profile path, or ``None`` to let the shared reader find it."""
    return os.environ.get("FIREFOX_PROFILE") or None
