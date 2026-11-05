"""Prueba del cDAQ (cdaq.py y herramientas/cdaq_descubrir.py) SIN el chasis, en el propio proceso.

- nidaqmx sustituido: Task y System falsos (registran cada llamada y su hilo); las constantes y DaqError son las
  REALES del paquete instalado, y se comprueba que las firmas de los falsos coinciden con las reales.
- Modo simulado del panel (datos sintéticos).
- Driver real SIN dispositivos: solo se listan dispositivos (no se crea ninguna tarea).
Registro de cambios y ficheros en una carpeta temporal (%TEMP%). Salida: líneas OK / FALLO como las demás pruebas.
"""
import inspect
import os
import sys
import tempfile
import threading
import time
import traceback

PANEL = r"C:\Proyectos\_HERRAMIENTAS\Panel_laboratorio"
sys.path.insert(0, PANEL)
sys.path.insert(0, os.path.join(PANEL, "herramientas"))
TMP = tempfile.mkdtemp(prefix="cdaq_")

import registro
registro.CARPETA = os.path.join(TMP, "registro")

resultados = []


def ok(nombre, cond, detalle=""):
    resultados.append((nombre, bool(cond), detalle))
    print(("OK    " if cond else "FALLO ") + nombre + (f"  [{detalle}]" if detalle else ""), flush=True)


def esperar(cond, t=5.0):
    fin = time.time() + t
    while time.time() < fin:
        if cond():
            return True
        time.sleep(0.05)
    return cond()


# ------------------------------------------------------------- 0. con cDAQ desactivado no se carga nidaqmx
import cdaq
c0 = cdaq.CDAQ(registro.Registro(), {}, lambda: None)
ok("desactivado por defecto: estado «desactivado»", c0.estado == "desactivado", c0.estado)
ok("desactivado: nidaqmx NO se ha importado", "nidaqmx" not in sys.modules)
try:
    c0.tomar("prueba")
    ok("desactivado: tomar da error", False)
except RuntimeError as e:
    ok("desactivado: tomar da error claro", "desactivado" in str(e), str(e)[:60])
d = cdaq.config_por_defecto()
ok("config: 16 termopares tipo K y 8 tensiones", len(d["termopares"]["canales"]) == 16 and len(d["tensiones"]["canales"]) == 8
   and all(q["tipo"] == "K" for q in d["termopares"]["canales"].values()))
ok("config: TODO_CONFIRMAR en tipos, 9201 y periodo",
   any("tipo" in x for x in d["TODO_CONFIRMAR"]) and any("periodo_s" in x for x in d["TODO_CONFIRMAR"])
   and all(q["mide"] == "TODO_CONFIRMAR" for q in d["tensiones"]["canales"].values()))

# ------------------------------------------------------------- nidaqmx falso (Task y System)
import nidaqmx.constants as K
from nidaqmx.errors import DaqError
from nidaqmx.task.collections import AIChannelCollection


class Banco:
    def reset(self, con_9201=True, simulado=False, sin_nada=False):
        ch = Dev("cDAQ1", "cDAQ-9178", sim=simulado)
        self.dispositivos = [] if sin_nada else [ch, Dev("cDAQ1Mod1", "NI 9213", ch, 1, sim=simulado)] + (
            [Dev("cDAQ1Mod2", "NI 9201", ch, 2, sim=simulado)] if con_9201 else [])
        self.tareas, self.hilos = [], []
        self.fallo_start = self.fallo_read = None
        self.abiertos, self.modo_comun = set(), set()


B = Banco()


class Dev:
    def __init__(self, name, ptype, chasis=None, slot=None, sim=False):
        self.name, self.product_type, self._ch, self._slot, self.is_simulated = name, ptype, chasis, slot, sim
        self.serial_num = 0x1A2B3C

    @property
    def compact_daq_chassis_device(self):
        if self._ch is None:
            raise DaqError("Specified property is not supported by the device", -200197)
        return self._ch

    @property
    def compact_daq_slot_num(self):
        if self._slot is None:
            raise DaqError("Specified property is not supported by the device", -200197)
        return self._slot


def _hilo(que):
    B.hilos.append((threading.current_thread().name, que))


