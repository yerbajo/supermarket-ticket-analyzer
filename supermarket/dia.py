"""Dia online-shop catalogue connector, driven by a real browser.

Everything under ``www.dia.es`` sits behind **Akamai**: plain HTTP and even
headless Chromium get ``403 Access Denied`` from ``errors.edgesuite.net``. What
does work (verified 2026) is a Chromium with a real window in a virtual display::

    xvfb-run -a .venv/bin/python -m ...

so ``DiaBrowser`` launches Chromium **headed** on purpose. On a machine with a
desktop session the window simply appears there; on a server, wrap the command
in ``xvfb-run -a`` as above.

Verified live shape (2026), which corrects the connector that shipped with the
fork (it used ``/api/v1/plp-back/reduced`` + a path, and expected ``plp_items``):

* Category tree: ``GET /api/v1/plp-insight/initial_analytics/l1/all/L105`` returns
  ``menu_analytics``, a dict keyed by category id (``L101``, ``L2033``…) whose
  values carry ``parameter`` (``L1_quesos`` / ``L2_curado``) and ``path``
  (``/quesos/c/L101``).
* Category products:
  ``GET /api/v1/plp-back/l1/all/<top-id>/reduced?category_id=<id>&page=<n>``,
  where ``top-id`` is the level-1 navigation id the category hangs from.
* The product list is ``items`` (**not** ``plp_items``) and each item looks like::

      {"object_id": "42070", "display_name": "Banana granel 900 g aprox.",
       "url": "/frutas/platanos-y-bananas/p/42070", "weight_in_grams": 900,
       "prices": {"price": 1.0, "price_per_unit": 1.11, "measure_unit": "KILO", ...}}

  so the shop itself provides the price per unit; ``measure_unit`` is mapped to
  our ``kg``/``l``/``ud`` labels and anything unknown stays ``None``.
* ``total_items`` gives the catalogue size of the category and ``page_size`` the
  page size, so paging is computed from those two.
"""

from __future__ import annotations

import json
import math
import os
import time

from tickets import store
from tickets.units import normalize_unit

CHAIN = "Dia"
SITE_ROOT = "https://www.dia.es"
API_ROOT = f"{SITE_ROOT}/api/v1"
TREE_PATH = f"{API_ROOT}/plp-insight/initial_analytics/l1/all/L105"
PRODUCTS_PATH = f"{API_ROOT}/plp-back/l1/all/{{top_id}}/reduced?category_id={{category_id}}&page={{page}}"
HOME_URL = f"{SITE_ROOT}/"
CONSENT_SELECTOR = "#onetrust-accept-btn-handler"

#: Environment variable naming the browser binary. In the container there is no
#: Playwright browser bundle: the system Chromium is installed and pointed at
#: here, which keeps the image hundreds of megabytes smaller.
CHROMIUM_PATH_ENV = "CHROMIUM_PATH"

# Dia's own unit labels -> ours. Unknown values are left as None on purpose.
MEASURE_UNITS = {"KILO": "kg", "LITRO": "l", "UNIDAD": "ud", "UD": "ud", "UNIDADES": "ud"}


class DiaError(RuntimeError):
    """Raised when Dia cannot be read: blocked browser, bad status or new shape."""


def parse_category_tree(payload):
    """Flatten ``menu_analytics`` into ``id/name/path/level/top_id`` rows.

    ``top_id`` is the level-1 category the entry hangs from (itself when the
    entry already is level 1); the products endpoint needs it.
    """
    menu = payload.get("menu_analytics")
    if menu is None:
        keys = ", ".join(sorted(payload)) or "no keys"
        raise DiaError(f"Dia tree payload has no 'menu_analytics' (keys: {keys}).")

    rows = []

    def walk(nodes, top_id):
        for node_id, node in (nodes or {}).items():
            parameter = node.get("parameter")
            level = 1 if str(parameter or "").startswith("L1_") else 2
            current_top = node_id if level == 1 else top_id
            rows.append(
                {
                    "id": node_id,
                    "name": str(parameter or "").split("_", 1)[-1],
                    "path": node.get("path"),
                    "level": level,
                    "top_id": current_top,
                }
            )
            walk(node.get("children"), current_top)

    walk(menu, None)
    return rows


