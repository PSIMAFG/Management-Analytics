# Management Analytics

Cuatro herramientas de escritorio para apoyar la gestión de una organización multisede: planificación del personal, costeo de la dotación, seguimiento de indicadores y digitalización de boletas de honorarios. Cada proyecto resuelve un problema real de gestión con un enfoque analítico distinto (optimización con restricciones, simulación de escenarios, medición del desempeño y extracción de documentos con OCR).

Todos los datos incluidos son sintéticos y se generan de forma reproducible con una semilla fija. Conservan la estructura, las proporciones y los patrones de un caso real, pero ningún nombre, identificador, monto ni lugar corresponde a personas u organizaciones reales.

## Proyectos

| Proyecto | Problema que resuelve | Técnicas principales |
| --- | --- | --- |
| [Optibox](optibox/) | Asignar turnos de atención al personal cubriendo la demanda de cada bloque horario, sin exceder jornadas ni romper reglas de competencias y salas. | Cadena de reglas para filtrar y priorizar candidatos, optimización con OR-Tools CP-SAT en dos fases. |
| [Simulador de costos de dotación](staffing-simulator/) | Proyectar el costo de un equipo según escenarios de contratación, tipo de contrato y jornada, y compararlo con el presupuesto y con lo ejecutado. | Motor de costeo mensual con prorrateo, retenciones y aportes parametrizados por año; comparación de escenarios. |
| [Monitor de indicadores multisede](kpi-monitor/) | Seguir 24 indicadores en 5 sedes contra sus metas, con avance mensual y acumulado, proyección al cierre y reporte ejecutivo. | Definición declarativa de indicadores, reglas de meta, semáforo, índice ponderado, proyección con bootstrap. |
| [Lector de boletas de honorarios](fee-receipt-reader/) | Registrar en lote boletas de honorarios en PDF o imagen, validar sus montos y producir informes por programa y período. | OCR con RapidOCR, reconstrucción de líneas, validación de RUT y de la tripleta bruto, retención y líquido. |

Cada proyecto incluye una guía visual en PDF (`docs/guia_visual.pdf`) que muestra todas las pestañas de su interfaz con datos de demostración.

## Stack común

- Python 3.11 o superior.
- Interfaz de escritorio con PySide6 y gráficos embebidos con matplotlib.
- Persistencia en SQLite, creada y poblada automáticamente en la primera ejecución.
- Importación y exportación Excel con openpyxl.
- Tests con pytest y lint con ruff.
- Ejecutable autocontenido para Windows con PyInstaller.

Cada proyecto es independiente: tiene su propio `pyproject.toml`, sus dependencias, sus tests, su script de construcción y su documentación. La lógica está separada en cuatro capas (`domain`, `data`, `services` y `ui`) y el dominio no depende de la persistencia ni de la interfaz.

## Cómo ejecutar un proyecto

Desde la carpeta del proyecto:

```powershell
python run.py
```

El lanzador crea un entorno virtual propio del proyecto, instala las dependencias que falten y las actualiza dentro de rangos compatibles en cada ejecución. Si no hay conexión, continúa con lo ya instalado. También se puede usar el ejecutable generado con `build.ps1`, que no requiere Python.

El README de cada proyecto explica su arquitectura, su modelo de datos, cómo correr los tests y cómo generar el ejecutable. El archivo `RESUMEN_EJECUTIVO.md` de cada uno resume el problema y los resultados para un lector no técnico.

## Licencia

MIT. Ver [LICENSE](LICENSE).
