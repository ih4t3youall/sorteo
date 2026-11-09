#!/bin/bash
# Instala y levanta el Sorteo de La Bestia. Pensado para: git clone ... && ./run.sh
#
#   ./run.sh               chequea/instala lo necesario y lo deja andando en segundo plano.
#                          En un servidor Linux con systemd lo instala como servicio
#                          (arranca solo al prender la máquina y se reinicia si se cae).
#   ./run.sh --consola     arranca en esta terminal (Ctrl+C lo apaga) y abre el navegador
#   ./run.sh --estado      dice si está andando
#   ./run.sh --parar       lo apaga
#   ./run.sh --reiniciar   lo apaga y lo vuelve a levantar (por ejemplo después de un git pull)
#   ./run.sh --log         muestra el log
#   ./run.sh --desinstalar saca el arranque automático (no borra los datos)
#   ./run.sh --exportar [archivo]   guarda TODOS los datos en un .zip (por defecto en respaldos/)
#   ./run.sh --importar archivo     reemplaza todos los datos por los de ese .zip (antes hace un respaldo)
#
# Arranca solo al prender la máquina: Linux con systemd (o cron @reboot si no hay sudo) y Windows.
# En Windows hay que correrlo desde Git Bash, y como administrador para que arranque sin que nadie inicie sesión.
#   PUERTO=8782 ./run.sh   usa otro puerto (por defecto el de config.json, 8781)
#   MODO=prod ./run.sh     se conecta al Sistema de Registros de producción (192.168.0.150); MODO=dev → localhost
#                          (sin MODO usa el de config.json)
#
# Solo hace falta Python 3.9 o más nuevo con sqlite3 (viene con Python); no usa paquetes externos.
# Los datos quedan en datos/ (base SQLite) y los respaldos en respaldos/.

set -euo pipefail
DIR_INICIAL="$(pwd)"
cd "$(dirname "$0")"
CARPETA="$(pwd)"

PUERTO="${PUERTO:-$(sed -n 's/.*"puerto": *\([0-9]*\).*/\1/p' config.json 2>/dev/null | head -1)}"
PUERTO="${PUERTO:-8781}"
MODO="${MODO:-}"
ARG_MODO=""
case "$MODO" in "") ;; dev|prod) ARG_MODO="--$MODO" ;; *) echo "MODO tiene que ser dev o prod" >&2; exit 1 ;; esac
SERVICIO="sorteo-la-bestia"
UNIDAD="/etc/systemd/system/$SERVICIO.service"
PIDFILE="datos/servidor.pid"
LOG="datos/servidor.log"
SO="$(uname -s)"
case "$SO" in MINGW*|MSYS*) SO="Windows" ;; esac
TAREA="Sorteo La Bestia"
VBS="$CARPETA/sorteo-arranque.vbs"
MARCA_CRON="# sorteo-la-bestia"

ok()    { echo "  ✓ $*"; }
aviso() { echo "  ! $*"; }
error() { echo "  ✗ $*" >&2; exit 1; }

# root o sudo, para instalar paquetes y el servicio
if [ "$(id -u)" -eq 0 ]; then
  SUDO=""
elif command -v sudo >/dev/null; then
  SUDO="sudo"
else
  SUDO="no"
fi
como_root() {
  [ "$SUDO" = "no" ] && error "Hace falta ser root (o tener sudo) para: $*"
  $SUDO "$@"
}

hay_systemd() {
  [ "$SO" = "Linux" ] && command -v systemctl >/dev/null && [ -d /run/systemd/system ]
}
servicio_instalado() { hay_systemd && [ -f "$UNIDAD" ]; }

# --------------------------------------------------------------- python ---

# Primer Python >= 3.8 que tenga sqlite3 (el de sistema puede ser viejo y haber otro instalado al lado).
buscar_python() {
  local p
  for p in python3 python3.13 python3.12 python3.11 python3.10 python3.9 \
           /usr/local/bin/python3 /opt/homebrew/bin/python3 python py; do
    command -v "$p" >/dev/null 2>&1 || continue
    if "$p" -c 'import sys, sqlite3; sys.exit(sys.version_info < (3, 9))' 2>/dev/null; then
      command -v "$p"; return 0
    fi
  done
  return 1
}

