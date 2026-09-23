# Simulador de costos de dotación: resumen ejecutivo

## Problema

Una organización con cinco sedes financia sus equipos con cuatro programas (convenios), cada uno con montos asignados por ítem: recurso humano, arriendos, compras, capacitación, etc. El costo del personal depende del tipo de contrato (honorarios o plazo fijo y planta, con reglas de costeo distintas), la jornada, las fechas de ingreso y término, los cambios de jornada y las personas que trabajan en más de un programa. Proyectarlo en planillas es lento y propenso a errores que no se ven: contratos que se siguen cobrando después de su término, valores hora que faltan y quedan en cero, filas que no entran en los totales, o gastos de operación que no se pueden comparar con lo que cada ítem del convenio tiene asignado. Tampoco permite responder rápido preguntas como «¿cuánto cuesta abrir siete puestos desde julio?» o «¿qué pasa si parte del equipo pasa a plazo fijo?».

## Solución

Una aplicación de escritorio que calcula el costo de cada puesto mes a mes con reglas únicas y probadas (valor hora de honorarios por categoría y año, sueldo del grado 15 con reajustes para plazo fijo y planta, jornadas, prorrateo de los meses de ingreso y término, ausentismo esperado, retención de honorarios) y que permite:

- cargar la estructura financiera de cada convenio (monto total e ítems presupuestarios) y planificar sus costos contra ella;
- armar escenarios de contratación y de otros gastos, y compararlos lado a lado contra un escenario base;
- cargar desde Excel lo pagado cada mes por ítem y medir la desviación contra lo planificado;
- exportar un informe Excel completo, cuyos desgloses siempre cuadran con el total.

## Resultados con los datos de demostración

Los datos son sintéticos, pero imitan la estructura y las magnitudes de un caso real: 5 sedes, 4 programas con 20 ítems presupuestarios y 55 personas en 2026. Cifras en millones de pesos.

| Escenario | Recurso humano | Otros gastos | Planificado | Asignado (687,9) | Saldo |
| --- | ---: | ---: | ---: | ---: | ---: |
| Dotación vigente | 563,1 | 62,0 | 625,1 | 687,9 | +62,9 |
| Expansión (7 puestos nuevos desde julio) | 610,9 (+8,5 %) | 62,0 | 672,9 | 687,9 | +15,0 |
| Reconversión a plazo fijo (honorarios desde mayo) | 538,8 (-4,3 %) | 62,0 | 600,8 | 687,9 | +87,1 |

- **La holgura total esconde un déficit puntual.** La dotación vigente (58 puestos: 55 personas y 3 vacantes) usa el 90,9 % de lo asignado, pero el ítem de recurso humano del Programa Comunitario planifica 153,3 contra 148,0 asignados: un déficit de 5,3 (1 de los 20 ítems).
- **La reconversión no siempre es más cara.** Al pasar de honorarios al sueldo del grado 15, el costo de recurso humano baja 24,3 (-4,3 %) en vez de subir: el programa deja de pagar el valor hora de mercado y paga solo la base del grado 15, con la diferencia de grado real a cargo del municipio.
- **La expansión reduce la holgura pero no genera déficit.** Suma 47,8 de recurso humano en el año (+8,5 %) y el saldo total baja de 62,9 a 15,0.
- **La ejecución va levemente bajo lo planificado.** De enero a agosto se pagaron 401,4 contra 406,7 planificados en los ítems con ejecución registrada (-5,3, es decir -1,3 %).
- **Los supuestos quedan a la vista.** El ausentismo esperado de 3 % reduce el costo de recurso humano en 16,0. La retención de honorarios (78,9 sobre 517,3 brutos) se informa aparte, sin alterar el costo para la organización, y el aporte del empleador de plazo fijo y planta queda en 0 % por defecto hasta que se cargue la tasa real.

![Estructura financiera del programa](docs/img/03_estructura_del_programa.png)

![Presupuesto y ejecución por ítem](docs/img/07_presupuesto_y_ejecucion.png)
