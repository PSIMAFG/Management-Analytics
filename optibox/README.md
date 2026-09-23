# Optibox

Optibox asigna los turnos semanales de personal en un centro de atención con
varias salas (boxes) y varios cargos. A partir de la disponibilidad, las
competencias, los contratos, las ausencias y la demanda de atenciones por día
y bloque horario, construye una agenda de lunes a viernes que cubre la mayor
cantidad de demanda posible y, dentro de eso, la de mejor calidad según el
escenario elegido (preferencias de sala, continuidad, metas por persona,
mezcla de tipos de atención).

## Problema que resuelve

Programar manualmente los turnos de un equipo de varios cargos, con
contratos de distintas horas, ventanas de disponibilidad, ausencias,
reuniones y salas con restricciones propias, es lento y propenso a errores:
solapes de horario, sesiones que cruzan el almuerzo, personal sobre su
contrato o salas que exceden su capacidad. Optibox automatiza esa
programación con una cadena de reglas de negocio y un modelo de optimización,
y explica con una causa concreta por qué una atención quedó sin cubrir.

## Funcionalidades

- Genera candidatos (persona, tipo de atención, sala, día, hora de inicio)
  aplicando una cadena ordenada de reglas de negocio y los prioriza.
- Resuelve la semana con un modelo de optimización (CP-SAT) en dos fases:
  primero maximiza la cobertura de la demanda y después, sin perderla,
  mejora la calidad del plan según el escenario.
- Valida cada plan de forma independiente del optimizador (solapes,
  contrato, topes, cobertura, compatibilidad, disponibilidad y almuerzo).
- Explica cada turno sin cubrir con una causa (sin candidatos, contrato
  agotado, personal o salas ocupadas, tope alcanzado, piso de cobertura del
  escenario o límite de tiempo del optimizador).
- Calcula métricas de cobertura, carga por persona, utilización de salas y
  rendimiento de horas (ver más abajo), y guarda cada corrida con sus
  parámetros para compararla con otras.
- Exporta el plan y los datos maestros a Excel, e importa datos maestros con
  validación fila por fila (nunca importa a medias).
- Interfaz de escritorio con panel de parámetros, franja de totales,
  pestañas de resultados y edición de demanda, ausencias y contratos.

## Arquitectura

Capas con dependencia en un solo sentido: `domain` no importa `data`,
`services` ni `ui`; `data` no importa `ui` ni `services`; `ui` solo habla con
`services` y con modelos de `domain`.

| Capa | Contenido |
| --- | --- |
| `domain` | Modelos inmutables, cadena de reglas (`rules.py`), modelo CP-SAT (`optimizer.py`), heurística voraz (`greedy.py`), validador (`validator.py`), métricas (`metrics.py`), diagnóstico de faltantes (`diagnosis.py`), agenda (`agenda.py`), grilla horaria (`timegrid.py`) |
| `data` | Esquema SQLite (`schema.sql`), acceso a la base (`db.py`), repositorios (`repository.py`), generador sintético (`seed.py`), codificación de corridas (`codec.py`), exportación e importación Excel (`excel_format.py`, `excel_master.py`, `excel_plan.py`) |
| `services` | `PlanningService` (arma la instancia de la semana, corre las dos etapas, valida, persiste y calcula métricas), `MasterDataService` (lee y edita datos maestros), `ExcelService` (exporta e importa) |
| `ui` | Ventana principal (PySide6) con panel lateral, franja de totales y pestañas con tablas y gráficos matplotlib embebidos |

### Flujo de datos de una optimización

1. `MainWindow` pide a `PlanningService.optimize` la semana, el escenario y
   el límite de tiempo elegidos en el panel lateral.
2. `PlanningService` arma la `PlanningInstance` desde los datos maestros de
   SQLite (`build_instance`): resuelve el contrato vigente de cada persona
   por día (con prorrateo si cambia a mitad de semana), sus máscaras de
   disponibilidad, ausencias, feriados y bloqueos.
3. `rules.generate_candidates` recorre la cadena de reglas y devuelve los
   candidatos priorizados, con un resumen de exclusiones por regla.
4. `optimizer.optimize` corre la heurística voraz (línea base y hint) y
   después el modelo CP-SAT en dos fases (cobertura y calidad).
