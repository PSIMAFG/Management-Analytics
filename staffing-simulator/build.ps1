<#
.SYNOPSIS
    Genera el ejecutable autocontenido para Windows con PyInstaller.

.DESCRIPTION
    Prepara el entorno virtual del proyecto, instala las dependencias de
    ejecución, desarrollo y construcción, corre los tests y construye un
    único .exe sin consola en dist\. Al final abre el ejecutable en modo
    de autoprueba para confirmar que la ventana carga con la base sintética.

.PARAMETER SkipTests
    Omite la ejecución de pytest antes de construir.

.PARAMETER SkipAutotest
    Omite la autoprueba del ejecutable generado.
#>
[CmdletBinding()]
param(
    [switch]$SkipTests,
    [switch]$SkipAutotest
)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot
# Este archivo se guarda en UTF-8 con BOM: sin él, Windows PowerShell 5.1 lo
# lee con la página de códigos del sistema y los mensajes pierden las tildes.

$appName = "SimuladorDotacion"
$package = "staffing_simulator"
$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
# Caché propia de PyInstaller para que construcciones en paralelo no se pisen.
$env:PYINSTALLER_CONFIG_DIR = Join-Path $PSScriptRoot "build\pyinstaller-cache"

if (-not (Test-Path $python)) {
    Write-Host "Creando el entorno virtual del proyecto"
    if (Get-Command py -ErrorAction SilentlyContinue) {
        py -3 -m venv .venv
    } else {
        python -m venv .venv
    }
}

# Los ejecutables nativos (pip, pytest, PyInstaller) escriben avisos e
# información de progreso en stderr; con $ErrorActionPreference = "Stop"
# PowerShell 5.1 convierte esas líneas en errores terminantes aunque el
# proceso termine con código 0. Se baja a "Continue" solo para esas
# llamadas y se valida el resultado real con $LASTEXITCODE.
$previousEap = $ErrorActionPreference

Write-Host "Instalando dependencias de ejecución, desarrollo y construcción"
$ErrorActionPreference = "Continue"
& $python -m pip install --upgrade --disable-pip-version-check --quiet -e ".[dev,build]"
$pipExitCode = $LASTEXITCODE
$ErrorActionPreference = $previousEap
if ($pipExitCode -ne 0) { throw "No se pudieron instalar las dependencias." }

if (-not $SkipTests) {
    Write-Host "Ejecutando tests"
    $ErrorActionPreference = "Continue"
    & $python -m pytest
    $pytestExitCode = $LASTEXITCODE
    $ErrorActionPreference = $previousEap
    if ($pytestExitCode -ne 0) { throw "Los tests fallaron; no se genera el ejecutable." }
}

Write-Host "Construyendo $appName.exe"
$pyinstallerArgs = @(
    "--noconfirm", "--clean", "--onefile", "--windowed",
    "--name", $appName,
    "--paths", "src",
    "--collect-data", $package,
    "--collect-data", "matplotlib",
    "--copy-metadata", "matplotlib",
    "--hidden-import", "openpyxl.cell._writer",
    "--exclude-module", "tkinter",
    "--exclude-module", "PySide6.Qt3DAnimation",
    "--exclude-module", "PySide6.Qt3DCore",
    "--exclude-module", "PySide6.Qt3DExtras",
    "--exclude-module", "PySide6.Qt3DInput",
    "--exclude-module", "PySide6.Qt3DLogic",
    "--exclude-module", "PySide6.Qt3DRender",
    "--exclude-module", "PySide6.QtBluetooth",
    "--exclude-module", "PySide6.QtCharts",
    "--exclude-module", "PySide6.QtDataVisualization",
    "--exclude-module", "PySide6.QtDesigner",
    "--exclude-module", "PySide6.QtMultimedia",
    "--exclude-module", "PySide6.QtMultimediaWidgets",
    "--exclude-module", "PySide6.QtNetwork",
    "--exclude-module", "PySide6.QtNfc",
    "--exclude-module", "PySide6.QtOpenGL",
    "--exclude-module", "PySide6.QtOpenGLWidgets",
    "--exclude-module", "PySide6.QtPdf",
    "--exclude-module", "PySide6.QtPdfWidgets",
    "--exclude-module", "PySide6.QtPositioning",
    "--exclude-module", "PySide6.QtQml",
    "--exclude-module", "PySide6.QtQuick",
    "--exclude-module", "PySide6.QtQuick3D",
    "--exclude-module", "PySide6.QtQuickWidgets",
    "--exclude-module", "PySide6.QtRemoteObjects",
    "--exclude-module", "PySide6.QtSensors",
    "--exclude-module", "PySide6.QtSerialPort",
    "--exclude-module", "PySide6.QtSpatialAudio",
    "--exclude-module", "PySide6.QtSql",
    "--exclude-module", "PySide6.QtSvgWidgets",
    "--exclude-module", "PySide6.QtTest",
    "--exclude-module", "PySide6.QtWebChannel",
    "--exclude-module", "PySide6.QtWebEngineCore",
    "--exclude-module", "PySide6.QtWebEngineQuick",
    "--exclude-module", "PySide6.QtWebEngineWidgets",
    "--exclude-module", "PySide6.QtWebSockets",
    "--exclude-module", "PySide6.QtXml",
    "src\$package\__main__.py"
)
$ErrorActionPreference = "Continue"
& $python -m PyInstaller @pyinstallerArgs
$pyinstallerExitCode = $LASTEXITCODE
$ErrorActionPreference = $previousEap
if ($pyinstallerExitCode -ne 0) { throw "PyInstaller terminó con errores." }

$exe = Join-Path $PSScriptRoot "dist\$appName.exe"
$sizeMb = [math]::Round((Get-Item $exe).Length / 1MB, 1)
Write-Host "Ejecutable generado: $exe ($sizeMb MB)"

if (-not $SkipAutotest) {
    Write-Host "Autoprueba del ejecutable"
    $process = Start-Process -FilePath $exe -ArgumentList "--autotest" -Wait -PassThru
    if ($process.ExitCode -ne 0) { throw "La autoprueba del ejecutable falló (código $($process.ExitCode))." }
    Write-Host "Autoprueba correcta: la ventana carga con la base sintética."
}
