"""Search Amazon.es for the products where buying there could pay off.

Supermarket own-brand hygiene is far cheaper **per litre** than Amazon (measured
2026-10-12: Dia 1,09 €/l, MAS UNNIA 1,19 €/l, Mercadona Deliplus 1,20 €/l against
Instituto Español 750 ml at 3,00 €/l and Inibsa 1 l at 3,70 €/l), so this module is
only used on national brands, big packs, nappies, capsules and drugstore goods.
Which products those are is decided in :func:`tickets.store.amazon_candidates`.

What a real Chromium sees (measured 2026-10-12, ``https://www.amazon.es/s?k=<q>``):

* HTTP 200 and 60 results, **with or without** the Firefox session and even
  headless. The session is used when it is there and never required.
* Each result is a block marked ``data-component-type="s-search-result"``, with
  ``data-asin``, an ``h2`` title and a ``.a-price .a-offscreen`` price
  (``"4,25 €"``).
* Amazon prints its own unit price in text, e.g. ``"1,86 € /l"``, ``"0,43 €/100 g"``
  or ``"0,07 € /unidad"``. Formats actually seen: ``/100 ml``, ``/100 g``,
  ``/unidad``, ``/l`` and ``/kg``, with or without spaces around the slash.
  Anything else (``"/100"``, ``"/rollos de papel"``) is refused rather than
  guessed.
* Sponsored blocks carry the word ``Patrocinado``.

A page with no result blocks at all is an explicit error, not an empty list: an
empty list would read as "Amazon has nothing", which is a different and much more
dangerous claim than "the page changed".
"""

from __future__ import annotations

import contextlib
import html as html_entity
import re
import time
from urllib.parse import urlencode

HOST_SUFFIX = "amazon.es"
SEARCH_URL = "https://www.amazon.es/s"
PRODUCT_URL = "https://www.amazon.es/dp/{asin}"

#: The page's result blocks are marked with this attribute.
_RESULT_MARKER = 'data-component-type="s-search-result"'

#: Fewest seconds between two searches: this is a public shop, not an API.
MIN_SEARCH_INTERVAL = 3.0

#: The unit price is printed between parentheses; read up to the closing one.
_UNIT_PRICE_RE = re.compile(r"(\d[\d.,]*)\s*€\s*/\s*([^)]{1,18})")
_PRICE_RE = re.compile(r'class="a-offscreen">([^<]+)</span>')
_ASIN_RE = re.compile(r'data-asin="([^"]*)"')
_TITLE_RE = re.compile(r"<h2[^>]*>(.*?)</h2>", re.S)
_TAG_RE = re.compile(r"<[^>]+>", re.S)

#: ``100 ml`` is a tenth of a litre, so multiply by ten to reach our unit.
_PER_HUNDRED = {"100ml": ("l", 10.0), "100g": ("kg", 10.0)}
#: Amazon's unit names mapped to ours (kg, l, ud), with no factor.
_PLAIN_UNITS = {
    "l": ("l", 1.0), "litro": ("l", 1.0), "litros": ("l", 1.0), "lt": ("l", 1.0),
    "kg": ("kg", 1.0), "kilo": ("kg", 1.0), "kilos": ("kg", 1.0), "kgs": ("kg", 1.0),
    "unidad": ("ud", 1.0), "unidades": ("ud", 1.0), "ud": ("ud", 1.0), "uds": ("ud", 1.0),
}

_last_search_at = 0.0


class AmazonError(RuntimeError):
    """Amazon could not be read: the page changed, a captcha, or a dead browser."""


def search_url(query, page=1) -> str:
    """The URL of one results page of ``query``."""
    params = {"k": str(query)}
    if page and page > 1:
        params["page"] = int(page)
    return f"{SEARCH_URL}?{urlencode(params)}"


