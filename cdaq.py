"""cDAQ-9178 (NI 9213 termopares + NI 9201 tensión) manejado por el panel con NI-DAQmx (paquete nidaqmx), sin LabVIEW.

    *** NO PROBADO CON HARDWARE ***  (fase 2A, 2026-11-05: escrito y probado sin el chasis, con sustitutos de
    nidaqmx y con el modo simulado del propio panel). Las comprobaciones con el chasis están en
    docs/PLAN_PRUEBAS_LABORATORIO.md. Estudio y fuentes: docs/ESTUDIO_cDAQ.md.

Mismo patrón que tc08.py (sin clase base: interfaz implícita): tomar / soltar / cerrar / configurar, resumen,
historia_desde, ficha, iniciar_registro / parar_registro. Diferencias:

  - UN SOLO HILO es dueño de todas las llamadas a DAQmx (abre, lee, cierra). Los endpoints solo cambian la
    configuración o piden parar y leen datos protegidos con self.lock. Motivo: la documentación leída no garantiza
    que nidaqmx sea seguro entre hilos (estudio §3.8).
  - Dos tareas DAQmx, una por módulo: «termopares» (NI 9213) y «tensiones» (NI 9201). Los canales de un módulo nunca
    se reparten entre tareas: un módulo solo puede estar en una tarea y queda bloqueado entero (manual 372838E cap. 2).
    Cada tarea ocupa uno de los 3 motores AI del chasis.
  - Lectura BAJO DEMANDA (temporizada por software): cada periodo_s se lee una muestra de cada canal activo. Las tareas
    se arrancan una vez (start) y quedan reservadas mientras el panel tiene el cDAQ; así otro programa no puede
    quitarle el módulo entre lecturas. Con el 9213 la lectura software «siempre espera a una muestra nueva» (ayuda
    NI-DAQmx, Timing Considerations for C Series): en alta resolución con 16 canales puede tardar ~1 s.
    OPCIÓN NO IMPLEMENTADA: reloj de hardware (cfg_samp_clk_timing, CONTINUOUS) con tiempo t0 + i/fs; ver estudio §4.
  - Sello de tiempo: time.time() (reloj del PC, como el resto del panel) justo al volver cada lectura. Con lectura
    bajo demanda el valor es el de la última conversión completada (retraso de hasta un barrido del módulo).
  - Termopar abierto: el 9213 tiene detección (OTD) que lleva la entrada a fondo de escala; DAQmx lo indica con
    in_stream.open_thrmcpl_chans_exist / open_thrmcpl_chans (hay que leer _exist antes). El valor de ese canal se
    guarda VACÍO (None) y se marca «abierto» (canal, CSV y resumen); nunca se guarda el fondo de escala como dato.
    Igual con common_mode_range_error_chans (modo común fuera de ±1,2 V): el valor se guarda, con aviso.
  - Errores: módulo reservado por otro programa (p. ej. un VI de LabVIEW) -> estado «ocupado»; chasis o módulo
    ausente / USB desconectado -> «no_disponible» o «desconectado»; se cierran las tareas y se reintenta cada
    reintento_s (ocupado: como mínimo 30 s) sin afectar al resto del panel. Otros errores del driver -> «error» y
    también se reintenta.
  - Modo simulado (cfg «simulado»: true): datos sintéticos generados aquí, sin NI-DAQmx. Marcado SIEMPRE: estado
    «simulado»: true, nombres con prefijo SIM_, ids «sim.…», aviso en el resumen y en el CSV (y el servidor llama al
    fichero cdaq_SIMULADO.csv). Un dispositivo simulado de NI MAX (is_simulated) se marca igual.
"""
import collections
import copy
import math
import random
import re
import threading
import time
import uuid

HISTORIA_S = 2 * 3600
PERIODO_MIN_S = 0.2
PERIODO_MAX_S = 3600.0
REINTENTO_MIN_S = 2.0
REINTENTO_OCUPADO_S = 30.0          # sin reintentos agresivos si el módulo lo tiene otro programa
TIMEOUT_LECTURA_S = 10.0
TIPOS_TC = "JKTENBRSCA"              # C y A: el driver 26.8 los tiene; POR VERIFICAR con el 9213 real (estudio §2.4 D4)
MODOS_ADC = {"alta_resolucion": "HIGH_RESOLUTION", "alta_velocidad": "HIGH_SPEED"}
AUTOZERO = {"cada_muestra": "EVERY_SAMPLE", "una_vez": "ONCE", "no": "NONE"}
CJC = {"interna": "BUILT_IN", "constante": "CONSTANT_USER_VALUE"}
LIMITE_9201_V = 10.0                  # rango de medida del 9201 (±10 V nominal). Trabajo seguro: ±60 V DC; ±100 V = solo protección
ERR_OCUPADO = {-50103, -200022, -201105}          # recurso reservado por otra tarea (de este u otro programa)
ERR_DESCONECTADO = {-201003, -88705, -89130}      # dispositivo no accesible / no presente
COLORES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#8a5a2b"]
AVISO_SIM = "DATOS SIMULADOS: NO SON MEDIDAS REALES"


