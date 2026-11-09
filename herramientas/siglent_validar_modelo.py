r"""Valida siglent_modelo.py contra la SPD3303X real, SIN CARGA (autorizado por el usuario el 9-nov-2026).

Prudente: con salida encendida solo ≤ 5 V y 0,1 A. Guarda la configuración al empezar y la devuelve al terminar,
apagando todo y comprobándolo MIDIENDO. No toca *SAV/*RCL ni la red. Lanzar con algo que lo vigile (si NI-VISA se
cuelga, el proceso no se puede matar hasta que el driver lo suelta).

    python herramientas\siglent_validar_modelo.py
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import siglent_modelo as M          # noqa: E402

RES = []
DOCS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs")


def chk(g, n, cond, det=""):
    est = "OMITIDA" if cond is None else "OK" if cond else "FALLO"
    RES.append((est, g, n, str(det)))
    print(f"{est:7} [{g}] {n}" + (f"  [{det}]" if det != "" else ""), flush=True)


def st(e):
    return M.estado(e.consultar(M.ORDENES["estado"]))


def apagar_todo(e):
    for c in ("CH1", "CH2", "CH3"):
        try:
            e.escribir(M.ORDENES["salida"].format(c=c, onoff="OFF"))
            time.sleep(0.5)
        except Exception:
            pass
    time.sleep(0.5)
    return e.numero(M.ORDENES["medir_V"].format(c="CH1")), e.numero(M.ORDENES["medir_V"].format(c="CH2")), st(e)


def main():
    e = M.Enlace()
    idn = e.consultar("*IDN?")
    chk("0", "identificación", M.IDN_MODELO in idn, idn)
    previo = {c: (e.consultar(M.ORDENES["leer_V"].format(c=c)), e.consultar(M.ORDENES["leer_I"].format(c=c))) for c in M.CANALES}
    s0 = st(e)
    if s0["CH1"]["salida"] or s0["CH2"]["salida"]:
        sys.exit("Alguna salida está encendida (no por mí): no sigo")
    try:
        g = "1 opción → equipo"
        for c in M.CANALES:
            for v in (0.0, 1.234, 31.999):
                e.escribir(M.ORDENES["fijar_V"].format(c=c, v=v))
                r = float(e.consultar(M.ORDENES["leer_V"].format(c=c)))
                chk(g, f"{c} V = {v:.3f}", abs(r - v) < M.RESOLUCION_V / 2, r)
            for i in (0.001, 1.5, 3.2):
                e.escribir(M.ORDENES["fijar_I"].format(c=c, v=i))
                r = float(e.consultar(M.ORDENES["leer_I"].format(c=c)))
                chk(g, f"{c} I = {i:.3f}", abs(r - i) < M.RESOLUCION_I / 2, r)
        g = "2 guardas del modelo"
        for cfg, que in (({"CH1": {"V": -1}}, "V negativa"), ({"CH1": {"V": 33}}, "V > 32"), ({"CH2": {"I": 3.3}}, "I > 3,2"),
                         ({"CH1": {"V": 10}}, "V por encima del tope del panel (5 V)")):
            errs = M.comprobar(cfg, topes={"V": 5, "I": 1} if "tope" in que else None)
            chk(g, f"{que}: el modelo lo frena", bool(errs), errs)
        e.escribir(M.ORDENES["fijar_V"].format(c="CH1", v=-1.0))
        r = float(e.consultar(M.ORDENES["leer_V"].format(c="CH1")))
        chk(g, "el EQUIPO acepta -1 V sin avisar (por eso la guarda es del panel)", r < 0, r)
        e.escribir(M.ORDENES["fijar_V"].format(c="CH1", v=0.0))
        chk(g, "cambiar el modo con una salida encendida: el modelo lo frena",
            bool(M.comprobar({"cambia_modo": True}, salidas_encendidas=True)))
        g = "3 salida y exactitud"
        for c in M.CANALES:
            for v in (1.0, 5.0):
                e.escribir(M.ORDENES["fijar_V"].format(c=c, v=v))
                e.escribir(M.ORDENES["fijar_I"].format(c=c, v=0.1))
                e.escribir(M.ORDENES["salida"].format(c=c, onoff="ON"))
                time.sleep(0.8)
                m = e.numero(M.ORDENES["medir_V"].format(c=c))
                s = st(e)
                chk(g, f"{c} {v:g} V: mide dentro de la exactitud (±0,03 % + 10 mV) y CV", M.dentro_de_exactitud("V", v, m)
                    and s[c]["salida"] and s[c]["regula"] == "CV", f"{m} V · {s[c]}")
                e.escribir(M.ORDENES["salida"].format(c=c, onoff="OFF"))
                time.sleep(0.5)
            m, s = e.numero(M.ORDENES["medir_V"].format(c=c)), st(e)
            chk(g, f"{c} apagada: estado OFF y 0 V medidos", m < 0.05 and not s[c]["salida"], m)
        g = "4 modos"
        for modo in ("serie", "paralelo"):
            e.escribir(M.ORDENES["modo"].format(n=M.MODOS[modo]))
            time.sleep(0.8)
            s = st(e)
            e.escribir(M.ORDENES["fijar_V"].format(c="CH1", v=2.0))
            e.escribir(M.ORDENES["fijar_I"].format(c="CH1", v=0.1))
            e.escribir(M.ORDENES["salida"].format(c="CH1", onoff="ON"))
            time.sleep(1.0)
            m1, m2 = e.numero(M.ORDENES["medir_V"].format(c="CH1")), e.numero(M.ORDENES["medir_V"].format(c="CH2"))
            v2 = e.consultar(M.ORDENES["leer_V"].format(c="CH2"))
            s2 = st(e)
            apagar_todo(e)
            chk(g, f"{modo}: modo por los bits y CH1 manda (2 V en CH1)", s["modo"] == modo,
                f"miden CH1 {m1} V · CH2 {m2} V · consigna CH2 {v2} · estado {s2['CH1']} {s2['CH2']}")
        e.escribir(M.ORDENES["modo"].format(n=0))
        time.sleep(0.8)
        chk(g, "vuelta a independiente", st(e)["modo"] == "independiente")
        g = "5 APAGAR TODO"
        for c in M.CANALES:
            e.escribir(M.ORDENES["fijar_V"].format(c=c, v=1.0))
            e.escribir(M.ORDENES["salida"].format(c=c, onoff="ON"))
        e.escribir(M.ORDENES["salida"].format(c="CH3", onoff="ON"))
        time.sleep(1.0)
        v1, v2, s = apagar_todo(e)
        chk(g, "apaga CH1, CH2 y CH3 y lo confirma midiendo", v1 < 0.05 and v2 < 0.05 and not s["CH1"]["salida"] and not s["CH2"]["salida"],
            f"{v1} V · {v2} V")
        g = "6 ritmo del panel"
        t0 = time.time()
        for _ in range(4):
            for c in M.CANALES:
                e.numero(M.ORDENES["medir_V"].format(c=c))
                e.numero(M.ORDENES["medir_I"].format(c=c))
            st(e)
        dt = (time.time() - t0) / 4
        chk(g, f"una lectura completa (V e I de los dos canales + estado) con pausa {M.PAUSA_S * 1000:.0f} ms", dt < 2.0, f"{dt:.2f} s")
    finally:
        apagar_todo(e)
        e.escribir(M.ORDENES["modo"].format(n=0))
        for c, (v, i) in previo.items():
            e.escribir(f"{c}:VOLTage {v}")
            e.escribir(f"{c}:CURRent {i}")
        fin = {c: (e.consultar(M.ORDENES["leer_V"].format(c=c)), e.consultar(M.ORDENES["leer_I"].format(c=c))) for c in M.CANALES}
        print("configuración devuelta:", fin, "· estado", st(e), flush=True)
        e.cerrar()
    n = {k: sum(1 for r in RES if r[0] == k) for k in ("OK", "FALLO", "OMITIDA")}
    print(f"\n{n['OK']} OK · {n['FALLO']} FALLO · {n['OMITIDA']} OMITIDA")
    os.makedirs(DOCS, exist_ok=True)
    with open(os.path.join(DOCS, time.strftime("siglent_validacion_modelo_%Y%m%d_%H%M.json")), "w", encoding="utf-8") as f:
        json.dump(RES, f, ensure_ascii=False, indent=1)
    sys.exit(1 if n["FALLO"] else 0)


if __name__ == "__main__":
    main()
