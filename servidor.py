#!/usr/bin/env python3
"""Sorteo - La Bestia.

El cajero entra con su usuario + PIN del Sistema de Registros, escribe el DNI del cliente
(si no existe lo da de alta) y carga el número de ticket. Todos los días a las 23:59 los
tickets cargados se cruzan con el archivo de ventas que subió un administrador (más adelante,
con la API de Apliton) y se guardan las compras con sus artículos. Con eso se calculan las
chances de cada campaña y se hace el sorteo.

Uso:  python3 servidor.py                (modo de config.json; dev por defecto)
      python3 servidor.py --prod         (Sistema de Registros en 192.168.0.150)
      python3 servidor.py --dev          (Sistema de Registros en localhost)
      python3 servidor.py 8782           (otro puerto)
      python3 servidor.py --no-browser

Configuración en config.json. Datos en datos/sorteo.db (SQLite). Respaldos en respaldos/.
Solo usa la biblioteca estándar de Python 3. Endpoints: GET /api (o ver API.md).
"""
import base64, csv, hashlib, hmac, io, json, math, os, re, secrets, shutil, socket, sqlite3, sys
import threading, time, traceback, unicodedata, urllib.error, urllib.request, webbrowser, zipfile
from datetime import date, datetime, timedelta
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from xml.etree import ElementTree as ET

CARPETA = os.path.dirname(os.path.abspath(__file__))
DATOS = os.path.join(CARPETA, "datos")
DB = os.path.join(DATOS, "sorteo.db")
RESPALDOS = os.path.join(CARPETA, "respaldos")
MAX_RESPALDOS = 30
MAX_CUERPO = 25 * 1024 * 1024  # el archivo de ventas viaja en base64
COOKIE = "sorteo_sesion"
EDAD_MINIMA = 18
MAX_FALLOS_PIN = 5
BLOQUEO_SEG = 5 * 60
ID_USUARIO_LOCAL = 0  # en el Sistema de Registros los ids empiezan en 1


def cargar_config():
    cfg = {"modo": "dev", "puerto": 8781, "registros": {"dev": "http://localhost:8780", "prod": "http://192.168.0.150:8780"},
           "roles_permitidos": ["cajero"], "digitos_ticket": 8, "hora_corte": "23:59", "dias_retener_archivos": 7,
           "apliton": {"activo": False, "url": "", "token": ""}}
    ruta = os.path.join(CARPETA, "config.json")
    if os.path.exists(ruta):
        with open(ruta, encoding="utf-8") as f:
            cfg.update(json.load(f))
    if "--prod" in sys.argv:
        cfg["modo"] = "prod"
    elif "--dev" in sys.argv:
        cfg["modo"] = "dev"
    elif os.environ.get("SORTEO_MODO") in ("dev", "prod"):
        cfg["modo"] = os.environ["SORTEO_MODO"]
    if cfg["modo"] not in ("dev", "prod"):
        sys.exit(f"config.json: modo tiene que ser dev o prod (es {cfg['modo']!r})")
    puerto = next((int(a) for a in sys.argv[1:] if a.isdigit()), None)
    if puerto:
        cfg["puerto"] = puerto
    cfg["registros_url"] = cfg["registros"][cfg["modo"]].rstrip("/")
    cfg["roles_permitidos"] = [r.strip().lower() for r in cfg["roles_permitidos"]]
    return cfg


CFG = cargar_config()

ESQUEMA = """
CREATE TABLE IF NOT EXISTS sesiones (
    token_hash TEXT PRIMARY KEY,
    usuario_id INTEGER NOT NULL,      -- id del usuario en el Sistema de Registros
    usuario TEXT NOT NULL,
    nombre TEXT NOT NULL,
    sector TEXT NOT NULL DEFAULT '',
    es_admin INTEGER NOT NULL DEFAULT 0,
    roles TEXT NOT NULL DEFAULT '[]',
    creado TEXT NOT NULL,
    vence TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS clientes (
    id INTEGER PRIMARY KEY,
    dni TEXT NOT NULL UNIQUE,
    nombre TEXT NOT NULL,
    apellido TEXT NOT NULL,
    nacimiento TEXT NOT NULL,          -- AAAA-MM-DD
    telefono TEXT NOT NULL,
    email TEXT NOT NULL DEFAULT '',
    creado TEXT NOT NULL,
    creado_por_id INTEGER, creado_por TEXT NOT NULL DEFAULT '',
    modificado TEXT, modificado_por TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS clientes_cambios (
    id INTEGER PRIMARY KEY,
    cliente_id INTEGER NOT NULL REFERENCES clientes(id) ON DELETE CASCADE,
    fecha TEXT NOT NULL,
    usuario_id INTEGER, usuario TEXT NOT NULL,
    antes TEXT NOT NULL, despues TEXT NOT NULL  -- JSON solo con los campos que cambiaron
);
CREATE TABLE IF NOT EXISTS tickets (
    id INTEGER PRIMARY KEY,
    numero TEXT NOT NULL,              -- como lo escribió el cajero
    clave TEXT NOT NULL UNIQUE,        -- últimos N dígitos (con ceros) para cruzar con el archivo
    cliente_id INTEGER NOT NULL REFERENCES clientes(id),
    cargado TEXT NOT NULL,
    cajero_id INTEGER, cajero_usuario TEXT NOT NULL DEFAULT '', cajero_nombre TEXT NOT NULL DEFAULT '', cajero_sector TEXT NOT NULL DEFAULT '',
    punto_venta TEXT,                  -- todavía no lo informa el Sistema de Registros
    estado TEXT NOT NULL DEFAULT 'pendiente',  -- pendiente | confirmado | ambiguo
    detalle_estado TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS tickets_cliente ON tickets(cliente_id);
CREATE INDEX IF NOT EXISTS tickets_estado ON tickets(estado);
CREATE TABLE IF NOT EXISTS ventas (
    id INTEGER PRIMARY KEY,
    ticket_id INTEGER NOT NULL UNIQUE REFERENCES tickets(id) ON DELETE CASCADE,
    ticket_archivo TEXT NOT NULL UNIQUE,  -- número completo como viene en el archivo / API (con prefijo)
    emision TEXT,                         -- AAAA-MM-DD HH:MM:SS
    razon_social TEXT NOT NULL DEFAULT '',
    total REAL NOT NULL,                  -- suma de cant. x precio unitario final
    unidades REAL NOT NULL,
    cant_items REAL,                      -- como viene en el archivo
    renglones INTEGER NOT NULL,
    origen TEXT NOT NULL,                 -- archivo | api
    archivo_id INTEGER,
    confirmado TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS venta_items (
    id INTEGER PRIMARY KEY,
    venta_id INTEGER NOT NULL REFERENCES ventas(id) ON DELETE CASCADE,
    renglon INTEGER NOT NULL,
    producto TEXT NOT NULL,               -- código
    cantidad REAL NOT NULL,
    bonif REAL,
    precio_unitario_final REAL NOT NULL,
    subtotal REAL NOT NULL,
    extra TEXT NOT NULL DEFAULT '{}'      -- otras columnas del archivo, por si sirven después
);
CREATE INDEX IF NOT EXISTS items_producto ON venta_items(producto);
CREATE TABLE IF NOT EXISTS productos (
    codigo TEXT PRIMARY KEY,
    descripcion TEXT,                     -- se completa cuando se importe el catálogo
    rubro TEXT,
    marca TEXT,
    actualizado TEXT
);
CREATE TABLE IF NOT EXISTS archivos (
    id INTEGER PRIMARY KEY,
    nombre TEXT NOT NULL,
    subido TEXT NOT NULL,
    subido_por TEXT NOT NULL,
    filas INTEGER NOT NULL,
    tickets INTEGER NOT NULL,
    desde TEXT, hasta TEXT,               -- rango de emisión que trae
    columnas TEXT NOT NULL DEFAULT '{}',
    procesado TEXT                        -- última vez que entró en un cruce
);
-- Filas del archivo esperando el cruce. Se borran a los N días (solo quedan las ventas que cruzaron).
CREATE TABLE IF NOT EXISTS filas_archivo (
    id INTEGER PRIMARY KEY,
    archivo_id INTEGER NOT NULL REFERENCES archivos(id) ON DELETE CASCADE,
    renglon INTEGER NOT NULL,
    ticket TEXT NOT NULL, clave TEXT NOT NULL,
    emision TEXT, razon_social TEXT NOT NULL DEFAULT '',
    producto TEXT NOT NULL, cantidad REAL NOT NULL, cant_items REAL, bonif REAL, precio REAL NOT NULL,
    extra TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS filas_clave ON filas_archivo(clave);
CREATE TABLE IF NOT EXISTS corridas (
    id INTEGER PRIMARY KEY,
    fecha TEXT NOT NULL,
    motivo TEXT NOT NULL,                 -- automático | manual
    usuario TEXT NOT NULL DEFAULT '',
    confirmados INTEGER NOT NULL, ambiguos INTEGER NOT NULL, pendientes INTEGER NOT NULL,
    detalle TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS campanas (
    id INTEGER PRIMARY KEY,
    nombre TEXT NOT NULL,
    desde TEXT NOT NULL, hasta TEXT NOT NULL,  -- fechas de emisión de los tickets que cuentan
    modo TEXT NOT NULL,                        -- por_ticket: 1 chance si el ticket >= monto · por_monto: 1 chance cada monto
    monto REAL NOT NULL,
    max_chances_ticket INTEGER,                -- tope por ticket (opcional)
    activa INTEGER NOT NULL DEFAULT 1,
    notas TEXT NOT NULL DEFAULT '',
    creada TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sorteos (
    id INTEGER PRIMARY KEY,
    campana_id INTEGER NOT NULL REFERENCES campanas(id),
    fecha TEXT NOT NULL,
    realizado_por TEXT NOT NULL,
    descripcion TEXT NOT NULL DEFAULT '',
    reglas TEXT NOT NULL,                      -- la campaña tal como estaba
    participantes INTEGER NOT NULL,
    chances_total INTEGER NOT NULL,
    padron TEXT NOT NULL,                      -- JSON [{cliente_id, dni, nombre, chances}]
    padron_hash TEXT NOT NULL,
    ganadores TEXT NOT NULL                    -- JSON [{puesto, cliente_id, dni, nombre, telefono, email, chances, numero_sorteado}]
);
CREATE TABLE IF NOT EXISTS ajustes (clave TEXT PRIMARY KEY, valor TEXT NOT NULL);
"""

