# Instala y levanta el Sorteo de La Bestia en Windows, sin Git Bash. Se usa a través de run.cmd:
#
#   .\run.cmd               chequea/instala lo necesario y lo deja andando en segundo plano
#   .\run.cmd --consola     arranca en esta ventana (Ctrl+C lo apaga) y abre el navegador
#   .\run.cmd --estado      dice si está andando
#   .\run.cmd --parar       lo apaga
#   .\run.cmd --reiniciar   lo apaga y lo vuelve a levantar (por ejemplo después de un git pull)
#   .\run.cmd --log         muestra el log
#   .\run.cmd --desinstalar saca el arranque automático (no borra los datos)
#   .\run.cmd --exportar [archivo]   guarda TODOS los datos en un .zip (por defecto en respaldos\)
#   .\run.cmd --importar archivo     reemplaza todos los datos por los de ese .zip (antes hace un respaldo)
#
# Como administrador arranca al prender la máquina sin que nadie inicie sesión y abre el firewall.
# Sin administrador arranca cuando alguien inicia sesión y el firewall hay que abrirlo a mano.
#   $env:PUERTO=8782; .\run.cmd   usa otro puerto (por defecto el de config.json, 8781)
#   $env:MODO='prod'; .\run.cmd   se conecta al Sistema de Registros de producción (sin MODO usa el de config.json)
#
# Este archivo va en UTF-8 con BOM: sin BOM, Windows PowerShell 5.1 rompe los acentos.

$Argumentos = @($args)
$ErrorActionPreference = 'Continue'  # con 'Stop', PowerShell 5.1 corta ante cualquier texto en stderr de un programa

$DirInicial = (Get-Location).Path
Set-Location -LiteralPath $PSScriptRoot
$Carpeta = $PSScriptRoot

$Tarea = 'Sorteo La Bestia'
$Vbs = Join-Path $Carpeta 'sorteo-arranque.vbs'
$PidFile = Join-Path $Carpeta 'datos\servidor.pid'
$Log = Join-Path $Carpeta 'datos\servidor.log'
$VbsInicio = Join-Path ([Environment]::GetFolderPath('Startup')) 'sorteo-arranque.vbs'
$PY = $null

function Ok($texto)    { Write-Host "  [ok] $texto" -ForegroundColor Green }
function Aviso($texto) { Write-Host "  [!]  $texto" -ForegroundColor Yellow }
function Fallar($texto) { Write-Host "  [x]  $texto" -ForegroundColor Red; exit 1 }

if ($env:OS -ne 'Windows_NT') { Fallar 'run.ps1 es para Windows; en Linux o Mac usá ./run.sh' }

$Puerto = $env:PUERTO
if (-not $Puerto) {
  try {
    $Puerto = [string](Get-Content -LiteralPath 'config.json' -Raw -Encoding UTF8 -ErrorAction Stop | ConvertFrom-Json).puerto
  } catch { $Puerto = $null }
}
if (-not $Puerto) { $Puerto = '8781' }

$ArgModo = @()
if ($env:MODO) {
  if ($env:MODO -notin 'dev', 'prod') { Fallar 'MODO tiene que ser dev o prod' }
  $ArgModo = @("--$($env:MODO)")
}

function Es-Admin {
  $identidad = [Security.Principal.WindowsIdentity]::GetCurrent()
  return ([Security.Principal.WindowsPrincipal]$identidad).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

# --------------------------------------------------------------- python ---
# El código Python que se pasa con -c no puede tener comillas dobles: PowerShell 5.1 no las escapa.

function Sin-CR($codigo) { return $codigo.Replace("`r`n", "`n") }

# Devuelve la ruta real del primer Python 3.9+ con sqlite3. Salta el acceso directo de la Microsoft Store, que no ejecuta nada.
function Buscar-Python {
  $prueba = 'import sys, sqlite3; assert sys.version_info >= (3, 9); print(sys.executable)'
  $candidatos = @()
  foreach ($nombre in 'python', 'py', 'python3') {
    if (Get-Command $nombre -CommandType Application -ErrorAction SilentlyContinue) { $candidatos += $nombre }
  }
  # Recién instalado, el PATH de esta ventana todavía no lo incluye.
  foreach ($patron in "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe", "$env:ProgramFiles\Python3*\python.exe") {
    $candidatos += @(Get-Item -Path $patron -ErrorAction SilentlyContinue | ForEach-Object { $_.FullName })
  }
  foreach ($c in $candidatos) {
    $salida = & $c -c $prueba 2>$null
    if ($LASTEXITCODE -eq 0 -and $salida) { return [string]($salida | Select-Object -Last 1) }
  }
  return $null
}

function Instalar-Python {
  Write-Host '  Instalando Python 3...'
  if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
    Fallar 'Falta Python 3. Instalalo desde https://www.python.org/downloads/ y volvé a correr .\run.cmd'
  }
  & winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements
  $maquina = [Environment]::GetEnvironmentVariable('Path', 'Machine')
  $usuario = [Environment]::GetEnvironmentVariable('Path', 'User')
  $env:Path = "$maquina;$usuario"
}