class FSystem:
    @staticmethod
    def local():
        return FSystem()

    @property
    def devices(self):
        _hilo("devices")
        return list(B.dispositivos)


class FChan:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class FColl:
    def __init__(self, t):
        self.t, self.chans = t, []

    def add_ai_thrmcpl_chan(self, physical_channel, name_to_assign_to_channel='', min_val=0.0, max_val=100.0,
                            units=K.TemperatureUnits.DEG_C, thermocouple_type=K.ThermocoupleType.J,
                            cjc_source=K.CJCSource.CONSTANT_USER_VALUE, cjc_val=25.0, cjc_channel=''):
        _hilo("add_ai_thrmcpl_chan")
        c = FChan(fisico=physical_channel, nombre=name_to_assign_to_channel, tipo=thermocouple_type, cjc=cjc_source,
                  cjc_val=cjc_val, min=min_val, max=max_val, medida="tc")
        self.chans.append(c)
        return c

    def add_ai_voltage_chan(self, physical_channel, name_to_assign_to_channel='', terminal_config=K.TerminalConfiguration.DEFAULT,
                            min_val=-5.0, max_val=5.0, units=K.VoltageUnits.VOLTS, custom_scale_name=''):
        _hilo("add_ai_voltage_chan")
        c = FChan(fisico=physical_channel, nombre=name_to_assign_to_channel, term=terminal_config, min=min_val,
                  max=max_val, medida="v")
        self.chans.append(c)
        return c


class FIn:
    def __init__(self, t):
        self.t, self._leido = t, False

    @property
    def open_thrmcpl_chans_exist(self):
        self._leido = True
        return bool(B.abiertos & {c.nombre for c in self.t.ai_channels.chans})

    @property
    def open_thrmcpl_chans(self):
        if not self._leido:
            raise DaqError("Read OpenThrmcplChansExist first", -200000)
        return sorted(B.abiertos & {c.nombre for c in self.t.ai_channels.chans})

    @property
    def common_mode_range_error_chans_exist(self):
        return bool(B.modo_comun & {c.nombre for c in self.t.ai_channels.chans})

    @property
    def common_mode_range_error_chans(self):
        return sorted(B.modo_comun & {c.nombre for c in self.t.ai_channels.chans})


class FTask:
    def __init__(self, new_task_name=""):
        _hilo("Task")
        self.ai_channels, self.in_stream = FColl(self), FIn(self)
        self.arrancada = self.cerrada = False
        B.tareas.append(self)

    def start(self):
        _hilo("start")
        if B.fallo_start:
            raise DaqError("The specified resource is reserved.", B.fallo_start)
        self.arrancada = True

    def read(self, number_of_samples_per_channel=None, timeout=10.0):
        _hilo("read")
        if B.fallo_read:
            raise DaqError("Device cannot be accessed.", B.fallo_read)
        v = [(100.0 if c.nombre in B.abiertos else 20.0 + int(c.nombre[2:])) if c.medida == "tc"
             else 1.5 + int(c.nombre[1:]) for c in self.ai_channels.chans]
        return v[0] if len(v) == 1 else v

    def close(self):
        _hilo("close")
        self.cerrada = True


def firmas_iguales(falso, real):
    a = list(inspect.signature(falso).parameters)
    b = list(inspect.signature(real).parameters)
    return a == b, f"{a} / {b}"


for nombre in ("add_ai_thrmcpl_chan", "add_ai_voltage_chan"):
    igual, det = firmas_iguales(getattr(FColl, nombre), getattr(AIChannelCollection, nombre))
    ok(f"firma del falso = firma real de nidaqmx: {nombre}", igual, "" if igual else det)
import nidaqmx
igual, det = firmas_iguales(FTask.read, nidaqmx.Task.read)
ok("firma del falso = firma real: Task.read", igual, "" if igual else det)
for p in ("open_thrmcpl_chans_exist", "open_thrmcpl_chans", "common_mode_range_error_chans_exist",
          "common_mode_range_error_chans"):
    from nidaqmx.task._in_stream import InStream
    ok(f"propiedad real existe: in_stream.{p}", isinstance(getattr(InStream, p, None), property))