_lock = threading.RLock()
_con = None


def iniciar_db():
    global _con
    os.makedirs(DATOS, exist_ok=True)
    os.makedirs(RESPALDOS, exist_ok=True)
    _con = sqlite3.connect(DB, check_same_thread=False)
    _con.row_factory = sqlite3.Row
    _con.execute("PRAGMA journal_mode = WAL")
    _con.execute("PRAGMA foreign_keys = ON")
    _con.executescript(ESQUEMA)
    _con.commit()


def filas(sql, args=()):
    with _lock:
        return _con.execute(sql, args).fetchall()


def fila(sql, args=()):
    with _lock:
        return _con.execute(sql, args).fetchone()


def ejecutar(sql, args=()):
    with _lock, _con:
        return _con.execute(sql, args).lastrowid


def ajuste(clave, valor=None):
    if valor is None:
        r = fila("SELECT valor FROM ajustes WHERE clave = ?", (clave,))
        return r[0] if r else None
    ejecutar("INSERT INTO ajustes VALUES (?, ?) ON CONFLICT(clave) DO UPDATE SET valor = excluded.valor", (clave, valor))


def respaldar():
    destino = os.path.join(RESPALDOS, f"sorteo_{datetime.now():%Y-%m-%d_%H-%M-%S}.db")
    with _lock:
        copia = sqlite3.connect(destino)
        _con.backup(copia)
        copia.close()
    viejos = sorted(f for f in os.listdir(RESPALDOS) if f.startswith("sorteo_") and f.endswith(".db"))
    for f in viejos[:-MAX_RESPALDOS]:
        os.remove(os.path.join(RESPALDOS, f))
    return destino


# ------------------------------------------------------ exportar / importar ---
# Tabla por tabla y por nombre de columna, para poder importar en una versión más nueva aunque cambie la base.
# Se usa desde run.sh (--exportar / --importar): así funciona aunque ningún admin pueda entrar a la página.

FORMATO_EXPORTACION = 1
TABLAS_SIN_EXPORTAR = {"sesiones"}


def tablas_db():
    return [r[0] for r in _con.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")
            if r[0] not in TABLAS_SIN_EXPORTAR]


def columnas_db(tabla):
    return [r[1] for r in _con.execute(f'PRAGMA table_info("{tabla}")')]


def exportar(destino=None):
    destino = destino or os.path.join(RESPALDOS, f"sorteo_exportado_{datetime.now():%Y-%m-%d_%H-%M-%S}.zip")
    _con.execute("BEGIN")  # una sola foto de la base, aunque el servidor esté guardando
    try:
        tablas = {t: {"columnas": columnas_db(t), "filas": [list(r) for r in _con.execute(f'SELECT * FROM "{t}"')]} for t in tablas_db()}
    finally:
        _con.rollback()
    contenido = {"sistema": "sorteo", "formato": FORMATO_EXPORTACION, "exportado": ahora(), "tablas": tablas}
    temporal = destino + ".tmp"
    with zipfile.ZipFile(temporal, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("sorteo.json", json.dumps(contenido, ensure_ascii=False))
    os.replace(temporal, destino)
    for t, d in tablas.items():
        print(f"  {t}: {len(d['filas'])}")
    print(f"Exportado en: {destino}")


def leer_exportacion(origen):
    try:
        with zipfile.ZipFile(origen) as z:
            d = json.loads(z.read("sorteo.json").decode("utf-8"))
    except (OSError, zipfile.BadZipFile, KeyError, ValueError) as e:
        raise ValueError(f"el archivo no es una exportación del sorteo ({e})")
    if not isinstance(d, dict) or d.get("sistema") != "sorteo" or not isinstance(d.get("tablas"), dict):
        raise ValueError("el archivo no es una exportación del sorteo")
    if not isinstance(d.get("formato"), int) or d["formato"] > FORMATO_EXPORTACION:
        raise ValueError("el archivo viene de una versión más nueva del sorteo")
    for t, x in d["tablas"].items():
        if not isinstance(x.get("columnas"), list) or not isinstance(x.get("filas"), list) or any(len(f) != len(x["columnas"]) for f in x["filas"]):
            raise ValueError(f"la tabla {t} del archivo está dañada")
    return d


def importar(origen, confirmar=False):
    """Sin confirmar solo revisa el archivo: devuelve 10 si esta instalación ya tiene datos (se van a reemplazar)."""
    d = leer_exportacion(origen)
    locales = tablas_db()
    print(f"Archivo exportado el {d.get('exportado')}:")
    for t, x in d["tablas"].items():
        print(f"  {t}: {len(x['filas'])}" + ("" if t in locales else "  (esta versión no tiene esa tabla: se ignora)"))
    actuales = sum(_con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in locales)
    if not confirmar:
        if actuales:
            print(f"Esta instalación ya tiene {actuales} filas: importar las reemplaza todas.")
        return 10 if actuales else 0
    print(f"Respaldo previo: {respaldar()}")
    try:
        _con.execute("BEGIN IMMEDIATE")
        _con.execute("PRAGMA defer_foreign_keys = ON")
        for t in locales + list(TABLAS_SIN_EXPORTAR):
            _con.execute(f'DELETE FROM "{t}"')
        for t, x in d["tablas"].items():
            if t not in locales:
                continue
            destino = set(columnas_db(t))
            indices = [i for i, c in enumerate(x["columnas"]) if c in destino]
            sobran = [c for c in x["columnas"] if c not in destino]
            if sobran:
                print(f"  {t}: se ignoran las columnas {', '.join(sobran)}")
            lista = ", ".join(f'"{x["columnas"][i]}"' for i in indices)
            _con.executemany(f'INSERT INTO "{t}" ({lista}) VALUES ({", ".join("?" * len(indices))})',
                             [[f[i] for i in indices] for f in x["filas"]])
        rotas = _con.execute("PRAGMA foreign_key_check").fetchall()
        if rotas:
            raise ValueError(f"{len(rotas)} filas apuntan a datos que no están en el archivo")
        _con.commit()
    except (sqlite3.Error, ValueError):
        _con.rollback()
        raise
    print("Importado. Esta instalación quedó igual que el archivo (las sesiones abiertas se cerraron).")
    return 0


# ---------------------------------------------------------------- general ---

class ApiError(Exception):
    def __init__(self, status, mensaje, **extra):
        super().__init__(mensaje)
        self.status, self.mensaje, self.extra = status, mensaje, extra


class Respuesta:
    def __init__(self, cuerpo, tipo="application/json; charset=utf-8", status=200, headers=None):
        self.cuerpo = cuerpo if isinstance(cuerpo, bytes) else json.dumps(cuerpo, ensure_ascii=False).encode()
        self.tipo, self.status, self.headers = tipo, status, headers or {}


def ahora():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def texto(cuerpo, clave, obligatorio=False, largo=120, etiqueta=None):
    v = cuerpo.get(clave)
    v = "" if v is None else re.sub(r"\s+", " ", str(v)).strip()
    if obligatorio and not v:
        raise ApiError(400, f"Falta {etiqueta or clave}", campo=clave)
    if len(v) > largo:
        raise ApiError(400, f"{(etiqueta or clave).capitalize()} es demasiado largo (máx. {largo})", campo=clave)
    return v


def entero(valor, etiqueta, defecto=None):
    if valor in (None, ""):
        return defecto
    try:
        return int(valor)
    except (TypeError, ValueError):
        raise ApiError(400, f"{etiqueta} inválido")


def fecha_iso(valor, etiqueta, campo=None):
    try:
        return datetime.strptime(str(valor or ""), "%Y-%m-%d").date()
    except ValueError:
        raise ApiError(400, f"{etiqueta}: fecha inválida", campo=campo)


def sin_acentos(t):
    return "".join(c for c in unicodedata.normalize("NFD", t) if unicodedata.category(c) != "Mn")


def fin_de_jornada(momento=None):
    """La hora de corte (23:59) de hoy, o de mañana si ya pasó: ahí vencen todas las sesiones."""
    momento = momento or datetime.now()
    fin = datetime.strptime(f"{momento:%Y-%m-%d} {CFG['hora_corte']}:00", "%Y-%m-%d %H:%M:%S")
    return fin if momento < fin else fin + timedelta(days=1)


# ------------------------------------------------- Sistema de Registros ---

def registros(metodo, ruta, cuerpo=None, token=None, timeout=6):
    """Llama a la API del Sistema de Registros. Devuelve (status, json)."""
    datos = json.dumps(cuerpo).encode() if cuerpo is not None else None
    req = urllib.request.Request(CFG["registros_url"] + ruta, data=datos, method=metodo)
    if datos is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}
    except (urllib.error.URLError, OSError, ValueError):
        raise ApiError(502, f"No me puedo conectar con el Sistema de Registros ({CFG['registros_url']}). Avisale al administrador.")


def puede_entrar(u):
    return bool(u.get("es_admin")) or any(r.lower() in CFG["roles_permitidos"] for r in u.get("roles_nombres") or [])


# --------------------------------------------------------- usuario temporal ---
# Para cuando el servidor no llega al Sistema de Registros: entra sin pasar por allá y solo puede cargar y editar.

_fallos_local = [0, 0.0]  # fallos, bloqueado_hasta


def usuario_local():
    """El usuario de config.json, o None si no hay. Se lee en cada uso: desactivarlo en el archivo no pide reiniciar."""
    try:
        with open(os.path.join(CARPETA, "config.json"), encoding="utf-8") as f:
            u = json.load(f).get("usuario_local")
    except (OSError, ValueError) as e:
        print(f"config.json no se pudo leer: {e}")
        return None
    if not u or not u.get("usuario") or not re.fullmatch(r"\d{4}", str(u.get("pin") or "")):
        return None
    en_archivo = u.get("activo") is True
    return {"usuario": str(u["usuario"]).strip().lower(), "nombre": u.get("nombre") or "Usuario temporal", "pin": str(u["pin"]),
            "en_archivo": en_archivo, "activo": en_archivo and ajuste("usuario_local_desactivado") != "1"}


