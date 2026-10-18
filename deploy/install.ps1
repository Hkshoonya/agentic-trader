# Install this checkout as the system's Windows host: deploy/install.sh's counterpart.
#
#   powershell -ExecutionPolicy Bypass -File deploy\install.ps1             register the logon task
#   powershell -ExecutionPolicy Bypass -File deploy\install.ps1 -Enable     ...and start it now
#   powershell -ExecutionPolicy Bypass -File deploy\install.ps1 -KeepAwake  also: never sleep on mains (admin)
#   powershell -ExecutionPolicy Bypass -File deploy\install.ps1 -Uninstall  stop the host and remove the task
#
# One scheduled task, AgenticTrading-Host, runs `agentic-trading host run` at sign-in: the supervisor
# that starts the daemon, console and venues gateway and runs the swarm, upgrade and canary timers,
# as the systemd units do on Linux. Run one host at a time: two would share one broker login.
param([switch]$Enable, [switch]$KeepAwake, [switch]$Uninstall)
$ErrorActionPreference = "Stop"

$repo = Split-Path -Parent $PSScriptRoot
$task = "AgenticTrading-Host"
$pythonw = Join-Path $repo ".venv\Scripts\pythonw.exe"
$python = Join-Path $repo ".venv\Scripts\python.exe"
$config = "config\agentic.toml"

if ($Uninstall) {
    if (Test-Path $python) { & $python -m agentic_trading host shutdown --config (Join-Path $repo $config) | Out-Host }
    Unregister-ScheduledTask -TaskName $task -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "removed task $task"
    exit 0
}

if (-not (Test-Path $pythonw)) { Write-Error "no $pythonw : create the venv first (py -3 -m venv .venv; .venv\Scripts\pip install -e "".[dev,venues]"")"; exit 2 }
if (-not (Test-Path (Join-Path $repo $config))) { Write-Error "no $config : import or create it first"; exit 2 }

# The supervisor holds its own lock, so a second start (a re-run of this script, a manual `host run`)
# exits at once instead of doubling every service.
$action = New-ScheduledTaskAction -Execute $pythonw -Argument "-m agentic_trading host run --config $config" `
    -WorkingDirectory $repo
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $task -Action $action -Trigger $trigger -Settings $settings -Force `
    -Description "Agentic Trading: daemon, console, venues, swarm, upgrade and canary for $repo" | Out-Null
Write-Host "registered task $task for $repo (starts at sign-in, restarts within a minute if it dies)"

$branch = (& git -C $repo symbolic-ref --short -q HEAD 2>$null)
if ($branch -ne "live") { [Console]::Error.WriteLine("warning: checkout is on '$branch'; the self-upgrader ships to 'live'") }
& $python -c "import agentic_trading.host" 2>$null
if ($LASTEXITCODE -ne 0) { [Console]::Error.WriteLine("warning: this venv cannot import agentic_trading.host: reinstall with pip install -e .") }

if ($KeepAwake) {
    # deploy/windows-host.ps1's power settings: on mains, never sleep or hibernate, and the lid does nothing.
    powercfg /change standby-timeout-ac 0
    powercfg /change hibernate-timeout-ac 0
    powercfg /setacvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 0
    powercfg /setactive SCHEME_CURRENT
    Write-Host "power: on mains, never sleep or hibernate; closing the lid does nothing"
}

if ($Enable) {
    Start-ScheduledTask -TaskName $task
    Write-Host "started $task; check it with: .venv\Scripts\agentic-trading host status --config $config"
} else {
    Write-Host "start it with: deploy\install.ps1 -Enable (or sign out and in)"
}