from nidaqmx.task.channels import AIChannel
for p in ("ai_adc_timing_mode", "ai_auto_zero_mode"):
    ok(f"propiedad real existe: AIChannel.{p}", isinstance(getattr(AIChannel, p, None), property))
from nidaqmx.system import Device
for p in ("product_type", "compact_daq_chassis_device", "compact_daq_slot_num", "is_simulated", "serial_num"):
    ok(f"propiedad real existe: Device.{p}", isinstance(getattr(Device, p, None), property))

_importar_real = cdaq._importar_nidaqmx
cdaq._importar_nidaqmx = lambda: (FTask, FSystem, K, DaqError)


def nuevo(**cfg_extra):
    cfg = {"habilitado": True, "periodo_s": 0.2, "reintento_s": 2.0}
    cfg.update(cfg_extra)
    reg = registro.Registro()
    guardados = []
    c = cdaq.CDAQ(reg, cfg, lambda: guardados.append(1))
    return c, cfg, guardados


def activar(cfg, tc=(), v=()):
    for k, q in cfg["termopares"]["canales"].items():
        q["activo"] = int(k) in tc
    for k, q in cfg["tensiones"]["canales"].items():
        q["activo"] = int(k) in v


def limpiar(c):
    try:
        if c.grab:
            c.parar_registro("prueba")
        c._detener(espera_s=5)
    except Exception:
        traceback.print_exc()


