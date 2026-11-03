"""Ticket parsing tests: offline, against the extracted MAS text fixture.

The fixture is the real ticket text with the card-payment block removed; the
tests never open ``samples/inbox`` (gitignored) and never touch the network.
"""

import pathlib

import pypdf
import pytest

from tickets import ocr, parsing

FIXTURE = pathlib.Path(__file__).resolve().parent / "fixtures" / "mas_ticket_46.txt"
DIA_FIXTURE = pathlib.Path(__file__).resolve().parent / "fixtures" / "dia_ticket_ocr.txt"

MAS_HEADER_ONLY = (
    "SUPERMERCADOS MAS\n"
    "12/10/2026 11:22:15-TLF.954644884-IVA INCLUIDO\n"
    "TOTAL COMPRA. . . . .  22.86\n"
    "FACTURA SIMPLIFICADA 261012/043/S/103/024205\n"
)


@pytest.fixture
def mas_text():
    return FIXTURE.read_text(encoding="utf-8")


@pytest.fixture
def dia_text():
    return DIA_FIXTURE.read_text(encoding="utf-8")


def _blank_pdf(path):
    """A one-page PDF with no text layer, like an iOS screenshot export."""
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=200, height=200)
    with open(path, "wb") as handle:
        writer.write(handle)
    return path


def test_header_fields_of_the_real_ticket(mas_text):
    parsed = parsing.parse_ticket(mas_text)

    assert parsed["chain"] == "MAS"
    assert parsed["purchased_at"] == "2026-10-12T11:22:15"
    assert parsed["ticket_number"] == "261012/043/S/103/024205"
    assert parsed["total"] == pytest.approx(22.86)


def test_product_lines_of_the_real_ticket(mas_text):
    lines = parsing.parse_ticket(mas_text)["lines"]

    assert len(lines) == 8
    by_name = {line["raw_text"].split()[0]: line for line in lines}

    soja = by_name["BEBIDA"]
    assert soja["raw_text"] == "BEBIDA SOJA S/AZ 1L"
    assert soja["qty"] == pytest.approx(8.0)
    assert soja["price_paid"] == pytest.approx(0.75)
    assert soja["total_paid"] == pytest.approx(6.00)
    assert soja["purchase_unit"] == "ud"

    melon = by_name["MELON"]
    assert melon["raw_text"] == "MELON PIEL DE SAPO"
    assert melon["qty"] == pytest.approx(1.527)
    assert melon["price_paid"] == pytest.approx(1.65)
    assert melon["total_paid"] == pytest.approx(2.52)
    assert melon["purchase_unit"] == "kg"

    romani = by_name["ROMANILLAS"]
    assert romani["qty"] == pytest.approx(2.0)
    assert romani["price_paid"] == pytest.approx(1.10)
    assert romani["total_paid"] == pytest.approx(2.20)
    assert romani["purchase_unit"] == "ud"


def test_line_totals_agree_with_the_ticket_total(mas_text):
    parsed = parsing.parse_ticket(mas_text)

    assert parsed["lines_total"] == pytest.approx(22.86)
    assert parsed["mismatch"] is False


def test_payment_and_total_lines_are_not_products(mas_text):
    lines = parsing.parse_ticket(mas_text)["lines"]

    assert all("TARJETA" not in line["raw_text"] for line in lines)
    assert all("TOTAL" not in line["raw_text"] for line in lines)


def test_unknown_chain_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(parsing, "extract_pdf_text", lambda path: "TIENDA RANDOM\n1x 1.00 1.00 A\n")

    with pytest.raises(parsing.ParseError):
        parsing.parse_pdf(tmp_path / "x.pdf")


def test_image_pdf_without_text_layer_raises(tmp_path):
    path = _blank_pdf(tmp_path / "foto.pdf")

    with pytest.raises(parsing.ParseError) as excinfo:
        parsing.extract_pdf_text(path)

    message = str(excinfo.value)
    assert "foto.pdf" in message
    assert "OCR" in message


def test_ticket_without_product_lines_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(parsing, "extract_pdf_text", lambda path: MAS_HEADER_ONLY)

    with pytest.raises(parsing.ParseError):
        parsing.parse_pdf(tmp_path / "vacio.pdf")


def test_parse_pdf_end_to_end_on_fixture(tmp_path, monkeypatch):
    text = FIXTURE.read_text(encoding="utf-8")
    monkeypatch.setattr(parsing, "extract_pdf_text", lambda path: text)

    parsed = parsing.parse_pdf(tmp_path / "46.pdf")

    assert parsed["chain"] == "MAS"
    assert len(parsed["lines"]) == 8


# --------------------------------------------------------------------------- #
# Dia: real OCR output, where the amounts need normalising.
# --------------------------------------------------------------------------- #


