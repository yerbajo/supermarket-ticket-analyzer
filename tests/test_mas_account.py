"""MAS account tests: offline. No network, no real Firefox profile, no cookies printed."""

import json
import pathlib
import sqlite3

import pytest

from supermarket import mas_account
from tickets import store

FIXTURE = pathlib.Path(__file__).resolve().parent / "fixtures" / "mas_ticket_46.txt"

TICKET_A = (
    "https://www.supermercadosmas.com/clubmascustomers/customer/getticketpdf/centre/043"
    "/date/12%2F10%2F2026/caja/103/numticket/46/"
)
TICKET_B = (
    "https://www.supermercadosmas.com/clubmascustomers/customer/getticketpdf/centre/043"
    "/date/06%2F10%2F2026/caja/102/numticket/197/"
)


def _profile(root, name, cookies=True):
    directorio = root / name
    directorio.mkdir(parents=True)
    if cookies:
        _cookies_db(directorio / "cookies.sqlite", [(".www.supermercadosmas.com", "PHPSESSID", "x")])
    return directorio


def _cookies_db(path, filas):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE moz_cookies (name TEXT, value TEXT, host TEXT, path TEXT, expiry INTEGER)")
    conn.executemany(
        "INSERT INTO moz_cookies (host, name, value, path, expiry) VALUES (?,?,?,?,?)",
        [(host, name, value, "/", 0) for host, name, value in filas],
    )
    conn.commit()
    conn.close()


class FakeResponse:
    def __init__(self, text="", content=b"", status=200):
        self.text = text
        self.content = content
        self.status_code = status


class FakeSession:
    """Serves the history pages and the ticket downloads from dictionaries."""

    def __init__(self, pages, files, failing=()):
        self.pages = pages
        self.files = files
        self.failing = set(failing)
        self.requested = []

    def get(self, url, timeout=None):
        self.requested.append(url)
        if url in self.pages:
            return FakeResponse(text=self.pages[url])
        if url in self.files:
            if url in self.failing:
                return FakeResponse(content=b"<html>not a pdf</html>")
            return FakeResponse(content=self.files[url])
        return FakeResponse(status=404)


def _history(links):
    return "<html><body>" + "".join(f'<a href="{link}">Ticket</a>' for link in links) + "</body></html>"


# --------------------------------------------------------------------------- #
# The Firefox profile and its cookies
# --------------------------------------------------------------------------- #


def test_find_profile_prefers_the_one_profiles_ini_marks_default(tmp_path):
    _profile(tmp_path, "otro.default")
    default = _profile(tmp_path, "preferido.default")
    (tmp_path / "profiles.ini").write_text(
        "[Profile0]\nName=default\nPath=preferido.default\nDefault=1\n", encoding="utf-8"
    )

    assert mas_account.find_profile(root=tmp_path) == default


def test_find_profile_without_any_cookie_database(tmp_path):
    (tmp_path / "vacio.default").mkdir()

    with pytest.raises(mas_account.MasAccountError) as excinfo:
        mas_account.find_profile(root=tmp_path)

    assert "Firefox" in str(excinfo.value)


def test_firefox_cookies_reads_only_the_requested_host(tmp_path):
    perfil = tmp_path / "perfil.default"
    perfil.mkdir()
    _cookies_db(
        perfil / "cookies.sqlite",
        [
            (".www.supermercadosmas.com", "PHPSESSID", "sesion"),
            (".supermercadosmas.com", "form_key", "clave"),
            (".dia.es", "PHPSESSID", "otra"),
            (".google.com", "NID", "rastreo"),
        ],
    )

    cookies = mas_account.firefox_cookies(profile=perfil)

    assert cookies == {"PHPSESSID": "sesion", "form_key": "clave"}
    assert "otra" not in cookies.values()


def test_firefox_cookies_without_a_session_names_the_fix(tmp_path):
    perfil = tmp_path / "perfil.default"
    perfil.mkdir()
    _cookies_db(perfil / "cookies.sqlite", [(".dia.es", "PHPSESSID", "otra")])

    with pytest.raises(mas_account.MasAccountError) as excinfo:
        mas_account.firefox_cookies(profile=perfil)

    assert "inicia sesión" in str(excinfo.value)


def test_session_from_firefox_sets_the_browser_user_agent(tmp_path):
    perfil = tmp_path / "perfil.default"
    perfil.mkdir()
    _cookies_db(perfil / "cookies.sqlite", [(".www.supermercadosmas.com", "PHPSESSID", "x")])

    session = mas_account.session_from_firefox(profile=perfil)

    assert "Firefox" in session.headers["User-Agent"]
    assert session.cookies.get("PHPSESSID") == "x"


