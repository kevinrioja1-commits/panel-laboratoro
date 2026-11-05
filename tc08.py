"""TC-08 (registrador de termopares de Pico) manejado por el panel (pedido del usuario el 2026-11-03).

El panel configura los canales (activo, nombre segun el ensayo, tipo de termopar, limite para la alarma visual) y
el intervalo, mide de forma continua con la libreria oficial de Pico (usbtc08.dll, la que trae PicoLog) y pone a cada
muestra la hora del PC: temperaturas en vivo alineadas con la fuente y el osciloscopio. En un ensayo guarda
temperaturas.csv y, al terminar, temperaturas.picolog (formato de PicoLog: se abre con la aplicacion).

Verificado el 2026-11-03 con el TC-08 A0067/685:
  - el TC-08 convierte como minimo cada 200 ms, aunque haya un solo canal (usb_tc08_run lo sube a 200). Los «100 ms»
    de PicoLog son un punto intermedio interpolado entre dos medidas reales (comprobado en una captura suya);
  - retraso medida -> lectura ~0,2 s; el reloj del TC-08 no deriva frente al del PC en la prueba (12 s);
  - el .picolog escrito por picolog_fichero.py lo abre PicoLog 6.3 sin problemas.
El TC-08 solo lo puede usar un programa a la vez: mientras lo tiene el panel, PicoLog no lo ve («Soltar TC-08»).
"""
import collections
import copy
import ctypes
import json
import os
import subprocess
import threading
import time
import uuid

import picolog_fichero

DLL = r"C:\Program Files\Pico Technology\PicoLog\usbtc08.dll"
PLANTILLA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "plantilla_picolog.json")
TIPOS = "BEJKNRST"
INTERVALO_MIN_MS = 200            # el TC-08 no convierte mas deprisa (medido)
HISTORIA_S = 2 * 3600
# sin rojo (el rojo es de las alarmas); los mismos que En vivo y osc.m (2026-11-04)
COLORES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#2a78d6"]


def config_por_defecto():
    return {"intervalo_ms": 200, "red_hz": 50, "en_uso": False,
            "canales": {str(i): {"activo": i == 1, "nombre": f"TC{i}", "tipo": "K", "limite_C": None}
                        for i in range(1, 9)}}


def _picolog_abierto():
    try:
        r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq PicoLog.exe", "/NH"], capture_output=True, timeout=5,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return b"PicoLog.exe" in r.stdout
    except Exception:
        return False


