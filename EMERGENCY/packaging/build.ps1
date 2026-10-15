# Reproducible build. Run from anywhere:
#   powershell -ExecutionPolicy Bypass -File packaging\build.ps1                 -> dist\ESRMS_Desktop.exe
#   powershell -ExecutionPolicy Bypass -File packaging\build.ps1 -Installer      -> also dist\ESRMS-Setup-<version>.exe
param([switch]$Installer, [string]$Version = "1.0.1")
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$py = Join-Path $root ".venv\Scripts\python.exe"
& $py -m pip install -r requirements.txt -r packaging\requirements-desktop.txt
& $py packaging\make_icon.py
& $py -m PyInstaller --noconfirm --clean ESRMS_Desktop.spec
if (-not (Test-Path dist\ESRMS_Desktop.exe)) { throw "Build failed: dist\ESRMS_Desktop.exe missing" }
if ($Installer) {
    $iscc = @("$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe", "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
              "$env:ProgramFiles\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $iscc) { throw "Inno Setup 6 not found. Install it: winget install JRSoftware.InnoSetup" }
    # Compiled outside dist\ and moved in: antivirus scanning of dist\ESRMS_Desktop.exe can lock dist\ (error 110).
    New-Item -ItemType Directory -Force build\installer | Out-Null
    & $iscc "/DAppVersion=$Version" /Obuild\installer packaging\ESRMS-Setup.iss
    if ($LASTEXITCODE -ne 0) { throw "Installer build failed" }
    Copy-Item "build\installer\ESRMS-Setup-$Version.exe" dist\ -Force
}
Get-ChildItem dist\*.exe | Select-Object Name, Length, LastWriteTime
