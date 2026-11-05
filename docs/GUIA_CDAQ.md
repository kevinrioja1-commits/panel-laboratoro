# Guía rápida del cDAQ-9178 en el panel

> **Estado: NO PROBADO CON HARDWARE** (fase 2A). Todo lo de esta guía se ha probado sin el chasis: con el modo simulado del panel y con sustitutos de `nidaqmx`.
>
> Lo que falta comprobar con el chasis está en `docs/PLAN_PRUEBAS_LABORATORIO.md`. El estudio completo está en `docs/ESTUDIO_cDAQ.md`.

Los ejemplos de respuesta son **reales**: se capturaron el 2026-11-05 con el panel en modo simulado y en una carpeta temporal. Los JSON largos están recortados (`…`).

---

## 1. Modo simulado: cómo activarlo y ver sus datos

El modo simulado genera **datos sintéticos sin NI-DAQmx ni chasis**, para probar la API y el registro. **Siempre** va marcado de forma que no se pueda confundir con una medida real:

| Dónde | Marca |
|---|---|
| `/api/estado` → `cdaq`, `/api/cdaq/historia` | `"simulado": true` y `"aviso": "DATOS SIMULADOS: NO SON MEDIDAS REALES"` |
| Canales | Nombres con prefijo `SIM_` (por ejemplo `SIM_T0`) e ids con prefijo `sim.` (por ejemplo `sim.9213.ai0`) |
| Equipos | `SIM_cDAQ`, `NI 9213 (SIMULADO)`, serie `SIMULADO` |
| Registro | El fichero se llama **`cdaq_SIMULADO.csv`**, su primera línea es `# *** DATOS SIMULADOS: NO SON MEDIDAS REALES ***` y las columnas son `SIM_…` |

Un dispositivo simulado creado en NI MAX (`is_simulated`) también se marca igual. No se ha creado ninguno.

### Activarlo

**Opción A: con el panel en marcha, sin reiniciar** (PowerShell):

```powershell
$h = @{ "X-Autor" = "usuario" }
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8765/api/cdaq/configurar -Headers $h -ContentType "application/json" -Body '{"habilitado": true, "simulado": true}'
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8765/api/cdaq/tomar -Headers $h
```

**Opción B: en `config.json`, con el panel cerrado.** En la sección `"cdaq"` hay que poner `"habilitado": true` y `"simulado": true`. Después se arranca el panel y se llama a `/api/cdaq/tomar`.

> `configurar` **guarda en `config.json`**, igual que el TC-08. Para dejarlo como estaba:
>
> 1. Soltar el cDAQ: `/api/cdaq/soltar`.
> 2. Desactivarlo:
>    ```powershell
>    Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8765/api/cdaq/configurar -Headers $h -ContentType "application/json" -Body '{"habilitado": false, "simulado": false}'
>    ```
>
> Con el cDAQ tomado, no deja desactivarlo: responde «Suelta el cDAQ antes de desactivarlo».

### Ver los datos

**La interfaz web todavía no muestra el cDAQ** (ver §4). Los datos se ven así:

- **Últimos valores:** `GET /api/estado` → `cdaq.canales[*].ultimo`. También en el navegador: `http://127.0.0.1:8765/api/estado`.
- **Serie temporal (últimas 2 h en memoria):** `GET /api/cdaq/historia?desde=0`. En el navegador: `http://127.0.0.1:8765/api/cdaq/historia?desde=0`.
- **Fichero:** con `/api/cdaq/registro/iniciar` y `/api/cdaq/registro/parar`. Queda en `C:\Proyectos\Osciloscopio\<proyecto>\registros\<carpeta>\AAAAMMDD_HHMM_<nombre>\`, con `cdaq_SIMULADO.csv` (o `cdaq.csv` si es real) y `registro.json`. Se abre en Excel o en MATLAB como un CSV con cabecera `#`.

**Qué datos genera:**
- **Termopares:** unos 25 °C + 2 °C × número de canal, con una oscilación lenta de ±3 °C y un poco de ruido.
- **Tensiones:** un seno de ±5 V con periodo de 20 s, al que se aplican la `ganancia` y el `desplazamiento` de cada canal.

