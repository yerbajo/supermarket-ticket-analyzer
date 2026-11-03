"""Unit-normalisation tests: price + size text -> (unit_price, unit_label)."""

import pytest

from tickets.units import extract_size, normalize_unit


@pytest.mark.parametrize(
    "size_text, expected_value, expected_label",
    [
        ("1 l", 1.0, "l"),
        ("500 g", 0.5, "kg"),
        ("6 x 33 cl", 1.98, "l"),
        ("Pack 2 x 1,5 l", 3.0, "l"),
        ("2 x 500 gr", 1.0, "kg"),
        ("6 ud", 6.0, "ud"),
        ("unidad", 1.0, "ud"),
    ],
)
def test_extract_size_parses_known_sizes(size_text, expected_value, expected_label):
    value, label = extract_size(size_text)
    assert label == expected_label
    assert value == pytest.approx(expected_value, rel=1e-9)


@pytest.mark.parametrize(
    "size_text",
    ["Pack ahorro", "", None, "granel", "Bandeja 400"],
)
def test_extract_size_never_guesses(size_text):
    assert extract_size(size_text) == (None, None)


@pytest.mark.parametrize(
    "price, size_text, expected_price, expected_label",
    [
        (2.00, "1 l", 2.00, "l"),
        (1.50, "500 g", 3.00, "kg"),
        (5.00, "Bandeja 1 kg", 5.00, "kg"),
        (1.00, "100 ml", 10.00, "l"),
        (3.00, "6 x 33 cl", 3.00 / 1.98, "l"),
        (4.50, "Pack 2 x 1,5 l", 1.50, "l"),
        (6.00, "6 ud", 1.00, "ud"),
        (3.60, "1 ud", 3.60, "ud"),
        # Abreviaturas y palabras que aparecen en tickets y catalogos espanoles.
        (2.50, "500 gr", 5.00, "kg"),
        (2.49, "tarrina 125gr", 19.92, "kg"),
        (3.45, "vasito 225 gr", 3.45 / 0.225, "kg"),
        (2.00, "125 grs", 16.00, "kg"),
        (3.00, "250 gramos", 12.00, "kg"),
        (3.00, "1,5 lt", 2.00, "l"),
        (3.00, "2 lts", 1.50, "l"),
        (3.00, "1 litro", 3.00, "l"),
        (3.00, "3 litros", 1.00, "l"),
        (3.00, "33 cc", 3.00 / 0.033, "l"),
        (1.00, "2 x 500 gr", 1.00, "kg"),
    ],
)
def test_normalize_parses_known_sizes(price, size_text, expected_price, expected_label):
    unit_price, unit_label = normalize_unit(price, size_text)
    assert unit_label == expected_label
    assert unit_price == pytest.approx(expected_price, rel=1e-4)


@pytest.mark.parametrize(
    "price, size_text",
    [
        (2.00, "Pack ahorro"),
        (2.00, ""),
        (2.00, None),
        (None, "1 l"),
        (2.00, "granel"),
        # Un numero sin unidad no debe interpretarse como unidades: seria inventar.
        (13.99, "Bandeja de turron EL ALMENDRO 400"),
    ],
)
def test_normalize_never_guesses_unparseable(price, size_text):
    assert normalize_unit(price, size_text) == (None, None)


@pytest.mark.parametrize(
    "size_text",
    [
        "Champú ELVIVE total repair 5 l'oréal paris 3",  # brand, not five litres
        "Gel 3 l’oréal",
        "Crema 2 l'or",
    ],
)
def test_a_brand_with_an_apostrophe_is_not_a_size(size_text):
    """A real MAS catalogue name read as '5 litres' until this was found."""
    assert normalize_unit(4.40, size_text) == (None, None)
    assert extract_size(size_text) == (None, None)


@pytest.mark.parametrize(
    "size_text, esperado",
    [("1 l", (1.0, "l")), ("500 gr", (0.5, "kg")), ("Pack 2 x 1,5 l", (3.0, "l")), ("33 cl", (0.33, "l"))],
)
def test_real_sizes_still_parse(size_text, esperado):
    assert extract_size(size_text) == esperado


@pytest.mark.parametrize(
    "size_text, esperado",
    [
        ("YOGUR SABOR DIA P-16", (16.0, "ud")),      # the ticket's way of writing a pack
        ("ANCHOA A.VEGETAL P-3", (3.0, "ud")),
        ("YOGUR P4", (4.0, "ud")),
        ("Bebida P 6", (6.0, "ud")),
        # A real measure still wins over the pack notation.
        ("Yogur 4 x 125 g P-16", (0.5, "kg")),
    ],
)
def test_pack_notation_is_read_as_units(size_text, esperado):
    assert extract_size(size_text) == esperado


def test_a_pack_never_matches_a_different_pack():
    """The 16-pack the ticket names must not land on the 4 x 125 g catalogue product."""
    from tickets.matching import _sizes_conflict

    assert _sizes_conflict(extract_size("YOGUR SABOR DIA P-16"), extract_size("Yogur sabor plátano Dia Láctea 4 x 125 g"))
