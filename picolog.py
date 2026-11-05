"""Temperaturas de PicoLog en el panel (nivel 1, acordado con el usuario el 2026-11-03).

PicoLog 6 sigue mandando (el usuario configura y captura en el como siempre); el panel SOLO LEE lo que PicoLog va
guardando, sin tocar el TC-08 ni la aplicacion. PicoLog es una aplicacion Electron y guarda en
%LOCALAPPDATA%\\PicoLog dos bases LevelDB (estudiadas el 2026-11-03 con PicoLog 6.2.13 y un TC-08):

  Capture-Data  la captura en curso. Claves "<equipo>|<serie>.<canal>/<nivel>/<inicio del bloque, ms Unix>"
                (las mismas que el data-map de un .picolog). Nivel 1 = muestras en bruto: float32 big-endian, NaN
                donde aun no hay dato, 1000 por bloque. PicoLog reescribe el bloque al añadir datos y lo vuelca
                enseguida a tablas .ldb (comprimidas con snappy). Al empezar una captura borra la anterior.
  IndexedDB     la configuracion: clave CurrentCaptureState = JSON en UTF-16 con la misma forma que metadata.json
                (devices.items[].thermocoupleN: channelIdentifier, description, input 'thermo-k', enabled,
                captureConfig.interval en ms), isRunning, startDate.

Se leen los ficheros directamente (PicoLog los deja leer; solo el LOCK esta bloqueado). Las tablas .ldb no cambian
una vez escritas: se leen una vez. Retraso medido respecto a PicoLog: ~1 s.

Durante un ensayo se graba temperaturas.csv (mismo reloj del PC que fuente.csv) con los nombres de los canales de
PicoLog (los pone el usuario segun el ensayo: TC1, TC2...).
"""
import calendar
import collections
import json
import os
import struct
import subprocess
import threading
import time

BASE = os.path.join(os.environ.get("LOCALAPPDATA", ""), "PicoLog")
DATOS = os.path.join(BASE, "Capture-Data")
CONFIG = os.path.join(BASE, "IndexedDB", "file__0.indexeddb.leveldb")
HISTORIA_S = 2 * 3600
CADA_S = 1.0
CONFIG_CADA_S = 3.0


# ------------------------------------------------------------------- lector LevelDB minimo (solo lectura)
def _varint(b, i):
    r = s = 0
    while True:
        x = b[i]
        i += 1
        r |= (x & 0x7F) << s
        s += 7
        if x < 128:
            return r, i


def _snappy(b):
    n, i = _varint(b, 0)
    out = bytearray()
    while i < len(b):
        t = b[i]
        i += 1
        k = t & 3
        if k == 0:
            ln = (t >> 2) + 1
            if ln > 60:
                nb = ln - 60
                ln = int.from_bytes(b[i:i + nb], "little") + 1
                i += nb
            out += b[i:i + ln]
            i += ln
            continue
        if k == 1:
            ln, off = 4 + ((t >> 2) & 7), ((t >> 5) << 8) | b[i]
            i += 1
        elif k == 2:
            ln, off = (t >> 2) + 1, int.from_bytes(b[i:i + 2], "little")
            i += 2
        else:
            ln, off = (t >> 2) + 1, int.from_bytes(b[i:i + 4], "little")
            i += 4
        for _ in range(ln):                  # la copia puede solaparse con lo que se esta escribiendo
            out.append(out[-off])
    if len(out) != n:
        raise ValueError("snappy: longitud distinta")
    return bytes(out)


def _entradas(blk):
    nrest = struct.unpack("<I", blk[-4:])[0]
    fin = len(blk) - 4 - 4 * nrest
    i, clave = 0, b""
    while i < fin:
        sh, i = _varint(blk, i)
        ns, i = _varint(blk, i)
        vl, i = _varint(blk, i)
        clave = clave[:sh] + blk[i:i + ns]
        i += ns
        yield clave, blk[i:i + vl]
        i += vl


def _tabla(f):
    """Entradas (clave, secuencia, tipo, valor) de una tabla .ldb."""
    def bloque(off, size):
        d = f[off:off + size]
        return _snappy(d) if f[off + size] == 1 else d
    pie = f[-48:]
    if pie[-8:] != struct.pack("<Q", 0xdb4775248b80fb57):
        raise ValueError("no es una tabla LevelDB")
    _, j = _varint(pie, 0)
    _, j = _varint(pie, j)
    ioff, j = _varint(pie, j)
    isz, _ = _varint(pie, j)
    for _, h in _entradas(bloque(ioff, isz)):
        o, k = _varint(h, 0)
        s, _ = _varint(h, k)
        for ik, v in _entradas(bloque(o, s)):
            st = struct.unpack("<Q", ik[-8:])[0]
            yield ik[:-8], st >> 8, st & 0xFF, v


