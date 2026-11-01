@echo off
REM Fish Memory workers control (bypass PowerShell execution policy)
REM   fishmem_workers.cmd start ^| stop ^| status ^| once ^| eval ^| health
setlocal
set "HERE=%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%HERE%fishmem_workers.ps1" %*
exit /b %ERRORLEVEL%
