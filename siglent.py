"""Fuente Siglent SPD3303X en el panel (9-nov-2026). Equipo aparte de la fuente EA (fuente.py): sus salidas CH1, CH2 y
CH3, su tarjeta, su siglent.csv y su sitio en En vivo, Ensayo y Tramo.

Opciones y guardas: siglent_modelo.py (traducción 1:1 de SCPI, validada con el equipo; norma del usuario).
  - UN SOLO HILO habla con la fuente (NI-VISA): lee cada periodo_s V, I, P y CV/CC de los dos canales y atiende la cola
    de órdenes de la página (como el cDAQ). Cada orden se comprueba leyendo de vuelta.
  - Encender pide confirmación; apagar nunca. APAGAR TODO apaga CH1, CH2 y CH3 y lo confirma MIDIENDO.
  - SIN vigilante en el equipo: el panel hace de vigilante. Si se pierde la comunicación más de CORTE_S, se intenta
    apagar todo y queda «sin_comunicacion» (al volver, las salidas siguen apagadas). Al cerrar el panel: apaga.
  - Consignas: el modelo frena negativos, el máximo del equipo y los topes de seguridad del panel (el equipo no avisa).
"""
import collections
import copy
import random
import threading
import time
import uuid

import siglent_modelo as M

HISTORIA_S = 2 * 3600
CORTE_S = 3.0
REINTENTO_S = 5.0
# 9-nov (causa del cuelgue): al reenchufar el USB, si el panel le habla en el mismo segundo en que reaparece, la
# fuente deja de contestar y cada lectura se queda ~100 s dentro del driver USBTMC (sin respetar el timeout de VISA).
# Tras reaparecer se espera este tiempo antes de abrir la sesión
ESPERA_TRAS_APARECER_S = 8.0
COLUMNAS = ("V", "I", "P")


def config_por_defecto():
    return {"habilitado": True, "simulado": False, "recurso": "", "periodo_s": 0.5,
            "topes": {"V": M.V_MAX, "I": M.I_MAX},       # de seguridad del panel (≤ los del equipo)
            "nombres": {"CH1": "CH1", "CH2": "CH2", "CH3": "CH3"}}


def completar(cfg):
    for k, v in config_por_defecto().items():
        if k not in cfg:
            cfg[k] = copy.deepcopy(v)
        elif isinstance(v, dict):
            for k2, v2 in v.items():
                cfg[k].setdefault(k2, v2)
    return cfg


class EnlaceSimulado:
    """Imita la SPD3303X (para probar sin el equipo). Marcado SIMULADO en todo lo que publica."""

    def __init__(self, *a, **k):
        self.c = {"CH1": [0.0, 0.0, False], "CH2": [0.0, 0.0, False]}
        self.ch3, self.track, self.carga_ohm = False, 0, 100.0
        self.recurso = "SIMULADA"

    def escribir(self, o):
        p = o.replace(",", " ").split()
        if p[0].endswith(":VOLTage"):
            self.c[p[0][:3]][0] = float(p[1])
        elif p[0].endswith(":CURRent"):
            self.c[p[0][:3]][1] = float(p[1])
        elif p[0] == "OUTPut" and p[1] in ("CH1", "CH2"):
            self.c[p[1]][2] = p[2] == "ON"
        elif p[0] == "OUTPut" and p[1] == "CH3":
            self.ch3 = p[2] == "ON"
        elif p[0] == "OUTPut:TRACK":
            self.track = int(p[1])

    def _medida(self, ch):
        v, i, on = self.c[ch]
        if not on:
            return 0.0, 0.0
        im = min(v / self.carga_ohm, i)
        return round(im * self.carga_ohm if im < i else v, 3), round(im, 3)

    def consultar(self, o):
        if o == "*IDN?":
            return "Siglent Technologies,SPD3303X,SIMULADA,0,0"
        if o == "SYSTem:STATus?":
            v = {0: 1, 1: 3, 2: 2}[self.track] << 2
            v |= 16 if self.c["CH1"][2] else 0
            v |= 32 if self.c["CH2"][2] else 0
            return hex(v)
        if o == "SYSTem:ERRor?":
            return "0  No Error"
        if o.endswith(":VOLTage?"):
            return f"{self.c[o[:3]][0]:.3f}"
        if o.endswith(":CURRent?"):
            return f"{self.c[o[:3]][1]:.3f}"
        ch = o.split()[-1]
        v, i = self._medida(ch)
        return f"{v + random.gauss(0, 0.0005) if v else 0:.3f}" if "VOLT" in o else f"{i:.3f}" if "CURR" in o else f"{v * i:.3f}"

    def numero(self, o, intentos=3):
        return float(self.consultar(o))

    def cerrar(self):
        pass


