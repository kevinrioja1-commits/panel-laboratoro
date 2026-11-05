"""Banco de pruebas de En vivo SIN equipos: el servidor del panel en el puerto 8766, con un osc.h5 real como
«ultimo registro» (cerrado) y su envolvente de 0,1 s en la memoria de En vivo, como si se acabara de registrar."""
import collections
import os
import sys
import tempfile
import time

PANEL = r"C:\Proyectos\_HERRAMIENTAS\Panel_laboratorio"
H5 = sys.argv[1]
sys.path.insert(0, PANEL)
os.chdir(PANEL)
TMP = tempfile.mkdtemp(prefix="banco_")
import registro
registro.CARPETA = TMP
import osciloscopio
osciloscopio.Osciloscopio.iniciar_sincronizacion = lambda self, *a, **k: None   # sin hablar con el MXO
import h5py
import uvicorn
import servidor as S

S.osc._datalogger_vivo = lambda: False
S.detalle.preparar(H5)
while not S.detalle.estado()["listo"]:
    if S.detalle.estado()["error"]:
        sys.exit(S.detalle.estado()["error"])
    time.sleep(0.2)
with h5py.File(H5, "r") as f:
    eti = {k[1:]: f[k].attrs.get("Label", "") for k in f if k.startswith("C")}
    eti = {k: (v.decode() if isinstance(v, bytes) else str(v)) for k, v in eti.items()}
d = S.detalle.datos
cs = sorted(d["canales"])
ini = min(c["x0"] for c in d["canales"].values())
fin = max(c["x0"] + c["n"] / d["sr"] for c in d["canales"].values())
datos = {}
for c in cs:
    filas = collections.deque()
    t = ini
    while t < fin:                      # envolvente de 0,1 s como la del datalogger (por trozos de 500 s)
        r = S.detalle.tramo(t, min(fin, t + 500), puntos=5000).get(c)
        if r:
            for tt, mn, mx in zip(r["t"], r["mn"], r["mx"]):
                filas.append((r["base"] + tt, mn, mx, (mn + mx) / 2, False))
        t += 500
    datos[c] = filas
S.osc.vivo = {"info": {"canales": [int(c) for c in cs], "etiquetas": eti, "srate": d["sr"]}, "datos": datos}
S.osc.dl.update({"fichero": H5, "fin": 0})
if "--idn" in sys.argv: S.osc.idn = "BANCO (sin MXO)"
if "--estado" in sys.argv:              # la configuracion real del MXO que guardo el ensayo al empezar
    import json as _j
    _e = _j.load(open(os.path.join(os.path.dirname(H5), "ensayo.json"), encoding="utf-8"))["osciloscopio"]["al_empezar"]
    S.osc.estado, S.osc.hora_lectura, S.osc.modo = _e, time.time(), "conectado"
if "--temp" in sys.argv:                # termopares simulados (TC2 pasa su límite de 35 °C)
    import math as _m
    _t0 = time.time()

    def _hist(desde=0.0):
        ahora = time.time()
        ts = [ahora - 600 + k * 0.2 for k in range(int(600 / 0.2))]
        d1 = [(t, 25 + 8 * (1 - _m.exp(-(t - ts[0]) / 200))) for t in ts if t > desde]
        d2 = [(t, 28 + 9 * (1 - _m.exp(-(t - ts[0]) / 150)) + 0.3 * _m.sin(t)) for t in ts if t > desde]
        return {"canales": [{"id": "1", "nombre": "TC1 LED", "unidad": "°C", "color": "", "limite_C": None},
                            {"id": "2", "nombre": "TC2 carcasa", "unidad": "°C", "color": "", "limite_C": 35.0}],
                "datos": {"1": d1, "2": d2}}
    S.temps.historia_desde = _hist
print("banco listo:", H5, {c: len(v) for c, v in datos.items()}, flush=True)
uvicorn.run(S.app, host="127.0.0.1", port=8766, log_level="warning")
