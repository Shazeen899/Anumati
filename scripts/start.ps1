$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot -Parent)
& .\.venv\Scripts\python.exe scripts/runtime.py start
if ($LASTEXITCODE -ne 0) { throw 'Startup failed. Inspect .runtime logs.' }
