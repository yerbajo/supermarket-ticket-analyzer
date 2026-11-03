"""OCR tests: the system-tool checks and the render/read seams, all offline."""

import pytest
from PIL import Image

from tickets import ocr, parsing


def test_require_tools_names_what_is_missing(monkeypatch):
    monkeypatch.setattr(ocr.shutil, "which", lambda tool: None if tool == "tesseract" else "/usr/bin/x")

    with pytest.raises(parsing.ParseError) as excinfo:
        ocr.require_tools()

    message = str(excinfo.value)
    assert "tesseract" in message
    assert "tesseract-ocr-spa" in message  # the exact package to install
    assert "pdftoppm" not in message.split("instálalo")[0]  # pdftoppm is present here


def test_missing_tools_reports_the_absent_ones(monkeypatch):
    monkeypatch.setattr(ocr.shutil, "which", lambda tool: None)
    assert sorted(ocr.missing_tools()) == ["pdftoppm", "tesseract"]


def test_render_pdf_pages_sorts_by_page_number(monkeypatch, tmp_path):
    def fake_run(command, capture_output, check):
        prefix = command[-1]
        # pdftoppm writes page-1.png, page-2.png...; here out of order on purpose.
        for name in ("page-2.png", "page-10.png", "page-1.png"):
            (tmp_path / name).write_bytes(b"png")

        class Result:
            returncode = 0
            stderr = b""

        return Result()

    monkeypatch.setattr(ocr.subprocess, "run", fake_run)
    monkeypatch.setattr(ocr, "require_tools", lambda: None)

    pages = ocr.render_pdf_pages("ticket.pdf", out_dir=tmp_path)

    assert [page.name for page in pages] == ["page-1.png", "page-2.png", "page-10.png"]


def test_render_pdf_pages_reports_a_pdftoppm_failure(monkeypatch, tmp_path):
    class Result:
        returncode = 1
        stderr = b"Syntax Error: no pages"

    monkeypatch.setattr(ocr.subprocess, "run", lambda *a, **k: Result())
    monkeypatch.setattr(ocr, "require_tools", lambda: None)

    with pytest.raises(parsing.ParseError) as excinfo:
        ocr.render_pdf_pages("ticket.pdf", out_dir=tmp_path)

    assert "pdftoppm" in str(excinfo.value)
    assert "no pages" in str(excinfo.value)


def test_image_text_reads_a_real_image(monkeypatch, tmp_path):
    png = tmp_path / "page-1.png"
    Image.new("RGB", (120, 40), "white").save(png)
    monkeypatch.setattr(ocr, "require_tools", lambda: None)

    text = ocr.image_text(png)

    assert isinstance(text, str)  # blank image: content is irrelevant, no crash


def test_image_text_wraps_a_tesseract_failure(monkeypatch, tmp_path):
    png = tmp_path / "page-1.png"
    Image.new("RGB", (10, 10), "white").save(png)
    monkeypatch.setattr(ocr, "require_tools", lambda: None)

    import pytesseract

    def boom(*args, **kwargs):
        raise RuntimeError("tesseract exploded")

    monkeypatch.setattr(pytesseract, "image_to_string", boom)

    with pytest.raises(parsing.ParseError) as excinfo:
        ocr.image_text(png)

    assert "tesseract exploded" in str(excinfo.value)


def test_pdf_text_joins_the_pages(monkeypatch, tmp_path):
    monkeypatch.setattr(ocr, "render_pdf_pages", lambda path, dpi: [tmp_path / "a", tmp_path / "b"])
    monkeypatch.setattr(ocr, "image_text", lambda png, lang, psm: f"texto de {png.name}")

    assert ocr.pdf_text("ticket.pdf") == "texto de a\ntexto de b"
