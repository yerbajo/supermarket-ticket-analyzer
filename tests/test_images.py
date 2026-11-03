"""Image download tests: offline, with a fake fetcher and a temporary root."""

import pytest

from tickets import images, store

JPEG = b"\xff\xd8\xff\xe0" + b"0" * 40
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 40
HTML = b"<!DOCTYPE html><html>403 Access Denied</html>"


def _product(conn, external_id, chain="Mercadona", image_url="https://cdn.example/p.jpg", name=None):
    return store.upsert_product(
        conn,
        {
            "chain": chain,
            "external_id": external_id,
            "name": name or f"Producto {external_id}",
            "price": 1.0,
            "unit_price": 1.0,
            "unit_label": "ud",
            "image_url": image_url,
            "observed_at": "2026-10-12T00:00:00Z",
        },
    )


def _fetcher(payloads, seen=None):
    def fetch_bytes(url):
        if seen is not None:
            seen.append(url)
        return payloads.get(url, (None, None))

    return fetch_bytes


# --------------------------------------------------------------------------- #
# Byte checks
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("content", [JPEG, PNG, b"RIFF0000WEBPVP8 ", b"GIF89a...", b"\x00\x00\x00\x18ftypavif"])
def test_real_image_formats_are_accepted(content):
    assert images._looks_like_image(content) is True


@pytest.mark.parametrize("content", [HTML, b"", b"{\"error\": \"no\"}", b"%PDF-1.4"])
def test_anything_that_is_not_an_image_is_refused(content):
    assert images._looks_like_image(content) is False


@pytest.mark.parametrize(
    "url, content_type, esperado",
    [
        ("https://cdn/p.jpg", "image/jpeg", ".jpg"),
        ("https://cdn/p", "image/png", ".png"),
        ("https://cdn/p.webp", None, ".webp"),
        ("https://cdn/p?id=1", "image/webp", ".webp"),
        ("https://cdn/p", "text/html", ".jpg"),
    ],
)
def test_extension_for(url, content_type, esperado):
    assert images.extension_for(url, content_type) == esperado


# --------------------------------------------------------------------------- #
# Downloading
# --------------------------------------------------------------------------- #


def test_downloads_writes_files_and_records_them(conn, tmp_path):
    _product(conn, "1", image_url="https://cdn.example/uno.jpg")
    _product(conn, "2", image_url="https://cdn.example/dos.png")
    fetch = _fetcher(
        {"https://cdn.example/uno.jpg": (JPEG, "image/jpeg"),
         "https://cdn.example/dos.png": (PNG, "image/png")}
    )

    counts = images.download_images(
        conn, tmp_path / "img", {"Mercadona": fetch}, chains=["Mercadona"], pause=0
    )

    assert counts == {"pending": 2, "downloaded": 2, "skipped": 0, "failed": 0,
                      "bytes": len(JPEG) + len(PNG), "no_fetcher": 0}
    assert (tmp_path / "img" / "Mercadona" / "1.jpg").read_bytes() == JPEG
    assert (tmp_path / "img" / "Mercadona" / "2.png").read_bytes() == PNG
    guardados = {
        row["external_id"]: row["image_file"]
        for row in conn.execute("SELECT external_id, image_file FROM products")
    }
    assert guardados == {"1": "Mercadona/1.jpg", "2": "Mercadona/2.png"}


def test_a_second_pass_has_nothing_to_do(conn, tmp_path):
    _product(conn, "1")
    fetch = _fetcher({"https://cdn.example/p.jpg": (JPEG, "image/jpeg")})
    images.download_images(conn, tmp_path / "img", {"Mercadona": fetch}, pause=0)

    counts = images.download_images(conn, tmp_path / "img", {"Mercadona": fetch}, pause=0)

    assert counts["pending"] == 0
    assert counts["downloaded"] == 0


def test_an_error_page_is_counted_and_not_stored(conn, tmp_path):
    _product(conn, "1")
    fetch = _fetcher({"https://cdn.example/p.jpg": (HTML, "text/html")})

    counts = images.download_images(conn, tmp_path / "img", {"Mercadona": fetch}, pause=0)

    assert counts["failed"] == 1
    assert counts["downloaded"] == 0
    assert not list((tmp_path / "img").rglob("*.jpg"))
    assert conn.execute("SELECT image_file FROM products").fetchone()["image_file"] is None


def test_an_oversized_response_is_refused(conn, tmp_path):
    _product(conn, "1")
    grande = JPEG + b"0" * (images.MAX_BYTES + 1)
    fetch = _fetcher({"https://cdn.example/p.jpg": (grande, "image/jpeg")})

    counts = images.download_images(conn, tmp_path / "img", {"Mercadona": fetch}, pause=0)

    assert counts["failed"] == 1
    assert counts["downloaded"] == 0


