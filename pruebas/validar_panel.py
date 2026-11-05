r"""Validacion del panel con los equipos reales: osciloscopio MXO34, fuente EA-PS 9500-20 y la resistencia de 1 ohm.

Todo va por la API del panel (http://127.0.0.1:8765, autor «claude»): el panel sigue siendo el unico que habla con
los equipos y aplica sus protecciones (topes de la fuente, GTL tardio, modo manual...).

Uso, con el panel arrancado y en «modo panel» (no en manual):
    python validar_panel.py              capturas, datalogger y fuente SIN encender la salida
    python validar_panel.py --salida     ademas enciende la salida sobre la resistencia de 1 ohm
                                         (SOLO con la confirmacion expresa del usuario, cada vez)
Opciones: --proyecto PR00702-SUNWELD  --carpeta validacion_panel  --solo osc,fuente,datalogger

Que comprueba (cada punto sale OK / FALLO / AVISO):
  osciloscopio  captura completa (.h5 .png .json .set), registro completo, solo imagen, sin ficha, captura vacia
                rechazada; estructura del .h5 (canales, NumPoints, XInc, XOrg, unidades, Label), .png valido, campos
                del .json, .set presente; configuracion igual al terminar
  fuente        ritmo de lectura (0,1 y 0,5 s), consignas por encima de los topes rechazadas, consignas dentro
                aceptadas y releidas; con --salida: CC a 1 A y CV a 3 V sobre 1 ohm, foto de la fuente en la
                ficha de una captura, apagado
  datalogger    registro de 12 s (con --salida, con un escalon de corriente a mitad): srate, canales, Label,
                conversion a V, sin huecos ni empalmes ambiguos, registro.json
Al terminar: salida APAGADA, ritmo y consignas de la fuente como estaban, y el informe en un .txt junto a las
capturas. Los ficheros de prueba se quedan (no se borra nada sin permiso del usuario).
"""
import argparse
import json
import os
import struct
import sys
import time
import urllib.error
import urllib.request

import h5py
import numpy as np

PANEL = "http://127.0.0.1:8765"
BASE = r"C:\Proyectos\Osciloscopio"
RESULTADOS = []                      # (grupo, prueba, estado, detalle)


# ------------------------------------------------------------------ utilidades
def api(ruta, datos=None, esperar_error=False, timeout=180):
    """GET si datos es None; POST con JSON si no. Devuelve (codigo, respuesta)."""
    req = urllib.request.Request(PANEL + ruta, headers={"X-Autor": "claude", "Content-Type": "application/json"},
                                 data=None if datos is None else json.dumps(datos).encode())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            cuerpo = r.read()
            return r.status, (json.loads(cuerpo) if cuerpo else None)
    except urllib.error.HTTPError as e:
        try:
            detalle = json.loads(e.read()).get("detail")
        except Exception:
            detalle = str(e)
        if not esperar_error:
            raise RuntimeError(f"{ruta}: HTTP {e.code}: {detalle}") from None
        return e.code, detalle


def estado():
    return api("/api/estado")[1]


def anotar(grupo, prueba, ok, detalle=""):
    ok = None if ok is None else bool(ok)      # los resultados de numpy (np.bool_) no son «is True»
    estado_ = "OK" if ok is True else "FALLO" if ok is False else "AVISO"
    RESULTADOS.append((grupo, prueba, estado_, str(detalle)))
    print(f"  [{estado_:5}] {grupo} · {prueba}" + (f" — {detalle}" if detalle else ""), flush=True)
    return ok


def png_valido(ruta):
    with open(ruta, "rb") as f:
        cab = f.read(24)
    if cab[:8] != b"\x89PNG\r\n\x1a\n":
        return False, "no empieza por la firma PNG"
    ancho, alto = struct.unpack(">II", cab[16:24])
    return (ancho >= 640 and alto >= 400), f"{ancho}x{alto} px, {os.path.getsize(ruta) // 1024} KB"


def texto_h5(v):
    if isinstance(v, bytes):
        v = v.decode(errors="replace")
    return str(v).strip().strip("\x00")


def sin_comillas(s):
    return str(s or "").strip().strip('"').strip("'")


