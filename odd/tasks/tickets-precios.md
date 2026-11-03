# Feature: tickets-precios

Analizador de tickets de compra (PDF) y comparativa de precios de productos similares entre
cadenas españolas. Proyecto personal, repo nuevo `~/tickets-precios`.

## Objetivo

Dos capacidades sobre una misma base de datos:

1. **Análisis de compras**: importar los PDF de tickets descargados a mano desde la app o web de
   cada supermercado, extraer líneas de artículo (producto, cantidad, precio pagado, €/kg o €/l)
   y generar un informe estático con gasto por cadena, mes y categoría, y evolución de precio de
   los productos repetidos.
2. **Comparativa de productos similares**: emparejar cada línea de ticket con el catálogo actual
   de las cadenas soportadas y mostrar el precio equivalente (misma unidad de medida) en cada una,
   con el ahorro potencial.

## No-alcance

- No hay descarga automática de tickets: los PDF se descargan a mano y se dejan en una carpeta.
- No hay app móvil ni servicio web en la primera fase (CLI + informe HTML estático).
- No se redistribuye ni publica el código heredado (ver `NOTICE.md`).

## Decisiones tomadas

| Decisión | Valor | Motivo |
|---|---|---|
| Cadenas | Mercadona, Día, Supermercados MAS | Cadenas donde compra el usuario |
| Base de código | Fork de `joseluam97/Supermarket-Price-Scraper` | Decisión explícita del usuario |
| Licencia | Repo local, uso personal, adiciones separadas | El original no tiene licencia |
| Entrega | CLI + informe HTML estático | Decisión explícita del usuario |
| Ubicación | Repo nuevo `~/tickets-precios` | Decisión explícita del usuario |
| Almacén | SQLite | Un solo usuario, sin servidor de BD |
| Extracción PDF | `pypdf` (capa de texto) | Sin dependencias de sistema |
| Entorno Python | `uv venv .venv` | `python3 -m venv` falla: falta `python3.14-venv` (apt) |
| Día | Playwright con Chromium **con ventana** (Xvfb) | Akamai bloquea HTTP plano y headless |
| Fichero de ejemplo | `env.example` | `.env.example` está bloqueado por la política de rutas sensibles |

## Convenciones

- Módulos nuevos (`tickets/`, `reports/`) en inglés: identificadores, docstrings y comentarios.
- Código heredado (`supermarket/`, `main_supermarket.py`): se conserva tal cual, con firmas en
  español, para minimizar el diff del fork. Solo se repara lo que está roto.
- Cadenas: cada conector devuelve el mismo contrato; nunca escribe Excel directamente, escribe en
  la base de datos.
- El matching **nunca inventa**: si la confianza es baja, la línea queda sin emparejar y se marca
  para revisión manual. Un alias confirmado a mano se persiste y no se vuelve a preguntar.

## Tareas

| # | Tarea | Estado |
|---|---|---|
| T1 | Bootstrap y saneamiento del fork (rutas cross-platform, requirements reales, código muerto fuera, aviso de licencia) | hecho |
| T2 | Modelo de datos y almacén SQLite (`tickets`, `ticket_lines`, `products`, `price_observations`, `product_aliases`) | hecho |
| T3 | Conector Mercadona sobre API pública, escribiendo en el almacén | hecho |
| T4 | Conector Día (tienda online) con errores explícitos | hecho y **verificado en vivo** |
| T5 | Conector Supermercados MAS sobre Magento GraphQL | hecho |
| T6 | Motor de matching ticket ↔ catálogo (normalización + alias + fuzzy) | hecho |
| T7 | Parser de tickets PDF por cadena | **primera mitad hecha (MAS)**; Día pendiente de OCR |
| T11 | OCR de los tickets de Día (tesseract) | **esperando** `sudo apt install tesseract-ocr tesseract-ocr-spa` |
| T8 | Informe HTML estático (gasto, evolución de precio, comparativa de similares) | hecho |
| T9 | CLI orquestadora y cron (retira `main_supermarket.py`, `excel_data.py` y `carrefour.py`) | hecho |

