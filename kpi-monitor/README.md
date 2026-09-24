# Monitor de indicadores multisede

Aplicación de escritorio para hacer seguimiento a los indicadores de gestión de una organización con varias sedes: cuánto lleva cada indicador respecto de su meta anual, si la sede o la red van a cumplir al cierre del año, qué sedes explican la brecha, y un reporte ejecutivo listo para compartir.

Guía visual de la interfaz, pestaña por pestaña: [docs/guia_visual.pdf](docs/guia_visual.pdf).

## Problema que resuelve

Una organización con varias sedes compromete un conjunto de indicadores de gestión con metas anuales (algunas fijas, otras que crecen con el tiempo, otras por tramos). Cada mes llegan observaciones por sede. La jefatura necesita, a cualquier mes de corte, una lectura consolidada: cómo va cada indicador en cada sede y en la red completa, si el año se va a cerrar en meta, cuáles son las alertas que requieren atención, y un reporte para la toma de decisiones. Calcular esto a mano con planillas es lento y propenso a errores de agregación (sumar porcentajes en vez de razones, sumar un stock en vez de tomar el último corte, prorratear una meta que no debería prorratearse, etc.).

## Funcionalidades

- Catálogo declarativo de indicadores: cada indicador define su agregación (flujo o stock), tipo de denominador, dirección (mayor o menor es mejor), escala, techo natural, regla de meta por año y peso.
- Cálculo de cumplimiento a la fecha, meta a la fecha, estado de semáforo, índice ponderado por programa y proyección al cierre con banda de incertidumbre, para cualquier año, mes de corte, programa, sede o la red completa.
- Comparación entre sedes: cumplimiento del mismo indicador en todas las sedes en la misma escala, y un mapa de calor indicador por sede.
- Matriz de semáforo indicador por sede y red.
- Alertas automáticas: indicador en riesgo, sede sin reporte en el mes de corte, posible subregistro, dato imposible, meta no determinable.
- Ficha de cada indicador (qué mide, numerador, denominador, fuente, qué significa un cero).
- Importación de observaciones desde Excel en formato largo, con validación fila por fila y reporte de rechazos.
- Exportación de reporte ejecutivo en Excel (con formato y colores de semáforo) y en PDF.

## Arquitectura

El proyecto sigue una arquitectura por capas, con dependencias en una sola dirección:

    domain -> (no depende de nada del proyecto)
    data   -> depende de domain
    services -> depende de domain y data
    ui     -> depende de services y domain (nunca de data directamente)

`errors.py` puede usarse desde cualquier capa.

### Módulos principales

| Módulo | Responsabilidad |
| --- | --- |
| `domain/models.py`, `domain/enums.py` | Modelos inmutables del dominio: indicador, regla de meta, tramo, meta fija por sede, observación. |
| `domain/goals.py` | Meta efectiva por año según el tipo de regla (absoluta, aumento relativo, aumento con techo, tramos, línea base) y prorrateo de la meta a la fecha. |
| `domain/aggregation.py` | Acumulado del año a la fecha: razón de sumas de numeradores y denominadores; stock evaluado en el último corte disponible, nunca sumado. |
| `domain/compliance.py` | Cumplimiento según la dirección del indicador y clasificación en semáforo. |
| `domain/projection.py` | Proyección al cierre de año: ritmo promedio para flujos, arrastre del último corte para stocks, banda de incertidumbre y probabilidad de cumplir por remuestreo (bootstrap) de los aportes mensuales con semilla fija. |
| `domain/index.py` | Índice ponderado por programa (suma de peso por cumplimiento topado en 100 %, sobre el peso con dato disponible) y su serie mes a mes. |
| `domain/alerts.py` | Reglas de alerta (riesgo, sede sin reporte, subregistro, dato imposible, meta no determinable). |
| `domain/engine.py` | Orquesta el cálculo completo de un indicador (meta, acumulado, cumplimiento, proyección) a partir del catálogo y las observaciones. |
| `domain/quality.py`, `domain/reporting.py`, `domain/summary.py` | Calidad de datos (meses no reportados, cobertura de peso), agregados para el reporte y resúmenes para la interfaz. |
| `data/schema.sql`, `data/db.py` | Esquema SQLite y acceso a la base. |
| `data/repositories.py` | Repositorios que traducen entre filas de SQLite y objetos de dominio. |
| `data/catalog_seed.py`, `data/synthetic.py`, `data/seed.py` | Generador de datos sintéticos (catálogo y observaciones) con semilla fija. |
| `data/excel_io.py` | Importación y exportación de observaciones en Excel. |
| `data/excel_report.py`, `data/pdf_report.py` | Reporte ejecutivo en Excel y en PDF. |
| `services/monitor_service.py` | Evalúa indicadores para un período, programa y sede o red; series mensuales; proyección; índice ponderado; alertas. |
| `services/catalog_service.py` | Consulta y edición del catálogo (indicadores, metas, pesos, ficha). |
| `services/data_service.py` | Importación de observaciones y generación de la plantilla de importación. |
| `services/report_service.py` | Arma el reporte ejecutivo (Excel y PDF) a partir de los servicios anteriores. |
| `ui/main_window.py` | Ventana principal: panel de parámetros, franja de totales y pestañas. |
| `ui/views.py`, `ui/charts.py`, `ui/presenters.py`, `ui/editors.py` | Pestañas, gráficos, formateo de datos para la UI y editores del catálogo. |

