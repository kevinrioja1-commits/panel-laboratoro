"""cDAQ-9178 (NI 9213 termopares + NI 9201 tensión) manejado por el panel con NI-DAQmx (paquete nidaqmx), sin LabVIEW.

    PROBADO CON HARDWARE el 9-nov-2026 (fase 2B). Las opciones salen de cdaq_modelo.py: cada opción del panel es una
    traducción 1:1 de lo que admite el driver, validada con herramientas/cdaq_validar_modelo.py (85 comprobaciones)
    y contrastada con la documentación de NI (norma del usuario: el panel no se inventa otra forma de hablar con un
    equipo con driver). Plan y resultados: docs/PLAN_PRUEBAS_LABORATORIO.md, docs/cdaq_opciones_*.md.

Mismo patrón que tc08.py (sin clase base: interfaz implícita): tomar / soltar / cerrar / configurar, resumen,
historia_desde, ficha, iniciar_registro / parar_registro. Diferencias:

  - UN SOLO HILO es dueño de todas las llamadas a DAQmx (abre, lee, cierra). Los endpoints solo cambian la
    configuración o piden parar y leen datos protegidos con self.lock. Motivo: la documentación leída no garantiza
    que nidaqmx sea seguro entre hilos (estudio §3.8).
  - Dos tareas DAQmx, una por módulo: «termopares» (NI 9213) y «tensiones» (NI 9201). Los canales de un módulo nunca
    se reparten entre tareas: un módulo solo puede estar en una tarea y queda bloqueado entero (manual 372838E cap. 2).
    Cada tarea ocupa uno de los 3 motores AI del chasis.
  - Muestreo por módulo: BAJO DEMANDA (una muestra cada periodo_s, sello = reloj del PC) o con RELOJ DE HARDWARE
    (continuo o finito, a tasa_S_s; el hilo recoge lo acumulado cada 0,1 s; sello t0 + i/fs). La tasa no puede pasar
    del máximo de la regla validada (cdaq_modelo.tasa_max_9213/9201): en el 9213 el driver no avisa y repetiría
    muestras. Reloj externo y disparos digitales por PFI0/PFI1 (configuración validada; con señal real, pendiente).
    Las tareas se arrancan una vez (start) y quedan reservadas mientras el panel tiene el cDAQ.
  - Historia en memoria: un punto (la media) cada 0,1 s como mucho. CSV del registro: todas las muestras hasta
    1 kS/s; por encima, la media de cada 0,1 s, y las muestras completas en el registro TDMS del propio driver
    (preparado en pausa al abrir; al registrar se abre un fichero nuevo y se quita la pausa sin parar la tarea).
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

import cdaq_modelo as M   # 9-nov-2026: opciones validadas con el hardware (traducción 1:1 al driver; norma del usuario)

HISTORIA_S = 2 * 3600
HIST_DT_S = 0.1                       # la historia en memoria guarda como mucho un punto (media) cada 0,1 s por canal
CSV_MAX_S_S = 1000.0                  # hasta aquí el CSV lleva TODAS las muestras; por encima, la media de cada 0,1 s
TDMS_DESDE_S_S = 1000.0               # con «tdms»: «auto», el registro del driver (TDMS) se usa por encima de esta tasa
PERIODO_MIN_S = 0.05
PERIODO_MAX_S = 3600.0
CICLO_RELOJ_S = 0.1                   # con reloj de hardware, el hilo recoge lo acumulado cada 0,1 s
REINTENTO_MIN_S = 2.0
REINTENTO_OCUPADO_S = 30.0          # sin reintentos agresivos si el módulo lo tiene otro programa
TIMEOUT_LECTURA_S = 10.0
TIPOS_TC = "".join(M.TC_TIPOS)        # los 10 tipos, validados con el 9213 real el 9-nov (A y C incluidos)
MODOS_ADC = M.TC_MODO_ADC
AUTOZERO = M.TC_AUTOZERO
CJC = M.TC_CJC
LIMITE_9201_V = 10.0                  # rango de medida del 9201 (±10 V fijo). Trabajo seguro: ±60 V DC; ±100 V = solo protección
NUMERO = {"termopares": "9213", "tensiones": "9201"}
# opciones de muestreo comunes a los dos módulos (cada módulo tiene las suyas: una tarea y un ritmo por módulo)
# 9-nov (pedido del usuario): por defecto CONTINUO con reloj de hardware; tasa vacía = automática (cdaq_modelo.tasa_auto:
# 9213 la máxima de la regla × 0,98, 9201 1 kS/s)
MUESTREO_DEF = {"muestreo": "continuo", "tasa_S_s": None, "muestras": 1000, "reloj": "interno",
                "disparo": "ninguno", "disparo_fuente": "PFI0", "disparo_flanco": "subida", "pre_muestras": 100,
                "pausa_cuando": "alto", "bufer_muestras": None}
OPCIONES = {   # clave -> valores admitidos (los del modelo validado)
    "muestreo": M.MUESTREO, "reloj": M.RELOJ, "disparo": M.DISPARO, "disparo_fuente": M.FUENTE_DISPARO,
    "disparo_flanco": M.FLANCO, "pausa_cuando": M.NIVEL,
    "modo_adc": M.TC_MODO_ADC, "autozero": M.TC_AUTOZERO, "cjc": M.TC_CJC, "escala": M.TC_ESCALA,
    "unidades": M.TC_UNIDADES, "tdms": {"auto": 0, "si": 1, "no": 0}}
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
        "termopares": dict({           # NI 9213 (opciones: cdaq_modelo.py)
            "modulo": "",             # nombre en NI MAX (p. ej. cDAQ1Mod5); vacío = el 9213 del chasis
            "modo_adc": "alta_resolucion",   # alta_resolucion | alta_velocidad | automatico
            "autozero": "cada_muestra",      # cada_muestra | una_vez | no  (las especificaciones suponen cada muestra)
            "cjc": "interna",                # interna | constante
            "cjc_valor_C": 25.0,             # solo con cjc = constante
            "escala": "polinomio",           # polinomio | tabla (tabla: B desde 0 °C)
            "unidades": "C",                 # C | F
            "canales": {str(i): {"activo": i == 0, "nombre": f"T{i}", "tipo": "K", "min_C": -20.0, "max_C": 400.0,
                                 "desfase_cables_V": 0.0, "limite_C": None} for i in range(16)},
        }, **MUESTREO_DEF),
        "tensiones": dict({            # NI 9201 (RSE, ±10 V fijo; ganancia/desplazamiento = escala del DRIVER)
            "modulo": "",
            "conversion_Hz": None,           # None = la del driver
            "tdms": "auto",                  # auto (por encima de 1 kS/s) | si | no: registro del propio driver
            "canales": {str(i): {"activo": i == 0, "nombre": f"V{i}", "mide": "TODO_CONFIRMAR", "unidad": "V",
                                 "min_V": -10.0, "max_V": 10.0, "ganancia": 1.0, "desplazamiento": 0.0,
                                 "limite": None} for i in range(8)},
        }, **MUESTREO_DEF),
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


def cfg_modelo(cfg, rol):
    """Sección del panel -> configuración de cdaq_modelo (lo que valida y traduce al driver). Los canales llevan como
    nombre el virtual (tc0, v3...): así se reconocen en open_thrmcpl_chans y al leer."""
    c = cfg[rol]
    m = {k: c.get(k, v) for k, v in MUESTREO_DEF.items()}
    if m.get("tasa_S_s") is not None:
        m["tasa_S_s"] = float(m["tasa_S_s"])
    elif m.get("muestreo") != "bajo_demanda":          # vacía = automática
        n = sum(1 for q in c["canales"].values() if q.get("activo"))
        if n:
            m["tasa_S_s"] = M.tasa_auto(NUMERO[rol], n, c.get("modo_adc", "alta_resolucion"), c.get("autozero", "cada_muestra"))
    canales = []
    for k, q in sorted(c["canales"].items(), key=lambda x: int(x[0])):
        if not q.get("activo"):
            continue
        n = int(k)
        if rol == "termopares":
            # 9-nov (pedido del usuario): el rango NO se elige. En el 9213 la entrada es fija (±78 mV) y el rango del
            # canal solo limita lo que admite el driver: se usa el rango completo del tipo (validado con los 10 tipos)
            lo, hi = rango_tipo(q["tipo"], c.get("escala", "polinomio"))
            canales.append({"canal": n, "nombre": f"tc{n}", "tipo": q["tipo"], "min_C": lo, "max_C": hi,
                            "desfase_cables_V": float(q.get("desfase_cables_V") or 0.0)})
        else:
            g, d = float(q.get("ganancia", 1.0)), float(q.get("desplazamiento", 0.0))
            canales.append({"canal": n, "nombre": f"v{n}", "escala": None if (g, d) == (1.0, 0.0) else
                            {"pendiente": g, "ordenada": d, "unidad": (q.get("unidad") or "V")[:20]}})
    m["canales"] = canales
    if rol == "termopares":
        for k in ("modo_adc", "autozero", "cjc", "cjc_valor_C", "escala", "unidades"):
            m[k] = c.get(k)
    else:
        m["conversion_Hz"] = c.get("conversion_Hz")
    return m


def rango_tipo(tipo, escala="polinomio"):
    """Rango completo (°C) que admite el driver para un tipo de termopar (sonda del 9-nov)."""
    r = M.TC_TIPOS_TABLA.get(tipo) if escala == "tabla" else None
    return tuple(float(x) for x in (r or M.TC_TIPOS[tipo]))


def limites(cfg):
    """Máximo de muestreo de cada módulo con la configuración actual (regla validada; el driver no avisa en el 9213)."""
    out = {}
    for rol in ("termopares", "tensiones"):
        n = sum(1 for q in cfg[rol]["canales"].values() if q.get("activo"))
        if not n:
            out[rol] = None
            continue
        c = cfg[rol]
        mx = M.tasa_max_9213(n, c.get("modo_adc", "alta_resolucion"), c.get("autozero", "cada_muestra")) \
            if rol == "termopares" else M.tasa_max_9201(n)
        auto = M.tasa_auto(NUMERO[rol], n, c.get("modo_adc", "alta_resolucion"), c.get("autozero", "cada_muestra"))
        out[rol] = {"canales": n, "maximo_S_s": mx, "periodo_min_s": 1.0 / mx, "auto_S_s": auto}
    return out


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
                        "unidad": "°F" if cfg["termopares"].get("unidades") == "F" else "°C",
                        "rango_C": rango_tipo(q["tipo"], cfg["termopares"].get("escala", "polinomio")),
                        "color": COLORES[n % 8], "limite": q.get("limite_C"),
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

    def abrir(self, cfg, tdms_dir=None):
        """Una tarea por módulo, creada con el traductor VALIDADO (cdaq_modelo.crear_tarea): lo que se elige en el
        panel es exactamente lo que se programa en el driver. tdms_dir: carpeta para el registro TDMS en pausa."""
        import types
        tc, v = _canales_activos(cfg)
        equipos, modulos = self._buscar(cfg, tc, v)
        chasis = equipos[0]["nombre"]
        drv = types.SimpleNamespace(Task=self.Task, constants=self.K)
        self.modos = {}
        try:
            for rol, hay in (("termopares", tc), ("tensiones", v)):
                if not hay:
                    continue
                mc = cfg_modelo(cfg, rol)
                t = M.crear_tarea(drv, NUMERO[rol], modulos[rol], chasis, mc)
                self.tareas[rol] = t
                info = {"muestreo": mc["muestreo"], "fs": mc.get("tasa_S_s"), "n": 0, "t0": None,
                        "terminada": False, "siguiente": 0.0, "muestras": int(mc.get("muestras") or 0),
                        "tdms": None, "esperando_disparo": mc.get("disparo") in ("inicio", "referencia")}
                if mc["muestreo"] != "bajo_demanda":
                    if not mc.get("bufer_muestras"):        # 10 s de margen por si el hilo se retrasa
                        t.in_stream.input_buf_size = max(1000, int(info["fs"] * 10))
                    quiere = cfg[rol].get("tdms", "no")
                    if rol == "tensiones" and tdms_dir and (quiere == "si" or (quiere == "auto" and info["fs"] > TDMS_DESDE_S_S)):
                        # el registro del DRIVER, preparado y en pausa: al empezar un registro del panel se abre
                        # un fichero nuevo y se quita la pausa sin parar la tarea (probado el 9-nov: 0 muestras perdidas)
                        import os
                        espera = os.path.join(tdms_dir, f"cdaq_{rol}_espera.tdms")
                        t.in_stream.configure_logging(espera, logging_mode=self.K.LoggingMode.LOG_AND_READ,
                                                      operation=self.K.LoggingOperation.CREATE_OR_REPLACE)
                        t.in_stream.logging_pause = True
                        info["tdms"], info["tdms_espera"] = "preparado", espera
                t.start()                          # reserva el módulo (aquí sale el «ocupado» si lo tiene otro)
                try:                                   # lo que tiene programado el DRIVER (no lo que pidió el panel)
                    info["driver"] = _leer_driver(t, rol)
                except Exception as e:                 # informativo: nunca impide medir
                    info["driver"] = {"error": f"{type(e).__name__}: {e}"}
                self.modos[rol] = info
        except Exception:
            self.cerrar()
            raise
        simulado = any(e["simulado_daqmx"] for e in equipos)
        self.canales = _descripcion_canales(cfg, "sim." if simulado else "")
        return {"equipos": equipos, "canales": self.canales, "simulado": simulado,
                "aviso": "Dispositivo SIMULADO de NI MAX: " + AVISO_SIM if simulado else ""}

    def espera(self, periodo_s):
        """Cuánto dormir hasta el próximo ciclo: con reloj de hardware, CICLO_RELOJ_S; si todo es bajo demanda, el periodo."""
        return CICLO_RELOJ_S if any(i["muestreo"] != "bajo_demanda" for i in self.modos.values()) else periodo_s

    def leer(self, periodo_s=1.0):
        """{id: [(t, valor o None, aviso), ...]}. Termopar abierto -> valor None y aviso «abierto».
        Bajo demanda: una muestra por canal cada periodo_s (sello: reloj del PC al volver).
        Con reloj de hardware: todo lo acumulado; sello t0 + i/fs (t0 estimado en la primera lectura con datos)."""
        K = self.K
        out = {}
        ahora = time.time()
        for rol, t in self.tareas.items():
            info = self.modos.get(rol, {"muestreo": "bajo_demanda", "siguiente": 0.0})
            mios = [c for c in self.canales if c["modulo"] == rol]
            if info["muestreo"] == "bajo_demanda":
                if ahora < info["siguiente"]:
                    continue
                info["siguiente"] = max(info["siguiente"] + periodo_s, ahora) if info["siguiente"] else ahora + periodo_s
                datos = t.read(timeout=TIMEOUT_LECTURA_S)
                tl = time.time()
                datos = [[x] for x in (datos if isinstance(datos, list) else [datos])]
                sellos = [tl]
            else:
                if info["terminada"]:
                    continue
                # finito: «todo lo disponible» espera a la captura entera (-200284): se lee lo que ya hay
                hay = t.in_stream.avail_samp_per_chan if info["muestreo"] == "finito" else K.READ_ALL_AVAILABLE
                if hay == 0:
                    if info["muestreo"] == "finito" and info["n"] >= info["muestras"]:
                        info["terminada"] = True
                    continue
                datos = t.read(number_of_samples_per_channel=hay, timeout=0.0)
                tl = time.time()
                if datos and not isinstance(datos[0], list):
                    datos = [datos] if len(mios) == 1 else [[x] for x in datos]
                k = len(datos[0]) if datos else 0
                if info["muestreo"] == "finito" and info["n"] + k >= info["muestras"]:
                    info["terminada"] = True
                if not k:
                    continue
                info["esperando_disparo"] = False
                if info["t0"] is None:
                    info["t0"] = tl - k / info["fs"]
                sellos = [info["t0"] + (info["n"] + i) / info["fs"] for i in range(k)]
                info["n"] += k
            abiertos, modo_comun = set(), set()
            if rol == "termopares":
                ins = t.in_stream
                if ins.open_thrmcpl_chans_exist:        # hay que leer _exist antes que la lista (doc. de nidaqmx)
                    abiertos = set(ins.open_thrmcpl_chans)
                if ins.common_mode_range_error_chans_exist:
                    modo_comun = set(ins.common_mode_range_error_chans)
            for c, xs in zip(mios, datos):
                if c["virtual"] in abiertos:
                    out[c["id"]] = [(s, None, "abierto") for s in sellos]
                else:          # tensión: ya escalada por el DRIVER (escala propia = ganancia y desplazamiento)
                    av = "modo_comun" if c["virtual"] in modo_comun else ""
                    out[c["id"]] = [(s, float(x), av) for s, x in zip(sellos, xs)]
        return out

    def tdms(self, rol, ruta):
        """Registro TDMS del driver: ruta = fichero nuevo y quitar la pausa; None = pausa. Sin parar la tarea."""
        t, info = self.tareas.get(rol), self.modos.get(rol, {})
        if not t or not info.get("tdms"):
            return False
        if ruta:
            t.in_stream.start_new_file(ruta)
            t.in_stream.logging_pause = False
            info["tdms"] = ruta
        else:
            # El driver solo vuelca y cierra el fichero del registro al PARAR la tarea (probado el 9-nov: con la tarea
            # en marcha, ni pausando ni con start_new_file se escribe nada). Se para un instante, se vuelve a dejar el
            # TDMS preparado en pausa y se arranca otra vez (hueco de milisegundos en En vivo, ya fuera del registro)
            t.stop()
            t.in_stream.configure_logging(info["tdms_espera"], logging_mode=self.K.LoggingMode.LOG_AND_READ,
                                          operation=self.K.LoggingOperation.CREATE_OR_REPLACE)
            t.in_stream.logging_pause = True
            t.start()
            info["t0"], info["n"] = None, 0          # nueva base de tiempos tras el rearranque
            info["tdms"] = "preparado"
        return True

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


def _leer_driver(t, rol):
    """Lectura de vuelta de la tarea ya arrancada: lo que de verdad tiene el driver, para enseñarlo y validarlo."""
    def v(f):
        try:
            x = f()
            return x.name if hasattr(x, "name") else x
        except Exception:
            return None
    ch = t.ai_channels[0] if len(t.ai_channels) else None
    d = {"canales": [c.name for c in t.ai_channels], "temporizacion": v(lambda: t.timing.samp_timing_type)}
    if d["temporizacion"] == "SAMPLE_CLOCK":
        d.update(modo=v(lambda: t.timing.samp_quant_samp_mode), tasa_S_s=v(lambda: t.timing.samp_clk_rate),
                 fuente_reloj=v(lambda: t.timing.samp_clk_src), bufer=v(lambda: t.in_stream.input_buf_size))
    d["disparo_inicio"] = v(lambda: t.triggers.start_trigger.trig_type)
    d["disparo_referencia"] = v(lambda: t.triggers.reference_trigger.trig_type)
    d["pausa"] = v(lambda: t.triggers.pause_trigger.trig_type)
    d["registro_tdms"] = v(lambda: t.in_stream.logging_mode)
    if ch is not None and rol == "termopares":
        d.update(modo_adc=v(lambda: ch.ai_adc_timing_mode), autocero=v(lambda: ch.ai_auto_zero_mode),
                 cjc=v(lambda: ch.ai_thrmcpl_cjc_src), escala=v(lambda: ch.ai_thrmcpl_scale_type),
                 unidades=v(lambda: ch.ai_temp_units), tipos=[v(lambda c=c: c.ai_thrmcpl_type) for c in t.ai_channels],
                 rangos=[[v(lambda c=c: c.ai_min), v(lambda c=c: c.ai_max)] for c in t.ai_channels])
    elif ch is not None:
        d.update(terminal=v(lambda: ch.ai_term_cfg), rango_V=[v(lambda: ch.ai_rng_low), v(lambda: ch.ai_rng_high)],
                 unidades=v(lambda: ch.ai_voltage_units))
    return d


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

    def abrir(self, cfg, tdms_dir=None):
        self.modos = {r: {"muestreo": "bajo_demanda"} for r in ("termopares", "tensiones")}
        self.canales = _descripcion_canales(cfg, "sim.")
        equipos = [{"rol": "chasis", "nombre": "SIM_cDAQ", "producto": "cDAQ-9178 (SIMULADO)", "serie": "SIMULADO",
                    "simulado_daqmx": False},
                   {"rol": "termopares", "nombre": "SIM_cDAQMod1", "producto": "NI 9213 (SIMULADO)", "serie": "SIMULADO",
                    "ranura": 1, "simulado_daqmx": False},
                   {"rol": "tensiones", "nombre": "SIM_cDAQMod2", "producto": "NI 9201 (SIMULADO)", "serie": "SIMULADO",
                    "ranura": 2, "simulado_daqmx": False}]
        return {"equipos": equipos, "canales": self.canales, "simulado": True, "aviso": AVISO_SIM}

    def espera(self, periodo_s):
        return periodo_s

    def leer(self, periodo_s=1.0):
        ahora = time.time()
        x = ahora - self.t0
        out = {}
        for c in self.canales:
            if c["modulo"] == "termopares":
                if c["n"] in self.abiertos:
                    out[c["id"]] = [(ahora, None, "abierto")]
                else:
                    out[c["id"]] = [(ahora, 25 + 2 * c["n"] + 3 * math.sin(2 * math.pi * x / 300 + c["n"])
                                     + random.gauss(0, 0.05), "")]
            else:
                volt = 5 * math.sin(2 * math.pi * x / 20 + c["n"]) + random.gauss(0, 0.01)
                out[c["id"]] = [(ahora, c["ganancia"] * volt + c["desplazamiento"], "")]
        return out

    def tdms(self, rol, ruta):
        return False

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
        self._hilo_detectar = None
        self.lecturas = 0
        self.ultimo_error = None
        self._ordenes = []                 # órdenes para el hilo dueño de DAQmx (TDMS)
        self.modos = {}                    # por módulo: muestreo, tasa, muestras, disparo, TDMS (copia del backend)
        self.acum = {}                     # por canal: media de 0,1 s en curso para la historia (y el CSV a > 1 kS/s)
        import tempfile
        import os
        self.tdms_dir = os.path.join(tempfile.gettempdir(), "panel_cdaq")
        if self.cfg["habilitado"] and self.cfg["en_uso"]:
            self._lanzar("panel (arranque)")
        elif self.cfg["habilitado"] and not self.cfg["simulado"]:
            # solo mira si está (lista dispositivos, sin tareas). El hilo de medida espera a que termine: así nunca
            # hay dos hilos llamando a DAQmx a la vez
            self._hilo_detectar = threading.Thread(target=self._detectar, name="cdaq-detectar", daemon=True)
            self._hilo_detectar.start()

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
        d = self._hilo_detectar
        if d and d.is_alive():
            d.join(timeout=TIMEOUT_LECTURA_S + 5)      # que la detección del arranque no use DAQmx a la vez
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
                self._atender_ordenes(b)
                t_ciclo = time.time()
                try:
                    lect = b.leer(float(self.cfg["periodo_s"]))
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
                    if lect:
                        self.lecturas += 1
                    self._guardar_muestras(lect)
                    self.modos = {r: dict(i) for r, i in getattr(b, "modos", {}).items()}
                self._esperar(b.espera(float(self.cfg["periodo_s"])) - (time.time() - t_ciclo))
        finally:
            if b is not None:
                b.cerrar()
            self._primer.set()
            with self.lock:
                for o in self._ordenes:              # órdenes que ya no se pueden atender
                    o["resultado"], o["error"] = False, "el cDAQ se ha cerrado"
                    o["hecho"].set()
                self._ordenes = []

    def _atender_ordenes(self, b):
        """Órdenes para el driver que vienen de otros hilos (p. ej. abrir o pausar el TDMS al empezar o parar un
        registro): las ejecuta este hilo, el único que llama a DAQmx."""
        with self.lock:
            ordenes, self._ordenes = self._ordenes, []
        for o in ordenes:
            try:
                o["resultado"], o["error"] = b.tdms(o["rol"], o["ruta"]), None
            except Exception as e:
                o["resultado"], o["error"] = False, f"{type(e).__name__}: {e}"
            o["hecho"].set()

    def _orden_tdms(self, rol, ruta, espera_s=3.0):
        o = {"rol": rol, "ruta": ruta, "hecho": threading.Event(), "resultado": False, "error": None}
        with self.lock:
            if not (self._hilo and self._hilo.is_alive()):
                return False, "el cDAQ no está midiendo"
            self._ordenes.append(o)
        if not o["hecho"].wait(espera_s):
            return False, "el hilo del cDAQ no ha respondido"
        return o["resultado"], o["error"]

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
            import os
            os.makedirs(self.tdms_dir, exist_ok=True)
            info = b.abrir(cfg, self.tdms_dir)
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
        """lect: {id: [(t, v, aviso), ...]}. Historia en memoria: como mucho un punto (la media) cada HIST_DT_S por
        canal, para aguantar 2 h a cualquier tasa. CSV del registro: todas las muestras hasta CSV_MAX_S_S; por encima,
        la media de cada 0,1 s (las muestras completas van al TDMS del driver)."""
        csv = []
        for c in self.canales:
            ms = lect.get(c["id"])
            if not ms:
                continue
            fs = (self.modos.get(c["modulo"]) or {}).get("fs") or 0
            if (self.modos.get(c["modulo"]) or {}).get("muestreo", "bajo_demanda") == "bajo_demanda":
                fs = 0
            h = self.hist[c["id"]]
            for t, v, aviso in ms:
                if fs <= 1 / HIST_DT_S:                    # pocas muestras: todas
                    h.append((round(t, 6), v))
                    if fs <= CSV_MAX_S_S:
                        csv.append((c["id"], t, v, aviso))
                    continue
                if fs <= CSV_MAX_S_S:
                    csv.append((c["id"], t, v, aviso))
                a = self.acum.get(c["id"])
                if v is None or (a and t >= a[0] + HIST_DT_S):   # cierra la media de 0,1 s
                    if a and a[2]:
                        p = (round(a[0] + HIST_DT_S / 2, 6), a[1] / a[2])
                        h.append(p)
                        if fs > CSV_MAX_S_S:
                            csv.append((c["id"], p[0], p[1], ""))
                    a = None
                if v is None:
                    h.append((round(t, 6), None))
                    self.acum.pop(c["id"], None)
                    if fs > CSV_MAX_S_S:
                        csv.append((c["id"], t, None, aviso))
                    continue
                if a is None:
                    a = [t, 0.0, 0]
                a[1] += v
                a[2] += 1
                self.acum[c["id"]] = a
            t, v, aviso = ms[-1]
            while h and h[0][0] < t - HISTORIA_S:
                h.popleft()
            c["t_ultimo"], c["ultimo"], c["aviso"] = round(t, 3), v, aviso
        if self.grab and csv:
            self._escribir(csv)

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
        for k in ("modo_adc", "autozero", "cjc", "escala", "unidades"):
            if k in tcn:
                if tcn[k] not in OPCIONES[k]:
                    raise ValueError(f"{k} = {tcn[k]!r} no válido ({', '.join(OPCIONES[k])})")
                ct[k] = tcn[k]
        if "cjc_valor_C" in tcn:
            ct["cjc_valor_C"] = float(tcn["cjc_valor_C"])
        _aplicar_muestreo(ct, tcn)
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
            for k2 in ("min_C", "max_C", "desfase_cables_V"):
                if k2 in v:
                    q[k2] = float(v[k2] or 0.0)
            if q["min_C"] >= q["max_C"]:
                raise ValueError(f"Termopar {k}: min_C debe ser menor que max_C")
            if "limite_C" in v:
                q["limite_C"] = None if v["limite_C"] in (None, "") else float(v["limite_C"])
        vn = nuevo.get("tensiones") or {}
        cv = c["tensiones"]
        if "modulo" in vn:
            cv["modulo"] = str(vn["modulo"]).strip()[:60]
        if "tdms" in vn:
            if vn["tdms"] not in OPCIONES["tdms"]:
                raise ValueError(f"tdms = {vn['tdms']!r} no válido (auto, si, no)")
            cv["tdms"] = vn["tdms"]
        if "conversion_Hz" in vn:
            f = None if vn["conversion_Hz"] in (None, "", 0) else float(vn["conversion_Hz"])
            if f is not None and not 1000 <= f <= M.V_MAX_1CH:
                raise ValueError(f"conversion_Hz entre 1000 y {M.V_MAX_1CH:g} Hz (o vacío = la del driver)")
            cv["conversion_Hz"] = f
        _aplicar_muestreo(cv, vn)
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
                                 "(el NI 9201 solo mide ±10 V; es el rango de entrada, antes de la escala)")
            if float(q.get("ganancia", 1.0)) == 0:
                raise ValueError(f"Tensión {k}: la ganancia no puede ser 0")
            if "limite" in v:
                q["limite"] = None if v["limite"] in (None, "") else float(v["limite"])
        for sec in ("termopares", "tensiones"):
            nombres = [q["nombre"] for q in c[sec]["canales"].values() if q["activo"]]
            if len(set(nombres)) != len(nombres):
                raise ValueError(f"Dos canales activos con el mismo nombre ({sec})")
        todos = [q["nombre"] for s in ("termopares", "tensiones") for q in c[s]["canales"].values() if q["activo"]]
        if len(set(todos)) != len(todos):
            raise ValueError("Un termopar y una tensión activos con el mismo nombre (serían la misma columna)")
        # guardas del modelo validado: lo que el driver acepta pero no cumple (velocidad del 9213) o rechaza
        for rol in ("termopares", "tensiones"):
            if any(q["activo"] for q in c[rol]["canales"].values()):
                errores = M.comprobar(NUMERO[rol], cfg_modelo(c, rol))
                if errores:
                    raise ValueError(f"{'Termopares (NI 9213)' if rol == 'termopares' else 'Tensiones (NI 9201)'}: "
                                     + "; ".join(errores))
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
                    "aviso": self.aviso, "probado_con_hardware": not self.simulado,
                    "lectura": self._lectura_txt(), "periodo_s": self.cfg["periodo_s"],
                    "modulos": self._modulos(), "limites": limites(self.cfg),
                    "equipos": self.equipos, "captura": self.captura, "alarmas": alarmas,
                    "abiertos": [c["nombre"] for c in self.canales if c.get("aviso") == "abierto"],
                    "canales": [{k: v for k, v in c.items() if k not in ("n", "virtual", "ganancia", "desplazamiento")}
                                for c in self.canales],
                    "config": self.cfg, "lecturas": self.lecturas, "ultimo_error": self.ultimo_error,
                    "registro": None if not g else {"fichero": g["fichero"], "desde": g["t0"], "filas": g["filas"]}}

    def _modulos(self):
        """Por módulo: cómo mide ahora (lo que tiene el backend) y lo configurado."""
        out = {}
        for rol in ("termopares", "tensiones"):
            c = self.cfg[rol]
            i = self.modos.get(rol) or {}
            if not any(q.get("activo") for q in c["canales"].values()):
                continue
            situacion = ("sin tarea" if not i else "captura terminada" if i.get("terminada")
                         else "esperando disparo" if i.get("esperando_disparo") else "midiendo")
            out[rol] = {"muestreo": c.get("muestreo", "bajo_demanda"), "tasa_S_s": c.get("tasa_S_s"),
                        "tasa_efectiva": cfg_modelo(self.cfg, rol).get("tasa_S_s"),
                        "muestras": c.get("muestras"), "disparo": c.get("disparo", "ninguno"),
                        "reloj": c.get("reloj", "interno"), "situacion": situacion,
                        "leidas": i.get("n", 0), "tdms": i.get("tdms"), "driver": i.get("driver")}
        return out

    def _lectura_txt(self):
        partes = []
        for rol, m in self._modulos().items():
            q = "termopares" if rol == "termopares" else "tensión"
            partes.append(f"{q}: bajo demanda cada {self.cfg['periodo_s']:g} s" if m["muestreo"] == "bajo_demanda"
                          else f"{q}: reloj de hardware {m['muestreo']} a {(m.get('tasa_efectiva') or 0):.4g} S/s"
                          + (" (automática)" if m["tasa_S_s"] is None else ""))
        return "; ".join(partes) or "sin canales activos"

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
                    "origen": "cDAQ-9178 manejado por el panel (NI-DAQmx; opciones validadas con el hardware el 9-nov-2026)",
                    "probado_con_hardware": not self.simulado, "periodo_s": self.cfg["periodo_s"], "lectura": self._lectura_txt(),
                    "modulos": self._modulos(),
                    "termopares": {k: self.cfg["termopares"].get(k) for k in
                                   ("modo_adc", "autozero", "cjc", "cjc_valor_C", "escala", "unidades")},
                    "canales": [{k: c.get(k) for k in ("id", "modulo", "canal", "nombre", "tipo", "unidad", "mide",
                                                       "limite")} for c in self.canales]}

    # -------------------------------------------------- registro (CSV con el formato de temperaturas.csv)
    def iniciar_registro(self, fichero, autor, pre_s=30.0, cabecera=None, modulos=None):
        """modulos: ["termopares"], ["tensiones"] o los dos (None = todos). 9-nov (pedido del usuario): un ensayo puede
        llevar solo las temperaturas del 9213 o solo las tensiones del 9201."""
        with self.lock:
            if self.grab:
                raise RuntimeError("Ya se está registrando el cDAQ")
            if self.estado != "midiendo" or not self.canales:
                raise RuntimeError("El cDAQ no está midiendo (o no hay canales activos)")
            cans = [c for c in self.canales if not modulos or c["modulo"] in modulos]
            if not cans:
                raise RuntimeError(f"El cDAQ no tiene canales activos en {', '.join(modulos)}")
            t0 = time.time()
            f = open(fichero, "w", encoding="utf-8", newline="")
            if self.simulado:
                f.write(f"# *** {AVISO_SIM} ***\n")
            f.write("# Registro del cDAQ-9178 (NI 9213 termopares + NI 9201 tensión) manejado por el panel de laboratorio\n")
            ct = self.cfg["termopares"]
            for k, v in [("inicio", time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t0))),
                         ("simulado", "SÍ: " + AVISO_SIM if self.simulado else "no"),
                         ("probado_con_hardware", "sí (opciones validadas el 9-nov-2026, cdaq_validar_modelo.py)"),
                         ("equipos", "; ".join(f"{e['rol']} {e['nombre']} {e['producto']} serie {e['serie']}"
                                               for e in self.equipos)),
                         ("lectura", self._lectura_txt()),
                         ("muestras", f"todas hasta {CSV_MAX_S_S:g} S/s; por encima, la media de cada {HIST_DT_S:g} s "
                                      "(las completas, en el TDMS del driver: cdaq_tensiones.tdms)"),
                         ("termopares", f"modo ADC {ct['modo_adc']}, autozero {ct['autozero']}, CJC {ct['cjc']}, "
                                        f"escala {ct.get('escala')}, unidades {ct.get('unidades')}"),
                         ("modulos registrados", ", ".join(sorted({c["modulo"] for c in cans}))),
                         ("canales", "; ".join(f"{c['nombre']} = {c['modulo']} {c['canal']} {c['tipo']} [{c['unidad']}]"
                                               + (f" mide {c['mide']}" if c.get("mide") else "") for c in cans)),
                         ("celdas vacías", "sin dato en ese instante o termopar ABIERTO (ver columna avisos)"),
                         ("t_rel_s", "segundos desde el inicio; negativo = antes de empezar (lo que ya estaba en memoria)"),
                         ("hora", "reloj del PC (hora local) al volver la lectura; t_unix_s en segundos Unix")] + \
                    list((cabecera or {}).items()):
                f.write(f"# {k}: {v}\n")
            f.write(",".join(["fecha_hora", "t_unix_s", "t_rel_s"] + [_columna(c) for c in cans] + ["avisos"])
                    + "\n")
            self.grab = {"fichero": fichero, "f": f, "t0": t0, "filas": 0, "canales": [c["id"] for c in cans],
                         "nombres": {c["id"]: c["nombre"] for c in cans},
                         "modulos": sorted({c["modulo"] for c in cans})}
            desde = t0 - max(0.0, float(pre_s or 0))
            previas = [(i, t, v, "abierto" if v is None else "") for i in self.grab["canales"]
                       for t, v in self.hist.get(i, ()) if t >= desde]
            if previas:
                self._escribir(previas)
            tdms = (self.modos.get("tensiones") or {}).get("tdms") and "tensiones" in self.grab["modulos"]
        if tdms:      # FUERA del lock: lo ejecuta el hilo dueño de DAQmx
            import os
            ruta = os.path.join(os.path.dirname(fichero), "cdaq_tensiones.tdms")
            hecho, error = self._orden_tdms("tensiones", ruta)
            with self.lock:
                self.grab["tdms"] = ruta if hecho else None
            if not hecho:
                self.reg.anotar("panel (automático)", "cdaq", "TDMS del driver NO iniciado", error)
        self.reg.anotar(autor, "cdaq", "registro del cDAQ iniciado",
                        {"fichero": fichero, "simulado": self.simulado, "canales": [c["nombre"] for c in self.canales],
                         "tdms": (self.grab or {}).get("tdms")})

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
        if g.get("tdms"):
            self._orden_tdms("tensiones", None)      # pausa: la tarea sigue midiendo
        self.reg.anotar(autor, "cdaq", "registro del cDAQ terminado",
                        {"fichero": g["fichero"], "filas": g["filas"], "motivo": motivo, "tdms": g.get("tdms")})
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


def _aplicar_muestreo(dst, src):
    """Opciones de muestreo, reloj, disparo y búfer de un módulo (valores del modelo validado)."""
    for k in ("muestreo", "reloj", "disparo", "disparo_fuente", "disparo_flanco", "pausa_cuando"):
        if k in src:
            if src[k] not in OPCIONES[k]:
                raise ValueError(f"{k} = {src[k]!r} no válido ({', '.join(OPCIONES[k])})")
            dst[k] = src[k]
    if "tasa_S_s" in src:
        dst["tasa_S_s"] = None if src["tasa_S_s"] in (None, "") else float(src["tasa_S_s"])
        if dst["tasa_S_s"] is not None and dst["tasa_S_s"] <= 0:
            raise ValueError("tasa_S_s tiene que ser mayor que 0")
    for k, mn in (("muestras", 2), ("pre_muestras", 2)):
        if k in src:
            n = int(src[k])
            if n < mn:
                raise ValueError(f"{k}: al menos {mn}")
            dst[k] = n
    if "bufer_muestras" in src:
        dst["bufer_muestras"] = None if src["bufer_muestras"] in (None, "", 0) else int(src["bufer_muestras"])
    for k, v in MUESTREO_DEF.items():
        dst.setdefault(k, v)


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
