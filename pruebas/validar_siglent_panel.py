r"""La Siglent SPD3303X A TRAVÉS DEL PANEL (fase 2, 9-nov-2026), con el equipo real y SIN CARGA.

Encendidos solo ≤ 5 V / 0,1 A (autorizado por el usuario el 9-nov). Al terminar: todo apagado (comprobado midiendo),
modo independiente y las consignas que había.
    python pruebas\validar_siglent_panel.py
"""
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


def chk(g, n, cond, det=""):
    est = "OMITIDA" if cond is None else "OK" if cond else "FALLO"
    RES.append((est, g, n, str(det)))
    print(f"{est:7} [{g}] {n}" + (f"  [{det}]" if det != "" else ""), flush=True)


def sp():
    return api("/api/estado")[1]["siglent"]


def esperar(cond, t=8.0):
    fin = time.time() + t
    while time.time() < fin:
        x = cond()
        if x:
            return x
        time.sleep(0.3)
    return cond()


def main():
    c, r = api("/api/siglent/conectar", {}, error=True)
    g = "conexión"
    chk(g, "conectar: la reconoce", c == 200 and r["estado"] == "conectada" and "SPD3303X" in r["idn"], r if c != 200 else r["idn"])
    if c != 200 or r["estado"] != "conectada":
        sys.exit(1)
    esperar(lambda: all(x["V_cons"] is not None and x["I_cons"] is not None for x in sp()["canales"].values()))
    previo = {k: (v["V_cons"], v["I_cons"]) for k, v in sp()["canales"].items()}
    chk(g, "lee consignas, medidas y modo", sp()["modo"] == "independiente" and all(x[0] is not None for x in previo.values()), previo)
    try:
        g = "consignas"
        api("/api/siglent/fijar", {"canal": "CH1", "V": 1.234, "I": 0.1})
        q = sp()["canales"]["CH1"]
        chk(g, "CH1 1,234 V / 0,1 A: leído de vuelta", (q["V_cons"], q["I_cons"]) == (1.234, 0.1), q)
        for cuerpo, que in (({"canal": "CH1", "V": -1}, "negativa"), ({"canal": "CH2", "V": 33}, "> 32 V"),
                            ({"canal": "CH1", "I": 3.3}, "> 3,2 A")):
            c, d = api("/api/siglent/fijar", cuerpo, error=True)
            chk(g, f"guarda: consigna {que} rechazada por el panel", c == 409, d)
        api("/api/siglent/opciones", {"topes": {"V": 5}})
        c, d = api("/api/siglent/fijar", {"canal": "CH1", "V": 6}, error=True)
        chk(g, "tope de seguridad del panel (5 V): 6 V rechazado", c == 409 and "tope" in str(d), d)
        api("/api/siglent/opciones", {"topes": {"V": 32}})
        g = "salida"
        c, d = api("/api/siglent/salida", {"canal": "CH1", "encender": True}, error=True)
        chk(g, "encender sin confirmar: rechazado", c == 409 and "confirmación" in str(d), d)
        for ch in ("CH1", "CH2"):
            api("/api/siglent/fijar", {"canal": ch, "V": 5.0, "I": 0.1})
            api("/api/siglent/salida", {"canal": ch, "encender": True, "confirmado": True})
            q = esperar(lambda: sp()["canales"][ch] if sp()["canales"][ch]["salida"] and (sp()["canales"][ch]["V"] or 0) > 4.9 else None)
            chk(g, f"{ch} encendida a 5 V: el panel la ve medir 5 V en CV", bool(q) and abs(q["V"] - 5) <= 0.0115 and q["regula"] == "CV", q)
            c, d = api("/api/siglent/modo", {"modo": "serie"}, error=True)
            chk(g, f"con {ch} encendida no deja cambiar el modo", c == 409, d)
            api("/api/siglent/salida", {"canal": ch, "encender": False})
            q = esperar(lambda: sp()["canales"][ch] if not sp()["canales"][ch]["salida"] and sp()["canales"][ch]["V"] is not None and sp()["canales"][ch]["V"] < 0.05 else None)
            chk(g, f"{ch} apagada: el panel la ve a 0 V", bool(q), q)
        g = "APAGAR TODO"
        for ch in ("CH1", "CH2", "CH3"):
            if ch != "CH3":
                api("/api/siglent/fijar", {"canal": ch, "V": 1.0})
            api("/api/siglent/salida", {"canal": ch, "encender": True, "confirmado": True})
        c, d = api("/api/apagar_todo", {}, error=True)
        q = esperar(lambda: sp() if not sp()["encendida"] and all(sp()["canales"][x]["V"] is not None and sp()["canales"][x]["V"] < 0.05 for x in ("CH1", "CH2")) else None)
        chk(g, "APAGAR TODO del panel apaga CH1, CH2 y CH3 (comprobado midiendo)", c == 200 and d.get("siglent") == "apagada" and bool(q), d)
        g = "modos"
        for modo in ("serie", "paralelo", "independiente"):
            c, d = api("/api/siglent/modo", {"modo": modo}, error=True)
            chk(g, f"modo {modo} (salidas apagadas)", c == 200 and d["modo"] == modo, d if c != 200 else "")
        api("/api/siglent/modo", {"modo": "serie"})
        c, d = api("/api/siglent/fijar", {"canal": "CH1", "V": 31}, error=True)
        chk(g, "serie: 31 V en CH1 rechazado (el equipo se quedaría en 30 sin avisar)", c == 409, d)
        c, d = api("/api/siglent/fijar", {"canal": "CH2", "V": 1}, error=True)
        chk(g, "serie: CH2 no se ajusta (manda CH1)", c == 409, d)
        api("/api/siglent/modo", {"modo": "independiente"})
        g = "historia y ficheros"
        h = api(f"/api/siglent/historia?desde={time.time() - 20}")[1]
        chk(g, "historia en el formato común (CH1/CH2 V y A)", {c["id"] for c in h["canales"]} == {"CH1.V", "CH1.I", "CH2.V", "CH2.I"}
            and all(len(v) > 5 for v in h["datos"].values()), {k: len(v) for k, v in h["datos"].items()})
        c, r = api("/api/siglent/registro/iniciar", {"proyecto": "PR00737-ECOCOOK", "carpeta": "_validacion_siglent", "nombre": "registro",
                                                    "pre_s": 0}, error=True)
        time.sleep(3)
        api("/api/siglent/registro/parar", {}, error=True)
        ruta = os.path.join(r"C:\Proyectos\Osciloscopio", r["ruta"], "siglent.csv") if c == 200 else ""
        n = len([l for l in open(ruta, encoding="utf-8") if not l.startswith("#")]) - 1 if ruta and os.path.exists(ruta) else 0
        chk(g, "registro: siglent.csv con filas", n >= 3, f"{n} filas")
        t1 = time.time() - 0.5
        c, r = api("/api/tramo/guardar", {"t0": t1 - 3, "t1": t1, "proyecto": "PR00737-ECOCOOK", "carpeta": "_validacion_siglent",
                                         "nombre": "tramo", "que": {"siglent": True}}, error=True)
        det = (r or {}).get("ficheros", {}).get("siglent.csv") if c == 200 else r
        chk(g, "✂ Tramo: siglent.csv", c == 200 and str(det).endswith("filas"), det)
    finally:
        api("/api/siglent/apagar", {}, error=True)
        api("/api/siglent/modo", {"modo": "independiente"}, error=True)
        api("/api/siglent/opciones", {"topes": {"V": 32, "I": 3.2}}, error=True)
        for ch, (v, i) in previo.items():
            if v is not None and i is not None:
                api("/api/siglent/fijar", {"canal": ch, "V": v, "I": i}, error=True)
        q = sp()
        print("devuelta:", {k: (x["V_cons"], x["I_cons"], x["salida"]) for k, x in q["canales"].items()}, q["modo"], flush=True)
    n = {k: sum(1 for r in RES if r[0] == k) for k in ("OK", "FALLO", "OMITIDA")}
    print(f"\n{n['OK']} OK · {n['FALLO']} FALLO · {n['OMITIDA']} OMITIDA")
    sys.exit(1 if n["FALLO"] else 0)


if __name__ == "__main__":
    main()
