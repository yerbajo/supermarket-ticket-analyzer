"""CLI tests: offline, no network and no browser.

Every chain ingest is monkeypatched, so these tests only exercise the wiring:
argument parsing, the Spanish summary lines, the exit codes and the report.
"""

import pytest

import tickets_cli
from supermarket import dia, mas, mercadona
from tickets import parsing


def _fake_ingest(counts, calls=None):
    def fake(conn, **kwargs):
        if calls is not None:
            calls.update(kwargs)
        return counts

    return fake


def _boom(message):
    def fake(*args, **kwargs):
        raise dia.DiaError(message)

    return fake


def _run(capsys, *argv):
    code = tickets_cli.main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out + captured.err


def test_estado_on_empty_database(tmp_path, capsys):
    code, out = _run(capsys, "estado", "--db", str(tmp_path / "vacia.db"))

    assert code == 0
    assert "Tickets" in out
    assert "0" in out
    assert "Mercadona" not in out  # nothing invented on an empty store


def test_informe_on_empty_database(tmp_path, capsys):
    salida = tmp_path / "informe.html"
    code, out = _run(
        capsys, "informe", "--db", str(tmp_path / "vacia.db"), "--salida", str(salida)
    )

    assert code == 0
    assert salida.exists()
    assert str(salida) in out


def test_informe_creates_the_missing_parent_directory(tmp_path, capsys):
    salida = tmp_path / "sub" / "informe.html"
    code, _ = _run(
        capsys, "informe", "--db", str(tmp_path / "vacia.db"), "--salida", str(salida)
    )

    assert code == 0
    assert salida.exists()


def test_precios_mercadona_reports_the_fake_counts(tmp_path, monkeypatch, capsys):
    calls = {}
    monkeypatch.setattr(
        mercadona,
        "ingest_mercadona",
        _fake_ingest({"categories": 3, "leaves": 5, "products": 7, "skipped": 1}, calls),
    )

    code, out = _run(
        capsys, "precios", "--cadena", "mercadona", "--db", str(tmp_path / "t.db"),
        "--pausa", "0.1",
    )

    assert code == 0
    assert "Mercadona: 7 productos" in out
    assert calls["sleep_seconds"] == 0.1


def test_precios_dia_error_is_reported_without_traceback(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        dia, "ingest_dia", _boom("Could not launch Chromium: run under xvfb-run -a.")
    )

    code, out = _run(capsys, "precios", "--cadena", "dia", "--db", str(tmp_path / "t.db"))

    assert code == 1
    assert "xvfb-run" in out
    assert "Traceback" not in out


def test_precios_todas_keeps_going_after_one_failure(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        mercadona,
        "ingest_mercadona",
        _fake_ingest({"categories": 1, "leaves": 2, "products": 3, "skipped": 0}),
    )
    monkeypatch.setattr(
        mas,
        "ingest_mas",
        _fake_ingest(
            {"categories": 2, "products": 4, "skipped": 0, "requests": 5, "stopped": False}
        ),
    )
    monkeypatch.setattr(dia, "ingest_dia", _boom("Dia said no."))

    code, out = _run(capsys, "precios", "--cadena", "todas", "--db", str(tmp_path / "t.db"))

    assert code == 1
    assert "Mercadona: 3 productos" in out
    assert "MAS: 4 productos" in out
    assert "Día" in out


def test_dia_walk_reuses_a_single_browser(tmp_path, monkeypatch, capsys):
    """Regression: a browser per request would restart Chromium on every page."""
    opened = []

    class FakeBrowser:
        def __init__(self):
            self.calls = []
            self.closed = False
            opened.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            self.closed = True
            return False

        def fetch_json(self, path):
            self.calls.append(path)
            return {"items": [], "pagination": {"page_size": 20}, "total_items": 0}

    def fake_ingest(conn, fetch_json, **kwargs):
        fetch_json("/api/v1/tree")
        fetch_json("/api/v1/page")
        fetch_json("/api/v1/page2")
        return {"categories": 1, "products": 0, "skipped": 0, "pages": 3}

    monkeypatch.setattr(dia, "DiaBrowser", FakeBrowser)
    monkeypatch.setattr(dia, "ingest_dia", fake_ingest)

    code, out = _run(capsys, "precios", "--cadena", "dia", "--db", str(tmp_path / "t.db"))

    assert code == 0
    assert len(opened) == 1  # one browser, not one per request
    assert len(opened[0].calls) == 3
    assert opened[0].closed is True  # the walk closed it