## Descubrimientos sobre las fuentes de datos

### Mercadona (API pública, verificada en vivo)

- `GET /api/categories/` → 26 categorías de nivel 1, cada una con su lista `categories` de nivel 2.
- `GET /api/categories/{nivel2}/` → esa categoría de nivel 2, cuyas `categories` son las **hojas con los `products` ya dentro**.
- Las hojas **no** son consultables por URL: `/api/categories/420/` devuelve **404**. El conector heredado asumía otra cosa y estaba roto.
- Tamaño real del catálogo: 26 nivel 1, 152 nivel 2, 444 hojas, **4.592 productos**.
- Semántica de precios: `price_instructions.unit_price` = lo que pagas; `price_instructions.bulk_price` = precio por unidad base (€/kg, €/l, €/ud); `size_format` = etiqueta base (`l`, `kg`, `ud`; aparece `m`, que no mapeamos).
- Sin cookies, sin Selenium, sin WAF.

### Día (API interna de la tienda online)

- `GET https://www.dia.es/api/v1/plp-back/reduced` **sin cookie → 403 `text/html`** (WAF). Con `COOKIE_DIA` responde JSON con `plp_items`, `prices_price` y `prices_price_per_unit`.
- Coste asumido: la cookie caduca y hay que renovarla a mano desde el navegador.

### Supermercados MAS (Magento 2, verificado en vivo)

- La tienda corre **Magento 2 con tema Hyvä** detrás de Cloudflare. El `POST` a `/graphql` con cuerpo JSON devuelve **HTTP 500** «Syntax Error: Unexpected <EOF>» (se pierde el cuerpo); la misma consulta **por `GET` con la query en la URL** funciona (`/graphql?query={...}`).
- El árbol completo de categorías se obtiene en **una sola petición** anidando tres niveles de `children`: 20 categorías de nivel 1, 40 de nivel 2, 49 de nivel 3 → **109 categorías** (102 hojas). El cuarto nivel viene vacío, así que la profundidad es exhaustiva.
- Filtrar por `parent_id` de una categoría interna **no** devuelve nada (Frutas → 0 hijos); por eso el árbol se lee de una vez en lugar de categoría a categoría.
- `products(filter: {category_uid: {eq: "<uid>"}}, pageSize: N, currentPage: P)` pagina con `total_count`.
- `ProductInterface` tiene 38 campos y **ninguno** de peso o medida: el €/kg se deriva del tamaño escrito en el nombre del producto.
- Las URL de producto son `/<url_key>/` con barra final y sin `.html`.
- Su `robots.txt` prohíbe `/catalogsearch/` y `/catalog/product/view/`: el conector no usa ninguna de las dos. El endpoint `/rest/V1/` está cerrado a anónimos (401).
- Medición real (presupuesto de 8 peticiones): 233 productos leídos de 7 categorías, 223 distintos en base, **195 con €/unidad (87%)**. El catálogo completo no se ha recorrido todavía; el conector acepta `max_requests` y devuelve `stopped: true` si se corta, para no confundir una pasada parcial con una completa.

## Evidencia de cierre

### T1 — bootstrap y saneamiento (hecho)

- `uv venv .venv` + `uv pip install -r requirements.txt` correcto. Instalado: requests 2.34.2, pandas 3.0.6, pypdf 6.20.0, rapidfuzz 3.14.6.
- `python3 -m venv` no es viable en esta máquina (falta el paquete apt `python3.14-venv`).
- `.env.example` rechazado por la política de rutas sensibles → se usa `env.example`.
- Fuera del árbol: `exportarDatos.py` (código muerto de un scraper de zapatos ajeno), `supermarket/__pycache__/*.pyc` (estaban versionados).
- `Guia env.pdf` y el README original movidos a `docs/`.
- `requirements.txt` reescrito: el original no listaba `requests` ni `numpy` (que sí se importan) y arrastraba `pyowm`, `pywhatkit`, `discord-webhook` de otro proyecto.