---

## 2. Las seis rutas `/api/cdaq/`

- Base: `http://127.0.0.1:8765`. El panel solo escucha en local.
- **Todo POST exige la cabecera `X-Autor`**; sin ella el panel responde 403.
- Si una orden no es válida o no se puede cumplir, el panel responde **409** con `{"detail": "motivo"}`.
- Abreviatura usada en los ejemplos: `$u = "http://127.0.0.1:8765"; $h = @{ "X-Autor" = "usuario" }`.

### 2.1 `POST /api/cdaq/tomar`

El panel toma el cDAQ: crea y arranca las dos tareas (9213 y 9201) en su hilo. Espera hasta 5 s al primer intento y devuelve el estado, que es lo mismo que `/api/estado` → `cdaq`.

```powershell
Invoke-RestMethod -Method Post -Uri "$u/api/cdaq/tomar" -Headers $h
```
```json
{
 "estado": "midiendo", "mensaje": "", "simulado": true,
 "aviso": "DATOS SIMULADOS: NO SON MEDIDAS REALES", "probado_con_hardware": false,
 "lectura": "bajo demanda (software)", "periodo_s": 1.0,
 "equipos": [
  {"rol": "chasis", "nombre": "SIM_cDAQ", "producto": "cDAQ-9178 (SIMULADO)", "serie": "SIMULADO", "simulado_daqmx": false},
  {"rol": "termopares", "nombre": "SIM_cDAQMod1", "producto": "NI 9213 (SIMULADO)", "serie": "SIMULADO", "ranura": 1, "simulado_daqmx": false},
  {"rol": "tensiones", "nombre": "SIM_cDAQMod2", "producto": "NI 9201 (SIMULADO)", "serie": "SIMULADO", "ranura": 2, "simulado_daqmx": false}],
 "alarmas": [], "abiertos": [],
 "canales": [
  {"id": "sim.9213.ai0", "modulo": "termopares", "canal": "ai0", "nombre": "SIM_T0", "tipo": "thermo-k", "unidad": "°C",
   "color": "#2a78d6", "limite": null, "ultimo": 24.995026193715862, "t_ultimo": 1793872469.791, "aviso": ""},
  {"id": "sim.9201.ai0", "modulo": "tensiones", "canal": "ai0", "nombre": "SIM_V0", "tipo": "tension", "mide": "TODO_CONFIRMAR",
   "unidad": "V", "color": "#e87ba4", "limite": null, "ultimo": 0.0028437859128754017, "t_ultimo": 1793872469.791, "aviso": ""}],
 "config": {…}, "lecturas": 1, "ultimo_error": null, "registro": null
}
```

**Valores posibles de `estado`:**

| Estado | Significado |
|---|---|
| `desactivado` | `habilitado: false`. `tomar` responde 409 |
| `libre` | Habilitado y sin tomar |
| `conectando` | Primer intento en curso |
| `midiendo` | Tareas arrancadas y leyendo |
| `ocupado` | Otro programa tiene el módulo (¿VI de LabVIEW?). Reintenta cada 30 s como mínimo |
| `no_disponible` | No aparece el chasis o un módulo. Reintenta cada `reintento_s` |
| `desconectado` | Se perdió el chasis en marcha (USB o alimentación). Reintenta cada `reintento_s` |
| `error` | Otro error del driver. Reintenta cada `reintento_s` |

En todos los casos de fallo, `mensaje` explica el motivo y, si lo hay, el código DAQmx.

**Avisos por canal** (campo `aviso`):
- `"abierto"`: termopar abierto; `ultimo` vale `null`. El canal también aparece en `abiertos`.
- `"modo_comun"`: el modo común está fuera de ±1,2 V; el valor se guarda.

### 2.2 `POST /api/cdaq/soltar`

Cierra las tareas DAQmx: los módulos quedan libres para otro programa. Además pone `en_uso: false`, así que el panel no lo toma solo al volver a arrancar.