class Siglent:
    def __init__(self, registro, cfg, guardar_cfg, fabrica=None):
        self.reg, self.cfg, self._guardar = registro, completar(cfg), guardar_cfg
        self._fabrica = fabrica
        self.lock = threading.RLock()
        self.estado, self.mensaje = ("desconectada" if self.cfg["habilitado"] else "desactivada"), ""
        self.idn, self.simulado = "", False
        self.canales = {c: {"V_cons": None, "I_cons": None, "V": None, "I": None, "P": None, "salida": False, "regula": ""}
                        for c in M.CANALES}
        self.ch3 = {"salida": None}         # el equipo no informa de CH3 por SCPI: lo último que se ordenó
        self.modo = None
        self.hist = collections.deque()
        self.grab = None
        self.ultimo_error = None
        self._ordenes, self._hilo, self._parar = [], None, threading.Event()
        self._ult_ok = 0.0
        self._corte_pendiente = None     # 9-nov: el vigilante no pudo apagar; se mantiene el aviso hasta reconectar
        self._ausente = False            # 9-nov: NI-VISA dejó de verla (USB desenchufado): al volver, se espera
        self.marcas = collections.deque(maxlen=500)

    # -------------------------------------------------- conectar / desconectar
    def conectar(self, autor, esperar_s=8.0):
        with self.lock:
            if not self.cfg["habilitado"]:
                raise RuntimeError("La Siglent está desactivada en config.json («siglent»: «habilitado»)")
            if self._hilo and self._hilo.is_alive():
                return self.resumen()
            self._parar.clear()
            self.estado, self.mensaje = "conectando", ""
            self._listo = threading.Event()
            self._hilo = threading.Thread(target=self._bucle, name="siglent", daemon=True)
            self._hilo.start()
        self._listo.wait(esperar_s)
        with self.lock:
            if self.estado == "conectando":
                # 9-nov: visto dos veces; el hilo queda dentro de NI-VISA (no se puede cortar desde Python)
                self.mensaje = (f"NI-VISA no contesta tras {esperar_s:g} s (driver USB colgado). Suele soltarse solo en unos "
                                "minutos; si no, apaga y enciende la Siglent o desenchufa y enchufa su USB")
        self.reg.anotar(autor, "siglent", "Siglent conectada" if self.estado == "conectada" else f"Siglent: {self.estado}",
                        self.mensaje or self.idn)
        return self.resumen()

    def desconectar(self, autor):
        """Apaga las salidas (sin vigilante en el equipo, nunca se dejan encendidas sin el panel) y cierra."""
        if self.estado == "conectada":
            try:
                self.apagar_todo(autor, "desconectar")
            except Exception:
                pass
        self._parar.set()
        if self._hilo:
            self._hilo.join(timeout=10)
        with self.lock:
            self.estado = "desconectada" if self.cfg["habilitado"] else "desactivada"
            self.mensaje = ""
        self.reg.anotar(autor, "siglent", "Siglent desconectada")
        return self.resumen()

    def cerrar(self):
        """Al cerrar el panel: apaga todo (decisión del 9-nov: la Siglent no tiene vigilante propio)."""
        try:
            if self.estado == "conectada":
                self.apagar_todo("panel (cierre)", "cierre del panel")
        except Exception:
            pass
        try:
            if self.grab:
                self.parar_registro("panel (cierre)", "cierre del panel")
        except Exception:
            pass
        self._parar.set()

    # -------------------------------------------------- hilo dueño del enlace
    def _nuevo_enlace(self):
        if self._fabrica:
            return self._fabrica(self.cfg)
        # 9-nov: NI-VISA en un proceso hijo (si el driver USB se cuelga, el panel no queda retenido)
        return EnlaceSimulado() if self.cfg["simulado"] else M.EnlaceProceso(self.cfg.get("recurso") or None)

    def _bucle(self):
        e = None
        try:
            while not self._parar.is_set():
                if e is None:
                    if self._esperar_usb():
                        continue
                    try:
                        e = self._nuevo_enlace()
                        idn = e.consultar("*IDN?")
                        if M.IDN_MODELO not in idn:
                            raise ConnectionError(f"No es una {M.IDN_MODELO}: {idn}")
                        with self.lock:
                            self.idn, self.simulado = idn, isinstance(e, EnlaceSimulado)
                            self.estado, self.mensaje = "conectada", ""
                            self._ult_ok = time.time()
                        if self._corte_pendiente:
                            self._tras_corte(e)
                    except Exception as x:
                        if e:
                            e.cerrar()
                        e = None
                        with self.lock:
                            if self._corte_pendiente:
                                # 9-nov (visto en la prueba): el reintento borraba el aviso del vigilante en 1 s
                                self.estado = "sin_comunicacion"
                                self.mensaje = self._corte_pendiente + " Reintentando conectar…"
                            else:
                                self.estado, self.mensaje = "desconectada", f"{type(x).__name__}: {x}"
                            self.ultimo_error = f"{type(x).__name__}: {x}"
                        self._listo.set()
                        self._parar.wait(REINTENTO_S)
                        continue
                    self._listo.set()
                t0 = time.time()
                self._atender(e)
                try:
                    self._leer(e)
                    with self.lock:
                        self._ult_ok = time.time()
                        if self.estado == "sin_comunicacion":
                            self.estado, self.mensaje = "conectada", "comunicación recuperada (salidas apagadas)"
                except Exception as x:
                    with self.lock:
                        self.ultimo_error = f"{type(x).__name__}: {x}"
                        perdida = time.time() - self._ult_ok > CORTE_S
                    if perdida:
                        self._corte(e, x)
                        e.cerrar()
                        e = None
                        continue
                self._parar.wait(max(0.05, float(self.cfg["periodo_s"]) - (time.time() - t0)))
        finally:
            if e is not None:
                e.cerrar()
            self._listo.set()
            with self.lock:
                for o in self._ordenes:
                    o["error"] = "la Siglent se ha desconectado"
                    o["hecho"].set()
                self._ordenes = []

    def _esperar_usb(self):
        """True = todavía no se abre la sesión (no está o acaba de reaparecer y hay que dejarla arrancar)."""
        if self._fabrica or self.cfg["simulado"]:
            return False
        if M.hijos_retenidos():
            # el anterior sigue dentro del driver: abrir otro solo añadiría otro proceso colgado
            with self.lock:
                if not self._corte_pendiente:
                    self.estado = "desconectada"
                    self.mensaje = ("La Siglent no contesta por USB: el driver retiene la sesión anterior (se suelta sola en "
                                    "~2 min; si no vuelve, apaga y enciende la Siglent)")
            self._listo.set()
            self._parar.wait(REINTENTO_S)
            return True
        try:
            hay = M.presente_aislado(self.cfg.get("recurso") or None)
        except Exception:
            return False                 # si no se puede listar, que lo diga el intento de conexión
        if hay is None:                  # NI-VISA no contesta ni para listar: se reintenta más tarde
            with self.lock:
                if not self._corte_pendiente:
                    self.estado = "desconectada"
                    self.mensaje = "NI-VISA no contesta (driver USB colgado): apaga y enciende la Siglent si no se suelta"
            self._listo.set()
            self._parar.wait(REINTENTO_S)
            return True
        if not hay:
            self._ausente = True
            with self.lock:
                if not self._corte_pendiente:
                    self.estado, self.mensaje = "desconectada", "NI-VISA no ve la Siglent (¿USB conectado y fuente encendida?)"
            self._listo.set()
            self._parar.wait(REINTENTO_S)
            return True
        if self._ausente:
            self._ausente = False
            with self.lock:
                if not self._corte_pendiente:
                    self.mensaje = f"Vuelve a verse por USB: se espera {ESPERA_TRAS_APARECER_S:g} s a que arranque su USB"
            self._parar.wait(ESPERA_TRAS_APARECER_S)
            return self._parar.is_set()
        return False

    def _corte(self, e, x):
        """Vigilante del panel: comunicación perdida > CORTE_S -> intentar apagar y avisar."""
        apagada = False
        try:
            for c in ("CH1", "CH2", "CH3"):
                e.escribir(M.ORDENES["salida"].format(c=c, onoff="OFF"))
            apagada = True
        except Exception:
            pass
        with self.lock:
            self.estado = "sin_comunicacion"
            self.mensaje = (f"Sin comunicación más de {CORTE_S:g} s ({type(x).__name__}). "
                            + ("Se han mandado apagar las salidas." if apagada else
                               "NO se ha podido mandar apagar: revisa la fuente (no tiene vigilante propio)."))
            self._corte_pendiente = None if apagada else self.mensaje
        self.reg.anotar("panel (automático)", "siglent", "Siglent sin comunicación", self.mensaje)
        self._hueco(self.mensaje)

    def _tras_corte(self, e):
        """Al volver la comunicación tras un corte en el que NO se pudo apagar: lo que siga encendido se apaga (es lo
        que promete el vigilante del panel) y se comprueba midiendo."""
        try:
            s = M.estado(e.consultar(M.ORDENES["estado"]))
            on = [c for c in M.CANALES if s[c]["salida"]]
            for c in ("CH1", "CH2", "CH3"):
                e.escribir(M.ORDENES["salida"].format(c=c, onoff="OFF"))
            s = M.estado(e.consultar(M.ORDENES["estado"]))
            sigue = [c for c in M.CANALES if s[c]["salida"]]
            txt = ("comunicación recuperada tras el corte: " +
                   (f"{', '.join(on)} seguía(n) encendida(s) y se ha(n) APAGADO" if on else "las salidas ya estaban apagadas")
                   + (f"; ¡{', '.join(sigue)} NO se ha podido apagar!" if sigue else "") + " (CH3 mandado apagar)")
            with self.lock:
                self.mensaje = txt
                self._corte_pendiente = None
            self.reg.anotar("panel (automático)", "siglent", "Siglent: comunicación recuperada", txt)
            self._hueco(txt)
        except Exception as x:
            self.reg.anotar("panel (automático)", "siglent", "Siglent: no se pudo comprobar tras el corte", str(x))

    def _leer(self, e):
        s = M.estado(e.consultar(M.ORDENES["estado"]))
        fila = {"t": time.time()}
        for c in M.CANALES:
            v = e.numero(M.ORDENES["medir_V"].format(c=c))
            i = e.numero(M.ORDENES["medir_I"].format(c=c))
            fila[c] = {"V": v, "I": i, "P": round(v * i, 4), "salida": s[c]["salida"], "regula": s[c]["regula"]}
        with self.lock:
            self.modo = s["modo"]
            for c in M.CANALES:
                self.canales[c].update(fila[c])
            self.hist.append(fila)
            while self.hist and self.hist[0]["t"] < fila["t"] - HISTORIA_S:
                self.hist.popleft()
            if self.grab:
                self._escribir([fila])

    def _leer_consignas(self, e):
        for c in M.CANALES:
            self.canales[c]["V_cons"] = float(e.consultar(M.ORDENES["leer_V"].format(c=c)))
            self.canales[c]["I_cons"] = float(e.consultar(M.ORDENES["leer_I"].format(c=c)))

    # -------------------------------------------------- órdenes (las ejecuta el hilo)
    def _orden(self, fn, espera_s=8.0):
        o = {"fn": fn, "hecho": threading.Event(), "resultado": None, "error": None}
        with self.lock:
            if self.estado != "conectada":
                raise RuntimeError(f"La Siglent no está conectada ({self.estado})")
            self._ordenes.append(o)
        if not o["hecho"].wait(espera_s):
            raise RuntimeError("La Siglent no ha respondido a tiempo")
        if o["error"]:
            raise RuntimeError(o["error"])
        return o["resultado"]

    def _atender(self, e):
        with self.lock:
            ordenes, self._ordenes = self._ordenes, []
            if self.canales["CH1"]["V_cons"] is None:
                ordenes.insert(0, {"fn": lambda en: self._leer_consignas(en), "hecho": threading.Event(),
                                   "resultado": None, "error": None})
        for o in ordenes:
            try:
                o["resultado"] = o["fn"](e)
            except Exception as x:
                o["error"] = f"{type(x).__name__}: {x}"
            o["hecho"].set()

    def fijar(self, autor, canal, V=None, I=None):
        if canal not in M.CANALES:
            raise ValueError(f"Canal {canal!r} no válido (CH1, CH2; CH3 solo se enciende o apaga)")
        if self.modo in ("serie", "paralelo") and canal == "CH2":
            raise ValueError(f"En modo {self.modo} manda CH1: CH2 no se ajusta")
        errores = M.comprobar({"modo": self.modo or "independiente", canal: {"V": V, "I": I}}, self.cfg["topes"])
        if errores:
            raise ValueError("; ".join(errores))

        def hacer(e):
            for k, v in (("V", V), ("I", I)):
                if v is None:
                    continue
                e.escribir(M.ORDENES["fijar_" + k].format(c=canal, v=float(v)))
                leido = float(e.consultar(M.ORDENES["leer_" + k].format(c=canal)))
                if abs(leido - float(v)) > 0.0006:
                    raise RuntimeError(f"{canal} {k}: se pidió {v} y la fuente tiene {leido}")
                self.canales[canal][f"{k}_cons"] = leido
            return {k: self.canales[canal][f"{k}_cons"] for k in ("V", "I")}
        r = self._orden(hacer)
        self.reg.anotar(autor, "siglent", f"{self._nombre(canal)}: consignas", r)
        self.marca(f"{self._nombre(canal)} consignas V={r['V']} I={r['I']}")
        return self.resumen()

    def salida(self, autor, canal, encender, confirmado=False):
        if canal not in M.CANALES + (M.CH3,):
            raise ValueError(f"Canal {canal!r} no válido (CH1, CH2, CH3)")
        if encender and not confirmado:
            raise ValueError(f"Encender {self._nombre(canal)} necesita confirmación (la Siglent no tiene vigilante propio)")
        if encender and canal == "CH2" and self.modo in ("serie", "paralelo"):
            raise ValueError(f"En modo {self.modo} se enciende CH1")

        def hacer(e):
            e.escribir(M.ORDENES["salida"].format(c=canal, onoff="ON" if encender else "OFF"))
            time.sleep(0.4)
            if canal == M.CH3:
                self.ch3["salida"] = encender
                return encender
            s = M.estado(e.consultar(M.ORDENES["estado"]))
            if s[canal]["salida"] != encender:
                raise RuntimeError(f"{canal}: la fuente no ha {'encendido' if encender else 'apagado'}")
            if not encender:
                v = e.numero(M.ORDENES["medir_V"].format(c=canal))
                if v > 0.05:
                    raise RuntimeError(f"{canal}: el estado dice apagada pero mide {v} V")
            return encender
        self._orden(hacer)
        self.reg.anotar(autor, "siglent", f"{self._nombre(canal)} {'ENCENDIDA' if encender else 'apagada'}")
        self.marca(f"{self._nombre(canal)} {'ON' if encender else 'OFF'}")
        return self.resumen()

    def apagar_todo(self, autor, motivo="APAGAR"):
        def hacer(e):
            for c in ("CH1", "CH2", "CH3"):
                e.escribir(M.ORDENES["salida"].format(c=c, onoff="OFF"))
                time.sleep(0.3)
            self.ch3["salida"] = False
            time.sleep(0.4)
            v = {c: e.numero(M.ORDENES["medir_V"].format(c=c)) for c in M.CANALES}
            s = M.estado(e.consultar(M.ORDENES["estado"]))
            if any(s[c]["salida"] or v[c] > 0.05 for c in M.CANALES):
                raise RuntimeError(f"No se ha confirmado el apagado: {v} · {s}")
            return v
        r = self._orden(hacer)
        self.reg.anotar(autor, "siglent", f"Siglent: todo apagado ({motivo})", r)
        self.marca(f"APAGAR TODO ({motivo})")
        return self.resumen()

    def poner_modo(self, autor, modo):
        if modo not in M.MODOS:
            raise ValueError(f"modo {modo!r} no válido ({', '.join(M.MODOS)})")
        if any(self.canales[c]["salida"] for c in M.CANALES):
            raise ValueError("El modo se cambia con las salidas apagadas")

        def hacer(e):
            e.escribir(M.ORDENES["modo"].format(n=M.MODOS[modo]))
            time.sleep(0.6)
            s = M.estado(e.consultar(M.ORDENES["estado"]))
            if s["modo"] != modo:
                raise RuntimeError(f"La fuente sigue en modo {s['modo']}")
            self.modo = modo
            return modo
        self._orden(hacer)
        self.reg.anotar(autor, "siglent", f"Siglent: modo {modo}")
        return self.resumen()

    def opciones(self, autor, topes=None, nombres=None, periodo_s=None):
        c = copy.deepcopy(self.cfg)
        if topes:
            for k, mx in (("V", M.V_MAX), ("I", M.I_MAX)):
                if k in topes:
                    x = float(topes[k])
                    if not 0 < x <= mx:
                        raise ValueError(f"Tope {k} entre 0 y {mx:g}")
                    c["topes"][k] = x
        if nombres:
            for k, v in nombres.items():
                if k in c["nombres"]:
                    c["nombres"][k] = str(v).strip()[:30] or k
        if periodo_s is not None:
            p = float(periodo_s)
            if not 0.3 <= p <= 60:
                raise ValueError("periodo_s entre 0,3 y 60 s (una lectura completa tarda ~0,3 s)")
            c["periodo_s"] = p
        with self.lock:
            self.cfg.clear()
            self.cfg.update(c)
            self._guardar()
        self.reg.anotar(autor, "siglent", "opciones de la Siglent", {"topes": topes, "nombres": nombres, "periodo_s": periodo_s})
        return self.resumen()

    # -------------------------------------------------- consultas
    def _nombre(self, c):
        return self.cfg["nombres"].get(c, c)

    def marca(self, texto):
        with self.lock:
            self.marcas.append((time.time(), texto))

    def encendida(self):
        return any(self.canales[c]["salida"] for c in M.CANALES) or bool(self.ch3["salida"])

    def resumen(self):
        with self.lock:
            g = self.grab
            return {"estado": self.estado, "mensaje": self.mensaje, "idn": self.idn, "simulado": self.simulado,
                    "modo": self.modo, "canales": copy.deepcopy(self.canales), "ch3": dict(self.ch3),
                    "encendida": self.encendida(), "topes": dict(self.cfg["topes"]), "nombres": dict(self.cfg["nombres"]),
                    "periodo_s": self.cfg["periodo_s"], "maximos": {m: {"V": v, "I": i} for m, (v, i) in M.MAX_MODO.items()},
                    "vigilante": f"del panel ({CORTE_S:g} s): la SPD3303X no tiene vigilante propio",
                    "ultimo_error": self.ultimo_error,
                    "registro": None if not g else {"fichero": g["fichero"], "desde": g["t0"], "filas": g["filas"]}}

    def historia_desde(self, desde=0.0):
        """Formato común (como el cDAQ): {canales: [{id, nombre, unidad, color}], datos: {id: [[t, v]...]}}."""
        cols = {"#6250d6": "CH1", "#0f8b8d": "CH2"}
        canales, datos = [], {}
        with self.lock:
            filas = [f for f in self.hist if f["t"] > desde]
        for color, c in cols.items():
            for k, u in (("V", "V"), ("I", "A")):
                i = f"{c}.{k}"
                canales.append({"id": i, "nombre": f"{self._nombre(c)} {k}", "unidad": u, "color": color})
                datos[i] = [[round(f["t"], 3), f[c][k] if f[c]["salida"] else 0.0] for f in filas]
        return {"simulado": self.simulado, "canales": canales, "datos": datos}

    def ficha(self):
        r = self.resumen()
        return {k: r[k] for k in ("idn", "simulado", "modo", "canales", "ch3", "topes", "nombres", "periodo_s", "vigilante")}

    # -------------------------------------------------- registro y tramo (siglent.csv)
    def _cabecera(self, f, t0, cabecera):
        f.write("# Registro de la fuente Siglent SPD3303X (panel de laboratorio). MATLAB: osc(ruta)\n")
        for k, v in [("inicio", time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t0))), ("equipo", self.idn),
                     ("simulado", "SÍ" if self.simulado else "no"), ("modo", self.modo),
                     ("nombres", "; ".join(f"{c} = {n}" for c, n in self.cfg["nombres"].items())),
                     ("hora", "reloj del PC al leer; t_unix_s en segundos Unix")] + list((cabecera or {}).items()):
            f.write(f"# {k}: {v}\n")
        f.write("fecha_hora,t_unix_s,t_rel_s," + ",".join(f"{c}_{k}" for c in M.CANALES for k in ("V", "I", "P", "salida", "regula"))
                + ",marcas\n")

    def _linea(self, fila, t0, marcas=""):
        t = fila["t"]
        hora = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t)) + f".{int(t % 1 * 1000):03d}"
        vals = []
        for c in M.CANALES:
            q = fila[c]
            vals += [f"{q['V']:.4f}", f"{q['I']:.4f}", f"{q['P']:.4f}", "1" if q["salida"] else "0", q["regula"]]
        return f"{hora},{t:.3f},{t - t0:.3f}," + ",".join(vals) + "," + marcas.replace(",", " ") + "\n"

    def iniciar_registro(self, fichero, autor, pre_s=30.0, cabecera=None):
        with self.lock:
            if self.grab:
                raise RuntimeError("Ya se está registrando la Siglent")
            if self.estado != "conectada":
                raise RuntimeError("La Siglent no está conectada")
            t0 = time.time()
            f = open(fichero, "w", encoding="utf-8", newline="")
            self._cabecera(f, t0, cabecera)
            self.grab = {"fichero": fichero, "f": f, "t0": t0, "filas": 0, "id": str(uuid.uuid4())}
            previas = [x for x in self.hist if x["t"] >= t0 - max(0.0, float(pre_s or 0))]
            self._escribir(previas)
        self.reg.anotar(autor, "siglent", "registro de la Siglent iniciado", {"fichero": fichero})

    def _escribir(self, filas):
        g = self.grab
        for fila in filas:
            m = "; ".join(t for tt, t in self.marcas if g.get("ult_marca", 0) < tt <= fila["t"])
            if m:
                g["ult_marca"] = fila["t"]
            g["f"].write(self._linea(fila, g["t0"], m))
            g["filas"] += 1
        g["f"].flush()

    def _hueco(self, motivo):
        g = self.grab
        if g:
            t = time.time()
            g["f"].write(f"{time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(t))},{t:.3f},{t - g['t0']:.3f},"
                         + "," * (5 * len(M.CANALES)) + "hueco: " + str(motivo).replace(",", " ")[:200] + "\n")
            g["f"].flush()

    def parar_registro(self, autor, motivo="parado"):
        with self.lock:
            g = self.grab
            if not g:
                return None
            g["f"].close()
            self.grab = None
        self.reg.anotar(autor, "siglent", "registro de la Siglent terminado", {"fichero": g["fichero"], "filas": g["filas"],
                                                                              "motivo": motivo})
        return g["fichero"]

    def exportar_csv(self, fichero, t0, t1, cabecera=None):
        with self.lock:
            filas = [x for x in self.hist if t0 <= x["t"] <= t1]
        if not filas:
            return 0
        with open(fichero, "w", encoding="utf-8", newline="") as f:
            self._cabecera(f, t0, cabecera)
            for x in filas:
                f.write(self._linea(x, t0))
        return len(filas)
