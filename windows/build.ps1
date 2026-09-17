# Build the Windows app: one folder, one double-clickable executable.
#
#   powershell -ExecutionPolicy Bypass -File windows\build.ps1
#
# Produces:
#   dist\AgenticTrader\AgenticTrader.exe      <- the window people open
#   dist\AgenticTrader\agentic-trading.exe    <- the same CLI as the source tree
#   dist\AgenticTrader\payload\...            <- config template + 11 years of bars
#   dist\AgenticTrader-windows-x64.zip        <- the thing you copy to another PC

param(
    [string]$Python = "py",
    [switch]$SkipTests,
    [switch]$NoZip
)

$ErrorActionPreference = "Stop"
$repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $repo

Write-Host "repository: $repo"

# 1. A build virtualenv, so the machine's own Python stays untouched.
$venv = Join-Path $repo ".venv-build"
if (-not (Test-Path $venv)) {
    Write-Host "creating build environment"
    & $Python -m venv $venv
}
$py = Join-Path $venv "Scripts\python.exe"
if (-not (Test-Path $py)) {
    throw "no interpreter at $py — is Python 3.11+ installed?"
}

& $py -m pip install --upgrade pip wheel | Out-Null
Write-Host "installing the project and PyInstaller"
& $py -m pip install ".[dev]" pyinstaller

# 2. Tests first: never ship a binary the suite would not pass.
if (-not $SkipTests) {
    Write-Host "running the test suite"
    & $py -m pytest tests -q
    if ($LASTEXITCODE -ne 0) { throw "tests failed; refusing to build" }
}

# 3. Build.
Write-Host "building"
if (Test-Path (Join-Path $repo "build")) { Remove-Item -Recurse -Force (Join-Path $repo "build") }
& $py -m PyInstaller --noconfirm --clean windows\AgenticTrader.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

$app = Join-Path $repo "dist\AgenticTrader"
if (-not (Test-Path (Join-Path $app "AgenticTrader.exe"))) { throw "launcher was not produced" }
if (-not (Test-Path (Join-Path $app "agentic-trading.exe"))) { throw "CLI was not produced" }

# 4. Smoke test the frozen build the way a user would: seed a workspace and read
#    the state back. This catches a missing data file or hidden import that only
#    shows up after freezing.
$smoke = Join-Path $env:TEMP ("agentic-smoke-" + [guid]::NewGuid().ToString("N"))
Write-Host "smoke test: $smoke"
& (Join-Path $app "AgenticTrader.exe") --selftest --workspace $smoke
if ($LASTEXITCODE -ne 0) { throw "frozen launcher failed its self-test" }
& (Join-Path $app "agentic-trading.exe") --help | Select-Object -First 3
if ($LASTEXITCODE -ne 0) { throw "frozen CLI did not start" }

# 5. Zip it for copying to another computer.
if (-not $NoZip) {
    $zip = Join-Path $repo "dist\AgenticTrader-windows-x64.zip"
    if (Test-Path $zip) { Remove-Item $zip }
    Write-Host "packaging $zip"
    Compress-Archive -Path (Join-Path $app "*") -DestinationPath $zip -CompressionLevel Optimal
    $size = [math]::Round((Get-Item $zip).Length / 1MB, 1)
    Write-Host "done: $zip ($size MB)"
}

Write-Host ""
Write-Host "Next: copy dist\AgenticTrader-windows-x64.zip to the other PC, unzip"
Write-Host "anywhere, and run AgenticTrader.exe. It starts in shadow mode."
