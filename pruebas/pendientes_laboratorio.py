r"""TODAS las pruebas de laboratorio pendientes del panel, de una vez (pedido del usuario el 2026-11-04).

Montaje: panel arrancado; osciloscopio MXO34 en la red con una señal en C1 (vale la de Probe Comp o lo que haya
conectado); fuente EA por USB con su salida sobre la resistencia de 1 ohm (solo para la parte B).
Todo va por la API del panel (autor «claude"): el panel sigue siendo el único que habla con los equipos.

    python pendientes_laboratorio.py                    parte A (osciloscopio; NO enciende nada)
    python pendientes_laboratorio.py --con-salida       A + B (B ENCIENDE la fuente: <= 3 V / 0,6 A sobre 1 ohm;
                                                        SOLO con la confirmación expresa del usuario)
    opciones: --proyecto PR00737-ECOCOOK   --solo osc|fuente

Parte A · osciloscopio
  A1  muestreos bajos del registro (50, 20, 10, 5, 2 y 1 kSa/s, 1 canal, 10 s cada uno): que el MXO respete el
      muestreo pedido en Roll (srate = srate_pedido), sin huecos ni empalmes ambiguos y con las muestras esperadas.
      Resultado: el muestreo MÍNIMO real (para dejar en el desplegable solo los que valen).
  A2  vista en vivo sin registro (C1 a 100 kSa/s): arranca, llegan datos a En vivo; un ✂ Tramo a mitad queda
      PENDIENTE y se recorta al parar con las muestras reales; con la vista parada hay puntos reales (detalle).
  A3  la configuración del osciloscopio queda como estaba (horizontal y canales).
Parte B · fuente (con --con-salida)
  B   validar_seguridad.py: APAGAR y «local» durante un plan, desconectar con la salida ON, encender al iniciar.

Los ficheros de prueba quedan en <proyecto>\registros\_validacion_laboratorio y <proyecto>\tramos\_validacion_laboratorio
(no se borra nada sin permiso del usuario). Informe en <proyecto>\registros\_validacion_laboratorio\.
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

import h5py

PANEL = "http://127.0.0.1:8765"
BASE = r"C:\Proyectos\Osciloscopio"
CARPETA = "_validacion_laboratorio"
AQUI = os.path.dirname(os.path.abspath(__file__))
RES = []


def api(ruta, datos=None, error=False, timeout=180):
    req = urllib.request.Request(PANEL + ruta, headers={"X-Autor": "claude", "Content-Type": "application/json"},
                                 data=None if datos is None else json.dumps(datos).encode())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            c = r.read()
            return r.status, (json.loads(c) if c else None)
    except urllib.error.HTTPError as e:
        try:
            d = json.loads(e.read()).get("detail")
        except Exception:
            d = str(e)
        if not error:
            raise RuntimeError(f"{ruta}: HTTP {e.code}: {d}") from None
        return e.code, d


def est():
    return api("/api/estado")[1]


def ok(grupo, nombre, cond, detalle=""):
    RES.append((grupo, nombre, bool(cond), str(detalle)))
    print(("OK    " if cond else "FALLO ") + f"[{grupo}] {nombre}" + (f"  [{detalle}]" if detalle else ""), flush=True)


def esperar(cond, t, cada=0.5):
    fin = time.time() + t
    while time.time() < fin:
        if cond():
            return True
        time.sleep(cada)
    return cond()


def dl():
    return est()["osciloscopio"].get("datalogger") or {}


def dl_terminado(fichero):
    d = dl()
    return d.get("fichero") == fichero and not d.get("en_marcha") and d.get("fin") is not None


def config_osc():
    """Lo que lee el panel (horizontal, disparo y canales completos): debe quedar igual tras las pruebas."""
    e = api("/api/osc/leer", {})[1] or {}
    return {k: e.get(k) for k in ("horizontal", "disparo", "canales")}


# ------------------------------------------------------------------ parte A
def parte_a(proyecto):
    try:
        antes = config_osc()
    except RuntimeError as e:
        sys.exit(f"No se puede leer el osciloscopio: {e}")
    print("Configuración del osciloscopio antes:", json.dumps(antes, ensure_ascii=False), flush=True)
    # A1 muestreos bajos
    minimo = None
    for sr in (50e3, 20e3, 10e3, 5e3, 2e3, 1e3):
        nombre = f"sr_{int(sr)}"
        c, d = api("/api/osc/datalogger/iniciar", {"canales": [1], "sr": sr, "duracion": 10, "proyecto": proyecto,
                                                   "carpeta": CARPETA, "nombre": nombre,
                                                   "notas": "prueba de muestreo bajo (pendientes_laboratorio.py)"},
                   error=True)
        if c != 200:
            ok("A1", f"{sr / 1e3:g} kSa/s: arranca", False, d)
            continue
        fich = dl().get("fichero")
        if not esperar(lambda: dl_terminado(fich), 150, 1):
            ok("A1", f"{sr / 1e3:g} kSa/s: termina", False, "no terminó en 150 s")
            api("/api/osc/datalogger/parar", {}, error=True)
            esperar(lambda: dl_terminado(fich), 60, 1)
            continue
        try:
            with h5py.File(fich, "r") as f:
                real, pedido = float(f.attrs["srate"]), float(f.attrs.get("srate_pedido", sr))
                n = f["C1"].shape[0]
                huecos = len(f["C1"].attrs.get("huecos_en_muestra", []))
                amb = int(f.attrs.get("empalmes_ambiguos", 0))
            bien = abs(real - pedido) < 1e-6 and huecos == 0 and amb == 0 and n >= 0.9 * 10 * real
            ok("A1", f"{sr / 1e3:g} kSa/s", bien, f"srate real {real:g}, {n} muestras, {huecos} huecos, {amb} ambiguos")
            if abs(real - pedido) < 1e-6:
                minimo = sr
        except Exception as e:
            ok("A1", f"{sr / 1e3:g} kSa/s: fichero", False, e)
    ok("A1", "muestreo mínimo que respeta el MXO en Roll", minimo is not None, f"{minimo:g} Sa/s" if minimo else "ninguno")

    # A2 vista en vivo + ✂ Tramo pendiente + puntos reales
    c, d = api("/api/osc/vista/iniciar", {"canales": [1], "sr": 100e3}, error=True)
    ok("A2", "vista en vivo: arranca", c == 200, d if c != 200 else "")
    if c == 200:
        fich = dl().get("fichero")
        ok("A2", "es una vista (temporal), no un registro", dl().get("vista") and "panel_vista_osc" in (fich or ""), fich)
        llegan = esperar(lambda: any((api("/api/osc/vivo?desde=0")[1].get("datos") or {}).values()), 60, 1)
        ok("A2", "llegan datos a En vivo", llegan)
        time.sleep(6)
        t1 = time.time() - 1.5
        t0 = t1 - 3
        c, r = api("/api/tramo/guardar", {"t0": t0, "t1": t1, "proyecto": proyecto, "carpeta": CARPETA,
                                         "nombre": "tramo_vista", "que": {"osc": True},
                                         "notas": "✂ Tramo durante la vista (pendientes_laboratorio.py)"}, error=True)
        ok("A2", "✂ Tramo durante la vista queda PENDIENTE", c == 200 and "PENDIENTE" in str(r.get("ficheros", {})), r)
        api("/api/osc/vista/parar", {})
        ok("A2", "la vista para", esperar(lambda: dl_terminado(fich), 60, 1))
        if c == 200:
            ruta = os.path.join(BASE, r["ruta"])
            hecho = esperar(lambda: os.path.exists(os.path.join(ruta, "osc.h5")), 60, 1)
            muestras = None
            if hecho:
                with h5py.File(os.path.join(ruta, "osc.h5"), "r") as f:
                    muestras = f["C1"].shape[0]
            ok("A2", "el tramo se recorta al parar (≈ 3 s a 100 kSa/s)", hecho and muestras and 0.95 * 3e5 <= muestras <= 1.05 * 3e5,
               f"{muestras} muestras")
        det = None
        for _ in range(60):
            det = api(f"/api/osc/vivo/detalle?t0={t0 + 1}&t1={t0 + 1.001}&puntos=500")[1]
            if det.get("estado") != "preparando":
                break
            time.sleep(1)
        c1 = (det or {}).get("canales", {}).get("1") or {}
        ok("A2", "con la vista parada hay puntos reales (1 ms = 100 muestras)", det.get("estado") == "listo" and c1.get("crudo"),
           f"{det.get('estado')}, {len(c1.get('t', []))} puntos")

    # A3 configuracion
    despues = config_osc()
    ok("A3", "configuración del osciloscopio como estaba", despues == antes,
       "" if despues == antes else f"antes {antes} · después {despues}")


# ------------------------------------------------------------------ parte B
def parte_b(proyecto):
    r = subprocess.run([sys.executable, "-u", os.path.join(AQUI, "validar_seguridad.py"), "--proyecto", proyecto],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    print(r.stdout, flush=True)
    for l in r.stdout.splitlines():
        if l.startswith(("OK    ", "FALLO ")):
            ok("B", l[6:], l.startswith("OK"))
    if r.returncode not in (0, None) and not any(g == "B" for g, *_ in RES):
        ok("B", "validar_seguridad.py", False, (r.stderr or r.stdout)[-300:])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--proyecto", default="PR00737-ECOCOOK")
    ap.add_argument("--con-salida", action="store_true", help="incluye la parte B (ENCIENDE la fuente)")
    ap.add_argument("--solo", choices=("osc", "fuente"))
    a = ap.parse_args()
    e = est()
    if e.get("ensayo"):
        sys.exit("Hay un ensayo en marcha: termínalo antes")
    if (e["osciloscopio"].get("datalogger") or {}).get("en_marcha"):
        sys.exit("Hay un registro del osciloscopio en marcha: páralo antes")
    if a.solo != "fuente":
        if e["osciloscopio"].get("modo") == "manual":
            sys.exit("El panel está en «Modo manual»: vuelve a modo panel antes")
        parte_a(a.proyecto)
    if a.solo != "osc":
        if a.con_salida:
            parte_b(a.proyecto)
        else:
            print("Parte B (fuente) NO lanzada: hace falta --con-salida (enciende la fuente)", flush=True)
    n = sum(1 for *_, c, _ in RES if c)
    txt = (f"Pruebas pendientes de laboratorio {time.strftime('%Y-%m-%d %H:%M')}: {n} OK de {len(RES)}\n" +
           "\n".join(("OK    " if c else "FALLO ") + f"[{g}] {p}" + (f"  [{d}]" if d else "") for g, p, c, d in RES))
    car = os.path.join(BASE, a.proyecto, "registros", CARPETA)
    os.makedirs(car, exist_ok=True)
    fi = os.path.join(car, time.strftime("informe_%Y%m%d_%H%M.txt"))
    with open(fi, "w", encoding="utf-8") as f:
        f.write(txt + "\n")
    print("\n" + txt.splitlines()[0] + f"\nInforme: {fi}")


if __name__ == "__main__":
    main()