def test_a_chain_without_a_fetcher_is_counted_not_guessed(conn, tmp_path):
    _product(conn, "1", chain="MAS", image_url="https://mas.example/p.jpg")

    counts = images.download_images(conn, tmp_path / "img", {}, chains=["MAS"], pause=0)

    assert counts["no_fetcher"] == 1
    assert counts["downloaded"] == 0


def test_only_bought_restricts_the_pass_to_products_in_tickets(conn, tmp_path):
    comprado = _product(conn, "1")
    _product(conn, "2")  # never bought
    store.upsert_ticket(
        conn,
        {
            "chain": "Mercadona",
            "ticket_number": "T-1",
            "purchased_at": "2026-10-12T11:22:15",
            "total": 1.0,
            "lines": [
                {"raw_text": "Producto 1", "qty": 1, "price_paid": 1.0,
                 "total_paid": 1.0, "purchase_unit": "ud", "product_id": comprado}
            ],
        },
    )
    fetch = _fetcher({"https://cdn.example/p.jpg": (JPEG, "image/jpeg")})

    counts = images.download_images(
        conn, tmp_path / "img", {"Mercadona": fetch}, only_bought=True, pause=0
    )

    assert counts["pending"] == 1
    assert counts["downloaded"] == 1
    filas = {row["external_id"]: row["image_file"] for row in conn.execute("SELECT external_id, image_file FROM products")}
    assert filas == {"1": "Mercadona/1.jpg", "2": None}


def test_the_limit_is_honoured(conn, tmp_path):
    for i in range(5):
        _product(conn, str(i))
    fetch = _fetcher({"https://cdn.example/p.jpg": (JPEG, "image/jpeg")})

    counts = images.download_images(conn, tmp_path / "img", {"Mercadona": fetch}, limit=2, pause=0)

    assert counts["pending"] == 2
    assert counts["downloaded"] == 2


def test_products_without_an_image_url_are_never_pending(conn, tmp_path):
    _product(conn, "1", image_url=None)

    counts = images.download_images(conn, tmp_path / "img", {}, pause=0)

    assert counts["pending"] == 0


def test_images_root_prefers_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("IMAGES_DIR", str(tmp_path / "fotos"))
    assert images.images_root("/otra/base/compras.db") == tmp_path / "fotos"

    monkeypatch.delenv("IMAGES_DIR")
    assert images.images_root("/otra/base/compras.db") == images.Path("/otra/base/images")


def test_large_pictures_are_reduced_before_being_stored(conn, tmp_path):
    """Dia serves full-resolution photos; a page needs a small JPEG."""
    import io
    import random

    from PIL import Image

    # Noise, not a flat colour: a flat 1500x1500 PNG compresses to a few KB and
    # would never reach the shrink threshold.
    lado = 800
    ruido = bytes(random.randrange(256) for _ in range(lado * lado * 3))
    buffer = io.BytesIO()
    Image.frombytes("RGB", (lado, lado), ruido).save(buffer, format="PNG")
    grande = buffer.getvalue()
    assert len(grande) > images.SHRINK_ABOVE_BYTES
    _product(conn, "1")
    fetch = _fetcher({"https://cdn.example/p.jpg": (grande, "image/png")})

    counts = images.download_images(conn, tmp_path / "img", {"Mercadona": fetch}, pause=0)

    guardada = tmp_path / "img" / "Mercadona" / "1.jpg"  # re-encoded, extension normalised
    assert guardada.exists()
    assert counts["downloaded"] == 1
    with Image.open(guardada) as reducida:
        assert max(reducida.size) <= images.MAX_SIDE
    assert guardada.stat().st_size < len(grande) / 4


def test_small_pictures_are_stored_untouched(conn, tmp_path):
    _product(conn, "1")
    fetch = _fetcher({"https://cdn.example/p.jpg": (JPEG, "image/jpeg")})

    images.download_images(conn, tmp_path / "img", {"Mercadona": fetch}, pause=0)

    assert (tmp_path / "img" / "Mercadona" / "1.jpg").read_bytes() == JPEG


def test_redo_forgets_the_recorded_pictures(conn, tmp_path):
    _product(conn, "1")
    fetch = _fetcher({"https://cdn.example/p.jpg": (JPEG, "image/jpeg")})
    images.download_images(conn, tmp_path / "img", {"Mercadona": fetch}, pause=0)

    counts = images.download_images(
        conn, tmp_path / "img", {"Mercadona": fetch}, pause=0, redo=True
    )

    assert counts["pending"] == 1
    assert counts["downloaded"] == 1