### T2 — almacén SQLite (hecho)

- `tickets/store.py` (esquema + upsert/observaciones/alias) y `tickets/units.py` (normalización €/kg, €/l, €/ud; devuelve `(None, None)` si no puede, nunca adivina).
- `pytest -q`: **22 pasan en 0,12 s** (test de red deseleccionado por defecto). Con `--run-network`: 23 pasan.

### T3 — conector Mercadona (hecho)

- `supermarket/mercadona.py` reescrito con `requests` puro: `fetch_json` / `parse_category` / `ingest_mercadona(conn, sleep_seconds=0.5)`, errores explícitos con URL y código de estado.
- Verificación en vivo (categoría 112 «Aceite, vinagre y sal»): 35 productos parseados, **35/35 con unidad normalizada**, y la aritmética cuadra: `Aceite de oliva 0,4º Hacendado, garrafa 5 l → 17,25 € → 3,45 €/l`.
- Idempotencia real: reimportar la misma categoría deja 35 productos y 35 observaciones (no duplica).

### T5 — conector Supermercados MAS (hecho)

- `supermarket/mas.py` con el mismo contrato que Mercadona: `graphql` / `parse_category_tree` / `fetch_category_tree` / `parse_products` / `ingest_mas(conn, sleep_seconds=0.5, session=None, max_requests=400)`.
- Verificación en vivo (8 peticiones): 109 categorías, 233 productos, 223 distintos, 195 con €/unidad.
- Defecto encontrado y corregido antes de que hiciera daño: `tickets/units.py` no reconocía `gr`, `grs`, `gramos`, `lt`, `lts`, `litros` ni `cc`, así que **0 de 6** productos reales de MAS obtenían €/kg y los tickets con `500 GR` se quedaban sin comparar. Corregido test-first (11 casos nuevos en rojo, luego en verde) y sin regresiones.

### T6 — motor de matching (hecho)

- `tickets/matching.py`: `normalize_text`, `match_line`, `match_ticket_lines`, `confirm_alias`; y `units.extract_size` como única fuente de verdad de unidades (`normalize_unit` ahora delega en ella).
- Orden de resolución: alias confirmado por una persona (score 1,0) → fuzzy sobre el catálogo **de la misma cadena** (`0,6 × token_set_ratio + 0,4 × partial_ratio`) → guardián de tamaño → umbral 82; por debajo devuelve `None`.
- **Guardián de tamaño**: si ambos lados exponen tamaño y difieren (unidad distinta o más de un 10% relativo), el candidato se descarta. Si sólo un lado lo expone, no se descarta ni se premia.
- Verificación con catálogo real (35 aceites de Mercadona + 6 frutas de MAS):

| Línea de ticket | Resultado |
|---|---|
| `ACEITE OLIVA 0,4 HACENDADO GARRAFA 5L` | `Aceite de oliva 0,4º Hacendado` — **3,45 €/l** (la garrafa) |
| `ACEITE OLIVA 0,4 HACENDADO BOT 1L` | mismo nombre de producto, pero **3,70 €/l** (la botella) |
| `ACEITE OLIVA VIRGEN EXTRA HACENDADO 3L` | `Aceite de oliva virgen extra` — 4,05 €/l |
| `ARANDANOS CUBO 500 GR` | `Arandanos cubo 500 gr` — score 100 |
| `FRESA TARRINA 125 GR` | **sin emparejar**: hay una fresa de 500 g y el tamaño no cuadra |
| `CHORIZO ARTESANO DULCE` | **sin emparejar**: no hay candidato plausible |

  El primer y el segundo caso demuestran lo que importa: dos productos con el **mismo nombre** se distinguen sólo por el tamaño, y el motor no los confunde.
- `pytest -q`: 62 pasan.

### Día (tras Akamai, verificado en vivo)