def login_local(local, pin):
    if not local["activo"]:
        raise ApiError(403, "El usuario temporal está desactivado")
    with _lock:
        if _fallos_local[1] > time.time():
            raise ApiError(429, f"Demasiados intentos. Probá de nuevo en {int((_fallos_local[1] - time.time()) // 60) + 1} min.")
        if not hmac.compare_digest(pin.encode(), local["pin"].encode()):
            _fallos_local[0] += 1
            if _fallos_local[0] >= MAX_FALLOS_PIN:
                _fallos_local[:] = [0, time.time() + BLOQUEO_SEG]
            raise ApiError(401, "Usuario o PIN incorrecto")
        _fallos_local[:] = [0, 0.0]
    return respuesta_sesion({"id": ID_USUARIO_LOCAL, "usuario": local["usuario"], "nombre": local["nombre"]})


# -------------------------------------------------------------- sesiones ---

def _hash_token(token):
    return hashlib.sha256(token.encode()).hexdigest()


def crear_sesion(u):
    token = secrets.token_urlsafe(32)
    vence = fin_de_jornada()  # todos (también los admins) se desloguean a las 23:59
    ejecutar("DELETE FROM sesiones WHERE vence < ?", (ahora(),))
    ejecutar("INSERT INTO sesiones VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
             (_hash_token(token), u["id"], u["usuario"], u["nombre"], u.get("sector") or "", int(bool(u.get("es_admin"))),
              json.dumps(u.get("roles_nombres") or [], ensure_ascii=False), ahora(), vence.strftime("%Y-%m-%d %H:%M:%S")))
    return token, vence


def respuesta_sesion(u):
    token, vence = crear_sesion(u)
    s = fila("SELECT * FROM sesiones WHERE token_hash = ?", (_hash_token(token),))
    galleta = f"{COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={segundos_hasta(vence)}"
    return Respuesta({"token": token, "usuario": sesion_dict(s)}, headers={"Set-Cookie": galleta})


def segundos_hasta(vence):
    if isinstance(vence, str):
        vence = datetime.strptime(vence, "%Y-%m-%d %H:%M:%S")
    return max(0, int((vence - datetime.now()).total_seconds()))


def sesion_dict(s):
    return {"id": s["usuario_id"], "usuario": s["usuario"], "nombre": s["nombre"], "sector": s["sector"],
            "es_admin": bool(s["es_admin"]), "roles": json.loads(s["roles"]), "sesion_segundos": segundos_hasta(s["vence"])}


# ----------------------------------------------------------------- tickets ---

def clave_ticket(numero):
    """Clave para cruzar: los últimos N dígitos (completando con ceros). El archivo trae un prefijo
    (punto de venta / tipo) que el cajero no escribe, así que se compara solo el final."""
    d = re.sub(r"\D", "", str(numero or ""))
    n = CFG["digitos_ticket"]
    return d[-n:].rjust(n, "0") if d else ""


def validar_ticket(numero):
    numero = str(numero or "").strip()
    if not re.fullmatch(r"\d{1,20}", numero):
        raise ApiError(400, "El número de ticket tiene que tener solo números", campo="ticket")
    if not int(numero):
        raise ApiError(400, "El número de ticket no puede ser 0", campo="ticket")
    return numero, clave_ticket(numero)


def chequear_no_es_dni(numero, dni):
    """Si el cajero escribió el DNI en el lugar del ticket (pasa cuando se apura), se avisa."""
    if numero.lstrip("0") == str(dni).lstrip("0"):
        raise ApiError(400, "Ese número es el DNI del cliente: escribí el número de ticket", campo="ticket")


def chequear_ticket_libre(clave):
    t = fila("""SELECT t.numero, t.cargado, c.nombre, c.apellido, c.dni FROM tickets t JOIN clientes c ON c.id = t.cliente_id
                WHERE t.clave = ?""", (clave,))
    if t:
        cuando = datetime.strptime(t["cargado"], "%Y-%m-%d %H:%M:%S").strftime("%d/%m %H:%M")
        raise ApiError(409, f"El ticket {t['numero']} ya está cargado a nombre de {t['nombre']} {t['apellido']} "
                            f"(DNI {t['dni']}) el {cuando}", campo="ticket")