# ------------------------------------------------------------------ osciloscopio
def comprobar_h5_captura(grupo, ruta, activos, etiquetas):
    """Formato de R&S: Waveforms/Cx con NumPoints, XInc, XOrg, XUnit, YUnit; 'Cx Data'; 'Cx Additional Info'.Label."""
    puntos = {}
    with h5py.File(ruta, "r") as h:
        anotar(grupo, ".h5: cabecera del equipo", "Frame" in h and "MXO" in texto_h5(h["Frame"].attrs.get("Model", "")),
               texto_h5(h["Frame"].attrs.get("Model", "?")) + " · " + texto_h5(h["Frame"].attrs.get("DateTime", "?")))
        canales = sorted(k for k in h.get("Waveforms", {}) if k.startswith("C"))
        anotar(grupo, ".h5: canales guardados = canales activos", canales == [f"C{c}" for c in activos],
               f"en el fichero {canales}, activos {['C' + c for c in activos]}")
        for c in canales:
            g = h[f"Waveforms/{c}"]
            falta = [a for a in ("NumPoints", "XInc", "XOrg", "XUnit", "YUnit") if a not in g.attrs]
            d = g.get(f"{c} Data")
            n = int(g.attrs.get("NumPoints", 0))
            datos = d[:] if d is not None else np.array([])
            ok = not falta and d is not None and len(datos) >= n > 0 and np.all(np.isfinite(datos[:n]))
            anotar(grupo, f".h5 {c}: atributos y datos", ok,
                   f"{n} puntos, XInc {float(g.attrs.get('XInc', 0)):.3g} s, XOrg {float(g.attrs.get('XOrg', 0)):.3g} s, "
                   f"{texto_h5(g.attrs.get('YUnit', '?'))}" + (f", faltan {falta}" if falta else ""))
            info = g.get(f"{c} Additional Info")
            lab = texto_h5(info.attrs.get("Label", "")) if info is not None else None
            esperado = sin_comillas(etiquetas.get(c[1:], ""))
            anotar(grupo, f".h5 {c}: etiqueta", lab is not None and lab == esperado,
                   f"en el .h5 '{lab}', en el osciloscopio '{esperado}'")
            puntos[c] = n
    return puntos


def captura(nombre, args, **opciones):
    datos = {"proyecto": args.proyecto, "carpeta": args.carpeta, "nombre": nombre, "notas": "validación del panel",
             "formas": True, "ambito": "DISP", "png": True, "ficha": True, "fuente": True, "single": True}
    datos.update(opciones)
    t0 = time.time()
    codigo, r = api("/api/osc/capturar", datos)
    return r, time.time() - t0