class NoDisponible(Exception):
    """El chasis o un módulo no aparece en NI-DAQmx (no conectado, apagado, sin driver o sin nidaqmx)."""


def config_por_defecto():
    """Sección «cdaq» de config.json. Lo marcado TODO_CONFIRMAR lo tiene que confirmar el usuario."""
    return {
        "habilitado": False,          # False: el panel ni siquiera carga nidaqmx; el cDAQ sale «desactivado»
        "simulado": False,            # True: datos sintéticos (para desarrollar la interfaz y el registro)
        "en_uso": False,              # lo tenía el panel al cerrarse: al arrancar lo vuelve a tomar
        "chasis": "",                 # nombre en NI MAX (p. ej. cDAQ1); vacío = el primer cDAQ-9178 que aparezca
        "periodo_s": 1.0,             # TODO_CONFIRMAR: frecuencia de registro (lectura bajo demanda)
        "reintento_s": 10.0,
        "TODO_CONFIRMAR": [
            "periodo_s: frecuencia de registro deseada",
            "termopares.canales.*.tipo: tipo de termopar de cada canal (K por defecto)",
            "termopares.canales.*.activo/nombre/min_C/max_C/limite_C: qué canales se usan y para qué",
            "tensiones.canales.*.mide: qué mide cada canal del 9201 (y su unidad, ganancia y desplazamiento)",
        ],
        "termopares": {               # NI 9213
            "modulo": "",             # nombre en NI MAX (p. ej. cDAQ1Mod1); vacío = el 9213 del chasis
            "modo_adc": "alta_resolucion",   # alta_resolucion | alta_velocidad
            "autozero": "cada_muestra",      # cada_muestra | una_vez | no  (las especificaciones suponen cada muestra)
            "cjc": "interna",                # interna | constante
            "cjc_valor_C": 25.0,             # solo con cjc = constante
            "canales": {str(i): {"activo": i == 0, "nombre": f"T{i}", "tipo": "K", "min_C": -20.0, "max_C": 400.0,
                                 "limite_C": None} for i in range(16)},
        },
        "tensiones": {                # NI 9201
            "modulo": "",
            "canales": {str(i): {"activo": i == 0, "nombre": f"V{i}", "mide": "TODO_CONFIRMAR", "unidad": "V",
                                 "min_V": -10.0, "max_V": 10.0, "ganancia": 1.0, "desplazamiento": 0.0,
                                 "limite": None} for i in range(8)},
        },
    }


def completar(cfg, defecto=None):
    """Añade a cfg (en sitio) las claves que falten respecto a config_por_defecto(), sin tocar las que hay."""
    defecto = config_por_defecto() if defecto is None else defecto
    for k, v in defecto.items():
        if k not in cfg:
            cfg[k] = copy.deepcopy(v)
        elif isinstance(v, dict) and isinstance(cfg[k], dict):
            completar(cfg[k], v)
    return cfg


def _numero(producto):
    """'NI 9213' / 'NI-9213' / 'cDAQ-9178' -> '9213' / '9178' (el formato exacto del driver: NO PROBADO)."""
    m = re.search(r"(\d{4})", str(producto or ""))
    return m.group(1) if m else ""


def _canales_activos(cfg):
    tc = [(int(k), q) for k, q in cfg["termopares"]["canales"].items() if q.get("activo")]
    v = [(int(k), q) for k, q in cfg["tensiones"]["canales"].items() if q.get("activo")]
    return sorted(tc, key=lambda x: x[0]), sorted(v, key=lambda x: x[0])


def _descripcion_canales(cfg, prefijo):
    """Canales tal como los ve el panel (los mismos para el backend real y el simulado)."""
    tc, v = _canales_activos(cfg)
    canales = []
    for n, q in tc:
        canales.append({"id": f"{prefijo}9213.ai{n}", "modulo": "termopares", "canal": f"ai{n}", "n": n,
                        "virtual": f"tc{n}", "nombre": q["nombre"], "tipo": f"thermo-{q['tipo'].lower()}",
                        "unidad": "°C", "color": COLORES[n % 8], "limite": q.get("limite_C"),
                        "ultimo": None, "t_ultimo": None, "aviso": ""})
    for n, q in v:
        canales.append({"id": f"{prefijo}9201.ai{n}", "modulo": "tensiones", "canal": f"ai{n}", "n": n,
                        "virtual": f"v{n}", "nombre": q["nombre"], "tipo": "tension", "mide": q.get("mide", ""),
                        "unidad": q.get("unidad") or "V", "ganancia": float(q.get("ganancia", 1.0)),
                        "desplazamiento": float(q.get("desplazamiento", 0.0)), "color": COLORES[(n + 4) % 8],
                        "limite": q.get("limite"), "ultimo": None, "t_ultimo": None, "aviso": ""})
    return canales


# ====================================================================== backend real (NI-DAQmx)
def _importar_nidaqmx():
    """Separado para que las pruebas puedan sustituirlo (pruebas/prueba_cdaq_sin_hardware.py)."""
    try:
        import nidaqmx
        import nidaqmx.constants as K
        from nidaqmx.errors import DaqError
        from nidaqmx.system import System
    except Exception as e:                      # paquete no instalado o driver ausente
        raise NoDisponible(f"No se puede cargar nidaqmx ({type(e).__name__}: {e}). "
                           "Ver requirements.txt y el driver NI-DAQmx") from e
    return nidaqmx.Task, System, K, DaqError


