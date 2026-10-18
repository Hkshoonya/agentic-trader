# Move the running system to or from this Windows machine: deploy/migrate.sh's counterpart.
#
#   old host:  deploy\migrate.ps1 export FILE   stops the host and its task here, then packs the state
#   new host:  deploy\migrate.ps1 import FILE   unpacks it into this checkout (then deploy\install.ps1 -Enable)
#
# The archive is the same as migrate.sh makes, so either end may be Linux or Windows. FILE holds
# config\agentic.toml, config\secrets.toml, .env, data\ and ~\.config\agentic-trading (broker tokens,
# upgrade.env): carry it privately, never through a synced folder, and delete it once imported. The arm
# latches are left behind on purpose, so the new host starts disarmed.
param(
    [Parameter(Mandatory = $true, Position = 0)][ValidateSet("export", "import")][string]$Action,
    [Parameter(Mandatory = $true, Position = 1)][string]$File,
    [string]$HomeDir = $HOME
)
$ErrorActionPreference = "Stop"

$repo = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repo ".venv\Scripts\python.exe"
$config = Join-Path $repo "config\agentic.toml"
$tar = Join-Path $env:WINDIR "System32\tar.exe"  # bsdtar: reads and writes migrate.sh's .tgz
$File = [System.IO.Path]::GetFullPath($File)

function Host-Status {
    if (-not ((Test-Path $python) -and (Test-Path $config))) { return $null }
    $state = & $python -c @"
import json, sys
from agentic_trading.config import load_config
from agentic_trading.host import client
from pathlib import Path
c = load_config(sys.argv[1]); s = Path(c.state_dir)
s = s if s.is_absolute() else Path(sys.argv[2]) / s
st = client.status(s)
print(json.dumps({'up': client.running(st), 'timers': {k: v.get('state') for k, v in (st.get('timers') or {}).items()}}))
"@ $config $repo 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $state) { return $null }
    return $state | ConvertFrom-Json
}

function Owner-Only([string]$Path) {
    # chmod 600/700's counterpart: only this user (and SYSTEM, which Windows needs) can open it. A file
    # takes plain grants (icacls drops inheritable ones on a file, leaving it unreadable even to you); a
    # folder takes inheritable ones, and everything inside is reset to inherit them.
    if (-not (Test-Path $Path)) { return }
    if (Test-Path $Path -PathType Container) {
        icacls $Path /inheritance:r /grant:r "${env:USERNAME}:(OI)(CI)F" "SYSTEM:(OI)(CI)F" /Q | Out-Null
        icacls (Join-Path $Path "*") /reset /T /Q 2>$null | Out-Null
    } else {
        icacls $Path /inheritance:r /grant:r "${env:USERNAME}:F" "SYSTEM:F" /Q | Out-Null
    }
}

if ($Action -eq "export") {
    if (Test-Path $File) { Write-Error "refusing: $File exists; pick a new name"; exit 2 }
    $status = Host-Status
    if ($status) {
        $busy = @($status.timers.PSObject.Properties | Where-Object { $_.Value -eq "active" -and $_.Name -ne "agentic-trading-upgrade-watch" } | ForEach-Object { $_.Name })
        if ($busy.Count -gt 0) { Write-Error "refusing: $($busy -join ', ') is mid-run; try again when it finishes"; exit 1 }
    }
    Disable-ScheduledTask -TaskName "AgenticTrading-Host" -ErrorAction SilentlyContinue | Out-Null
    if ($status -and $status.up) { & $python -m agentic_trading host shutdown --config $config | Out-Host }
    Write-Host "stopped the host and disabled its task on this machine"
    $items = @("config/agentic.toml", "data")
    if (Test-Path (Join-Path $repo "config\secrets.toml")) { $items += "config/secrets.toml" }
    if (Test-Path (Join-Path $repo ".env")) { $items += ".env" }
    # Windows' tar cannot rename on the way in (no -s), so the home files are staged under _home/,
    # the name migrate.sh gives them, in a private folder that is gone once the archive is written.
    $stage = Join-Path ([System.IO.Path]::GetTempPath()) "agentic-move-$PID"
    $homeItems = @()
    if (Test-Path (Join-Path $HomeDir ".config\agentic-trading")) {
        New-Item -ItemType Directory -Path $stage | Out-Null
        Owner-Only $stage
        New-Item -ItemType Directory -Path (Join-Path $stage "_home\.config") | Out-Null
        Copy-Item -Recurse (Join-Path $HomeDir ".config\agentic-trading") (Join-Path $stage "_home\.config\agentic-trading")
        $homeItems = @("-C", $stage, "_home")
    }
    try {
        New-Item -ItemType File -Path $File | Out-Null
        Owner-Only $File  # private before a single byte is written
        & $tar -czf $File --exclude "data/state/arm.json" --exclude "data/state/venues_arm.json" `
            --exclude "data/state/*.pid" --exclude "data/state/host/*" -C $repo @items @homeItems
        if ($LASTEXITCODE -ne 0) { Write-Error "tar failed"; exit 1 }
    } finally {
        if (Test-Path $stage) { Remove-Item -Recurse -Force $stage }
    }
    $count = (& $tar -tzf $File | Measure-Object).Count
    Write-Host "wrote ${File}: $count entries, $([math]::Round((Get-Item $File).Length / 1MB, 1)) MB (owner only)"
    exit 0
}

if (-not (Test-Path $File)) { Write-Error "cannot read $File"; exit 2 }
$status = Host-Status
if ($status -and $status.up) { Write-Error "refusing: the host is running here; stop it first (deploy\install.ps1 -Uninstall)"; exit 1 }
if (Test-Path $config) {
    Copy-Item $config "$config.bak-migrate" -Force
    Write-Host "kept the existing config as config\agentic.toml.bak-migrate"
}
& $tar -xzf $File -C $repo --exclude "_home/*" --exclude "data/state/arm.json" --exclude "data/state/venues_arm.json"
if ($LASTEXITCODE -ne 0) { Write-Error "tar failed"; exit 1 }
$entries = & $tar -tzf $File
if ($entries | Where-Object { $_ -like "_home/*" }) {
    & $tar -xzf $File -C $HomeDir --strip-components 1 "_home/*"
    if ($LASTEXITCODE -ne 0) { Write-Error "tar failed on the home files"; exit 1 }
}
foreach ($private in @("config\secrets.toml", ".env", "config\agentic.toml")) { Owner-Only (Join-Path $repo $private) }
Owner-Only (Join-Path $HomeDir ".config\agentic-trading")
Write-Host "imported $(($entries | Measure-Object).Count) entries into $repo and $HomeDir\.config\agentic-trading"
$linux = Select-String -Path $config -Pattern '"/(home|root|mnt)/' -ErrorAction SilentlyContinue
if ($linux) {
    [Console]::Error.WriteLine("warning: config\agentic.toml names Linux paths; make them relative to the checkout or Windows paths:")
    $linux | ForEach-Object { [Console]::Error.WriteLine("warning:   line $($_.LineNumber): $($_.Line.Trim())") }
}
Write-Host "next: deploy\install.ps1 -Enable, then delete $File"
