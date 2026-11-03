# tickets-precios

Analiza tus tickets de compra (PDF descargado a mano desde la app o web de cada supermercado) y
compara el precio de **productos similares** entre cadenas.

Estado: en construcción. Base de código: fork de
[`joseluam97/Supermarket-Price-Scraper`](https://github.com/joseluam97/Supermarket-Price-Scraper)
— ver [NOTICE.md](NOTICE.md) para la situación de licencia.

## Para qué sirve

1. **Análisis de lo que compras**: importas los PDF de tus tickets, la herramienta extrae las
   líneas de artículo (producto, cantidad, precio pagado, €/kg o €/l) y te dice cuánto gastas por
   cadena, por mes y por categoría, y cómo evoluciona el precio de cada producto que repites.
2. **Comparativa de precios similares**: contrasta lo que pagaste con el catálogo actual de las
   cadenas soportadas, emparejando líneas de ticket con productos de catálogo y mostrando el
   precio equivalente (misma unidad de medida) en cada una.

## Cadenas

| Cadena | Catálogo de precios | Tickets PDF |
|---|---|---|
| Mercadona | API pública `tienda.mercadona.es` | pendiente de muestras |
| Día | API v1 de la tienda online, **desde navegador real** (Akamai bloquea HTTP plano y headless) | pendiente de muestras |
| Supermercados MAS | Magento GraphQL (GET) | pendiente de muestras |
| Carrefour | heredado, pendiente de revisión | no previsto |

### Día necesita navegador

`www.dia.es` está detrás de Akamai: HTTP plano y Chromium headless reciben 403. La ingesta de Día se
lanza con un Chromium con ventana real, así que en una máquina sin escritorio hay que envolver el
comando:

```bash
xvfb-run -a python3 tickets_cli.py precios --cadena dia
```

No hace falta pegar ninguna cookie: el navegador obtiene y renueva las de Akamai por sí solo.

## Uso

```bash
python3 tickets_cli.py precios --cadena todas    # descarga los catálogos
python3 tickets_cli.py estado                    # qué hay en la base de datos
python3 tickets_cli.py informe                   # informe HTML en report/informe.html
python3 tickets_cli.py actualizar                # todo lo anterior (entrada de cron)
python3 tickets_cli.py sesiones --navegador       # ¿siguen valiendo las sesiones de las tiendas?
python3 tickets_cli.py imagenes --solo-comprados  # fotos de los productos que has comprado
```

### Sincronización con el host (el día a día)

```bash
./scripts/sincronizar.sh              # tickets de las dos cuentas + copia y reinicio del host
./scripts/sincronizar.sh --catalogo   # además refresca los catálogos de las tiendas
./scripts/sincronizar.sh --informe    # además regenera el informe HTML
./scripts/sincronizar.sh --comprobar  # no descarga nada: sólo mira si las sesiones siguen vivas
./scripts/sincronizar.sh --sin-host   # sólo local, sin tocar el host
```

### De dónde salen los tickets

Los tickets se descargan solos de las cuentas de las tiendas, reutilizando la sesión que ya tienes
abierta **en Firefox** (Firefox guarda las cookies sin cifrar; el script las lee de tu perfil y no
las copia a ningún sitio):

| Cadena | Vía | Qué se obtiene |
|---|---|---|
| Supermercados MAS | Endpoint de PDF de su área de cliente | Los mismos PDF que se descargan a mano (con capa de texto) |
| Día | API JSON de su área de cliente | Datos exactos, **sin OCR**: `variable_weight` marca las líneas por peso y el `code` de cada línea es el `external_id` del catálogo, así que el emparejamiento es exacto |

Las sesiones caducan. Cuando eso pase, `./scripts/sincronizar.sh --comprobar` (o cualquier descarga)
lo dice por su nombre y no toca nada más; la solución es abrir Firefox, iniciar sesión en esa tienda
y volver a lanzarlo. Si usas otro perfil de Firefox, indícalo con `FIREFOX_PROFILE=/ruta/del/perfil`.

En un servidor, la entrada de cron va bajo `xvfb-run -a`: ver [`docs/cron.md`](docs/cron.md).

La importación de tickets en PDF **todavía no está**: el parser necesita muestras reales de cada
cadena y aún no las hay, así que la CLI no ofrece ese comando en lugar de fingirlo.

## Instalación

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp env.example .env
```

## Documentación

- Plan, decisiones y tareas: [`odd/tasks/tickets-precios.md`](odd/tasks/tickets-precios.md)
- Cómo sacar las cookies: [`docs/guia-cookies-original.pdf`](docs/guia-cookies-original.pdf)
- README del proyecto original: [`docs/README-original.md`](docs/README-original.md)