```powershell
Invoke-RestMethod -Method Post -Uri "$u/api/cdaq/soltar" -Headers $h
```
```json
{"estado": "libre", "mensaje": "cDAQ libre (las tareas DAQmx están cerradas)", "simulado": true, …, "lecturas": 7, "registro": null}
```

Si hay un registro en marcha, lo rechaza:
```json
409 {"detail": "Se está registrando el cDAQ (registro o ensayo en marcha): termina antes"}
```

### 2.3 `POST /api/cdaq/configurar`

Cambia la sección `cdaq` de `config.json`. Basta con enviar lo que cambia. Si cambian canales, modos o el periodo con el cDAQ midiendo, el hilo reabre las tareas. Durante un registro **solo** deja cambiar los límites (`limite_C`, `limite`).

```powershell
Invoke-RestMethod -Method Post -Uri "$u/api/cdaq/configurar" -Headers $h -ContentType "application/json" -Body '{"termopares": {"canales": {"0": {"nombre": "Horno", "limite_C": 80}}}}'
```
```json
{"estado": "midiendo", "mensaje": "", "simulado": true, …, "lecturas": 3, …}
```
A partir de ahí, el canal 0 se llama `SIM_Horno` (en real, `Horno`) y tiene `limite: 80.0`.

Ejemplo rechazado:
```powershell
Invoke-RestMethod -Method Post -Uri "$u/api/cdaq/configurar" -Headers $h -ContentType "application/json" -Body '{"termopares": {"canales": {"0": {"tipo": "Z"}}}}'
```
```json
409 {"detail": "Tipo de termopar Z no válido (J, K, T, E, N, B, R, S, C, A)"}
```

**Claves admitidas:**
- **Generales:** `habilitado`, `simulado`, `chasis`, `periodo_s` (de 0,2 a 3600 s) y `reintento_s`.
- **`termopares`:**
  - `modulo`.
  - `modo_adc`: `alta_resolucion` o `alta_velocidad`.
  - `autozero`: `cada_muestra`, `una_vez` o `no`.
  - `cjc`: `interna` o `constante`; con `constante`, también `cjc_valor_C`.
  - `canales{"0".."15": {activo, nombre, tipo, min_C, max_C, limite_C}}`.
- **`tensiones`:** `modulo` y `canales{"0".."7": {activo, nombre, mide, unidad, min_V, max_V, ganancia, desplazamiento, limite}}`. `min_V` y `max_V` van entre −10 y 10. El valor guardado es `ganancia × V + desplazamiento`, en `unidad`.

**Reglas de nombres:** dos canales activos no pueden llamarse igual, tampoco un termopar y una tensión. Las comas y los espacios se cambian por `_`.

### 2.4 `GET /api/cdaq/historia?desde=<t_unix>`

Devuelve las muestras en memoria (últimas 2 h) posteriores a `desde`, en segundos Unix. Con `desde=0` devuelve todo. Tiene la misma forma que `/api/temperaturas/historia`. Los `null` son termopares abiertos.

```powershell
Invoke-RestMethod -Uri "$u/api/cdaq/historia?desde=0"
```
```json
{
 "simulado": true, "aviso": "DATOS SIMULADOS: NO SON MEDIDAS REALES",
 "canales": [
  {"id": "sim.9213.ai0", "nombre": "SIM_Horno", "unidad": "°C", "color": "#2a78d6", "limite": 80.0, "modulo": "termopares"},
  {"id": "sim.9201.ai0", "nombre": "SIM_V0", "unidad": "V", "color": "#e87ba4", "limite": null, "modulo": "tensiones"}],
 "datos": {
  "sim.9213.ai0": [[1793872471.802, 25.074772023956065], [1793872472.12, 25.094855186973568], [1793872473.126, 25.06279661830777]],
  "sim.9201.ai0": [[1793872471.802, 2.951438542215475], [1793872472.12, 0.008702426457931199], [1793872473.126, 1.5588337038660984]]}
}
```

