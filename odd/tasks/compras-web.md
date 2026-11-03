# Feature: compras-web

Aplicación web para seguir las compras a partir de los tickets ya parseados, con comparación de
productos entre las cadenas scrapeadas y equivalencias de marca blanca.

## Objetivo

1. **Listado de compras**: cada ticket con fecha y hora, supermercado (con su logo) e importe; y
   resumen (gasto total, por cadena, del mes).
2. **Detalle de compra**: desglose de productos con precio, unidades y **€/kg cuando la línea es por
   peso**.
3. **Ficha de producto**: el mismo producto en cada cadena scrapeada (precio y €/unidad); si es marca
   propia, las **marcas blancas equivalentes** de las otras cadenas.
4. **Añadir ticket**: subir un PDF desde la pantalla inicial y que se parsee e importe.
5. Servida en la LAN tras un proxy inverso de la casa (Caddy), en un host Linux con Docker.

## Alcance y no-alcance

- Dentro: app web de un solo usuario, LAN, sin autenticación (igual que el resto de servicios de casa).
- Dentro: reutilizar el almacén SQLite y los parsers ya existentes.
- Fuera: scraping desde la web (los catálogos se descargan con la CLI, no desde una petición HTTP).
- Fuera: multiusuario, edición de tickets ya importados, app móvil.

## Decisiones

| Decisión | Valor | Motivo |
|---|---|---|
| Stack | FastAPI + Jinja2 + uvicorn, HTML renderizado en servidor | El proyecto es Python puro; no hay toolchain Node en la máquina ni en el host, y tras un proxy inverso es lo más robusto y lo que menos código necesita |
| Assets | CSS y logos **locales**, sin CDN | La app es LAN-only y debe funcionar sin salida a internet |
| Diseño | Skills `better-ui`, `better-colors`, `better-typography`, `better-layout`, `better-accessibility` | Petición explícita del usuario y calidad de interfaz |
| Marca | `products.brand` + léxico de marcas propias por cadena | Las tiendas publican la marca de verdad (MAS: `manufacturer`; Día: `dia_brand`), y además se detecta por nombre |
| Despliegue | `webserver/compras/` + `caddy/compras.caddy` + tile en Homepage | Es el patrón ya establecido en la casa |
| Imagen | Base de Playwright (chromium + Xvfb) + tesseract y poppler | Día necesita navegador con ventana y los tickets de Día, OCR |

## Marca propia: por qué es verificable

Sondeado el 2026-10-12:

- **MAS** expone la marca como atributo: la agregación `manufacturer` (etiqueta «Marca») devuelve
  LA CARLOTEÑA, JOSE CABO, EL POZO…, y la propia web tiene páginas de marca
  (`/charcuteria-quesos/marca-ifa_eliges/`). El campo no está en `ProductInterface`, pero **sí se
  puede filtrar**, que es lo que hace falta para obtener el conjunto de marca propia de una cadena.
- **Día** expone la faceta `dia_brand` («Marca Dia») en el listado de productos.
- **Mercadona** no expone marca: sus enseñas propias (Hacendado, Bosque Verde, Deliplus, Compy) se
  detectan por nombre, y Mercadona las usa de forma consistente.

Evidencia de nuestros propios catálogos (frecuencia de tokens en nombres de producto):

| Cadena | Tokens dominantes | Lectura |
|---|---|---|
| MAS (9.712 productos) | IFA (1.433), ELIGES (908), UNNIA (206) | IFA ELIGES es su marca propia |
| Día (5.619 productos) | DIA (2.335), VEGECAMPO (178), SELECCIÓN (163) | Dia, Vegecampo y Dia Selección |

## Tareas

| # | Tarea | Estado |
|---|---|---|
| C1 | Plan, decisiones y léxico de marcas propias con tests | hecho |
| C2 | `products.brand` + detección de marca propia por nombre | pendiente |
| C3 | App FastAPI: esqueleto, rutas, sistema de diseño y plantillas con logos | pendiente |
| C4 | Listado de compras con resumen (gasto total, por cadena, del mes) | pendiente |
| C5 | Detalle de compra con líneas, unidades y €/kg | pendiente |
| C6 | Ficha de producto: mismo producto por cadena + equivalencias de marca blanca | pendiente |
| C7 | Subida de PDF y parseo desde la pantalla inicial | pendiente |
| C8 | Tests offline + verificación con navegador (capturas) | pendiente |
| C9 | Artefactos de despliegue en `webserver/` (compose, Dockerfile, vhost, tile, runbook bilingüe) | hecho |
| C13 | Descarga automática de tickets desde las cuentas (MAS por PDF, Día por API JSON) | hecho |
| C14 | Emparejamiento exacto por código de producto en Día (55% → 91%) | hecho |
| C15 | Script de sincronización y comprobación de sesiones (`scripts/sincronizar.sh`, `tickets_cli.py sesiones`) | hecho |
| C16 | Desplegable de mes en la pantalla principal: mes actual por defecto y **todas las estadísticas de ese mes** | hecho |

## C16 — Desplegable de mes (hecho)

- `GET /` acepta `?mes=YYYY-MM`; por defecto el **mes actual** según el reloj de la tienda
  (`Europe/Madrid`), y `?mes=todos` para el histórico completo.
- Se filtra por `substr(purchased_at,1,7)`: gasto, número de compras, media por compra, gasto por
  cadena y el historial. El panel lleva el mes como título («octubre 2026», «Todo el histórico»).
- El selector es un **formulario GET real** (`<select name="mes">` etiquetado + botón «Ver»): funciona
  con teclado y sin JavaScript. Lista los meses que tienen tickets, más el mes actual aunque esté vacío.
- Un valor inválido (`?mes=2026-13`, `?mes=octubre`, vacío) cae al mes actual; un mes sin compras
  muestra ceros y un mensaje, nunca una cifra inventada.
- Verificado contra la base: octubre 2026 → 5 compras / 158,45 €; marzo 2026 → 19 / 410,98 €;
  `todos` → 117 / 2.882,38 €; y en navegador (selección por teclado, sin desborde a 320 px, un solo `h1`).
- `pytest -q`: 256 pasan.

## Reglas de la casa que aplican

De las reglas de la casa del dueño (leídas antes de empezar):

- **Todo markdown generado en el home se publica** al árbol de docs servido con
  `scripts/publish-docs.sh` **antes** de dar la tarea por terminada; un proyecto nuevo con markdown
  añade una línea a su tabla `MAP`. `tickets-precios` todavía no está en esa tabla.
- `docs/` guarda **pares bilingües**: `<feature>.md` en inglés (canónico) y `<feature>.es.md`
  (espejo completo en español).
- Los runbooks son bloques **Action** (lo que teclea la persona en el host) y **Record** (la salida
  pegada). Nunca se escribe un `Record` como si se hubiera ejecutado.
- Las acciones sobre el host son acciones humanas vía SSH; esta máquina no tiene
  `docker` ni `caddy`.

## Evidencia de cierre

(vacío: se rellena al cerrar cada tarea)
