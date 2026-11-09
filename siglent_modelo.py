r"""Modelo de la fuente Siglent SPD3303X y su traducción a SCPI (9-nov-2026).

Norma del usuario para equipos con driver: el panel es una traducción 1:1 de lo que admite el equipo, probado en el
hardware (herramientas/siglent_descubrir.py, siglent_capacidades.py, siglent_validar_modelo.py) y contrastado con la
documentación de Siglent (C:\Proyectos\_HERRAMIENTAS\Documentos Sigilent: Quick Start E02A, Datasheet E03A).

Transporte: NI-VISA (USBTMC). El «driver» solo transporta SCPI; lo que el equipo admite lo deciden sus órdenes.
  - Escribir y leer POR SEPARADO con pausa (write + read_raw): con query() se colgó dentro del driver USB (9-nov).
  - Terminador «\n». Órdenes cada 50 ms sin fallos (medido); el panel usa PAUSA_S = 0,1 s.
Lo que el EQUIPO NO vigila y el modelo sí: acepta consignas negativas (VOLT -1) y por encima del máximo se queda en el
máximo SIN error. Sin vigilante de comunicación ni OCP por SCPI (el OCP solo desde el frontal): el panel apaga al
cerrarse o al perder la comunicación. El temporizador interno no se usa (mientras corre no contesta a las medidas).
"""

IDN_MODELO = "SPD3303X"
PAUSA_S = 0.1
CANALES = ("CH1", "CH2")                  # ajustables
CH3 = "CH3"                               # 2,5 / 3,3 / 5 V por conmutador del frontal, 3,2 A; solo ON/OFF por SCPI
V_MAX, I_MAX = 32.0, 3.2                  # por canal (Quick Start: 0~32 V / 0~3,2 A)
RESOLUCION_V, RESOLUCION_I = 0.001, 0.001
EXACTITUD = {"V": (0.0003, 0.010), "I": (0.003, 0.010)}   # ±(proporción de la lectura + fijo), datasheet E03A
MODOS = {"independiente": 0, "serie": 1, "paralelo": 2}    # OUTPut:TRACK {0|1|2}
# máximos de la SALIDA en cada modo (Quick Start: serie 0~60 V/0~3,2 A, controlado por CH1; paralelo 0~32 V/0~6,4 A)
MAX_MODO = {"independiente": (V_MAX, I_MAX), "serie": (60.0, I_MAX), "paralelo": (V_MAX, 2 * I_MAX)}

ORDENES = {   # operación del panel -> orden SCPI (Quick Start, «Command description»)
    "identificar": "*IDN?",
    "estado": "SYSTem:STATus?",
    "error": "SYSTem:ERRor?",
    "fijar_V": "{c}:VOLTage {v:.3f}", "leer_V": "{c}:VOLTage?",
    "fijar_I": "{c}:CURRent {v:.3f}", "leer_I": "{c}:CURRent?",
    "medir_V": "MEASure:VOLTage? {c}", "medir_I": "MEASure:CURRent? {c}", "medir_P": "MEASure:POWEr? {c}",
    "salida": "OUTPut {c},{onoff}",
    "modo": "OUTPut:TRACK {n}",
    "onda": "OUTPut:WAVE {c},{onoff}",
}


def estado(hexa):
    """SYSTem:STATus? -> dict. Bits (Quick Start): 0/1 CH1/CH2 CC; 2-3 modo (01 indep., 10 paralelo; serie = 11,
    medido el 9-nov); 4/5 CH1/CH2 ON; 6/7 temporizadores; 8/9 forma de onda en pantalla."""
    v = int(str(hexa).strip(), 16)
    modo = {1: "independiente", 3: "serie", 2: "paralelo"}.get((v >> 2) & 3, f"desconocido({(v >> 2) & 3})")
    return {"CH1": {"regula": "CC" if v & 1 else "CV", "salida": bool(v & 16), "onda": bool(v & 256)},
            "CH2": {"regula": "CC" if v & 2 else "CV", "salida": bool(v & 32), "onda": bool(v & 512)},
            "modo": modo, "temporizador": [bool(v & 64), bool(v & 128)]}


