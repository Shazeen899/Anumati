param([string]$Python = 'python')
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot -Parent)
& $Python -m venv .venv
if ($LASTEXITCODE -ne 0) { throw 'Python 3.12+ with venv and pip is required.' }
& .\.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
if ($LASTEXITCODE -ne 0) { throw 'Python dependency installation failed.' }
Push-Location frontend
try { npm.cmd ci --no-audit --no-fund; if ($LASTEXITCODE -ne 0) { throw 'React dependency installation failed.' }; npm.cmd run build; if ($LASTEXITCODE -ne 0) { throw 'React build failed.' } } finally { Pop-Location }
if (-not (Test-Path .env)) {
    & .\.venv\Scripts\python.exe -c "import secrets;from pathlib import Path;Path('.env').write_text('DJANGO_SECRET_KEY='+secrets.token_urlsafe(48)+'\nDJANGO_DEBUG=1\nDEMO_MODE=1\nPGDATABASE=anumati\nPGUSER=anumati\nPGPASSWORD='+secrets.token_urlsafe(24)+'\nPGHOST=127.0.0.1\nPGPORT=55432\nREDIS_URL=redis://127.0.0.1:56379/0\nDEMO_PASSWORD='+secrets.token_urlsafe(15)+'\n')"
}
& .\.venv\Scripts\python.exe scripts/runtime.py credentials
Write-Host 'Install/extract PostgreSQL and Redis as described in README.md, then run scripts/start.ps1.'