class TC08:
    def __init__(self, registro, cfg, guardar_cfg):
        self.reg = registro
        self.cfg = cfg                       # config.json -> "temperaturas" (se guarda con guardar_cfg)
        self._guardar = guardar_cfg
        self.lock = threading.RLock()
        self.estado = "libre"                # libre | midiendo | error
        self.mensaje = ""
        self.serie = ""
        self.intervalo_ms = None             # el real que acepta el TC-08
        self.canales = []                    # activos: [{id, canal, nombre, tipo, unidad, intervalo_s, ...}]
        self.hist = {}
        self.grab = None
        self.captura = None
        self.equipos = []
        self._h = 0
        self._dll = None
        self._t0 = 0.0
        self._parar = threading.Event()
        self._hilo = None
        if self.cfg.get("en_uso"):           # lo tenia el panel al cerrarse: se vuelve a tomar
            threading.Thread(target=self._tomar_al_arrancar, daemon=True).start()

    def _tomar_al_arrancar(self):
        try:
            self.tomar("panel (arranque)")
        except Exception as e:
            self.mensaje = str(e)

    # -------------------------------------------------- tomar / soltar el TC-08
    def tomar(self, autor):
        with self.lock:
            if self.estado == "midiendo":
                return self.resumen()
            if _picolog_abierto():
                raise RuntimeError("PicoLog está abierto: el TC-08 solo lo puede usar un programa a la vez. "
                                   "Cierra PicoLog y vuelve a pulsar")
            if self._dll is None:
                self._dll = ctypes.WinDLL(DLL)
            h = self._dll.usb_tc08_open_unit()
            if h <= 0:
                raise RuntimeError("No se encuentra el TC-08 (¿conectado por USB?)" if h == 0
                                   else "No se pudo abrir el TC-08 (¿lo está usando otro programa?)")
            self._h = h
            buf = ctypes.create_string_buffer(256)
            self._dll.usb_tc08_get_unit_info2(h, buf, 256, 4)
            self.serie = buf.value.decode(errors="replace")
            self._arrancar()
            self.cfg["en_uso"] = True
            self._guardar()
        self.reg.anotar(autor, "temperaturas", "TC-08 tomado por el panel",
                        {"serie": self.serie, "intervalo_ms": self.intervalo_ms,
                         "canales": [c["nombre"] for c in self.canales]})
        return self.resumen()

    def soltar(self, autor):
        with self.lock:
            if self.grab:
                raise RuntimeError("Se están registrando las temperaturas (ensayo en marcha): termina antes")
            self._detener()
            if self._h > 0:
                self._dll.usb_tc08_close_unit(self._h)
            self._h = 0
            self.estado, self.mensaje = "libre", "TC-08 libre (lo puede usar PicoLog)"
            self.cfg["en_uso"] = False
            self._guardar()
        self.reg.anotar(autor, "temperaturas", "TC-08 soltado (libre para PicoLog)")
        return self.resumen()

    def cerrar(self):
        """Al cerrar el panel: suelta el TC-08 sin cambiar «en_uso» (al volver a arrancar lo toma otra vez)."""
        try:
            self._parar.set()
            if self._h > 0:
                self._dll.usb_tc08_stop(self._h)
                self._dll.usb_tc08_close_unit(self._h)
        except Exception:
            pass
        self._h = 0
        self.estado = "libre"

    def configurar(self, nuevo, autor):
        """{intervalo_ms, red_hz, canales: {"1": {activo, nombre, tipo, limite_C}}}: se guarda y, si se esta
        midiendo, se aplica al momento (no durante un registro: cambiaria las columnas de temperaturas.csv)."""
        c = copy.deepcopy(self.cfg)
        if "intervalo_ms" in nuevo:
            c["intervalo_ms"] = max(INTERVALO_MIN_MS, int(float(nuevo["intervalo_ms"])))
        if "red_hz" in nuevo:
            c["red_hz"] = 60 if int(nuevo["red_hz"]) == 60 else 50
        for k, v in (nuevo.get("canales") or {}).items():
            if str(k) not in c["canales"]:
                raise ValueError(f"Canal {k} no válido (1 a 8)")
            q = c["canales"][str(k)]
            if "activo" in v:
                q["activo"] = bool(v["activo"])
            if "nombre" in v:
                n = str(v["nombre"]).strip().replace(",", " ")[:40]
                q["nombre"] = n or f"TC{k}"
            if "tipo" in v:
                t = str(v["tipo"]).strip().upper()[:1]
                if t not in TIPOS:
                    raise ValueError(f"Tipo de termopar {v['tipo']} no válido ({', '.join(TIPOS)})")
                q["tipo"] = t
            if "limite_C" in v:
                q["limite_C"] = None if v["limite_C"] in (None, "") else float(v["limite_C"])
        nombres = [q["nombre"] for q in c["canales"].values() if q["activo"]]
        if len(set(nombres)) != len(nombres):
            raise ValueError("Dos canales activos con el mismo nombre")
        cambia_medida = any(c[k] != self.cfg[k] for k in ("intervalo_ms", "red_hz")) or any(
            (c["canales"][k]["activo"], c["canales"][k]["tipo"], c["canales"][k]["nombre"]) !=
            (self.cfg["canales"][k]["activo"], self.cfg["canales"][k]["tipo"], self.cfg["canales"][k]["nombre"])
            for k in c["canales"])
        with self.lock:
            if cambia_medida and self.grab:
                raise RuntimeError("No se pueden cambiar canales ni intervalo durante el registro (ensayo en marcha); "
                                   "los límites de alarma sí")
            self.cfg.clear()
            self.cfg.update(c)
            self._guardar()
            if self.estado == "midiendo" and cambia_medida:
                self._detener()
                self._arrancar()
            elif not cambia_medida:
                for ch in self.canales:
                    ch["limite_C"] = self.cfg["canales"][ch["canal"][2:]]["limite_C"]
        self.reg.anotar(autor, "temperaturas", "configuración del TC-08", nuevo)
        return self.resumen()

    # -------------------------------------------------- medida continua
    def _arrancar(self):
        d, h = self._dll, self._h
        d.usb_tc08_set_mains(h, 1 if self.cfg["red_hz"] == 60 else 0)
        canales = []
        d.usb_tc08_set_channel(h, 0, ord(" "))
        for k in range(1, 9):
            q = self.cfg["canales"][str(k)]
            d.usb_tc08_set_channel(h, k, ord(q["tipo"]) if q["activo"] else ord(" "))
            if q["activo"]:
                canales.append({"id": f"{self.serie}.ch{k}", "canal": f"ch{k}", "n": k, "nombre": q["nombre"],
                                "tipo": f"thermo-{q['tipo'].lower()}", "unidad": "°C", "color": COLORES[k - 1],
                                "limite_C": q["limite_C"], "ultimo": None, "t_ultimo": None, "modo": "average"})
        if not canales:
            self.estado, self.mensaje, self.canales = "midiendo", "Ningún canal activo", []
            return
        real = d.usb_tc08_run(h, int(self.cfg["intervalo_ms"]))
        if real <= 0:
            raise RuntimeError("El TC-08 no acepta esa configuración")
        self._t0 = time.time()
        self.intervalo_ms = real
        for c in canales:
            c["intervalo_s"] = real / 1000
        self.hist = {c["id"]: collections.deque() for c in canales}
        self.canales = canales
        self.captura = {"id": str(uuid.uuid4()), "inicio": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(self._t0)) + "Z",
                        "app": "panel de laboratorio (TC-08 directo)", "timezone": "Europe/Madrid", "en_marcha": True}
        self.equipos = [{"tipo": "TC08Device", "serie": self.serie}]
        self.estado = "midiendo"
        self.mensaje = "" if real == self.cfg["intervalo_ms"] else \
            f"El TC-08 mide cada {real} ms (pedido {self.cfg['intervalo_ms']} ms)"
        self._parar.clear()
        self._hilo = threading.Thread(target=self._bucle, daemon=True)
        self._hilo.start()

    def _detener(self):
        self._parar.set()
        if self._hilo and self._hilo is not threading.current_thread():
            self._hilo.join(timeout=2)
        if self._h > 0:
            self._dll.usb_tc08_stop(self._h)
        if self.estado == "midiendo":
            self.estado = "libre"

    def _bucle(self):
        N = 600
        temp, tms, ovf = (ctypes.c_float * N)(), (ctypes.c_int32 * N)(), ctypes.c_short()
        fallos = 0
        while not self._parar.is_set():
            self._parar.wait(0.02)          # lo mas rapido posible: el dato sale en cuanto el TC-08 lo convierte
            nuevas = {}
            with self.lock:
                if self.estado != "midiendo" or self._h <= 0:
                    return
                for c in self.canales:
                    n = self._dll.usb_tc08_get_temp(self._h, temp, tms, N, ctypes.byref(ovf), c["n"], 0, 0)
                    if n < 0:
                        fallos += 1
                        continue
                    fallos = 0
                    if n:
                        nuevas[c["id"]] = [(round(self._t0 + tms[i] / 1000, 3), float(temp[i])) for i in range(n)]
                if fallos >= 20:
                    self.estado, self.mensaje = "error", "Sin respuesta del TC-08 (¿desconectado?)"
                    self.reg.anotar("panel (automático)", "temperaturas", "TC-08 sin respuesta")
                    return
                if nuevas:
                    self._guardar_muestras(nuevas)

    def _guardar_muestras(self, nuevas):
        for c in self.canales:
            s = nuevas.get(c["id"])
            if not s:
                continue
            h = self.hist[c["id"]]
            h.extend(s)
            while h and h[0][0] < s[-1][0] - HISTORIA_S:
                h.popleft()
            c["t_ultimo"], c["ultimo"] = s[-1]
        if self.grab:
            self._escribir(nuevas)
            for i, s in nuevas.items():
                self.grab["todo"].setdefault(i, {}).update({int(round(t * 1000)): v for t, v in s})

    # -------------------------------------------------- consultas (misma forma que picolog.PicoLog)
    def resumen(self):
        g = self.grab
        alarmas = [c["nombre"] for c in self.canales if c.get("limite_C") is not None and c.get("ultimo") is not None
                   and c["ultimo"] >= c["limite_C"]]
        return {"estado": self.estado, "mensaje": self.mensaje, "serie": self.serie, "intervalo_ms": self.intervalo_ms,
                "config": self.cfg, "captura": self.captura, "equipos": self.equipos, "alarmas": alarmas,
                "canales": [{k: v for k, v in c.items() if k != "n"} for c in self.canales],
                "registro": None if not g else {"fichero": g["fichero"], "desde": g["t0"], "filas": g["filas"]}}

    def historia_desde(self, desde=0.0):
        with self.lock:
            return {"canales": [{"id": c["id"], "nombre": c["nombre"], "unidad": c["unidad"], "color": c["color"],
                                 "limite_C": c.get("limite_C")} for c in self.canales],
                    "datos": {i: [p for p in h if p[0] > desde] for i, h in self.hist.items()}}

    def ficha(self):
        return {"captura": self.captura, "equipos": self.equipos, "origen": "TC-08 manejado por el panel",
                "intervalo_ms": self.intervalo_ms, "red_hz": self.cfg["red_hz"],
                "canales": [{k: c[k] for k in ("id", "canal", "nombre", "tipo", "unidad", "intervalo_s", "limite_C")}
                            for c in self.canales]}

    # -------------------------------------------------- temperaturas.csv y temperaturas.picolog
    def iniciar_registro(self, fichero, autor, pre_s=30.0, cabecera=None):
        with self.lock:
            if self.grab:
                raise RuntimeError("Ya se están registrando las temperaturas")
            if self.estado != "midiendo" or not self.canales:
                raise RuntimeError("El TC-08 no está midiendo (o no hay canales activos)")
            t0 = time.time()
            f = open(fichero, "w", encoding="utf-8", newline="")
            f.write("# Registro de temperaturas (TC-08 manejado por el panel de laboratorio). MATLAB: osc(ruta)\n")
            for k, v in [("inicio", time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t0))),
                         ("equipo", f"Pico TC-08 {self.serie}, filtro de red {self.cfg['red_hz']} Hz"),
                         ("canales", "; ".join(f"{c['nombre']} = {c['canal']} {c['tipo']} cada {c['intervalo_s']:g} s"
                                               for c in self.canales)),
                         ("t_rel_s", "segundos desde el inicio; negativo = antes de empezar (lo que ya estaba en memoria)"),
                         ("hora", "reloj del PC (hora local) al medir; t_unix_s en segundos Unix")] + list((cabecera or {}).items()):
                f.write(f"# {k}: {v}\n")
            f.write(",".join(["fecha_hora", "t_unix_s", "t_rel_s"] + [f"{c['nombre']}_C" for c in self.canales]) + "\n")
            self.grab = {"fichero": fichero, "f": f, "t0": t0, "filas": 0, "canales": [c["id"] for c in self.canales],
                         "todo": {}, "info": [dict(c) for c in self.canales]}
            previas = {i: [p for p in self.hist.get(i, ()) if p[0] >= t0 - max(0.0, float(pre_s or 0))]
                       for i in self.grab["canales"]}
            self._escribir(previas)
            for i, s in previas.items():
                self.grab["todo"].setdefault(i, {}).update({int(round(t * 1000)): v for t, v in s})
        self.reg.anotar(autor, "temperaturas", "registro de temperaturas iniciado",
                        {"fichero": fichero, "canales": [c["nombre"] for c in self.canales]})

    def _escribir(self, nuevas):
        g = self.grab
        filas = collections.defaultdict(dict)
        for i in g["canales"]:
            for t, v in nuevas.get(i, ()):
                filas[round(t, 3)][i] = v
        for t in sorted(filas):
            vals = [filas[t].get(i) for i in g["canales"]]
            hora = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t)) + f".{int(round(t % 1 * 1000)) % 1000:03d}"
            g["f"].write(f"{hora},{t:.3f},{t - g['t0']:.3f}," + ",".join("" if v is None else f"{v:.3f}" for v in vals) + "\n")
            g["filas"] += 1
        g["f"].flush()

    def parar_registro(self, autor, motivo="parado"):
        with self.lock:
            g = self.grab
            if not g:
                return None
            g["f"].close()
            self.grab = None
        picolog = os.path.splitext(g["fichero"])[0] + ".picolog"
        try:
            self._escribir_picolog(picolog, g)
        except Exception as e:
            picolog = None
            self.reg.anotar("panel (automático)", "temperaturas", "NO se pudo escribir el .picolog", str(e))
        self.reg.anotar(autor, "temperaturas", "registro de temperaturas terminado",
                        {"fichero": g["fichero"], "picolog": picolog, "filas": g["filas"], "motivo": motivo})
        return g["fichero"]

    def _escribir_picolog(self, ruta, g):
        """temperaturas.picolog con la estructura de un .picolog de PicoLog (plantilla_picolog.json)."""
        meta = json.load(open(PLANTILLA, encoding="utf-8"))["metadata"]
        dev = meta["devices"]["items"][0]
        serie = self.serie or dev.get("serialInternal", "")
        dev["serialInternal"] = serie
        todos = [m for d in g["todo"].values() for m in d]
        iv = int(round(g["info"][0]["intervalo_s"] * 1000)) if g["info"] else 200
        iso = lambda ms: time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ms / 1000)) + f".{int(ms) % 1000:03d}Z"
        ahora = int(time.time() * 1000)
        meta.update(id=str(uuid.uuid4()), startDate=iso(min(todos) if todos else ahora), modifiedDate=iso(ahora),
                    savedDate=iso(ahora), isRunning=False, hasStarted=True, hasStopped=True, annotations=[],
                    alarms=[], labels=[])
        info = {c["canal"]: c for c in g["info"]}
        canales = []
        for k, ch in dev.items():
            if not (isinstance(ch, dict) and ch.get("channelIdentifier")):
                continue
            ch["deviceSerial"] = serie
            c = info.get(ch["channelIdentifier"])
            ch["enabled"] = c is not None
            ch["captureConfig"] = {"interval": iv, "mode": "average"}   # como en los .picolog de PicoLog
            ch["capturedSamples"] = 0
            if c:
                m = g["todo"].get(c["id"], {})
                # en PicoLog el tiempo de cada muestra es un multiplo del intervalo: se redondea a la rejilla
                rejilla = {int(round(t / iv)) * iv: v for t, v in m.items()}
                v = list(rejilla.values())
                ch.update(description=c["nombre"], input=c["tipo"], capturedSamples=len(v),
                          min=min(v) if v else None, max=max(v) if v else None, valuePrivate=v[-1] if v else None)
                canales.append({"clave": f"tc-08.{serie.replace('/', '|')}.{c['canal']}", "muestras": rejilla})
        if "tableSettings" in meta:
            meta["tableSettings"]["interval"] = iv
            meta["tableSettings"]["columns"] = [
                dict(col, channelKey=f"tc-08.{serie}.{c['canal']}") for col, c in
                zip(meta["tableSettings"].get("columns", [{}]) * len(g["info"]), g["info"])]
        picolog_fichero.escribir(ruta, canales, iv, meta)