5. `validator.validate_plan` reaplica todas las reglas de negocio al plan
   elegido; si encuentra una violación, la corrida no se guarda.
6. `metrics.compute_metrics` calcula cobertura, carga, utilización y
   rendimiento de horas; `diagnosis.diagnose_unmet` explica cada faltante.
7. La corrida (plan, métricas, diagnóstico y parámetros) se guarda en
   SQLite y la ventana actualiza la franja de totales y todas las pestañas.

### Modelo de datos

Tablas principales y sus relaciones (claves foráneas):

- `role` (cargo) `1 -- N` `staff` (persona) `1 -- N` `contract` (contratos
  versionados por vigencia), `availability` (ventanas semanales),
  `staff_skill` (competencias, además de las del cargo), `staff_target`
  (metas de minutos por tipo de atención) y `staff_room` (reglas de sala).
- `service_type` (tipo de atención) `N -- N` `role` vía `role_service`, y
  `N -- N` `room` vía `service_room`.
- `room` (sala): tipo, admite grupos o evaluación, cupo, capacidad dura y
  blanda si es la sala administrativa.
- `blocking` (bloqueos y reuniones) y `absence` (ausencias, con estado:
  solo "aprobada" bloquea) referencian `staff` cuando el destinatario es una
  persona puntual.
- `demand` (día, bloque, tipo, sesiones requeridas, prioridad) y
  `center_hours` (horario del centro por día) definen la semana disponible.
- `scenario` `1 -- N` `scenario_weight` (pesos de mezcla por tipo).
- `run` (una corrida) `1 -- N` `assignment` (sesiones y tramos
  administrativos), `unmet_demand` (faltantes con su causa) y
  `rule_exclusion` (conteo de exclusiones por regla).

## Reglas de negocio y decisiones de diseño

### Cadena de reglas (etapa 1)

Para cada candidato (persona, tipo, sala, día, minuto de inicio) se evalúa,
en este orden, la primera regla que falla:

1. Contrato vigente en la semana (`contrato`).
2. Competencia para el tipo de atención, incluida la lista blanca
   individual (`competencia`).
3. Disponibilidad: el intervalo completo cabe dentro de una ventana
   (`disponibilidad`).
4. Ausencias aprobadas y feriados (`ausencia`).
5. Bloqueos y reuniones que aplican a la persona (`bloqueo`).
6. Sala activa y compatible con el tipo (flags de grupo o evaluación,
   reserva por cargo) (`sala_compatible`).
7. Sala permitida para la persona (matriz de permitida, prohibida o
   preferida) (`sala_permitida`).
8. Cabe en el horario del centro sin cruzar el almuerzo ni el cierre
   (`horario`).
9. Existe demanda del tipo en el bloque de inicio (`demanda`).

Un contrato que no cubre todos los días hábiles de la semana (empieza,
termina o cambia a mitad de semana) deja sin disponibilidad los días que no
cubre; los minutos de contrato de la semana se prorratean por día hábil
cubierto, de modo que ninguna sesión cae fuera de su vigencia.

### Modelo de optimización (etapa 2)

**Conjuntos.** $K$: candidatos generados por la etapa 1 (cada uno con
persona $p_k$, tipo $s_k$, sala $r_k$, día $d_k$, minutos $[i_k, i_k+\Delta_k)$).
$P$: personal asistencial. $D$: días hábiles de la semana. $T$: slots de 15
minutos del horario abierto de cada día.

**Variables.**
$x_k \in \{0,1\}$ para cada candidato $k \in K$ (se asigna la sesión).
$a_{p,d,t} \in \{0,1\}$ para cada persona asistencial $p$, día $d$ y slot
$t$ en que $p$ puede trabajar (tramo administrativo).

**Restricciones duras.**

Una sola cosa por persona y slot (una sesión que cubre $t$ o administrativo,
nunca ambas):

$$\sum_{k \in K:\, p_k=p,\, t \in [i_k, i_k+\Delta_k)} x_k \;+\; a_{p,d,t} \;\le\; 1 \qquad \forall p, d, t$$

Una sesión por sala y slot:

$$\sum_{k \in K:\, r_k=r,\, t \in [i_k, i_k+\Delta_k)} x_k \;\le\; 1 \qquad \forall r, d, t$$

