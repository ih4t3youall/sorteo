# Sorteo — La Bestia

Sistema de sorteo para clientes. El cajero entra con su **usuario + PIN del Sistema de Registros**, escribe el DNI del
cliente (si no existe lo da de alta) y carga el número de ticket. Todos los días a las **23:59** los tickets se cruzan con
el archivo de ventas que subió un administrador, se guardan las compras con sus artículos y se calculan las chances.

## Arrancar

```bash
./run.sh                 # usa el modo de config.json (dev por defecto) y el puerto 8781
MODO=prod ./run.sh       # producción: valida contra el Sistema de Registros en 192.168.0.150:8780
python3 servidor.py --dev        # en la consola, Sistema de Registros en localhost:8780
```

`config.json`:

| Clave | Qué es |
|---|---|
| `modo` | `dev` (Registros en `localhost:8780`) o `prod` (Registros en `192.168.0.150:8780`). `--dev`/`--prod` o `MODO=` lo pisan |
| `puerto` | Puerto del sorteo (8781) |
| `roles_permitidos` | Roles del Sistema de Registros que pueden entrar (`cajero`). Los admins de Registros entran siempre |
| `digitos_ticket` | Cuántos dígitos finales se comparan al cruzar (8: el cajero escribe `00056066`, el archivo trae `0003-00056066`) |
| `hora_corte` | Hora del cruce automático y del deslogueo de todos (`23:59`) |
| `dias_retener_archivos` | Días que se guardan los renglones de un archivo esperando cruce; después solo quedan las ventas cruzadas |
| `usuario_local` | Usuario temporal `{activo, usuario, nombre, pin}` para cuando no hay red con Registros (ver abajo). Con `activo: false` queda desactivado al instante, sin reiniciar |
| `apliton` | Lugar para la API de Apliton (todavía sin definir: `traer_ventas_apliton()` en `servidor.py`) |

## Arranque automático

`./run.sh` deja el programa andando y además configurado para arrancar solo al prender la máquina:

| Dónde | Cómo |
|---|---|
| Linux con systemd y sudo | Servicio `sorteo-la-bestia` (si se cae, se reinicia) |
| Linux sin eso | Línea `@reboot` en el cron del usuario |
| Windows, Git Bash como administrador | Tarea programada «Sorteo La Bestia»: arranca sin que nadie inicie sesión. También abre el puerto en el firewall |
| Windows, Git Bash sin administrador | Carpeta Inicio: arranca cuando alguien inicia sesión. El firewall hay que abrirlo a mano |

En Windows hace falta [Git for Windows](https://git-scm.com/download/win) para correr `run.sh`, y corre sin ventana (el log queda en
`datos/servidor.log`). `./run.sh --desinstalar` saca el arranque automático. En cada servidor conviene poner el `modo` en
`config.json`: con `MODO=prod ./run.sh` solo, un `--reiniciar` posterior sin `MODO` vuelve al modo del archivo.

## Exportar e importar todos los datos

```bash
./run.sh --exportar                     # queda en respaldos/sorteo_exportado_<fecha>.zip
./run.sh --exportar /ruta/archivo.zip
./run.sh --importar /ruta/archivo.zip
```

Exportar se puede hacer con el programa andando. Trae todas las tablas menos las sesiones, incluidos los renglones que
esperan cruce. No trae `config.json` (cada instalación tiene el suyo). El `.zip` tiene datos personales de los clientes.

Importar **reemplaza todos los datos** de esa instalación: si ya tiene datos pide escribir `borrar todo`, hace un respaldo
antes y, si algo falla, no cambia nada. Apaga el programa mientras tanto y lo vuelve a levantar. Las filas se cargan por
nombre de columna, así que el archivo sirve para una versión más nueva: las columnas nuevas toman su valor por defecto y
las que ya no existen se ignoran. Si una columna nueva es obligatoria y no tiene valor por defecto, la importación falla.

## Cómo se conecta con el Sistema de Registros

El sorteo **solo lee**: manda `{usuario, pin}` a `POST /api/login` de Registros, toma los datos de la persona
(`nombre`, `sector`, `es_admin`, `roles_nombres`), cierra esa sesión allá (`POST /api/logout`) y abre una propia.
No modifica nada en Registros. Si el usuario no tiene el rol `cajero` (ni es admin) no entra.
Todas las sesiones del sorteo (también las de los admins) vencen a las 23:59.

## Usuario temporal (sin el Sistema de Registros)

Mientras el servidor no llegue al Sistema de Registros, en `config.json` hay un usuario (`caja.temporal`) que entra sin
consultar allá. Tiene los mismos permisos que un cajero: solo carga y edición. Lo comparten todos, así que los tickets
quedan a nombre de «Caja temporal». Cinco PIN equivocados lo bloquean 5 minutos.

Se desactiva de dos formas, y las dos le cierran la sesión al momento: cualquier admin desde el **Panel**, o poniendo
`"activo": false` en `config.json`. Sin conexión con Registros no puede entrar ningún admin ni otro cajero.

