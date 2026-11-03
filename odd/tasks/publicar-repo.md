# Feature: publicar el repositorio

Publicar este proyecto en `github.com/yerbajo/supermarket-ticket-analyzer` sin datos sensibles y con
un README que sirva de manual. Decisión del dueño: **publicar solo lo propio**, reescribiendo lo que
se hubiera heredado del fork.

## Specs

- **S1** — «he creado un repo para subir esta app https://github.com/yerbajo/supermarket-ticket-analyzer».
  El repositorio destino es público y estaba vacío.
- **S2** — «Quiero que revises que no existan datos sensibles.»
- **S3** — «genera un readme con el objetivo, la descripción asi como un manual de uso con capturas
  de pantalla.»
- **S4** — «crea issue, rama y pr hacia main».
- **S5** (respuesta del dueño a la pregunta de licencia) — publicar solo el código propio: el fork
  original no tiene licencia, así que el árbol publicado no puede contener material del autor
  original.
- **S6** (respuesta del dueño) — el README va en español.

## Tareas

| # | Tarea | Specs | Estado |
|---|---|---|---|
| T1 | Auditar los ficheros que se publicarían: credenciales, rutas, hosts, datos personales | S2 | hecho |
| T2 | Sanear los hallazgos: fixture con datos de terceros, hosts y rutas privadas | S2 | hecho |
| T3 | Quitar del árbol el material del autor original y reescribir `NOTICE.md` | S5 | hecho |
| T4 | Verificar que los conectores reescritos no comparten código con el original | S5 | hecho |
| T5 | Base de datos de demostración sintética y script de capturas | S3 | hecho |
| T6 | README: objetivo, descripción, manual de uso y capturas | S3, S6 | hecho |
| T7 | Arreglar las 4 pruebas de portada que dependían del mes en curso | S4 | hecho |
| T8 | Publicar: commit inicial en `main`, issue, rama y PR | S1, S4 | hecho |

## Evidencia

### T1 — auditoría (2026-11-03)

Sobre los 78 ficheros rastreados por git (`git ls-files`, sin `report/images/`):

- **Credenciales: ninguna.** No hay contraseñas, tokens, claves de API ni cookies en el árbol. Las
  cookies se leen del perfil de Firefox en tiempo de ejecución y no se escriben (`supermarket/firefox.py`).
  `git grep` de `password|secret|token|api_key|authorization|cookie|bearer` no devuelve ninguna
  asignación con valor.
- **Datos de compra: fuera de git por `.gitignore`.** `samples/inbox/` (PDF reales), `report/`
  (bases SQLite con 118 compras reales, fotos e informes) y `web/screenshots/` no están rastreados.
  Comprobado con `git status --ignored`.
- **Datos de terceros en fixtures: encontrados y quitados.** El ticket de ejemplo de MAS llevaba
  «ATENDIDO POR \<nombre y apellido del personal de tienda\>» y el de Día el número de empleado; el
  nombre y el número se han eliminado. El NIF y la dirección que quedan son del supermercado, datos
  públicos.
- **Infraestructura privada: encontrada y quitada.** `192.168.1.245`, `dev.albertucho.home`,
  `/home/alberto/...` aparecían en el `Dockerfile`, `scripts/sincronizar.sh`, `docs/cron.md` y
  `odd/tasks/compras-web.md`. Ahora el host sale de `COMPRAS_HOST` o de `scripts/host.local`
  (ignorado), las rutas de casa son `~/...` y los comentarios del contenedor son genéricos.

### T3 y T4 — independencia del código heredado

Comparación línea a línea de los dos ficheros que nacieron del fork con los del proyecto original
(`git show upstream/HEAD:<fichero>`), normalizando espacios en blanco:

```
supmarket/mercadona.py  común: import requests · import time · }
supmarket/dia.py        común: import os · import time · try: · }
```

No hay ninguna sentencia compartida: lo único común son importaciones, bloques y llaves. Se
retiraron `docs/README-original.md` (copia del README del autor), `docs/guia-cookies-original.pdf`
(copia de su «Guia env.pdf») y `example.env` (sus dos variables de cookie, ya sin uso porque la
sesión se lee de Firefox). `NOTICE.md` cuenta el origen y qué se publica.

### T5 y T6 — capturas y README

`scripts/demo.py` construye una base con 6 compras inventadas (3 cadenas, 3 meses, productos
repetidos, una oferta de Amazon) y `scripts/capturas.py` la sirve y hace las cinco capturas de
`docs/img/`. Ninguna captura contiene una compra real: la primera versión usaba
`web/screenshots/`, que sí las tenía, y se descartó por eso.

### T7 — pruebas de portada

Cuatro pruebas de `tests/test_web.py` pedían `/` y esperaban datos de octubre de 2026; al cambiar el
mes en curso la portada abre noviembre y el historial sale vacío. Ahora piden `/‍?mes=2026-10`
explícitamente. La aplicación no cambia: la portada sigue abriendo el mes en curso (lo cubre
`test_index_defaults_to_the_current_month`, que ya usaba `current_month()`).

Suite completa: `387 passed` antes de añadir la prueba de la demo.

### T8 publicación

`origin` = `git@github.com:yerbajo/supermarket-ticket-analyzer.git`. `main` arranca con un único
commit de importación (el historial del fork no se sube) y el trabajo de documentación va en una
rama con su PR.

## Log

- L1 (dueño): «he creado un repo para subir esta app https://github.com/yerbajo/supermarket-ticket-analyzer. Quiero que revises que no existan datos sensibles. genera un readme con el objetivo, la descripción asi como un manual de uso con capturas de pantalla. crea issue, rama y pr hacia main»
- L2 (dueño, tras la pregunta de licencia): «Solo lo propio: reescribir conectores».
- L3 (dueño, idioma del README): «Español».