class BackendDAQmx:
    """NO PROBADO CON HARDWARE. Crea las dos tareas bajo demanda y las lee. Solo lo usa el hilo del CDAQ."""

    def __init__(self):
        self.Task, self.System, self.K, self.DaqError = _importar_nidaqmx()
        self.tareas = {}                         # "termopares" | "tensiones" -> Task
        self.canales = []

    def _buscar(self, cfg, tc, v):
        devs = list(self.System.local().devices)
        if not devs:
            raise NoDisponible("NI-DAQmx no ve ningún dispositivo (¿cDAQ-9178 conectado por USB y alimentado?)")
        chasis = [d for d in devs if _numero(d.product_type) == "9178" and (not cfg["chasis"] or d.name == cfg["chasis"])]
        if not chasis:
            raise NoDisponible("No aparece el cDAQ-9178" + (f" «{cfg['chasis']}»" if cfg["chasis"] else "") +
                               f" (NI-DAQmx ve: {', '.join(d.name for d in devs)})")
        ch = chasis[0]
        equipos = [{"rol": "chasis", "nombre": ch.name, "producto": ch.product_type, "serie": _serie(ch),
                    "simulado_daqmx": bool(_prop(ch, "is_simulated", False))}]
        modulos = {}
        for rol, numero, hace_falta in (("termopares", "9213", tc), ("tensiones", "9201", v)):
            nombre = cfg[rol]["modulo"]
            candidatos = [d for d in devs if _numero(d.product_type) == numero and (
                d.name == nombre if nombre else _prop(d, "compact_daq_chassis_device", None) is not None
                and _prop(d, "compact_daq_chassis_device").name == ch.name)]
            if candidatos:
                m = candidatos[0]
                modulos[rol] = m.name
                equipos.append({"rol": rol, "nombre": m.name, "producto": m.product_type, "serie": _serie(m),
                                "ranura": _prop(m, "compact_daq_slot_num", None),
                                "simulado_daqmx": bool(_prop(m, "is_simulated", False))})
            elif hace_falta:
                raise NoDisponible(f"No aparece el NI {numero} ({rol})" + (f" «{nombre}»" if nombre else
                                   f" en el chasis {ch.name}"))
        return equipos, modulos

    def abrir(self, cfg):
        K = self.K
        tc, v = _canales_activos(cfg)
        equipos, modulos = self._buscar(cfg, tc, v)
        try:
            if tc:
                t = self.Task()
                self.tareas["termopares"] = t
                ct = cfg["termopares"]
                for n, q in tc:
                    c = t.ai_channels.add_ai_thrmcpl_chan(
                        f"{modulos['termopares']}/ai{n}", name_to_assign_to_channel=f"tc{n}",
                        min_val=float(q["min_C"]), max_val=float(q["max_C"]), units=K.TemperatureUnits.DEG_C,
                        thermocouple_type=K.ThermocoupleType[q["tipo"]], cjc_source=K.CJCSource[CJC[ct["cjc"]]],
                        cjc_val=float(ct["cjc_valor_C"]))
                    c.ai_adc_timing_mode = K.ADCTimingMode[MODOS_ADC[ct["modo_adc"]]]
                    c.ai_auto_zero_mode = K.AutoZeroType[AUTOZERO[ct["autozero"]]]
                t.start()                          # reserva el módulo (aquí sale el «ocupado» si lo tiene otro)
            if v:
                t = self.Task()
                self.tareas["tensiones"] = t
                for n, q in v:
                    t.ai_channels.add_ai_voltage_chan(
                        f"{modulos['tensiones']}/ai{n}", name_to_assign_to_channel=f"v{n}",
                        terminal_config=K.TerminalConfiguration.RSE,
                        min_val=float(q["min_V"]), max_val=float(q["max_V"]), units=K.VoltageUnits.VOLTS)
                t.start()
        except Exception:
            self.cerrar()
            raise
        simulado = any(e["simulado_daqmx"] for e in equipos)
        self.canales = _descripcion_canales(cfg, "sim." if simulado else "")
        return {"equipos": equipos, "canales": self.canales, "simulado": simulado,
                "aviso": "Dispositivo SIMULADO de NI MAX: " + AVISO_SIM if simulado else ""}

    def leer(self):
        """{id: (t, valor o None, aviso)}. Termopar abierto -> valor None y aviso «abierto»."""
        out = {}
        for rol, t in self.tareas.items():
            datos = t.read(timeout=TIMEOUT_LECTURA_S)
            ahora = time.time()
            mios = [c for c in self.canales if c["modulo"] == rol]
            if not isinstance(datos, list):
                datos = [datos]
            abiertos, modo_comun = set(), set()
            if rol == "termopares":
                ins = t.in_stream
                if ins.open_thrmcpl_chans_exist:        # hay que leer _exist antes que la lista (doc. de nidaqmx)
                    abiertos = set(ins.open_thrmcpl_chans)
                if ins.common_mode_range_error_chans_exist:
                    modo_comun = set(ins.common_mode_range_error_chans)
            for c, x in zip(mios, datos):
                if c["virtual"] in abiertos:
                    out[c["id"]] = (ahora, None, "abierto")
                elif c["modulo"] == "tensiones":
                    out[c["id"]] = (ahora, c["ganancia"] * float(x) + c["desplazamiento"], "")
                else:
                    out[c["id"]] = (ahora, float(x), "modo_comun" if c["virtual"] in modo_comun else "")
        return out

    def cerrar(self):
        for t in list(self.tareas.values()):
            try:
                t.close()                              # para, desreserva y libera el módulo
            except Exception:
                pass
        self.tareas = {}

    def clasificar(self, e):
        """(estado, mensaje) para una excepción del driver."""
        if isinstance(e, NoDisponible):
            return "no_disponible", str(e)
        codigo = getattr(e, "error_code", None)
        if codigo in ERR_OCUPADO:
            return "ocupado", (f"Módulo reservado por otro programa o tarea (¿VI de LabVIEW abierto?). "
                               f"DAQmx {codigo}")
        if codigo in ERR_DESCONECTADO:
            return "desconectado", f"El cDAQ no responde (¿USB desconectado o sin alimentación?). DAQmx {codigo}"
        if codigo is not None:
            return "error", f"DAQmx {codigo}: {str(e).splitlines()[0] if str(e) else ''}"
        return "error", f"{type(e).__name__}: {e}"


