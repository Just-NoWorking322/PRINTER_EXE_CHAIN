# Build NurMarketKassa.exe (Flet pack / PyInstaller).
# Run from project folder:
#   powershell -ExecutionPolicy Bypass -File .\build_exe.ps1

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$venvRoot = $null
foreach ($d in @(".venv", "venv")) {
    $p = Join-Path $PSScriptRoot $d
    if (Test-Path (Join-Path $p "Scripts\flet.exe")) {
        $venvRoot = $p
        break
    }
}
if (-not $venvRoot) {
    Write-Host "No venv with flet. Create: python -m venv .venv && .\.venv\Scripts\pip install -r requirements.txt"
    exit 1
}

$flet = Join-Path $venvRoot "Scripts\flet.exe"
$dist = Join-Path $PSScriptRoot "dist"
$name = "NurMarketKassa"

$capBundle = Join-Path $PSScriptRoot "bundle_escpos\capabilities.json"
$capSite = Join-Path $venvRoot "Lib\site-packages\escpos\capabilities.json"
if (Test-Path $capBundle) {
    $capJson = $capBundle
} elseif (Test-Path $capSite) {
    $capJson = $capSite
} else {
    Write-Host "Missing escpos/capabilities.json (bundle_escpos or venv site-packages). pip install python-escpos"
    exit 1
}

Get-Process -Name $name -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
$exeOut = Join-Path $dist "$name.exe"
if (Test-Path $exeOut) {
    Remove-Item -LiteralPath $exeOut -Force -ErrorAction SilentlyContinue
}

# Windows: source;dest -> escpos/ inside bundle
$addData = "$capJson;escpos"

& $flet pack main.py `
    -n $name `
    -y `
    -v `
    --distpath $dist `
    --product-name "Nur Market Kassa" `
    --file-description "Kassa POS Nur Market" `
    --add-data $addData `
    --hidden-import escpos.printer `
    --hidden-import escpos.capabilities `
    --hidden-import serial `
    --hidden-import usb.core `
    --hidden-import usb.util

Write-Host ""
Write-Host "Done:" $exeOut
