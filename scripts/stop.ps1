$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Split-Path $PSScriptRoot -Parent)).Path
Set-Location $projectRoot
$ledgerPath = Join-Path $projectRoot '.runtime/processes.json'
if (Test-Path -LiteralPath $ledgerPath) {
    $ledger = Get-Content -LiteralPath $ledgerPath -Raw | ConvertFrom-Json
    foreach ($name in @('beat', 'worker', 'web', 'redis')) {
        $entry = $ledger.$name
        if ($entry) {
            $proc = Get-CimInstance Win32_Process -Filter "ProcessId = $($entry.pid)" -ErrorAction SilentlyContinue
            if ($proc -and $proc.CommandLine -and $proc.CommandLine.Contains($projectRoot)) {
                Stop-Process -Id $entry.pid
                Write-Host "Stopped $name"
            }
        }
    }
}
$pgCtl = Join-Path $projectRoot '.runtime/postgres/pgsql/bin/pg_ctl.exe'
$pgData = Join-Path $projectRoot '.runtime/pgdata'
if ((Test-Path -LiteralPath $pgCtl) -and (Test-Path -LiteralPath (Join-Path $pgData 'postmaster.pid'))) {
    & $pgCtl -D $pgData stop -m fast -w
}