def insertar_ticket(cliente_id, numero, clave, yo):
    return _con.execute(
        """INSERT INTO tickets (numero, clave, cliente_id, cargado, cajero_id, cajero_usuario, cajero_nombre, cajero_sector)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (numero, clave, cliente_id, ahora(), yo["id"], yo["usuario"], yo["nombre"], yo["sector"])).lastrowid


def ticket_dict(r, con_items=False):
    d = {k: r[k] for k in r.keys()}
    d["cliente"] = f"{r['nombre']} {r['apellido']}" if "nombre" in r.keys() else None
    if con_items and r["venta_id"]:
        d["items"] = [dict(x) for x in filas("""SELECT i.*, p.descripcion, p.rubro, p.marca FROM venta_items i
                                                LEFT JOIN productos p ON p.codigo = i.producto WHERE venta_id = ? ORDER BY renglon""", (r["venta_id"],))]
        for it in d["items"]:
            it["extra"] = json.loads(it["extra"])
    if r["estado"] == "ambiguo" and con_items:
        d["candidatos"] = candidatos(r["clave"])
    return d


SQL_TICKETS = """SELECT t.*, c.dni, c.nombre, c.apellido, v.id AS venta_id, v.ticket_archivo, v.emision, v.total,
                        v.unidades, v.renglones, v.razon_social, v.origen
                 FROM tickets t JOIN clientes c ON c.id = t.cliente_id LEFT JOIN ventas v ON v.ticket_id = t.id"""


# ---------------------------------------------------------------- clientes ---

def normalizar_dni(v):
    dni = re.sub(r"[.\s-]", "", str(v or ""))
    if not re.fullmatch(r"\d{6,10}", dni):
        raise ApiError(400, "El DNI tiene que tener entre 6 y 10 números", campo="dni")
    return dni.lstrip("0") or "0"


def edad(nacimiento, hoy=None):
    hoy = hoy or date.today()
    return hoy.year - nacimiento.year - ((hoy.month, hoy.day) < (nacimiento.month, nacimiento.day))


def datos_cliente(cuerpo):
    d = {
        "nombre": texto(cuerpo, "nombre", True, 60, "el nombre"),
        "apellido": texto(cuerpo, "apellido", True, 60, "el apellido"),
        "telefono": texto(cuerpo, "telefono", True, 30, "el teléfono"),
        "email": texto(cuerpo, "email", False, 120, "el email").lower(),
    }
    nac = fecha_iso(cuerpo.get("nacimiento"), "Fecha de nacimiento", "nacimiento")
    if nac > date.today() or nac.year < 1900:
        raise ApiError(400, "La fecha de nacimiento no es válida", campo="nacimiento")
    if edad(nac) < EDAD_MINIMA:
        raise ApiError(400, f"Tiene que ser mayor de {EDAD_MINIMA} años para participar", campo="nacimiento")
    d["nacimiento"] = nac.isoformat()
    tel = re.sub(r"[^\d+]", "", d["telefono"])
    if not re.fullmatch(r"\+?\d{6,15}", tel):
        raise ApiError(400, "El teléfono no es válido", campo="telefono")
    d["telefono"] = tel
    if d["email"] and not re.fullmatch(r"[^\s@]+@[^\s@.]+(\.[^\s@.]+)*\.[A-Za-z]{2,}", d["email"]):
        raise ApiError(400, "El email no es válido (ej. nombre@gmail.com). Si no tiene, dejalo vacío", campo="email")
    for k in ("nombre", "apellido"):
        d[k] = " ".join(p[:1].upper() + p[1:] for p in d[k].split(" "))
    return d


def cliente_dict(r, admin=False):
    d = {k: r[k] for k in ("id", "dni", "nombre", "apellido", "nacimiento", "telefono", "email", "creado", "creado_por",
                           "modificado", "modificado_por")}
    d["edad"] = edad(date.fromisoformat(r["nacimiento"]))
    if admin:
        d["tickets"] = fila("SELECT COUNT(*) FROM tickets WHERE cliente_id = ?", (r["id"],))[0]
    return d


# --------------------------------------------------------- lectura archivo ---

def _celdas_xlsx(datos):
    """Primera hoja de un .xlsx como lista de filas (solo biblioteca estándar)."""
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    try:
        z = zipfile.ZipFile(io.BytesIO(datos))
    except zipfile.BadZipFile:
        raise ApiError(400, "El archivo no es un Excel .xlsx válido (si es .xls viejo, guardalo como .xlsx o .csv)")
    compartidos = []
    if "xl/sharedStrings.xml" in z.namelist():
        for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall("m:si", ns):
            compartidos.append("".join(t.text or "" for t in si.iter(f"{{{ns['m']}}}t")))
    # primera hoja según workbook.xml
    hoja = "xl/worksheets/sheet1.xml"
    try:
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        primera = wb.find("m:sheets/m:sheet", ns)
        rid = primera.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
        for rel in rels:
            if rel.get("Id") == rid:
                destino = rel.get("Target").lstrip("/")
                hoja = destino if destino.startswith("xl/") else "xl/" + destino
    except (KeyError, AttributeError, ET.ParseError):
        pass
    # estilos con formato fecha, para convertir números de serie
    fechas_estilo = set()
    try:
        st = ET.fromstring(z.read("xl/styles.xml"))
        propios = {n.get("numFmtId"): n.get("formatCode", "") for n in st.findall("m:numFmts/m:numFmt", ns)}
        for i, xf in enumerate(st.findall("m:cellXfs/m:xf", ns)):
            fid = xf.get("numFmtId", "0")
            codigo = propios.get(fid, "")
            if (fid.isdigit() and (14 <= int(fid) <= 22 or 45 <= int(fid) <= 47)) or re.search(r"[dy]|h+:m", re.sub(r'"[^"]*"|\[[^\]]*\]', "", codigo.lower())):
                fechas_estilo.add(str(i))
    except (KeyError, ET.ParseError):
        pass
    salida = []
    for row in ET.fromstring(z.read(hoja)).iter(f"{{{ns['m']}}}row"):
        valores = {}
        for c in row.findall("m:c", ns):
            ref = re.match(r"([A-Z]+)", c.get("r", "A")).group(1)
            col = 0
            for ch in ref:
                col = col * 26 + ord(ch) - 64
            t = c.get("t")
            v = c.find("m:v", ns)
            if t == "s" and v is not None:
                val = compartidos[int(v.text)]
            elif t == "inlineStr":
                val = "".join(x.text or "" for x in c.iter(f"{{{ns['m']}}}t"))
            elif v is None:
                val = ""
            elif t in ("str", "e"):
                val = v.text or ""
            elif t == "b":
                val = v.text == "1"
            else:
                val = float(v.text)
                if c.get("s") in fechas_estilo:
                    val = datetime(1899, 12, 30) + timedelta(days=val)
                elif val.is_integer():
                    val = int(val)
            valores[col - 1] = val
        if valores:
            salida.append([valores.get(i, "") for i in range(max(valores) + 1)])
    return salida


def _celdas_csv(datos):
    for cod in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            txt = datos.decode(cod)
            break
        except UnicodeDecodeError:
            continue
    muestra = txt[:5000]
    sep = max((";", ",", "\t", "|"), key=muestra.count)
    return [fila_ for fila_ in csv.reader(io.StringIO(txt), delimiter=sep)]


COLUMNAS = [  # (clave, nombres posibles ya normalizados, obligatoria)
    ("cant_items", ["cant items", "cantidad items", "cant de items", "items"], False),
    ("ticket", ["ticket", "nro ticket", "n ticket", "numero ticket", "comprobante", "nro comprobante", "numero"], True),
    ("emision", ["emision", "fecha emision", "fecha", "fecha hora"], False),
    ("razon_social", ["razon social", "cliente", "nombre cliente"], False),
    ("producto", ["producto", "codigo", "cod producto", "codigo producto", "articulo", "cod articulo"], True),
    ("bonif", ["bonif", "bonificacion", "descuento", "bonif %"], False),
    ("precio", ["precio unitario final", "precio unit final", "precio final", "precio unitario", "precio"], True),
    ("cantidad", ["cant", "cantidad", "unidades"], True),
]


def _norm_col(t):
    return re.sub(r"[^a-z0-9%]+", " ", sin_acentos(str(t).lower())).strip()


def detectar_columnas(encabezado):
    norm = [_norm_col(c) for c in encabezado]
    usadas, mapa = set(), {}
    for clave, nombres, _ in COLUMNAS:
        for nombre in nombres:
            i = next((i for i, n in enumerate(norm) if n == nombre and i not in usadas), None)
            if i is not None:
                mapa[clave] = i
                usadas.add(i)
                break
    return mapa


def numero(v, etiqueta, renglon):
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    s = str(v or "").strip().replace("$", "").replace(" ", "").replace("%", "")
    if not s:
        return None
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    elif "," in s:
        s = s.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"-?\d{1,3}(\.\d{3})+", s):
        s = s.replace(".", "")
    try:
        return float(s)
    except ValueError:
        raise ApiError(400, f"Renglón {renglon}: «{v}» no es un número válido en {etiqueta}")


def fecha_hora(v):
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(v, (int, float)) and not isinstance(v, bool) and 20000 < v < 80000:
        return (datetime(1899, 12, 30) + timedelta(days=float(v))).strftime("%Y-%m-%d %H:%M:%S")
    s = str(v or "").strip()
    for f in ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M",
              "%Y-%m-%d", "%d-%m-%Y %H:%M:%S", "%d-%m-%Y %H:%M", "%d-%m-%Y", "%d/%m/%y %H:%M", "%d/%m/%y"):
        try:
            return datetime.strptime(s, f).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    return None


def leer_archivo(nombre, datos):
    """Devuelve (filas_normalizadas, columnas_detectadas)."""
    ext = nombre.lower().rsplit(".", 1)[-1]
    if ext == "xlsx":
        crudas = _celdas_xlsx(datos)
    elif ext in ("csv", "txt"):
        crudas = _celdas_csv(datos)
    else:
        raise ApiError(400, "Formato no soportado: subí un .xlsx o .csv")
    # el encabezado puede no estar en la primera fila (títulos arriba)
    for idx, fila_ in enumerate(crudas[:15]):
        mapa = detectar_columnas(fila_)
        if all(c in mapa for c, _, ob in COLUMNAS if ob):
            break
    else:
        faltan = [c for c, _, ob in COLUMNAS if ob]
        raise ApiError(400, "No encontré el encabezado. Tiene que tener columnas: Ticket, Producto, Cant. y Precio unitario final "
                            f"(busqué en las primeras 15 filas). Obligatorias: {', '.join(faltan)}")
    encabezado = [str(c).strip() for c in crudas[idx]]
    usadas = set(mapa.values())
    salida = []
    for n, fila_ in enumerate(crudas[idx + 1:], start=idx + 2):
        fila_ = list(fila_) + [""] * (len(encabezado) - len(fila_))
        g = lambda k: fila_[mapa[k]] if k in mapa else ""
        ticket = str(g("ticket")).strip()
        if isinstance(g("ticket"), float):
            ticket = str(int(g("ticket")))
        if not ticket and not str(g("producto")).strip():
            continue  # renglón vacío
        if not ticket or not re.search(r"\d", ticket):
            continue  # totales, subtítulos, etc.
        cantidad = numero(g("cantidad"), "Cant.", n)
        precio = numero(g("precio"), "Precio unitario final", n)
        if cantidad is None or precio is None:
            raise ApiError(400, f"Renglón {n}: falta la cantidad o el precio")
        prod = g("producto")
        prod = str(int(prod)) if isinstance(prod, float) and prod.is_integer() else str(prod).strip()
        extra = {encabezado[i]: (v.isoformat(sep=" ") if isinstance(v, datetime) else v)
                 for i, v in enumerate(fila_[:len(encabezado)]) if i not in usadas and encabezado[i] and v not in ("", None)}
        salida.append({
            "renglon": n, "ticket": ticket, "clave": clave_ticket(ticket),
            "emision": fecha_hora(g("emision")) if "emision" in mapa else None,
            "razon_social": str(g("razon_social")).strip()[:200],
            "producto": prod[:60], "cantidad": cantidad, "precio": precio,
            "cant_items": numero(g("cant_items"), "Cant. items", n) if "cant_items" in mapa else None,
            "bonif": numero(g("bonif"), "Bonif.", n) if "bonif" in mapa else None,
            "extra": json.dumps(extra, ensure_ascii=False, default=str),
        })
    if not salida:
        raise ApiError(400, "El archivo no tiene renglones de ventas")
    return salida, {k: encabezado[i] for k, i in mapa.items()}


# ------------------------------------------------------------------- cruce ---

def candidatos(clave):
    return [dict(r) for r in filas("""SELECT f.ticket, MIN(f.emision) AS emision, MAX(f.razon_social) AS razon_social,
                                             SUM(f.cantidad * f.precio) AS total, COUNT(*) AS renglones, f.archivo_id
                                      FROM filas_archivo f WHERE f.clave = ? AND f.ticket NOT IN (SELECT ticket_archivo FROM ventas)
                                      GROUP BY f.ticket ORDER BY emision""", (clave,))]


def confirmar(ticket_id, ticket_archivo, origen="archivo"):
    """Pasa las filas del archivo de ese ticket a ventas + venta_items."""
    rs = filas("SELECT * FROM filas_archivo WHERE ticket = ? ORDER BY archivo_id DESC, renglon", (ticket_archivo,))
    if not rs:
        raise ApiError(404, "Ese ticket ya no está en los archivos")
    archivo = rs[0]["archivo_id"]
    rs = [r for r in rs if r["archivo_id"] == archivo]  # si se subió dos veces, el más nuevo
    total = round(sum(r["cantidad"] * r["precio"] for r in rs), 2)
    emisiones = [r["emision"] for r in rs if r["emision"]]
    cant_items = next((r["cant_items"] for r in rs if r["cant_items"] is not None), None)
    vid = _con.execute(
        """INSERT INTO ventas (ticket_id, ticket_archivo, emision, razon_social, total, unidades, cant_items, renglones, origen, archivo_id, confirmado)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (ticket_id, ticket_archivo, min(emisiones) if emisiones else None, next((r["razon_social"] for r in rs if r["razon_social"]), ""),
         total, sum(r["cantidad"] for r in rs), cant_items, len(rs), origen, archivo, ahora())).lastrowid
    for i, r in enumerate(rs, 1):
        _con.execute("""INSERT INTO venta_items (venta_id, renglon, producto, cantidad, bonif, precio_unitario_final, subtotal, extra)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                     (vid, i, r["producto"], r["cantidad"], r["bonif"], r["precio"], round(r["cantidad"] * r["precio"], 2), r["extra"]))
        _con.execute("INSERT OR IGNORE INTO productos (codigo) VALUES (?)", (r["producto"],))
    _con.execute("UPDATE tickets SET estado = 'confirmado', detalle_estado = '' WHERE id = ?", (ticket_id,))
    return vid


def traer_ventas_apliton():
    """Lugar para la API de Apliton: cuando esté la definición, traer las ventas de los tickets pendientes
    y cargarlas en filas_archivo (o directo con confirmar(..., origen='api'))."""
    raise NotImplementedError("La integración con la API de Apliton todavía no está definida")


def procesar(motivo, usuario=""):
    """Cruza los tickets pendientes (y ambiguos) con las filas de los archivos subidos."""
    with _lock, _con:
        if CFG["apliton"].get("activo"):
            try:
                traer_ventas_apliton()
            except NotImplementedError as e:
                print(f"Apliton: {e}")
        confirmados = ambiguos = 0
        for t in _con.execute("SELECT id, clave, cargado FROM tickets WHERE estado IN ('pendiente', 'ambiguo')").fetchall():
            cands = candidatos(t["clave"])
            if not cands:
                continue
            if len(cands) > 1:
                # mismo número en dos puntos de venta: si uno solo es del día en que se cargó, es ese
                mismo_dia = [c for c in cands if (c["emision"] or "")[:10] == t["cargado"][:10]]
                if len(mismo_dia) == 1:
                    cands = mismo_dia
            if len(cands) == 1:
                confirmar(t["id"], cands[0]["ticket"])
                confirmados += 1
            else:
                _con.execute("UPDATE tickets SET estado = 'ambiguo', detalle_estado = ? WHERE id = ?",
                             (f"{len(cands)} tickets del archivo terminan en ese número: elegí cuál es", t["id"]))
                ambiguos += 1
        limite = (datetime.now() - timedelta(days=CFG["dias_retener_archivos"])).strftime("%Y-%m-%d %H:%M:%S")
        _con.execute("DELETE FROM filas_archivo WHERE archivo_id IN (SELECT id FROM archivos WHERE subido < ?)", (limite,))
        _con.execute("UPDATE archivos SET procesado = ?", (ahora(),))
        pendientes = _con.execute("SELECT COUNT(*) FROM tickets WHERE estado = 'pendiente'").fetchone()[0]
        _con.execute("INSERT INTO corridas (fecha, motivo, usuario, confirmados, ambiguos, pendientes) VALUES (?, ?, ?, ?, ?, ?)",
                     (ahora(), motivo, usuario, confirmados, ambiguos, pendientes))
    print(f"[{ahora()}] Cruce {motivo}: {confirmados} confirmados, {ambiguos} ambiguos, {pendientes} pendientes")
    return {"confirmados": confirmados, "ambiguos": ambiguos, "pendientes": pendientes}


def _tareas_periodicas():
    while True:
        try:
            ejecutar("DELETE FROM sesiones WHERE vence < ?", (ahora(),))
            hoy = date.today().isoformat()
            if datetime.now().strftime("%H:%M") >= CFG["hora_corte"] and ajuste("ultimo_cruce_auto") != hoy:
                ajuste("ultimo_cruce_auto", hoy)
                procesar("automático")
        except Exception:
            traceback.print_exc()
        time.sleep(20)


def _respaldo_diario():
    while True:
        time.sleep(24 * 3600)
        try:
            respaldar()
        except Exception:
            traceback.print_exc()


# --------------------------------------------------------------- campañas ---

def campana_dict(r):
    return {k: r[k] for k in r.keys()} | {"activa": bool(r["activa"])}


def datos_campana(cuerpo):
    d = {"nombre": texto(cuerpo, "nombre", True, 100, "el nombre"), "notas": texto(cuerpo, "notas", False, 2000)}
    d["desde"] = fecha_iso(cuerpo.get("desde"), "Desde").isoformat()
    d["hasta"] = fecha_iso(cuerpo.get("hasta"), "Hasta").isoformat()
    if d["hasta"] < d["desde"]:
        raise ApiError(400, "La fecha «hasta» es anterior a «desde»")
    d["modo"] = cuerpo.get("modo")
    if d["modo"] not in ("por_ticket", "por_monto"):
        raise ApiError(400, "modo tiene que ser por_ticket o por_monto")
    try:
        d["monto"] = float(cuerpo.get("monto"))
    except (TypeError, ValueError):
        raise ApiError(400, "Monto inválido")
    if d["monto"] <= 0:
        raise ApiError(400, "El monto tiene que ser mayor a 0")
    d["max_chances_ticket"] = entero(cuerpo.get("max_chances_ticket"), "Tope de chances por ticket")
    d["activa"] = 1 if cuerpo.get("activa", True) else 0
    return d


def chances_ticket(c, total):
    if c["modo"] == "por_ticket":
        n = 1 if total >= c["monto"] else 0
    else:
        n = int(math.floor(total / c["monto"] + 1e-9))
    if c["max_chances_ticket"]:
        n = min(n, c["max_chances_ticket"])
    return max(0, n)


def padron(c):
    """[{cliente_id, dni, nombre, telefono, email, tickets, monto, chances}] de los clientes con chances en la campaña."""
    rs = filas("""SELECT c.id AS cliente_id, c.dni, c.nombre || ' ' || c.apellido AS nombre, c.telefono, c.email, v.total
                  FROM ventas v JOIN tickets t ON t.id = v.ticket_id JOIN clientes c ON c.id = t.cliente_id
                  WHERE date(COALESCE(v.emision, t.cargado)) BETWEEN ? AND ?""", (c["desde"], c["hasta"]))
    por = {}
    for r in rs:
        p = por.setdefault(r["cliente_id"], {"cliente_id": r["cliente_id"], "dni": r["dni"], "nombre": r["nombre"], "telefono": r["telefono"],
                                             "email": r["email"], "tickets": 0, "monto": 0.0, "chances": 0})
        p["tickets"] += 1
        p["monto"] = round(p["monto"] + r["total"], 2)
        p["chances"] += chances_ticket(c, r["total"])
    return sorted((p for p in por.values() if p["chances"] > 0), key=lambda p: (-p["chances"], p["nombre"]))


# ------------------------------------------------------------------ export ---

def csv_respuesta(nombre, encabezado, filas_):
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(encabezado)
    for f in filas_:
        w.writerow(["" if v is None else (str(v).replace(".", ",") if isinstance(v, float) else v) for v in f])
    return Respuesta(("﻿" + buf.getvalue()).encode("utf-8"), "text/csv; charset=utf-8",
                     headers={"Content-Disposition": f'attachment; filename="{nombre}_{date.today()}.csv"'})


DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]


def _dia_hora(emision):
    if not emision:
        return "", ""
    d = datetime.strptime(emision, "%Y-%m-%d %H:%M:%S")
    return DIAS[d.weekday()], d.hour


# ---------------------------------------------------------------- rutas ---

RUTAS = []


def ruta(metodo, patron, nivel, descripcion):
    """nivel: 'publico' | 'usuario' (cajero o admin) | 'admin'."""
    def deco(f):
        RUTAS.append((metodo, re.compile(patron), f, nivel, patron, descripcion))
        return f
    return deco


@ruta("GET", r"/api", "publico", "Esta lista de endpoints")
def api_indice(req, yo, cuerpo):
    return {"endpoints": [{"metodo": m, "ruta": p.replace(r"(\d+)", "{id}").replace(r"(\w+)", "{nombre}"), "acceso": n, "descripcion": d}
                          for m, _, _, n, p, d in RUTAS]}


@ruta("GET", r"/api/estado", "publico", "{sistema, modo, registros_url, registros_ok}")
def api_estado(req, yo, cuerpo):
    try:
        ok = registros("GET", "/api/estado", timeout=2)[0] == 200
    except ApiError:
        ok = False
    local = usuario_local()
    return {"sistema": "sorteo", "modo": CFG["modo"], "registros_url": CFG["registros_url"], "registros_ok": ok,
            "usuario_local_activo": bool(local and local["activo"])}


@ruta("POST", r"/api/login", "publico", "{usuario, pin}: valida contra el Sistema de Registros → {usuario}")
def api_login(req, yo, cuerpo):
    usuario = str(cuerpo.get("usuario") or "").strip().lower()
    pin = str(cuerpo.get("pin") or "")
    if not usuario or not pin:
        raise ApiError(400, "Poné tu usuario y tu PIN")
    local = usuario_local()
    if local and usuario == local["usuario"]:
        return login_local(local, pin)
    status, r = registros("POST", "/api/login", {"usuario": usuario, "pin": pin})
    if status != 200:
        msg = r.get("error") or "No se pudo iniciar sesión"
        if "creá uno" in msg:
            msg = "Todavía no tenés PIN: crealo primero en el Sistema de Registros"
        raise ApiError(status if status in (400, 401, 403, 429) else 502, msg)
    u = r.get("usuario") or {}
    try:
        registros("POST", "/api/logout", {}, token=r.get("token"), timeout=3)  # solo leemos; no dejamos la sesión abierta allá
    except ApiError:
        pass
    if "roles_nombres" not in u:
        raise ApiError(502, "El Sistema de Registros está desactualizado (no informa los roles). Avisale al administrador.")
    if not puede_entrar(u):
        raise ApiError(403, "Tu usuario no está habilitado para el sorteo (hace falta el rol cajero)")
    return respuesta_sesion(u)


@ruta("POST", r"/api/logout", "usuario", "Cierra la sesión")
def api_logout(req, yo, cuerpo):
    ejecutar("DELETE FROM sesiones WHERE token_hash = ?", (_hash_token(req.token),))
    return Respuesta({"ok": True}, headers={"Set-Cookie": f"{COOKIE}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"})


@ruta("GET", r"/api/yo", "usuario", "Quién está logueado (nombre, sector, es_admin, sesion_segundos)")
def api_yo(req, yo, cuerpo):
    return {**yo, "modo": CFG["modo"]}


# --- carga (cajero) ---

@ruta("GET", r"/api/clientes/dni/(\d+)", "usuario", "Busca un cliente por DNI → cliente o 404")
def api_cliente_dni(req, yo, cuerpo, dni):
    r = fila("SELECT * FROM clientes WHERE dni = ?", (normalizar_dni(dni),))
    if not r:
        raise ApiError(404, "No existe")
    return cliente_dict(r, yo["es_admin"])


@ruta("POST", r"/api/clientes", "usuario",
      "Alta de cliente {dni, nombre, apellido, nacimiento (AAAA-MM-DD), telefono, email?, ticket?}. Con ticket lo carga en el mismo paso")
def api_cliente_crear(req, yo, cuerpo):
    dni = normalizar_dni(cuerpo.get("dni"))
    d = datos_cliente(cuerpo)
    ticket = cuerpo.get("ticket")
    with _lock, _con:
        if _con.execute("SELECT 1 FROM clientes WHERE dni = ?", (dni,)).fetchone():
            raise ApiError(409, "Ya existe un cliente con ese DNI", campo="dni")
        if ticket not in (None, ""):
            numero_, clave = validar_ticket(ticket)
            chequear_no_es_dni(numero_, dni)
            chequear_ticket_libre(clave)
        cid = _con.execute("""INSERT INTO clientes (dni, nombre, apellido, nacimiento, telefono, email, creado, creado_por_id, creado_por)
                              VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                           (dni, d["nombre"], d["apellido"], d["nacimiento"], d["telefono"], d["email"], ahora(), yo["id"], yo["nombre"])).lastrowid
        tid = insertar_ticket(cid, numero_, clave, yo) if ticket not in (None, "") else None
    return {"cliente": cliente_dict(fila("SELECT * FROM clientes WHERE id = ?", (cid,)), yo["es_admin"]), "ticket_id": tid}