def _prop(obj, nombre, defecto=None):
    try:
        return getattr(obj, nombre)
    except Exception:
        return defecto


def _serie(dev):
    s = _prop(dev, "serial_num", None)
    return f"{s:X}" if isinstance(s, int) else (s or "")


# ====================================================================== backend simulado (sin NI-DAQmx)
class BackendSimulado:
    """Datos sintéticos. Todo sale marcado como SIMULADO (ids «sim.», nombres SIM_). No usa NI-DAQmx."""

    def __init__(self, abiertos=()):
        self.abiertos = set(abiertos)          # canales del 9213 que se simulan abiertos (para pruebas)
        self.canales = []
        self.t0 = time.time()

    def abrir(self, cfg):
        self.canales = _descripcion_canales(cfg, "sim.")
        equipos = [{"rol": "chasis", "nombre": "SIM_cDAQ", "producto": "cDAQ-9178 (SIMULADO)", "serie": "SIMULADO",
                    "simulado_daqmx": False},
                   {"rol": "termopares", "nombre": "SIM_cDAQMod1", "producto": "NI 9213 (SIMULADO)", "serie": "SIMULADO",
                    "ranura": 1, "simulado_daqmx": False},
                   {"rol": "tensiones", "nombre": "SIM_cDAQMod2", "producto": "NI 9201 (SIMULADO)", "serie": "SIMULADO",
                    "ranura": 2, "simulado_daqmx": False}]
        return {"equipos": equipos, "canales": self.canales, "simulado": True, "aviso": AVISO_SIM}

    def leer(self):
        ahora = time.time()
        x = ahora - self.t0
        out = {}
        for c in self.canales:
            if c["modulo"] == "termopares":
                if c["n"] in self.abiertos:
                    out[c["id"]] = (ahora, None, "abierto")
                else:
                    out[c["id"]] = (ahora, 25 + 2 * c["n"] + 3 * math.sin(2 * math.pi * x / 300 + c["n"])
                                    + random.gauss(0, 0.05), "")
            else:
                volt = 5 * math.sin(2 * math.pi * x / 20 + c["n"]) + random.gauss(0, 0.01)
                out[c["id"]] = (ahora, c["ganancia"] * volt + c["desplazamiento"], "")
        return out

    def cerrar(self):
        pass

    def clasificar(self, e):
        return "error", f"{type(e).__name__}: {e}"