Capacidad dura de la sala administrativa (si existe):

$$\sum_{p} a_{p,d,t} \;\le\; \text{cap\_dura} \qquad \forall d, t$$

Contrato: minutos de sesiones más administrativo más bloqueos del
destinatario dentro de su disponibilidad, no superan los minutos
contratados de la semana (los bloqueos ya se descuentan al construir la
instancia, quedan implícitos en el minutaje disponible):

$$\sum_{k:\, p_k=p} \Delta_k\, x_k \;+\; 15 \sum_{d,t} a_{p,d,t} \;\le\; \text{minutos\_contrato}(p)$$

Administrativo asociado: el administrativo colocado el mismo día cubre al
menos el que exigen las sesiones de ese día, y no supera ese requerido más
el tope diario adicional configurable:

$$\text{requerido}(p,d) \;\le\; 15 \sum_{t} a_{p,d,t} \;\le\; \text{requerido}(p,d) + \text{extra\_diario}$$

Topes semanales, por persona y por día de cada tipo de atención (los que
declara el tipo):

$$\sum_{k:\, s_k=s,\ \ldots} x_k \;\le\; \text{tope}(s, \ldots)$$

Cobertura por (día, bloque, tipo) no supera la demanda:

$$\sum_{k:\, d_k=d,\, \text{bloque}(i_k)=b,\, s_k=s} x_k \;\le\; \text{demanda}(d,b,s)$$

**Restricciones y objetivo blandos (fase B).** Exceso sobre la capacidad
blanda de la sala administrativa, desvío de las metas de minutos por
persona y tipo, desvío de la mezcla del escenario frente a sus cotas
mínima y máxima, preferencia de sala (según el ranking de la persona) y
número de salas distintas por persona y día (continuidad): cada término se
resta u ordena en el objetivo con el peso que define el escenario.

**Objetivo lexicográfico.**

$$\text{Fase A:}\quad \max \sum_{k \in K} w(s_k)\, x_k \qquad w(\text{alta})=4,\ w(\text{media})=2,\ w(\text{baja})=1$$

$$\text{Fase B:}\quad \sum_{k} w(s_k) x_k \;\ge\; \Big\lceil \text{piso}\% \cdot z_A \Big\rceil, \qquad \max\ \text{calidad(escenario)}$$

donde $z_A$ es el valor óptimo (o el mejor encontrado) de la fase A y
`piso` es `coverage_floor_pct` del escenario (100 % por defecto: no se
sacrifica cobertura por calidad). La fase B usa la solución de la fase A
como pista inicial (`hint`), y la fase A usa la heurística voraz. La
heurística voraz también sirve de línea base: si el CP-SAT no mejora su
cobertura ponderada en el tiempo disponible, se conserva la voraz.

### Rendimiento de horas (personal y salas)

Métrica nueva, calculada siempre desde las sesiones, los tramos
administrativos y los bloqueos del plan; ver `domain/metrics.py`.

Para una persona:

- **Jornada programada**: minutos dentro de sus ventanas de disponibilidad
  y del horario abierto del centro, sin almuerzo. Los feriados no son
  jornada.
- **Tiempo contratado disponible** = $\min(\text{jornada programada},\ \text{minutos de contrato})$:
  si la disponibilidad supera el contrato, ese exceso no se considera
  ocioso.
- **Permisos** (ausencias aprobadas dentro de la jornada programada): no
  son ociosos ni productivos, se informan y se descuentan aparte.
- **Horas productivas** = atención + administrativo + reuniones y
  bloqueos. **Horas productivas clínicas** = solo atención.
- **Horas ociosas** = $\max(0,\ \text{tiempo contratado disponible} - \text{permisos} - \text{productivas})$.
- Indicadores: % productivo = productivas / (tiempo contratado disponible
  − permisos); % productivo clínico = atención / (tiempo contratado
  disponible − permisos); % ocioso = ociosas / (tiempo contratado
  disponible − permisos). El personal de apoyo administrativo (que no
  recibe sesiones) no tiene horas ociosas: todo su tiempo presente cuenta
  como administrativo, igual que en la carga por persona, para no
  contradecir esa pestaña.