def test_unknown_chain_is_rejected_by_argparse(tmp_path):
    with pytest.raises(SystemExit) as excinfo:
        tickets_cli.main(["precios", "--cadena", "lidl", "--db", str(tmp_path / "t.db")])

    assert excinfo.value.code == 2


def test_there_is_a_tickets_subcommand(tmp_path, capsys):
    code, out = _run(capsys, "tickets", "--inbox", str(tmp_path), "--db", str(tmp_path / "t.db"))

    assert code == 0
    assert "PDFs" in out or "pdf" in out.lower()


def _parsed_ticket():
    return {
        "chain": "MAS",
        "ticket_number": "261012/043/S/103/024205",
        "purchased_at": "2026-10-12T11:22:15",
        "total": 22.86,
        "lines_total": 22.86,
        "mismatch": False,
        "unverified": 0,
        "skipped": 0,
        "lines": [
            {"raw_text": "BEBIDA SOJA S/AZ 1L", "qty": 8.0, "price_paid": 0.75,
             "total_paid": 6.00, "purchase_unit": "ud"},
        ],
    }


def test_tickets_imports_and_then_reports_already_imported(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(tickets_cli.parsing, "parse_pdf", lambda path: _parsed_ticket())
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "46.pdf").write_bytes(b"%PDF-1.4 fake")
    db = tmp_path / "t.db"

    code, out = _run(capsys, "tickets", "--inbox", str(inbox), "--db", str(db))
    assert code == 0
    assert "46.pdf" in out
    assert "261012/043/S/103/024205" in out
    assert "1 líneas" in out

    code, out = _run(capsys, "tickets", "--inbox", str(inbox), "--db", str(db))
    assert code == 0
    assert "ya importado" in out


def test_tickets_reports_a_failure_and_exits_1(tmp_path, monkeypatch, capsys):
    def boom(path):
        raise parsing.ParseError("no tiene capa de texto; necesita OCR")

    monkeypatch.setattr(tickets_cli.parsing, "parse_pdf", boom)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "foto.pdf").write_bytes(b"%PDF-1.4 fake")

    code, out = _run(capsys, "tickets", "--inbox", str(inbox), "--db", str(tmp_path / "t.db"))

    assert code == 1
    assert "foto.pdf" in out
    assert "OCR" in out


