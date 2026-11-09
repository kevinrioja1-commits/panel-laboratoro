r"""Validación del cDAQ A TRAVÉS DEL PANEL (fases 1-4 de la integración, 9-nov-2026), con el chasis real.

Lo que se elige por la API del panel (lo mismo que hará la página) tiene que quedar así en el DRIVER: se comprueba
con la lectura de vuelta que el panel enseña (cdaq.modulos.<rol>.driver). Además: guardas, reloj de hardware,
En vivo (historia por el selector de temperaturas), registro CSV, TDMS del driver y disparos/reloj externo por PFI0
SIN señal (deben quedarse esperando). Al terminar deja la configuración del cDAQ como estaba.

Montaje: termopar en TC0 (y lo que haya en TC1/TC2), una tensión fija en AI0 del 9201 (masa en el borne 9).
    python pruebas\validar_cdaq_panel.py
"""
import copy
import json
import os
import sys
import time
import urllib.error
import urllib.request

URL = "http://127.0.0.1:8765"
RES = []


def api(ruta, datos=None, error=False, timeout=60):
    req = urllib.request.Request(URL + ruta, headers={"X-Autor": "claude", "Content-Type": "application/json"},
                                 data=None if datos is None else json.dumps(datos).encode())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        try:
            d = json.loads(e.read()).get("detail")
        except Exception:
            d = str(e)
        if not error:
            raise RuntimeError(f"{ruta}: {e.code} {d}") from None
        return e.code, d


def chk(g, nombre, cond, detalle=""):
    est = "OMITIDA" if cond is None else "OK" if cond else "FALLO"
    RES.append((est, g, nombre, str(detalle)))
    print(f"{est:7} [{g}] {nombre}" + (f"  [{detalle}]" if detalle != "" else ""), flush=True)


def cd():
    return api("/api/estado")[1]["cdaq"]


def conf(p, error=False):
    return api("/api/cdaq/configurar", p, error=error)


def esperar(cond, t=10.0, paso=0.3):
    fin = time.time() + t
    while time.time() < fin:
        x = cond()
        if x:
            return x
        time.sleep(paso)
    return cond()


def drv(rol):
    return ((cd().get("modulos") or {}).get(rol) or {}).get("driver") or {}


def reabierto(rol, clave, valor, t=12):
    """Espera a que el hilo reabra la tarea con la configuración nueva (lectura de vuelta del driver)."""
    return esperar(lambda: drv(rol).get(clave) == valor, t)


MUESTREO_BASE = {"muestreo": "bajo_demanda", "disparo": "ninguno", "reloj": "interno", "tasa_S_s": None}


def main():
    e = cd()
    if e["estado"] == "desactivado":
        sys.exit("El cDAQ está desactivado en config.json")
    guardado = copy.deepcopy(e["config"])
    estaba = e["estado"]
    try:
        fase1()
        fase2()
        fase3()
        fase4()
    finally:
        restaurar(guardado, estaba)
    n = {k: sum(1 for r in RES if r[0] == k) for k in ("OK", "FALLO", "OMITIDA")}
    print(f"\n{n['OK']} OK · {n['FALLO']} FALLO · {n['OMITIDA']} OMITIDA")
    os.makedirs(os.path.join(os.path.dirname(os.path.abspath(__file__)), "informes"), exist_ok=True)
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "informes",
                           time.strftime("cdaq_panel_%Y%m%d_%H%M.txt")), "w", encoding="utf-8") as f:
        f.write("\n".join(f"{a:7} [{b}] {c}  {d}" for a, b, c, d in RES) + "\n")
    sys.exit(1 if n["FALLO"] else 0)


def restaurar(g, estaba):
    claves_mod = ("modo_adc", "autozero", "cjc", "cjc_valor_C", "escala", "unidades", "muestreo", "tasa_S_s", "muestras",
                  "reloj", "disparo", "disparo_fuente", "disparo_flanco", "pre_muestras", "pausa_cuando", "bufer_muestras",
                  "conversion_Hz", "tdms")
    p = {"periodo_s": g["periodo_s"]}
    for rol in ("termopares", "tensiones"):
        p[rol] = {k: g[rol][k] for k in claves_mod if k in g[rol]}
        p[rol]["canales"] = {k: {kk: q[kk] for kk in ("activo", "nombre", "tipo", "min_C", "max_C", "ganancia",
                                                      "desplazamiento", "unidad") if kk in q}
                             for k, q in g[rol]["canales"].items()}
    c, d = conf(p, error=True)
    print("configuración del cDAQ restaurada" if c == 200 else f"NO SE PUDO RESTAURAR: {d}")
    if estaba != "midiendo":
        api("/api/cdaq/soltar", {}, error=True)