def pruebas_osciloscopio(args, est0):
    g = "osciloscopio"
    canales = est0["osciloscopio"]["estado"]["canales"]
    activos = [c for c in sorted(canales) if canales[c].get("activo") == "1"]
    etiquetas = {c: canales[c].get("etiqueta", "") for c in canales}
    anotar(g, "canales activos", bool(activos), ", ".join(f"C{c} '{sin_comillas(etiquetas[c])}'" for c in activos))
    if not activos:
        return {}
    generados = {}

    # 1. captura completa
    r, t = captura("val_completa", args)
    base = os.path.join(BASE, r["ruta"])
    anotar(g, "captura completa", sorted(os.path.splitext(f)[1] for f in r["ficheros"]) == [".h5", ".json", ".png", ".set"],
           f"{', '.join(r['ficheros'])} en {t:.0f} s" + (f"; errores del equipo: {r['errores']}" if r.get("errores", "").startswith("-") else ""))
    for f in r["ficheros"]:
        anotar(g, f"existe {f}", os.path.isfile(os.path.join(os.path.dirname(base), f)))
    puntos_disp = comprobar_h5_captura(g, base + ".h5", activos, etiquetas)
    ok, det = png_valido(base + ".png")
    anotar(g, ".png válido", ok, det)
    anotar(g, ".set guardado", os.path.getsize(base + ".set") > 1000, f"{os.path.getsize(base + '.set') // 1024} KB")
    with open(base + ".json", encoding="utf-8") as f:
        ficha = json.load(f)
    falta = [k for k in ("hora", "nombre", "notas", "canales_guardados", "medidas", "osciloscopio", "fuente",
                         "ficheros", "ambito_exportacion", "single", "estaba_en_run") if k not in ficha]
    anotar(g, ".json: campos", not falta, "faltan " + ", ".join(falta) if falta else f"{len(ficha)} campos")
    anotar(g, ".json: ficheros = los que hay", sorted(ficha.get("ficheros", [])) == sorted(r["ficheros"]))
    fte = ficha.get("fuente")
    anotar(g, ".json: foto de la fuente", bool(fte) if est0["fuente"]["estado"] == "conectada" else None,
           "con la fuente conectada" if fte else "fuente no conectada: sin foto")
    generados["captura completa"] = base

    # 2. registro completo (ALL)
    r, t = captura("val_registro_completo", args, ambito="ALL", png=False)
    b2 = os.path.join(BASE, r["ruta"])
    with h5py.File(b2 + ".h5", "r") as h:
        p_all = {c: int(h[f"Waveforms/{c}"].attrs["NumPoints"]) for c in h["Waveforms"] if c.startswith("C")}
    anotar(g, "registro completo ≥ display", all(p_all[c] >= puntos_disp.get(c, 0) for c in p_all),
           f"ALL {p_all} · DISP {puntos_disp} ({t:.0f} s)")
    generados["registro completo"] = b2

    # 3. solo imagen
    r, t = captura("val_solo_imagen", args, formas=False, ficha=False)
    anotar(g, "solo imagen: solo .png", [os.path.splitext(f)[1] for f in r["ficheros"]] == [".png"],
           f"{', '.join(r['ficheros'])} en {t:.0f} s")

    # 4. sin ficha
    r, t = captura("val_sin_ficha", args, png=False, ficha=False)
    anotar(g, "sin ficha: solo .h5", [os.path.splitext(f)[1] for f in r["ficheros"]] == [".h5"],
           f"{', '.join(r['ficheros'])} en {t:.0f} s")

    # 5. nada marcado
    codigo, det = api("/api/osc/capturar", {"proyecto": args.proyecto, "nombre": "val_vacia", "formas": False,
                                            "png": False, "ficha": False}, esperar_error=True)
    anotar(g, "captura vacía rechazada", codigo == 409, det)
    return generados


def comparar_config(grupo, antes, despues, texto):
    dif = []
    for sec in ("horizontal", "disparo"):
        for k, v in antes.get(sec, {}).items():
            if despues.get(sec, {}).get(k) != v:
                dif.append(f"{sec}.{k}: {v} → {despues.get(sec, {}).get(k)}")
    for c, d in antes.get("canales", {}).items():
        for k, v in d.items():
            if despues.get("canales", {}).get(c, {}).get(k) != v:
                dif.append(f"C{c}.{k}: {v} → {despues['canales'][c].get(k)}")
    anotar(grupo, texto, not dif, "; ".join(dif[:8]) if dif else "sin diferencias")


# ------------------------------------------------------------------ fuente
def ritmo_medido(segundos):
    horas, t = set(), time.time()
    while time.time() - t < segundos:
        h = estado()["fuente"]["medida"].get("hora")
        if h:
            horas.add(h)
        time.sleep(0.02)
    h = sorted(horas)
    d = np.diff(h)
    return float(np.mean(d)) if len(d) else None


def esperar(segundos):
    time.sleep(segundos)
    return estado()["fuente"]