### Flujo de datos

1. Al iniciar, `app.py` crea (si no existe) la base SQLite en el directorio de datos del usuario y la puebla con el generador sintético.
2. La UI pide al usuario año, mes de corte, programa e indicador o sede.
3. `MonitorService` lee el catálogo y las observaciones desde los repositorios, calcula el resultado de cada indicador con `domain/engine.py` y arma series, proyección, índice ponderado y alertas.
4. Las pestañas muestran esos resultados en tablas y gráficos matplotlib embebidos.
5. Importar datos y exportar reportes pasan por `DataService` y `ReportService`, que a su vez usan los repositorios y el motor de dominio.

### Modelo de datos

Tablas principales (SQLite, con claves foráneas activas):

- `site`: sedes (código, nombre, tipo).
- `site_population`: población de referencia de cada sede por año (para indicadores de cobertura poblacional).
- `program`: programas que agrupan indicadores.
- `indicator`: definición declarativa de cada indicador (agregación, tipo de denominador, multiplicador, dirección, escala, techo natural, ficha técnica).
- `goal_rule`: regla de meta y peso de un indicador en un año (`goal_rule.indicator_code, year` es clave primaria; sin fila, el indicador no está vigente ese año).
- `goal_band`: tramos de cumplimiento de una regla por tramos (cotas semiabiertas).
- `site_goal`: meta fija anual por sede, para indicadores con meta fija por sede.
- `observation`: dato mensual por indicador y sede (numerador, denominador, si fue reportado, origen); única por indicador, sede, año y mes.
- `setting`: parámetros de cálculo (umbrales de semáforo, prevalencia, período por defecto, configuración del bootstrap).

## Reglas de negocio y decisiones de diseño

- **Acumulado del año a la fecha** es siempre una razón de sumas, nunca un promedio de porcentajes mensuales: $`\text{acumulado} = \dfrac{\sum \text{numeradores}}{\sum \text{denominadores}}`$. Para denominador fijo por sede es $`\dfrac{\sum \text{numeradores}}{\text{meta fija}}`$. Un indicador de stock nunca se suma entre meses: se usa el valor del último corte disponible en el año. El multiplicador $k$ (para denominadores del tipo "k veces el stock") se aplica una sola vez.
- **Red = suma de las sedes** siempre (numeradores, denominadores y metas fijas); la red nunca toma el estado de la peor sede ni el promedio de los estados.
- **Meta efectiva por año** según el tipo de regla:
  - Absoluta: el valor fijo.
  - Aumento relativo: $`\text{meta} = \text{referencia}_{\text{año anterior}} \times (1 + \text{factor})`$.
  - Aumento con techo: $`\text{meta} = \min(\text{techo},\ \text{referencia}_{\text{año anterior}} \times (1 + \text{factor}))`$.
  - Tramos: intervalos semiabiertos con un porcentaje de cumplimiento fijo por tramo (por ejemplo, para un tiempo de espera en días: hasta 20 días, 100 %; entre 20 y 30, 75 %; y así decreciendo).
  - Línea base: se informa el valor pero no tiene semáforo.
  
  La referencia del año anterior es el acumulado completo de ese año; si es cero o no existe, el indicador queda en estado "meta no determinable" en vez de dividir por cero. Toda la aplicación usa una única función para resolver la meta efectiva, de modo que ninguna vista pueda calcularla de otra forma.