def _registro(b):
    """Entradas del registro .log (lotes de escritura en bloques de 32 KiB)."""
    frag = b""
    for blk in range(0, len(b), 32768):
        i = blk
        while i + 7 <= min(blk + 32768, len(b)):
            _, ln, tp = struct.unpack("<IHB", b[i:i + 7])
            if tp == 0 and ln == 0:
                break
            d = b[i + 7:i + 7 + ln]
            i += 7 + ln
            if tp == 2:
                frag = d
                continue
            if tp == 3:
                frag += d
                continue
            rec = d if tp == 1 else frag + d
            if len(rec) < 12:
                continue
            seq, n = struct.unpack("<QI", rec[:12])
            j = 12
            try:
                for q in range(n):
                    t = rec[j]
                    j += 1
                    kl, j = _varint(rec, j)
                    k = rec[j:j + kl]
                    j += kl
                    v = b""
                    if t == 1:
                        vl, j = _varint(rec, j)
                        v = rec[j:j + vl]
                        j += vl
                    yield k, seq + q, t, v
            except IndexError:                   # lote a medio escribir: el resto en la siguiente lectura
                return


class _Base:
    """Una base LevelDB leida «por encima»: ultima version de cada clave que cumpla 'filtro'. Las tablas .ldb se
    leen una sola vez (no cambian); el .log se relee siempre."""

    def __init__(self, carpeta, filtro):
        self.carpeta, self.filtro = carpeta, filtro
        self._tablas = {}

    def leer(self):
        ultimo = {}

        def poner(k, seq, t, v):
            if self.filtro(k) and (k not in ultimo or seq > ultimo[k][0]):
                ultimo[k] = (seq, t, v)
        nombres = sorted(os.listdir(self.carpeta))
        vivas = set()
        for n in nombres:
            r = os.path.join(self.carpeta, n)
            try:
                if n.endswith(".ldb"):
                    vivas.add(n)
                    if n not in self._tablas:
                        with open(r, "rb") as fh:
                            self._tablas[n] = [e for e in _tabla(fh.read()) if self.filtro(e[0])]
                    for e in self._tablas[n]:
                        poner(*e)
                elif n.endswith(".log") and n[0].isdigit():
                    with open(r, "rb") as fh:
                        for e in _registro(fh.read()):
                            poner(*e)
            except (OSError, ValueError, IndexError, struct.error):
                continue                         # a medio escribir o recien borrado: en la siguiente lectura
        for n in list(self._tablas):             # tablas que PicoLog ya ha compactado y borrado
            if n not in vivas:
                del self._tablas[n]
        return {k: v for k, (s, t, v) in ultimo.items() if t == 1}