@ruta("PUT", r"/api/clientes/(\d+)", "usuario", "Modifica un cliente (todo menos el DNI). Manda solo lo que cambia")
def api_cliente_editar(req, yo, cuerpo, cid):
    with _lock, _con:
        r = _con.execute("SELECT * FROM clientes WHERE id = ?", (int(cid),)).fetchone()
        if not r:
            raise ApiError(404, "No existe el cliente")
        if "dni" in cuerpo and normalizar_dni(cuerpo["dni"]) != r["dni"]:
            raise ApiError(400, "El DNI no se puede cambiar", campo="dni")
        d = datos_cliente({**dict(r), **{k: v for k, v in cuerpo.items() if k != "dni"}})
        antes = {k: r[k] for k in d if r[k] != d[k]}
        if antes:
            _con.execute("UPDATE clientes SET nombre = ?, apellido = ?, nacimiento = ?, telefono = ?, email = ?, modificado = ?, modificado_por = ? WHERE id = ?",
                         (d["nombre"], d["apellido"], d["nacimiento"], d["telefono"], d["email"], ahora(), yo["nombre"], r["id"]))
            _con.execute("INSERT INTO clientes_cambios (cliente_id, fecha, usuario_id, usuario, antes, despues) VALUES (?, ?, ?, ?, ?, ?)",
                         (r["id"], ahora(), yo["id"], yo["nombre"], json.dumps(antes, ensure_ascii=False),
                          json.dumps({k: d[k] for k in antes}, ensure_ascii=False)))
    return cliente_dict(fila("SELECT * FROM clientes WHERE id = ?", (int(cid),)), yo["es_admin"])