# ====================================================================== el instrumento
class CDAQ:
    def __init__(self, registro, cfg, guardar_cfg, fabrica=None):
        """cfg: referencia viva a config.json -> «cdaq» (se completa con los valores por defecto que falten).
        fabrica: función cfg -> backend (las pruebas pasan sustitutos); por defecto, simulado o NI-DAQmx."""
        self.reg = registro
        self.cfg = completar(cfg)
        self._guardar = guardar_cfg
        self._fabrica = fabrica
        self.lock = threading.RLock()
        self.estado = "libre" if self.cfg["habilitado"] else "desactivado"
        # libre | conectando | midiendo | ocupado | no_disponible | desconectado | error | desactivado
        self.mensaje = "" if self.cfg["habilitado"] else "cDAQ desactivado en config.json («cdaq»: «habilitado»)"
        self.simulado = False
        self.aviso = ""
        self.equipos = []
        self.canales = []
        self.hist = {}
        self.grab = None
        self.captura = None
        self._parar = threading.Event()
        self._reconfigurar = threading.Event()
        self._primer = threading.Event()
        self._hilo = None
        self.lecturas = 0
        self.ultimo_error = None
        if self.cfg["habilitado"] and self.cfg["en_uso"]:
            self._lanzar("panel (arranque)")
        elif self.cfg["habilitado"] and not self.cfg["simulado"]:
            threading.Thread(target=self._detectar, daemon=True).start()   # solo mira si está (sin tareas)

    # -------------------------------------------------- utilidades
    def _nuevo_backend(self):
        if self._fabrica:
            return self._fabrica(self.cfg)
        return BackendSimulado() if self.cfg["simulado"] else BackendDAQmx()

    def _detectar(self):
        """Al arrancar sin «en_uso»: dice si el chasis está, sin crear tareas (solo lista dispositivos)."""
        try:
            b = self._nuevo_backend()
            if isinstance(b, BackendDAQmx):
                tc, v = _canales_activos(self.cfg)
                equipos, _ = b._buscar(self.cfg, tc, v)
                with self.lock:
                    if self.estado == "libre":
                        self.equipos = equipos
                        self.mensaje = "cDAQ visible; libre (pulsa Tomar)"
        except Exception as e:
            with self.lock:
                if self.estado == "libre":
                    self.estado, self.mensaje = "no_disponible", str(e) if isinstance(e, NoDisponible) else \
                        f"{type(e).__name__}: {e}"

    def _poner(self, estado, mensaje, anotar=True):
        cambia = estado != self.estado
        self.estado, self.mensaje = estado, mensaje
        if cambia and anotar:
            self.reg.anotar("panel (automático)", "cdaq", f"cDAQ {estado}", mensaje)

    # -------------------------------------------------- tomar / soltar
    def tomar(self, autor, esperar_s=5.0):
        """Arranca el hilo dueño del cDAQ. Devuelve el resumen tras el primer intento (o tras esperar_s)."""
        with self.lock:
            if not self.cfg["habilitado"]:
                raise RuntimeError("El cDAQ está desactivado en config.json («cdaq»: «habilitado»: true para usarlo)")
            if self._hilo and self._hilo.is_alive():
                return self.resumen()
            self.cfg["en_uso"] = True
            self._guardar()
        self.reg.anotar(autor, "cdaq", "cDAQ tomado por el panel" + (" (SIMULADO)" if self.cfg["simulado"] else ""))
        self._lanzar(autor)
        self._primer.wait(esperar_s)
        return self.resumen()

    def _lanzar(self, autor):
        with self.lock:
            self._parar.clear()
            self._reconfigurar.clear()
            self._primer.clear()
            self.estado, self.mensaje = "conectando", ""
            self._hilo = threading.Thread(target=self._bucle, name="cdaq", daemon=True)
            self._hilo.start()

    def soltar(self, autor):
        with self.lock:
            if self.grab:
                raise RuntimeError("Se está registrando el cDAQ (registro o ensayo en marcha): termina antes")
            self.cfg["en_uso"] = False
            self._guardar()
        self._detener()
        with self.lock:
            self.estado, self.mensaje = "libre", "cDAQ libre (las tareas DAQmx están cerradas)"
        self.reg.anotar(autor, "cdaq", "cDAQ soltado (módulos libres)")
        return self.resumen()

    def _detener(self, espera_s=TIMEOUT_LECTURA_S + 5):
        self._parar.set()
        h = self._hilo
        if h and h is not threading.current_thread():
            h.join(timeout=espera_s)

    def cerrar(self):
        """Al cerrar el panel: cierra las tareas (libera los módulos) sin cambiar «en_uso»."""
        try:
            if self.grab:
                self.parar_registro("panel (cierre)", "cierre del panel")
        except Exception:
            pass
        self._detener(espera_s=5)
        if self.estado not in ("desactivado",):
            self.estado = "libre"

    # -------------------------------------------------- hilo dueño de DAQmx
    def _bucle(self):
        b = None
        try:
            while not self._parar.is_set():
                if b is None:
                    b = self._abrir()
                    if b is None:                       # fallo: estado ya puesto; esperar y reintentar
                        self._primer.set()
                        espera = max(REINTENTO_MIN_S, float(self.cfg.get("reintento_s", 10)))
                        if self.estado == "ocupado":
                            espera = max(espera, REINTENTO_OCUPADO_S)
                        self._esperar(espera)
                        continue
                    self._primer.set()
                if self._reconfigurar.is_set():
                    self._reconfigurar.clear()
                    b.cerrar()
                    b = None
                    continue
                t_ciclo = time.time()
                try:
                    lect = b.leer()
                except Exception as e:
                    estado, mensaje = b.clasificar(e)
                    b.cerrar()
                    b = None
                    with self.lock:
                        self.ultimo_error = mensaje
                        self._poner(estado, mensaje)
                        self._hueco(mensaje)
                    self._esperar(max(REINTENTO_MIN_S, float(self.cfg.get("reintento_s", 10))))
                    continue
                with self.lock:
                    self.lecturas += 1
                    self._guardar_muestras(lect)
                self._esperar(float(self.cfg["periodo_s"]) - (time.time() - t_ciclo))
        finally:
            if b is not None:
                b.cerrar()
            self._primer.set()

    def _esperar(self, s):
        """Espera s segundos o hasta que se pida parar o reconfigurar."""
        fin = time.time() + max(0.0, s)
        while not self._parar.is_set() and not self._reconfigurar.is_set():
            r = fin - time.time()
            if r <= 0:
                return
            self._parar.wait(min(r, 0.1))

    def _abrir(self):
        self._reconfigurar.clear()                      # se abre con la configuración actual
        cfg = copy.deepcopy(self.cfg)
        b = None
        try:
            b = self._nuevo_backend()
            info = b.abrir(cfg)
        except Exception as e:
            estado, mensaje = (b.clasificar(e) if b else
                               ("no_disponible", str(e)) if isinstance(e, NoDisponible) else ("error", str(e)))
            if b:
                b.cerrar()
            with self.lock:
                self.ultimo_error = mensaje
                self._poner(estado, mensaje)
            return None
        with self.lock:
            viejos = self.hist
            self.canales = info["canales"]
            self.hist = {c["id"]: viejos.get(c["id"], collections.deque()) for c in self.canales}
            self.equipos = info["equipos"]
            self.simulado = bool(info["simulado"])
            self.aviso = info.get("aviso", "")
            if self.simulado:
                for c in self.canales:
                    c["nombre"] = "SIM_" + c["nombre"]
            self.captura = {"id": str(uuid.uuid4()), "inicio": time.strftime("%Y-%m-%dT%H:%M:%S"),
                            "app": "panel de laboratorio (cDAQ por NI-DAQmx, lectura bajo demanda)",
                            "simulado": self.simulado}
            self._poner("midiendo", "" if self.canales else "Ningún canal activo")
        return b

    def _guardar_muestras(self, lect):
        nuevas = {}
        for c in self.canales:
            m = lect.get(c["id"])
            if not m:
                continue
            t, v, aviso = m
            t = round(t, 3)
            h = self.hist[c["id"]]
            h.append((t, v))
            while h and h[0][0] < t - HISTORIA_S:
                h.popleft()
            c["t_ultimo"], c["ultimo"], c["aviso"] = t, v, aviso
            nuevas[c["id"]] = (t, v, aviso)
        if self.grab and nuevas:
            self._escribir([(i,) + m for i, m in nuevas.items()])

    # -------------------------------------------------- configuración
    def configurar(self, nuevo, autor):
        """Cambia la sección «cdaq». Si cambia la medida y se está midiendo, el hilo reabre las tareas.
        No se permite cambiar la medida durante un registro (cambiaría las columnas del CSV); los límites sí."""
        c = copy.deepcopy(self.cfg)
        for k in ("habilitado", "simulado"):
            if k in nuevo:
                c[k] = bool(nuevo[k])
        if "chasis" in nuevo:
            c["chasis"] = str(nuevo["chasis"]).strip()[:60]
        if "periodo_s" in nuevo:
            p = float(nuevo["periodo_s"])
            if not PERIODO_MIN_S <= p <= PERIODO_MAX_S:
                raise ValueError(f"periodo_s entre {PERIODO_MIN_S} y {PERIODO_MAX_S} s")
            c["periodo_s"] = p
        if "reintento_s" in nuevo:
            c["reintento_s"] = max(REINTENTO_MIN_S, float(nuevo["reintento_s"]))
        tcn = nuevo.get("termopares") or {}
        ct = c["termopares"]
        if "modulo" in tcn:
            ct["modulo"] = str(tcn["modulo"]).strip()[:60]
        for k, opciones in (("modo_adc", MODOS_ADC), ("autozero", AUTOZERO), ("cjc", CJC)):
            if k in tcn:
                if tcn[k] not in opciones:
                    raise ValueError(f"{k} = {tcn[k]!r} no válido ({', '.join(opciones)})")
                ct[k] = tcn[k]
        if "cjc_valor_C" in tcn:
            ct["cjc_valor_C"] = float(tcn["cjc_valor_C"])
        for k, v in (tcn.get("canales") or {}).items():
            if str(k) not in ct["canales"]:
                raise ValueError(f"Canal de termopar {k} no válido (0 a 15)")
            q = ct["canales"][str(k)]
            _nombre_activo(q, v, f"T{k}")
            if "tipo" in v:
                t = str(v["tipo"]).strip().upper()
                if len(t) != 1 or t not in TIPOS_TC:
                    raise ValueError(f"Tipo de termopar {v['tipo']} no válido ({', '.join(TIPOS_TC)})")
                q["tipo"] = t
            for k2 in ("min_C", "max_C"):
                if k2 in v:
                    q[k2] = float(v[k2])
            if q["min_C"] >= q["max_C"]:
                raise ValueError(f"Termopar {k}: min_C debe ser menor que max_C")
            if "limite_C" in v:
                q["limite_C"] = None if v["limite_C"] in (None, "") else float(v["limite_C"])
        vn = nuevo.get("tensiones") or {}
        cv = c["tensiones"]
        if "modulo" in vn:
            cv["modulo"] = str(vn["modulo"]).strip()[:60]
        for k, v in (vn.get("canales") or {}).items():
            if str(k) not in cv["canales"]:
                raise ValueError(f"Canal de tensión {k} no válido (0 a 7)")
            q = cv["canales"][str(k)]
            _nombre_activo(q, v, f"V{k}")
            for k2 in ("mide", "unidad"):
                if k2 in v:
                    q[k2] = str(v[k2]).strip().replace(",", " ")[:60]
            for k2 in ("min_V", "max_V", "ganancia", "desplazamiento"):
                if k2 in v:
                    q[k2] = float(v[k2])
            if not -LIMITE_9201_V <= q["min_V"] < q["max_V"] <= LIMITE_9201_V:
                raise ValueError(f"Tensión {k}: rango entre -{LIMITE_9201_V:g} y {LIMITE_9201_V:g} V "
                                 "(el NI 9201 solo mide ±10 V)")
            if "limite" in v:
                q["limite"] = None if v["limite"] in (None, "") else float(v["limite"])
        for sec in ("termopares", "tensiones"):
            nombres = [q["nombre"] for q in c[sec]["canales"].values() if q["activo"]]
            if len(set(nombres)) != len(nombres):
                raise ValueError(f"Dos canales activos con el mismo nombre ({sec})")
        todos = [q["nombre"] for s in ("termopares", "tensiones") for q in c[s]["canales"].values() if q["activo"]]
        if len(set(todos)) != len(todos):
            raise ValueError("Un termopar y una tensión activos con el mismo nombre (serían la misma columna)")
        cambia_medida = _medida(c) != _medida(self.cfg)
        with self.lock:
            if cambia_medida and self.grab:
                raise RuntimeError("No se pueden cambiar canales, modos ni periodo durante el registro; "
                                   "los límites sí")
            if self.estado != "desactivado" and not c["habilitado"] and self._hilo and self._hilo.is_alive():
                raise RuntimeError("Suelta el cDAQ antes de desactivarlo")
            self.cfg.clear()
            self.cfg.update(c)
            self._guardar()
            if not c["habilitado"]:
                self.estado, self.mensaje = "desactivado", "cDAQ desactivado en config.json"
            elif self.estado == "desactivado":
                self.estado, self.mensaje = "libre", ""
            if cambia_medida and self._hilo and self._hilo.is_alive():
                self._reconfigurar.set()
            elif not cambia_medida:
                for ch in self.canales:
                    q = self.cfg[ch["modulo"]]["canales"][str(ch["n"])]
                    ch["limite"] = q.get("limite_C") if ch["modulo"] == "termopares" else q.get("limite")
        self.reg.anotar(autor, "cdaq", "configuración del cDAQ", nuevo)
        return self.resumen()

    # -------------------------------------------------- consultas
    def resumen(self):
        with self.lock:
            g = self.grab
            alarmas = [c["nombre"] for c in self.canales if c.get("limite") is not None and c.get("ultimo") is not None
                       and c["ultimo"] >= c["limite"]]
            return {"estado": self.estado, "mensaje": self.mensaje, "simulado": self.simulado,
                    "aviso": self.aviso, "probado_con_hardware": False,
                    "lectura": "bajo demanda (software)", "periodo_s": self.cfg["periodo_s"],
                    "equipos": self.equipos, "captura": self.captura, "alarmas": alarmas,
                    "abiertos": [c["nombre"] for c in self.canales if c.get("aviso") == "abierto"],
                    "canales": [{k: v for k, v in c.items() if k not in ("n", "virtual", "ganancia", "desplazamiento")}
                                for c in self.canales],
                    "config": self.cfg, "lecturas": self.lecturas, "ultimo_error": self.ultimo_error,
                    "registro": None if not g else {"fichero": g["fichero"], "desde": g["t0"], "filas": g["filas"]}}

    def historia_desde(self, desde=0.0):
        """Misma forma que tc08/picolog: {canales: [...], datos: {id: [[t, v]...]}}; v = None si el termopar está abierto."""
        with self.lock:
            return {"simulado": self.simulado, "aviso": self.aviso,
                    "canales": [{"id": c["id"], "nombre": c["nombre"], "unidad": c["unidad"], "color": c["color"],
                                 "limite": c.get("limite"), "modulo": c["modulo"]} for c in self.canales],
                    "datos": {i: [p for p in h if p[0] > desde] for i, h in self.hist.items()}}

    def ficha(self):
        with self.lock:
            return {"captura": self.captura, "equipos": self.equipos, "simulado": self.simulado, "aviso": self.aviso,
                    "origen": "cDAQ-9178 manejado por el panel (NI-DAQmx, lectura bajo demanda)",
                    "probado_con_hardware": False, "periodo_s": self.cfg["periodo_s"],
                    "termopares": {k: self.cfg["termopares"][k] for k in ("modo_adc", "autozero", "cjc", "cjc_valor_C")},
                    "canales": [{k: c.get(k) for k in ("id", "modulo", "canal", "nombre", "tipo", "unidad", "mide",
                                                       "limite")} for c in self.canales]}

    # -------------------------------------------------- registro (CSV con el formato de temperaturas.csv)
    def iniciar_registro(self, fichero, autor, pre_s=30.0, cabecera=None):
        with self.lock:
            if self.grab:
                raise RuntimeError("Ya se está registrando el cDAQ")
            if self.estado != "midiendo" or not self.canales:
                raise RuntimeError("El cDAQ no está midiendo (o no hay canales activos)")
            t0 = time.time()
            f = open(fichero, "w", encoding="utf-8", newline="")
            if self.simulado:
                f.write(f"# *** {AVISO_SIM} ***\n")
            f.write("# Registro del cDAQ-9178 (NI 9213 termopares + NI 9201 tensión) manejado por el panel de laboratorio\n")
            ct = self.cfg["termopares"]
            for k, v in [("inicio", time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t0))),
                         ("simulado", "SÍ: " + AVISO_SIM if self.simulado else "no"),
                         ("probado_con_hardware", "NO (fase 2A)"),
                         ("equipos", "; ".join(f"{e['rol']} {e['nombre']} {e['producto']} serie {e['serie']}"
                                               for e in self.equipos)),
                         ("lectura", f"bajo demanda (software) cada {self.cfg['periodo_s']:g} s"),
                         ("termopares", f"modo ADC {ct['modo_adc']}, autozero {ct['autozero']}, CJC {ct['cjc']}"),
                         ("canales", "; ".join(f"{c['nombre']} = {c['modulo']} {c['canal']} {c['tipo']} [{c['unidad']}]"
                                               + (f" mide {c['mide']}" if c.get("mide") else "") for c in self.canales)),
                         ("celdas vacías", "sin dato en ese instante o termopar ABIERTO (ver columna avisos)"),
                         ("t_rel_s", "segundos desde el inicio; negativo = antes de empezar (lo que ya estaba en memoria)"),
                         ("hora", "reloj del PC (hora local) al volver la lectura; t_unix_s en segundos Unix")] + \
                    list((cabecera or {}).items()):
                f.write(f"# {k}: {v}\n")
            f.write(",".join(["fecha_hora", "t_unix_s", "t_rel_s"] + [_columna(c) for c in self.canales] + ["avisos"])
                    + "\n")
            self.grab = {"fichero": fichero, "f": f, "t0": t0, "filas": 0, "canales": [c["id"] for c in self.canales],
                         "nombres": {c["id"]: c["nombre"] for c in self.canales}}
            desde = t0 - max(0.0, float(pre_s or 0))
            previas = [(i, t, v, "abierto" if v is None else "") for i in self.grab["canales"]
                       for t, v in self.hist.get(i, ()) if t >= desde]
            if previas:
                self._escribir(previas)
        self.reg.anotar(autor, "cdaq", "registro del cDAQ iniciado",
                        {"fichero": fichero, "simulado": self.simulado, "canales": [c["nombre"] for c in self.canales]})

    def _escribir(self, muestras):
        """muestras: [(id, t, v, aviso), ...]. Una fila por instante (como temperaturas.csv); avisos al final."""
        g = self.grab
        filas = collections.defaultdict(dict)
        avisos = collections.defaultdict(list)
        for i, t, v, aviso in muestras:
            if i not in g["nombres"]:
                continue
            filas[round(t, 3)][i] = v
            if aviso:
                avisos[round(t, 3)].append(f"{aviso}:{g['nombres'][i]}")
        for t in sorted(filas):
            vals = [filas[t].get(i) for i in g["canales"]]
            hora = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t)) + f".{int(round(t % 1 * 1000)) % 1000:03d}"
            g["f"].write(f"{hora},{t:.3f},{t - g['t0']:.3f}," + ",".join("" if v is None else f"{v:.6g}" for v in vals)
                         + "," + ";".join(avisos.get(t, [])) + "\n")
            g["filas"] += 1
        g["f"].flush()

    def parar_registro(self, autor, motivo="parado"):
        with self.lock:
            g = self.grab
            if not g:
                return None
            g["f"].close()
            self.grab = None
        self.reg.anotar(autor, "cdaq", "registro del cDAQ terminado",
                        {"fichero": g["fichero"], "filas": g["filas"], "motivo": motivo})
        return g["fichero"]

    def _hueco(self, motivo):
        """Deja constancia en el CSV de que se ha perdido la comunicación (fila sin datos con el motivo)."""
        g = self.grab
        if not g:
            return
        t = time.time()
        hora = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t)) + f".{int(round(t % 1 * 1000)) % 1000:03d}"
        g["f"].write(f"{hora},{t:.3f},{t - g['t0']:.3f}," + "," * (len(g["canales"]) - 1) + ","
                     + "hueco:" + str(motivo).replace(",", " ").replace("\n", " ")[:200] + "\n")
        g["filas"] += 1
        g["f"].flush()


def _nombre_activo(q, v, defecto):
    if "activo" in v:
        q["activo"] = bool(v["activo"])
    if "nombre" in v:
        n = re.sub(r"[,;\s]+", "_", str(v["nombre"]).strip())[:40]
        q["nombre"] = n or defecto


def _medida(c):
    """Lo que cambia las tareas o las columnas del CSV (todo menos los límites)."""
    m = copy.deepcopy(c)
    for q in m["termopares"]["canales"].values():
        q.pop("limite_C", None)
    for q in m["tensiones"]["canales"].values():
        q.pop("limite", None)
    m.pop("en_uso", None)
    m.pop("TODO_CONFIRMAR", None)
    return m


def _columna(c):
    u = "C" if c["unidad"] == "°C" else re.sub(r"[^A-Za-z0-9]+", "", c["unidad"]) or "u"
    return f"{c['nombre']}_{u}"
