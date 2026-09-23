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

$appName = "MonitorIndicadores"
$package = "kpi_monitor"
$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
# Caché propia de PyInstaller para que construcciones en paralelo no se pisen.
$env:PYINSTALLER_CONFIG_DIR = Join-Path $PSScriptRoot "build\pyinstaller-cache"

if (-not (Test-Path $python)) {
    Write-Host "Creando el entorno virtual del proyecto"
    if (Get-Command py -ErrorAction SilentlyContinue) {
        py -3.12 -m venv .venv
    } else {
        Write-Host "No se encontró el lanzador 'py -3.12'; se usa 'python' (verifique que sea la versión 3.12)."
        python -m venv .venv
    }
}

Write-Host "Instalando dependencias de ejecución, desarrollo y construcción"
& $python -m pip install --upgrade --disable-pip-version-check --quiet -e ".[dev,build]"
if ($LASTEXITCODE -ne 0) { throw "No se pudieron instalar las dependencias." }

if (-not $SkipTests) {
    Write-Host "Ejecutando tests"
    & $python -m pytest
    if ($LASTEXITCODE -ne 0) { throw "Los tests fallaron; no se genera el ejecutable." }
}

Write-Host "Construyendo $appName.exe"
# Módulos de PySide6 y Pillow que el proyecto no usa (sin QML, multimedia, red,
# PDF nativo de Qt, dispositivos ni motores 3D/web): se excluyen para reducir
# el tamaño del .exe. matplotlib usa PdfPages (no Qt6Pdf) para el reporte PDF.
$unusedQtModules = @(
    "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuickWidgets", "PySide6.QtQuick3D",
    "PySide6.QtQuickControls2", "PySide6.QtNetwork", "PySide6.QtPdf", "PySide6.QtPdfWidgets",
    "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets", "PySide6.QtBluetooth", "PySide6.QtNfc",
    "PySide6.QtPositioning", "PySide6.QtSensors", "PySide6.QtSerialPort", "PySide6.QtSerialBus",
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
    "PySide6.QtWebView", "PySide6.QtWebChannel", "PySide6.QtWebSockets", "PySide6.QtDesigner",
    "PySide6.QtHelp", "PySide6.QtTest", "PySide6.QtVirtualKeyboard", "PySide6.QtRemoteObjects",
    "PySide6.QtScxml", "PySide6.QtStateMachine", "PySide6.QtDBus", "PySide6.QtCharts",
    "PySide6.QtDataVisualization", "PySide6.QtGraphs", "PySide6.QtSpatialAudio",
    "PySide6.QtLocation", "PySide6.QtTextToSpeech", "PySide6.QtOpenGLWidgets",
    "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.Qt3DInput", "PySide6.Qt3DLogic",
    "PySide6.Qt3DAnimation", "PySide6.Qt3DExtras"
)
$unusedPillowPlugins = @(
    "PIL.AvifImagePlugin", "PIL.WebPImagePlugin"
)
$excludeArgs = foreach ($module in ($unusedQtModules + $unusedPillowPlugins)) {
    "--exclude-module", $module
}
$pyinstallerArgs = @(
    "--noconfirm", "--clean", "--onefile", "--windowed",
    "--name", $appName,
    "--paths", "src",
    "--collect-data", $package,
    "--collect-data", "matplotlib",
    "--exclude-module", "tkinter"
) + $excludeArgs + @(
    "src\$package\__main__.py"
)
& $python -m PyInstaller @pyinstallerArgs
if ($LASTEXITCODE -ne 0) { throw "PyInstaller terminó con errores." }

$exe = Join-Path $PSScriptRoot "dist\$appName.exe"
$sizeMb = [math]::Round((Get-Item $exe).Length / 1MB, 1)
Write-Host "Ejecutable generado: $exe ($sizeMb MB)"

if (-not $SkipAutotest) {
    Write-Host "Autoprueba del ejecutable"
    $process = Start-Process -FilePath $exe -ArgumentList "--autotest" -Wait -PassThru
    if ($process.ExitCode -ne 0) { throw "La autoprueba del ejecutable falló (código $($process.ExitCode))." }
    Write-Host "Autoprueba correcta: la ventana carga con la base sintética."
}
