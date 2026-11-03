"""Take the README's screenshots against a thrown-away demo database.

Starts the app on a free port with ``DB_PATH`` pointing at the synthetic demo
database (:mod:`scripts.demo`), walks the pages with Playwright and writes the
PNGs into ``docs/img/``. No real ticket is involved, so the images can live in
the repository.

    python3 scripts/capturas.py              # docs/img/*.png
    python3 scripts/capturas.py --db /tmp/x.db --out /tmp/shots
"""

from __future__ import annotations

import argparse
import os
import pathlib
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request

RUTA = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RUTA))

from scripts import demo  # noqa: E402

#: (file name, path, theme, full page?) — the pages the manual walks through.
PAGINAS = [
    ("portada", "/?mes=todos", "light", True),
    ("portada-oscuro", "/?mes=todos", "dark", True),
    ("ticket", "/tickets/3", "light", True),
    ("producto", "/productos/{producto}", "light", True),
    ("nuevo", "/tickets/nuevo", "light", False),
]

ANCHO = 1280
ALTO = 900


def puerto_libre() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def esperar(url: str, intentos: int = 100) -> None:
    for _ in range(intentos):
        try:
            urllib.request.urlopen(url, timeout=1).read()
            return
        except (urllib.error.URLError, OSError):
            time.sleep(0.1)
    raise SystemExit(f"La app no arrancó en {url}")


def producto_estrella(db: pathlib.Path) -> int:
    conn = sqlite3.connect(db)
    try:
        fila = conn.execute(
            "SELECT id FROM products WHERE chain = 'Mercadona' AND external_id = 'M-101'"
        ).fetchone()
    finally:
        conn.close()
    if fila is None:
        raise SystemExit("La base de demostración no tiene el producto esperado (M-101).")
    return fila[0]


def capturar(db: pathlib.Path, salida: pathlib.Path) -> list[pathlib.Path]:
    from playwright.sync_api import sync_playwright

    salida.mkdir(parents=True, exist_ok=True)
    producto = producto_estrella(db)
    puerto = puerto_libre()
    base = f"http://127.0.0.1:{puerto}"
    entorno = {**os.environ, "DB_PATH": str(db)}
    servidor = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "web.app:app", "--port", str(puerto), "--log-level", "warning"],
        cwd=str(RUTA),
        env=entorno,
    )
    escritos = []
    try:
        esperar(f"{base}/salud")
        with sync_playwright() as playwright:
            navegador = playwright.chromium.launch()
            for nombre, ruta, tema, completa in PAGINAS:
                pagina = navegador.new_page(viewport={"width": ANCHO, "height": ALTO})
                # El tema se elige en localStorage antes del primer pintado, igual que en el navegador.
                pagina.add_init_script(f"localStorage.setItem('tema', '{tema}')")
                pagina.goto(base + ruta.format(producto=producto), wait_until="networkidle")
                destino = salida / f"{nombre}.png"
                pagina.screenshot(path=str(destino), full_page=completa)
                pagina.close()
                escritos.append(destino)
            navegador.close()
    finally:
        servidor.terminate()
        servidor.wait(timeout=10)
    return escritos


def main(argv=None):
    parser = argparse.ArgumentParser(description="Capturas del README sobre una base de demostración.")
    parser.add_argument("--db", default=str(RUTA / "report" / "demo.db"))
    parser.add_argument("--out", default=str(RUTA / "docs" / "img"))
    parser.add_argument("--reusar", action="store_true", help="No reconstruir la base de demostración.")
    args = parser.parse_args(argv)

    db = pathlib.Path(args.db)
    if not args.reusar or not db.exists():
        demo.build(db)
    for ruta in capturar(db, pathlib.Path(args.out)):
        print(ruta)


if __name__ == "__main__":
    main()