instalar_python() {
  echo "  Instalando Python 3..."
  if [ "$SO" = "Windows" ]; then
    command -v winget >/dev/null || error "Falta Python 3. Instalalo desde https://www.python.org/downloads/ y volvé a correr ./run.sh"
    winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements
    error "Python instalado: cerrá esta ventana, abrí otra de Git Bash y volvé a correr ./run.sh"
  elif [ "$SO" = "Darwin" ]; then
    if command -v brew >/dev/null; then
      brew install python3
    else
      error "Falta Python 3. Instalalo desde https://www.python.org/downloads/ (o con 'xcode-select --install') y volvé a correr ./run.sh"
    fi
  elif command -v apt-get >/dev/null; then
    como_root apt-get update -qq
    como_root env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3 libsqlite3-0
  elif command -v dnf >/dev/null; then
    como_root dnf install -y python3 || como_root dnf install -y python39
  elif command -v yum >/dev/null; then
    como_root yum install -y python3 || como_root yum install -y python38
  elif command -v zypper >/dev/null; then
    como_root zypper --non-interactive install python3
  elif command -v apk >/dev/null; then
    como_root apk add --no-cache python3
  elif command -v pacman >/dev/null; then
    como_root pacman -Sy --noconfirm python
  else
    error "No sé instalar Python en este sistema. Instalá Python 3.9 o más nuevo y volvé a correr ./run.sh"
  fi
}

preparar() {
  echo "Chequeando lo necesario..."
  PY="$(buscar_python || true)"
  if [ -z "$PY" ]; then
    instalar_python
    hash -r
    PY="$(buscar_python || true)"
    [ -n "$PY" ] || error "Se instaló Python pero no hay uno 3.9+ con sqlite3. Instalá un Python más nuevo y volvé a correr ./run.sh"
  fi
  ok "$("$PY" -V) en $PY (SQLite $("$PY" -c 'import sqlite3; print(sqlite3.sqlite_version)'))"

  [ -f servidor.py ] && [ -f index.html ] || error "Faltan servidor.py o index.html: corré ./run.sh desde la carpeta del proyecto"
  "$PY" -m py_compile servidor.py 2>/dev/null || "$PY" -c 'import ast; ast.parse(open("servidor.py").read())' \
    || error "servidor.py tiene errores con este Python"
  ok "servidor.py compila"

  mkdir -p datos respaldos
  [ -w datos ] && [ -w respaldos ] || error "No puedo escribir en datos/ o respaldos/ (revisá los permisos de $CARPETA)"
  ok "carpetas datos/ y respaldos/ listas"

  case "$PUERTO" in ''|*[!0-9]*) error "PUERTO tiene que ser un número (es '$PUERTO')" ;; esac
  [ "$PUERTO" -ge 1 ] && [ "$PUERTO" -le 65535 ] || error "PUERTO fuera de rango: $PUERTO"
}

# ---------------------------------------------------------------- red -----

puerto_ocupado() {
  "$PY" - "$PUERTO" <<'EOF'
import socket, sys
s = socket.socket()
if sys.platform != "win32":  # en Windows esta opción haría que el puerto parezca libre aunque esté ocupado
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
    s.bind(("0.0.0.0", int(sys.argv[1])))
except OSError:
    sys.exit(0)
sys.exit(1)
EOF
}

# Responde el Sorteo en el puerto? (espera hasta $2 segundos)
responde() {
  "$PY" - "$PUERTO" "${1:-1}" <<'EOF'
import json, sys, time, urllib.request
puerto, espera = sys.argv[1], float(sys.argv[2])
fin = time.time() + espera
while True:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{puerto}/api/estado", timeout=2) as r:
            sys.exit(0 if json.load(r).get("sistema") == "sorteo" else 1)
    except Exception:
        if time.time() > fin:
            sys.exit(1)
        time.sleep(0.5)
EOF
}

ip_local() {
  "$PY" -c '
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
try:
    s.connect(("10.255.255.255", 1)); print(s.getsockname()[0])
except OSError:
    pass' 2>/dev/null | tr -d '\r' || true
}

mostrar_direcciones() {
  local ip; ip="$(ip_local)"
  echo "  En esta máquina:   http://localhost:$PUERTO"
  [ -n "$ip" ] && echo "  Desde la red/celu: http://$ip:$PUERTO"
  echo "  API:               http://localhost:$PUERTO/api"
}