def comprobar(cfg, topes=None, salidas_encendidas=False):
    """Lo que el equipo no vigila: devuelve la lista de errores (vacía = se puede).
    cfg: {"modo", "CH1": {"V", "I"}, "CH2": {"V", "I"}}; topes: {"V", "I"} de seguridad del panel (≤ los del equipo)."""
    e = []
    modo = cfg.get("modo", "independiente")
    if modo not in MODOS:
        e.append(f"modo {modo!r} no válido ({', '.join(MODOS)})")
        return e
    if cfg.get("cambia_modo") and salidas_encendidas:
        e.append("el modo (independiente/serie/paralelo) solo se cambia con las salidas apagadas")
    tv, ti = (topes or {}).get("V", V_MAX), (topes or {}).get("I", I_MAX)
    # cada canal: 0-32 V / 0-3,2 A. En SERIE la consigna de CH1 se copia en CH2 y la salida es la suma; el equipo
    # deja la consigna en 30 V SIN AVISAR si se pide más (medido el 9-nov: 32 -> 30): 60 V en total (Quick Start).
    # En paralelo, 32 V y 3,2 A por canal (6,4 A en total)
    vmax_c = MAX_MODO["serie"][0] / 2 if modo == "serie" else V_MAX
    for c in CANALES:
        q = cfg.get(c) or {}
        if modo != "independiente" and c == "CH2":
            continue                     # en serie y paralelo manda CH1
        for k, mx, tope, u in (("V", vmax_c, tv, "V"), ("I", I_MAX, ti, "A")):
            if q.get(k) is None:
                continue
            x = float(q[k])
            if x < 0:
                e.append(f"{c}: {k} = {x:g} {u} negativo (el equipo lo aceptaría sin avisar)")
            elif x > mx + 1e-9:
                e.append(f"{c}: {k} = {x:g} {u} por encima del máximo del equipo ({mx:g} {u}; se quedaría en {mx:g} sin avisar)")
            elif x > tope + 1e-9:
                e.append(f"{c}: {k} = {x:g} {u} por encima del tope de seguridad del panel ({tope:g} {u})")
    return e


_RM = None
ID_USB = "0x0483::0x7540"


def _rm():
    """Un solo ResourceManager de NI-VISA por proceso (9-nov: se creaba uno nuevo en cada reintento y no se cerraba)."""
    global _RM
    if _RM is None:
        import pyvisa
        _RM = pyvisa.ResourceManager()
    return _RM


def presente(recurso=None):
    """¿Ve NI-VISA la fuente por USB? (solo lista, no abre sesión)."""
    rs = [r for r in _rm().list_resources("USB?*::INSTR") if ID_USB in r]
    return bool(rs) if not recurso else recurso in rs


def dentro_de_exactitud(magnitud, consigna, medida):
    p, fijo = EXACTITUD[magnitud]
    return abs(medida - consigna) <= p * abs(consigna) + fijo


class Enlace:
    """Transporte NI-VISA a la SPD3303X: escribir y leer por separado, con pausa, de un hilo a la vez."""

    def __init__(self, recurso=None, pausa_s=PAUSA_S, timeout_ms=1500):
        import threading
        self.rm = _rm()
        if not recurso:
            rs = [r for r in self.rm.list_resources("USB?*::INSTR") if ID_USB in r]
            if not rs:
                raise ConnectionError("NI-VISA no ve la Siglent SPD3303X (¿USB conectado y fuente encendida?)")
            recurso = rs[0]
        self.recurso, self.pausa = recurso, pausa_s
        self.f = self.rm.open_resource(recurso, open_timeout=2000)
        self.f.write_termination = "\n"
        self.f.timeout = timeout_ms
        self.lock = threading.Lock()
        # 9-nov: al abrir una sesión nueva (p. ej. tras reiniciar el panel) la fuente puede tener una respuesta a medias
        # de la anterior y el primer *IDN? se colgaba en el driver: «device clear» de USBTMC antes de nada
        try:
            self.f.clear()
        except Exception:
            pass

    def escribir(self, orden):
        import time
        with self.lock:
            self.f.write(orden)
            time.sleep(self.pausa)

    def consultar(self, orden):
        import time
        with self.lock:
            self.f.write(orden)
            time.sleep(self.pausa)
            return self.f.read_raw().decode("ascii", "replace").strip()

    def numero(self, orden, intentos=3):
        """Respuesta numérica; la fuente a veces contesta vacío (visto con el temporizador): se reintenta."""
        ultimo = None
        for _ in range(intentos):
            try:
                r = self.consultar(orden)
                if r:
                    return float(r)
            except Exception as e:
                ultimo = e
        raise ConnectionError(f"Sin respuesta a {orden!r}" + (f" ({type(ultimo).__name__})" if ultimo else ""))

    def cerrar(self):
        try:
            self.f.close()
        except Exception:
            pass


# ------------------------------------------------------------------ enlace en un proceso aparte (9-nov-2026)
# CAUSA del cuelgue visto el 9-nov: si la fuente deja de contestar por USB (p. ej. al reenchufarla), la lectura se
# queda ~100 s dentro del driver USBTMC de Windows sin respetar el timeout de VISA, y Windows no deja terminar el
# proceso mientras tanto: el panel no podía cerrarse ni reiniciarse (el nuevo encontraba el puerto ocupado).
# SOLUCIÓN: NI-VISA solo se usa en un proceso hijo. Cada orden tiene un tiempo límite; si no vuelve, el hijo se
# abandona (se manda matar sin esperarlo) y el panel sigue: lo trata como pérdida de comunicación y abre otro.
LIMITE_ORDEN_S = 5.0
LIMITE_ABRIR_S = 15.0
LIMITE_LISTAR_S = 10.0
_ABANDONADOS = []          # hijos mandados matar que el driver aún retiene


def hijos_retenidos():
    """Cuántos procesos de comunicación abandonados siguen vivos (retenidos por el driver USB)."""
    _ABANDONADOS[:] = [p for p in _ABANDONADOS if p.poll() is None]
    return len(_ABANDONADOS)