@ruta("POST", r"/api/tickets", "usuario", "Carga un ticket {cliente_id, numero}. Si ya está cargado: 409")
def api_ticket_crear(req, yo, cuerpo):
    numero_, clave = validar_ticket(cuerpo.get("numero"))
    cid = entero(cuerpo.get("cliente_id"), "cliente_id")
    with _lock, _con:
        cl = _con.execute("SELECT dni FROM clientes WHERE id = ?", (cid,)).fetchone()
        if not cl:
            raise ApiError(404, "No existe el cliente")
        chequear_no_es_dni(numero_, cl["dni"])
        chequear_ticket_libre(clave)
        tid = insertar_ticket(cid, numero_, clave, yo)
    return {"id": tid, "numero": numero_, "estado": "pendiente"}


# --- admin: usuario temporal ---

@ruta("GET", r"/api/usuario-local", "admin", "Estado del usuario temporal (el que entra sin el Sistema de Registros)")
def api_usuario_local(req, yo, cuerpo):
    local = usuario_local()
    if not local:
        return {"configurado": False}
    return {"configurado": True, "usuario": local["usuario"], "nombre": local["nombre"], "activo": local["activo"],
            "en_archivo": local["en_archivo"], "ultimo_cambio": ajuste("usuario_local_cambio")}


@ruta("PUT", r"/api/usuario-local", "admin", "{activo: true|false}. Al desactivarlo se cierran sus sesiones")
def api_usuario_local_cambiar(req, yo, cuerpo):
    if not isinstance(cuerpo.get("activo"), bool):
        raise ApiError(400, "activo tiene que ser true o false")
    ajuste("usuario_local_desactivado", "0" if cuerpo["activo"] else "1")
    ajuste("usuario_local_cambio", f"{'Reactivado' if cuerpo['activo'] else 'Desactivado'} por {yo['nombre']} el {ahora()}")
    if not cuerpo["activo"]:
        ejecutar("DELETE FROM sesiones WHERE usuario_id = ?", (ID_USUARIO_LOCAL,))
    return api_usuario_local(req, yo, {})


# --- admin: clientes y tickets ---

@ruta("GET", r"/api/clientes", "admin", "Lista de clientes. ?q= (nombre, apellido, DNI, teléfono) &limite &offset")
def api_clientes(req, yo, cuerpo):
    q = f"%{req.consulta.get('q', '').strip()}%"
    limite = min(entero(req.consulta.get("limite"), "limite", 100), 1000)
    offset = entero(req.consulta.get("offset"), "offset", 0)
    cond = "WHERE nombre || ' ' || apellido LIKE ? OR dni LIKE ? OR telefono LIKE ? OR email LIKE ?"
    total = fila(f"SELECT COUNT(*) FROM clientes {cond}", (q, q, q, q))[0]
    rs = filas(f"""SELECT c.*, (SELECT COUNT(*) FROM tickets WHERE cliente_id = c.id) AS n_tickets,
                          (SELECT COALESCE(SUM(v.total), 0) FROM ventas v JOIN tickets t ON t.id = v.ticket_id WHERE t.cliente_id = c.id) AS monto
                   FROM clientes c {cond} ORDER BY c.id DESC LIMIT ? OFFSET ?""", (q, q, q, q, limite, offset))
    return {"total": total, "items": [cliente_dict(r) | {"tickets": r["n_tickets"], "monto": r["monto"]} for r in rs]}


@ruta("GET", r"/api/clientes/(\d+)", "admin", "Un cliente con sus tickets y el historial de cambios")
def api_cliente(req, yo, cuerpo, cid):
    r = fila("SELECT * FROM clientes WHERE id = ?", (int(cid),))
    if not r:
        raise ApiError(404, "No existe el cliente")
    d = cliente_dict(r, True)
    d["lista_tickets"] = [ticket_dict(t) for t in filas(SQL_TICKETS + " WHERE t.cliente_id = ? ORDER BY t.id DESC", (r["id"],))]
    d["cambios"] = [dict(x) for x in filas("SELECT fecha, usuario, antes, despues FROM clientes_cambios WHERE cliente_id = ? ORDER BY id DESC", (r["id"],))]
    return d


@ruta("GET", r"/api/tickets", "admin", "Tickets. ?estado=pendiente|confirmado|ambiguo &q= &desde &hasta (fecha de carga) &limite &offset")
def api_tickets(req, yo, cuerpo):
    cond, args = [], []
    if req.consulta.get("estado"):
        cond.append("t.estado = ?"); args.append(req.consulta["estado"])
    if req.consulta.get("q"):
        q = f"%{req.consulta['q'].strip()}%"
        cond.append("(t.numero LIKE ? OR c.dni LIKE ? OR c.nombre || ' ' || c.apellido LIKE ? OR t.cajero_nombre LIKE ?)"); args += [q] * 4
    for k, op in (("desde", ">="), ("hasta", "<=")):
        if req.consulta.get(k):
            cond.append(f"date(t.cargado) {op} ?"); args.append(fecha_iso(req.consulta[k], k).isoformat())
    where = (" WHERE " + " AND ".join(cond)) if cond else ""
    limite = min(entero(req.consulta.get("limite"), "limite", 100), 1000)
    offset = entero(req.consulta.get("offset"), "offset", 0)
    total = fila(f"SELECT COUNT(*) FROM tickets t JOIN clientes c ON c.id = t.cliente_id{where}", args)[0]
    rs = filas(SQL_TICKETS + where + " ORDER BY t.id DESC LIMIT ? OFFSET ?", args + [limite, offset])
    resumen = {r["estado"]: r["n"] for r in filas("SELECT estado, COUNT(*) AS n FROM tickets GROUP BY estado")}
    return {"total": total, "resumen": resumen, "items": [ticket_dict(r) for r in rs]}


@ruta("GET", r"/api/tickets/(\d+)", "admin", "Un ticket con los artículos (y los candidatos si es ambiguo)")
def api_ticket(req, yo, cuerpo, tid):
    r = fila(SQL_TICKETS + " WHERE t.id = ?", (int(tid),))
    if not r:
        raise ApiError(404, "No existe el ticket")
    return ticket_dict(r, True)


@ruta("POST", r"/api/tickets/(\d+)/resolver", "admin", "Ticket ambiguo: {ticket_archivo} elige cuál de los del archivo es")
def api_ticket_resolver(req, yo, cuerpo, tid):
    with _lock, _con:
        t = _con.execute("SELECT * FROM tickets WHERE id = ?", (int(tid),)).fetchone()
        if not t or t["estado"] == "confirmado":
            raise ApiError(400, "El ticket no existe o ya está confirmado")
        elegido = str(cuerpo.get("ticket_archivo") or "")
        if elegido not in [c["ticket"] for c in candidatos(t["clave"])]:
            raise ApiError(400, "Ese no es uno de los candidatos")
        confirmar(t["id"], elegido)
    return api_ticket(req, yo, {}, tid)


@ruta("DELETE", r"/api/tickets/(\d+)", "admin", "Borra un ticket cargado por error (y su compra, si ya se cruzó)")
def api_ticket_borrar(req, yo, cuerpo, tid):
    ejecutar("DELETE FROM tickets WHERE id = ?", (int(tid),))
    return {"ok": True}


# --- admin: archivos y cruce ---