# ------------------------------------------------------------- 1. funcionamiento normal (falso)
try:
    B.reset()
    c, cfg, g = nuevo()
    activar(cfg, tc=(0, 3, 15), v=(0, 7))
    r = c.tomar("prueba")
    ok("tomar: estado midiendo", r["estado"] == "midiendo", f"{r['estado']} {r['mensaje']}")
    ok("tomar: en_uso guardado", cfg["en_uso"] is True and g)
    ok("dos tareas, una por módulo", len(B.tareas) == 2, str(len(B.tareas)))
    mods = [{ch.fisico.split("/")[0] for ch in t.ai_channels.chans} for t in B.tareas]
    ok("ninguna tarea mezcla módulos y ningún módulo está en dos tareas",
       all(len(m) == 1 for m in mods) and len({next(iter(m)) for m in mods}) == 2, str(mods))
    tcs = B.tareas[0].ai_channels.chans
    ok("9213: canales ai0, ai3, ai15 del 9213", [x.fisico for x in tcs] == ["cDAQ1Mod1/ai0", "cDAQ1Mod1/ai3", "cDAQ1Mod1/ai15"])
    ok("9213: tipo K, CJC interna (BUILT_IN)", all(x.tipo == K.ThermocoupleType.K and x.cjc == K.CJCSource.BUILT_IN
                                                  for x in tcs))
    ok("9213: modo ADC alta resolución y autozero en cada muestra",
       all(x.ai_adc_timing_mode == K.ADCTimingMode.HIGH_RESOLUTION and x.ai_auto_zero_mode == K.AutoZeroType.EVERY_SAMPLE
           for x in tcs))
    vs = B.tareas[1].ai_channels.chans
    ok("9201: RSE, ±10 V, canales ai0 y ai7", all(x.term == K.TerminalConfiguration.RSE and x.min == -10 and x.max == 10
                                                 for x in vs) and [x.fisico for x in vs] == ["cDAQ1Mod2/ai0", "cDAQ1Mod2/ai7"])
    ok("tareas arrancadas (reservan el módulo)", all(t.arrancada for t in B.tareas))
    ok("llegan lecturas", esperar(lambda: c.lecturas >= 3), str(c.lecturas))
    h = c.historia_desde(0)
    ok("historia: valores del 9213 y 9201 (con ganancia 1)", h["datos"]["9213.ai3"][-1][1] == 23.0
       and h["datos"]["9201.ai7"][-1][1] == 8.5, str({k: v[-1] for k, v in h["datos"].items()}))
    ok("sello de tiempo = reloj del PC", abs(h["datos"]["9213.ai0"][-1][0] - time.time()) < 2)
    ok("no simulado", c.resumen()["simulado"] is False and not c.canales[0]["nombre"].startswith("SIM_"))
    fuera = {(n, q) for n, q in B.hilos if n not in ("cdaq", "cdaq-detectar")}
    ok("todas las llamadas a DAQmx desde los hilos del cDAQ", not fuera, str(fuera))
    ok("la detección del arranque solo lista dispositivos (sin tareas)",
       {q for n, q in B.hilos if n == "cdaq-detectar"} <= {"devices"})
    ok("el hilo de medida no empieza hasta que acaba la detección",
       not c._hilo_detectar or not c._hilo_detectar.is_alive())

    # termopar abierto y modo común
    B.abiertos, B.modo_comun = {"tc3"}, {"tc15"}
    ok("termopar abierto: valor vacío y aviso", esperar(lambda: c.canales[1]["aviso"] == "abierto"
                                                         and c.canales[1]["ultimo"] is None),
       str(c.canales[1]))
    ok("termopar abierto: en resumen()['abiertos']", c.resumen()["abiertos"] == ["T3"], str(c.resumen()["abiertos"]))
    ok("modo común: valor guardado con aviso", c.canales[2]["aviso"] == "modo_comun" and c.canales[2]["ultimo"] == 35.0)
    f = os.path.join(TMP, "cdaq.csv")
    c.iniciar_registro(f, "prueba", pre_s=10)
    esperar(lambda: c.grab["filas"] >= 4)
    # durante el registro: cambiar canales no; límites sí
    try:
        c.configurar({"termopares": {"canales": {"1": {"activo": True}}}}, "prueba")
        ok("registro: no deja cambiar canales", False)
    except RuntimeError:
        ok("registro: no deja cambiar canales", True)
    c.configurar({"termopares": {"canales": {"0": {"limite_C": 10}}}}, "prueba")
    ok("registro: sí deja cambiar límites (alarma activa)", esperar(lambda: "T0" in c.resumen()["alarmas"]))
    # desconexión USB en marcha y vuelta
    B.fallo_read = -201003
    ok("USB desconectado: estado «desconectado»", esperar(lambda: c.estado == "desconectado"), c.estado)
    ok("USB desconectado: tareas cerradas", all(t.cerrada for t in B.tareas))
    B.fallo_read = None
    n_tareas = len(B.tareas)
    ok("USB reconectado: vuelve a «midiendo» solo", esperar(lambda: c.estado == "midiendo", 6), c.estado)
    ok("USB reconectado: tareas nuevas", len(B.tareas) == n_tareas + 2)
    c.parar_registro("prueba")
    txt = open(f, encoding="utf-8").read()
    lineas = [l for l in txt.splitlines() if not l.startswith("#")]
    cab = lineas[0].split(",")
    ok("CSV: columnas fecha_hora, t_unix_s, t_rel_s, T0_C, T3_C, T15_C, V0_V, V7_V, avisos",
       cab == ["fecha_hora", "t_unix_s", "t_rel_s", "T0_C", "T3_C", "T15_C", "V0_V", "V7_V", "avisos"], str(cab))
    ok("CSV: todas las filas con el mismo número de columnas", all(len(l.split(",")) == len(cab) for l in lineas))
    ok("CSV: termopar abierto = celda vacía + «abierto:T3»", any("abierto:T3" in l and l.split(",")[4] == ""
                                                                  for l in lineas[1:]))
    ok("CSV: hueco por la desconexión anotado", any("hueco:" in l for l in lineas[1:]))
    ok("CSV: pre-registro (t_rel_s negativo) con varias filas", sum(1 for l in lineas[1:] if l.split(",")[2].startswith("-")) >= 2)
    ok("CSV: no dice simulado", "SIMULADOS" not in txt and "# simulado: no" in txt)
    # soltar
    r = c.soltar("prueba")
    ok("soltar: tareas cerradas y estado libre", r["estado"] == "libre" and all(t.cerrada for t in B.tareas)
       and cfg["en_uso"] is False)
    ok("tras soltar el hilo termina", not (c._hilo and c._hilo.is_alive()))
except Exception:
    ok("bloque 1 sin excepciones", False, traceback.format_exc()[-300:])
finally:
    limpiar(c)