- **Meta a la fecha** (prorrateo): solo se prorratea cuando el acumulado crece con el tiempo dentro del año (flujo sobre meta fija o sobre stock), como $`\text{meta a la fecha} = \text{meta anual} \times \dfrac{\text{mes de corte}}{12}`$. No se prorratea cuando el denominador también es un flujo del período (ambos crecen juntos) ni en indicadores que son un promedio.
- **Cumplimiento**: si mayor es mejor, $`\text{valor} / \text{meta a la fecha}`$; si menor es mejor, $`\text{meta a la fecha} / \text{valor}`$ (con valor cero, se considera que cumple). En indicadores por tramos, el cumplimiento es el porcentaje del tramo alcanzado.
- **Semáforo** único y configurable: verde desde 100 % de cumplimiento, amarillo desde 85 %, rojo bajo 85 %; "sin datos" si no hay observaciones reportadas en el período; "no determinable" si la meta no se puede calcular; los indicadores de línea base no llevan color.
- **Índice ponderado por programa**: $`\sum \text{peso}_i \times \min(1, \text{cumplimiento}_i)`$ dividido por la suma de pesos con dato disponible; se informa además qué porcentaje del peso total quedó sin dato ese mes.
- **Proyección al cierre**: una sola proyección por indicador. Para numeradores de flujo, se usa el ritmo promedio de los meses transcurridos del año proyectado sobre los meses restantes (horizonte = 12 − mes de corte), con una banda de incertidumbre (percentiles 10 y 90) y una probabilidad de cumplir obtenidas por remuestreo (bootstrap) de los aportes mensuales ya observados, con semilla fija para que el resultado sea reproducible. Con menos de tres meses de datos en el año, se informa "datos insuficientes" en vez de una proyección poco confiable. Para indicadores de stock, la proyección es el arrastre del último corte disponible.
- **Período de corte** (año y mes) es siempre una elección explícita del usuario en la interfaz, nunca se infiere de la fecha del sistema. Un mes sin observación reportada por una sede se trata como "falta el dato", nunca como cero.
- **Alertas**: indicador en riesgo (semáforo rojo o proyección bajo la meta), sede sin reporte en el mes de corte, posible subregistro (numerador cero con denominador mayor que cero), dato imposible (valor sobre el techo natural, o numerador mayor que el denominador en una proporción) y meta no determinable.

## Datos de ejemplo

El generador sintético (`data/catalog_seed.py`, `data/synthetic.py`) crea, con una semilla fija (mismo resultado en cada ejecución):

- Una organización genérica ("Organización Ejemplo") con 5 sedes de ejemplo.
- Un catálogo de 24 indicadores de gestión distribuidos en 3 programas, con nombres genéricos pero con sentido de gestión real (por ejemplo, cobertura de personas en seguimiento, tiempo de espera a primera atención, proporción de egresos por logro de objetivos). Los pesos, metas y tramos son valores sintéticos que conservan la magnitud y las proporciones típicas de un convenio de gestión real, pero no reproducen cifras exactas.
- Tres años de observaciones mensuales (dos años anteriores completos y el año en curso hasta un mes de corte reciente) por indicador y sede, calibradas en magnitud y estacionalidad (caídas en febrero y diciembre), con una mezcla deliberada de estados de semáforo, algunos meses no reportados y algunos ceros de subregistro, para que el panel muestre casos reales de todo tipo.

Este catálogo y estas observaciones sintéticas se usan únicamente para la demostración; el motor de cálculo es genérico y funciona igual con cualquier catálogo y observaciones cargados por importación Excel o por la API de servicios.

## Cómo ejecutarlo

Desde el código fuente (crea el entorno virtual la primera vez):

```powershell
python run.py
```

Otras banderas de `run.py`:

- `--datos <carpeta>`: usa otra carpeta de datos en vez de la del usuario.
- `--reiniciar-datos`: borra y vuelve a generar la base sintética.
- `--autotest`: recorre todas las pestañas sin abrir ventana visible y termina con código 0 si no hay problemas (o distinto de 0 si los hay).
- `--capturas <carpeta>`: genera capturas de pantalla de cada pestaña y cierra la aplicación sola.

Desde el ejecutable (`dist\MonitorIndicadores.exe`, ver más abajo): al abrirlo por primera vez crea la base sintética en una carpeta `data` junto al propio ejecutable (o en el directorio de datos del usuario si se ejecuta desde otra ubicación sin esa carpeta), y la reutiliza en las siguientes aperturas. La ruta se puede forzar con la variable de entorno `KPI_MONITOR_DATA_DIR`.

## Tests y estilo

```powershell
.venv\Scripts\python -m pytest
.venv\Scripts\python -m ruff check .
.venv\Scripts\python -m ruff format --check .
```

## Generar el ejecutable

```powershell
.\build.ps1
```

Instala las dependencias, corre los tests, construye `dist\MonitorIndicadores.exe` con PyInstaller (un solo archivo, sin consola) y termina corriendo `--autotest` sobre el ejecutable generado para confirmar que abre correctamente.

## Estructura de carpetas

    kpi-monitor/
      pyproject.toml, run.py, build.ps1, README.md, RESUMEN_EJECUTIVO.md
      docs/img/            capturas generadas con --capturas (datos sintéticos)
      src/kpi_monitor/
        app.py, paths.py, logging_setup.py, errors.py
        domain/           modelos, reglas, cálculos (sin acceso a datos ni a la interfaz)
        data/             base SQLite, esquema, repositorios, generador sintético, Excel
        services/         casos de uso que orquestan dominio y datos
        ui/                interfaz PySide6 con gráficos matplotlib embebidos
      tests/
