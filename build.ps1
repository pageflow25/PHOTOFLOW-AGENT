<#
.SYNOPSIS
    Gera o executável do PhotoFlow Print Agent com PyInstaller.

.PARAMETER Clean
    Apaga dist/, build/ e arquivo .spec antes de compilar.
#>

param(
    [switch]$Clean
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if ($Clean) {
    foreach ($item in @("dist", "build", "PhotoFlow-Agent.spec")) {
        if (Test-Path $item) {
            Remove-Item -Recurse -Force $item
        }
    }
}

if (-not (Get-Command pyinstaller -ErrorAction SilentlyContinue)) {
    Write-Host "Instalando PyInstaller..." -ForegroundColor Yellow
    pip install pyinstaller
}

$pyArgs = @(
    "--onedir",
    "--windowed",
    "--name", "PhotoFlow-Agent",
    "--hidden-import", "win32print",
    "--hidden-import", "win32ui",
    "--hidden-import", "win32con",
    "--hidden-import", "pywintypes",
    "--hidden-import", "PIL._tkinter_finder",
    "--collect-submodules", "tkinter",
    "gui.py"
)

& pyinstaller @pyArgs
if ($LASTEXITCODE -ne 0) {
    throw "Falha no build (exit code $LASTEXITCODE)."
}

$distPath = Join-Path $PSScriptRoot "dist\PhotoFlow-Agent"
$envDest = Join-Path $distPath ".env"

if (-not (Test-Path $envDest)) {
    if (Test-Path ".env") {
        Copy-Item ".env" $envDest
    } elseif (Test-Path ".env.example") {
        Copy-Item ".env.example" $envDest
    }
}

Write-Host "Build concluido:" -ForegroundColor Green
Write-Host "  EXE:  $distPath\PhotoFlow-Agent.exe"
Write-Host "  ENV:  $envDest"