# ------------------------------------------------------------- 2. módulo ocupado (VI de LabVIEW abierto)
try:
    B.reset()
    B.fallo_start = -50103
    c, cfg, g = nuevo()
    r = c.tomar("prueba")
    ok("ocupado: estado «ocupado» y mensaje que apunta a LabVIEW", r["estado"] == "ocupado" and "LabVIEW" in r["mensaje"],
       r["mensaje"][:80])
    ok("ocupado: lo creado se cierra", all(t.cerrada for t in B.tareas))
    n = len(B.tareas)
    time.sleep(2.5)
    ok("ocupado: no reintenta antes de 30 s", len(B.tareas) == n, f"{n} -> {len(B.tareas)}")
    B.fallo_start = -200022
    ok("clasificar -200022 y -201105 como ocupado", cdaq.BackendDAQmx.clasificar(None, DaqError("x", -200022))[0] == "ocupado"
       and cdaq.BackendDAQmx.clasificar(None, DaqError("x", -201105))[0] == "ocupado")
    ok("clasificar -88705 como desconectado", cdaq.BackendDAQmx.clasificar(None, DaqError("x", -88705))[0] == "desconectado")
    ok("clasificar otro código como error", cdaq.BackendDAQmx.clasificar(None, DaqError("x", -200077))[0] == "error")
except Exception:
    ok("bloque 2 sin excepciones", False, traceback.format_exc()[-300:])
finally:
    limpiar(c)

# ------------------------------------------------------------- 3. chasis o módulo ausente
try:
    B.reset(sin_nada=True)
    c, cfg, g = nuevo()
    r = c.tomar("prueba")
    ok("sin dispositivos: «no_disponible»", r["estado"] == "no_disponible", r["mensaje"][:80])
    B.reset(con_9201=False)
    ok("vuelve el chasis (sin 9201, con V0 activo): sigue no disponible y lo dice",
       esperar(lambda: "9201" in c.mensaje, 4), c.mensaje[:80])
    activar(cfg, tc=(0,), v=())
    c._reconfigurar.set()
    ok("sin canales del 9201: mide solo con el 9213", esperar(lambda: c.estado == "midiendo", 4), c.estado)
    ok("sin canales del 9201: una sola tarea", sum(1 for t in B.tareas if not t.cerrada) == 1)
except Exception:
    ok("bloque 3 sin excepciones", False, traceback.format_exc()[-300:])
finally:
    limpiar(c)

# ------------------------------------------------------------- 4. configurar: validaciones y reapertura
try:
    B.reset()
    c, cfg, g = nuevo()
    for nuevo_cfg, que in [({"termopares": {"canales": {"0": {"tipo": "X"}}}}, "tipo X"),
                           ({"tensiones": {"canales": {"0": {"max_V": 60}}}}, "9201 a 60 V"),
                           ({"termopares": {"canales": {"0": {"min_C": 500}}}}, "min_C > max_C"),
                           ({"termopares": {"modo_adc": "rapidisimo"}}, "modo ADC desconocido"),
                           ({"termopares": {"canales": {"16": {"activo": True}}}}, "canal 16"),
                           ({"tensiones": {"canales": {"0": {"nombre": "T0"}}}}, "nombre repetido T/V"),
                           ({"periodo_s": 0.01}, "periodo 0,01 s")]:
        try:
            c.configurar(nuevo_cfg, "prueba")
            ok(f"configurar rechaza {que}", False)
        except ValueError:
            ok(f"configurar rechaza {que}", True)
    for t in "CA":
        c.configurar({"termopares": {"canales": {"1": {"tipo": t}}}}, "prueba")
    ok("configurar acepta tipos C y A (POR VERIFICAR con el 9213 real)", cfg["termopares"]["canales"]["1"]["tipo"] == "A")
    c.tomar("prueba")
    n = len(B.tareas)
    c.configurar({"termopares": {"modo_adc": "alta_velocidad", "canales": {"2": {"activo": True, "nombre": "Horno"}}}},
                 "prueba")
    ok("reconfigurar en marcha: reabre las tareas", esperar(lambda: len(B.tareas) >= n + 2 and c.estado == "midiendo"))
    ok("reconfigurar: alta velocidad aplicada", all(x.ai_adc_timing_mode == K.ADCTimingMode.HIGH_SPEED
                                                    for x in B.tareas[-2].ai_channels.chans))
    ok("reconfigurar: canal nuevo con su nombre", any(x["nombre"] == "Horno" for x in c.canales))
    ok("tareas viejas cerradas", all(t.cerrada for t in B.tareas[:n]))
