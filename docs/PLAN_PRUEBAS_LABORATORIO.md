# Plan de pruebas en el laboratorio: cDAQ-9178 (NI 9213 + NI 9201). Fase 2B

**EJECUTADO el 9-nov-2026** con el chasis real (cDAQ1: NI 9213 en la ranura 5, NI 9201 en la ranura 6; NI-DAQmx 26.8.0).

Resultado:
- Pasos 0-6, 7.3, 7.4 y 9 (sin LabVIEW): hechos. Velocidad real del 9213 (D3) CERRADA: máx = mín(100, 1/(t_conv·(canales + internos))), t_conv 55 ms / 0,74 ms, internos 2 con autocero «cada muestra» y 1 con «una vez»/«no» (24 combinaciones medidas). Tipos C y A (D4): admitidos, con sus rangos.
- Toda opción del panel es una traducción 1:1 de NI-DAQmx: `cdaq_modelo.py` (raíz del panel) validado con `herramientas/cdaq_validar_modelo.py` (85 OK) y, a través del panel, `pruebas/validar_cdaq_panel.py` (42 OK). Qué admite el driver: `docs/cdaq_opciones_20261109_1202.md`. Propuesta: https://claude.ai/artifact/F13aq1yqUkhXbTvVsaKLEZ
- Pendiente: 7.1 (tensión contra un multímetro), 8 (USB fuera en marcha), 10.1 (registro de 10 min) y disparos/reloj externo con señal real en PFI0 (pendientes.json: manual_cdaq_*).

(Texto original del plan, antes de ejecutarlo:) No se había ejecutado; todo el código del cDAQ estaba NO PROBADO CON HARDWARE.

**Referencias:**
- Estudio y fuentes: `docs/ESTUDIO_cDAQ.md`.
- Entrada pendiente: `manual_cdaq_laboratorio` en `pruebas/pendientes.json`.

**Reglas durante la fase 2B:**
- Ir **en orden**. Si un paso falla, se para, se anota y se decide antes de seguir.
- NI MAX lo abre Kevin; Claude no lo abre.
- Nada de crear dispositivos simulados ni cambiar la configuración de NI sin preguntar.
- Señales en el 9201 **siempre dentro de ±10 V** (rango de medida). El límite de trabajo seguro es ±60 V DC canal-COM; los ±100 V son solo protección.
- En el 9213, nada por encima de ±30 V entre entradas y modo común dentro de ±1,2 V.

Cada resultado se apunta en la tabla del paso. Al final se actualizan el estudio, `pendientes.json` y las marcas «NO PROBADO» de lo que quede verificado.

---

## 0. Antes de empezar

| # | Qué | Cómo / qué debería verse | Resultado |
|---|---|---|---|
| 0.1 | **El VI de LabVIEW del compañero está CERRADO** | Confirmarlo con él. Aquí no hay LabVIEW de desarrollo, pero sí el runtime 2026, que podría ejecutar un VI compilado. Comprobar: `tasklist /FI "IMAGENAME eq LabVIEW.exe"` y preguntar el nombre de su ejecutable. Mientras su VI tenga un módulo, el panel saldrá «ocupado» (decisión 1: el servidor es el único dueño) | |
| 0.2 | Rama y versiones | `git -C C:\Proyectos\_HERRAMIENTAS\Panel_laboratorio status` limpio, en `feature/cdaq`. `python -c "import nidaqmx, importlib.metadata as m; print(m.version('nidaqmx'))"` → `1.7.0` | |
| 0.3 | Regresiones sin hardware siguen bien | `python pruebas\prueba_cdaq_sin_hardware.py` (95 OK) y `python pruebas\prueba_cdaq_servidor.py` (27 OK). Si el chasis ya está conectado, el bloque «driver real sin chasis» saldrá OMITIDA: es lo correcto | |

## 1. Conexión y NI MAX

| # | Qué | Cómo / qué debería verse | Resultado |
|---|---|---|---|
| 1.1 | Alimentación y USB | Fuente de 9-30 V y 15 W (NEC Class 2). USB directo al PC o a un hub **con alimentación propia**. LED POWER encendido; READY cuando el driver lo reconoce | |
| 1.2 | Windows lo ve | `powershell -c "Get-PnpDevice \| ? InstanceId -match 'VID_3923'"` → aparece el cDAQ | |
| 1.3 | **Chasis y módulos en NI MAX** (lo abre Kevin) | En «Devices and Interfaces»: cDAQ-9178 «cDAQ1» (o el nombre que tenga), con el NI 9213 y el NI 9201 debajo como `cDAQ1ModN`. Apuntar los **nombres y las ranuras**. Si están renombrados, ponerlos en `config.json` → `cdaq.chasis`, `cdaq.termopares.modulo` y `cdaq.tensiones.modulo` | |
| 1.4 | No hay tareas o canales globales del compañero que reserven los módulos | En NI MAX, «Data Neighborhood»: apuntar si hay tareas guardadas. La herramienta del paso 2 también las lista | |

