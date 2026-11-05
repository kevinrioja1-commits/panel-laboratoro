r"""✂ Tramo (2026-11-04): guardar un trozo de tiempo marcado en En vivo.

<proyecto>\tramos\[carpeta\]AAAAMMDD_HHMM_nombre\
    fuente.csv         la fuente en ese tramo (de la memoria del panel: ultimas 2 h), formato de fuente.csv
    temperaturas.csv   las temperaturas en ese tramo (TC-08 del panel o PicoLog), formato de temperaturas.csv
    osc.h5             el osciloscopio con las MUESTRAS REALES, recortado del ultimo registro (Registrar, Ensayo o
                       la vista en vivo temporal) con sus atributos: osc.m lo abre como un registro mas.
                       Si ese fichero aun se esta escribiendo, el recorte queda PENDIENTE y se hace al terminar.
    osc_resumen.csv    si no hay osc.h5 que cubra el tramo: lo que se vio en vivo (min, max, media cada 0,1 s)
    tramo.json         horas, notas, de donde sale cada cosa y el estado del recorte del osciloscopio
"""
import datetime as dt
import glob
import json
import os
import threading
import time

import h5py
import numpy as np

import almacen

TROZO = 1 << 24          # muestras por copia al recortar (32 MB en int16)


def _iso(t):
    return dt.datetime.fromtimestamp(t).isoformat(timespec="milliseconds")


def recortar_h5(src, dst, t0, t1):
    """Copia de src (osc.h5 del datalogger) solo [t0, t1] (hora Unix), con los atributos ajustados para que el
    recorte se lea como un registro: inicio_unix, inicio y desfase_muestras de cada canal. Devuelve un resumen."""
    with h5py.File(src, "r") as f:
        sr = float(f.attrs["srate"])
        T0 = float(f.attrs["inicio_unix"])
        nombres = sorted(k for k in f.keys() if k.startswith("C"))
        base = {c: int(f[c].attrs.get("desfase_muestras", 0)) for c in nombres}
        ini = max(base.values())
        # todo en CUENTAS ENTERAS de muestras (g = muestras desde T0): la hora Unix en double solo resuelve ~0,24 us,
        # mas de una muestra a 5 MSa/s, y restando horas el desfase entre canales podia salir con una muestra de error
        tramos = {}
        for c in nombres:
            g0 = base[c] - ini                                   # muestra 0 de ese canal, en muestras desde T0
            n = f[c].shape[0]
            i0 = max(0, int(np.ceil((t0 - T0) * sr)) - g0)
            i1 = min(n - 1, int(np.floor((t1 - T0) * sr)) - g0)
            if i1 >= i0:
                tramos[c] = (i0, i1, g0 + i0)
        if not tramos:
            raise ValueError("el registro del osciloscopio no tiene muestras en ese tramo")
        # nuevo desfase: muestras de retraso respecto al canal que empieza antes (todos con la misma base de tiempos)
        ref = min(v[2] for v in tramos.values())
        nb = {c: v[2] - ref for c, v in tramos.items()}
        inicio = T0 + (ref + max(nb.values())) / sr              # el 0 = primera muestra comun (como el datalogger)
        with h5py.File(dst, "w") as g:
            for k, v in f.attrs.items():
                g.attrs[k] = v
            g.attrs.update(inicio_unix=inicio, inicio=_iso(inicio)[:23], tramo_de=os.path.abspath(src),
                           tramo_t0_unix=t0, tramo_t1_unix=t1,
                           duracion_registro_s=max(i1 - i0 + 1 for i0, i1, _ in tramos.values()) / sr,
                           canales=np.array([int(c[1:]) for c in tramos]))
            for c, (i0, i1, _) in tramos.items():
                ds = f[c]
                m = i1 - i0 + 1
                d = g.create_dataset(c, shape=(m,), dtype=ds.dtype, chunks=(min(m, 1 << 20),))
                for a in range(0, m, TROZO):
                    d[a:min(m, a + TROZO)] = ds[i0 + a:i0 + min(m, a + TROZO)]
                for k, v in ds.attrs.items():
                    d.attrs[k] = v
                d.attrs["desfase_muestras"] = np.int64(nb[c])
                h = np.asarray(ds.attrs.get("huecos_en_muestra", np.array([], dtype=np.int64)), dtype=np.int64)
                d.attrs["huecos_en_muestra"] = h[(h >= i0) & (h <= i1)] - i0
    return {c: {"muestras": i1 - i0 + 1} for c, (i0, i1, _) in tramos.items()}


def cubre(src, t0, t1):
    """True si el osc.h5 tiene alguna muestra en [t0, t1]."""
    try:
        with h5py.File(src, "r") as f:
            sr = float(f.attrs["srate"])
            T0 = float(f.attrs["inicio_unix"])
            dur = max(f[k].shape[0] for k in f.keys() if k.startswith("C")) / sr
    except Exception:
        return False
    return t0 < T0 + dur and t1 > T0 - 60


