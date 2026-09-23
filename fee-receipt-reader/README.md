# Lector de boletas de honorarios

Aplicación de escritorio (Windows, PySide6) que procesa en lote boletas de honorarios
electrónicas en PDF o imagen, extrae sus datos con lectura de texto nativo u OCR, valida
montos y retenciones contra la ley y los parámetros del usuario, ofrece una cola de
revisión manual para lo que no cuadra, y genera informes en Excel por programa y período.

## Problema que resuelve

Una unidad administrativa recibe cada mes decenas de boletas de honorarios de
prestadores externos, en PDF (a veces escaneado) o imagen, muchas veces agrupadas en
paquetes de varias páginas. Debe registrarlas, comprobar que el bruto, la retención y el
líquido cuadran con la tasa legal del año, imputar cada boleta al programa que financia el
pago, y producir informes mensuales por programa. Hacerlo a mano es lento y propenso a
errores (folios mal copiados, retenciones mal calculadas, boletas duplicadas).

## Funcionalidades

- Procesamiento por lote de una carpeta de entrada, con la convención opcional
  `AAAA-MM/NNN Nombre del programa/archivo` (período de pago y código de programa).
  Acepta PDF nativo, PDF escaneado, imágenes (PNG, JPG, TIFF, BMP) y paquetes
  multipágina (una boleta más anexos).
- Detección de duplicados por hash del archivo y por (RUT emisor, folio).
- Extracción determinista de RUT, folio, fecha, período de servicio, montos, tasa
  impresa, programa, horas, tipo de jornada y decreto, con texto nativo o, si no hay,
  OCR (RapidOCR) reconstruyendo líneas por coordenadas.
- Validación con incidencias tipadas (bloqueantes o solo advertencias) y estados
  persistentes: pendiente, aprobada, corregida, descartada o error de lectura.
- Cola de revisión manual con vista previa de la página exacta, formulario con
  validadores, sugerencias (nombre confirmado del prestador, programa habitual,
  bruto desde líquido más retención) y auditoría de cada corrección.
- Catálogo de programas editable desde la interfaz (crear, editar, desactivar) con
  alias de texto por prioridad, importación y exportación en Excel.
- Parámetros configurables: organización receptora, ventana de fechas, tolerancias,
  confianza mínima del OCR; tasas de retención por año y valores hora de referencia
  por programa, también editables e importables.
- Informes en Excel con hojas Base, Resumen, Pendientes, una hoja por programa y,
  opcionalmente, un informe por prestador.
- Generador de datos sintéticos con semilla fija para tener algo que revisar desde
  el primer arranque, sin depender de boletas reales.

## Arquitectura

Capas con una sola dirección de dependencia: `domain` no importa nada de las otras
capas (es lógica pura, sin SQLite ni Qt); `data` no importa `ui` ni `services`; `ui`
solo habla con `services` y con los modelos de `domain`.

    src/receipt_reader/
      app.py              punto de entrada: arranca la aplicación o corre --autotest / --capturas
      paths.py            carpeta de datos (variable RECEIPT_READER_DATA_DIR o junto al .exe)
      domain/             reglas de negocio puras
      data/                SQLite, generador sintético, importación y exportación Excel
      services/            casos de uso que orquestan domain y data; la ui solo habla con services
      ui/                   PySide6 + matplotlib embebido

### Módulos principales

| Módulo | Responsabilidad |
| --- | --- |
| `domain/models.py` | Enumeraciones y registros inmutables (`ReceiptData`, `Settings`, `Program`...) |
| `domain/text.py` | Normalización de texto (guiones Unicode, mayúsculas sin tilde) previa a la extracción |
| `domain/layout.py` | Reconstrucción de líneas desde las cajas de OCR por coordenadas |
| `domain/classify.py` | Si una página es una boleta de honorarios (tolerante a errores típicos del OCR) |
| `domain/extraction.py` | Parsers deterministas: RUT, folio, fecha, montos, tasa, decreto, glosa |
| `domain/money.py`, `domain/rut.py`, `domain/dates.py` | Formato chileno de montos, RUT con dígito verificador, períodos `aaaa-mm` |
| `domain/location.py` | Período de pago y código de programa desde la ruta y el nombre del archivo |
| `domain/programs.py` | Resolución del programa (carpeta, luego alias en la glosa por prioridad) |
| `domain/retention.py` | Tasa legal por año y coherencia bruto, retención y líquido |
| `domain/drafts.py` | Arma la boleta a validar a partir de lo extraído y la ubicación del archivo |
| `domain/validation.py` | Incidencias tipadas y derivación del estado persistente |
| `domain/forms.py` | Validadores del formulario de revisión y de los parámetros |
| `domain/suggestions.py` | Sugerencias que se muestran en la revisión (nunca se aplican solas) |
| `domain/reporting.py`, `domain/records.py` | Estructuras de filtro e informe; registros que entrega `data` |
| `data/db.py`, `data/schema.sql` | Conexión SQLite y esquema versionado con migraciones |
| `data/receipt_repo.py`, `data/catalog_repo.py` | Repositorios de boletas y de catálogos/parámetros |
| `data/documents.py`, `data/ocr.py` | Lectura de PDF/imagen y motores de OCR (real y sintético para pruebas) |
| `data/excel_import.py`, `data/excel_export.py` | Importación de catálogos y exportación de informes |
| `data/synthetic.py`, `data/synthetic_render.py` | Generador reproducible de boletas ficticias y su render como PDF/imagen |
| `services/processing.py` | Procesa una carpeta: lee, extrae, valida y registra cada página |
| `services/review.py` | Cola de revisión, corrección auditada, aprobación y descarte |
| `services/catalog.py` | Parámetros y catálogos (programas, alias, tasas, prestadores) |
| `services/reports.py` | Totales, series para gráficos y exportación de informes |
| `services/context.py` | Catálogos vigentes, revalidación y reloj compartidos por los servicios |
| `services/app_services.py` | Punto de acceso único de la interfaz a los casos de uso |
| `ui/main_window.py` | Ventana principal: franja de totales, pestañas, filtros, procesamiento |
| `ui/review_panel.py`, `ui/receipt_form.py`, `ui/preview.py` | Cola de revisión, formulario y vista previa con zoom |
| `ui/settings_dialog.py` | Parámetros y catálogos (programas, tasas, valores hora, prestadores) |
| `ui/charts.py` | Preparación de datos y dibujo de los gráficos con matplotlib |