- Todo `dia.es` está detrás de **Akamai**: `403 Access Denied` (`errors.edgesuite.net`) y `Set-Cookie: h163j1mz=...` (reto JS de bot detection). Afecta incluso a `robots.txt` y al sitemap, no sólo a `/api/`.
- Consecuencia: **una cookie de sesión probablemente no basta** para `requests`, porque el bloqueo es por huella TLS y reto JavaScript, no sólo por credencial. El conector heredado, que sólo enviaba `COOKIE_DIA`, partía de una premisa optimista.
- Alternativas medidas: (a) navegador headless (Playwright/Selenium) — fiable pero añade dependencia pesada; (b) dejar Día fuera del catálogo y usar **sus propios tickets** como fuente de precio, que además ya imprimen €/kg en las líneas por peso; (c) usar sólo el catálogo de las cadenas abiertas (Mercadona, MAS) para comparar lo comprado en Día.
- El catálogo de Día sólo hace falta para responder «¿cuánto me costaría esto si lo comprara en Día?» cuando la compra se hizo en otra cadena.

### T8 — informe HTML (hecho)

- `reports/report.py`: `build_report(conn, output_path, title)` → HTML **autocontenido** (CSS y barras SVG en línea, sin CDN ni JavaScript externo) con seis secciones: resumen, gasto por cadena, por mes, por categoría, evolución de lo que pago y comparativa con otras cadenas.
- Comparativa: para cada línea emparejada busca un producto comparable en cada otra cadena con `matching.match_line`; si no lo hay, muestra «—» en vez de inventarlo.
- Formato español (`1.234,56 €`, `3,45 €/l`) y escapado de todo texto que venga de la base.
- `pytest -q`: 70 pasan.
- Informe de demostración generado con catálogo real (41 productos: 35 aceites de Mercadona + 6 frutas de MAS) y 3 tickets de ejemplo: 42,05 € totales, 4 de 5 líneas emparejadas (la de Día sin emparejar), 0 recursos externos → `report/informe-demo.html`.
- Atajo declarado en el código: el €/unidad pagado se deriva de `total_paid` y del tamaño del texto de la línea, **ignorando `qty`** hasta que T7 fije la semántica de cantidades del parser de tickets.

### T4 — conector Día (código hecho, verificación en vivo pendiente)

- `supermarket/dia.py` **sustituye** al conector heredado (que sólo mandaba `COOKIE_DIA` y se tragaba los errores con un `except` mudo).
- Nuevo: cabeceras de navegador completas (Chrome, `Accept-Language`, `Sec-*`, `Referer`), porque Akamai puntúa la petición entera y no sólo la cookie; `session_from_env()`, `fetch_json()`, `parse_products()` e `ingest_dia(conn, category_paths)`.
- Errores explícitos: sin cookie explica dónde copiarla; un 403 dice «Akamai: la cookie ha caducado» en vez de devolver una lista vacía; un cuerpo que no es JSON se reporta como tal; un payload sin `plp_items` **lanza** en vez de fingir que no hay productos.
- La lista de categorías se **pide explícitamente** (`category_paths`) en lugar de adivinarla: el descubrimiento heredado era frágil y no está verificado contra una sesión real.
- `pytest -q`: 79 pasan (8 tests nuevos del conector).
- **Pendiente de verificar**: el mapeo de campos (`plp_items`, `prices_price`, `prices_price_per_unit`, `prices_measure_unit`) procede del conector heredado, no de una respuesta en vivo, porque no había sesión al escribirlo. Está marcado como tal en el docstring del módulo y en el test que lo fija. Si Akamai sigue devolviendo 403 incluso con la cookie completa, la siguiente vía es `curl_cffi` (suplanta la huella TLS de Chrome, dependencia pequeña) antes que un navegador headless.

### T4 y T10 — conector Día (hecho y verificado en vivo)

