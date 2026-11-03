# Procedencia de los logos

Estos SVG son **marcas registradas de sus titulares**. Se usan aquí sólo para identificar la cadena
de cada compra en una aplicación personal y privada de la red doméstica: uso nominativo, sin
sugerir relación ni patrocinio. No se redistribuyen ni se usan con fines comerciales.

| Fichero | Cadena | Origen | Fecha |
|---|---|---|---|
| `mercadona.svg` | Mercadona | Wikimedia Commons, `Special:FilePath/Mercadona.svg` (logotipo, viewBox 1130×178) | 2026-10-12 |
| `dia.svg` | Día | Wikimedia Commons, `File:Dia 2019.svg` (indicado por el usuario; sustituye al icono de dashboard-icons, que era de otra marca) | 2026-10-12 |
| `mas.svg` | Supermercados MAS | Logo oficial de su propia web: `supermercadosmas.com/static/.../images/logo.svg` (sustituye a la marca antigua de Commons y al thumbnail de Google, que traía fondo negro) | 2026-10-12 |
| `carrefour.svg` | Carrefour | dashboard-icons (homarr-labs) | 2026-10-12 |
| `lidl.svg` | Lidl | dashboard-icons (homarr-labs) | 2026-10-12 |

Notas de mantenimiento:

- Wikimedia limita por ritmo (`HTTP 429`): pedir sus ficheros con pausas y con un `User-Agent`
  descriptivo.
- Los thumbnails de Google (`encrypted-tbn*.gstatic.com`) **caducan** y pueden traer fondo negro;
  no son una fuente válida para un asset que debe durar.
- Las proporciones son muy distintas a propósito y no se normalizan en el fichero: la interfaz las
  mete en una caja con `object-fit: contain` (ver `.logo` en `web/static/styles.css`), de modo que
  nada se deforma ni salta de línea.
- Carrefour y Lidl están de reserva: no son cadenas soportadas todavía.