# ------------------------------------------------------------------ fase 1: el modelo validado dentro del panel
def fase1():
    g = "F1 modelo en el panel"
    conf({"periodo_s": 1.0, "termopares": dict(MUESTREO_BASE, modo_adc="alta_resolucion", autozero="cada_muestra",
                                                cjc="interna", escala="polinomio", unidades="C",
                                                canales={"0": {"activo": True, "tipo": "K", "min_C": -20, "max_C": 400},
                                                         "1": {"activo": True}, "2": {"activo": True}}),
          "tensiones": dict(MUESTREO_BASE, canales={"0": {"activo": True, "ganancia": 1.0, "desplazamiento": 0.0}})})
    api("/api/cdaq/tomar", {})
    ok = esperar(lambda: cd()["estado"] == "midiendo" and all(c["ultimo"] is not None or c["aviso"] for c in cd()["canales"]), 15)
    e = cd()
    chk(g, "tomar: midiendo con las tareas del traductor validado", bool(ok), e["estado"])
    chk(g, "lo marca como probado con hardware", e["probado_con_hardware"] is True)
    c = {x["nombre"]: x for x in e["canales"]}
    t0 = c.get("T0", {}).get("ultimo")
    chk(g, "T0 (termopar en TC0) da una temperatura ambiente", t0 is not None and 10 < t0 < 45, t0)
    # 9-nov: sin depender del montaje: se activa un canal del 9213 SIN nada conectado (TC7) y se vuelve a dejar
    previo7 = (cd()["config"]["termopares"]["canales"].get("7") or {}).get("activo", False)
    conf({"termopares": {"canales": {"7": {"activo": True}}}})
    ab = esperar(lambda: next((x for x in cd()["canales"] if x["modulo"] == "termopares" and x["canal"] == "ai7"
                               and x["aviso"] == "abierto" and x["ultimo"] is None), None), 15)
    chk(g, "un canal sin termopar (TC7, sin nada) sale «abierto» (valor vacío)", bool(ab), cd().get("abiertos"))
    conf({"termopares": {"canales": {"7": {"activo": previo7}}}})
    v0 = c.get("V0", {}).get("ultimo")
    hay_v = v0 is not None and abs(v0) > 0.5
    chk(g, "V0 (tensión en AI0) se mide", True if hay_v else None, v0 if hay_v else f"sin tensión en AI0 ({v0} V): pon una fija para probarlo")
    d = drv("termopares")
    chk(g, "driver 9213: alta resolución, autocero cada muestra, CJC interna, polinomio, °C, tipo K",
        (d.get("modo_adc"), d.get("autocero"), d.get("cjc"), d.get("escala"), d.get("unidades"), (d.get("tipos") or [None])[0])
        == ("HIGH_RESOLUTION", "EVERY_SAMPLE", "BUILT_IN", "POLYNOMIAL", "DEG_C", "K"), d)
    d = drv("tensiones")
    mu = cd()["config"]["tensiones"].get("muestreo", "continuo")      # 9-nov: por defecto continuo
    chk(g, f"driver 9201: RSE, ±10 V, voltios, temporización según «{mu}»", (d.get("terminal"), d.get("rango_V"), d.get("unidades"),
        d.get("temporizacion")) == ("RSE", [-10.0, 10.0], "VOLTS", "ON_DEMAND" if mu == "bajo_demanda" else "SAMPLE_CLOCK"), d)
    # cada opción del 9213 llega al driver
    for clave, valor, propiedad, esperado in (("modo_adc", "alta_velocidad", "modo_adc", "HIGH_SPEED"),
                                              ("autozero", "una_vez", "autocero", "ONCE"),
                                              ("autozero", "no", "autocero", "NONE"),
                                              ("escala", "tabla", "escala", "TABLE"),
                                              ("unidades", "F", "unidades", "DEG_F"),
                                              ("cjc", "constante", "cjc", "CONSTANT_USER_VALUE")):
        conf({"termopares": {clave: valor}})
        chk(g, f"panel {clave}={valor} → driver {propiedad}={esperado}", reabierto("termopares", propiedad, esperado), drv("termopares").get(propiedad))
        if clave == "unidades":
            t_f = esperar(lambda: next((x["ultimo"] for x in cd()["canales"] if x["nombre"] == "T0" and x["ultimo"] and x["unidad"] == "°F"), None), 8)
            chk(g, "en °F con CJC interna, T0 sale en °F (≈ 50-115)", t_f is not None and 50 < t_f < 115, t_f)
    t_c = esperar(lambda: next((x["ultimo"] for x in cd()["canales"] if x["nombre"] == "T0" and x["ultimo"] and x["unidad"] == "°F"), None), 8)
    chk(g, "en °F con CJC constante (25 °C), T0 sale en °F (≈ 50-115)", t_c is not None and 50 < t_c < 115, t_c)
    conf({"termopares": {"modo_adc": "alta_resolucion", "autozero": "cada_muestra", "escala": "polinomio", "unidades": "C", "cjc": "interna"}})
    conf({"termopares": {"canales": {"1": {"tipo": "J", "min_C": 0, "max_C": 200}}}})
    chk(g, "tipo de un canal → driver", esperar(lambda: (drv("termopares").get("tipos") or [None, None])[1] == "J", 12), drv("termopares").get("tipos"))
    conf({"termopares": {"canales": {"1": {"tipo": "K", "min_C": -20, "max_C": 400}}}})
    # escala de la sonda en el 9201 (ganancia 10): driver FROM_CUSTOM_SCALE y lectura ×10
    conf({"tensiones": {"canales": {"0": {"ganancia": 10.0, "unidad": "V"}}}})
    ok = reabierto("tensiones", "unidades", "FROM_CUSTOM_SCALE")
    v10 = esperar(lambda: next((x["ultimo"] for x in cd()["canales"] if x["nombre"] == "V0" and x["ultimo"] is not None
                                and abs(x["ultimo"]) > 5 * abs(v0)), None), 8)
    if hay_v:
        chk(g, "escala ×10 de la sonda: la aplica el DRIVER y lee 10 veces lo mismo", ok and v10 is not None and abs(v10 / v0 - 10) < 0.2,
            f"{v0} → {v10}")
    else:
        chk(g, "escala ×10 de la sonda: el driver pasa a FROM_CUSTOM_SCALE", bool(ok))
        chk(g, "escala ×10: lee 10 veces lo mismo", None, "sin tensión en AI0")
    conf({"tensiones": {"canales": {"0": {"ganancia": 1.0}}}})
    # guardas
    c1, d1 = conf({"termopares": {"muestreo": "continuo", "tasa_S_s": 50, "canales": {str(i): {"activo": True} for i in range(16)}}}, error=True)
    chk(g, "guarda: 16 termopares a 50 S/s en alta resolución → rechazado por el panel", c1 >= 400 and "repetiría" in str(d1), d1)
    # el rango no se elige (9-nov): el de su tipo, aunque se pida otro
    conf({"termopares": {"canales": {"0": {"tipo": "T", "min_C": 0, "max_C": 500}}}})
    ok = esperar(lambda: (drv("termopares").get("tipos") or [None])[0] == "T", 12)
    r0 = (drv("termopares").get("rangos") or [[None, None]])[0]
    # el driver lo ajusta a lo que admite su entrada de ±78 mV: algo más ancho que el del tipo (T: -249,4..400)
    chk(g, "rango = el del tipo: T cubre -200..400 °C en el driver aunque se pida 0..500", bool(ok) and r0[0] <= -200 and r0[1] >= 399.9, r0)
    conf({"termopares": {"canales": {"0": {"tipo": "K"}}}})
    ok = esperar(lambda: (drv("termopares").get("tipos") or [None])[0] == "K", 12)
    r0 = (drv("termopares").get("rangos") or [[None, None]])[0]
    chk(g, "K cubre -200..1372 °C en el driver", bool(ok) and r0[0] <= -200 and r0[1] >= 1371.9, r0)
    c3, d3 = conf({"tensiones": {"muestreo": "continuo", "tasa_S_s": 900000}}, error=True)
    chk(g, "guarda: 9201 a 900 kS/s (máx 800 k) → rechazado", c3 >= 400, d3)
    c4, d4 = conf({"tensiones": {"disparo": "inicio"}}, error=True)
    chk(g, "guarda: disparo con bajo demanda → rechazado", c4 >= 400, d4)
    lim = cd()["limites"]["termopares"]
    chk(g, "el panel publica el máximo de la regla (3 termopares, alta resolución: 3,64 S/s)", abs(lim["maximo_S_s"] - 1 / (0.055 * 5)) < 1e-6, lim)


