# Builds the Windows desktop app into dist\MobilePOS\ and a zip to share.
# Run from PowerShell:  powershell -ExecutionPolicy Bypass -File build_windows.ps1
#
# Data stays compatible: the app still keeps its data file in the same
# per-user folder (%APPDATA%\MobilePOS\Mobile POS Desktop\pos.db), so the
# built app opens the same data as `python -m pos_desktop`.

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

function Run($exe, [string[]]$argList) {
    & $exe @argList
    if ($LASTEXITCODE -ne 0) { throw "Failed: $exe $argList" }
}

if (-not (Test-Path .venv\Scripts\python.exe)) { Run py @("-3", "-m", "venv", ".venv") }
$py = (Resolve-Path .venv\Scripts\python.exe).Path

Run $py @("-m", "pip", "install", "--quiet", "--upgrade", "pip")
Run $py @("-m", "pip", "install", "--quiet", "-e", ".[dev]", "pyinstaller", "pillow")

Write-Host "Running tests..."
Run $py @("-m", "pytest", "-q")

$version = (Select-String -Path pyproject.toml -Pattern '^version = "(.+)"').Matches[0].Groups[1].Value

Write-Host "Building MobilePOS $version..."
Run $py @("-m", "PyInstaller", "--noconfirm", "--clean", "--windowed",
    "--name", "MobilePOS",
    "--icon", "pos_desktop\resources\icon.png",
    "--add-data", "pos_desktop\resources;pos_desktop\resources",
    "packaging\launcher.py")

$zip = "dist\MobilePOS-$version-windows.zip"
Compress-Archive -Path dist\MobilePOS -DestinationPath $zip -Force
Write-Host "Done: dist\MobilePOS\MobilePOS.exe and $zip"