function Requerir-Python {
  $script:PY = Buscar-Python
  if (-not $script:PY) { Fallar 'No hay Python 3.9+ instalado' }
}

function Preparar {
  Write-Host 'Chequeando lo necesario...'
  $script:PY = Buscar-Python
  if (-not $script:PY) {
    Instalar-Python
    $script:PY = Buscar-Python
    if (-not $script:PY) { Fallar 'No encontré un Python 3.9+ con sqlite3. Cerrá esta ventana, abrí otra y volvé a correr .\run.cmd' }
  }
  $version = (& $script:PY -V 2>&1 | Out-String).Trim()
  $sqlite = (& $script:PY -c 'import sqlite3; print(sqlite3.sqlite_version)' | Out-String).Trim()
  Ok "$version en $script:PY (SQLite $sqlite)"

  if (-not ((Test-Path -LiteralPath 'servidor.py') -and (Test-Path -LiteralPath 'index.html'))) {
    Fallar 'Faltan servidor.py o index.html'
  }
  & $script:PY -c "import ast; ast.parse(open('servidor.py', encoding='utf-8').read())"
  if ($LASTEXITCODE -ne 0) { Fallar 'servidor.py tiene errores con este Python' }
  Ok 'servidor.py compila'

  foreach ($sub in 'datos', 'respaldos') {
    try {
      New-Item -ItemType Directory -Force -Path $sub -ErrorAction Stop | Out-Null
      $prueba = Join-Path $sub '.prueba_escritura'
      Set-Content -LiteralPath $prueba -Value 'x' -ErrorAction Stop
      Remove-Item -LiteralPath $prueba -ErrorAction Stop
    } catch { Fallar "No puedo escribir en $sub\ (revisá los permisos de $Carpeta)" }
  }
  Ok 'carpetas datos\ y respaldos\ listas'

  if ($Puerto -notmatch '^\d+$') { Fallar "PUERTO tiene que ser un número (es '$Puerto')" }
  if ([int]$Puerto -lt 1 -or [int]$Puerto -gt 65535) { Fallar "PUERTO fuera de rango: $Puerto" }
}

# ---------------------------------------------------------------- red -----

function Puerto-Ocupado {
  $codigo = Sin-CR @'
import socket, sys
s = socket.socket()
try:
    s.bind(('0.0.0.0', int(sys.argv[1])))
except OSError:
    sys.exit(0)
sys.exit(1)
'@
  & $script:PY -c $codigo $Puerto 2>$null | Out-Null
  return ($LASTEXITCODE -eq 0)
}

# ¿Responde el Sorteo en el puerto? Espera hasta $Espera segundos.
function Responde([int]$Espera = 1) {
  $codigo = Sin-CR @'
import json, sys, time, urllib.request
puerto, espera = sys.argv[1], float(sys.argv[2])
fin = time.time() + espera
while True:
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{puerto}/api/estado', timeout=2) as r:
            sys.exit(0 if json.load(r).get('sistema') == 'sorteo' else 1)
    except Exception:
        if time.time() > fin:
            sys.exit(1)
        time.sleep(0.5)
'@
  & $script:PY -c $codigo $Puerto "$Espera" 2>$null | Out-Null
  return ($LASTEXITCODE -eq 0)
}

function Mostrar-Direcciones {
  $codigo = Sin-CR @'
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
try:
    s.connect(('10.255.255.255', 1)); print(s.getsockname()[0])
except OSError:
    pass
'@
  $ip = (& $script:PY -c $codigo 2>$null | Out-String).Trim()
  Write-Host "  En esta máquina:   http://localhost:$Puerto"
  if ($ip) { Write-Host "  Desde la red/celu: http://${ip}:$Puerto" }
  Write-Host "  API:               http://localhost:$Puerto/api"
}

function Abrir-Firewall {
  if (-not (Es-Admin)) {
    Aviso "sin permisos de administrador no pude abrir el puerto $Puerto en el firewall: desde otras PC puede no conectar"
    return
  }
  try {
    Remove-NetFirewallRule -DisplayName $Tarea -ErrorAction SilentlyContinue
    New-NetFirewallRule -DisplayName $Tarea -Direction Inbound -Action Allow -Protocol TCP -LocalPort ([int]$Puerto) -ErrorAction Stop | Out-Null
    Ok "firewall de Windows: abrí el puerto $Puerto"
  } catch { Aviso "no pude abrir el puerto $Puerto en el firewall: $($_.Exception.Message)" }
}