def _to_float(token):
    """``"1.234,56"`` -> ``1234.56``; ``"4,25"`` -> ``4.25``."""
    text = str(token).strip()
    if not text:
        return None
    if "," in text:
        text = text.replace(".", "").replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return None


def _text(html) -> str:
    """Plain text of a fragment: tags dropped, entities decoded, spaces collapsed."""
    without = _TAG_RE.sub(" ", str(html or ""))
    without = html_entity.unescape(without).replace("\xa0", " ")
    return re.sub(r"\s+", " ", without).strip()


def _clean_unit_price_text(raw) -> str:
    """``"1,86€ /l"`` -> ``"1,86 €/l"``: what Amazon printed, tidied enough to show."""
    text = str(raw or "").replace("\xa0", " ")
    text = re.sub(r"\s*€\s*/\s*", " €/", text)
    return re.sub(r"\s+", " ", text).strip()


def unit_price_from_text(text):
    """``"0,30 €/100 ml"`` -> ``(3.0, "l")``; refuses anything it cannot read.

    Amazon prints per 100 ml, per 100 g, per unit, per kg and per l; the first
    two are scaled by ten into our ``l``/``kg``. Every other label returns
    ``(None, None)`` rather than a guess, so a strange line never becomes a
    comparison.
    """
    if not text:
        return None, None
    match = _UNIT_PRICE_RE.search(str(text).replace("\xa0", " "))
    if not match:
        return None, None
    value = _to_float(match.group(1))
    if value is None or value <= 0:
        return None, None

    label = re.sub(r"\s+", "", match.group(2).lower()).strip("()")
    if label in _PER_HUNDRED:
        unit, factor = _PER_HUNDRED[label]
        return round(value * factor, 4), unit
    if label in _PLAIN_UNITS:
        unit, factor = _PLAIN_UNITS[label]
        return round(value * factor, 4), unit
    return None, None


def _blocks(html) -> list:
    """Split the page into one substring per result block, in document order.

    The split starts at the ``<div`` that carries the marker, not at the marker
    itself: ``data-asin`` usually sits earlier in that same opening tag, and a
    substring starting mid-tag would lose it.
    """
    starts = []
    for match in re.finditer(_RESULT_MARKER, html):
        open_tag = html.rfind("<div", 0, match.start())
        starts.append(open_tag if open_tag >= 0 else match.start())
    if not starts:
        return []
    bounds = starts[1:] + [len(html)]
    return [html[start:end] for start, end in zip(starts, bounds)]


def parse_results(html) -> list:
    """The offers of one results page, in the order Amazon printed them.

    Every offer carries ``asin``, ``title``, ``price`` (float), ``unit_price`` and
    ``unit_label`` in our units (``None`` when Amazon's label could not be read),
    ``unit_price_text`` as printed, ``url`` and ``sponsored``. Blocks without an
    asin, a title or a price are skipped. A page that yields no usable offer
    raises :class:`AmazonError` instead of returning an empty list.
    """
    blocks = _blocks(html)
    if not blocks:
        raise AmazonError(
            "la página de Amazon no trajo ningún resultado; puede haber cambiado "
            "(o no haber ninguno para esa consulta)"
        )

    offers = []
    for block in blocks:
        asin_match = _ASIN_RE.search(block)
        title_match = _TITLE_RE.search(block)
        price_match = _PRICE_RE.search(block)
        asin = (asin_match.group(1).strip() if asin_match else "")
        title = _text(title_match.group(1)) if title_match else ""
        # A price range ("12,95 € - 15,00 €") is not one price: skip it.
        price_token = price_match.group(1) if price_match else ""
        price_numbers = re.findall(r"\d[\d.,]*", price_token)
        price = _to_float(price_numbers[0]) if len(price_numbers) == 1 else None
        if not asin or not title or price is None:
            continue

        unit_match = _UNIT_PRICE_RE.search(_text(block))
        unit_price_text = _clean_unit_price_text(unit_match.group(0)) if unit_match else None
        unit_price, unit_label = unit_price_from_text(unit_price_text)
        offers.append(
            {
                "asin": asin,
                "title": title,
                "price": price,
                "unit_price": unit_price,
                "unit_label": unit_label,
                "unit_price_text": unit_price_text,
                "url": PRODUCT_URL.format(asin=asin),
                "sponsored": "Patrocinado" in _text(block),
            }
        )

    if not offers:
        raise AmazonError(
            f"la página de Amazon trajo {len(blocks)} bloques pero ninguno con asín, "
            "título y precio legibles; puede haber cambiado"
        )
    return offers


