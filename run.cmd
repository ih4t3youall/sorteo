@echo off
rem Lanza run.ps1 sin cambiar la politica de ejecucion de Windows (Bypass vale solo para este proceso).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1" %*
set RC=%errorlevel%
if "%~1"=="" pause
exit /b %RC%