def test_tickets_uses_the_environment_inbox(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(tickets_cli.parsing, "parse_pdf", lambda path: _parsed_ticket())
    inbox = tmp_path / "entrada"
    inbox.mkdir()
    (inbox / "46.pdf").write_bytes(b"%PDF-1.4 fake")
    monkeypatch.setenv("TICKETS_INBOX", str(inbox))

    code, out = _run(capsys, "tickets", "--db", str(tmp_path / "t.db"))

    assert code == 0
    assert "46.pdf" in out


def test_tickets_descargar_mas_calls_the_account_download(tmp_path, monkeypatch, capsys):
    """`--descargar-mas` pulls the tickets from the account before importing."""
    from supermarket import mas_account

    llamadas = {}

    def falso_download(conn, inbox):
        llamadas["inbox"] = str(inbox)
        return {"found": 18, "downloaded": 18, "imported": 17, "already": 1, "failed": 0}

    monkeypatch.setattr(mas_account, "download_tickets", falso_download)
    inbox = tmp_path / "inbox"
    inbox.mkdir()

    code, out = _run(
        capsys, "tickets", "--descargar-mas", "--inbox", str(inbox), "--db", str(tmp_path / "t.db")
    )

    assert code == 0
    assert "18 tickets descargados de 18 disponibles, 17 nuevos, 1 ya estaban" in out
    assert llamadas["inbox"] == str(inbox)


def test_tickets_descargar_mas_reports_a_dead_session(tmp_path, monkeypatch, capsys):
    from supermarket import mas_account

    def falla(conn, inbox):
        raise mas_account.MasAccountError("la sesión puede haber caducado")

    monkeypatch.setattr(mas_account, "download_tickets", falla)

    code, out = _run(
        capsys, "tickets", "--descargar-mas", "--inbox", str(tmp_path), "--db", str(tmp_path / "t.db")
    )

    assert code == 1
    assert "caducado" in out
    assert "Traceback" not in out


def test_sesiones_reports_both_shops(monkeypatch, capsys):
    from supermarket import firefox

    def cookies(host_suffix, profile=None):
        return {"a": "x", "b": "y"} if "mas" in host_suffix else {"c": "z"}

    monkeypatch.setattr(firefox, "firefox_cookies", cookies)

    code, out = _run(capsys, "sesiones")

    assert code == 0
    assert "Supermercados MAS: sesión disponible en Firefox (2 cookies)" in out
    assert "Día: sesión disponible en Firefox (1 cookies)" in out


def test_sesiones_names_the_shop_that_needs_a_login(monkeypatch, capsys):
    from supermarket import firefox

    def cookies(host_suffix, profile=None):
        if "mas" in host_suffix:
            raise firefox.FirefoxSessionError("el perfil no tiene cookies de supermercadosmas.com")
        return {"c": "z"}

    monkeypatch.setattr(firefox, "firefox_cookies", cookies)

    code, out = _run(capsys, "sesiones")

    assert code == 1
    assert "Supermercados MAS: sin sesión" in out
    assert "inicia sesión" in out


def test_sesiones_with_navigator_checks_the_shops_for_real(monkeypatch, capsys):
    """Cookies present is not the same as a session that works."""
    from supermarket import dia_account, firefox, mas_account

    monkeypatch.setattr(firefox, "firefox_cookies", lambda *a, **k: {"a": "x"})
    monkeypatch.setattr(mas_account, "session_from_firefox", lambda *a, **k: object())
    monkeypatch.setattr(mas_account, "ticket_urls", lambda session: ["u1", "u2"])

    class SesionFalsa:
        fetch_json = staticmethod(lambda path: {})

    class BrowserFalso:
        def __enter__(self):
            return SesionFalsa()

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(dia_account, "browser_session", lambda *a, **k: BrowserFalso())
    monkeypatch.setattr(dia_account, "list_tickets", lambda fetch_json: [1, 2, 3])

    code, out = _run(capsys, "sesiones", "--navegador")

    assert code == 0
    assert "Supermercados MAS: 2 tickets a la vista" in out
    assert "Día: 3 tickets a la vista" in out


def test_sesiones_with_navigator_reports_a_broken_dia_session(monkeypatch, capsys):
    from supermarket import dia_account, firefox, mas_account

    monkeypatch.setattr(firefox, "firefox_cookies", lambda *a, **k: {"a": "x"})
    monkeypatch.setattr(mas_account, "session_from_firefox", lambda *a, **k: object())
    monkeypatch.setattr(mas_account, "ticket_urls", lambda session: ["u1"])

    class BrowserRoto:
        def __enter__(self):
            raise dia_account.DiaAccountError("la sesión de Día ya no vale")

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(dia_account, "browser_session", lambda *a, **k: BrowserRoto())

    code, out = _run(capsys, "sesiones", "--navegador")

    assert code == 1
    assert "Día: la sesión no funciona" in out
    assert "inicia sesión" in out


def test_tickets_descargar_dia_can_bring_the_pictures_in_the_same_session(tmp_path, monkeypatch, capsys):
    """Dia's session is short lived: tickets and pictures share one browser."""
    from supermarket import dia_account
    from tickets import images

    abiertas = []

    class SesionFalsa:
        fetch_json = staticmethod(lambda path: {})
        fetch_bytes = staticmethod(lambda url: (b"\xff\xd8\xff\xe0x", "image/jpeg"))

    class BrowserFalso:
        def __enter__(self):
            abiertas.append(1)
            return SesionFalsa()

        def __exit__(self, *exc):
            return False

    fotos = {}

    def falso_import(conn, fetch_json):
        return {"found": 2, "imported": 1, "already": 1, "lines": 4, "failed": 0}

    def falso_download(conn, raiz, fetchers, **kwargs):
        fotos["fetchers"] = sorted(fetchers)
        fotos["chains"] = kwargs.get("chains")
        return {"pending": 3, "downloaded": 3, "skipped": 0, "failed": 0, "bytes": 10, "no_fetcher": 0}

    monkeypatch.setattr(dia_account, "browser_session", lambda *a, **k: BrowserFalso())
    monkeypatch.setattr(dia_account, "import_tickets", falso_import)
    monkeypatch.setattr(images, "download_images", falso_download)

    code, out = _run(
        capsys, "tickets", "--descargar-dia", "--imagenes", "--solo-comprados",
        "--inbox", str(tmp_path), "--db", str(tmp_path / "t.db"),
    )

    assert code == 0
    assert len(abiertas) == 1  # una sola sesión de navegador
    assert "Día (cuenta): 1 tickets nuevos de 2 disponibles" in out
    assert "Día (fotos): 3 descargadas de 3 pendientes" in out
    assert fotos["fetchers"] == ["Dia"]
    assert fotos["chains"] == ["Dia"]
