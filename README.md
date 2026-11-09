# Panel de laboratorio

Panel web local para ver, controlar y registrar desde un solo sitio los equipos del laboratorio:

| Equipo | Conexión | Módulo |
|---|---|---|
| Osciloscopio R&S MXO34 | Ethernet (VISA HiSLIP, `mxo34-102098.local`) | `osciloscopio.py`, `web_osc.py`, `detalle_osc.py` |
| Fuente EA-PS 9500-20 | USB (COM virtual) | `fuente.py` |
| Fuente Siglent SPD3303X | USB (NI-VISA, en un proceso aparte) | `siglent.py`, `siglent_modelo.py` |
| cDAQ-9178 con NI 9213 (termopares) y NI 9201 (tensión) | USB (NI-DAQmx) | `cdaq.py`, `cdaq_modelo.py` |
| PicoLog / TC-08 (termopares) | USB | `picolog.py`, `picolog_fichero.py`, `tc08.py` |

Los equipos con driver (cDAQ, Siglent) se integran como traducción 1:1 de lo que admite el driver, validada con el
hardware: modelo + traductor + validador (`*_modelo.py` y `herramientas\*_validar_modelo.py`). El panel no inventa
opciones.

## Arrancar y cerrar
- **Arrancar:** doble clic en `arrancar_panel.bat` → http://127.0.0.1:8765 (solo escucha en este PC). Si el panel
  anterior aún se está cerrando, espera a que suelte el puerto.
- **Cerrar o reiniciar:** botón «⏻ Cerrar panel» de la cabecera (o `POST /api/cerrar`). Apaga las salidas de la
  Siglent (no tiene vigilante propio) y la EA según su opción, y deja el osciloscopio en LOCAL. No matar el proceso.
- **Requisitos:** Python 3.12 y `requirements.txt` (`python -m pip install --user -r requirements.txt`); drivers
  NI-VISA y NI-DAQmx instalados.

## Qué hace
- **Página:** osciloscopio (pantalla, canales, disparo, horizontal), apartado «Fuentes» (EA y Siglent; APAGAR de la
  cabecera apaga todas), termopares (PicoLog / TC-08) y cDAQ; casillas «en uso» de cada equipo en la cabecera;
  gráfica En vivo con todos los equipos; historial de cambios.
- **Guardar:** Capturar (un instante), Registrar (un tramo), ✂ Tramo (recortar lo ya medido) y **Ensayo**: una
  carpeta con un fichero por equipo (`fuente.csv`, `siglent.csv`, `cdaq.csv`, `temperaturas.csv`, `osc.h5`,
  `ensayo.json`) con la misma hora; entran los equipos conectados y marcados.
- **Ensayo programado** (`plan.py`): lista de etapas (consignas, rampas, mantener, pausa, captura, marca, apagar,
  esperar una condición) dirigidas a la EA o a un canal de la Siglent, con máximos por fuente y **guardas** (si una
  condición se cumple, se apaga todo y se para). Las condiciones usan cualquier medida (`ea.I`, `spd.CH1.V`,
  `cdaq.T0`, `tc.TC1`…). Planes guardados en `<proyecto>\ensayos\_planes\`.
- Los ficheros se guardan en `C:\Proyectos\Osciloscopio\<proyecto>\` y se abren con el visor MATLAB
  `C:\Proyectos\Osciloscopio\Visor_osciloscopio\osc.m`.

## Pruebas
Registro en `pruebas\pendientes.json` (qué equipos necesita cada prueba, si enciende salidas, estado e historial).
Para lanzar las pendientes con lo que haya conectado: `python pruebas\lanzar_pendientes.py` (`--lista` para ver qué
se lanzaría; `--con-salida` para las que encienden una fuente, solo con permiso). Validaciones con equipos:
`pruebas\validar_*.py`; regresiones sin equipos: `pruebas\prueba_*.py`.

## Documentación
- `C:\Proyectos\Osciloscopio\Contexto Claude\CONTEXTO_LABORATORIO.md`: resumen de todo y normas (empezar por aquí).
- `C:\Proyectos\Osciloscopio\Contexto Claude\http\README.md`: uso y funcionamiento del panel.
- `C:\Proyectos\Osciloscopio\Contexto Claude\http\CONTEXTO_CLAUDE.md`: API y normas para Claude.
- `C:\Proyectos\Osciloscopio\Contexto Claude\http\REVISION_EXTERNA_*.md` y `AUDITORIA_PENDIENTE_*.md`: revisiones
  independientes.
- `docs\`: estudio y guía del cDAQ, plan de pruebas de laboratorio y resultados de las validaciones de los modelos.