# --------------------------------------------------------------------------- #
# Ticket URLs
# --------------------------------------------------------------------------- #


def test_ticket_urls_walks_the_pages_and_dedupes():
    pages = {
        mas_account.HISTORY_URL: _history([TICKET_A, TICKET_B]),
        f"{mas_account.HISTORY_URL}?p=2": _history([TICKET_B, TICKET_A]),
    }

    urls = mas_account.ticket_urls(FakeSession(pages, {}))

    assert urls == [TICKET_A, TICKET_B]  # newest first, no repeats


def test_ticket_urls_stops_at_the_first_empty_page():
    pages = {
        mas_account.HISTORY_URL: _history([TICKET_A]),
        f"{mas_account.HISTORY_URL}?p=2": _history([]),
        f"{mas_account.HISTORY_URL}?p=3": _history([TICKET_B]),
    }
    session = FakeSession(pages, {})

    urls = mas_account.ticket_urls(session)

    assert urls == [TICKET_A]
    assert f"{mas_account.HISTORY_URL}?p=3" not in session.requested


def test_ticket_urls_without_tickets_points_at_the_session():
    pages = {mas_account.HISTORY_URL: _history([])}

    with pytest.raises(mas_account.MasAccountError) as excinfo:
        mas_account.ticket_urls(FakeSession(pages, {}))

    assert "caducado" in str(excinfo.value) or "sesión" in str(excinfo.value)


@pytest.mark.parametrize(
    "url, expected",
    [
        (TICKET_A, "mas-20261012-103-046.pdf"),
        (TICKET_B, "mas-20261006-102-197.pdf"),
        ("https://www.supermercadosmas.com/otra-cosa.pdf", None),
    ],
)
def test_filename_for_is_sortable_and_stable(url, expected):
    nombre = mas_account.filename_for(url)
    if expected is None:
        assert nombre.endswith(".pdf")
    else:
        assert nombre == expected


# --------------------------------------------------------------------------- #
# Downloading
# --------------------------------------------------------------------------- #


def _patch_import(monkeypatch, inserted=True):
    llamadas = []

    def falso_import(conn, path):
        llamadas.append(Path(path).name)
        return {"chain": "MAS", "ticket_id": 1, "inserted": inserted, "lines": []}

    monkeypatch.setattr("tickets.parsing.import_ticket", falso_import)
    monkeypatch.setattr("tickets.matching.match_ticket_lines", lambda conn, chain: {"lines": 0})
    return llamadas


from pathlib import Path  # noqa: E402  (used by the helper above)


def test_download_tickets_saves_imports_and_counts(conn, tmp_path, monkeypatch):
    pages = {mas_account.HISTORY_URL: _history([TICKET_A, TICKET_B])}
    files = {TICKET_A: b"%PDF-1.4 primero", TICKET_B: b"%PDF-1.4 segundo"}
    importados = _patch_import(monkeypatch)

    counts = mas_account.download_tickets(
        conn, tmp_path / "inbox", session=FakeSession(pages, files)
    )

    assert counts == {"found": 2, "downloaded": 2, "imported": 2, "already": 0, "failed": 0}
    assert sorted(importados) == ["mas-20261006-102-197.pdf", "mas-20261012-103-046.pdf"]
    guardados = sorted(p.name for p in (tmp_path / "inbox").glob("*.pdf"))
    assert guardados == ["mas-20261006-102-197.pdf", "mas-20261012-103-046.pdf"]


def test_download_tickets_reports_already_imported(conn, tmp_path, monkeypatch):
    pages = {mas_account.HISTORY_URL: _history([TICKET_A])}
    _patch_import(monkeypatch, inserted=False)

    counts = mas_account.download_tickets(
        conn, tmp_path / "inbox", session=FakeSession(pages, {TICKET_A: b"%PDF-1.4"})
    )

    assert counts["already"] == 1
    assert counts["imported"] == 0


def test_one_broken_ticket_does_not_stop_the_rest(conn, tmp_path, monkeypatch):
    pages = {mas_account.HISTORY_URL: _history([TICKET_A, TICKET_B])}
    files = {TICKET_A: b"<html>no es un pdf</html>", TICKET_B: b"%PDF-1.4 bueno"}
    _patch_import(monkeypatch)

    counts = mas_account.download_tickets(
        conn, tmp_path / "inbox", session=FakeSession(pages, files, failing=[TICKET_A])
    )

    assert counts["failed"] == 1
    assert counts["downloaded"] == 1
    assert (tmp_path / "inbox" / "mas-20261006-102-197.pdf").exists()
    assert not (tmp_path / "inbox" / "mas-20261012-103-046.pdf").exists()