except Exception:
    ok("bloque 4 sin excepciones", False, traceback.format_exc()[-300:])
finally:
    limpiar(c)

# ------------------------------------------------------------- 5. cerrar el panel
try:
    B.reset()
    c, cfg, g = nuevo()
    c.tomar("prueba")
    c.iniciar_registro(os.path.join(TMP, "cierre.csv"), "prueba")
    c.cerrar()
    ok("cerrar: tareas cerradas, registro cerrado, en_uso se mantiene",
       all(t.cerrada for t in B.tareas) and c.grab is None and cfg["en_uso"] is True)
    c2 = cdaq.CDAQ(registro.Registro(), cfg, lambda: None)
    ok("al volver a arrancar con en_uso: lo toma solo", esperar(lambda: c2.estado == "midiendo"), c2.estado)
    limpiar(c2)
except Exception:
    ok("bloque 5 sin excepciones", False, traceback.format_exc()[-300:])

# ------------------------------------------------------------- 6. dispositivo SIMULADO de NI MAX: marcado
try:
    B.reset(simulado=True)
    c, cfg, g = nuevo()
    r = c.tomar("prueba")
    ok("simulado de NI MAX: marcado simulado", r["simulado"] is True and "SIMULADO" in r["aviso"])
    ok("simulado de NI MAX: nombres SIM_ e ids sim.", all(x["nombre"].startswith("SIM_") and x["id"].startswith("sim.")
                                                         for x in c.canales))
except Exception:
    ok("bloque 6 sin excepciones", False, traceback.format_exc()[-300:])
finally:
    limpiar(c)

# ------------------------------------------------------------- 7. modo simulado del panel
try:
    c, cfg, g = nuevo(simulado=True)
    activar(cfg, tc=(0, 1), v=(0,))
    r = c.tomar("prueba")
    ok("simulado del panel: midiendo sin NI-DAQmx", r["estado"] == "midiendo" and r["simulado"] is True)
    ok("simulado del panel: aviso, nombres SIM_ e ids sim.",
       "NO SON MEDIDAS REALES" in r["aviso"] and all(x["nombre"].startswith("SIM_") and x["id"].startswith("sim.")
                                                       for x in c.canales))
    ok("simulado del panel: historia marcada", c.historia_desde(0)["simulado"] is True)
    esperar(lambda: c.lecturas >= 2)
    f = os.path.join(TMP, "cdaq_SIMULADO.csv")
    c.iniciar_registro(f, "prueba", pre_s=5)
    esperar(lambda: c.grab["filas"] >= 2)
    c.parar_registro("prueba")
    txt = open(f, encoding="utf-8").read()
    ok("simulado del panel: CSV empieza avisando", txt.startswith("# *** DATOS SIMULADOS: NO SON MEDIDAS REALES ***"))
    ok("simulado del panel: columnas SIM_", "SIM_T0_C" in txt and "SIM_V0_V" in txt)
    ok("simulado del panel: valores verosímiles", 15 < c.canales[0]["ultimo"] < 40, str(c.canales[0]["ultimo"]))
    s = cdaq.BackendSimulado(abiertos={1})
    s.abrir(cfg)
    lect = s.leer()
    ok("simulado: puede simular un termopar abierto", lect["sim.9213.ai1"][1] is None and lect["sim.9213.ai1"][2] == "abierto")
except Exception:
    ok("bloque 7 sin excepciones", False, traceback.format_exc()[-300:])
finally:
    limpiar(c)