# ------------------------------------------------------------------- PicoLog
class PicoLog:
    def __init__(self, registro):
        self.reg = registro
        self.lock = threading.Lock()
        self.estado = "sin datos"            # sin datos | cerrado | parado | capturando
        self.mensaje = ""
        self.captura = None                  # {id, inicio, app, timezone, en_marcha}
        self.equipos = []                    # [{tipo, serie}]
        self.canales = []                    # [{id, nombre, tipo, unidad, intervalo_s, sufijo, ultimo, t_ultimo}]
        self.hist = {}                       # id -> deque[(t, v)]
        self.grab = None                     # temperaturas.csv en marcha
        self._ult = {}
        self._t_config = 0.0
        self._datos = _Base(DATOS, lambda k: b"/1/" in k)
        # en las claves de IndexedDB el texto va en UTF-16 big-endian; en el valor, little-endian
        clave = "CurrentCaptureState"
        self._config = _Base(CONFIG, lambda k: clave.encode("utf-16-be") in k or clave.encode("utf-16-le") in k)
        threading.Thread(target=self._bucle, daemon=True).start()

    # -------------------------------------------------- lectura
    def _bucle(self):
        while True:
            t0 = time.time()
            try:
                if not os.path.isdir(DATOS):
                    self.estado, self.mensaje = "sin datos", "No se encuentra PicoLog en este PC"
                else:
                    if time.time() - self._t_config >= CONFIG_CADA_S:
                        self._t_config = time.time()
                        self._leer_config()
                    if self.canales and self.estado != "cerrado":
                        self._leer_datos()
            except Exception as e:               # que se vea, sin parar el hilo
                self.mensaje = f"Error leyendo PicoLog: {type(e).__name__}: {e}"
            time.sleep(max(0.05, CADA_S - (time.time() - t0)))

    def _abierto(self):
        """PicoLog en marcha (si se cierra, su configuracion guardada sigue diciendo «capturando»)."""
        try:
            r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq PicoLog.exe", "/NH"], capture_output=True, timeout=5,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return b"PicoLog.exe" in r.stdout
        except Exception:
            return True                          # si no se puede saber, no se bloquea nada

    def _leer_config(self):
        if not self._abierto():
            with self.lock:
                self.estado, self.mensaje = "cerrado", "PicoLog está cerrado"
            return
        vals = self._config.leer()
        if not vals:
            self.estado, self.mensaje, self.canales = "sin datos", "PicoLog no tiene ninguna captura abierta", []
            return
        marca = '{"current"'.encode("utf-16-le")        # hay otras claves con ese nombre (indices): la del JSON
        v = next((x for x in sorted(vals.values(), key=len, reverse=True) if marca in x), None)
        if v is None:
            self.estado, self.mensaje, self.canales = "sin datos", "No se entiende la configuración de PicoLog", []
            return
        i = v.find(marca)
        txt = v[i:].decode("utf-16-le", errors="ignore")
        cur = json.loads(txt[:txt.rindex("}") + 1])["current"]
        cap = {"id": cur.get("id"), "inicio": cur.get("startDate"), "app": cur.get("appVersion"),
               "timezone": cur.get("timezone"), "en_marcha": bool(cur.get("isRunning"))}
        canales, equipos = [], []
        for d in (cur.get("devices") or {}).get("items") or []:
            if not isinstance(d, dict) or not d.get("serialInternal"):
                continue
            serie = d["serialInternal"]
            equipos.append({"tipo": d.get("__type", ""), "serie": serie})
            for nombre_campo, ch in d.items():
                if not (isinstance(ch, dict) and ch.get("channelIdentifier") and ch.get("enabled")):
                    continue
                entrada = ch.get("input", "")
                canales.append({
                    "id": f"{serie}.{ch['channelIdentifier']}", "canal": ch["channelIdentifier"],
                    "nombre": ch.get("description") or ch["channelIdentifier"], "tipo": entrada,
                    "unidad": "°C" if entrada.startswith("thermo") else ch.get("unit", ""),
                    "intervalo_s": (ch.get("captureConfig") or {}).get("interval", 1000) / 1000,
                    "modo": (ch.get("captureConfig") or {}).get("mode", ""),
                    "sufijo": f"{serie.replace('/', '|')}.{ch['channelIdentifier']}".encode(),
                    "color": ch.get("color", "")})
        with self.lock:
            if self.captura is None or cap["id"] != self.captura.get("id"):
                if self.captura is not None:
                    self.reg.anotar("PicoLog", "picolog", "captura nueva en PicoLog", {"inicio": cap["inicio"]})
                self.hist, self._ult = {}, {}           # captura nueva: lo anterior ya no vale
            for c in canales:                            # lo ultimo leido se conserva al releer la configuracion
                previo = next((p for p in self.canales if p["id"] == c["id"]), None)
                c["ultimo"], c["t_ultimo"] = (previo or {}).get("ultimo"), (previo or {}).get("t_ultimo")
            self.captura, self.canales, self.equipos = cap, canales, equipos
            self.estado = "capturando" if cap["en_marcha"] else "parado"
            self.mensaje = "" if canales else "La captura de PicoLog no tiene canales activos"

    def _leer_datos(self):
        bloques = self._datos.leer()
        try:
            inicio = calendar.timegm(time.strptime(self.captura["inicio"][:19], "%Y-%m-%dT%H:%M:%S")) \
                if self.captura and self.captura.get("inicio") else 0       # startDate viene en UTC
        except ValueError:
            inicio = 0
        nuevas = collections.defaultdict(list)
        for c in self.canales:
            dt, ult = c["intervalo_s"], self._ult.get(c["id"], inicio - 1)
            for k, v in bloques.items():
                cab, _, ini = k.rpartition(b"/")
                if not cab.split(b"/")[0].endswith(c["sufijo"]):
                    continue
                t_ini, n = int(ini) / 1000, len(v) // 4
                if t_ini + n * dt <= ult:
                    continue
                for i, y in enumerate(struct.unpack(f">{n}f", v[:4 * n])):
                    t = round(t_ini + i * dt, 4)
                    if y == y and t > ult:
                        nuevas[c["id"]].append((t, y))
            nuevas[c["id"]].sort()
        if not any(nuevas.values()):
            return
        with self.lock:
            for c in self.canales:
                s = nuevas.get(c["id"])
                if not s:
                    continue
                h = self.hist.setdefault(c["id"], collections.deque())
                h.extend(s)
                while h and h[0][0] < s[-1][0] - HISTORIA_S:
                    h.popleft()
                self._ult[c["id"]] = s[-1][0]
                c["t_ultimo"], c["ultimo"] = s[-1]
            if self.grab:
                self._escribir(nuevas)

    # -------------------------------------------------- consultas
    def _publico(self, c):
        return {k: v for k, v in c.items() if k != "sufijo"}

    def resumen(self):
        g = self.grab
        return {"estado": self.estado, "mensaje": self.mensaje, "captura": self.captura, "equipos": self.equipos,
                "canales": [self._publico(c) for c in self.canales],
                "registro": None if not g else {"fichero": g["fichero"], "desde": g["t0"], "filas": g["filas"]}}

    def historia_desde(self, desde=0.0):
        with self.lock:
            return {"canales": [{"id": c["id"], "nombre": c["nombre"], "unidad": c["unidad"], "color": c["color"]}
                                for c in self.canales],
                    "datos": {i: [p for p in h if p[0] > desde] for i, h in self.hist.items()}}

    def ficha(self):
        """Lo que se guarda en ensayo.json al empezar: la configuracion de PicoLog tal como la tenia el usuario."""
        return {"captura": self.captura, "equipos": self.equipos,
                "canales": [{k: c[k] for k in ("id", "canal", "nombre", "tipo", "unidad", "intervalo_s", "modo")}
                            for c in self.canales],
                "nota": "leido de PicoLog (la configuracion y la captura las lleva PicoLog)"}

    # -------------------------------------------------- temperaturas.csv
    def iniciar_registro(self, fichero, autor, pre_s=30.0, cabecera=None):
        with self.lock:
            if self.grab:
                raise RuntimeError("Ya se están registrando las temperaturas")
            if not self.canales:
                raise RuntimeError("PicoLog no tiene canales activos")
            t0 = time.time()
            canales = [dict(self._publico(c)) for c in self.canales]
            f = open(fichero, "w", encoding="utf-8", newline="")
            f.write("# Registro de temperaturas (PicoLog, leido por el panel de laboratorio). MATLAB: osc(ruta)\n")
            for k, v in [("inicio", time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t0))),
                         ("picolog", f"PicoLog {self.captura.get('app', '')}, captura desde {self.captura.get('inicio', '')} (UTC)"),
                         ("equipos", ", ".join(f"{e['tipo']} {e['serie']}" for e in self.equipos)),
                         ("canales", "; ".join(f"{c['nombre']} = {c['canal']} {c['tipo']} cada {c['intervalo_s']:g} s"
                                               for c in canales)),
                         ("t_rel_s", "segundos desde el inicio; negativo = antes de empezar (lo que ya estaba en memoria)"),
                         ("hora", "reloj del PC (hora local); t_unix_s en segundos Unix")] + list((cabecera or {}).items()):
                f.write(f"# {k}: {v}\n")
            nombres = [c["nombre"].replace(",", " ") + (f"_{c['unidad']}".replace("°", "") if c["unidad"] else "")
                       for c in canales]
            f.write(",".join(["fecha_hora", "t_unix_s", "t_rel_s"] + nombres) + "\n")
            self.grab = {"fichero": fichero, "f": f, "t0": t0, "filas": 0, "canales": [c["id"] for c in canales]}
            previas = collections.defaultdict(list)
            for i in self.grab["canales"]:
                previas[i] = [p for p in self.hist.get(i, ()) if p[0] >= t0 - max(0.0, float(pre_s or 0))]
            self._escribir(previas)
        self.reg.anotar(autor, "picolog", "registro de temperaturas iniciado",
                        {"fichero": fichero, "canales": [c["nombre"] for c in canales]})

    def _escribir(self, nuevas):
        """Con el lock: una fila por instante (los canales con el mismo intervalo comparten fila)."""
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
        self.reg.anotar(autor, "picolog", "registro de temperaturas terminado",
                        {"fichero": g["fichero"], "filas": g["filas"], "motivo": motivo})
        return g["fichero"]