- **Playwright resuelve Akamai, pero sólo con ventana real**: `headless=True` recibe 403 igual que `requests`; `headless=False` dentro de `xvfb-run -a` pasa limpio (home 200, título correcto, cookies `_abck`, `ak_bmsc`, `bm_sz` obtenidas solas). En una máquina con escritorio la ventana aparece en la sesión del usuario; en un servidor, `xvfb-run`.
- **Consecuencia agradable: ya no hace falta pegar ninguna cookie.** El navegador obtiene y renueva las de Akamai por sí solo, así que el enfoque del conector heredado (`COOKIE_DIA` a mano) queda obsoleto. La variable sigue en `env.example` sólo por compatibilidad con los scripts heredados.
- **El mapeo heredado era erróneo**, no sólo incompleto:

| Heredado | Real (v1, 2026) |
|---|---|
| `GET /api/v1/plp-back/reduced` + ruta | `GET /api/v1/plp-back/l1/all/<id-nivel-1>/reduced?category_id=<id>&page=<n>` |
| `plp_items` | `items` |
| `prices_price` | `prices.price` |
| `prices_price_per_unit` | `prices.price_per_unit` |
| `prices_measure_unit` | `prices.measure_unit` (`KILO`, `LITRO`, `UNIDAD`) |

  La propia tienda da el precio por unidad, así que en Día no hay que derivarlo del nombre: **78 de 78 productos con €/unidad**.
- Árbol de categorías: `GET /api/v1/plp-insight/initial_analytics/l1/all/L105` → `menu_analytics`, con `parameter` (`L1_quesos`) y `path` (`/quesos/c/L101`). Verificado: 326 categorías (31 de nivel 1, 295 de nivel 2).
- Rareza de Día neutralizada: su árbol repite ids (el nodo de nivel 1 y su propio hijo "todo <categoría>" comparten id y ruta), lo que duplicaba peticiones. El conector deduplica por id.
- Verificación en vivo: 3 categorías, **9 páginas, 83 productos en 10,4 s** (78 distintos en base, todos con €/unidad).
- Coste: la ingesta de Día necesita un **display** (real o virtual) y Playwright. En esta máquina los navegadores **ya estaban instalados** de un uso anterior (1,3 GB con dos versiones de Chromium, agosto y septiembre), así que lo único que se añadió es el paquete de Python a `requirements.txt`. Mercadona y MAS siguen sin necesitar nada de eso.
- `pytest -q`: 87 pasan.

### T9 — CLI y cron (hecho)

- `tickets_cli.py` con cuatro comandos (`main(argv)` importable, sin jerarquías de clases):

| Comando | Qué hace |
|---|---|
| `precios --cadena {mercadona,dia,mas,todas}` | Descarga catálogos; una línea de resumen por cadena; códigos de salida 0/1/2 |
| `estado` | Qué hay en la base: productos por cadena, cuántos con €/unidad, lo más reciente, tickets y líneas emparejadas |
| `informe --salida RUTA` | Genera el HTML (crea el directorio si falta) |
| `actualizar` | Entrada de cron: todas las cadenas y después el informe |

- Si una cadena falla, las demás siguen y el proceso sale con código 1 para que cron se entere; nunca hay un fallo silencioso. Un error de Día añade el recordatorio de `xvfb-run -a`.
- **No hay comando `tickets` para PDF**: el parser es T7 y no está implementado; la CLI no finge que exista.
- `docs/cron.md` con las líneas de crontab, por qué Día va bajo `xvfb-run -a` y dónde quedan los logs.
- Defecto encontrado al revisar el código del worker (ningún test lo cubría): `_run_dia` abría **un Chromium nuevo por cada petición**. Con 326 categorías eso eran minutos de arranques de navegador. Corregido para reutilizar uno solo, con test de regresión que cuenta las instancias creadas. Verificado en vivo: 2 categorías, 4 páginas, 7,2 s en total (el arranque del navegador se paga una vez).
- `pytest -q`: 96 pasan.

### Retirada del código heredado

Fuera del árbol: `main_supermarket.py` (orquestador sustituido por la CLI), `excel_data.py` (export a Excel que ya nadie usa) y `supermarket/carrefour.py` (cadena no solicitada, con cookies manuales y sin verificar). Con ellos se va la última referencia a `COOKIE_DIA`/`COOKIE_CARREFOUR`; `env.example` queda con `TICKETS_INBOX` y `DB_PATH`.

