"""Read a logged-in browser session out of Firefox's own cookie store.

Firefox keeps its cookies **unencrypted** in ``cookies.sqlite`` (``moz_cookies``),
so a session the human already opened in the browser can be reused by a script
without pasting a single cookie anywhere. That is what makes the account
connectors usable: Supermercados MAS and Dia both need a session, and neither
needs the human to copy tokens around.

The profile is copied (with its write-ahead log) before reading, because Firefox
keeps the database locked while it runs. Snap Firefox keeps its profile under
``~/snap/firefox`` instead of ``~/.mozilla``, and both roots are searched.

A cookie is a credential: values are handed to the caller's session and never
logged, printed or written outside a temporary directory.
"""

from __future__ import annotations

import re
import shutil
import sqlite3
import tempfile
from pathlib import Path

#: Where Firefox keeps a profile, in search order: distro package, then snap.
PROFILE_ROOTS = (
    Path.home() / ".mozilla" / "firefox",
    Path.home() / "snap" / "firefox" / "common" / ".mozilla" / "firefox",
)


class FirefoxSessionError(RuntimeError):
    """No profile, no cookie database, or no session for the requested site."""


def _candidate_profiles(root):
    """Profile directories under ``root`` that hold a cookie database."""
    if not root.is_dir():
        return []
    with_cookies = [
        path for path in sorted(root.iterdir()) if (path / "cookies.sqlite").is_file()
    ]
    ini = root / "profiles.ini"
    if ini.is_file():
        for match in re.finditer(r"^Path=(.+)$", ini.read_text(errors="replace"), re.M):
            candidate = root / match.group(1).strip()
            if candidate in with_cookies:
                with_cookies.remove(candidate)
                with_cookies.insert(0, candidate)  # the default profile first
    return with_cookies


def find_profile(root=None) -> Path:
    """The Firefox profile that has a cookie database."""
    roots = (Path(root),) if root else PROFILE_ROOTS
    for candidate_root in roots:
        profiles = _candidate_profiles(candidate_root)
        if profiles:
            return profiles[0]
    raise FirefoxSessionError(
        "no se encontró un perfil de Firefox con cookies; abre Firefox e inicia sesión en el "
        "sitio, o pasa la ruta del perfil a mano"
    )


def firefox_cookies(host_suffix, profile=None) -> dict:
    """Cookies for ``host_suffix`` from the Firefox profile, as ``{name: value}``."""
    directorio = Path(profile) if profile else find_profile()
    if not (directorio / "cookies.sqlite").is_file():
        raise FirefoxSessionError(f"{directorio} no contiene cookies.sqlite")

    with tempfile.TemporaryDirectory(prefix="firefox-cookies-") as tmp:
        for sufijo in ("", "-wal", "-shm"):
            origen = directorio / f"cookies.sqlite{sufijo}"
            if origen.is_file():
                shutil.copy2(origen, Path(tmp) / origen.name)
        conn = sqlite3.connect(f"file:{tmp}/cookies.sqlite?mode=ro", uri=True)
        try:
            filas = conn.execute(
                "SELECT name, value FROM moz_cookies WHERE host LIKE ?", (f"%{host_suffix}",)
            ).fetchall()
        except sqlite3.DatabaseError as exc:
            raise FirefoxSessionError(f"no se pudo leer la base de cookies: {exc}") from exc
        finally:
            conn.close()

    if not filas:
        raise FirefoxSessionError(
            f"el perfil de Firefox no tiene cookies de {host_suffix}; inicia sesión en el "
            "navegador y vuelve a intentarlo"
        )
    return {name: value for name, value in filas}