## 2. Herramienta de descubrimiento (solo lectura)

| # | Qué | Cómo / qué debería verse | Resultado |
|---|---|---|---|
| 2.1 | Ejecutarla | `python herramientas\cdaq_descubrir.py` → escribe `docs\cdaq_descubrimiento_<fecha>.json` y `.md`. Están fuera de git por llevar números de serie. **No crea tareas** | |
| 2.2 | `product_type` exacto | Apuntar el texto literal del chasis y los módulos (¿«NI 9213» o «NI-9213»?). `cdaq.py` los reconoce por el número de 4 cifras (`_numero`). Confirmar que funciona | |
| 2.3 | Canales físicos | 9213: `ai0` a `ai15` (16). 9201: `ai0` a `ai7` (8) | |
| 2.4 | Propiedades clave | `ai_max_single_chan_rate`, `ai_max_multi_chan_rate` y `ai_min_rate` de cada módulo; `ai_samp_modes`; `ai_voltage_rngs` (9201: ±10 V; 9213: ±0,078 V); `ai_term_cfgs` del 9201 (se espera RSE); `ai_simultaneous_sampling_supported` (9201: False); `num_time_trigs`, contadores (`ci_physical_chans`) y `terminals` (PFI) del chasis | |
| 2.5 | `is_simulated` = False en los tres | Si sale True, es un simulado de NI MAX y el panel lo marcará como SIMULADO | |

## 3. Primera medida con el panel

| # | Qué | Cómo / qué debería verse | Resultado |
|---|---|---|---|
| 3.1 | Habilitar | En `config.json` → `cdaq`: `"habilitado": true`. Activar un termopar (`T0`) y una tensión (`V0`). Revisar los TODO_CONFIRMAR. Arrancar el panel | |
| 3.2 | Estado al arrancar | `/api/estado` → `cdaq.estado` = `libre`, mensaje «cDAQ visible» y `equipos` con chasis y módulos | |
| 3.3 | Tomar | `POST /api/cdaq/tomar` (cabecera `X-Autor`) → `midiendo`. **Sin error al crear el canal con CJC interna (`BUILT_IN`)**; si da error, apuntar el código | |
| 3.4 | Valores verosímiles | El termopar a temperatura ambiente marca lo mismo que un termómetro, dentro de unos ±2 °C (CJC máxima 1,7 °C de 0 a 70 °C, [L-213S]). `V0` sin nada conectado da un valor flotante: no vale como medida | |
| 3.5 | Valores por defecto reales | Con un script de una sola vez (a preparar al empezar la 2B): crear la tarea, leer `ai_auto_zero_mode` y `ai_adc_timing_mode` **antes** de fijarlos y apuntar el valor por defecto del 9213 | |
| 3.6 | Soltar | `POST /api/cdaq/soltar` → `libre`. En NI MAX (Test Panel) el módulo se puede abrir: está liberado | |

## 4. Velocidad real del 9213 (D3, SIN RESOLVER)

Para cada combinación: **16, 8 y 1 canales** × **alta resolución y alta velocidad**. Script de una sola vez, que no existe aún y se prepara al empezar la 2B; con el panel sin el cDAQ (Soltar). Se miden tres cosas:

- (a) `Device.ai_max_multi_chan_rate`, y `task.timing.samp_clk_max_rate` tras configurar los canales y el modo.
- (b) Lecturas **bajo demanda** por segundo: 20 lecturas cronometradas. Es el modo que usa el panel.
- (c) Tarea **con reloj de hardware** continua a la tasa máxima de (a) durante 30 s: ¿aparecen valores repetidos? Si la tasa supera lo que da el módulo, este repite muestras sin avisar (manual 372838E, «Slow Sample Rate Modules»).

| Canales | Modo | (a) MaxMultiChanRate / N | (a) samp_clk_max_rate | (b) lecturas/s bajo demanda | (c) ¿repetidos? | Referencia de la especificación |
|---|---|---|---|---|---|---|
| 16 | alta resolución | | | | | ~1 S/s (18 × 55 ms) |
| 16 | alta velocidad | | | | | 75 S/s (18 × 740 µs) o ~85 S/s según la ayuda |
| 8 | alta resolución | | | | | ~1,8 S/s (10 × 55 ms) |
| 8 | alta velocidad | | | | | 100 S/s (tope) |
| 1 | alta resolución | | | | | ~6 S/s (3 × 55 ms) |
| 1 | alta velocidad | | | | | 100 S/s (tope) |

Con el resultado se decide el `periodo_s` mínimo útil para cada modo y se cierra D3 en el estudio.

## 5. Tipos de termopar C y A (D4)

