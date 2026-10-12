# Keep a Windows laptop up as the system's 24/7 host. The system itself runs in WSL2 (see
# deploy/LAPTOP.md); this only stops Windows from putting it to sleep. Run once, in an
# administrator PowerShell:
#
#   powershell -ExecutionPolicy Bypass -File deploy\windows-host.ps1 [-Distro Ubuntu-24.04]
#
# On mains power it turns off sleep and hibernate and makes closing the lid do nothing. It adds a
# task that, at your sign-in, starts the distro and keeps it running with a hidden `sleep infinity`:
# WSL stops a distro when nothing holds it open, and the system's services stop with it.
param([string]$Distro = "Ubuntu-24.04")
$ErrorActionPreference = "Stop"

powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
powercfg /setacvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 0
powercfg /setactive SCHEME_CURRENT
Write-Host "power: on mains, never sleep or hibernate; closing the lid does nothing"

$wsl = Join-Path $env:WINDIR "System32\wsl.exe"
$action = New-ScheduledTaskAction -Execute "conhost.exe" -Argument "--headless `"$wsl`" -d $Distro --exec sleep infinity"
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName "AgenticTrading-WSL" -Action $action -Trigger $trigger `
    -Settings $settings -Description "Keeps the $Distro distro (and the trading system's services) running" -Force | Out-Null
Start-ScheduledTask -TaskName "AgenticTrading-WSL"
Write-Host "task AgenticTrading-WSL: starts $Distro at sign-in and keeps it running (started now)"
Write-Host "after any Windows restart, sign in once (a locked screen is fine) so the task starts"