def pruebas_fuente(args, est0, guardado):
    g = "fuente"
    f = est0["fuente"]
    if f["estado"] != "conectada":
        api("/api/fuente/conectar", {"origen": "auto"})
        time.sleep(2)
        f = estado()["fuente"]
    anotar(g, "conectada", f["estado"] == "conectada" and not f.get("simulada"),
           f"{f.get('idn')} · {f.get('puerto')}" + (" (SIMULADA: no vale como validación)" if f.get("simulada") else ""))
    if f["estado"] != "conectada":
        return
    guardado["periodo"] = f.get("periodo_s", 0.5)
    guardado["consignas"] = dict(f.get("consignas") or {})
    L = f["limites"]
    anotar(g, "valores del equipo leídos", f.get("equipo", {}).get("v_nom") == 500,
           f"{f.get('equipo', {}).get('v_nom')} V · {f.get('equipo', {}).get('i_nom')} A · {f.get('equipo', {}).get('p_nom')} W")
    anotar(g, "salida apagada al empezar", f["medida"].get("salida") is False, f"medida {f['medida']}")

    for p in (0.1, 0.5):
        api("/api/fuente/opciones", {"periodo_s": p})
        time.sleep(0.4)
        m = ritmo_medido(4 if p < 0.5 else 6)
        anotar(g, f"lectura cada {p:g} s", m is not None and abs(m - p) < max(0.03, 0.15 * p), f"medido {m:.3f} s" if m else "sin lecturas")

    for nombre, datos in (("V", {"V_lim_V": L["vmax_V"] + 10}), ("I", {"I_A": L["imax_A"] + 0.5}),
                          ("P", {"P_W": L["pmax_W"] + 10})):
        codigo, det = api("/api/fuente/fijar", datos, esperar_error=True)
        anotar(g, f"{nombre} por encima del tope rechazada", codigo == 409, det)

    cons = {"V_lim_V": 2.0, "I_A": min(0.5, L["imax_A"]), "P_W": min(10.0, L["pmax_W"])}
    api("/api/fuente/fijar", cons)
    k = esperar(1.5).get("consignas", {})
    anotar(g, "consignas dentro de los topes aceptadas y releídas",
           all(abs((k.get(n) or -1) - v) < 0.01 for n, v in cons.items()), f"fijadas {cons} · leídas {k}")

    if not args.salida:
        anotar(g, "encender la salida", None, "no probado (falta --salida y la confirmación del usuario)")
        return
    # ---- con salida: resistencia de 1 ohm. Topes por defecto: 100 V, 1 A, 20 W
    try:
        api("/api/fuente/fijar", {"V_lim_V": 2.0, "I_A": min(1.0, L["imax_A"]), "P_W": min(20.0, L["pmax_W"])})
        api("/api/fuente/salida", {"encender": True, "confirmado": True})
        m = esperar(3)["medida"]
        anotar(g, "salida encendida", m.get("salida") is True, f"{m}")
        I = min(1.0, L["imax_A"])
        anotar(g, f"CC a {I:g} A sobre 1 Ω", m.get("modo") == "CC" and abs(m["I"] - I) < 0.06 and 0.8 * I < m["V"] < 2.0,
               f"V {m.get('V'):.3f} V · I {m.get('I'):.3f} A · P {m.get('P'):.2f} W · modo {m.get('modo')}")
        guardado["captura_on"] = True
        # CV a unos voltios: la PS 9500-20 tiene ±0,5 V de precision (0,1 % de 500 V); a 0,5 V la salida real
        # podia ir de 0 a 1 V (el 2-nov dio 0,50 A por la manana y 0,32 A despues). Con 3 V el error es pequeno.
        Vcv = round(min(3.0, 0.7 * L["imax_A"]), 1)
        api("/api/fuente/fijar", {"V_lim_V": Vcv, "I_A": min(5.0, L["imax_A"])})
        m = esperar(2.5)["medida"]
        anotar(g, f"CV a {Vcv:g} V sobre 1 Ω", m.get("modo") == "CV" and abs(m["V"] - Vcv) < 0.6 and 0.7 * Vcv < m["I"] < 1.3 * Vcv,
               f"V {m.get('V'):.3f} V · I {m.get('I'):.3f} A · P {m.get('P'):.2f} W · modo {m.get('modo')}")
        api("/api/fuente/fijar", {"V_lim_V": 2.0, "I_A": I})
        esperar(1.5)
    finally:
        if not guardado.get("datalogger_con_salida"):
            apagar(g)


def apagar(g):
    try:
        api("/api/fuente/salida", {"encender": False})
    except Exception as e:
        anotar(g, "APAGAR la salida", False, f"{e} — ¡APAGA LA FUENTE A MANO!")
        return
    m = esperar(2)["medida"]
    anotar(g, "salida apagada", m.get("salida") is False and abs(m.get("I", 0)) < 0.05, f"{m}")


