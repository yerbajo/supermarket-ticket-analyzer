"""Offline tests for the Amazon.es search client and its unit-price parser.

Nothing here touches the network: the page is either the recorded fixture
(``tests/fixtures/amazon_search_results.html``, a public results page trimmed to
three blocks) or an injected ``fetch_page`` callable.
"""

from pathlib import Path

import pytest

from supermarket import amazon

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "amazon_search_results.html"


def _recorded_page():
    return FIXTURE.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# unit_price_from_text — the formats Amazon.es really printed (2026-10-12)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "text, expected",
    [
        ("0,30 €/100 ml", (3.0, "l")),      # per 100 ml -> per litre
        ("4,32€/100 ml", (43.2, "l")),      # no space before the euro sign
        ("1,86 € /l", (1.86, "l")),         # already per litre
        ("3,70 € / l", (3.7, "l")),         # spaces around the slash
        ("0,43 €/100 g", (4.3, "kg")),      # per 100 g -> per kilo
        ("2,70 € / kg", (2.70, "kg")),      # already per kilo
        ("0,33€/unidad", (0.33, "ud")),     # per item
        ("0,07 € /unidad", (0.07, "ud")),   # spaces around the slash
    ],
)
def test_unit_price_from_text_reads_the_real_formats(text, expected):
    assert amazon.unit_price_from_text(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "0,37 €/100",              # a hundred of nothing
        "5,00 €/rollos de papel",  # a unit we do not know
        "3,00 €",                  # no unit price at all
        "gratis",
        "",
        None,
    ],
)
def test_unit_price_from_text_refuses_what_it_cannot_read(text):
    assert amazon.unit_price_from_text(text) == (None, None)


# --------------------------------------------------------------------------- #
# parse_results — the recorded page
# --------------------------------------------------------------------------- #


def test_parse_results_reads_the_recorded_page():
    offers = amazon.parse_results(_recorded_page())

    assert [offer["asin"] for offer in offers] == ["B00L2IQOKO", "B00X9V2NGQ", "B01BMCT1ZY"]

    first = offers[0]
    assert "Asevi" in first["title"]
    assert first["price"] == 4.25
    assert first["unit_price"] == 1.86
    assert first["unit_label"] == "l"
    assert first["unit_price_text"] == "1,86 €/l"
    assert first["url"] == "https://www.amazon.es/dp/B00L2IQOKO"
    assert first["sponsored"] is False


def test_parse_results_flags_only_the_sponsored_block():
    offers = amazon.parse_results(_recorded_page())

    assert [offer["sponsored"] for offer in offers] == [False, True, False]
    assert offers[1]["asin"] == "B00X9V2NGQ"
    assert offers[1]["unit_price"] == 0.07
    assert offers[1]["unit_label"] == "ud"


def test_parse_results_converts_per_kilo_and_per_item():
    offers = amazon.parse_results(_recorded_page())

    assert offers[2]["unit_label"] == "kg"
    assert offers[2]["unit_price"] == 4.37


def test_parse_results_raises_on_a_page_without_results():
    """An empty list would read as "Amazon has nothing"; say the page changed."""
    with pytest.raises(amazon.AmazonError) as info:
        amazon.parse_results("<html><body><p>No hay resultados para tu búsqueda</p></body></html>")

    assert "cambiado" in str(info.value)


def test_parse_results_decodes_entities_in_the_title_and_unit_text():
    page = (
        '<div data-asin="A1" data-component-type="s-search-result">'
        '<h2><span>Champú H&amp;S 2x1L</span></h2>'
        '<span class="a-price"><span class="a-offscreen">9,95&nbsp;€</span></span>'
        '<span class="a-size-base">(1,00&nbsp;€/100&nbsp;ml)</span>'
        "</div>"
    )

    offer = amazon.parse_results(page)[0]

    assert offer["title"] == "Champú H&S 2x1L"
    assert offer["unit_price_text"] == "1,00 €/100 ml"
    assert (offer["unit_price"], offer["unit_label"]) == (10.0, "l")


# --------------------------------------------------------------------------- #
# search — no network beyond the injected fetcher
# --------------------------------------------------------------------------- #


def test_search_uses_the_injected_fetcher_and_builds_the_search_url():
    seen = []

    def fetch_page(url):
        seen.append(url)
        return _recorded_page()

    offers = amazon.search("detergente liquido", fetch_page, max_results=2, min_interval=0)

    assert [offer["asin"] for offer in offers] == ["B00L2IQOKO", "B00X9V2NGQ"]
    assert seen == [amazon.search_url("detergente liquido")]


def test_search_bounds_the_number_of_results():
    offers = amazon.search(
        "detergente", lambda url: _recorded_page(), max_results=1, min_interval=0
    )

    assert len(offers) == 1
