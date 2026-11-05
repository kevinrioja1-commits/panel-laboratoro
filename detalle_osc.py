"""Puntos reales de un registro del osciloscopio (osc.h5 del datalogger) para la grafica En vivo (2026-11-04).

Durante un registro, En vivo solo tiene el minimo, el maximo y la media de cada 0,1 s (lo que manda el datalogger;
5 MSa/s no se pueden llevar al navegador). Con el fichero ya CERRADO (registro parado, ensayo terminado, vista del
osciloscopio parada) este modulo lo lee por niveles, como el visor osc.m de MATLAB:
  - nivel 1: minimo y maximo de cada bloque de 512 muestras; cada nivel junta 8 bloques del anterior.
  - Se calculan una vez, en segundo plano (unos segundos por GB), y se guardan en <osc.h5>.niveles_panel.npz.
  - Para una vista [t0, t1] (hora Unix) se devuelven como mucho PUNTOS tramos (minimo y maximo de cada uno),
    leidos del nivel adecuado; si se ven pocas muestras, las muestras reales (crudo = True).
Nunca se abre un fichero que el datalogger este escribiendo (lo comprueba quien llama: servidor.py).
"""
import math
import os
import threading

import h5py
import numpy as np

B0 = 512         # muestras por bloque del nivel 1 (512: cache ~1/256 del .h5; de cerca se lee el .h5)
K = 8            # bloques de un nivel que forman uno del siguiente
TROZO = 1 << 24  # muestras por lectura al calcular los niveles (32 MB en int16)
VERSION = 2