def measure_label(measure_unit):
    """Map Dia's measure unit to ``kg``/``l``/``ud``; unknown -> ``None``."""
    if not measure_unit:
        return None
    return MEASURE_UNITS.get(str(measure_unit).strip().upper())


def parse_products(payload, category_name, observed_at=None):
    """Map a category-products payload to store rows, dropping unusable items."""
    items = payload.get("items")
    if items is None:
        keys = ", ".join(sorted(payload)) or "no keys"
        raise DiaError(f"Dia products payload has no 'items' (keys: {keys}).")

    observed_at = observed_at or store.utcnow()
    rows = []
    for item in items:
        external_id = item.get("object_id")
        name = item.get("display_name")
        prices = item.get("prices") or {}
        price = prices.get("price")
        if not external_id or not name or price is None:
            continue

        unit_price = prices.get("price_per_unit")
        unit_label = measure_label(prices.get("measure_unit"))
        if unit_label is None or unit_price is None:
            # Fall back to the size written in the name; never invent a unit.
            derived_price, derived_label = normalize_unit(price, name)
            if unit_label is None:
                unit_label, unit_price = derived_label, derived_price
            elif derived_label == unit_label:
                unit_price = derived_price

        rows.append(
            {
                "chain": CHAIN,
                "external_id": str(external_id),
                "name": name,
                "format": f"{item['weight_in_grams']} g" if item.get("weight_in_grams") else None,
                "price": float(price),
                "unit_price": float(unit_price) if unit_price is not None else None,
                "unit_label": unit_label,
                "category": category_name,
                "url": f"{SITE_ROOT}{item['url']}" if item.get("url") else None,
                "image_url": f"{SITE_ROOT}{item['image']}" if item.get("image") else None,
                "observed_at": observed_at,
            }
        )
    return rows


class DiaBrowser:
    """A real Chromium window that can call Dia's own API from inside the page.

    Used as a context manager::

        with DiaBrowser() as browser:
            payload = browser.fetch_json(TREE_PATH)
    """

    def __init__(self, headless=False, timeout_ms=60000, settle_ms=2500):
        self.headless = headless
        self.timeout_ms = timeout_ms
        self.settle_ms = settle_ms
        self._playwright = None
        self._browser = None
        self._page = None

    @staticmethod
    def launch_options():
        """Keyword arguments for ``chromium.launch`` for the current environment.

        * ``CHROMIUM_PATH`` selects an installed browser (the container case);
          without it Playwright uses its own bundled Chromium (the workstation).
        * Chromium refuses to use its sandbox as root, so ``--no-sandbox`` is
          added when running as root — again the container case — and nowhere
          else.
        """
        options = {
            "headless": False,
            "args": ["--disable-blink-features=AutomationControlled"],
        }
        executable = os.environ.get(CHROMIUM_PATH_ENV)
        if executable:
            options["executable_path"] = executable
        if os.geteuid() == 0:
            options["args"].append("--no-sandbox")
        return options

    def __enter__(self):
        from playwright.sync_api import sync_playwright  # lazy: tests need no browser

        self._playwright = sync_playwright().start()
        options = self.launch_options()
        options["headless"] = self.headless
        try:
            self._browser = self._playwright.chromium.launch(**options)
        except Exception as exc:  # missing browser or no display
            self._playwright.stop()
            self._playwright = None
            raise DiaError(
                "Could not launch Chromium. Install it with 'playwright install chromium' (or "
                "point CHROMIUM_PATH at an installed browser) and, on a machine without a "
                "desktop, run the command under 'xvfb-run -a'."
            ) from exc

        context = self._browser.new_context(
            locale="es-ES", timezone_id="Europe/Madrid", user_agent=_USER_AGENT
        )
        self._page = context.new_page()
        self._page.goto(HOME_URL, wait_until="domcontentloaded", timeout=self.timeout_ms)
        self._page.wait_for_timeout(self.settle_ms)
        self._dismiss_consent()
        return self

    def _dismiss_consent(self):
        """Close the OneTrust wall; it intercepts every click while it is up."""
        try:
            self._page.click(CONSENT_SELECTOR, timeout=8000)
        except Exception:
            self._page.evaluate(
                "document.querySelectorAll('#onetrust-consent-sdk,.onetrust-pc-dark-filter')"
                ".forEach(e => e.remove())"
            )

    def fetch_json(self, path):
        """Fetch ``path`` with the page's own headers and Akamai cookies."""
        if self._page is None:
            raise DiaError("DiaBrowser is not open; use it as a context manager.")
        result = self._page.evaluate(
            """async (u) => {
                const r = await fetch(u, {headers: {'accept': 'application/json'}});
                return {status: r.status, body: await r.text()};
            }""",
            path,
        )
        if result["status"] != 200:
            raise DiaError(f"Dia request failed: {path} -> HTTP {result['status']}")
        try:
            return json.loads(result["body"])
        except ValueError as exc:
            raise DiaError(f"Dia answered with something that is not JSON: {path}") from exc

    def __exit__(self, exc_type, exc, tb):
        if self._browser is not None:
            self._browser.close()
        if self._playwright is not None:
            self._playwright.stop()
        self._page = None
        self._browser = None
        self._playwright = None
        return False


