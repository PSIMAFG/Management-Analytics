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

$appName = "LectorBoletas"
$package = "receipt_reader"
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

Write-Host "Instalando dependencias de ejecución, desarrollo y construcción"
& $python -m pip install --upgrade --disable-pip-version-check --quiet -e ".[dev,build]"
if ($LASTEXITCODE -ne 0) { throw "No se pudieron instalar las dependencias." }

if (-not $SkipTests) {
    Write-Host "Ejecutando tests"
    & $python -m pytest
    if ($LASTEXITCODE -ne 0) { throw "Los tests fallaron; no se genera el ejecutable." }
}

Write-Host "Construyendo $appName.exe"
# El OCR funciona sin conexión: los modelos ONNX y la configuración de RapidOCR van dentro
# del ejecutable (--collect-data rapidocr). El motor de inferencia se importa en forma
# diferida, por eso se declara explícitamente; los motores opcionales se excluyen.
# PIL.AvifImagePlugin se excluye porque la aplicación no admite ese formato (solo
# pdf/png/jpg/jpeg/tif/tiff/bmp) y arrastra una biblioteca nativa de varios MB.
$pyinstallerArgs = @(
    "--noconfirm", "--clean", "--onefile", "--windowed",
    "--name", $appName,
    "--paths", "src",
    "--collect-data", $package,
    "--collect-data", "rapidocr",
    "--hidden-import", "rapidocr.inference_engine.onnxruntime",
    "--collect-binaries", "onnxruntime",
    "--collect-all", "pypdfium2",
    "--collect-all", "pypdfium2_raw",
    "--collect-data", "reportlab",
    "--exclude-module", "tkinter",
    "--exclude-module", "torch",
    "--exclude-module", "openvino",
    "--exclude-module", "paddle",
    "--exclude-module", "tensorrt",
    "--exclude-module", "PIL.AvifImagePlugin",
    "src\$package\__main__.py"
)

# Generamos primero el .spec (mismos argumentos, sin construir todavía) para poder filtrar del
# binario final una DLL de video (ffmpeg) que el hook de OpenCV agrega siempre aunque la
# aplicación nunca abre video ni cámara: ahorra ~13 MB sin afectar el OCR ni la lectura de
# PDF/imágenes. El resto de los argumentos (hidden-import, collect-*, exclude-module) se
# mantienen idénticos, así que el .spec siempre refleja lo declarado arriba.
$specPath = Join-Path $PSScriptRoot "$appName.spec"
$specArgs = $pyinstallerArgs | Where-Object { $_ -notin @("--noconfirm", "--clean") }
& $python -m PyInstaller.utils.cliutils.makespec @specArgs
if ($LASTEXITCODE -ne 0) { throw "No se pudo generar el archivo .spec." }

$specContent = Get-Content -Path $specPath -Raw
$ffmpegFilter = "a.binaries = [b for b in a.binaries if 'opencv_videoio_ffmpeg' not in b[0].lower() and 'opencv_videoio_ffmpeg' not in b[1].lower()]`r`n`r`n"
$patchedSpec = $specContent -replace "(?m)^pyz = PYZ\(a\.pure\)", ($ffmpegFilter + "pyz = PYZ(a.pure)")
if ($patchedSpec -eq $specContent) { throw "No se encontró el punto de inserción esperado en el .spec generado." }
Set-Content -Path $specPath -Value $patchedSpec -NoNewline -Encoding UTF8

& $python -m PyInstaller --noconfirm --clean $specPath
if ($LASTEXITCODE -ne 0) { throw "PyInstaller terminó con errores." }

$exe = Join-Path $PSScriptRoot "dist\$appName.exe"
$sizeMb = [math]::Round((Get-Item $exe).Length / 1MB, 1)
Write-Host "Ejecutable generado: $exe ($sizeMb MB)"

if (-not $SkipAutotest) {
    Write-Host "Autoprueba del ejecutable"
    $process = Start-Process -FilePath $exe -ArgumentList "--autotest" -Wait -PassThru
    if ($process.ExitCode -ne 0) { throw "La autoprueba del ejecutable falló (código $($process.ExitCode))." }
    Write-Host "Autoprueba correcta: la ventana carga con la base sintética y el OCR lee una muestra."
}