### Flujo de datos

1. `services/processing.py` recorre la carpeta de entrada, calcula el hash de cada
   archivo, obtiene el texto de cada página (nativo con `data/documents.py` u OCR con
   `data/ocr.py`) y lo clasifica con `domain/classify.py`.
2. Cada página de boleta se extrae con `domain/extraction.py`, se arma un borrador con
   `domain/drafts.py` (datos, trazas por campo con su origen y confianza, pistas de la
   ubicación del archivo) y se valida con `domain/validation.py` contra los catálogos
   vigentes (`services/context.py`).
3. `data/receipt_repo.py` guarda la boleta, sus incidencias y las trazas de cada campo
   en una transacción por archivo.
4. La revisión manual (`services/review.py`) vuelve a evaluar la boleta al corregir o
   aprobar; aprobar guarda los códigos de incidencia que el usuario aceptó, así que una
   incidencia aceptable que aparezca después (por ejemplo, al cambiar un parámetro)
   devuelve la boleta a la cola sin perder lo que ya se revisó.
5. `services/reports.py` y `data/excel_export.py` calculan los totales en SQL y arman
   el libro de Excel con formato chileno.

### Modelo de datos

Tablas principales en SQLite (esquema en `data/schema.sql`, versión en
`PRAGMA user_version`):

- `setting`: parámetros de validación y de la organización receptora (clave-valor).
- `program` y `program_alias`: catálogo de programas (código de carpeta de 3 dígitos,
  nombre, si está activo) y sus alias de texto con prioridad.
- `retention_rate`: tasa legal de retención por año.
- `reference_rate`: rango de valor hora de referencia por programa y año.
- `provider`: nombre canónico confirmado de un prestador, por RUT.
- `batch` y `source_file`: cada lote procesado y cada archivo (hash, páginas, si es
  duplicado de otro ya registrado).
- `receipt`: una fila por página de boleta, con todos sus campos, el estado y las
  referencias al programa de la carpeta, del texto y el finalmente asignado.
- `receipt_issue`: incidencias de cada boleta (código, campo, severidad, mensaje).
- `receipt_acceptance`: códigos de incidencia que el usuario aceptó al aprobar (solo
  esos se dan por revisados; ver más abajo).
- `field_extraction`: valor, confianza y origen de cada campo (texto nativo, OCR,
  carpeta, nombre de archivo, deducido o editado por el usuario).
- `correction`: auditoría de cada cambio manual (campo, valor antes y después).

## Reglas de negocio y decisiones de diseño

**Validación y aprobación.** Cada incidencia es bloqueante o solo una advertencia.
Algunas bloqueantes son "aceptables" (monto fuera de rango, mes de emisión distinto al
de la carpeta, programa de la carpeta distinto del de la glosa, un dato clave leído por
OCR con baja confianza, folio del documento distinto al que sugiere el nombre del
archivo, falta el período de servicio): el botón Aprobar las da por revisadas y guarda
sus códigos. Si después cambia algo (por ejemplo, el usuario ajusta un parámetro) y
aparece una incidencia aceptable que no estaba entre las aceptadas, la boleta vuelve a
la cola de revisión; las que ya se aceptaron no se vuelven a preguntar. Las incidencias
no aceptables (faltan datos, los montos no cuadran, un duplicado) exigen corregir o
descartar antes de poder aprobar.