# ------------------------------------------------------------------ datalogger
def pruebas_datalogger(args, est0, con_salida):
    g = "datalogger"
    canales = est0["osciloscopio"]["estado"]["canales"]
    activos = [int(c) for c in sorted(canales) if canales[c].get("activo") == "1"]
    if not activos:
        anotar(g, "canales activos", False, "ninguno")
        return None
    sr = 100e3 if len(activos) <= 4 else 50e3
    dur = 12
    api("/api/osc/datalogger/iniciar", {"canales": activos, "sr": sr, "duracion": dur, "proyecto": args.proyecto,
                                        "carpeta": args.carpeta, "nombre": "val_datalogger",
                                        "notas": "validación del panel" + (" · escalón de corriente 1 → 0,6 → 1 A" if con_salida else "")})
    t0 = time.time()
    escalon = [False, False]
    while True:
        time.sleep(0.5)
        dl = estado()["osciloscopio"]["datalogger"]
        if con_salida and not escalon[0] and time.time() - t0 > 5:
            api("/api/fuente/fijar", {"I_A": 0.6}); escalon[0] = True
        if con_salida and not escalon[1] and time.time() - t0 > 9:
            api("/api/fuente/fijar", {"I_A": 1.0}); escalon[1] = True
        if not dl.get("en_marcha") and dl.get("fin") is not None:
            break
        if time.time() - t0 > dur + 90:
            anotar(g, "termina", False, "más de 90 s de retraso; se pide parar")
            api("/api/osc/datalogger/parar", {})
            time.sleep(5)
            dl = estado()["osciloscopio"]["datalogger"]
            break
    anotar(g, "terminó sin error", dl.get("fin") == 0, f"código {dl.get('fin')} · {next((l for l in reversed(dl.get('salida', [])) if l), '')}")
    fichero = dl.get("fichero")
    if not fichero or not os.path.isfile(fichero):
        anotar(g, "osc.h5 existe", False, fichero)
        return None
    with h5py.File(fichero, "r") as h:
        anotar(g, "srate y canales", float(h.attrs.get("srate", 0)) == sr and list(h.attrs.get("canales", [])) == activos,
               f"srate {h.attrs.get('srate')} · canales {list(h.attrs.get('canales', []))}")
        anotar(g, "sin empalmes ambiguos", int(h.attrs.get("empalmes_ambiguos", -1)) == 0, f"{h.attrs.get('empalmes_ambiguos')}")
        anotar(g, "registro completo", "incompleto" not in h.attrs, texto_h5(h.attrs.get("incompleto", "")))
        dur_real = float(h.attrs.get("duracion_registro_s", 0))
        anotar(g, "duración", abs(dur_real - dur) < 3, f"{dur_real:.1f} s (pedidos {dur} s)")
        for c in activos:
            d = h.get(f"C{c}")
            if d is None:
                anotar(g, f"C{c} en el .h5", False)
                continue
            gan, off = float(d.attrs.get("ganancia_V", np.nan)), float(d.attrs.get("offset_V", np.nan))
            huecos = len(d.attrs.get("huecos_en_muestra", []))
            n = d.shape[0]
            anotar(g, f"C{c}: muestras, conversión a V y huecos",
                   n > 0.8 * sr * dur and np.isfinite(gan) and np.isfinite(off) and huecos == 0,
                   f"{n} muestras, ganancia {gan:.3g} V/cuenta, huecos {huecos}")
            lab = texto_h5(d.attrs.get("Label", ""))
            esperado = sin_comillas(canales[str(c)].get("etiqueta", ""))
            anotar(g, f"C{c}: etiqueta", lab == esperado, f"en el .h5 '{lab}', en el osciloscopio '{esperado}'")
            if con_salida and np.isfinite(gan):
                y = d[:] * gan + off
                # el escalon cae hacia 3,5-8 s del registro (medido el 2026-11-02): principio, centro y final
                t = n / sr
                tramos = [float(np.mean(y[int(a * sr):int(b * sr)])) for a, b in ((0.5, 2.5), (t / 2 - 1, t / 2 + 1), (t - 2.5, t - 0.5))]
                ok = abs(tramos[1] / tramos[0] - 0.6) < 0.05 and abs(tramos[2] / tramos[0] - 1) < 0.03
                anotar(g, f"C{c}: medias en los tramos 1 A / 0,6 A / 1 A", ok, ", ".join(f"{v:.4g} V" for v in tramos))
    ficha = os.path.join(os.path.dirname(fichero), "registro.json")
    if os.path.isfile(ficha):
        with open(ficha, encoding="utf-8") as f:
            r = json.load(f)
        anotar(g, "registro.json completo", "inicio" in r and any(k in r for k in ("codigo", "resumen", "salida_final")),
               ", ".join(sorted(r)[:12]))
    else:
        anotar(g, "registro.json", False, "no existe")
    return fichero