# ------------------------------------------------------------------ fase 2: reloj de hardware, En vivo, registro
def fase2():
    g = "F2 reloj de hardware"
    conf({"termopares": {"modo_adc": "alta_velocidad", "muestreo": "continuo", "tasa_S_s": 100,
                         "canales": {"2": {"activo": False}}},
          "tensiones": {"muestreo": "continuo", "tasa_S_s": 1000, "tdms": "no"}})
    ok = reabierto("termopares", "tasa_S_s", 100.0) and reabierto("tensiones", "tasa_S_s", 1000.0)
    d1, d2 = drv("termopares"), drv("tensiones")
    chk(g, "driver: 9213 continuo a 100 S/s y 9201 continuo a 1 kS/s, cada uno en su tarea", ok and d1.get("modo") == "CONTINUOUS"
        and d2.get("modo") == "CONTINUOUS" and d1.get("temporizacion") == "SAMPLE_CLOCK", (d1.get("modo"), d2.get("modo")))
    m0 = cd()["modulos"]
    time.sleep(3)
    m1 = cd()["modulos"]
    r1 = (m1["termopares"]["leidas"] - m0["termopares"]["leidas"]) / 3
    r2 = (m1["tensiones"]["leidas"] - m0["tensiones"]["leidas"]) / 3
    chk(g, "llegan ~100 muestras/s del 9213 y ~1000/s del 9201", 85 < r1 < 115 and 900 < r2 < 1100, f"{r1:.0f} y {r2:.0f} por s")
    e = api("/api/estado")[1]
    chk(g, "el cDAQ es un equipo aparte: «temperaturas» sigue siendo TC-08 o PicoLog (pueden medir a la vez)",
        e["temperaturas"]["fuente"] in ("tc08", "picolog"), e["temperaturas"])
    h = api(f"/api/cdaq/historia?desde={time.time() - 2}")[1]
    unis = {c["nombre"]: c["unidad"] for c in h["canales"]}
    n = {c["nombre"]: len(h["datos"].get(c["id"], [])) for c in h["canales"]}
    chk(g, "En vivo recibe termopares (°C) y tensión (V) con su unidad", unis.get("T0") == "°C" and unis.get("V0") == "V", unis)
    chk(g, "historia: ≤ 10 puntos/s por canal (media de 0,1 s) aunque se mida a 1 kS/s", all(5 <= x <= 25 for x in n.values()), n)
    # ✂ Tramo: cdaq.csv con lo que midió el cDAQ en esos segundos
    t1 = time.time() - 0.5
    c, r = api("/api/tramo/guardar", {"t0": t1 - 2, "t1": t1, "proyecto": "PR00737-ECOCOOK", "carpeta": "_validacion_cdaq",
                                     "nombre": "tramo_cdaq", "que": {"cdaq": True}}, error=True)
    det = (r or {}).get("ficheros", {}).get("cdaq.csv") if c == 200 else r
    chk(g, "✂ Tramo: guarda cdaq.csv con los datos del cDAQ", c == 200 and str(det).endswith("filas") and int(str(det).split()[0]) > 10, det)
    # registro CSV por la API del cDAQ
    c, r = api("/api/cdaq/registro/iniciar", {"proyecto": "PR00737-ECOCOOK", "carpeta": "_validacion_cdaq", "nombre": "reloj_hw",
                                              "pre_s": 0}, error=True)
    chk(g, "registro del cDAQ: empieza", c == 200, r if c != 200 else "")
    time.sleep(3)
    api("/api/cdaq/registro/parar", {}, error=True)
    if c == 200:
        ruta = os.path.join(r"C:\Proyectos\Osciloscopio", r["ruta"], "cdaq.csv") if "ruta" in r else None
        if ruta and os.path.exists(ruta):
            filas = [l for l in open(ruta, encoding="utf-8") if not l.startswith("#")][1:]
            n0 = sum(1 for l in filas if l.split(",")[3])            # T0
            nv = sum(1 for l in filas if l.split(",")[-2])            # V0
            chk(g, "CSV: todas las muestras (≈ 300 de T0 y ≈ 3000 de V0 en 3 s)", 250 < n0 < 360 and 2500 < nv < 3500, f"T0 {n0} · V0 {nv}")
        else:
            chk(g, "CSV escrito", False, r)


