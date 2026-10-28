$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot -Parent)
& .\.venv\Scripts\python.exe manage.py check
if ($LASTEXITCODE -ne 0) { throw 'Django checks failed.' }
& .\.venv\Scripts\python.exe manage.py test core website --noinput -v 2
if ($LASTEXITCODE -ne 0) { throw 'Tests failed.' }
Push-Location frontend
try { npm.cmd run build; if ($LASTEXITCODE -ne 0) { throw 'React build failed.' } } finally { Pop-Location }

Push-Location website/frontend
try { npm.cmd run build; if ($LASTEXITCODE -ne 0) { throw "Institution website build failed." } } finally { Pop-Location }