# ------------------------------------------------------------------ principal
def main():
    sys.stdout.reconfigure(errors="replace")      # la consola de Windows no tiene «Ω» ni «≥»
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--salida", action="store_true", help="encender la salida (solo con confirmación del usuario)")
    ap.add_argument("--proyecto", default="PR00702-SUNWELD")
    ap.add_argument("--carpeta", default="validacion_panel")
    ap.add_argument("--solo", default="osc,fuente,datalogger")
    args = ap.parse_args()
    partes = set(args.solo.split(","))
    print(f"Validación del panel · {time.strftime('%Y-%m-%d %H:%M')} · salida {'SÍ' if args.salida else 'no'}")

    est0 = estado()
    o = est0["osciloscopio"]
    con_osc = bool({"osc", "datalogger"} & partes)
    if con_osc and o["modo"] in ("manual", "datalogger", "error"):
        print(f"El osciloscopio está en modo '{o['modo']}' ({o.get('mensaje')}). Ponlo en modo panel y repite.")
        sys.exit(2)
    if con_osc:
        api("/api/osc/leer", {})
        est0 = estado()
    config0 = est0["osciloscopio"]["estado"]
    guardado, generados, fichero_dl = {}, {}, None
    try:
        if "osc" in partes:
            print("\n— Osciloscopio")
            generados = pruebas_osciloscopio(args, est0)
        if "fuente" in partes:
            print("\n— Fuente")
            guardado["datalogger_con_salida"] = args.salida and "datalogger" in partes
            pruebas_fuente(args, est0, guardado)
            if args.salida and guardado.get("captura_on") and "osc" in partes:
                r, _ = captura("val_fuente_encendida", args, png=False)
                with open(os.path.join(BASE, r["ruta"]) + ".json", encoding="utf-8") as f:
                    fte = json.load(f).get("fuente") or {}
                anotar("fuente", "foto de la fuente encendida en la ficha", fte.get("medida", {}).get("salida") is True,
                       f"{fte.get('medida')}")
        if "datalogger" in partes:
            print("\n— Datalogger")
            fichero_dl = pruebas_datalogger(args, est0, args.salida and guardado.get("captura_on"))
    finally:
        if args.salida and "fuente" in partes:
            apagar("fuente")
        try:
            f = estado()["fuente"]
            if f["estado"] == "conectada" and guardado:
                api("/api/fuente/opciones", {"periodo_s": guardado.get("periodo", 0.5)})
                k = {n: v for n, v in guardado.get("consignas", {}).items() if n in ("V_lim_V", "I_A", "P_W") and v is not None}
                L = f["limites"]
                if k and k.get("V_lim_V", 0) <= L["vmax_V"] and k.get("I_A", 0) <= L["imax_A"] and k.get("P_W", 0) <= L["pmax_W"]:
                    api("/api/fuente/fijar", k)
        except Exception as e:
            anotar("final", "restaurar ritmo y consignas de la fuente", False, e)
        try:
            if con_osc:
                api("/api/osc/leer", {})
                comparar_config("final", config0, estado()["osciloscopio"]["estado"],
                                "configuración del osciloscopio igual que al empezar")
        except Exception as e:
            anotar("final", "releer el osciloscopio", False, e)

    n = {s: sum(1 for r in RESULTADOS if r[2] == s) for s in ("OK", "FALLO", "AVISO")}
    resumen = f"\nResultado: {n['OK']} OK · {n['FALLO']} FALLO · {n['AVISO']} AVISO"
    print(resumen)
    carpeta = os.path.join(BASE, args.proyecto, "capturas", args.carpeta)
    os.makedirs(carpeta, exist_ok=True)
    informe = os.path.join(carpeta, time.strftime("validacion_%Y%m%d_%H%M.txt"))
    with open(informe, "w", encoding="utf-8") as f:
        f.write(f"Validación del panel · {time.strftime('%Y-%m-%d %H:%M')} · salida {'SÍ' if args.salida else 'no'}\n\n")
        for gr, pr, es, de in RESULTADOS:
            f.write(f"[{es:5}] {gr} · {pr}" + (f" — {de}" if de else "") + "\n")
        f.write(resumen + "\n\nPara verlo en MATLAB:\n")
        for nombre, b in generados.items():
            f.write(f"  osc('{b}.h5')   % {nombre}\n")
        if fichero_dl:
            f.write(f"  osc('{os.path.dirname(fichero_dl)}')   % datalogger\n")
    print(f"Informe: {informe}")
    sys.exit(1 if n["FALLO"] else 0)


if __name__ == "__main__":
    main()