# ------------------------------------------------------------------ fase 3: tensión rápida -> TDMS del driver
def fase3():
    g = "F3 TDMS del driver"
    conf({"tensiones": {"muestreo": "continuo", "tasa_S_s": 100000, "tdms": "auto"},
          "termopares": {"muestreo": "bajo_demanda", "modo_adc": "alta_resolucion"}})
    ok = reabierto("tensiones", "tasa_S_s", 100000.0) and esperar(lambda: cd()["modulos"]["tensiones"]["tdms"] == "preparado", 10)
    chk(g, "a 100 kS/s el TDMS del driver queda preparado (en pausa)", bool(ok), cd()["modulos"]["tensiones"].get("tdms"))
    chk(g, "driver: registro LOG_AND_READ", drv("tensiones").get("registro_tdms") == "LOG_AND_READ", drv("tensiones").get("registro_tdms"))
    c, r = api("/api/cdaq/registro/iniciar", {"proyecto": "PR00737-ECOCOOK", "carpeta": "_validacion_cdaq", "nombre": "tdms",
                                              "pre_s": 0}, error=True)
    t0 = time.time()
    time.sleep(3)
    api("/api/cdaq/registro/parar", {}, error=True)
    dur = time.time() - t0
    if c != 200:
        chk(g, "registro con TDMS: empieza", False, r)
        return
    carpeta = os.path.join(r"C:\Proyectos\Osciloscopio", r["ruta"])
    p = os.path.join(carpeta, "cdaq_tensiones.tdms")
    ok = esperar(lambda: os.path.exists(p) and os.path.getsize(p) > 0, 5)
    n = None
    if ok:
        from nptdms import TdmsFile
        n = sum(len(ch) for gr in TdmsFile.read(p).groups() for ch in gr.channels())
    chk(g, f"cdaq_tensiones.tdms con TODAS las muestras (≈ {dur:.1f} s × 100 kS/s)", n is not None and abs(n - dur * 1e5) < 0.15 * dur * 1e5, n)
    filas = [l for l in open(os.path.join(carpeta, "cdaq.csv"), encoding="utf-8") if not l.startswith("#")][1:]
    nv = sum(1 for l in filas if l.split(",")[-2])
    chk(g, "el CSV lleva la media de cada 0,1 s de la tensión (≈ 30 en 3 s)", 20 <= nv <= 45, nv)
    ok = esperar(lambda: cd()["modulos"]["tensiones"]["tdms"] == "preparado" and cd()["modulos"]["tensiones"]["leidas"] > 0, 8)
    chk(g, "tras el registro la tensión sigue midiendo y el TDMS vuelve a estar preparado", bool(ok))