@ruta("POST", r"/api/archivos", "admin", "Sube el archivo de ventas {nombre, datos: base64 o data:...;base64,...} (.xlsx o .csv)")
def api_archivo_subir(req, yo, cuerpo):
    nombre = texto(cuerpo, "nombre", True, 200, "el nombre del archivo")
    b64 = str(cuerpo.get("datos") or "")
    b64 = b64.split(",", 1)[1] if b64.startswith("data:") else b64
    try:
        datos = base64.b64decode(b64, validate=True)
    except ValueError:
        raise ApiError(400, "El archivo no llegó bien")
    renglones, columnas = leer_archivo(nombre, datos)
    emisiones = sorted(r["emision"] for r in renglones if r["emision"])
    with _lock, _con:
        aid = _con.execute("INSERT INTO archivos (nombre, subido, subido_por, filas, tickets, desde, hasta, columnas) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                           (nombre, ahora(), yo["nombre"], len(renglones), len({r["ticket"] for r in renglones}),
                            emisiones[0] if emisiones else None, emisiones[-1] if emisiones else None,
                            json.dumps(columnas, ensure_ascii=False))).lastrowid
        _con.executemany("""INSERT INTO filas_archivo (archivo_id, renglon, ticket, clave, emision, razon_social, producto, cantidad, cant_items, bonif, precio, extra)
                            VALUES (:a, :renglon, :ticket, :clave, :emision, :razon_social, :producto, :cantidad, :cant_items, :bonif, :precio, :extra)""",
                         [{**r, "a": aid} for r in renglones])
    claves = {r["clave"] for r in renglones}
    registrados = sum(1 for t in filas("SELECT clave FROM tickets WHERE estado != 'confirmado'") if t["clave"] in claves)
    return {"id": aid, "filas": len(renglones), "tickets": len({r["ticket"] for r in renglones}), "columnas": columnas,
            "coinciden": registrados, "desde": emisiones[0] if emisiones else None, "hasta": emisiones[-1] if emisiones else None}


@ruta("GET", r"/api/archivos", "admin", "Archivos subidos y últimos cruces")
def api_archivos(req, yo, cuerpo):
    arch = [dict(r) | {"columnas": json.loads(r["columnas"]), "en_espera": r["en_espera"]} for r in filas(
        "SELECT a.*, (SELECT COUNT(*) FROM filas_archivo WHERE archivo_id = a.id) AS en_espera FROM archivos a ORDER BY id DESC LIMIT 60")]
    corr = [dict(r) for r in filas("SELECT * FROM corridas ORDER BY id DESC LIMIT 30")]
    return {"archivos": arch, "corridas": corr, "hora_corte": CFG["hora_corte"], "dias_retener": CFG["dias_retener_archivos"]}


@ruta("DELETE", r"/api/archivos/(\d+)", "admin", "Borra un archivo subido (las compras ya cruzadas quedan)")
def api_archivo_borrar(req, yo, cuerpo, aid):
    ejecutar("DELETE FROM archivos WHERE id = ?", (int(aid),))
    return {"ok": True}


@ruta("POST", r"/api/procesar", "admin", "Corre el cruce ahora (además del automático de las 23:59)")
def api_procesar(req, yo, cuerpo):
    return procesar("manual", yo["nombre"])


# --- admin: campañas y sorteos ---

@ruta("GET", r"/api/campanas", "admin", "Campañas con participantes y chances")
def api_campanas(req, yo, cuerpo):
    salida = []
    for r in filas("SELECT * FROM campanas ORDER BY id DESC"):
        p = padron(r)
        salida.append(campana_dict(r) | {"participantes": len(p), "chances": sum(x["chances"] for x in p),
                                         "sorteos": fila("SELECT COUNT(*) FROM sorteos WHERE campana_id = ?", (r["id"],))[0]})
    return salida