| # | Qué | Cómo / qué debería verse | Resultado |
|---|---|---|---|
| 5.1 | El driver acepta C y A en el 9213 | Crear un canal con `ThermocoupleType.C` y luego con `A` (script de una sola vez, sin leer). ¿Error al crear o al arrancar? Apuntar el código | |
| 5.2 | Con el panel | `configurar` un canal a tipo `C`, Tomar → ¿midiendo? Si DAQmx no lo acepta, quitar C y A de `TIPOS_TC` en `cdaq.py` | |

## 6. Termopar abierto

| # | Qué | Cómo / qué debería verse | Resultado |
|---|---|---|---|
| 6.1 | Abrir un termopar en marcha | Con un registro en curso (`POST /api/cdaq/registro/iniciar`), soltar un hilo de `T0`. En el panel: `cdaq.abiertos` = `["T0"]`, `ultimo` = null. En el CSV: celda vacía y `abierto:T0` en `avisos` | |
| 6.2 | Volver a conectarlo | Vuelven los valores; el aviso desaparece en la siguiente lectura | |
| 6.3 | Canal activo sin termopar | Debe salir «abierto», no un valor de fondo de escala | |

## 7. NI 9201 con una señal de referencia segura

**Referencia:** una pila (1,5 V o 9 V) o una referencia de tensión conocida, **siempre dentro de ±10 V**. Muy por debajo del límite de trabajo de ±60 V DC. Positivo a `AIx` y negativo a `COM`.

| # | Qué | Cómo / qué debería verse | Resultado |
|---|---|---|---|
| 7.1 | Cada canal | Medir la referencia en `ai0` a `ai7` con un multímetro al lado. La diferencia debe quedar dentro de la exactitud calibrada: ganancia ±0,25 % de la lectura y offset ±0,25 % de 10,53 V (unos ±26 mV) en el peor caso | |
| 7.2 | Escala | Probar `ganancia` y `desplazamiento` de un canal y comprobar el valor escalado en el CSV | |
| 7.3 | Efecto del escaneo | El 9201 no es simultáneo: un ADC recorre los canales. Misma señal **variable** (por ejemplo, un generador ≤ 10 V) en `ai0` y `ai7`, y tarea con reloj de hardware (script de una sola vez): medir el desfase entre canales y apuntar `task.timing.ai_conv_rate`. Por defecto el driver añade 10 µs de margen entre canales (ayuda NI-DAQmx). En lectura bajo demanda el desfase es del orden de microsegundos y no importa a 1 S/s | |
| 7.4 | Configuración de terminal | Confirmar que el panel crea los canales en RSE sin error | |

## 8. USB desconectado en marcha

| # | Qué | Cómo / qué debería verse | Resultado |
|---|---|---|---|
| 8.1 | Desenchufar el USB con un registro en curso | `cdaq.estado` = `desconectado` (o `no_disponible`), con el mensaje y el código DAQmx. **El resto del panel sigue respondiendo.** En el CSV aparece una fila `hueco:...`. Apuntar el código DAQmx que sale de verdad (se esperan −201003, −88705 o −89130) | |
| 8.2 | Volver a enchufarlo | En `reintento_s` (10 s) vuelve a `midiendo` solo, sin reiniciar el panel. El registro continúa en el mismo CSV | |
| 8.3 | Quitar la alimentación del chasis | Igual que en 8.1 y 8.2 | |
| 8.4 | Panel arrancado sin el chasis | `no_disponible`; al conectarlo y pulsar Tomar, mide | |

## 9. Convivencia con el VI del compañero (solo si se coordina con él)

| # | Qué | Cómo / qué debería verse | Resultado |
|---|---|---|---|
| 9.1 | VI abierto antes que el panel | Tomar → `ocupado`, con un mensaje que menciona LabVIEW. Apuntar el código real (se esperan −50103, −200022 o −201105). El panel no se cae | |
| 9.2 | El compañero cierra el VI | El panel lo toma solo en 30 s o menos | |
| 9.3 | **Al terminar: confirmar que el VI queda cerrado** | Pregunta directa al compañero y la comprobación de 0.1 | |

## 10. Cierre y registro completo

| # | Qué | Cómo / qué debería verse | Resultado |
|---|---|---|---|
| 10.1 | Registro de 10 minutos con todos los canales previstos | CSV con la cabecera, los canales, `t_unix_s` coherente con el reloj del PC y sin huecos. Comparar la cadencia real con `periodo_s` | |
| 10.2 | Cerrar el panel con el cDAQ midiendo | Las tareas se cierran: después el módulo se abre sin error en el Test Panel de NI MAX. Al volver a arrancar, el panel lo toma solo (`en_uso`) | |
| 10.3 | Documentar | Rellenar las tablas, actualizar `docs/ESTUDIO_cDAQ.md` (D3, D4 y valores por defecto) y `pendientes.json` (`manual_cdaq_laboratorio`), y quitar «NO PROBADO CON HARDWARE» solo de lo verificado | |