def _polite_pause(min_interval) -> None:
    """Wait so two searches are never closer than ``min_interval`` seconds."""
    global _last_search_at
    if min_interval and min_interval > 0:
        remaining = min_interval - (time.monotonic() - _last_search_at)
        if remaining > 0:
            time.sleep(remaining)
    _last_search_at = time.monotonic()


def search(query, fetch_page, max_results=12, min_interval=MIN_SEARCH_INTERVAL) -> list:
    """Search Amazon for ``query`` and return up to ``max_results`` offers.

    ``fetch_page`` is the seam: it takes a URL and returns the page's HTML, so the
    tests never open a browser. Between two calls it waits at least
    ``min_interval`` seconds (``0`` disables the wait, for tests).
    """
    _polite_pause(min_interval)
    offers = parse_results(fetch_page(search_url(query)))
    return offers[: int(max_results)] if max_results is not None else offers


class AmazonSession:
    """An open Chromium page that can fetch an Amazon URL with the browser's own session."""

    def __init__(self, page, timeout_ms=60000, settle_ms=2500):
        self._page = page
        self._timeout = timeout_ms
        self._settle = settle_ms

    def fetch_page(self, url) -> str:
        """Open ``url`` and return the resulting HTML.

        A captcha is reported as such instead of being handed to the parser as a
        page that changed.
        """
        try:
            self._page.goto(url, wait_until="domcontentloaded", timeout=self._timeout)
        except Exception as exc:  # a dead network and a dead browser land here
            raise AmazonError(f"no se pudo abrir {url}: {str(exc)[:160]}") from exc
        self._page.wait_for_timeout(self._settle)
        html = self._page.content()
        lowered = html.lower()
        if "validatecaptcha" in str(url).lower() or "introduce los caracteres" in lowered:
            raise AmazonError(
                "Amazon pidió un captcha; vuelve a intentarlo más tarde o comprueba a mano"
            )
        return html


@contextlib.contextmanager
def open_session(profile=None, headless=False, timeout_ms=60000, settle_ms=2500):
    """Yield an :class:`AmazonSession`: a real Chromium window pointed at Amazon.es.

    The window matters as little as the session: the search page loads with or
    without the Firefox cookies and with or without a display (measured
    2026-10-12). The cookies are still sent when the Firefox profile has them,
    because a session never hurts; a missing profile is not an error. On a machine
    without a desktop, run under ``xvfb-run -a``.
    """
    from playwright.sync_api import sync_playwright  # lazy: tests need no browser

    from supermarket.firefox import FirefoxSessionError, firefox_cookies

    try:
        cookies = firefox_cookies(HOST_SUFFIX, profile=profile)
    except FirefoxSessionError:
        cookies = {}  # no profile, or none logged in: the search still works

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=headless, args=["--disable-blink-features=AutomationControlled"]
        )
        try:
            context = browser.new_context(locale="es-ES", timezone_id="Europe/Madrid")
            if cookies:
                context.add_cookies(
                    [
                        {"name": name, "value": value, "domain": f".{HOST_SUFFIX}", "path": "/"}
                        for name, value in cookies.items()
                    ]
                )
            page = context.new_page()
            yield AmazonSession(page, timeout_ms=timeout_ms, settle_ms=settle_ms)
        finally:
            browser.close()