def test_dia_header_fields_of_the_real_ticket(dia_text):
    parsed = parsing.parse_ticket(dia_text)

    assert parsed["chain"] == "Dia"
    assert parsed["ticket_number"] == "0234402-00295869"
    assert parsed["purchased_at"] == "2026-10-09T13:43:00"
    assert parsed["total"] == pytest.approx(36.95)


def test_dia_reads_every_line_of_the_real_ticket(dia_text):
    parsed = parsing.parse_ticket(dia_text)

    assert len(parsed["lines"]) == 10
    assert parsed["unverified"] == 0
    assert parsed["skipped"] == 0
    assert parsed["lines_total"] == pytest.approx(36.95)
    assert parsed["mismatch"] is False


def test_dia_joins_names_that_wrap_onto_two_lines(dia_text):
    names = [line["raw_text"] for line in parsing.parse_ticket(dia_text)["lines"]]

    assert "CANELONES DE CARNE" in names
    assert "YOGUR GRIEGO LIGERO" in names
    assert "PECHUGA ENTERA FAMIL" in names
    # The column header must never end up glued to the first product.
    assert not any("DESCRIPCIÓN" in name or "CANTIDAD" in name for name in names)


def test_dia_weighed_line_keeps_its_own_unit_price(dia_text):
    line = next(
        item for item in parsing.parse_ticket(dia_text)["lines"] if "PECHUGA" in item["raw_text"]
    )

    assert line["qty"] == pytest.approx(1.149)
    assert line["price_paid"] == pytest.approx(6.49)  # the €/kg printed by the shop
    assert line["total_paid"] == pytest.approx(7.46)
    assert line["purchase_unit"] == "kg"


def test_dia_recovers_the_amounts_ocr_mangled(dia_text):
    """The real OCR text contains ``269€``, ``1,779€`` and ``746€``."""
    lines = parsing.parse_ticket(dia_text)["lines"]
    by_name = {line["raw_text"].split()[0]: line for line in lines}

    assert by_name["NARANJA"]["total_paid"] == pytest.approx(2.69)
    assert by_name["CANELONES"]["price_paid"] == pytest.approx(1.79)  # read as 1,779
    assert by_name["PECHUGA"]["total_paid"] == pytest.approx(7.46)  # read as 746


@pytest.mark.parametrize(
    "token, expected_first",
    [("2,69 €", 2.69), ("269€", 2.69), ("1,779", 1.79), ("6,555", 6.55), ("1234", 12.34)],
)
def test_amount_candidates_prefer_the_canonical_reading(token, expected_first):
    assert parsing.amount_candidates(token)[0] == pytest.approx(expected_first)


@pytest.mark.parametrize("token", ["", "9", "0,5", "abc", "1.234,56", "€"])
def test_amount_candidates_refuse_ambiguous_tokens(token):
    assert parsing.amount_candidates(token) == []


def test_ocred_line_that_cannot_be_read_is_counted_not_guessed():
    text = (
        "Factura simplificada N* 1-2\n"
        "09/10/2026 13:43 N* de Tienda 2344\n"
        "Total a pagar........ 3,58 €\n"
        "Productos vendidos por Dia\n"
        "DESCRIPCIÓN CANTIDAD PRECIO KG TOTAL\n"
        "CANELONES DE CARNE 2 ud 1,79 € 3,58 € B\n"
        "PAN BASURA 2 ud 9 1,00 € B\n"  # an amount with a single digit: unreadable
        "Total venta DÍA... 3,58 €\n"
    )
    parsed = parsing.parse_ticket(text)

    assert len(parsed["lines"]) == 1
    assert parsed["skipped"] == 1
    assert parsed["lines"][0]["raw_text"] == "CANELONES DE CARNE"


def test_line_that_does_not_check_arithmetically_is_flagged():
    text = (
        "Factura simplificada N* 1-2\n"
        "Total a pagar........ 10,00 €\n"
        "Productos vendidos por Dia\n"
        "SIN CUADRE 2 ud 3,00 € 7,00 € B\n"  # 2 x 3,00 is 6,00, not 7,00
        "Total venta DÍA... 10,00 €\n"
    )
    parsed = parsing.parse_ticket(text)

    assert len(parsed["lines"]) == 1
    assert parsed["unverified"] == 1
    assert parsed["mismatch"] is True  # 7,00 against the printed 10,00


def test_parse_pdf_ocrs_a_ticket_without_text_layer(tmp_path, monkeypatch):
    """A picture PDF goes through OCR and is then parsed as usual."""
    text = DIA_FIXTURE.read_text(encoding="utf-8")

    def no_text_layer(path):
        raise parsing.ImageOnlyPdf(f"{path}: sin capa de texto")

    monkeypatch.setattr(parsing, "extract_pdf_text", no_text_layer)
    monkeypatch.setattr(ocr, "pdf_text", lambda path, dpi, lang: text)

    parsed = parsing.parse_pdf(tmp_path / "foto.pdf")

    assert parsed["chain"] == "Dia"
    assert len(parsed["lines"]) == 10
    assert parsed["mismatch"] is False
