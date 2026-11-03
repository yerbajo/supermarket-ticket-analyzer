#!/usr/bin/env bash
# sincronizar.sh — trae los tickets de las cuentas, refresca los catálogos y deja el host al día.
#
#   ./scripts/sincronizar.sh                 # tickets de las dos cuentas y copia al host
#   ./scripts/sincronizar.sh --catalogo      # además refresca los catálogos de las tiendas
#   ./scripts/sincronizar.sh --informe       # además regenera el informe HTML
#   ./scripts/sincronizar.sh --sin-host      # sólo local: no copia nada al host
#   ./scripts/sincronizar.sh --comprobar     # no descarga nada: sólo mira las sesiones
#
# POR QUÉ ESTE SCRIPT VIVE AQUÍ Y NO EN EL HOST: las sesiones de las tiendas son las de tu Firefox
# de esta máquina (Firefox guarda las cookies sin cifrar) y Día rechaza el HTTP plano: necesita una
# ventana real de Chromium con esas cookies. El contenedor no puede hacer ese paso, así que la
# descarga ocurre aquí y la base se copia al host, que la sirve en modo casi sólo lectura.
#
# Si una sesión ha caducado, el script lo dice por su nombre y no sigue: abre Firefox, inicia
# sesión en esa tienda y vuelve a lanzarlo. Nada se pega ni se copia a mano.

set -euo pipefail

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$RAIZ/.venv/bin/python"
DB="${COMPRAS_DB:-$RAIZ/report/full.db}"
CLI="$RAIZ/tickets_cli.py"
# El host de casa no vive en el repositorio: sale de COMPRAS_HOST o de scripts/host.local,
# que está en .gitignore (una sola línea, COMPRAS_HOST=usuario@host).
HOST="${COMPRAS_HOST:-$(sed -n 's/^COMPRAS_HOST=//p' "$RAIZ/scripts/host.local" 2>/dev/null || true)}"
DESTINO="${COMPRAS_DEST:-/opt/compras}"
SALUD="${COMPRAS_SALUD:-http://${HOST#*@}:8130/salud}"

con_catalogo=0
con_informe=0
al_host=1
solo_comprobar=0

while [ $# -gt 0 ]; do
  case "$1" in
    --catalogo) con_catalogo=1 ;;
    --informe) con_informe=1 ;;
    --sin-host) al_host=0 ;;
    --comprobar) solo_comprobar=1 ;;
    -h|--help) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Opción desconocida: $1 (usa --help)" >&2; exit 2 ;;
  esac
  shift
done

paso() { printf '\n\033[1m== %s\033[0m\n' "$1"; }
aviso() { printf '\033[33m%s\033[0m\n' "$1" >&2; }

if [ "$al_host" = 1 ] && [ -z "$HOST" ]; then
  aviso "No sé a qué host copiar: exporta COMPRAS_HOST o crea scripts/host.local (COMPRAS_HOST=usuario@host). Con --sin-host no hace falta."
  exit 2
fi


# Un navegador con ventana: Día bloquea el HTTP plano y también el modo headless. En una máquina
# con escritorio la ventana aparece y desaparece; en un servidor hace falta un display virtual.
if [ -z "${DISPLAY:-}" ]; then
  if command -v xvfb-run >/dev/null 2>&1; then
    NAVEGADOR=(xvfb-run -a)
  else
    aviso "No hay DISPLAY ni xvfb-run: la descarga de Día fallará. Instala xvfb o ejecútalo desde el escritorio."
    NAVEGADOR=()
  fi
else
  NAVEGADOR=()
fi

if [ ! -x "$PY" ]; then
  aviso "No encuentro el entorno en $PY. Crea el venv (uv venv .venv) e instala requirements.txt."
  exit 1
fi

paso "Sesiones de las tiendas"
if ! "$PY" "$CLI" sesiones; then
  aviso "Falta alguna sesión: inicia sesión en esa tienda con Firefox y vuelve a lanzar el script."
  exit 1
fi
if [ "$solo_comprobar" = 1 ]; then
  echo
  echo "Sólo comprobación: no se ha descargado nada."
  exit 0
fi

paso "Tickets de las cuentas (Día por API, MAS por PDF)"
"${NAVEGADOR[@]}" "$PY" "$CLI" tickets --descargar-dia --descargar-mas --imagenes --solo-comprados --db "$DB" \
  | grep -Ev "ya importado" || true

if [ "$con_catalogo" = 1 ]; then
  paso "Catálogos de las tiendas"
  "${NAVEGADOR[@]}" "$PY" "$CLI" precios --cadena todas --pausa 0.3 --db "$DB"
fi

if [ "$con_informe" = 1 ]; then
  paso "Informe HTML"
  "$PY" "$CLI" informe --db "$DB" --salida "$RAIZ/report/informe.html"
fi

paso "Qué hay en la base"
"$PY" "$CLI" estado --db "$DB"

if [ "$al_host" != 1 ]; then
  echo
  echo "Sin tocar el host (--sin-host). Base local: $DB"
  exit 0
fi

paso "Copia al host ($HOST:$DESTINO)"
if ! command -v rsync >/dev/null 2>&1; then
  aviso "No hay rsync: copia $DB a mano o instálalo."
  exit 1
fi
rsync -az --delete \
  --exclude '.venv' --exclude '.git' --exclude 'report' --exclude 'samples' \
  --exclude 'web/screenshots' --exclude '__pycache__' --exclude '*.pyc' \
  --exclude '.pytest_cache' --exclude '*.db' --exclude 'venv.broken' \
  "$RAIZ/" "$HOST:$DESTINO/app/"
rsync -az "$DB" "$HOST:$DESTINO/data/compras.db"
# Las fotos de producto viven junto a la base: el contenedor las servirá desde /data/images.
if [ -d "$RAIZ/report/images" ]; then
  rsync -az --delete "$RAIZ/report/images/" "$HOST:$DESTINO/data/images/"
fi
rsync -az "$RAIZ/../webserver/compras/docker-compose.yml" "$RAIZ/../webserver/compras/settings.env" \
  "$HOST:$DESTINO/" 2>/dev/null || aviso "No encontré webserver/compras; se usan los ficheros que ya están en el host."

paso "Reconstruir y reiniciar el contenedor"
ssh -o BatchMode=yes "$HOST" "cd $DESTINO && docker compose up -d --build 2>&1 | tail -3"

paso "Comprobación"
sleep 5
ssh -o BatchMode=yes "$HOST" "docker ps --filter name=compras --format '{{.Names}} {{.Status}}'"
curl -s -m 15 "$SALUD" && echo
echo "Web: $SALUD"