class DetalleOsc:
    def __init__(self):
        self.lock = threading.Lock()
        self.ruta = None        # fichero preparado (o preparandose)
        self.datos = None       # {"sr", "canales": {"1": {n, g, o, x0, niv: [(B, mn, mx), ...]}}}
        self.avance = 0.0       # 0..1 mientras se preparan los niveles
        self.error = None
        self._hilo = None

    # ------------------------------------------------------------ preparar
    def _clave(self, ruta):
        st = os.stat(ruta)
        return f"v{VERSION}|{st.st_size}|{int(st.st_mtime)}"

    def preparar(self, ruta):
        """Empieza a preparar los niveles de 'ruta' (si no lo estan ya). Devuelve el estado."""
        with self.lock:
            if self.ruta != ruta:
                self.ruta, self.datos, self.avance, self.error = ruta, None, 0.0, None
                self._hilo = threading.Thread(target=self._preparar, args=(ruta,), daemon=True)
                self._hilo.start()
            return self.estado()

    def estado(self):
        return {"fichero": self.ruta, "listo": self.datos is not None, "avance": round(self.avance, 3),
                "error": self.error}

    def _preparar(self, ruta):
        try:
            clave = self._clave(ruta)
            cache = ruta + ".niveles_panel.npz"
            with h5py.File(ruta, "r") as f:
                sr = float(f.attrs["srate"])
                t0 = float(f.attrs.get("inicio_unix", float("nan")))
                if math.isnan(t0):
                    raise ValueError("el registro no tiene hora de inicio (inicio_unix)")
                nombres = sorted(k for k in f.keys() if k.startswith("C"))
                base = {c: int(f[c].attrs.get("desfase_muestras", 0)) for c in nombres}
                ini = max(base.values()) if base else 0
                previos = {}
                if os.path.exists(cache):
                    try:
                        z = np.load(cache, allow_pickle=False)
                        if str(z["clave"]) == clave:
                            previos = {k: z[k] for k in z.files if k != "clave"}
                    except Exception:
                        previos = {}
                total = sum(f[c].shape[0] for c in nombres) or 1
                hecho = 0
                canales, guardar = {}, {"clave": np.array(clave)}
                for c in nombres:
                    ds = f[c]
                    n = ds.shape[0]
                    if f"{c}_mn0" in previos:
                        mn, mx = previos[f"{c}_mn0"], previos[f"{c}_mx0"]
                        hecho += n
                    else:
                        mn = np.empty(-(-n // B0), dtype=np.int16)
                        mx = np.empty_like(mn)
                        j = 0
                        for i in range(0, n, TROZO):
                            r = ds[i:i + TROZO]
                            m = len(r) // B0
                            if m:
                                rr = r[:m * B0].reshape(m, B0)
                                mn[j:j + m], mx[j:j + m] = rr.min(1), rr.max(1)
                                j += m
                            if len(r) > m * B0:            # ultimo bloque incompleto
                                mn[j], mx[j] = r[m * B0:].min(), r[m * B0:].max()
                                j += 1
                            hecho += len(r)
                            self.avance = hecho / total
                        mn, mx = mn[:j], mx[:j]
                    guardar[f"{c}_mn0"], guardar[f"{c}_mx0"] = mn, mx
                    niv = [(B0, mn, mx)]
                    while len(niv[-1][1]) > 4000:
                        B, a, b = niv[-1]
                        m = -(-len(a) // K)
                        pa = np.concatenate([a, np.full(m * K - len(a), a[-1], a.dtype)]).reshape(m, K).min(1)
                        pb = np.concatenate([b, np.full(m * K - len(b), b[-1], b.dtype)]).reshape(m, K).max(1)
                        niv.append((B * K, pa, pb))
                    canales[c[1:]] = {"n": n, "g": float(ds.attrs.get("ganancia_V", float("nan"))),
                                      "o": float(ds.attrs.get("offset_V", float("nan"))),
                                      "x0": t0 + (base[c] - ini) / sr, "niv": niv, "dataset": c}
            if not previos:
                try:
                    np.savez(cache, **guardar)        # sin comprimir: se carga al momento
                except Exception:
                    pass
            with self.lock:
                if self.ruta == ruta:
                    self.datos, self.avance = {"sr": sr, "canales": canales}, 1.0
        except Exception as e:
            with self.lock:
                if self.ruta == ruta:
                    self.error = f"{type(e).__name__}: {e}"

    # ------------------------------------------------------------ consultar
    def tramo(self, t0, t1, puntos=2000):
        """{canal: {"base", "t": [...], "mn", "mx", "crudo", "paso_s"}} entre t0 y t1 (hora Unix). t es RELATIVO a base
        (hora Unix del primer punto): en double la hora Unix solo resuelve ~0,24 us y a 5 MSa/s se juntaban muestras.
        crudo = muestras reales (mn = mx = valor); si no, minimo y maximo de cada tramo que empieza en t."""
        d, ruta = self.datos, self.ruta
        if d is None:
            raise RuntimeError("los puntos reales aún no están preparados")
        sr = d["sr"]
        puntos = max(100, min(int(puntos), 8000))
        out = {}
        with h5py.File(ruta, "r") as f:
            for c, k in d["canales"].items():
                i0 = max(0, int(math.floor((t0 - k["x0"]) * sr)))
                i1 = min(k["n"] - 1, int(math.ceil((t1 - k["x0"]) * sr)))
                if i1 <= i0:
                    continue
                m = i1 - i0 + 1
                g, o = k["g"], k["o"]
                if m < B0 * puntos:
                    y = f[k["dataset"]][i0:i1 + 1].astype(np.float64) * g + o
                    if m <= 2 * puntos:
                        t = np.arange(m) / sr
                        out[c] = {"base": k["x0"] + i0 / sr, "t": t.tolist(), "mn": y.tolist(), "mx": y.tolist(),
                                  "crudo": True, "paso_s": 1 / sr}
                        continue
                    b = m // puntos
                    yy = y[:b * puntos].reshape(puntos, b)
                    lo, hi, B, j0, nb = yy.min(1), yy.max(1), 1, i0, puntos
                    t = np.arange(puntos) * b / sr
                    out[c] = {"base": k["x0"] + i0 / sr, "t": t.tolist(), "mn": lo.tolist(), "mx": hi.tolist(), "crudo": False, "paso_s": b / sr}
                    continue
                niv = k["niv"]
                L = max(i for i, (B, _, _) in enumerate(niv) if B <= m / puntos)
                B, mn, mx = niv[L]
                j0 = i0 // B
                j1 = min(len(mn) - 1, i1 // B)
                nb = j1 - j0 + 1
                b = max(1, nb // puntos)
                kk = nb // b
                lo = mn[j0:j0 + kk * b].reshape(kk, b).min(1).astype(np.float64) * g + o
                hi = mx[j0:j0 + kk * b].reshape(kk, b).max(1).astype(np.float64) * g + o
                lo, hi = np.minimum(lo, hi), np.maximum(lo, hi)
                t = np.arange(kk) * b * B / sr
                out[c] = {"base": k["x0"] + j0 * B / sr, "t": t.tolist(), "mn": lo.tolist(), "mx": hi.tolist(), "crudo": False, "paso_s": b * B / sr}
        return out