Para una sala de atención: **horas abiertas** (horario del centro sin
almuerzo ni feriados, igual que en la utilización), **horas ocupadas** por
atenciones y **horas ociosas** = abiertas − ocupadas, con su porcentaje de
ocupación, por sala, por día y en total.

La pestaña "Rendimiento de horas" muestra una tabla y un gráfico de barras
apiladas por persona (atención, administrativo, reuniones, permisos,
ociosas) y una tabla y un gráfico por sala (ocupado frente a ocioso). La
franja de totales agrega las horas ociosas del personal y de las salas de
la semana, y el libro Excel exportado trae una hoja "Rendimiento de horas"
con ambas tablas.

## Datos de ejemplo

El generador sintético (semilla fija, `SEED` en `data/seed.py`) crea, cada
vez que se ejecuta, la misma base: 12 personas con la mezcla de cargos de un
equipo real (4 terapeutas ocupacionales, 3 fonoaudiólogos, 1 kinesiólogo, 2
psicólogos, 1 trabajador social y 1 apoyo administrativo), contratos de 18 a
44 horas semanales (con dos cambios de jornada en el año para probar la
vigencia a mitad de semana), 9 salas arquetipo (boxes, sala de usos
múltiples, sala de evaluación, gimnasio, sala grupal, sala psicosocial y
una sala administrativa), 7 tipos de atención con sus topes y minutos
administrativos,
los bloqueos operativos del centro (preparación, cierre, reunión técnica
semanal), 2 o 3 ausencias en la semana de la demo (aprobada, pendiente,
rechazada y una de medio día) y un feriado en la semana siguiente. La
demanda se genera con una distribución de Poisson por día, bloque y tipo,
calibrada para que la capacidad quede algo por debajo de la demanda en las
horas punta: así la demo también muestra turnos sin cubrir con causas
explicables. Al poblar la base se guarda además una corrida de ejemplo (con
límite de tiempo corto) para que la ventana abra con resultados.

Nombres de personas y de la organización son combinaciones sintéticas; los
nombres de sede y de programa son genéricos.

## Cómo ejecutarlo

Desde el código fuente (crea `.venv` la primera vez e instala las
dependencias declaradas en `pyproject.toml`):

```
python run.py
```

Argumentos: `--datos <carpeta>` usa una carpeta de datos alternativa;
`--reiniciar-datos` borra la base y la regenera; `--autotest` abre la
ventana, recorre todas las pestañas y sale con el código 0 si no encontró
problemas; `--capturas <carpeta>` guarda una captura de cada pestaña y sale.

Desde el ejecutable (`Optibox.exe`, generado con `build.ps1`): la base de
datos y los registros quedan en una carpeta `data` junto al .exe (o, si esa
carpeta no admite escritura, en `%LOCALAPPDATA%/ManagementAnalytics/Optibox`).
La variable de entorno `OPTIBOX_DATA_DIR` permite forzar otra ruta.

## Tests y verificación de estilo

```
.venv\Scripts\python -m pytest
.venv\Scripts\python -m ruff check .
.venv\Scripts\python -m ruff format --check .
```

Los tests cubren cada regla de la cadena (caso que pasa y que falla), la
generación de candidatos, el solver en instancias pequeñas con óptimo
conocido, el validador para cada tipo de violación, las métricas
(incluido el rendimiento de horas), el diagnóstico de faltantes, la
persistencia, la exportación e importación Excel y una autoprueba de la
interfaz completa.

## Generar el ejecutable

```
.\build.ps1
```

Empaqueta con PyInstaller (`--onefile --windowed`, sin consola) y corre
`--autotest` sobre el `.exe` resultante antes de darlo por bueno.

## Estructura de carpetas

    pyproject.toml, run.py, build.ps1, README.md, RESUMEN_EJECUTIVO.md
    docs/img/            capturas generadas con --capturas
    src/optibox/
      app.py, paths.py, logging_setup.py, errors.py
      domain/           modelos, reglas, optimizador, métricas, validador
      data/             esquema SQLite, repositorios, generador sintético, Excel
      services/         casos de uso (planificación, datos maestros, Excel)
      ui/                ventana principal y pestañas (PySide6 + matplotlib)
    tests/