# ------------------------------------------------------------------ fase 4: disparos y reloj externo (sin señal)
def fase4():
    g = "F4 disparos y reloj externo"
    conf({"tensiones": {"muestreo": "continuo", "tasa_S_s": 1000, "tdms": "no", "disparo": "inicio",
                        "disparo_fuente": "PFI0", "disparo_flanco": "subida"}})
    ok = reabierto("tensiones", "disparo_inicio", "DIGITAL_EDGE")
    chk(g, "disparo de inicio por PFI0 → driver DIGITAL_EDGE", bool(ok), drv("tensiones").get("disparo_inicio"))
    time.sleep(2)
    m = cd()["modulos"]["tensiones"]
    chk(g, "sin señal en PFI0: «esperando disparo» y 0 muestras", m["situacion"] == "esperando disparo" and m["leidas"] == 0, (m["situacion"], m["leidas"]))
    conf({"tensiones": {"disparo": "pausa", "pausa_cuando": "alto"}})
    chk(g, "pausa por nivel en PFI0 → driver DIGITAL_LEVEL", bool(reabierto("tensiones", "pausa", "DIGITAL_LEVEL")), drv("tensiones").get("pausa"))
    conf({"tensiones": {"disparo": "referencia", "muestreo": "finito", "muestras": 2000, "pre_muestras": 200}})
    chk(g, "referencia (pre-disparo) por PFI0 → driver DIGITAL_EDGE", bool(reabierto("tensiones", "disparo_referencia", "DIGITAL_EDGE")),
        drv("tensiones").get("disparo_referencia"))
    conf({"tensiones": {"disparo": "ninguno", "muestreo": "continuo", "reloj": "PFI0"}})
    ok = reabierto("tensiones", "fuente_reloj", "/cDAQ1/PFI0")
    time.sleep(2)
    m = cd()["modulos"]["tensiones"]
    chk(g, "reloj externo PFI0 → driver; sin señal no llega ninguna muestra", bool(ok) and m["leidas"] == 0, (drv("tensiones").get("fuente_reloj"), m["leidas"]))
    conf({"tensiones": {"reloj": "interno"}})
    chk(g, "vuelve al reloj interno y mide", bool(esperar(lambda: cd()["modulos"]["tensiones"]["leidas"] > 0, 10)))
    chk(g, "disparo y reloj externo CON señal real en PFI0", None, "sin señal en PFI0 (pendiente)")


if __name__ == "__main__":
    main()