_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)


def ingest_dia(conn, fetch_json, category_ids=None, max_pages=20, sleep_seconds=0.3, retries=2):
    """Walk the categories and upsert their products.

    ``fetch_json`` is a callable ``path -> payload``: in production
    ``DiaBrowser().fetch_json``, in tests a fake. ``category_ids`` limits the walk
    to those ids (handy while exploring); by default every category is walked.

    A walk is hundreds of requests long, so one bad page must not cost the rest: a
    page that fails is retried ``retries`` times and then counted in
    ``failed_pages`` while the walk carries on. A 502 from this API, four minutes
    into a run, aborted everything before this existed.
    """
    counts = {"categories": 0, "products": 0, "skipped": 0, "pages": 0, "failed_pages": 0}

    def traer(url):
        """The page at ``url``, or ``None`` when it keeps failing."""
        for intento in range(retries + 1):
            try:
                return fetch_json(url)
            except Exception:
                if intento >= retries:
                    counts["failed_pages"] += 1
                    return None
                time.sleep(1.0 + 2.0 * intento)
        return None

    tree = parse_category_tree(fetch_json(TREE_PATH))
    # Dia repeats some ids: the level-1 node and its own "todo <categoria>" child
    # share id and path, so the same listing would be fetched twice.
    unique = {}
    for row in tree:
        unique.setdefault(row["id"], row)
    selected = [row for row in unique.values() if category_ids is None or row["id"] in set(category_ids)]
    counts["categories"] = len(selected)

    for category in selected:
        first = traer(
            PRODUCTS_PATH.format(top_id=category["top_id"], category_id=category["id"], page=1)
        )
        counts["pages"] += 1
        if first is None:
            continue
        payloads = [first]
        page_size = (first.get("pagination") or {}).get("page_size") or 20
        total_items = first.get("total_items") or 0
        total_pages = min(math.ceil(total_items / page_size) if page_size else 1, max_pages)
        for page in range(2, total_pages + 1):
            if sleep_seconds:
                time.sleep(sleep_seconds)
            siguiente = traer(
                PRODUCTS_PATH.format(
                    top_id=category["top_id"], category_id=category["id"], page=page
                )
            )
            counts["pages"] += 1
            if siguiente is None:
                continue
            payloads.append(siguiente)

        observed_at = store.utcnow()
        for payload in payloads:
            items = payload.get("items") or []
            rows = parse_products(payload, category["name"], observed_at)
            counts["skipped"] += len(items) - len(rows)
            for row in rows:
                store.upsert_product(conn, row)
                counts["products"] += 1

    return counts