# Si hay un firewall activo, abre el puerto (solo ese).
abrir_firewall() {
  if [ "$SO" = "Windows" ]; then
    if net session >/dev/null 2>&1; then
      netsh advfirewall firewall delete rule name="$TAREA" >/dev/null 2>&1 || true
      netsh advfirewall firewall add rule name="$TAREA" dir=in action=allow protocol=TCP localport="$PUERTO" >/dev/null && ok "firewall de Windows: abrí el puerto $PUERTO"
    else
      aviso "sin permisos de administrador no pude abrir el puerto $PUERTO en el firewall: desde otras PC puede no conectar"
    fi
    return 0
  fi
  [ "$SO" = "Linux" ] || return 0
  if command -v ufw >/dev/null && como_root ufw status 2>/dev/null | grep -q "Status: active"; then
    if como_root ufw status | grep -qE "^$PUERTO(/tcp)?\s+ALLOW"; then
      ok "firewall (ufw): puerto $PUERTO abierto"
    else
      como_root ufw allow "$PUERTO/tcp" >/dev/null && ok "firewall (ufw): abrí el puerto $PUERTO"
    fi
  elif command -v firewall-cmd >/dev/null && como_root firewall-cmd --state >/dev/null 2>&1; then
    if como_root firewall-cmd --query-port="$PUERTO/tcp" >/dev/null 2>&1; then
      ok "firewall (firewalld): puerto $PUERTO abierto"
    else
      como_root firewall-cmd --permanent --add-port="$PUERTO/tcp" >/dev/null
      como_root firewall-cmd --reload >/dev/null
      ok "firewall (firewalld): abrí el puerto $PUERTO"
    fi
  fi
  return 0
}

# ------------------------------------------------------------ arrancar ----

instalar_servicio() {
  local usuario="${SUDO_USER:-$(id -un)}"
  local unidad
  unidad="[Unit]
Description=Sorteo - La Bestia
After=network-online.target
Wants=network-online.target

[Service]
User=$usuario
WorkingDirectory=$CARPETA
ExecStart=$PY $CARPETA/servidor.py $PUERTO $ARG_MODO --no-browser
Environment=PYTHONUNBUFFERED=1
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
"
  if [ -f "$UNIDAD" ] && [ "$(cat "$UNIDAD")" = "$unidad" ]; then
    ok "servicio $SERVICIO ya instalado"
  else
    printf '%s' "$unidad" | como_root tee "$UNIDAD" >/dev/null
    como_root systemctl daemon-reload
    ok "servicio $SERVICIO instalado (corre como '$usuario', arranca solo al prender la máquina)"
  fi
  [ -n "$SUDO" ] && [ "$usuario" != "root" ] && como_root chown -R "$usuario" datos respaldos
  como_root systemctl enable "$SERVICIO" >/dev/null 2>&1
  como_root systemctl restart "$SERVICIO"
}

arrancar_fondo() {
  PYTHONUNBUFFERED=1 nohup "$PY" servidor.py "$PUERTO" $ARG_MODO --no-browser >>"$LOG" 2>&1 &
  echo $! >"$PIDFILE"
}

# -------------------------------------------- arranque automático ----

instalar_cron() {
  command -v crontab >/dev/null || { aviso "no hay systemd ni cron: no va a arrancar solo al prender la máquina"; return 0; }
  local otras
  otras="$(crontab -l 2>/dev/null | grep -vF "$MARCA_CRON" || true)"
  {
    [ -z "$otras" ] || printf '%s\n' "$otras"
    echo "@reboot cd \"$CARPETA\" && PYTHONUNBUFFERED=1 \"$PY\" servidor.py $PUERTO $ARG_MODO --no-browser >>\"$LOG\" 2>&1 $MARCA_CRON"
  } | crontab -
  ok "arranca solo al prender la máquina (cron @reboot)"
}

quitar_cron() {
  command -v crontab >/dev/null || return 0
  crontab -l 2>/dev/null | grep -qF "$MARCA_CRON" || return 0
  crontab -l | { grep -vF "$MARCA_CRON" || true; } | crontab -
  echo "Arranque automático (cron) quitado."
}