@ruta("POST", r"/api/campanas", "admin", "Crea una campaña {nombre, desde, hasta, modo: por_ticket|por_monto, monto, max_chances_ticket?, activa?, notas?}")
def api_campana_crear(req, yo, cuerpo):
    d = datos_campana(cuerpo)
    cid = ejecutar("INSERT INTO campanas (nombre, desde, hasta, modo, monto, max_chances_ticket, activa, notas, creada) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   (d["nombre"], d["desde"], d["hasta"], d["modo"], d["monto"], d["max_chances_ticket"], d["activa"], d["notas"], ahora()))
    return campana_dict(fila("SELECT * FROM campanas WHERE id = ?", (cid,)))


@ruta("PUT", r"/api/campanas/(\d+)", "admin", "Modifica una campaña (mismos campos)")
def api_campana_editar(req, yo, cuerpo, cid):
    r = fila("SELECT * FROM campanas WHERE id = ?", (int(cid),))
    if not r:
        raise ApiError(404, "No existe la campaña")
    d = datos_campana({**dict(r), **cuerpo})
    ejecutar("UPDATE campanas SET nombre = ?, desde = ?, hasta = ?, modo = ?, monto = ?, max_chances_ticket = ?, activa = ?, notas = ? WHERE id = ?",
             (d["nombre"], d["desde"], d["hasta"], d["modo"], d["monto"], d["max_chances_ticket"], d["activa"], d["notas"], r["id"]))
    return campana_dict(fila("SELECT * FROM campanas WHERE id = ?", (r["id"],)))


@ruta("DELETE", r"/api/campanas/(\d+)", "admin", "Borra una campaña (si ya tiene sorteos, no)")
def api_campana_borrar(req, yo, cuerpo, cid):
    if fila("SELECT 1 FROM sorteos WHERE campana_id = ?", (int(cid),)):
        raise ApiError(409, "La campaña ya tiene sorteos hechos: desactivala en vez de borrarla")
    ejecutar("DELETE FROM campanas WHERE id = ?", (int(cid),))
    return {"ok": True}


@ruta("GET", r"/api/campanas/(\d+)/chances", "admin", "Padrón de la campaña: clientes con sus tickets, monto y chances")
def api_campana_chances(req, yo, cuerpo, cid):
    r = fila("SELECT * FROM campanas WHERE id = ?", (int(cid),))
    if not r:
        raise ApiError(404, "No existe la campaña")
    return {"campana": campana_dict(r), "padron": padron(r)}


@ruta("POST", r"/api/campanas/(\d+)/sortear", "admin",
      "Sortea {ganadores: n, descripcion?, excluir_ganadores_previos?: true}. Cada chance es un número; un cliente gana una sola vez por sorteo")
def api_sortear(req, yo, cuerpo, cid):
    with _lock:
        c = fila("SELECT * FROM campanas WHERE id = ?", (int(cid),))
        if not c:
            raise ApiError(404, "No existe la campaña")
        n = entero(cuerpo.get("ganadores"), "Cantidad de ganadores", 1)
        if not 1 <= n <= 100:
            raise ApiError(400, "La cantidad de ganadores tiene que ser entre 1 y 100")
        p = padron(c)
        if cuerpo.get("excluir_ganadores_previos", True):
            previos = {g["cliente_id"] for s in filas("SELECT ganadores FROM sorteos WHERE campana_id = ?", (c["id"],)) for g in json.loads(s["ganadores"])}
            p = [x for x in p if x["cliente_id"] not in previos]
        if len(p) < n:
            raise ApiError(400, f"Hay {len(p)} participantes con chances; no alcanza para {n} ganador(es)")
        # Cada chance es un número del 1 al total (en el orden del padrón); se sortea un número y gana su dueño.
        rng = secrets.SystemRandom()
        restantes, ganadores = list(p), []
        for puesto in range(1, n + 1):
            total = sum(x["chances"] for x in restantes)
            sorteado = rng.randint(1, total)
            acum = 0
            for i, x in enumerate(restantes):
                acum += x["chances"]
                if sorteado <= acum:
                    ganadores.append({**x, "puesto": puesto, "numero_sorteado": sorteado, "chances_en_juego": total})
                    restantes.pop(i)
                    break
        padron_json = json.dumps([{k: x[k] for k in ("cliente_id", "dni", "nombre", "tickets", "monto", "chances")} for x in p], ensure_ascii=False)
        sid = ejecutar("""INSERT INTO sorteos (campana_id, fecha, realizado_por, descripcion, reglas, participantes, chances_total, padron, padron_hash, ganadores)
                          VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                       (c["id"], ahora(), yo["nombre"], texto(cuerpo, "descripcion", False, 500),
                        json.dumps(campana_dict(c), ensure_ascii=False), len(p), sum(x["chances"] for x in p),
                        padron_json, hashlib.sha256(padron_json.encode()).hexdigest(), json.dumps(ganadores, ensure_ascii=False)))
    return api_sorteo(req, yo, {}, sid)


@ruta("GET", r"/api/sorteos", "admin", "Actas de los sorteos hechos")
def api_sorteos(req, yo, cuerpo):
    return [dict(r) | {"ganadores": json.loads(r["ganadores"])} for r in filas(
        """SELECT s.id, s.campana_id, c.nombre AS campana, s.fecha, s.realizado_por, s.descripcion, s.participantes, s.chances_total, s.padron_hash, s.ganadores
           FROM sorteos s JOIN campanas c ON c.id = s.campana_id ORDER BY s.id DESC""")]


@ruta("GET", r"/api/sorteos/(\d+)", "admin", "Acta de un sorteo (reglas, padrón y ganadores)")
def api_sorteo(req, yo, cuerpo, sid):
    r = fila("SELECT s.*, c.nombre AS campana FROM sorteos s JOIN campanas c ON c.id = s.campana_id WHERE s.id = ?", (int(sid),))
    if not r:
        raise ApiError(404, "No existe el sorteo")
    return dict(r) | {k: json.loads(r[k]) for k in ("reglas", "padron", "ganadores")}


# --- admin: exportar para análisis ---

@ruta("GET", r"/api/exportar/(\w+)", "admin", "CSV (separado con ;) para análisis: clientes | tickets | articulos")
def api_exportar(req, yo, cuerpo, que):
    if que == "clientes":
        rs = filas("""SELECT c.*, (SELECT COUNT(*) FROM tickets WHERE cliente_id = c.id) AS n_tickets,
                             (SELECT COALESCE(SUM(v.total), 0) FROM ventas v JOIN tickets t ON t.id = v.ticket_id WHERE t.cliente_id = c.id) AS monto
                      FROM clientes c ORDER BY c.id""")
        return csv_respuesta("clientes", ["id", "dni", "nombre", "apellido", "nacimiento", "edad", "telefono", "email", "alta", "alta_por",
                                          "tickets", "monto_total"],
                             [[r["id"], r["dni"], r["nombre"], r["apellido"], r["nacimiento"], edad(date.fromisoformat(r["nacimiento"])), r["telefono"],
                               r["email"], r["creado"], r["creado_por"], r["n_tickets"], float(r["monto"])] for r in rs])
    if que == "tickets":
        rs = filas(SQL_TICKETS + " ORDER BY t.id")
        return csv_respuesta("tickets", ["ticket_id", "numero", "ticket_archivo", "estado", "cargado", "cajero", "sector_cajero", "punto_venta",
                                         "cliente_id", "dni", "emision", "dia_semana", "hora", "total", "unidades", "renglones", "razon_social"],
                             [[r["id"], r["numero"], r["ticket_archivo"], r["estado"], r["cargado"], r["cajero_nombre"], r["cajero_sector"], r["punto_venta"],
                               r["cliente_id"], r["dni"], r["emision"], *_dia_hora(r["emision"]), r["total"], r["unidades"], r["renglones"], r["razon_social"]]
                              for r in rs])
    if que == "articulos":
        rs = filas("""SELECT i.*, v.ticket_archivo, v.emision, v.total, t.id AS ticket_id, t.cajero_nombre, t.cliente_id, c.nacimiento,
                             p.descripcion, p.rubro, p.marca
                      FROM venta_items i JOIN ventas v ON v.id = i.venta_id JOIN tickets t ON t.id = v.ticket_id
                      JOIN clientes c ON c.id = t.cliente_id LEFT JOIN productos p ON p.codigo = i.producto ORDER BY v.emision, i.venta_id, i.renglon""")
        return csv_respuesta("articulos", ["ticket_id", "ticket_archivo", "emision", "dia_semana", "hora", "cliente_id", "edad_cliente", "cajero",
                                           "renglon", "producto", "descripcion", "rubro", "marca", "cantidad", "bonif", "precio_unitario_final",
                                           "subtotal", "total_ticket"],
                             [[r["ticket_id"], r["ticket_archivo"], r["emision"], *_dia_hora(r["emision"]), r["cliente_id"],
                               edad(date.fromisoformat(r["nacimiento"]), date.fromisoformat(r["emision"][:10]) if r["emision"] else None),
                               r["cajero_nombre"], r["renglon"], r["producto"], r["descripcion"], r["rubro"], r["marca"], r["cantidad"], r["bonif"],
                               r["precio_unitario_final"], r["subtotal"], r["total"]] for r in rs])
    raise ApiError(404, "Exportaciones: clientes, tickets, articulos")


@ruta("GET", r"/api/resumen", "admin", "Números generales para el panel")
def api_resumen(req, yo, cuerpo):
    hoy = date.today().isoformat()
    g = lambda sql, a=(): fila(sql, a)[0]
    return {
        "clientes": g("SELECT COUNT(*) FROM clientes"),
        "clientes_hoy": g("SELECT COUNT(*) FROM clientes WHERE date(creado) = ?", (hoy,)),
        "tickets": g("SELECT COUNT(*) FROM tickets"),
        "tickets_hoy": g("SELECT COUNT(*) FROM tickets WHERE date(cargado) = ?", (hoy,)),
        "pendientes": g("SELECT COUNT(*) FROM tickets WHERE estado = 'pendiente'"),
        "ambiguos": g("SELECT COUNT(*) FROM tickets WHERE estado = 'ambiguo'"),
        "confirmados": g("SELECT COUNT(*) FROM tickets WHERE estado = 'confirmado'"),
        "monto": g("SELECT COALESCE(SUM(total), 0) FROM ventas"),
        "por_cajero_hoy": [dict(r) for r in filas("SELECT cajero_nombre AS cajero, COUNT(*) AS tickets FROM tickets WHERE date(cargado) = ? GROUP BY cajero_nombre ORDER BY 2 DESC", (hoy,))],
        "ultimo_cruce": dict(fila("SELECT * FROM corridas ORDER BY id DESC LIMIT 1") or {}) or None,
    }


# ------------------------------------------------------------- servidor ---

class Manejador(BaseHTTPRequestHandler):
    server_version = "SorteoLaBestia/1.0"

    def do_GET(self):
        self._despachar("GET")

    def do_POST(self):
        self._despachar("POST")

    def do_PUT(self):
        self._despachar("PUT")

    def do_DELETE(self):
        self._despachar("DELETE")

    def _despachar(self, metodo):
        url = urlparse(self.path)
        camino = url.path.rstrip("/") or "/"
        self.consulta = {k: v[-1] for k, v in parse_qs(url.query).items()}
        self.token = None
        try:
            if metodo == "GET" and camino in ("/", "/index.html"):
                with open(os.path.join(CARPETA, "index.html"), "rb") as f:
                    return self._enviar(Respuesta(f.read(), "text/html; charset=utf-8", headers={"Cache-Control": "no-cache"}))
            metodo_ok = False
            for m, patron, funcion, nivel, _, _ in RUTAS:
                coincide = patron.fullmatch(camino)
                if not coincide:
                    continue
                if m != metodo:
                    metodo_ok = True
                    continue
                yo = self._autenticar(nivel)
                cuerpo = self._leer_json() if metodo in ("POST", "PUT") else {}
                resultado = funcion(self, yo, cuerpo, *coincide.groups())
                return self._enviar(resultado if isinstance(resultado, Respuesta) else Respuesta(resultado))
            raise ApiError(405 if metodo_ok else 404, "Método no permitido" if metodo_ok else "No encontrado")
        except ApiError as e:
            self._enviar(Respuesta({"error": e.mensaje, **e.extra}, status=e.status))
        except Exception:
            traceback.print_exc()
            self._enviar(Respuesta({"error": "Error interno del servidor"}, status=500))

    def _autenticar(self, nivel):
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            self.token = auth[7:].strip()
        else:
            galleta = SimpleCookie(self.headers.get("Cookie", ""))
            if COOKIE in galleta:
                self.token = galleta[COOKIE].value
        if nivel == "publico":
            return None
        if not self.token:
            raise ApiError(401, "Tenés que iniciar sesión")
        s = fila("SELECT * FROM sesiones WHERE token_hash = ? AND vence > ?", (_hash_token(self.token), ahora()))
        if not s:
            raise ApiError(401, "La sesión venció, volvé a entrar")
        if s["usuario_id"] == ID_USUARIO_LOCAL and not (usuario_local() or {}).get("activo"):
            ejecutar("DELETE FROM sesiones WHERE token_hash = ?", (s["token_hash"],))
            raise ApiError(401, "El usuario temporal fue desactivado")
        yo = sesion_dict(s)
        if nivel == "admin" and not yo["es_admin"]:
            raise ApiError(403, "Solo para administradores")
        return yo

    def _leer_json(self):
        largo = int(self.headers.get("Content-Length") or 0)
        if largo > MAX_CUERPO:
            raise ApiError(413, "El pedido es demasiado grande")
        if not largo:
            return {}
        if "application/json" not in self.headers.get("Content-Type", ""):
            raise ApiError(415, "Se espera Content-Type: application/json")
        try:
            datos = json.loads(self.rfile.read(largo).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ApiError(400, "JSON inválido")
        if not isinstance(datos, dict):
            raise ApiError(400, "Se espera un objeto JSON")
        return datos

    def _enviar(self, r):
        self.send_response(r.status)
        self.send_header("Content-Type", r.tipo)
        self.send_header("Content-Length", str(len(r.cuerpo)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        if "Cache-Control" not in r.headers:
            self.send_header("Cache-Control", "no-store")
        for k, v in r.headers.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(r.cuerpo)

    def log_message(self, fmt, *args):
        if len(args) > 1 and str(args[1]).startswith(("4", "5")) and str(args[1]) not in ("401", "404", "409"):
            sys.stderr.write(f"[{self.log_date_time_string()}] {fmt % args}\n")


def ip_local():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except OSError:
        return None


def linea_de_comandos():
    """--exportar [archivo] · --importar archivo [--confirmar]. Devuelve el código de salida, o None si hay que levantar el servidor."""
    args = sys.argv[1:]
    try:
        if "--exportar" in args:
            siguiente = args[args.index("--exportar") + 1:]
            exportar(siguiente[0] if siguiente and not siguiente[0].startswith("--") else None)
            return 0
        if "--importar" in args:
            siguiente = args[args.index("--importar") + 1:]
            if not siguiente or siguiente[0].startswith("--"):
                raise ValueError("falta el archivo a importar")
            return importar(siguiente[0], "--confirmar" in args)
    except (OSError, sqlite3.Error, ValueError) as e:
        print(f"No se hizo nada: {e}", file=sys.stderr)
        return 1
    return None


def main():
    iniciar_db()
    codigo = linea_de_comandos()
    if codigo is not None:
        _con.close()
        sys.exit(codigo)
    if sys.stdout is None or sys.stderr is None:  # pythonw (Windows, arranque oculto) no tiene consola
        sys.stdout = sys.stderr = open(os.path.join(DATOS, "servidor.log"), "a", encoding="utf-8", buffering=1)
    respaldar()
    threading.Thread(target=_tareas_periodicas, daemon=True).start()
    threading.Thread(target=_respaldo_diario, daemon=True).start()
    puerto = CFG["puerto"]
    # En Windows esta opción deja a dos programas escuchar el mismo puerto sin avisar.
    ThreadingHTTPServer.allow_reuse_address = os.name != "nt"
    srv = ThreadingHTTPServer(("0.0.0.0", puerto), Manejador)
    with open(os.path.join(DATOS, "servidor.pid"), "w") as f:
        f.write(str(os.getpid()))
    ip = ip_local()
    print("Sorteo - La Bestia")
    print(f"  Modo:              {CFG['modo'].upper()}  (Sistema de Registros: {CFG['registros_url']})")
    print(f"  En esta compu:     http://localhost:{puerto}")
    if ip:
        print(f"  Desde la red:      http://{ip}:{puerto}")
    print(f"  Cruce automático:  todos los días a las {CFG['hora_corte']} (también se desloguea a todos)")
    print("Cerrar esta ventana (o Ctrl+C) apaga el servidor.")
    if "--no-browser" not in sys.argv:
        threading.Timer(0.8, lambda: webbrowser.open(f"http://localhost:{puerto}")).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nServidor detenido.")


if __name__ == "__main__":
    main()