# ------------------------------------------------- arranque automático ----

function Hay-Tarea { return [bool](Get-ScheduledTask -TaskName $Tarea -ErrorAction SilentlyContinue) }

# Deja el arranque automático y levanta el programa sin ventana.
function Arrancar-Windows {
  $pyw = Join-Path (Split-Path -Parent $script:PY) 'pythonw.exe'
  if (-not (Test-Path -LiteralPath $pyw)) { Fallar "No encontré $pyw" }
  $modoTexto = $ArgModo -join ' '
  $lineas = @(
    'Set sh = CreateObject("WScript.Shell")',
    ('sh.CurrentDirectory = "{0}"' -f $Carpeta),
    ('sh.Run """{0}"" servidor.py {1} {2} --no-browser", 0, False' -f $pyw, $Puerto, $modoTexto)
  )
  # En UTF-16: wscript no entiende UTF-8 y la ruta puede tener acentos.
  [IO.File]::WriteAllText($Vbs, (($lineas -join "`r`n") + "`r`n"), [Text.Encoding]::Unicode)

  # El Python de la Microsoft Store no corre como SYSTEM.
  if ((Es-Admin) -and ($pyw -notlike '*WindowsApps*')) {
    try {
      $accion = New-ScheduledTaskAction -Execute 'wscript.exe' -Argument ('"{0}"' -f $Vbs)
      $disparador = New-ScheduledTaskTrigger -AtStartup
      $cuenta = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
      $ajustes = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero)
      Register-ScheduledTask -TaskName $Tarea -Action $accion -Trigger $disparador -Principal $cuenta -Settings $ajustes -Force -ErrorAction Stop | Out-Null
      Remove-Item -LiteralPath $VbsInicio -ErrorAction SilentlyContinue
      Ok 'arranca solo al prender Windows (tarea programada, sin que nadie inicie sesión)'
    } catch { Aviso "no pude crear la tarea programada: $($_.Exception.Message)" }
  }
  if (-not (Hay-Tarea)) {
    try {
      Copy-Item -LiteralPath $Vbs -Destination $VbsInicio -Force -ErrorAction Stop
      Ok 'arranca solo cuando alguien inicia sesión en Windows (carpeta Inicio)'
      Aviso 'para que arranque sin iniciar sesión, corré run.cmd como administrador'
    } catch { Aviso 'no pude dejarlo en la carpeta Inicio: no va a arrancar solo al prender Windows' }
  }
  Start-Process -FilePath 'wscript.exe' -ArgumentList ('"{0}"' -f $Vbs)
}

function Quitar-Windows {
  if (Hay-Tarea) {
    try {
      Unregister-ScheduledTask -TaskName $Tarea -Confirm:$false -ErrorAction Stop
      Write-Host 'Tarea programada quitada.'
    } catch { Fallar 'No pude borrar la tarea: corré run.cmd como administrador' }
  }
  Remove-Item -LiteralPath $VbsInicio, $Vbs -ErrorAction SilentlyContinue
  Write-Host 'Arranque automático quitado (los datos siguen en datos\).'
}

# ------------------------------------------------------------ arrancar ----

function Proceso-Vivo([int]$Id) {
  $proceso = Get-Process -Id $Id -ErrorAction SilentlyContinue
  return [bool]($proceso -and $proceso.ProcessName -like 'python*')
}

function Parar([switch]$Callado) {
  $hizo = $false
  if (Test-Path -LiteralPath $PidFile) {
    $texto = (Get-Content -LiteralPath $PidFile -Raw).Trim()
    if ($texto -match '^\d+$' -and (Proceso-Vivo ([int]$texto))) {
      try {
        Stop-Process -Id ([int]$texto) -Force -ErrorAction Stop
      } catch { Fallar 'No pude apagarlo (lo arrancó la tarea programada): corré run.cmd como administrador' }
      for ($i = 0; $i -lt 10 -and (Proceso-Vivo ([int]$texto)); $i++) { Start-Sleep -Milliseconds 500 }
      $hizo = $true
    }
    Remove-Item -LiteralPath $PidFile -ErrorAction SilentlyContinue
  }
  if (-not $Callado) { if ($hizo) { Write-Host 'Sorteo apagado.' } else { Write-Host 'No estaba andando.' } }
}

