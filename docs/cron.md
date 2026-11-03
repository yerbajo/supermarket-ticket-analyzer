# Tareas programadas (cron)

## Línea de crontab

Una ejecución diaria a las 06:30 que descarga los tres catálogos y regenera el informe:

```cron
30 6 * * * cd ~/tickets-precios && xvfb-run -a .venv/bin/python tickets_cli.py actualizar >> ~/logs/tickets-precios.log 2>&1; echo "exit=$? $(date -Is)" >> ~/logs/tickets-precios.log
```

Crea el directorio del log una vez antes de instalar la línea:

```bash
mkdir -p ~/logs
```

Ajusta la ruta del repo y la de `~/logs` a las tuyas. El comando usa `tickets.db` en el directorio
del repo (o el fichero que indique `DB_PATH`); si quieres fijarlo tú, añade `--db /ruta/tickets.db`.

## Por qué `xvfb-run -a`

`www.dia.es` está detrás de Akamai: HTTP plano y Chromium headless reciben `403 Access Denied`.
Lo único que funciona es un Chromium con ventana real. En una máquina con escritorio la ventana
aparece en la sesión del usuario; en un servidor no hay pantalla, así que `xvfb-run -a` levanta un
display virtual sólo para ese proceso. Sin él, la cadena Día falla (y el comando sale con código 1).

`actualizar` y `precios` son los únicos comandos que tocan el navegador; `informe` y `estado` no lo
necesitan.

## Dónde va la salida

- El informe HTML: `report/informe.html` (o lo que ponga `--salida`). El directorio se crea solo.
- Los catálogos: la base de datos SQLite, por defecto `tickets.db` en el repo. Tanto el fichero
  `.db` como `report/` están en `.gitignore`.
- Por pantalla: una línea de resumen por cadena. Si una cadena falla, el error va a la salida de
  error y el proceso sigue con las demás.

## Dónde va el log

Donde apunte el `>>` de la línea de crontab (`~/logs/tickets-precios.log` en el ejemplo). La
redirección `2>&1` es imprescindible: sin ella los errores de una cadena no quedan registrados.

## Cómo comprobar el código de salida

Una ejecución manual:

```bash
cd ~/tickets-precios
xvfb-run -a .venv/bin/python tickets_cli.py actualizar; echo "exit=$?"
```

- `0`: todas las cadenas termiaron y el informe se escribió.
- `1`: alguna cadena falló; las demás siguen y el informe se genera igualmente.
- `2`: argumentos incorrectos (por ejemplo, una `--cadena` que no existe).

La línea de crontab del principio escribe `exit=N` al final del log, así que para comprobar la
última ejecución basta:

```bash
tail -n 1 ~/logs/tickets-precios.log
grep -n "error" ~/logs/tickets-precios.log
```