### 2.5 `POST /api/cdaq/registro/iniciar`

Empieza a grabar en `<proyecto>\registros\<carpeta>\AAAAMMDD_HHMM_<nombre>\`, que contendrá `cdaq.csv` (o `cdaq_SIMULADO.csv`) y `registro.json`.

- El cuerpo es `{proyecto, carpeta, nombre, notas, pre_s}`.
- `pre_s` son los segundos que ya estaban en memoria y se añaden al principio; en el CSV salen con `t_rel_s` negativo.
- El cDAQ tiene que estar `midiendo`.

```powershell
Invoke-RestMethod -Method Post -Uri "$u/api/cdaq/registro/iniciar" -Headers $h -ContentType "application/json" -Body '{"proyecto": "PR99999-PRUEBA", "nombre": "prueba_cdaq", "notas": "ejemplo", "pre_s": 5}'
```
```json
{"ruta": "PR99999-PRUEBA\\registros\\20261105_1054_prueba_cdaq", "simulado": true}
```

Mientras graba, `/api/estado` → `cdaq.grabando` muestra esa ruta. Contenido real del CSV generado:

```
# *** DATOS SIMULADOS: NO SON MEDIDAS REALES ***
# Registro del cDAQ-9178 (NI 9213 termopares + NI 9201 tensión) manejado por el panel de laboratorio
# inicio: 2026-11-05T10:54:33
# simulado: SÍ: DATOS SIMULADOS: NO SON MEDIDAS REALES
# probado_con_hardware: NO (fase 2A)
# equipos: chasis SIM_cDAQ cDAQ-9178 (SIMULADO) serie SIMULADO; termopares SIM_cDAQMod1 NI 9213 (SIMULADO) serie SIMULADO; tensiones SIM_cDAQMod2 NI 9201 (SIMULADO) serie SIMULADO
# lectura: bajo demanda (software) cada 1 s
# termopares: modo ADC alta_resolucion, autozero cada_muestra, CJC interna
# canales: SIM_Horno = termopares ai0 thermo-k [°C]; SIM_V0 = tensiones ai0 tension [V] mide TODO_CONFIRMAR
# celdas vacías: sin dato en ese instante o termopar ABIERTO (ver columna avisos)
# t_rel_s: segundos desde el inicio; negativo = antes de empezar (lo que ya estaba en memoria)
# hora: reloj del PC (hora local) al volver la lectura; t_unix_s en segundos Unix
# notas: ejemplo
# registro: PR99999-PRUEBA\registros\20261105_1054_prueba_cdaq
fecha_hora,t_unix_s,t_rel_s,SIM_Horno_C,SIM_V0_V,avisos
2026-11-05T10:54:29.791,1793872469.791,-3.460,24.995,0.00284379,
2026-11-05T10:54:30.793,1793872470.793,-2.458,25.1377,1.55393,
…
2026-11-05T10:54:35.138,1793872475.138,1.887,25.1989,4.0782,
```

**Columna `avisos`:**
- `abierto:<canal>`: termopar abierto; su celda queda vacía.
- `modo_comun:<canal>`: modo común fuera de rango.
- `hueco:<motivo>`: fila sin datos cuando se pierde el chasis en marcha.

### 2.6 `POST /api/cdaq/registro/parar`

```powershell
Invoke-RestMethod -Method Post -Uri "$u/api/cdaq/registro/parar" -Headers $h
```
```json
{"ruta": "PR99999-PRUEBA\\registros\\20261105_1054_prueba_cdaq"}
```

Si no hay ninguna grabación en marcha, responde `409 {"detail": "No hay ninguna grabación del cDAQ en marcha"}`. Al cerrar el panel, el registro se cierra solo.

---

## 3. Cambios en `config.json` el día del laboratorio

Todo está en la sección `"cdaq"`; las demás secciones (`temperaturas` del TC-08, `fuente` y `osciloscopio`) **no se tocan**. Se edita **con el panel cerrado**, porque el panel reescribe `config.json` entero al guardar. También se puede hacer con `/api/cdaq/configurar`.

| Clave | Hoy | Ese día |
|---|---|---|
| `habilitado` | `false` | **`true`** |
| `simulado` | `false` | **`false`**: comprobarlo; si está en `true` los datos son sintéticos |
| `en_uso` | `false` | Lo pone el panel al Tomar o Soltar. No hace falta tocarlo |
| `chasis` | `""` (el primer cDAQ-9178) | El nombre de NI MAX si hay más de uno o si está renombrado (por ejemplo `"cDAQ1"`) |
| `termopares.modulo`, `tensiones.modulo` | `""` (el 9213 y el 9201 del chasis) | Solo si hiciera falta fijarlos (por ejemplo `"cDAQ1Mod1"`). Los nombres salen en NI MAX y en `herramientas\cdaq_descubrir.py` |
| `periodo_s` | `1.0`, **TODO_CONFIRMAR** | La frecuencia de registro decidida. Bajo demanda, el 9213 en alta resolución con 16 canales tarda del orden de 1 s por lectura (por medir: plan, paso 4) |
| `termopares.canales.N.activo` / `nombre` | solo `0` activo, `T0` a `T15` | Los canales cableados, con un nombre que diga qué miden |
| `termopares.canales.N.tipo` | `"K"`, **TODO_CONFIRMAR** | El tipo real de cada termopar (J, K, T, E, N, B, R o S; C y A están por verificar) |
| `termopares.canales.N.min_C` / `max_C` / `limite_C` | −20 / 400 / `null` | El rango esperado y el límite de alarma (solo avisa) |
| `termopares.modo_adc` | `alta_resolucion` | `alta_velocidad` solo si hace falta más velocidad que precisión |
| `termopares.autozero` / `cjc` | `cada_muestra` / `interna` | Dejarlos así salvo que el plan diga otra cosa |
| `tensiones.canales.N.activo` / `nombre` / `mide` | solo `0` activo; `mide` = **TODO_CONFIRMAR** | Qué mide cada canal del 9201 |
| `tensiones.canales.N.unidad` / `ganancia` / `desplazamiento` | `V` / 1 / 0 | La escala a la unidad física (sonda, divisor o shunt) |
| `tensiones.canales.N.min_V` / `max_V` / `limite` | −10 / 10 / `null` | Dentro de ±10 V. **Nunca conectar más de ±60 V DC de trabajo**; los ±100 V son solo protección |
| `TODO_CONFIRMAR` | lista de pendientes | Vaciarla cuando todo esté confirmado |

Antes de todo eso: **el VI de LabVIEW del compañero cerrado** y el chasis visible en NI MAX (plan, pasos 0 y 1).

---

## 4. Qué NO hace todavía el cDAQ

- **Interfaz web:** ni `index.html` ni `vivo.html` muestran el cDAQ. No hay botones de Tomar, Soltar ni Grabar, ni gráfica, ni aviso visual de simulado o de termopar abierto. Todo se hace por la API (§2), y los datos se ven en `/api/estado`, en `/api/cdaq/historia` o en el CSV.
- **Ensayos:** no está integrado en `ensayo.py` ni en los planes. Un ensayo **no** graba el cDAQ ni lo toma solo; hay que lanzar aparte `/api/cdaq/registro/iniciar` y `/api/cdaq/registro/parar`. Tampoco está en los tramos (`tramo.py`).
- **Temperaturas del panel:** el 9213 no entra en la sección «Temperaturas» (TC-08/PicoLog) ni en `/api/temperaturas/...`; va aparte.
- **Seguridad:** los límites (`limite_C`, `limite`) solo aparecen en `alarmas`. **No actúan sobre ningún equipo.**
- **Reloj de hardware:** solo hay lectura bajo demanda. El muestreo continuo con reloj del chasis está documentado en el estudio (§4), pero no implementado.
- **Formato:** solo CSV. No hay HDF5 ni `.picolog`.
- **Hardware:** nada se ha probado con el chasis real. Ver `docs/PLAN_PRUEBAS_LABORATORIO.md`.