class EnlaceProceso:
    """Misma interfaz que Enlace (escribir, consultar, numero, cerrar), con NI-VISA en un proceso hijo."""

    def __init__(self, recurso=None, pausa_s=PAUSA_S, timeout_ms=1500):
        import json
        import queue
        import subprocess
        import sys
        import threading
        self._json, self._queue = json, queue
        self.lock = threading.Lock()
        self.muerto = False
        self.cola = queue.Queue()
        self.p = subprocess.Popen([sys.executable, "-u", __file__, "--hijo", recurso or "", str(pausa_s), str(timeout_ms)],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        threading.Thread(target=self._lector, daemon=True, name="siglent-hijo").start()
        self.recurso = self._pedir({"op": "abrir"}, LIMITE_ABRIR_S)

    def _lector(self):
        try:
            for linea in self.p.stdout:
                try:
                    self.cola.put(self._json.loads(linea))
                except ValueError:
                    pass
        finally:
            self.cola.put({"error": "el proceso de comunicación con la Siglent ha terminado", "fin": True})

    def _pedir(self, d, limite=LIMITE_ORDEN_S):
        with self.lock:
            if self.muerto:
                raise ConnectionError("enlace con la Siglent abandonado")
            try:
                self.p.stdin.write((self._json.dumps(d) + "\n").encode("utf-8"))
                self.p.stdin.flush()
                r = self.cola.get(timeout=limite)
            except self._queue.Empty:
                self._abandonar()
                raise ConnectionError(f"la Siglent no contesta en {limite:g} s (driver USB colgado): "
                                      "se abandona ese proceso de comunicación y se abrirá otro") from None
            except OSError as e:
                self._abandonar()
                raise ConnectionError(f"proceso de comunicación con la Siglent caído: {e}") from None
            if r.get("fin"):
                self._abandonar()
            if r.get("error"):
                raise ConnectionError(r["error"])
            return r.get("r")

    def _abandonar(self):
        self.muerto = True
        try:
            self.p.kill()            # no se espera: si el driver lo retiene, que termine cuando lo suelte
        except Exception:
            pass
        if self.p not in _ABANDONADOS:
            _ABANDONADOS.append(self.p)

    def escribir(self, orden):
        self._pedir({"op": "w", "o": orden})

    def consultar(self, orden):
        return self._pedir({"op": "q", "o": orden})

    def numero(self, orden, intentos=3):
        ultimo = None
        for _ in range(intentos):
            try:
                r = self.consultar(orden)
                if r:
                    return float(r)
            except ConnectionError as e:
                ultimo = e
                if self.muerto:
                    break
        raise ConnectionError(f"Sin respuesta a {orden!r}" + (f" ({ultimo})" if ultimo else ""))

    def cerrar(self):
        if not self.muerto:
            try:
                self._pedir({"op": "cerrar"}, 3.0)
            except Exception:
                pass
        self._abandonar()


def presente_aislado(recurso=None):
    """presente() en un proceso aparte con tiempo límite: True / False, o None si NI-VISA no contesta."""
    import subprocess
    import sys
    try:
        r = subprocess.run([sys.executable, "-u", __file__, "--listar", recurso or ""], capture_output=True,
                           timeout=LIMITE_LISTAR_S, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return r.stdout.decode("ascii", "replace").strip() == "1"
    except subprocess.TimeoutExpired:
        return None


def _hijo(recurso, pausa_s, timeout_ms):
    """Bucle del proceso hijo: una orden JSON por línea en stdin, una respuesta por línea en stdout."""
    import json
    import os
    import sys
    import time
    e = None
    # solo para pruebas sin equipo (pruebas/prueba_siglent_sin_hardware.py): imita un driver que no devuelve la lectura
    cuelga = os.environ.get("SIGLENT_HIJO_PRUEBA") == "cuelga"
    for linea in sys.stdin:
        try:
            d = json.loads(linea)
            op = d.get("op")
            if cuelga:
                if op == "q":
                    time.sleep(3600)
                r = "PRUEBA" if op == "abrir" else None
            elif op == "abrir":
                e = Enlace(recurso or None, pausa_s, timeout_ms)
                r = e.recurso
            elif op == "w":
                e.escribir(d["o"])
                r = None
            elif op == "q":
                r = e.consultar(d["o"])
            elif op == "cerrar":
                if e:
                    e.cerrar()
                sys.stdout.write(json.dumps({"r": None}) + "\n")
                sys.stdout.flush()
                return
            else:
                raise ValueError(f"orden desconocida {op!r}")
            sys.stdout.write(json.dumps({"r": r}) + "\n")
        except Exception as x:
            sys.stdout.write(json.dumps({"error": f"{type(x).__name__}: {x}"}) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    import sys as _sys
    if len(_sys.argv) > 1 and _sys.argv[1] == "--hijo":
        _hijo(_sys.argv[2], float(_sys.argv[3]), int(_sys.argv[4]))
    elif len(_sys.argv) > 1 and _sys.argv[1] == "--listar":
        print("1" if presente(_sys.argv[2] or None) else "0")
