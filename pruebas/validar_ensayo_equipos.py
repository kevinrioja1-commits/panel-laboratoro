r"""Fase 3 de la coherencia (9-nov-2026): el ensayo según los equipos conectados y marcados, por la API y con los
equipos reales. La Siglent SIN CARGA: solo se enciende CH1 a ≤ 2 V / 0,1 A (autorizado por el usuario el 9-nov).
La fuente EA solo se registra (no se enciende). Al terminar: Siglent apagada y con las consignas que tenía.
    python pruebas\validar_ensayo_equipos.py
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from validar_siglent_panel import api, chk, esperar, sp, RES   # noqa: E402

BASE = r"C:\Proyectos\Osciloscopio"
PROY = "PR00737-ECOCOOK"


def estado():
    return api("/api/estado")[1]


def ensayo(nombre, equipos, durante=None, apagar=True, espera=4):
    c, r = api("/api/ensayo/iniciar", {"proyecto": PROY, "carpeta": "_validacion_equipos", "nombre": nombre,
                                       "pre_s": 0, "cola_s": 1, "apagar_al_terminar": apagar, "temperaturas": True,
                                       "equipos": equipos}, error=True)
    if c != 200:
        return c, r, None, None
    if durante:
        durante()
    time.sleep(espera)
    api("/api/ensayo/terminar", {})
    esperar(lambda: estado().get("ensayo") is None, 20)
    ruta = os.path.join(BASE, r["rel"])
    with open(os.path.join(ruta, "ensayo.json"), encoding="utf-8") as fh:
        ficha = json.load(fh)
    return c, r, ruta, ficha


def _cabecera_csv(p):
    return next(l for l in open(p, encoding="utf-8") if l.startswith("fecha_hora")).strip()


def filas(ruta, f):
    p = os.path.join(ruta, f)
    if not os.path.exists(p):
        return None
    return len([l for l in open(p, encoding="utf-8") if not l.startswith("#")]) - 1


def encender_ch1():
    api("/api/siglent/fijar", {"canal": "CH1", "V": 2.0, "I": 0.1})
    api("/api/siglent/salida", {"canal": "CH1", "encender": True, "confirmado": True})
    esperar(lambda: sp()["canales"]["CH1"]["salida"] and (sp()["canales"]["CH1"]["V"] or 0) > 1.9)


def main():
    c, r = api("/api/siglent/conectar", {}, error=True)
    if c != 200 or r["estado"] != "conectada":
        sys.exit(f"Siglent sin conectar: {r}")
    c, r = api("/api/fuente/conectar", {}, error=True)
    ea = c == 200 and estado()["fuente"]["estado"] == "conectada"
    print("fuente EA:", "conectada" if ea else f"no ({r})", "· cDAQ:", estado()["cdaq"]["estado"], flush=True)
    esperar(lambda: all(x["V_cons"] is not None for x in sp()["canales"].values()))
    previo = {k: (v["V_cons"], v["I_cons"]) for k, v in sp()["canales"].items()}
    if sp()["encendida"]:
        sys.exit("La Siglent tiene alguna salida encendida (no por mí): no sigo")
    try:
        g = "solo Siglent + cDAQ"
        c, r, ruta, f = ensayo("siglent_cdaq", {"fuente": False, "siglent": True, "cdaq": True, "temperaturas": False},
                               durante=encender_ch1)
        chk(g, "inicia sin la fuente EA", c == 200, r if c != 200 else r["rel"])
        if c == 200:
            chk(g, "ensayo.json: equipos = Siglent y los módulos del cDAQ que miden", f.get("equipos")[:1] == ["Siglent"]
                and all(x.startswith("cDAQ") for x in f["equipos"][1:]) and len(f["equipos"]) > 1, f.get("equipos"))
            chk(g, "ficha de la Siglent en ensayo.json", bool(f.get("siglent")), list((f.get("siglent") or {}).keys())[:6])
            chk(g, "siglent.csv con filas", (filas(ruta, "siglent.csv") or 0) >= 4, filas(ruta, "siglent.csv"))
            chk(g, "cdaq.csv con filas", (filas(ruta, "cdaq.csv") or 0) >= 4, filas(ruta, "cdaq.csv"))
            chk(g, "sin fuente.csv (la EA no participa)", not os.path.exists(os.path.join(ruta, "fuente.csv")), os.listdir(ruta))
            q = esperar(lambda: sp() if not sp()["encendida"] and sp()["canales"]["CH1"]["V"] is not None and sp()["canales"]["CH1"]["V"] < 0.05 else None)
            chk(g, "«apagar al terminar» apaga la Siglent (medido 0 V)", bool(q), sp()["canales"]["CH1"])
            txt = open(os.path.join(ruta, "siglent.csv"), encoding="utf-8").read()
            chk(g, "la marca del apagado está en siglent.csv", "fin del ensayo" in txt or "apag" in txt.lower(), "")

        g = "la Siglent que NO participa no se toca"
        encender_ch1()
        c, r, ruta, f = ensayo("solo_cdaq", {"fuente": False, "siglent": False, "cdaq": True, "temperaturas": False})
        chk(g, "ensayo solo cDAQ", c == 200 and all(x.startswith("cDAQ") for x in f.get("equipos")), f.get("equipos") if f else r)
        if c == 200:
            mods = (f.get("cdaq") or {}).get("modulos_en_ensayo") or []
            if len(mods) == 2:   # 9-nov: termopares y tensiones por separado
                c2, r2, ruta2, f2 = ensayo("solo_tensiones", {"fuente": False, "siglent": False, "cdaq_termopares": False,
                                                              "temperaturas": False})
                cab = _cabecera_csv(os.path.join(ruta2, "cdaq.csv")) if c2 == 200 else ""
                chk(g, "cDAQ solo tensiones: ni termopares en equipos ni columnas del 9213 en cdaq.csv",
                    c2 == 200 and f2["equipos"] == ["cDAQ tensiones"] and "_C," not in cab + "," and "_V" in cab, f"{(f2 or {}).get('equipos', r2)} · columnas {cab}")
            else:
                chk(g, "cDAQ solo tensiones", None, f"el cDAQ mide {mods}")
        chk(g, "sin siglent.csv", c == 200 and not os.path.exists(os.path.join(ruta, "siglent.csv")), os.listdir(ruta) if ruta else "")
        chk(g, "la Siglent sigue encendida tras terminar (alimenta otra cosa)", sp()["canales"]["CH1"]["salida"], sp()["canales"]["CH1"])
        api("/api/siglent/salida", {"canal": "CH1", "encender": False})
        esperar(lambda: not sp()["encendida"])

        g = "sin equipos"
        c, r, _, _ = ensayo("nada", {"fuente": False, "siglent": False, "cdaq": False, "temperaturas": False})
        chk(g, "sin ningún equipo marcado: rechazado con motivo", c >= 400 and "equipo" in str(r), f"{c} {r}")
        c, r = api("/api/ensayo/iniciar", {"proyecto": PROY, "nombre": "x", "encender_al_iniciar": True, "confirmado": True,
                                           "equipos": {"fuente": False, "cdaq": True}}, error=True)
        chk(g, "«encender al iniciar» sin la EA: rechazado", c >= 400 and "EA" in str(r), f"{c} {r}")
        if c == 200:
            api("/api/ensayo/terminar", {})

        g = "fuente EA"
        if not ea:
            chk(g, "ensayo con la EA (sin encender)", None, "EA no conectada")
        else:
            c, r, ruta, f = ensayo("con_ea", {})
            chk(g, "por defecto entran todos los conectados", c == 200 and "fuente EA" in f["equipos"] and "Siglent" in f["equipos"],
                f.get("equipos") if f else r)
            chk(g, "fuente.csv y siglent.csv con filas", c == 200 and (filas(ruta, "fuente.csv") or 0) >= 4
                and (filas(ruta, "siglent.csv") or 0) >= 4, (filas(ruta, "fuente.csv"), filas(ruta, "siglent.csv")) if ruta else "")
    finally:
        if estado().get("ensayo"):
            api("/api/ensayo/terminar", {}, error=True)
            esperar(lambda: estado().get("ensayo") is None, 20)
        api("/api/siglent/apagar", {}, error=True)
        for ch, (v, i) in previo.items():
            api("/api/siglent/fijar", {"canal": ch, "V": v, "I": i}, error=True)
        q = sp()
        print("Siglent devuelta:", {k: (x["V_cons"], x["I_cons"], x["salida"]) for k, x in q["canales"].items()}, flush=True)
    n = {k: sum(1 for x in RES if x[0] == k) for k in ("OK", "FALLO", "OMITIDA")}
    print(f"\n{n['OK']} OK · {n['FALLO']} FALLO · {n['OMITIDA']} OMITIDA")
    sys.exit(1 if n["FALLO"] else 0)


if __name__ == "__main__":
    main()
