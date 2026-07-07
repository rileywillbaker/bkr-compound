# B-Quant database backup (Windows host, Docker Desktop).
# Dumps the Postgres/Timescale DB from the compose stack and keeps the last
# $Keep dumps. Schedule daily via Task Scheduler, e.g.:
#   schtasks /Create /SC DAILY /ST 23:30 /TN "B-Quant backup" `
#     /TR "powershell -NoProfile -File C:\path\to\bkr-compound\scripts\backup.ps1"
param(
    [string]$OutDir = (Join-Path $PSScriptRoot "..\backups"),
    [int]$Keep = 14
)

$ErrorActionPreference = "Stop"
New-Item -ItemType Directory -Force $OutDir | Out-Null

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$file = Join-Path $OutDir "bquant-$stamp.dump"

# Custom format (-Fc): compressed, restorable table-by-table with pg_restore.
docker compose exec -T db pg_dump -U bquant -d bquant -Fc | Set-Content -Path $file -AsByteStream
if ($LASTEXITCODE -ne 0 -or (Get-Item $file).Length -eq 0) {
    Remove-Item $file -ErrorAction SilentlyContinue
    throw "pg_dump failed"
}
Write-Output "wrote $file ($([math]::Round((Get-Item $file).Length / 1MB, 1)) MB)"

# Retention: newest $Keep dumps survive.
Get-ChildItem $OutDir -Filter "bquant-*.dump" |
    Sort-Object LastWriteTime -Descending |
    Select-Object -Skip $Keep |
    Remove-Item -Confirm:$false

# Restore (reference):
#   docker compose exec -T db pg_restore -U bquant -d bquant --clean --if-exists < backups\bquant-<stamp>.dump