carpeta_inicio_windows() { echo "$(cygpath -u "$APPDATA")/Microsoft/Windows/Start Menu/Programs/Startup"; }
hay_tarea_windows() { schtasks //Query //TN "$TAREA" >/dev/null 2>&1; }

# Deja el arranque automático en Windows y levanta el programa sin ventana.
arrancar_windows() {
  local pyw win_vbs
  pyw="$("$PY" -c 'import os, sys; print(os.path.join(os.path.dirname(sys.executable), "pythonw.exe"))' | tr -d '\r')"
  win_vbs="$(cygpath -w "$VBS")"
  # En UTF-16: wscript no entiende UTF-8 y la ruta puede tener acentos.
  "$PY" - "$win_vbs" "$(cygpath -w "$CARPETA")" "$pyw" "$PUERTO" "$ARG_MODO" <<'EOF'
import sys
vbs, carpeta, pyw, puerto, modo = sys.argv[1:6]
with open(vbs, "w", encoding="utf-16", newline="\r\n") as f:
    f.write('Set sh = CreateObject("WScript.Shell")\n')
    f.write(f'sh.CurrentDirectory = "{carpeta}"\n')
    f.write(f'sh.Run """{pyw}"" servidor.py {puerto} {modo} --no-browser", 0, False\n')
EOF
  local inicio; inicio="$(carpeta_inicio_windows)"
  case "$pyw" in
    *WindowsApps*) ;;  # el Python de la Microsoft Store no corre como SYSTEM
    *)
      if net session >/dev/null 2>&1 \
         && schtasks //Create //F //TN "$TAREA" //SC ONSTART //RU SYSTEM //RL HIGHEST //TR "wscript.exe \"$win_vbs\"" >/dev/null 2>&1 \
         && hay_tarea_windows; then
        rm -f "$inicio/sorteo-arranque.vbs"
        ok "arranca solo al prender Windows (tarea programada, sin que nadie inicie sesión)"
      fi ;;
  esac
  if ! hay_tarea_windows; then
    if cp "$VBS" "$inicio/sorteo-arranque.vbs"; then
      ok "arranca solo cuando alguien inicia sesión en Windows (carpeta Inicio)"
      aviso "para que arranque sin iniciar sesión, corré ./run.sh con Git Bash como administrador"
    else
      aviso "no pude dejarlo en la carpeta Inicio: no va a arrancar solo al prender Windows"
    fi
  fi
  wscript.exe "$win_vbs"
}

quitar_windows() {
  if hay_tarea_windows; then
    schtasks //Delete //F //TN "$TAREA" >/dev/null 2>&1 || error "No pude borrar la tarea: corré esto con Git Bash como administrador"
    echo "Tarea programada quitada."
  fi
  rm -f "$(carpeta_inicio_windows)/sorteo-arranque.vbs" "$VBS"
}

vivo() {
  if [ "$SO" = "Windows" ]; then tasklist //FI "PID eq $1" //NH 2>/dev/null | grep -qi python
  else kill -0 "$1" 2>/dev/null; fi
}

matar() {
  if [ "$SO" = "Windows" ]; then taskkill //F //PID "$1" >/dev/null
  else kill "$1"; fi
  for _ in 1 2 3 4 5 6 7 8 9 10; do vivo "$1" || break; sleep 0.5; done
}

parar() {
  local hizo=""
  if servicio_instalado && systemctl is-active --quiet "$SERVICIO"; then
    como_root systemctl stop "$SERVICIO"; hizo=1
  fi
  if [ -f "$PIDFILE" ]; then
    local pid; pid="$(tr -d '\r\n' <"$PIDFILE")"
    if [ -n "$pid" ] && vivo "$pid"; then
      matar "$pid"; hizo=1
    fi
    rm -f "$PIDFILE"
  fi
  [ -n "$hizo" ] && echo "Sorteo apagado." || echo "No estaba andando."
}

levantar() {
  preparar

  # Algo ya escucha en el puerto: si es este sistema lo reinicio, si es otro programa no lo toco.
  if puerto_ocupado; then
    if responde 1; then
      echo "  Ya estaba andando en el puerto $PUERTO; lo reinicio."
      parar >/dev/null
      sleep 1
      puerto_ocupado && error "El puerto $PUERTO sigue ocupado por un Sorteo que no arranqué yo (apagalo a mano)"
    else
      error "El puerto $PUERTO lo usa otro programa. Probá con otro:  PUERTO=8782 ./run.sh"
    fi
  fi

  echo "Arrancando..."
  if hay_systemd && [ "$SUDO" != "no" ]; then
    instalar_servicio
    abrir_firewall
  elif [ "$SO" = "Windows" ]; then
    abrir_firewall
    arrancar_windows
  else
    arrancar_fondo
    if [ "$SO" = "Linux" ]; then instalar_cron; fi
  fi

  if ! responde 15; then
    echo "  ✗ No arrancó. Últimas líneas del log:" >&2
    if servicio_instalado; then
      como_root journalctl -u "$SERVICIO" -n 30 --no-pager >&2 || true
    else
      tail -n 30 "$LOG" >&2 || true
    fi
    exit 1
  fi
  ok "responde en el puerto $PUERTO"

  echo
  echo "Sorteo andando."
  mostrar_direcciones
  echo "  Hora del servidor: $(date '+%Y-%m-%d %H:%M %Z') (a las 23:59 de esta hora se cruzan los tickets y se desloguea a todos)"
  echo "  Estado: ./run.sh --estado  ·  Log: ./run.sh --log  ·  Apagar: ./run.sh --parar"
}