# ------------------------------------------------------------- 8. driver REAL sin chasis (solo lista dispositivos)
cdaq._importar_nidaqmx = _importar_real
try:
    import nidaqmx.system
    hay = list(nidaqmx.system.System.local().devices)
    if hay:
        print(f"OMITIDA driver real sin chasis: NI-DAQmx ve {[d.name for d in hay]} (¿chasis conectado?)", flush=True)
    else:
        b = cdaq.BackendDAQmx()
        try:
            b._buscar(cdaq.config_por_defecto(), [(0, {})], [])
            ok("driver real sin chasis: NoDisponible", False)
        except cdaq.NoDisponible as e:
            ok("driver real sin chasis: NoDisponible con mensaje claro", "ningún dispositivo" in str(e), str(e)[:70])
        c = cdaq.CDAQ(registro.Registro(), {"habilitado": True}, lambda: None)
        ok("driver real sin chasis: al arrancar aparece «no_disponible»", esperar(lambda: c.estado == "no_disponible"),
           c.estado)
        ok("driver real sin chasis: ninguna tarea abierta", c._hilo is None)
except Exception:
    ok("bloque 8 sin excepciones", False, traceback.format_exc()[-300:])

# ------------------------------------------------------------- 9. herramienta de descubrimiento
try:
    import cdaq_descubrir as dsc

    class DevD:
        def __init__(self, name):
            self.name = name

        @property
        def product_type(self):
            return {"cDAQ1": "cDAQ-9178", "cDAQ1Mod1": "NI 9213"}[self.name]

        @property
        def serial_num(self):
            return 0x1234

        @property
        def ai_voltage_rngs(self):
            if self.name == "cDAQ1":
                raise DaqError("Specified property is not supported by the device", -200197)
            return [-0.078125, 0.078125]

        @property
        def ai_physical_chans(self):
            if self.name == "cDAQ1":
                raise DaqError("not supported", -200197)
            return type("Col", (), {"channel_names": [f"{self.name}/ai{i}" for i in range(16)]})()

        @property
        def ai_samp_modes(self):
            return [K.AcquisitionType.FINITE, K.AcquisitionType.CONTINUOUS]

    class PhysD:
        def __init__(self, name):
            self.name = name

        @property
        def ai_term_cfgs(self):
            return [K.TerminalConfiguration.DIFF]

        @property
        def ai_meas_types(self):
            raise RuntimeError("explota")

    class SysD:
        @staticmethod
        def local():
            return SysD()
        devices = [DevD("cDAQ1"), DevD("cDAQ1Mod1")]
        driver_version = type("V", (), {"major_version": 26, "minor_version": 8, "update_version": 0})()
        tasks = []

        @property
        def global_channels(self):
            raise DaqError("x", -200197)
        scales = []

    inv = dsc.descubrir(importar=lambda: (type("M", (), {"__version__": "falso"}), SysD, DevD, PhysD))
    d1, d2 = inv["dispositivos"]
    ok("descubrir: driver como 26.8.0", inv["driver"] == {"valor": "26.8.0"}, str(inv["driver"]))
    ok("descubrir: propiedad no soportada se anota con su código y sigue",
       d1["propiedades"]["ai_voltage_rngs"] == {"no_soportada": -200197, "detalle": d1["propiedades"]["ai_voltage_rngs"]["detalle"]})
    ok("descubrir: enums por nombre", d2["propiedades"]["ai_samp_modes"]["valor"] == ["FINITE", "CONTINUOUS"])
    ok("descubrir: 16 canales AI del 9213 con sus propiedades",
       len(d2["canales"]["AI"]) == 16 and d2["canales"]["AI"]["cDAQ1Mod1/ai0"]["ai_term_cfgs"]["valor"] == ["DIFF"])
    ok("descubrir: un error que no es de DAQmx tampoco aborta", "error" in d2["canales"]["AI"]["cDAQ1Mod1/ai0"]["ai_meas_types"])
    md = dsc.a_markdown(inv)
    ok("descubrir: markdown con tabla de dispositivos y aviso de serie", "| cDAQ1Mod1 |" in md and "números de serie" in md)
    ok("descubrir: real sin chasis termina limpio (0) sin escribir",
       dsc.main(["--salida", os.path.join(TMP, "desc")]) == 0 and not os.path.exists(os.path.join(TMP, "desc")))
except Exception:
    ok("bloque 9 sin excepciones", False, traceback.format_exc()[-300:])

fallos = [r for r in resultados if not r[1]]
print(f"\n{len(resultados) - len(fallos)} OK, {len(fallos)} FALLO  (temporal: {TMP})", flush=True)
sys.exit(1 if fallos else 0)
