r"""Fase 4 (9-nov-2026): ensayos PROGRAMADOS con varias fuentes, condiciones y guardas, con los equipos reales.

Montaje del usuario (9-nov): EA con la resistencia de 1 Ω y el osciloscopio midiendo en bornas (C1); Siglent SIN carga;
cDAQ con un termopar en TC0 (ambiente). Encendidos autorizados por el usuario. Prudente: la EA como mucho 1 A
(1 W en la resistencia) y la Siglent 5 V / 0,1 A. Las condiciones de temperatura se prueban con la temperatura real
(ambiente) y umbrales por encima y por debajo: los dos caminos (se cumple / no se cumple) sin calentar nada.
    python pruebas\validar_plan_fase4.py
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from validar_siglent_panel import api, chk, esperar, RES   # noqa: E402

BASE = r"C:\Proyectos\Osciloscopio"
PROY = "PR00737-ECOCOOK"
CARPETA = "_validacion_fase4"


def E():
    return api("/api/estado")[1]


def comprobar(plan):
    return api("/api/plan/comprobar", {"plan": plan, "osc": False}, error=True)


def correr(nombre, plan, equipos=None, osc=None, limite=120):
    c, r = api("/api/ensayo/iniciar", {"proyecto": PROY, "carpeta": CARPETA, "nombre": nombre, "plan": plan,
                                       "confirmado": True, "pre_s": 2, "cola_s": 1, "apagar_al_terminar": True,
                                       "temperaturas": False, "equipos": equipos or {}, "osc": osc}, error=True)
    if c != 200:
        return c, r, None, None
    fin = time.time() + limite
    while time.time() < fin and E().get("ensayo"):
        time.sleep(0.5)
    if E().get("ensayo"):
        api("/api/ensayo/plan/parar", {}, error=True)
        esperar(lambda: E().get("ensayo") is None, 30)
    ruta = os.path.join(BASE, r["rel"])
    with open(os.path.join(ruta, "ensayo.json"), encoding="utf-8") as fh:
        return c, r, ruta, json.load(fh)


def filas(ruta, f):
    p = os.path.join(ruta, f)
    if not os.path.exists(p):
        return []
    ls = [l.rstrip("\n") for l in open(p, encoding="utf-8") if not l.startswith("#")]
    cab = ls[0].split(",")
    return [dict(zip(cab, l.split(",", len(cab) - 1))) for l in ls[1:]]


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def todo_apagado():
    e = E()
    return (not (e["fuente"].get("medida") or {}).get("salida")) and not e["siglent"]["encendida"]


def main():
    e = E()
    if e["fuente"]["estado"] != "conectada" or e["siglent"]["estado"] != "conectada" or e["cdaq"]["estado"] != "midiendo":
        sys.exit(f"Faltan equipos: EA {e['fuente']['estado']}, Siglent {e['siglent']['estado']}, cDAQ {e['cdaq']['estado']}")
    if not todo_apagado():
        sys.exit("Hay alguna salida encendida (no por mí): no sigo")
    t0 = next(c["ultimo"] for c in e["cdaq"]["canales"] if c["nombre"] == "T0")
    print(f"T0 ambiente = {t0:.2f} °C", flush=True)
    prev_ea = {k: e["fuente"]["consignas"][k] for k in ("V_lim_V", "I_A", "P_W")}
    prev_spd = {k: (v["V_cons"], v["I_cons"]) for k, v in e["siglent"]["canales"].items()}
    MX = {"V": 2, "I": 1, "P": 2}
    MXS = {"V": 5, "I": 0.2}
    try:
        # ------------------------------------------------------------ 1. comprobación del plan
        g = "1 comprobar"
        malos = [
            ({"maximos_spd": MXS, "etapas": [{"tipo": "consignas", "fuente": "spd.CH1", "V": 6, "salida": "on"}]},
             "Siglent por encima del máximo del plan", "pasa el máximo del plan"),
            ({"maximos_spd": MXS, "etapas": [{"tipo": "consignas", "fuente": "spd.CH3", "V": 3}]},
             "CH3 con tensión", "CH3"),
            ({"etapas": [{"tipo": "esperar", "condicion": "cdaq.T9 >= 50", "max_s": 5}]},
             "condición con una medida que no existe", "no existe"),
            ({"etapas": [{"tipo": "esperar", "condicion": "cdaq.T0 mayor 50"}]}, "condición mal escrita", "no válida"),
            ({"maximos_spd": MXS, "etapas": [{"tipo": "consignas", "fuente": "spd.CH1", "V": -1}]}, "Siglent negativa", "negativ"),
            ({"maximos_spd": {"V": 40, "I": 0.2}, "etapas": [{"tipo": "consignas", "fuente": "spd.CH1", "V": 3}]},
             "máximo de la Siglent por encima de su tope", "tope"),
            ({"maximos": MX, "etapas": [{"tipo": "consignas", "V": 3, "salida": "on"}]}, "EA por encima del máximo", "máximo del plan"),
            ({"etapas": [{"tipo": "consignas", "fuente": "spd.CH1", "V": 3}]}, "Siglent sin maximos_spd", "maximos_spd"),
        ]
        for plan, que, debe in malos:
            c, d = comprobar(plan)
            chk(g, f"rechaza: {que}", c in (400, 409, 422) and debe in str(d), str(d)[:160])
        c, d = comprobar({"maximos": MX, "maximos_spd": MXS, "guardas": ["cdaq.T0 >= 45"],
                          "etapas": [{"tipo": "consignas", "V": 2, "I": 0.2, "P": 2, "salida": "on"},
                                     {"tipo": "consignas", "fuente": "spd.CH1", "V": 3, "I": 0.1, "salida": "on"},
                                     {"tipo": "esperar", "condicion": "cdaq.T0 >= 10", "max_s": 5}, {"tipo": "apagar", "fuente": "todo"}]})
        chk(g, "acepta un plan con EA + Siglent + esperar + guarda, y lo resume", c == 200 and d["resumen"]["usa_ea"]
            and d["resumen"]["usa_spd"] and "guardas" in d["resumen"]["texto"], (d.get("resumen") or {}).get("texto") if c == 200 else d)
        c, d = api("/api/ensayo/iniciar", {"proyecto": PROY, "carpeta": CARPETA, "nombre": "x", "confirmado": True,
                                           "equipos": {"siglent": False},
                                           "plan": {"maximos_spd": MXS, "etapas": [{"tipo": "consignas", "fuente": "spd.CH1", "V": 1, "I": 0.1}]}},
                   error=True)
        chk(g, "un plan que mueve la Siglent no empieza si la Siglent no está marcada", c >= 400 and "márcala" in str(d), d)
        c, d = comprobar({"maximos_spd": MXS, "etapas": [{"tipo": "consignas", "fuente": "spd.CH1", "V": 1, "salida": "on"}]})
        chk(g, "como con la EA: encender con una consigna que ya tenía la Siglent por encima del máximo del plan (I 0,5 > 0,2) se rechaza",
            c >= 400 and "I = 0.5 A pasa el máximo" in str(d), str(d)[:160])

        # ------------------------------------------------------------ 2. el ensayo completo (con osciloscopio)
        g = "2 ensayo EA + Siglent + condiciones"
        plan = {"nombre": "fase4_demo", "maximos": MX, "maximos_spd": MXS, "guardas": ["cdaq.T0 >= 45", "ea.I >= 0.95"],
                "etapas": [
                    {"tipo": "marca", "texto": "inicio de la demo de la fase 4"},
                    {"tipo": "consignas", "V": 2, "I": 0.2, "P": 2, "salida": "on"},
                    {"tipo": "consignas", "fuente": "spd.CH1", "V": 3, "I": 0.1, "salida": "on"},
                    {"tipo": "esperar", "condicion": "ea.I >= 0.15", "max_s": 10},
                    {"tipo": "rampa", "magnitud": "I", "hasta": 0.8, "paso": 0.2, "duracion_s": 8},
                    {"tipo": "rampa", "fuente": "spd.CH1", "magnitud": "V", "hasta": 5, "paso": 1, "duracion_s": 4},
                    {"tipo": "esperar", "condicion": "cdaq.T0 >= 10", "max_s": 5},
                    {"tipo": "mantener", "duracion_s": 3},
                    {"tipo": "esperar", "condicion": "cdaq.T0 >= 60", "max_s": 4, "si_no": "seguir"},
                    {"tipo": "apagar", "fuente": "todo"}]}
        c, r, ruta, f = correr("fase4_demo", plan, osc={"canales": [1], "sr": 100000}, limite=150)
        chk(g, "el ensayo programado empieza (EA, Siglent, cDAQ y osciloscopio)", c == 200, r if c != 200 else r["rel"])
        if c == 200:
            print("   carpeta:", ruta, flush=True)
            chk(g, "el plan termina bien", f.get("plan_estado_final") == "terminado", f"{f.get('plan_estado_final')} · {f.get('plan_mensaje')}")
            h = f.get("plan_historial") or []
            chk(g, "las 10 etapas, en orden", len(h) == 10 and all(x.get("resultado") == "fin" for x in h), [x.get("resultado") for x in h])
            fu = filas(ruta, "fuente.csv")
            # la meseta a 0,8 A (CC): media de las lecturas; la EA (20 A) mide con ±0,06 A de dispersión a 0,8 A
            mes = [num(x.get("I_A")) for x in fu if num(x.get("I_consigna_A")) == 0.8 and x.get("modo") == "CC" and x.get("salida") == "1"]
            media = sum(mes) / len(mes) if mes else None
            imax = max((num(x.get("I_A")) or 0) for x in fu) if fu else None
            chk(g, "fuente.csv: la EA regula a 0,8 A en CC por la resistencia (media de la meseta)", media is not None and abs(media - 0.8) < 0.02,
                f"media {media:.4f} A con {len(mes)} lecturas" if media else "sin meseta")
            chk(g, "y nunca pasa de 1 A (ni con el ruido de su medida)", imax is not None and imax < 1.0, imax)
            sg = filas(ruta, "siglent.csv")
            vmax = max((num(x.get("CH1_V")) or 0) for x in sg) if sg else None
            chk(g, "siglent.csv: CH1 llega a 5 V (rampa 3 → 5 V)", vmax is not None and 4.95 <= vmax <= 5.05, vmax)
            chk(g, "cdaq.csv y osc.h5 en la carpeta", os.path.exists(os.path.join(ruta, "cdaq.csv")) and os.path.exists(os.path.join(ruta, "osc.h5")),
                sorted(os.listdir(ruta)))
            txt = open(os.path.join(ruta, "fuente.csv"), encoding="utf-8").read()
            chk(g, "las etapas y las condiciones quedan como eventos en fuente.csv",
                "cumplido: ea.I >= 0.15" in txt and "cumplido: cdaq.T0 >= 10" in txt and "no se ha cumplido cdaq.T0 >= 60" in txt, "")
            stxt = open(os.path.join(ruta, "siglent.csv"), encoding="utf-8").read()
            chk(g, "y también en siglent.csv", "etapa 6/10" in stxt and "cumplido" in stxt, "")
            chk(g, "al terminar: todo apagado (medido)", esperar(todo_apagado, 8))
            chk(g, "ensayo.json: equipos y plan con Siglent y guardas", "Siglent" in f.get("equipos", []) and "fuente EA" in f["equipos"]
                and f["plan"].get("guardas") == ["cdaq.T0 >= 45", "ea.I >= 0.95"], f.get("equipos"))

        if os.environ.get("SOLO12"):
            return
        # ------------------------------------------------------------ 3. guardas
        g = "3 guardas"
        plan = {"maximos": MX, "maximos_spd": MXS, "guardas": ["ea.I >= 0.5"],
                "etapas": [{"tipo": "consignas", "V": 2, "I": 0.2, "P": 2, "salida": "on"},
                           {"tipo": "consignas", "fuente": "spd.CH1", "V": 3, "I": 0.1, "salida": "on"},
                           {"tipo": "rampa", "magnitud": "I", "hasta": 0.8, "paso": 0.1, "duracion_s": 8},
                           {"tipo": "mantener", "duracion_s": 10}]}
        c, r, ruta, f = correr("fase4_guarda_corriente", plan, limite=60)
        chk(g, "guarda «ea.I >= 0.5»: para el plan al pasar de 0,5 A", c == 200 and f.get("plan_estado_final") == "error"
            and "GUARDA" in (f.get("plan_mensaje") or ""), (f or {}).get("plan_mensaje") if c == 200 else r)
        if c == 200:
            fu = filas(ruta, "fuente.csv")
            imax = max((num(x.get("I_A")) or 0) for x in fu)
            chk(g, "no llega a la consigna final de la rampa (0,8 A)", imax < 0.75, imax)
            chk(g, "la guarda APAGA TODO: EA y Siglent (medido)", esperar(todo_apagado, 8))
        # temperatura: «se apaga al llegar a X» con la temperatura real (umbral por debajo del ambiente = ya cumplido)
        plan = {"maximos_spd": MXS, "guardas": [f"cdaq.T0 >= {t0 - 5:.1f}"],
                "etapas": [{"tipo": "consignas", "fuente": "spd.CH1", "V": 3, "I": 0.1, "salida": "on"},
                           {"tipo": "mantener", "duracion_s": 10}]}
        c, r, ruta, f = correr("fase4_guarda_temperatura", plan, equipos={"fuente": False}, limite=40)
        chk(g, f"guarda de temperatura «cdaq.T0 >= {t0 - 5:.1f}» (ya cumplida): no llega a encender y para",
            c == 200 and f.get("plan_estado_final") == "error" and "GUARDA" in (f.get("plan_mensaje") or ""), (f or {}).get("plan_mensaje") if c == 200 else r)
        if c == 200:
            sg = filas(ruta, "siglent.csv")
            chk(g, "la Siglent nunca se encendió", all((num(x.get("CH1_V")) or 0) < 0.05 for x in sg), len(sg))
        # esperar que no se cumple con «parar»
        plan = {"maximos_spd": MXS, "etapas": [{"tipo": "consignas", "fuente": "spd.CH1", "V": 2, "I": 0.1, "salida": "on"},
                                              {"tipo": "esperar", "condicion": f"cdaq.T0 >= {t0 + 30:.0f}", "max_s": 3},
                                              {"tipo": "marca", "texto": "no debería llegar aquí"}]}
        c, r, ruta, f = correr("fase4_esperar_parar", plan, equipos={"fuente": False}, limite=40)
        chk(g, "esperar que no se cumple (si no: PARAR): el plan para y apaga", c == 200 and f.get("plan_estado_final") == "error"
            and "no se ha cumplido" in (f.get("plan_mensaje") or "") and len(f.get("plan_historial") or []) == 2,
            (f or {}).get("plan_mensaje") if c == 200 else r)
        chk(g, "y deja la Siglent apagada (medido)", esperar(todo_apagado, 8))

        # ------------------------------------------------------------ 4. solo la Siglent (sin la EA en el ensayo)
        g = "4 solo Siglent"
        plan = {"maximos_spd": MXS, "etapas": [{"tipo": "consignas", "fuente": "spd.CH2", "V": 1, "I": 0.1, "salida": "on"},
                                              {"tipo": "rampa", "fuente": "spd.CH2", "magnitud": "V", "hasta": 2, "paso": 0.5, "duracion_s": 2},
                                              {"tipo": "consignas", "fuente": "spd.CH3", "salida": "on"},
                                              {"tipo": "mantener", "duracion_s": 1},
                                              {"tipo": "apagar", "fuente": "spd.CH3"},
                                              {"tipo": "apagar", "fuente": "spd.CH2"}]}
        c, r, ruta, f = correr("fase4_solo_siglent", plan, equipos={"fuente": False}, limite=40)
        chk(g, "plan solo con la Siglent (EA fuera del ensayo): termina", c == 200 and f.get("plan_estado_final") == "terminado",
            (f or {}).get("plan_mensaje") if c == 200 else r)
        if c == 200:
            sg = filas(ruta, "siglent.csv")
            chk(g, "CH2 llega a 2 V", max((num(x.get("CH2_V")) or 0) for x in sg) >= 1.95, "")
            chk(g, "sin fuente.csv (la EA no participa)", not os.path.exists(os.path.join(ruta, "fuente.csv")), "")
        chk(g, "todo apagado al final", esperar(todo_apagado, 8))
    finally:
        if E().get("ensayo"):
            api("/api/ensayo/plan/parar", {}, error=True)
            esperar(lambda: E().get("ensayo") is None, 30)
        api("/api/apagar_todo", {}, error=True)
        api("/api/fuente/fijar", prev_ea, error=True)
        for ch, (v, i) in prev_spd.items():
            if v is not None:
                api("/api/siglent/fijar", {"canal": ch, "V": v, "I": i}, error=True)
        e = E()
        print("devuelto: EA", e["fuente"]["consignas"], "· Siglent", {k: (x["V_cons"], x["I_cons"], x["salida"])
                                                                    for k, x in e["siglent"]["canales"].items()}, flush=True)
    n = {k: sum(1 for x in RES if x[0] == k) for k in ("OK", "FALLO", "OMITIDA")}
    print(f"\n{n['OK']} OK · {n['FALLO']} FALLO · {n['OMITIDA']} OMITIDA")
    sys.exit(1 if n["FALLO"] else 0)


if __name__ == "__main__":
    main()
