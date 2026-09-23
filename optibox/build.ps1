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

$appName = "Optibox"
$package = "optibox"
$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
# Caché propia de PyInstaller para que construcciones en paralelo no se pisen.
$env:PYINSTALLER_CONFIG_DIR = Join-Path $PSScriptRoot "build\pyinstaller-cache"

# Ejecuta un comando externo (python, pip, pytest, PyInstaller) sin que las
# líneas que ese programa escribe en stderr (progreso, INFO) se conviertan en
# errores de terminación de PowerShell cuando la invocación de este script
# corre con la salida redirigida (por ejemplo `.\build.ps1 *> log.txt`, común
# en CI o para guardar el registro). El resultado real se valida igual con
# $LASTEXITCODE, así que ningún fallo verdadero queda sin detectar.
function Invoke-Native {
    param(
        [Parameter(Mandatory)][string]$FilePath,
        [Parameter(Mandatory)][string[]]$Arguments,
        [Parameter(Mandatory)][string]$FailureMessage
    )
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & $FilePath @Arguments
    } finally {
        $ErrorActionPreference = $previous
    }
    if ($LASTEXITCODE -ne 0) { throw $FailureMessage }
}

if (-not (Test-Path $python)) {
    Write-Host "Creando el entorno virtual del proyecto"
    if (Get-Command py -ErrorAction SilentlyContinue) {
        Invoke-Native -FilePath "py" -Arguments @("-3.12", "-m", "venv", ".venv") -FailureMessage "No se pudo crear el entorno virtual."
    } else {
        Invoke-Native -FilePath "python" -Arguments @("-m", "venv", ".venv") -FailureMessage "No se pudo crear el entorno virtual."
    }
}

Write-Host "Instalando dependencias de ejecución, desarrollo y construcción"
Invoke-Native -FilePath $python `
    -Arguments @("-m", "pip", "install", "--upgrade", "--disable-pip-version-check", "--quiet", "-e", ".[dev,build]") `
    -FailureMessage "No se pudieron instalar las dependencias."

if (-not $SkipTests) {
    Write-Host "Ejecutando tests"
    Invoke-Native -FilePath $python -Arguments @("-m", "pytest") -FailureMessage "Los tests fallaron; no se genera el ejecutable."
}

Write-Host "Construyendo $appName.exe"

# Módulos de Qt que PySide6/matplotlib traen disponibles pero que esta
# aplicación no usa (solo importa QtCore, QtGui, QtWidgets y el backend Agg
# de matplotlib): excluirlos evita que PyInstaller empaquete sus DLL y
# plugins, lo que reduce bastante el tamaño del .exe sin quitar nada que se
# use en tiempo de ejecución.
$unusedQtModules = @(
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
    "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuick3D", "PySide6.QtQuickWidgets", "PySide6.QtQuickControls2",
    "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets",
    "PySide6.QtPdf", "PySide6.QtPdfWidgets",
    "PySide6.QtNetwork", "PySide6.QtNetworkAuth",
    "PySide6.QtBluetooth", "PySide6.QtNfc", "PySide6.QtPositioning", "PySide6.QtLocation",
    "PySide6.QtSensors", "PySide6.QtSerialPort", "PySide6.QtSerialBus",
    "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtDesigner", "PySide6.QtHelp",
    "PySide6.QtCharts", "PySide6.QtDataVisualization",
    "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.Qt3DInput", "PySide6.Qt3DLogic", "PySide6.Qt3DAnimation", "PySide6.Qt3DExtras",
    "PySide6.QtRemoteObjects", "PySide6.QtScxml", "PySide6.QtStateMachine",
    "PySide6.QtSpatialAudio", "PySide6.QtTextToSpeech", "PySide6.QtWebChannel", "PySide6.QtWebSockets"
)

# El proyecto solo usa el solver CP-SAT (ortools.sat.python.cp_model), que
# depende de ortools.util. Se recolecta solo esa parte en vez de todo el
# paquete: evita los avisos de "hidden import no encontrado" de los otros
# solvers de ortools (math_opt, linear_solver, etc.) sin dejar nada afuera.
$pyinstallerArgs = @(
    "--noconfirm", "--clean", "--onefile", "--windowed",
    "--name", $appName,
    "--paths", "src",
    "--collect-data", $package,
    "--collect-submodules", "ortools.sat",
    "--collect-submodules", "ortools.util",
    "--collect-data", "ortools.sat",
    "--collect-binaries", "ortools",
    "--exclude-module", "tkinter"
    # No se excluye "unittest": pyparsing (de matplotlib) lo importa siempre
    # y excluirlo rompe el arranque del ejecutable.
)
foreach ($module in $unusedQtModules) {
    $pyinstallerArgs += @("--exclude-module", $module)
}
$pyinstallerArgs += "src\$package\__main__.py"

Invoke-Native -FilePath $python -Arguments (@("-m", "PyInstaller") + $pyinstallerArgs) -FailureMessage "PyInstaller terminó con errores."

$exe = Join-Path $PSScriptRoot "dist\$appName.exe"
if (-not (Test-Path $exe)) { throw "PyInstaller no generó $exe." }
$sizeMb = [math]::Round((Get-Item $exe).Length / 1MB, 1)
Write-Host "Ejecutable generado: $exe ($sizeMb MB)"

if (-not $SkipAutotest) {
    Write-Host "Autoprueba del ejecutable"
    $process = Start-Process -FilePath $exe -ArgumentList "--autotest" -Wait -PassThru
    if ($process.ExitCode -ne 0) { throw "La autoprueba del ejecutable falló (código $($process.ExitCode))." }
    Write-Host "Autoprueba correcta: la ventana carga con la base sintética."
}