**Ventana de fechas.** La fecha de emisión se compara con el mes de la carpeta de pago
cuando la hay (desde `window_months_before` meses antes hasta `window_months_after`
meses después, configurable); si el archivo no está en una carpeta de mes reconocible,
se usa una ventana fija `date_window_start`–`date_window_end`, también configurable
desde la interfaz, con un valor por defecto amplio (desde 2018 hasta 60 días después de
hoy) para no bloquear boletas antiguas ni las de los próximos meses. En ambos casos,
una fecha fuera de la ventana es solo una advertencia: nunca impide aprobar la boleta,
porque una fecha atrasada legítima no debería quedar atascada esperando que alguien
edite los parámetros.

**Tripleta de montos.** Con tasa legal $t$ del año de emisión, la retención esperada es

$$\text{retención} = \operatorname{redondeo}(\text{bruto} \times t)$$

con una tolerancia configurable (1 peso por defecto), y el líquido debe cumplir
$\text{líquido} = \text{bruto} - \text{retención}$. El bruto nunca se calcula a partir
de otros campos: sin bruto leído, la boleta queda pendiente. Si falta la retención o el
líquido (pero no ambos), el que falta se deduce de la identidad y queda marcado con una
advertencia de "monto deducido".

**Programa.** Se resuelve por precedencia: código de carpeta primero, alias de la
glosa después (los alias se buscan como palabras completas y gana el de menor
prioridad). Si la carpeta y el texto apuntan a programas distintos, se usa la carpeta y
se marca el conflicto para que una persona lo confirme.

**Catálogo de programas.** Un programa desactivado no se ofrece para carpetas o alias
nuevos (no aparece en la resolución automática ni en el selector de alias), pero las
boletas que ya lo tenían asignado lo conservan: nada se borra ni se reasigna solo. El
texto de un alias es único en el catálogo; importarlo de nuevo apuntando a otro
programa lo reasigna, para poder corregir alias contradictorios sin borrarlos primero.

**Duplicados.** Por hash exacto del archivo (mismo contenido, otra ruta) y por
(RUT emisor, folio) entre boletas no descartadas; ambos casos son bloqueantes y no
aceptables, porque esconder un duplicado sería un error de imputación.

**Confianza del OCR.** La confianza media de la página se informa como advertencia;
además, cada campo clave (RUT emisor, folio, fecha, bruto, retención, líquido) que se
haya leído por OCR con una confianza menor que el mínimo configurado genera una
incidencia bloqueante aceptable propia, porque una confianza media alta puede esconder
un solo campo mal leído (por ejemplo, un folio).

## Datos de ejemplo

En el primer arranque se genera una base sintética con semilla fija (mismo resultado
en cada corrida): unos 12 a 15 prestadores con nombres y RUT sintéticos, la
organización receptora "Organización Ejemplo", 4 programas genéricos, entre 6 y 8
meses de boletas con jornadas y tarifas de magnitud realista, y casos deliberados para
la cola de revisión (folio ilegible, tripleta incoherente, tasa de otro año, duplicado,
programa de carpeta distinto del texto, receptor distinto, confianza baja en todos los
campos clave). Además se escriben boletas de muestra en `data/muestras/` con la
convención de carpetas, para poder procesar con OCR real desde la interfaz. Todas las
boletas sintéticas llevan la marca "EJEMPLO SINTÉTICO - SIN VALIDEZ TRIBUTARIA".

## Cómo ejecutarlo

Desde el código (crea `.venv` en la primera corrida e instala las dependencias):

    py -3.12 run.py

Los datos quedan en `data/` junto al proyecto (o en la carpeta que indique `--datos`).
Otros parámetros útiles: `--reiniciar-datos` (regenera la base sintética),
`--autotest` (recorre toda la interfaz sin abrir ventana y termina con código de salida
0 si no hay problemas), `--capturas <carpeta>` (guarda una captura de cada pestaña
sobre datos de ejemplo nuevos).

Desde el ejecutable: `LectorBoletas.exe` abre la ventana directamente y crea la base
sintética en una carpeta `data/` junto al .exe la primera vez.

## Tests y calidad

    .venv\Scripts\python -m pytest
    .venv\Scripts\python -m ruff check .
    .venv\Scripts\python -m ruff format --check .

Los tests cubren los parsers (RUT, montos, fechas, folio, decreto, horas), la
clasificación de páginas y reconstrucción de líneas, las reglas de validación y la
derivación del estado (incluida la aprobación con incidencias aceptadas), la
persistencia, los servicios de procesamiento y revisión, los informes Excel (con los
totales verificados contra SQL) y la interfaz (con Qt en modo `offscreen`).

## Generar el ejecutable

    .\build.ps1

Genera `dist\LectorBoletas.exe` con PyInstaller (`--onefile --windowed`, sin consola),
incluye los datos que necesitan RapidOCR, onnxruntime y pypdfium2, y corre
`--autotest` sobre el ejecutable ya construido para confirmar que abre y procesa al
menos una muestra con OCR real.

## Estructura de carpetas

    fee-receipt-reader/
      pyproject.toml, run.py, build.ps1, README.md, RESUMEN_EJECUTIVO.md
      docs/img/            capturas de pantalla (solo datos sintéticos)
      src/receipt_reader/
        app.py, paths.py, logging_setup.py, errors.py
        domain/
        data/
        services/
        ui/
      tests/