Siguen recuperables del historial si algún día hacen falta:

```bash
git show 6afe0f6:supermarket/carrefour.py > supermarket/carrefour.py
```

### T7 — parser de tickets (primera mitad: Supermercados MAS)

Las dos muestras revelaron dos casos muy distintos:

| Muestra | Naturaleza | Consecuencia |
|---|---|---|
| `46.pdf` (MAS) | **Capa de texto** (1.514 caracteres) | Se parsea sin OCR |
| `Documento PDF.pdf` (Día) | **148 imágenes, 0 fuentes, 0 caracteres**: pantallazo del iPhone exportado con iOS Quartz | Necesita OCR |

- Formato MAS deducido del ticket real: columnas fijas `nombre(20) <qty>x precio_unitario total IVA`. Un `1.527x 1.65 2.52` es una línea por peso: **la columna de precio ya es el €/kg**, así que no hay que deducirlo del nombre.
- `tickets/parsing.py`: `extract_pdf_text`, `detect_chain`, `parse_ticket`, `parse_pdf`, `import_ticket`. Sin OCR: un ticket en imagen lanza `ParseError` diciendo exactamente eso en lugar de devolver un texto vacío.
- `purchase_unit` nuevo en `ticket_lines`, con **migración idempotente** (`PRAGMA table_info` + `ALTER TABLE`) para no perder las bases ya creadas: cantidad entera → `ud`; cantidad fraccionaria → `kg` (una línea por peso de supermercado se tarifa por kilo). Límite documentado: un `1.5x` de un pack se leería como kilos y el diseño impreso no permite distinguirlo.
- El informe **ya usa el precio unitario del ticket** cuando existe (`purchase_unit`), en vez de ignorarlo: sin eso, el melón por peso se quedaba sin €/kg en la comparativa.
- La suma de líneas se contrasta con el total impreso; si difiere en más de 0,05 € se marca `mismatch` en lugar de aceptarse en silencio.
- `tickets_cli.py tickets --inbox RUTA`: importa todos los PDF de la carpeta, una línea por fichero; salida 1 si alguno falla; un ticket ya importado se reporta como tal en vez de fallar.
- Verificación con el ticket real: `MAS, ticket 261012/043/S/103/024205, 8 líneas, total 22,86 €`, suma de líneas 22,86 € (coincide). Segunda pasada: «ya importado». El ticket de Día falla con el mensaje de OCR.
- Higiene aplicada: `samples/inbox/` en `.gitignore` (los PDF personales no entran en git) y el fixture de tests es el texto del ticket **con el bloque de pago y tarjeta eliminado** (verificado: cero coincidencias de tarjeta, autorización o últimos dígitos).
- `pytest -q`: 111 pasan.

### T11 — OCR de Día: preparado, esperando el binario

- Decisión del usuario: **tesseract por apt**, no modelo de visión ni OCR solo con pip.
- Ya hecho: `pytesseract 0.3.13` instalado (sin sudo), `pdftoppm` comprueba que rinde el ticket de Día a un PNG de **1042×4944 px** sin problema, y el error de "tesseract no instalado" es explícito.
- Falta el binario, que necesita contraseña:

```bash
sudo apt install -y tesseract-ocr tesseract-ocr-spa
```

- Cuando esté, quedan: `tickets/ocr.py` (render con `pdftoppm` + OCR con `lang="spa"`), el parser del diseño de Día (columnas `DESCRIPCIÓN | CANTIDAD | PRECIO KG | TOTAL`, con nombres que ocupan dos líneas) y la verificación con la comprobación aritmética: la suma de líneas debe cuadrar con el total impreso (36,95 €).

## Nota de estado
`main_supermarket.py` y `excel_data.py` quedan **roto/código muerto a propósito**: el primero importaba `gestion_mercadona`, que ya no existe. Se retiran en T9 junto con la CLI nueva, para no dejar dos puntos de entrada.