# Una ruta que escribió el usuario es relativa a donde estaba parado, no a esta carpeta.
ruta_del_usuario() {
  case "$1" in
    /*|[A-Za-z]:*) echo "$1" ;;
    *) echo "$DIR_INICIAL/$1" ;;
  esac
}

# ----------------------------------------------------------------- main ---

case "${1:-}" in
  ""|--fondo)
    levantar ;;
  --consola)
    preparar
    puerto_ocupado && error "El puerto $PUERTO está ocupado (¿ya está andando? ./run.sh --estado)"
    shift
    exec "$PY" servidor.py "$PUERTO" $ARG_MODO "$@" ;;
  --estado)
    PY="$(buscar_python || true)"; [ -n "$PY" ] || error "No hay Python 3.9+ instalado"
    if responde 1; then
      echo "Andando en el puerto $PUERTO."
      servicio_instalado && echo "  Servicio systemd: $(systemctl is-active "$SERVICIO") / $(systemctl is-enabled "$SERVICIO" 2>/dev/null)"
      mostrar_direcciones
    else
      echo "No está andando."
      exit 1
    fi ;;
  --parar)
    parar ;;
  --reiniciar)
    parar >/dev/null || true
    levantar ;;
  --log)
    if servicio_instalado; then como_root journalctl -u "$SERVICIO" -n 100 --no-pager
    else tail -n 100 "$LOG" 2>/dev/null || echo "Todavía no hay log."; fi ;;
  --exportar)
    PY="$(buscar_python || true)"; [ -n "$PY" ] || error "No hay Python 3.9+ instalado"
    if [ $# -ge 2 ]; then exec "$PY" servidor.py --exportar "$(ruta_del_usuario "$2")"; fi
    exec "$PY" servidor.py --exportar ;;
  --importar)
    [ $# -ge 2 ] || error "Falta el archivo:  ./run.sh --importar sorteo_exportado.zip"
    PY="$(buscar_python || true)"; [ -n "$PY" ] || error "No hay Python 3.9+ instalado"
    ARCHIVO="$(ruta_del_usuario "$2")"
    [ -f "$ARCHIVO" ] || error "No existe el archivo: $ARCHIVO"
    rc=0; "$PY" servidor.py --importar "$ARCHIVO" || rc=$?
    case "$rc" in
      0) ;;
      10)
        printf 'Para reemplazar TODOS los datos de esta instalación escribí «borrar todo»: '
        read -r RESPUESTA
        [ "$RESPUESTA" = "borrar todo" ] || { echo "Cancelado: no se tocó nada."; exit 1; } ;;
      *) exit 1 ;;
    esac
    ANDABA=""
    if responde 1; then ANDABA=1; parar >/dev/null; fi
    rc=0; "$PY" servidor.py --importar "$ARCHIVO" --confirmar || rc=$?
    if [ -n "$ANDABA" ]; then levantar; fi
    exit "$rc" ;;
  --desinstalar)
    if [ "$SO" = "Windows" ]; then quitar_windows; exit 0; fi
    if [ "$SO" = "Linux" ]; then quitar_cron; fi
    servicio_instalado || { echo "El servicio no está instalado."; exit 0; }
    como_root systemctl disable --now "$SERVICIO" >/dev/null 2>&1 || true
    como_root rm -f "$UNIDAD"
    como_root systemctl daemon-reload
    echo "Servicio $SERVICIO desinstalado (los datos siguen en datos/)." ;;
  -h|--help)
    sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//' ;;
  *)
    echo "Opción desconocida: $1  (ver ./run.sh --help)" >&2; exit 1 ;;
esac