function Levantar {
  Preparar

  # Algo ya escucha en el puerto: si es este sistema lo reinicio, si es otro programa no lo toco.
  if (Puerto-Ocupado) {
    if (Responde 1) {
      Write-Host "  Ya estaba andando en el puerto $Puerto; lo reinicio."
      Parar -Callado
      Start-Sleep -Seconds 1
      if (Puerto-Ocupado) { Fallar "El puerto $Puerto sigue ocupado por un Sorteo que no arranqué yo (apagalo a mano)" }
    } else {
      Fallar "El puerto $Puerto lo usa otro programa. Probá con otro:  `$env:PUERTO=8782; .\run.cmd"
    }
  }

  Write-Host 'Arrancando...'
  Abrir-Firewall
  Arrancar-Windows

  if (-not (Responde 15)) {
    Write-Host '  [x]  No arrancó. Últimas líneas del log:' -ForegroundColor Red
    if (Test-Path -LiteralPath $Log) { Get-Content -LiteralPath $Log -Tail 30 -Encoding UTF8 }
    exit 1
  }
  Ok "responde en el puerto $Puerto"

  Write-Host ''
  Write-Host 'Sorteo andando.'
  Mostrar-Direcciones
  $hora = Get-Date -Format 'yyyy-MM-dd HH:mm'
  Write-Host "  Hora del servidor: $hora (a las 23:59 de esta hora se cruzan los tickets y se desloguea a todos)"
  Write-Host '  Estado: .\run.cmd --estado  ·  Log: .\run.cmd --log  ·  Apagar: .\run.cmd --parar'
}

# Una ruta que escribió el usuario es relativa a donde estaba parado, no a esta carpeta.
function Ruta-Del-Usuario($ruta) {
  if ([IO.Path]::IsPathRooted($ruta)) { return $ruta }
  return (Join-Path $DirInicial $ruta)
}

# ----------------------------------------------------------------- main ---

$Opcion = if ($Argumentos.Count -ge 1) { [string]$Argumentos[0] } else { '' }
$Resto = @($Argumentos | Select-Object -Skip 1)

switch -Exact ($Opcion) {
  { $_ -in '', '--fondo' } { Levantar; break }
  '--consola' {
    Preparar
    if (Puerto-Ocupado) { Fallar "El puerto $Puerto está ocupado (¿ya está andando? .\run.cmd --estado)" }
    & $PY servidor.py $Puerto @ArgModo @Resto
    exit $LASTEXITCODE
  }
  '--estado' {
    Requerir-Python
    if (Responde 1) {
      Write-Host "Andando en el puerto $Puerto."
      Mostrar-Direcciones
    } else {
      Write-Host 'No está andando.'
      exit 1
    }
    break
  }
  '--parar' { Parar; break }
  '--reiniciar' { Parar -Callado; Levantar; break }
  '--log' {
    if (Test-Path -LiteralPath $Log) { Get-Content -LiteralPath $Log -Tail 100 -Encoding UTF8 } else { Write-Host 'Todavía no hay log.' }
    break
  }
  '--exportar' {
    Requerir-Python
    if ($Resto.Count -ge 1) { & $PY servidor.py --exportar (Ruta-Del-Usuario $Resto[0]) } else { & $PY servidor.py --exportar }
    exit $LASTEXITCODE
  }
  '--importar' {
    if ($Resto.Count -lt 1) { Fallar 'Falta el archivo:  .\run.cmd --importar sorteo_exportado.zip' }
    Requerir-Python
    $archivo = Ruta-Del-Usuario $Resto[0]
    if (-not (Test-Path -LiteralPath $archivo -PathType Leaf)) { Fallar "No existe el archivo: $archivo" }
    & $PY servidor.py --importar $archivo
    $rc = $LASTEXITCODE
    if ($rc -eq 10) {
      $respuesta = Read-Host 'Para reemplazar TODOS los datos de esta instalación escribí «borrar todo»'
      if ($respuesta -cne 'borrar todo') { Write-Host 'Cancelado: no se tocó nada.'; exit 1 }
    } elseif ($rc -ne 0) { exit 1 }
    $andaba = Responde 1
    if ($andaba) { Parar -Callado }
    & $PY servidor.py --importar $archivo --confirmar
    $rc = $LASTEXITCODE
    if ($andaba) { Levantar }
    exit $rc
  }
  '--desinstalar' { Quitar-Windows; break }
  { $_ -in '-h', '--help' } {
    Get-Content -LiteralPath $PSCommandPath -Encoding UTF8 | Select-Object -First 16 | ForEach-Object { $_ -replace '^# ?', '' }
    break
  }
  default {
    Write-Host "Opción desconocida: $Opcion  (ver .\run.cmd --help)" -ForegroundColor Red
    exit 1
  }
}