class Tramos:
    def __init__(self, osc, fte, temps, reg):
        self.osc, self.fte, self.temps, self.reg = osc, fte, temps, reg
        self.pendientes = []               # recortes del osciloscopio esperando a que se cierre su fichero

    def guardar(self, p, autor):
        t0, t1 = float(p["t0"]), float(p["t1"])
        if not t1 > t0:
            raise ValueError("Marca un tramo (el final después del principio)")
        if t1 - t0 > 24 * 3600:
            raise ValueError("Un tramo puede durar como mucho 24 h")
        que = p.get("que") or {"fuente": True, "osc": True, "temp": True}
        nombre = (p.get("nombre") or "").strip() or "tramo"
        ruta, _, rel = almacen.destino(p.get("proyecto", ""), "tramos", p.get("carpeta", ""), nombre, carpeta_propia=True)
        ficha = {"tipo": "tramo", "nombre": nombre, "proyecto": p.get("proyecto", ""), "carpeta": p.get("carpeta", ""),
                 "ruta": rel, "notas": p.get("notas", ""), "t0_unix": t0, "t1_unix": t1, "desde": _iso(t0),
                 "hasta": _iso(t1), "duracion_s": round(t1 - t0, 6), "guardado": _iso(time.time()), "ficheros": {}}
        cab = {"tramo": f"{_iso(t0)} a {_iso(t1)} ({rel})"}
        # fuente
        if que.get("fuente"):
            try:
                n = self.fte.exportar_csv(os.path.join(ruta, "fuente.csv"), t0, t1, cab)
                ficha["ficheros"]["fuente.csv"] = f"{n} filas" if n else "sin datos en ese tramo"
                if not n:
                    os.remove(os.path.join(ruta, "fuente.csv"))
            except Exception as e:
                ficha["ficheros"]["fuente.csv"] = f"NO: {e}"
        # temperaturas
        if que.get("temp"):
            try:
                n = self._temperaturas(os.path.join(ruta, "temperaturas.csv"), t0, t1, cab)
                ficha["ficheros"]["temperaturas.csv"] = f"{n} filas" if n else "sin datos en ese tramo"
            except Exception as e:
                ficha["ficheros"]["temperaturas.csv"] = f"NO: {e}"
        # osciloscopio
        if que.get("osc"):
            ficha["ficheros"]["osc.h5"] = self._osciloscopio(ruta, t0, t1, autor)
        self._escribir_ficha(ruta, ficha)
        self.reg.anotar(autor, "panel", "tramo guardado", {"ruta": rel, "ficheros": ficha["ficheros"]})
        return {"ruta": rel, "carpeta": ruta, "ficheros": ficha["ficheros"]}

    # ---------------------------------------------------------------- partes
    def _temperaturas(self, fichero, t0, t1, cab):
        h = self.temps.historia_desde(t0 - 1e-6)
        canales = [c for c in h.get("canales", []) if h["datos"].get(c["id"])]
        filas = {}
        for c in canales:
            for t, v in h["datos"][c["id"]]:
                if t0 <= t <= t1:
                    filas.setdefault(int(round(t * 1000)), {})[c["id"]] = v
        if not filas:
            return 0
        with open(fichero, "w", encoding="utf-8", newline="") as f:
            f.write("# Registro de temperaturas (tramo guardado desde En vivo, panel de laboratorio). MATLAB: osc(ruta)\n")
            for k, v in [("inicio", _iso(t0)), ("canales", "; ".join(c["nombre"] for c in canales)),
                         ("t_rel_s", "segundos desde el inicio del tramo"),
                         ("hora", "reloj del PC (hora local) al medir; t_unix_s en segundos Unix")] + list(cab.items()):
                f.write(f"# {k}: {v}\n")
            f.write(",".join(["fecha_hora", "t_unix_s", "t_rel_s"] + [f"{c['nombre']}_C" for c in canales]) + "\n")
            for ms in sorted(filas):
                t = ms / 1000
                fila = filas[ms]
                f.write(",".join([_iso(t), f"{t:.3f}", f"{t - t0:.3f}"] +
                                 ["" if fila.get(c["id"]) is None else f"{fila[c['id']]:.3f}" for c in canales]) + "\n")
        return len(filas)

    def _osciloscopio(self, ruta, t0, t1, autor):
        src = self.osc.dl.get("fichero")
        if src and os.path.exists(src) and cubre(src, t0, t1):
            escribiendo = self.osc._datalogger_vivo() or self.osc.dl.get("fin") is None
            if escribiendo:
                pend = {"src": src, "ruta": ruta, "t0": t0, "t1": t1, "autor": autor}
                self.pendientes.append(pend)
                self.osc.no_borrar.add(src)
                threading.Thread(target=self._recortar_al_cerrar, args=(pend,), daemon=True).start()
                return "PENDIENTE: el registro del osciloscopio sigue abierto; se recorta en cuanto termine"
            try:
                r = recortar_h5(src, os.path.join(ruta, "osc.h5"), t0, t1)
                return "muestras reales: " + ", ".join(f"{c} {v['muestras']}" for c, v in r.items())
            except Exception as e:
                return f"NO se pudo recortar: {e}"
        # el último registro no lo cubre: registros, ensayos y vistas anteriores ya cerrados (2026-11-04; antes solo se
        # miraba el último y un tramo de un registro anterior se quedaba sin muestras reales)
        otro = self._buscar_h5(t0, t1, excluir=src)
        if otro:
            try:
                r = recortar_h5(otro, os.path.join(ruta, "osc.h5"), t0, t1)
                de = os.path.relpath(otro, almacen.BASE) if otro.startswith(almacen.BASE) else "la vista en vivo"
                return f"muestras reales (de {de}): " + ", ".join(f"{c} {v['muestras']}" for c, v in r.items())
            except Exception as e:
                return f"NO se pudo recortar: {e}"
        # sin osc.h5 que lo cubra: lo que se vio en vivo (min, max, media cada 0,1 s), si lo hay
        v = self.osc.vivo_desde(t0 - 1e-6)
        filas = {c: [f for f in fs if f[0] <= t1] for c, fs in (v.get("datos") or {}).items()}
        if not any(filas.values()):
            return "sin datos del osciloscopio en ese tramo"
        with open(os.path.join(ruta, "osc_resumen.csv"), "w", encoding="utf-8", newline="") as f:
            f.write("# Osciloscopio en vivo: minimo, maximo y media de cada 0,1 s (no hay osc.h5 que cubra el tramo)\n")
            f.write("canal,t_unix_s,min_V,max_V,media_V,recortada\n")
            for c, fs in sorted(filas.items()):
                for t, mn, mx, me, rec in fs:
                    f.write(f"C{c},{t:.3f},{mn:.6g},{mx:.6g},{me:.6g},{int(bool(rec))}\n")
        return "solo resumen en vivo (osc_resumen.csv): no hay osc.h5 que cubra el tramo"

    def _buscar_h5(self, t0, t1, excluir=None, horas=26):
        """El osc.h5 cerrado (registros, ensayos, datalogger_*.h5 y vistas temporales) que más cubre [t0, t1], de los
        modificados en las últimas 'horas' (las 2 h de memoria de En vivo, con margen)."""
        from osciloscopio import VISTA_DIR
        b = almacen.BASE
        patrones = [os.path.join(b, "*", "registros", "**", "osc.h5"), os.path.join(b, "*", "ensayos", "**", "osc.h5"),
                    os.path.join(b, "*", "datalogger_*.h5"), os.path.join(VISTA_DIR, "*.h5")]
        limite = time.time() - horas * 3600
        norm = lambda x: os.path.normcase(os.path.abspath(x))
        fuera = norm(excluir) if excluir else None
        mejor, cubierto = None, 0.0
        for patron in patrones:
            for f in glob.glob(patron, recursive=True):
                try:
                    if norm(f) == fuera or os.path.getmtime(f) < limite:
                        continue
                    with h5py.File(f, "r") as h:
                        sr, T0 = float(h.attrs["srate"]), float(h.attrs["inicio_unix"])
                        dur = max(h[k].shape[0] for k in h.keys() if k.startswith("C")) / sr
                except Exception:
                    continue
                solape = min(t1, T0 + dur) - max(t0, T0)
                if solape > cubierto:
                    mejor, cubierto = f, solape
        return mejor

    def _recortar_al_cerrar(self, pend):
        try:
            while self.osc.dl.get("fichero") == pend["src"] and (self.osc._datalogger_vivo()
                                                                 or self.osc.dl.get("fin") is None):
                time.sleep(1)
            r = recortar_h5(pend["src"], os.path.join(pend["ruta"], "osc.h5"), pend["t0"], pend["t1"])
            estado = "muestras reales: " + ", ".join(f"{c} {v['muestras']}" for c, v in r.items())
        except Exception as e:
            estado = f"NO se pudo recortar: {e}"
        finally:
            self.osc.no_borrar.discard(pend["src"])
            if pend in self.pendientes:
                self.pendientes.remove(pend)
        try:
            fj = os.path.join(pend["ruta"], "tramo.json")
            with open(fj, encoding="utf-8") as f:
                ficha = json.load(f)
            ficha["ficheros"]["osc.h5"] = estado + " (recortado al terminar el registro)"
            self._escribir_ficha(pend["ruta"], ficha)
        except Exception:
            pass
        self.reg.anotar("panel (automático)", "panel", "tramo: osciloscopio recortado", {"ruta": pend["ruta"], "estado": estado})

    @staticmethod
    def _escribir_ficha(ruta, ficha):
        tmp = os.path.join(ruta, "tramo.json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(ficha, f, ensure_ascii=False, indent=1)
        os.replace(tmp, os.path.join(ruta, "tramo.json"))
