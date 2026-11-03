# Origen del código

Este proyecto empezó como fork local de
[`joseluam97/Supermarket-Price-Scraper`](https://github.com/joseluam97/Supermarket-Price-Scraper),
que **no incluye fichero de licencia**; sin licencia, el código queda bajo copyright por defecto
(todos los derechos reservados).

El fork fue el punto de partida, no el código que hay aquí. Lo que se publica en este repositorio:

1. **Los conectores de Mercadona y Día están reescritos** contra las APIs que cada cadena expone
   hoy. La comparación línea a línea con los ficheros del proyecto original no comparte ninguna
   sentencia: solo `import requests`, `import time`, `import os`, `try:` y llaves de cierre, que son
   sintaxis y no expresión. Los endpoints y los nombres de campo son hechos, no código heredado.
2. **El resto de módulos es nuevo**: `tickets/`, `reports/`, `web/`, `tests/`, `scripts/`,
   `tickets_cli.py` y el contenedor. En el proyecto original no existen los conectores de
   Supermercados MAS ni de Amazon, ni la lectura de la sesión de Firefox.
3. **El material del autor original no se distribuye**: su README y su guía de cookies vivían en
   `docs/` y se han retirado. Este repositorio no redistribuye nada suyo.
4. **El historial del fork no se publica**: el repositorio arranca con un commit inicial propio.

Cada endpoint usado está verificado en vivo contra la cadena correspondiente y anotado en
`odd/tasks/tickets-precios.md`, junto con la fecha de la comprobación.
