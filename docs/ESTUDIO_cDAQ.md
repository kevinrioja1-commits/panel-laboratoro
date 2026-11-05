# Estudio: integración del NI cDAQ-9178 (NI 9213 + NI 9201) en Panel_laboratorio

Fase 1: estudio (2026-11-05). Fase 2A: implementación sin hardware, en la rama `feature/cdaq` (2026-11-05). Ver la §0.
Fecha: 2026-11-05.

**Claves de fuente** usadas en todo el documento:

| Clave | Documento | Origen |
|---|---|---|
| [L-213S] | *NI-9213 Specifications*, 378914D-01 (oct 2026) | Local: `Documentos Driver NI-DAQmx\ni-9213_specifications_2026-11-04-15-43-37.pdf` |
| [L-213G] | *NI-9213 Getting Started* (sin código de revisión; descargado el 2026-11-04) | Local |
| [L-201S] | *NI-9201 and sbRIO-9201 Specifications* (sin código de revisión; descargado el 2026-11-04) | Local |
| [L-201D] | *NI 9201 Datasheet* 373783A-02 (mar 2016) | Local. Es idéntica (mismo MD5) a la copia de datatec.eu |
| [L-UM] | *NI cDAQ-9171/9174/9178 User Manual* 372838E-01 (jul 2016) | Local. Es idéntico (mismo MD5) a farnell.com/datasheets/3216357.pdf |
| [L-HLP] | Ayuda instalada de NI-DAQmx 26.8: `mxdevconsid.chm`, `mxcncpts.chm`, `daqhelp.chm` | `C:\Program Files (x86)\National Instruments\NI-DAQ\docs\` (CHM fechados 2026-01/04) |
| [L-H] | Cabecera `NIDAQmx.h` del driver 26.8 y textos de error de `nicaiu.dll` 26.8.0 | Local, instalados |
| [W-9178] | *NI cDAQ-9178 Specifications* 374046A-01 (mayo 2013) | Web: download.ni.com/support/manuals/374046a.pdf. Desde la fase 2A también en local: `Documentos Driver NI-DAQmx\cdaq-9178_specifications_374046a-01.pdf` |
| [W-PY] | Paquete `nidaqmx`: PyPI, GitHub ni/nidaqmx-python (README, `examples/`, `generated/nidaqmx/*.py`) y readthedocs | Web |
| [W-SHOP] | Fichas de producto de ni.com (model-ni-9201, model-ni-9213) | Web |

Las páginas `ni.com/docs/...` (especificaciones del 9178, 9213 y 9201, *Getting Started* del 9213 y la referencia C de `DAQmxCreateAIThrmcplChan`) **no se pudieron leer**: la herramienta solo recibió el menú de navegación, sin contenido. Las he sustituido por el PDF 374046A, por la cabecera C instalada y por los PDF locales.

---

## 0. Actualización tras la fase 2A (2026-11-05)

### 0.1 Decisiones de Kevin

- El cDAQ-9178 se conecta por USB a **este** PC. **El servidor es el único dueño de los dos módulos.** El VI de LabVIEW del compañero no puede usarlos a la vez.
- La lectura por defecto es **bajo demanda (software)**. El reloj de hardware queda documentado (§4), pero no se implementa.
- Lo pendiente de contestar (qué mide cada canal del 9201, la frecuencia de registro y el tipo de termopar) queda configurable en `config.json` → `cdaq`, con valores por defecto marcados **TODO_CONFIRMAR**: lista `cdaq.TODO_CONFIRMAR`, `"mide": "TODO_CONFIRMAR"` en cada canal del 9201, tipo `"K"` y `periodo_s` 1 s.

### 0.2 Correcciones de la fase 1 aceptadas (decisión 4)

1. **Máximo 3 tareas AI simultáneas por chasis** (`ai`, `te0`, `te1`). Las «7 tareas» del 9178 son flujos de datos de cualquier tipo.
2. **Bloqueo por módulo:** un módulo solo puede estar en una tarea, y se bloquea **entero** aunque se use un solo canal.
3. **Velocidad del 9213 con 16 canales: SIN RESOLVER hasta medirla.** La especificación (75 S/s) y la fórmula de la ayuda del driver (unos 85 S/s) no cuadran (D3). La medida está en `docs/PLAN_PRUEBAS_LABORATORIO.md`.
4. **NI 9201:**
   - No es simultáneo: un solo ADC multiplexado.
   - **800 kS/s** con un canal y **500 kS/s** escaneando (columna «All Other Chassis»).
   - **Límite de trabajo: ±60 V DC** canal-COM. Los ±100 V son **solo protección**. El rango de medida es ±10 V.
5. **Tipos C y A del 9213: por verificar con el driver** y el módulo real. `cdaq.py` los acepta en la configuración, marcados «POR VERIFICAR».
6. **La datasheet 373783A del 9201 se contradice** sobre la simultaneidad. **Mandan la especificación actual y la ayuda de NI.**

### 0.3 Qué se ha implementado (todo **NO PROBADO CON HARDWARE**)

| Fichero | Qué es |
|---|---|
| `cdaq.py` | Instrumento `CDAQ` con el patrón de `tc08.py`. Un único hilo es dueño de DAQmx; una tarea por módulo; lectura bajo demanda; termopar abierto; estados ocupado, no disponible y desconectado con reintento; modo simulado marcado; registro CSV |
| `herramientas/cdaq_descubrir.py` | Inventario de solo lectura (sin tareas) en `docs/cdaq_descubrimiento_*.json` y `.md`. Esas salidas están en `.gitignore` porque llevan números de serie |
| `servidor.py`, `config.json` | Registro mínimo: instancia, clave `cdaq` en `/api/estado`, rutas `/api/cdaq/...`, cierre de tareas al cerrar el panel. Nueva sección `cdaq` **desactivada** (`habilitado: false`) |
| `pruebas/prueba_cdaq_sin_hardware.py`, `pruebas/prueba_cdaq_servidor.py` | Pruebas sin chasis, registradas en `pruebas/pendientes.json` |
| `requirements.txt` | Dependencias, con `nidaqmx==1.7.0` |
| `docs/PLAN_PRUEBAS_LABORATORIO.md` | Lo que hay que comprobar el día que se conecte el chasis (fase 2B) |

**Cómo funciona `cdaq.py`:**

- **Estados:**
  - `desactivado`: `habilitado: false`; no se carga `nidaqmx`.
  - `libre` y `conectando`.
  - `midiendo`.
  - `ocupado`: códigos DAQmx −50103, −200022 y −201105; reintenta como mínimo cada 30 s.
  - `no_disponible`: falta el chasis o un módulo.
  - `desconectado`: códigos −201003, −88705 y −89130; reintenta cada `reintento_s`.
  - `error`.
- **Termopar abierto:** se lee `in_stream.open_thrmcpl_chans_exist` y luego `open_thrmcpl_chans`. El valor se guarda **vacío**; en el CSV aparece `abierto:<nombre>` en la columna `avisos`, y el canal sale en `resumen()["abiertos"]`.
- **Modo común fuera de rango:** el valor se guarda, con el aviso `modo_comun`.
- **Desconexión durante un registro:** se escribe una fila `hueco:<motivo>` en el CSV.
- **Simulado:** en el modo simulado del panel y con dispositivos simulados de NI MAX (`is_simulated`), se marca `simulado: true`, los nombres llevan `SIM_`, los ids `sim.`, se añade un aviso, el CSV empieza con «DATOS SIMULADOS: NO SON MEDIDAS REALES» y el fichero se llama `cdaq_SIMULADO.csv`.

**Límites de la implementación y de la interfaz:**
- **La interfaz web todavía no muestra el cDAQ:** solo la API. Mostrarlo en `index.html` y `vivo.html` queda para una fase posterior, porque no se pidió en la 2A.
- **No está integrado en `ensayo.py` ni `tramo.py`**, para no tocar su lógica.
- El registro del cDAQ se hace desde `/api/cdaq/registro/iniciar` y `/api/cdaq/registro/parar` y crea `<proyecto>\registros\...\cdaq.csv` y `registro.json`. Usa `almacen.destino` sin modificar `almacen.py`.

**Simulación sin chasis:**
- En la ayuda local de NI-DAQmx 26.8 y en la API C instalada **no hay forma de crear dispositivos simulados por línea de comandos**. La ayuda remite a NI MAX, y en `NIDAQmx.h` solo existen `DAQmxAddNetworkDevice` y `DAQmxDeleteNetworkDevice`.
- Crear uno en NI MAX es un cambio en la configuración de NI del PC: no se ha hecho. Por eso las pruebas usan sustitutos de `nidaqmx` y el modo simulado del panel.

---

## 1. Estructura actual del servidor

**Nota previa:** el repo **no es un repositorio git** (`git status` responde «not a git repository»). Las versiones anteriores se guardan como copias `*.antes_<motivo>_<fecha>` junto a cada fichero.

| Aspecto | Cómo está hecho | Dónde |
|---|---|---|
| Lenguaje | Python 3.12.10 (hay `__pycache__\*.cpython-312.pyc`). Solo Windows: `ctypes.WinDLL`, `tasklist`, `os.startfile` | — |
| Dependencias | `fastapi`, `uvicorn`, `pyvisa` (osciloscopio por HiSLIP), `pyserial` (fuente, importado de forma diferida), `h5py`, `numpy`. No hay `requirements.txt` | `servidor.py:16-19`, `osciloscopio.py:20-21`, `fuente.py:99` |
| Punto de entrada | `arrancar_panel.bat` → `python servidor.py`. En `servidor.py:747-752` se ejecuta `uvicorn.run(app, host="127.0.0.1", port=8765)`. Los instrumentos se crean al importar el módulo | `servidor.py:33-76` |
| Instrumentos | **La fuente es una EA-PS 9500-20 1U por USB/COM virtual, no una Siglent SPD3303X** (`fuente.py:1`, `servidor.py:1`, `README.md`). El osciloscopio es un R&S MXO34. Las temperaturas vienen de un Pico TC-08 (`tc08.py`) o de la base LevelDB de PicoLog (`picolog.py`) | — |
| Clase base | **No existe.** Hay una interfaz implícita (*duck typing*) entre `TC08` y `PicoLog` («misma forma que picolog.PicoLog», `tc08.py:263`) | — |
| Patrón común | `lock` (RLock), `estado` y `mensaje` en texto, hilo daemon `_bucle` con `threading.Event` `_parar`, historia en `deque` de 2 h (`HISTORIA_S = 7200`), `grab` (fichero abierto o `None`), `resumen()` para `/api/estado` y `reg.anotar(autor, equipo, accion, detalle)` en cada cambio | `tc08.py:51-262` |
| Registro en el servidor | `tc = TC08(reg, cfg["temperaturas"], guardar_cfg)`. El proxy `_Temperaturas.activa()` devuelve `tc` si está midiendo o grabando y, si no, `pl`. Solo admite **una** fuente de temperaturas activa a la vez. `tramos`, `ensayos.pl` y `ensayos.tc` usan ese proxy | `servidor.py:60-76` |
| Sondeo | Solo `threading`, sin asyncio propio: FastAPI ejecuta los endpoints síncronos en su pool de hilos. Fuente: 0,1 s. Osciloscopio: 1 s, más ping cada 3 s. PicoLog: 1 s. TC-08: `wait(0.02)`. No hay `queue`: los datos se comparten en `deque` protegidos con lock | `fuente.py:462`, `osciloscopio.py:457`, `tc08.py:223` |
| Sellado de tiempo | **`time.time()`** (época Unix, reloj del PC) en todas las muestras. El TC-08 usa `self._t0 = time.time()` al arrancar más los ms que devuelve la DLL. `monotonic()` solo se usa para un tiempo mínimo del osciloscopio | `fuente.py:486`, `tc08.py:198,240` |
| Datalogger | Raíz `almacen.BASE = C:\Proyectos\Osciloscopio`. `destino()` genera `AAAAMMDD_HHMM_nombre`. Se escriben `fuente.csv` (columnas en `fuente.py:86`), `temperaturas.csv` (`fecha_hora, t_unix_s, t_rel_s, <nombre>_C…`, cabecera `#`, `flush` por lote, pre-registro de `pre_s`) y `temperaturas.picolog`. El osciloscopio escribe `osc.h5` (HDF5) mediante un datalogger externo. La auditoría va a `registro\cambios_AAAAMMDD.jsonl` | `almacen.py:13,84`, `tc08.py:286-343`, `registro.py` |
| Configuración | `config.json` con los bloques `osciloscopio`, `fuente` y `temperaturas` (este último con `intervalo_ms`, `red_hz`, `en_uso` y `canales{n: activo, nombre, tipo, limite_C}`). Se reescribe entero desde `guardar_cfg()`. Hay además constantes en el código (`tc08.py:27-33`, `fuente.py:68-89`) | `servidor.py:55` |
| Errores | Fuente: tras 5 fallos pasa a `sin_comunicacion` y llama a `_reabrir()`. Osciloscopio: reconecta cuando el puerto 4880 acepta. TC-08: tras 20 fallos pasa a `error` y **no reconecta solo**. En la API, `RuntimeError`/`ValueError` dan 409 y el resto 500 | `fuente.py:504-512`, `tc08.py:241-244`, `servidor.py:99-106` |
| API/UI | FastAPI en 127.0.0.1:8765, solo con *polling* (sin websockets ni SSE). `GET /api/estado` se consulta cada 1 s. `static/vivo.html` pide temperaturas cada 100 ms. Todo POST exige la cabecera `X-Autor`. Rutas de temperatura: `/api/tc08/{tomar,soltar,configurar}`, `/api/temperaturas/registro/{iniciar,parar}` y `/api/temperaturas/historia`; la respuesta tiene la forma `{canales:[…], datos:{id:[[t,v]…]}}` | `servidor.py:82-` |
| Pruebas | `pruebas\lanzar_pendientes.py` lee `pendientes.json` y lanza las pruebas según los equipos visibles. `banco_sin_equipos.py` simula el servidor en el puerto 8766 | `pruebas\` |
| NI/LabVIEW | **Ninguna** mención a NI, nidaqmx, cDAQ ni LabVIEW en los ficheros vivos | — |

`tc08.py` es la plantilla natural para el cDAQ. Su ciclo es `tomar` → `_arrancar` → `_bucle` → `_guardar_muestras` → `resumen`/`historia_desde`/`ficha` → `iniciar_registro`/`parar_registro`, y `configurar` valida los cambios y reinicia la medida.

---

## 2. Alcance del hardware

**Leyenda de origen:** **L** = documentación local; **W** = web; **L+W** = ambas, y coinciden salvo que se indique otra cosa.

### 2.1 NI 9213 (16 termopares)

| Concepto | Valor | Fuente | Origen |
|---|---|---|---|
| Canales | 16 termopares, más 1 canal interno de autozero y 1 de CJC | [L-213S] | L |
| Nombres físicos en DAQmx | `cDAQ1ModN/ai0` a `ai15`. Canales internos `_aignd_vs_aignd` (autozero) y `_cjtemp` (CJC) | [L-HLP] `cdaqphychannels`, `9211interchan` | L |
| ADC | Delta-sigma, 24 bits | [L-213S]; la ficha web dice «24 bits» | L+W |
| Muestreo | Escaneado (multiplexor y un único ADC) | [L-213S], [L-213G] | L |
| Rango de tensión | ±78,125 mV | [L-213S]; la ficha web coincide | L+W |
| Tipos de termopar | El texto de rangos lista «J, K, T, E, N, B, R, S» (NIST). La tabla de sensibilidad y la gráfica de errores incluyen además **C y A** (IEC 60584-1:2013). La revisión D (oct 2026) dice literalmente «Added thermocouple type C and A errors graph». El driver 26.8 y `nidaqmx` tienen las constantes `A_Type_TC = 16208` y `C = 16209` | [L-213S], [L-H], [W-PY] `constants.py` | L+W |
| Modo alta resolución | 55 ms de conversión por canal; 1 S/s con todos los canales. Ancho de banda 14,4 Hz. Ruido 200 nV RMS. Rechazo de 50/60 Hz de 60 dB | [L-213S] | L |
| Modo alta velocidad | 740 µs por canal; **75 S/s con todos los canales** (incluidos autozero y CJC, según la nota 2). Ancho de banda 78 Hz. Ruido 7 µV RMS. La ficha web dice «hasta 75 S/s por canal» | [L-213S], [W-SHOP] | L+W |
| Máximo con menos canales | «1/(tiempo de conversión × nº de canales), o 100 S/s, lo que sea menor». Muestrear más deprisa puede degradar la exactitud | [L-213S] nota 1 | L |
| Máximo según el driver | «El máximo de 100 S/s del 9213/9214 aplica a tareas con 13 canales AI o menos por módulo». El máximo real es `AI.MaxMultiChanRate / nº de canales`; ejemplo de la ayuda: 14 canales con 1360,54 dan 97,18 S/s. También puede consultarse `SampClk.MaxRate` | [L-HLP] `cdaqtimingconsiderations` | L (ver §2.4, D3) |
| Modo por defecto en cDAQ | `AI.ADCTimingMode` = **High Resolution** por defecto en CompactDAQ. Hay que fijar High Speed explícitamente | [L-HLP] | L |
| Sensibilidad de medida | Alta resolución: J, K, T, E, N < 0,02 °C; B, R, S < 0,15 °C; C, A < 0,05 °C. Alta velocidad: J, K, T, E < 0,25 °C; N < 0,35 °C; B < 1,2 °C; R, S < 2,8 °C; C, A < 1 °C | [L-213S] tabla 11 (*) | L |
| Error de ganancia | Típico a 25 °C: 0,03 % (alta resolución) / 0,04 % (alta velocidad). Máximo a 1 año entre −40 y 70 °C: 0,15 % / 0,16 % | [L-213S] | L |
| Error de offset | Alta resolución: 4 µV típico / 6 µV máximo. Alta velocidad: 14 µV / 17 µV. Hay que sumar 0,05 µV/Ω si la impedancia de fuente supera 50 Ω | [L-213S] | L |
| Exactitud de la CJC | 0 a 70 °C: 0,8 °C típico / 1,7 °C máximo. −40 a 70 °C: 1,1 °C / 2,1 °C | [L-213S] | L |
| Modo común | Canal-COM ±1,2 V; COM-tierra ±250 V. El módulo mide el modo común de cada canal y avisa por software si se sale del rango | [L-213S], [L-213G], [L-HLP] | L |
| Protección y entrada | Sobretensión ±30 V entre dos entradas cualesquiera. Impedancia diferencial 78 MΩ. Corriente de entrada 50 nA | [L-213S] | L |
| Aislamiento | Canal-canal: **ninguno** (COM común). Canal-tierra: 250 V RMS CAT II continuo. Prueba de rigidez: 2300 V RMS (conector negro) / 3000 V RMS (negro y naranja) | [L-213S]; la ficha web dice 250 Vrms | L+W |
| Consumo | 490 mW máximo | [L-213S] | L |

(*) La tabla 11 sale desordenada al extraer el texto del PDF. El emparejamiento fila-valor de arriba sigue el orden de lectura del PDF y es coherente con la fila K. Conviene confirmarlo mirando el PDF (pág. 9).

**CJC, autozero y termopar abierto en el 9213:**
- **CJC:** sensor interno. DAQmx lo lee «una vez por flanco del reloj de muestreo» ([L-HLP] `cjcchannel`). Los gradientes térmicos en las bornas, por ejemplo con módulos vecinos calientes, causan error ([L-213G] «Cold-Junction Accuracy»). El ejemplo oficial en Python usa `CJCSource.CONSTANT_USER_VALUE` con 25 °C, y es también el **valor por defecto** de `add_ai_thrmcpl_chan` (junto con el tipo J y el rango 0-100 °C) ([W-PY] `_ai_channel_collection.py:1762`). Para el 9213 hay que pasar explícitamente la CJC interna (`CJCSource.BUILT_IN`). *Pendiente de verificar con dispositivo simulado que DAQmx la acepta en el 9213.*
- **Autozero:** es opcional, pero **las especificaciones suponen autozero en cada muestra** ([L-213G]). En DAQmx es `ai_auto_zero_mode` = `AutoZeroType.EVERY_SAMPLE` ([W-PY]). *No he encontrado su valor por defecto en el 9213:* hay que leerlo del canal y fijarlo explícitamente.
- **Termopar abierto (OTD):** cada canal tiene una fuente de corriente que, si el termopar está abierto, lleva la entrada a fondo de escala ([L-213G]). En DAQmx se detecta con `OpenThrmcplChansExist`/`OpenThrmcplChans` (`in_stream.open_thrmcpl_chans_exist` y `open_thrmcpl_chans` en Python), que indican los canales abiertos desde la última consulta ([L-HLP] `openthermdet`, [W-PY]). Esa corriente produce un pequeño error de *lead offset* con cables largos y finos. Según la ayuda, la OTD **solo se puede desactivar en el NI 4353 y el NI 9214, no en el 9213**.
- **Modo común fuera de rango:** `CommonModeRangeErrorChansExist`/`…Chans` ([L-HLP]). Un canal flotante o sin conectar no excede el rango.

**Qué NO hace el 9213:** no muestrea de forma simultánea, no tiene aislamiento canal-canal, no admite más de ±1,2 V de modo común, no pasa de 100 S/s, y **no permite repartir sus canales entre dos tareas** (§3).

### 2.2 NI 9201 (8 entradas ±10 V)

| Concepto | Valor | Fuente | Origen |
|---|---|---|---|
| Canales | 8 single-ended (0 diferenciales), referidos a COM | [L-201S], [L-201D], [W-SHOP] | L+W |
| Nombres físicos | `cDAQ1ModN/ai0` a `ai7` | [L-HLP] `cdaqphychannels` | L |
| ADC | SAR de 12 bits, uno solo con multiplexor («scanned, buffered… sampled by a single ADC»). DAQmx lo trata como *C Series Scanned device* | [L-201S], [L-201D], [L-HLP] | L |
| Simultáneo | **No** ([L-201D] tabla comparativa; [W-SHOP] «Simultáneo: No»). Ojo: la misma datasheet 373783A-02 incluye la frase «Each AI channel provides an independent signal path and ADC, enabling you to sample all channels simultaneously», que **contradice** a la línea anterior y a la tabla. Parece un error de copia; mandan las especificaciones de 2026 y la ayuda de DAQmx | [L-201D] pág. 4 | L+W |
| Velocidad (agregada) | **Chasis de expansión R Series:** 475 kS/s en un canal y 475 kS/s escaneando. **Resto de chasis (incluido el cDAQ-9178):** **800 kS/s en un canal** y **500 kS/s escaneando** | [L-201S] tabla 3, [L-201D] tabla 1 | L (W: «500 kS/s») |
| Tabla específica del cDAQ-9178 | **No existe.** Ni las especificaciones del 9201 ni las del 9178 dan una tabla propia: el 9178 cae en «All Other Chassis». [W-9178] solo dice «Maximum sample rate: Determined by the C Series I/O module». Por defecto DAQmx añade **10 µs de margen entre canales** y espacia el reloj de conversión si la velocidad no lo permite. La conversión puede fijarse con `AIConv.Rate` y `ActiveDevs` | [L-201S], [W-9178], [L-UM] cap. 2 «Scanned Modules», [L-HLP] | L+W |
| Rango | ±10 V nominal (mínimo ±10,3 V, típico ±10,53 V, máximo ±10,8 V) | [L-201S] | L |
| Exactitud calibrada | Ganancia: ±0,04 % típico / ±0,25 % máximo. Offset: ±0,07 % / ±0,25 % del rango (10,53 V). Deriva: 34 ppm/°C y 100 µV/°C | [L-201S] | L |
| Ruido | 0,7 LSB RMS; 5 LSB pico a pico. DNL −0,9 a 1,5 LSB; INL ±1,5 LSB | [L-201S] | L |
| Ancho de banda e impedancia | 690 kHz mínimo (−3 dB); 1 MΩ y 5 pF. Diafonía −75 dB a 10 kHz. Establecimiento 2 µs | [L-201S] | L |
| Sobretensión | Protección de **±100 V** canal-COM. Pero el **límite de seguridad de trabajo es ±60 V DC** canal-COM | [L-201S] | L |
| Aislamiento | Canal-canal: ninguno. Canal-tierra: 250 V RMS CAT II (bornas de tornillo o muelle) o 60 V DC CAT I (DSUB) | [L-201S]; la ficha web coincide | L+W |
| Consumo | 1 W máximo | [L-201S] | L |

**Qué NO hace el 9201:** no hace muestreo simultáneo (hay desfase entre canales), no tiene entrada diferencial, no ofrece rangos menores de ±10 V (12 bits sobre ±10,53 V da unos 5,1 mV/LSB, cálculo propio), no tiene aislamiento canal-canal y no admite medir por encima de ±10,8 V.

### 2.3 Chasis cDAQ-9178

| Concepto | Valor | Fuente | Origen |
|---|---|---|---|
| Bus | USB 2.0 Hi-Speed (480 Mb/s). Si se usa un hub, debe tener alimentación externa | [W-9178], [L-UM] | L+W |
| Alimentación | 9 a 30 V, 15 W máximo (incluye hasta 1 W de módulo por ranura). Fuente NEC Class 2 | [W-9178]; la serigrafía del chasis en [L-UM] dice «9-30 V 15 W MAX» | L+W |
| Motores de temporización AI | **3: `ai`, `te0` y `te1`.** Hasta 3 tareas AI simultáneas | [L-UM] cap. 2; [L-HLP] coincide | L |
| Flujos de datos | 7 flujos independientes: «hasta siete tareas simultáneas temporizadas por hardware» (AI, AO, contadores, DIO) | [L-UM] STC3, [W-9178] | L+W |
| Contadores | 4 de 32 bits, con bases de 80 MHz, 20 MHz y 100 kHz | [L-UM], [W-9178] | L+W |
| Temporización AI | FIFO de 127 muestras por ranura. Exactitud 50 ppm de la frecuencia de muestreo. Resolución 12,5 ns | [W-9178] | W |
| Otros | Funcionamiento de −20 a 55 °C; IP30. **No admite modo reposo** de los módulos. Al quitar el USB, el chasis puede devolver los módulos a su estado de encendido | [W-9178] | W |

### 2.4 Datos a verificar y discrepancias

| # | Dato recibido | Resultado |
|---|---|---|
| D1 | cDAQ-9178: USB 2.0 Hi-Speed, 9-30 V, 15 W | **Coincide** ([W-9178], [L-UM]) |
| D2 | 3 motores AI (`ai`, `te0`, `te1`), hasta 7 tareas de reloj hardware, 4 contadores | **Coincide, con un matiz:** las 7 son flujos de datos de cualquier tipo. **Para AI el límite es 3 tareas.** Además solo hay 1 tarea AO, 1 DI y 1 DO temporizadas por hardware por chasis ([L-HLP]) |
| D3 | 9213: 75 S/s con todos los canales, 100 S/s máximo | **SIN RESOLVER hasta medirla** (decisión 4; plan de laboratorio). Coincide con [L-213S]. La ayuda del driver añade que los 100 S/s solo aplican con 13 canales o menos, y da la fórmula `MaxMultiChanRate/N`. Con el ejemplo de la ayuda (1360,54), 16 canales darían unos 85 S/s, **no 75**. Las dos fuentes no cuadran exactamente. Hay que medirlo con la API (`ai_max_multi_chan_rate`, `samp_clk_max_rate`) en un dispositivo simulado o real |
| D4 | 9213: tipos J K T E N B R S, más C y A en la revisión de oct 2026 | **Parcial.** La revisión D (378914D-01, oct 2026) dice «Added thermocouple type C and A **errors graph**», y C y A aparecen en la tabla de sensibilidad. Pero la línea de rangos de la misma especificación sigue listando solo los 8 tipos NIST. El driver 26.8 sí tiene los tipos A y C. **Por verificar con el driver y el 9213 real** (decisión 4) |
| D5 | 9213: 55 ms y 740 µs por canal; sensibilidad del tipo K < 0,02 °C (alta resolución) y < 0,25 °C (alta velocidad) | **Coincide** ([L-213S]) |
| D6 | 9201: 8 single-ended ±10 V, SAR de 12 bits, 500 kS/s, un solo ADC | **Coincide.** Los 500 kS/s son **agregados escaneando**; con un solo canal llega a **800 kS/s** («All Other Chassis») |
| D7 | 9201: sobretensión ±100 V | **Coincide como protección**, pero el **límite de seguridad de trabajo es ±60 V DC** canal-COM. No hay que confundirlos |
| D8 | 9201: tabla de velocidad máxima para el cDAQ-9178 | **No existe tabla específica.** Se aplica la columna «All Other Chassis» (§2.2) |
| D9 | (Contexto) «fuente Siglent SPD3303X» | **No coincide con el repo:** la fuente integrada es la **EA-PS 9500-20 1U** (`fuente.py:1`, `README.md`) |
| D10 | (Interno) Datasheet 373783A-02 | Se contradice sobre la simultaneidad (§2.2) |
| D11 | (Interno) Versiones de Python de `nidaqmx` | PyPI (1.7.0, 2026-10-26) dice `>=3.9`, igual que readthedocs. El README de GitHub (rama master) dice «CPython 3.11+». Python 3.12 vale en ambos casos |

---

## 3. Restricciones del chasis que afectan al diseño

1. **Tres tareas AI como máximo por chasis** (`ai`, `te0`, `te1`) ([L-UM], [L-HLP]). Una tarea para el 9213 y otra para el 9201 ocupan dos. Queda un motor libre, pero ningún módulo AI libre: por la decisión 1, el panel es dueño de los dos.
2. **Los canales de un módulo no se pueden repartir en varias tareas** ([L-UM] cap. 2: «channels from a single module cannot be used in multiple tasks»). Si el VI del compañero usa *un* canal del 9213, el panel no puede usar *ningún* otro canal de ese módulo mientras tanto. El conflicto no es «los mismos canales», sino **el módulo entero**.
3. **Módulos lentos y rápidos en la misma tarea:** si la tarea va más deprisa que el módulo lento (el 9213), este **repite la última muestra sin avisar** ([L-UM] «Slow Sample Rate Modules»; [L-HLP]). Además, «la primera muestra se adquiere al hacer *commit* de la tarea» y la segunda tras el disparo de arranque. El propio manual recomienda separarlos en tareas distintas, que es lo propuesto.
4. **Lectura bajo demanda también reserva motor:** «On-demand analog input tasks must reserve resources from a timing engine» ([L-HLP] `timingengines`). Leer bajo demanda no evita el límite de 3 ni el conflicto con LabVIEW.
5. **Sin modo de punto único temporizado por hardware:** los C Series no admiten «hardware-timed single-point» ni «Wait for Next Sample Clock» ([L-HLP]).
6. **Reserva exclusiva:** los recursos se reservan al pasar al estado *Reserved*, y esa transición falla si otra tarea ya los tiene ([L-HLP] `reservedstate`). Textos del driver instalado ([L-H]): −50103 «The specified resource is reserved», −200022 «Resource requested by this task has already been reserved by a different task» y −201105 (lo mismo «with conflicting settings»).
7. **Conflicto con LabVIEW:**
   - La documentación habla de «tareas» sin distinguir proceso. *Deducción no verificada:* una tarea del VI del compañero bloquea igual que una nuestra, y el error esperado es uno de los del punto 6.
   - La coexistencia sería posible repartiendo los módulos, pero **Kevin ha decidido que el servidor es el único dueño** (§0.1). Si el VI está abierto con un módulo reservado, el panel queda en «ocupado» y reintenta cada 30 s. El plan de laboratorio incluye confirmar que el VI está cerrado.
   - En **este** PC no hay LabVIEW de desarrollo (no existe ningún `LabVIEW.exe`; solo runtimes 2026 y recursos). Si el VI corre en el PC del compañero con el cDAQ conectado allí, el panel **no verá el chasis**: un USB solo tiene un anfitrión (deducción). Ver la pregunta P1.
8. **Varios hilos o procesos:** en la documentación leída **no he encontrado** ninguna garantía sobre seguridad entre hilos de `nidaqmx` ni del driver (el README de GitHub tampoco la menciona). Diseño prudente: **un único hilo dueño** de todas las llamadas DAQmx dentro del proceso del panel, y los endpoints solo tocan datos protegidos por lock (como `tc08.py`).
9. **Simulación:** un dispositivo simulado se comporta igual en verificación y reserva de recursos, y devuelve un seno a fondo de escala con un 3 % de ruido. **No se puede mezclar en una tarea con dispositivos reales** ([L-HLP] `tasksimdev`, `readwritesimdev`).
10. **Desconexión USB:** DAQmx devuelve −201003 «Device cannot be accessed… no longer present / not powered» o −88705 «device is not present or is not active» ([L-H]).
11. **Rebose:** −200279 «not able to keep up with the hardware acquisition» si no se lee a tiempo; la FIFO del chasis es de 127 muestras por ranura ([W-9178]).

---

## 4. Propuesta de integración (encajada en el patrón del repo)

**Clase `CDAQ`** en un fichero nuevo, `cdaq.py`, con la misma interfaz implícita que `TC08`:
- **Estado:** `lock`, `estado` (`libre | midiendo | ocupado | desconectado | error`), `mensaje`, `canales`, `hist`, `grab`.
- **Métodos:** `tomar(autor)`, `soltar(autor)`, `cerrar()`, `configurar(nuevo, autor)`, `resumen()`, `historia_desde(desde)`, `ficha()`, `iniciar_registro(...)` y `parar_registro(...)`.
- **Interno:** `_arrancar()`, `_bucle()` y `_guardar_muestras()`.
- **Sin clase base nueva:** no la hay en el repo y crear una ahora obligaría a tocar `TC08`, `PicoLog` y `Fuente`.

**Dos tareas DAQmx** (§3, puntos 1 a 3):
- **`tarea_tc` (9213):**
  - `add_ai_thrmcpl_chan` por canal activo, con el tipo y el rango de cada canal y `cjc_source=CJCSource.BUILT_IN`.
  - `ai_auto_zero_mode = EVERY_SAMPLE` y `ai_adc_timing_mode` = `HIGH_RESOLUTION` o `HIGH_SPEED` según la configuración.
  - `cfg_samp_clk_timing(fs_tc, CONTINUOUS)` con `fs_tc` **igual o menor que el máximo real**, consultado con `samp_clk_max_rate` tras configurar. Así no se repiten muestras.
- **`tarea_v` (9201):** `add_ai_voltage_chan` (RSE, ±10 V), con `cfg_samp_clk_timing(fs_v, CONTINUOUS)` y un búfer amplio.

**Temporizado por hardware frente a bajo demanda:**
- **Recomiendo reloj de hardware continuo** en las dos tareas: da un intervalo exacto (50 ppm) y no pierde muestras.
- Bajo demanda sería más simple para el 9213 (según [L-HLP], la lectura software «siempre espera a una muestra nueva»), pero reserva motor igualmente (§3.4) y deja el intervalo a merced del sistema operativo.
- Ambas tareas se leen en **un solo hilo** cada 100-200 ms con `read(READ_ALL_AVAILABLE)` y, en cada vuelta, se comprueba `open_thrmcpl_chans_exist` y `common_mode_range_error_chans_exist`.

**Sellado de tiempo común (reloj del PC, `time.time()`, igual que el resto del panel):**
- `t_i = t_inicio + i/fs_real`, donde `t_inicio` se toma con `time.time()` justo tras `task.start()` y `fs_real` es `timing.samp_clk_rate` leído después de configurar. Es el mismo esquema que el TC-08: `_t0` más el desplazamiento que da el equipo.
- **Riesgos:**
  - La latencia del arranque (sin cuantificar en la documentación).
  - La deriva relativa entre el reloj del chasis (50 ppm, unos 0,18 s por hora en el peor caso, cálculo propio) y el del PC.
  - La primera muestra del 9213, que se adquiere al hacer *commit* (§3.3).
- **Propuesta:** hacer `commit` explícito antes de `start`, descartar la primera muestra del 9213 y anotar cada minuto la diferencia entre el tiempo calculado y `time.time()` (en un registro de diagnóstico; no se corrige en caliente salvo que lo pidas).

**Formato en el datalogger** (mismo estilo que `temperaturas.csv`):
- **`cdaq_temperaturas.csv`:** `fecha_hora, t_unix_s, t_rel_s, <nombre>_C…`, más un indicador por fila con los canales abiertos o en error. La cabecera `#` incluye el modo ADC, el autozero, la CJC, el tipo de cada canal y el número de serie.
- **`cdaq_tensiones.csv`:** `…, <nombre>_V…`, con escala opcional a unidades físicas.
- **Por encima de unos 100 S/s, HDF5** (`cdaq.h5`, con `h5py`, que ya se usa en el repo). En CSV, 8 canales a 1 kS/s ocupan del orden de cientos de MB por hora (estimación propia).
- **Historia en memoria:** la de 2 h no puede guardar el 9201 a velocidad plena. Se diezmaría para la vista (mínimo/máximo por intervalo) y la velocidad completa iría solo a fichero.
- **En un ensayo,** `ensayo.py` añadiría estos ficheros a la carpeta del ensayo, igual que hoy hace con `temperaturas.csv`.

**Configuración:**
- Bloque nuevo `"cdaq"` en `config.json`: `en_uso`; `chasis` (nombre en MAX); `modulos` localizados por **tipo de producto** (`NI 9213`, `NI 9201`) en lugar de por ranura fija, para que no dependa de `cDAQ1Mod3`.
- **`tc`:** `modo_adc` (`alta_resolucion|alta_velocidad`), `fs`, `autozero`, y `canales{n: activo, nombre, tipo, min_C, max_C, limite_C}`.
- **`v`:** `fs` y `canales{n: activo, nombre, unidad, escala: {m, b}, limite}`.
- Validación en `configurar()`, igual que en `tc08.py:132`.

**Errores y reconexión:**
- −50103, −200022 o −201105 → `estado = "ocupado"`, con el mensaje «módulo reservado por otro programa (¿VI de LabVIEW?)». **Sin reintentos agresivos:** solo cuando se pulse «Tomar» o con un sondeo lento (por ejemplo, cada 30 s) si lo prefieres.
- −201003 o −88705 → `estado = "desconectado"`. Se cierran las tareas y se reintenta cada pocos segundos comprobando que el dispositivo vuelve a aparecer en `System.local().devices`. Es la misma idea que el vigilante del osciloscopio y **mejora el TC-08, que hoy no reconecta solo**.
- −200279 → se reinicia la tarea, se anota un hueco en el CSV y se registra en `reg.anotar`.
- Termopar abierto → el canal se marca en `resumen()` y en la web, y la celda del CSV queda vacía o marcada (no se escribe el fondo de escala como si fuera una temperatura).
- La API sigue la convención actual: 409 para `RuntimeError`/`ValueError`.

**Servidor y web:**
- Endpoints `/api/cdaq/{tomar,soltar,configurar}`, `/api/cdaq/historia?desde` y `/api/cdaq/registro/{iniciar,parar}`, más una clave `cdaq` en `/api/estado`.
- **Sobre `_Temperaturas`:** hoy solo admite una fuente de temperatura. O el 9213 entra como **tercera fuente** del proxy, o se muestra **aparte** (pregunta P7).

**Pruebas sin hardware:**
- **Dispositivo simulado en NI MAX** (un cDAQ-9178 simulado con 9213 y 9201 en las ranuras reales). Lo crearías tú en NI MAX; yo no lo abro.
- Un script de enumeración y una prueba de `CDAQ` contra el simulado: tipos de producto, canales, `samp_clk_max_rate`, valores por defecto de autozero y modo ADC, si acepta CJC `BUILT_IN`, y cómo se comportan `tomar`, `soltar` y el error de reserva al abrir dos veces.
- Siguiendo la norma de `pruebas\pendientes.json`, cada prueba que requiera el chasis real quedaría como entrada pendiente (`necesita: cdaq`), y habría que añadir `cdaq` a `ve_el_panel()` en `lanzar_pendientes.py`.

**Ficheros que se crearían o tocarían (fase 2):**
- **Nuevos:** `cdaq.py` y `pruebas\prueba_cdaq_simulado.py`.
- **Modificados:** `servidor.py` (instancia, endpoints y estado), `config.json` (bloque `cdaq`), `ensayo.py` (ficheros del ensayo), `tramo.py` (si los tramos deben incluir el cDAQ), `static\index.html` y `static\vivo.html` (UI), y `pruebas\pendientes.json` y `pruebas\lanzar_pendientes.py`. Además, `pip install nidaqmx` (requiere tu permiso).
- Cada fichero modificado tendría su copia `*.antes_cdaq_<fecha>`, según la costumbre del repo.

---

## 5. Plan por pasos (sin código) y riesgos

| Paso | Contenido | Riesgo principal |
|---|---|---|
| 0 | Contestar las preguntas (§6). Instalar `nidaqmx` (1.7.0) con tu permiso. Crear en NI MAX el cDAQ-9178 simulado con 9213 y 9201 | Que el cDAQ no vaya a estar en este PC (P1), lo que cambiaría todo el enfoque |
| 1 | Script de enumeración de solo lectura con `nidaqmx` (simulado y, en el laboratorio, real): nombres, `MaxMultiChanRate`, valores por defecto (autozero, modo ADC) y CJC `BUILT_IN` | Que el simulado no reproduzca algún límite real (D3) |
| 2 | `cdaq.py` independiente, con prueba contra el simulado y sin tocar el servidor | Seguridad entre hilos no documentada → un solo hilo dueño |
| 3 | Integración en `servidor.py` y `config.json`, con endpoints y estado | Romper lo ya validado: pasar `validar_rediseno`, la regresión de seguridad y el banco |
| 4 | Ficheros del datalogger e integración en `ensayo.py` y `tramo.py` | Tamaño de los ficheros del 9201; sello de tiempo y deriva |
| 5 | UI en `index.html` y `vivo.html`, probada en Chrome | Carga de la historia del 9201 en el navegador |
| 6 | Pruebas pendientes en `pendientes.json` | — |
| 7 | **En el laboratorio, con el chasis:** enumeración real; comparar valores con el VI del compañero; desconectar un termopar (OTD); desenchufar el USB (reconexión); prueba de convivencia con el VI abierto (error esperado de reserva) | Interferir con el trabajo del compañero: coordinarlo antes |

**Riesgos transversales:**
- Conflicto con LabVIEW (§3.7).
- Repetición silenciosa de muestras si `fs` supera el máximo del 9213 (§3.3).
- Exactitud térmica: gradientes en las bornas y módulos vecinos calientes ([L-213G]).
- Límite de seguridad de ±60 V DC del 9201 frente a lo que se conecte.

---

## 6. Preguntas abiertas

**Estado tras la fase 2A:**
- **Contestadas:** P1 (sí, este PC), P2 (el panel es el único dueño) y P10 (`nidaqmx` 1.7.0 instalado; no se crean dispositivos simulados en NI MAX).
- **Pendientes y configurables con TODO_CONFIRMAR:** P3, P4 (frecuencia), P5 y P6.
- **Siguen abiertas:** P7, P8 y P9.

1. **P1.** ¿El cDAQ irá por USB a **este** PC (el del panel)? ¿El VI del compañero corre en este PC o en otro? (Aquí no hay LabVIEW de desarrollo.)
2. **P2.** ¿Cómo convivimos con el compañero: por turnos (Tomar/Soltar, como TC-08 y PicoLog), repartiendo módulos (¿quién se queda el 9213 y quién el 9201?), o el panel es el único dueño y él lee del panel?
3. **P3.** 9213: ¿cuántos canales, qué **tipo de termopar** en cada uno, nombres, rangos esperados y límites de alarma?
4. **P4.** 9213: ¿**alta resolución** (unos 1 S/s con 16 canales, mejor ruido y rechazo de 50 Hz) o **alta velocidad** (hasta 75 S/s)? ¿Qué frecuencia de registro quieres?
5. **P5.** 9201: ¿qué mide cada canal (señal, rango esperado, sonda, divisor o shunt, escala a unidades físicas)? ¿Puede alguna señal superar ±10 V, o su referencia (COM) estar lejos de tierra?
6. **P6.** 9201: ¿a qué frecuencia y durante cuánto tiempo quieres registrar? Eso decide si el formato es CSV o HDF5.
7. **P7.** ¿El 9213 sustituye al TC-08/PicoLog como «temperaturas» del panel o se muestra a la vez y por separado?
8. **P8.** ¿Los límites de temperatura o tensión deben actuar sobre la fuente (por ejemplo, apagarla), o solo avisar como hoy `limite_C`?
9. **P9.** ¿Sabes los nombres del chasis y los módulos en NI MAX del PC donde está ahora (por si están renombrados) y en qué ranura va cada módulo?
10. **P10.** ¿Me autorizas a instalar `nidaqmx` con pip en la fase 2? ¿Creas tú el dispositivo simulado en NI MAX o te paso los pasos?

---

## 7. Qué he verificado de verdad y qué no

**Verificado:**
- **Documentación local leída completa:** especificaciones y *Getting Started* del 9213, especificaciones y datasheet 373783A del 9201, y del manual 372838E los capítulos 1 y 2, el STC3 y el comienzo del capítulo 5. Del manual no leí los capítulos de AO, DIO y contadores en detalle, porque no afectan al diseño.
- **Ayuda instalada** (CHM extraídas al temporal): *Timing Considerations for C Series*, OTD, *Configurable ADC Timing*, *Common-Mode Over-Range*, canales físicos e internos del cDAQ, *Timing Engines*, *Reserved State*, CJC y simulación.
- **Web:** especificaciones del 9178 (374046A-01); PyPI, GitHub y readthedocs de `nidaqmx` (ejemplos `thrmcpl_sample.py`, `cont_thrmcpl_samples_int_clk.py` y `system_properties.py`; nombres de propiedades y constantes en `generated/nidaqmx`); fichas de producto del 9201 y del 9213; página de descargas de NI-DAQmx (última versión ofrecida: «2026 Q4»).
- **Instalación:**
  - **NI-DAQmx 26.8.0:** el registro indica 26.80.49579, instalado el 2026-11-04; `nicaiu.dll` 26.8.0f440; la API devuelve 26.8.0.
  - **NI MAX:** `C:\Program Files (x86)\National Instruments\MAX\NIMax.exe` (26.8.0f97).
  - **NI Hardware Configuration Utility:** `C:\Program Files\National Instruments\NI Hardware Configuration\NIHardwareManager.exe` («2026 Q4»).
  - **Ayuda:** `C:\Program Files (x86)\National Instruments\NI-DAQ\Docs\daqhelp.chm` (más `mxcncpts.chm`, `mxdevconsid.chm`, `measfunds.chm` y `Device_Pinouts.chm`).
  - **Ejemplos:** `C:\Users\Public\Documents\National Instruments\NI-DAQ\Examples\`, solo en ANSI C y .NET, no hay de Python. Para el 9213 sirve `DAQmx ANSI C\Analog In\Measure Temperature\Cont Thrmcpl Samples-Int Clk\ContThrmcplSamps-IntClk.c`. Para el 9201: `…\Measure Voltage\Cont Acq-Int Clk`, `Acq-Int Clk` y `…\Measure Slow Varying Signal\One Sample` (bajo demanda).
  - Los accesos directos se resolvieron con WScript.Shell sin lanzarlos.
- **Entorno:**
  - Python 3.12.10.
  - **`import nidaqmx` falla: el paquete no está instalado** (no lo he instalado).
  - Para enumerar sin instalarlo usé `nicaiu.dll` por ctypes, solo con funciones `DAQmxGetSys*`, `DAQmxGetDev*` y `DAQmxGetErrorString`, sin crear tareas. Resultado: driver 26.8.0 y **ningún dispositivo** (`DAQmxGetSysDevNames` vacío). Windows tampoco ve ningún USB de NI (VID 3923).
  - Es lo esperado: **el chasis no está conectado, porque no estás en el laboratorio**.
- **Repo:** mapa con citas. Comprobé yo mismo `fuente.py:1`, `README.md`, `servidor.py:30-80` y la estructura y el bucle de `tc08.py`. El resto de citas de línea viene de una lectura delegada (agente de solo lectura) y no lo he revisado línea a línea.

**NO verificado:**
- **Nombres reales de los módulos** (`cDAQ1ModN`), canales, rangos y modos que da la API con el hardware. Sin chasis no hay enumeración: los nombres de este informe son los **por defecto** según la ayuda.
- Máximo real del 9213 con 16 canales (D3), valor por defecto del autozero en el 9213, y que acepte CJC `BUILT_IN`.
- Comportamiento real con el VI de LabVIEW abierto: el error concreto (−50103, −200022 o −201105) es una deducción.
- Seguridad entre hilos de `nidaqmx` y del driver.
- Páginas de `ni.com/docs` (no cargaron contenido) y la referencia C en línea (usé la cabecera instalada).
- Emparejamiento exacto de la tabla de sensibilidad del 9213 para los tipos distintos de K (ver la nota (*)).
- **Documentos que faltan en la carpeta local:**
  - Especificaciones del cDAQ-9178 (374046A; obtenidas de la web, pero conviene guardarlas en local).
  - *Getting Started* u *Operating Instructions* del 9201.
  - *C Series Support in NI-DAQmx* (Info Code `rdcdaq`), citado por el manual.
  - Documentación de `nidaqmx` para Python.
  - Ayuda de NI MAX sobre dispositivos simulados.
  - La ayuda de NI-DAQmx sí está instalada (CHM), pero no está en esa carpeta.
- **Versiones de los documentos:**
  - El manual del chasis (2016), la datasheet del 9201 (2016) y las especificaciones del 9178 (2013) son **muy anteriores** al driver 26.8 (2026).
  - Las especificaciones del 9213 (oct 2026) y del 9201 (descargada en nov 2026) están al día.