## Quién hace qué

- **Cajero:** buscar un DNI, dar de alta un cliente, modificar sus datos (todo menos el DNI) y cargar tickets.
- **Admin** (admin del Sistema de Registros): además ve clientes, tickets, archivos, campañas, chances, sorteos y exporta.

## Reglas

- El DNI no se repite y no se puede cambiar. El cliente tiene que ser mayor de 18.
- Un número de ticket se carga una sola vez (se compara por los últimos 8 dígitos). No puede ser igual al DNI del cliente.
- Cada ticket guarda quién lo cargó (nombre, usuario y sector del cajero). `punto_venta` queda vacío hasta que Registros lo informe.
- Cruce: por cada ticket pendiente se buscan renglones del archivo cuyo ticket termine en esos 8 dígitos. Si hay uno, se confirma.
  Si hay varios (mismo número en dos cajas) y solo uno es del día en que se cargó, es ese; si no, queda **ambiguo** y el admin elige.
- Total de la compra = Σ cant. × precio unitario final. Solo se guardan las ventas que cruzan con un ticket cargado.
- Campañas: `por_ticket` = 1 chance por ticket ≥ monto · `por_monto` = 1 chance cada `monto` pesos (con tope opcional por ticket).
  Cuenta la fecha de emisión del ticket.
- Sorteo: cada chance es un número del 1 al total; se sortea uno al azar (`secrets`) y gana su dueño. Un cliente gana una
  sola vez por sorteo. Queda un acta con las reglas, el padrón completo y su huella SHA-256.

## Archivo de ventas

`.xlsx` o `.csv` (`;` o `,`). El encabezado puede estar en cualquiera de las primeras 15 filas. Columnas que reconoce
(sin importar mayúsculas, acentos ni puntos): **Ticket**, Emisión, Razón social, **Producto** (código), **Cant.**,
Cant. items, Bonif., **Precio unitario final** (en negrita, obligatorias). Las demás columnas se guardan en `extra`.

## Base de datos (`datos/sorteo.db`)

`clientes`, `clientes_cambios` (historial), `tickets`, `ventas` (una por ticket confirmado), `venta_items` (un renglón por
artículo), `productos` (código; `descripcion`, `rubro` y `marca` vacíos hasta importar el catálogo), `archivos`,
`filas_archivo` (temporal), `corridas` (cada cruce), `campanas`, `sorteos` (actas), `sesiones`.

## Endpoints

Todo JSON. Login con cookie o `Authorization: Bearer <token>`. Lista completa: `GET /api`.

| Método | Ruta | Acceso | Qué hace |
|---|---|---|---|
| GET | `/api/estado` | público | `{sistema, modo, registros_url, registros_ok}` |
| POST | `/api/login` | público | `{usuario, pin}` → `{token, usuario}`. El usuario temporal se valida contra `config.json` |
| POST | `/api/logout` | usuario | Cierra la sesión |
| GET | `/api/yo` | usuario | Quién está logueado |
| GET | `/api/clientes/dni/{dni}` | usuario | Cliente o 404 |
| POST | `/api/clientes` | usuario | Alta `{dni, nombre, apellido, nacimiento, telefono, email?, ticket?}` |
| PUT | `/api/clientes/{id}` | usuario | Modifica (menos el DNI) |
| POST | `/api/tickets` | usuario | `{cliente_id, numero}`; 409 si ya está cargado |
| GET / PUT | `/api/usuario-local` | admin | Estado del usuario temporal / `{activo}` |
| GET | `/api/clientes` · `/api/clientes/{id}` | admin | Lista / detalle con tickets y cambios |
| GET | `/api/tickets` · `/api/tickets/{id}` | admin | Lista (`estado`, `q`, `desde`, `hasta`) / detalle con artículos |
| POST | `/api/tickets/{id}/resolver` | admin | Ticket ambiguo: `{ticket_archivo}` |
| DELETE | `/api/tickets/{id}` | admin | Borra un ticket cargado por error |
| POST | `/api/archivos` | admin | `{nombre, datos: base64}` |
| GET / DELETE | `/api/archivos` · `/api/archivos/{id}` | admin | Lista con los cruces / borra |
| POST | `/api/procesar` | admin | Cruce ahora |
| GET / POST / PUT / DELETE | `/api/campanas[/{id}]` | admin | Campañas `{nombre, desde, hasta, modo, monto, max_chances_ticket?, activa?, notas?}` |
| GET | `/api/campanas/{id}/chances` | admin | Padrón con chances |
| POST | `/api/campanas/{id}/sortear` | admin | `{ganadores, descripcion?, excluir_ganadores_previos?}` → acta |
| GET | `/api/sorteos` · `/api/sorteos/{id}` | admin | Actas |
| GET | `/api/exportar/{clientes\|tickets\|articulos}` | admin | CSV para análisis |
| GET | `/api/resumen` | admin | Números del panel |
