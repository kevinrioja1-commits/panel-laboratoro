r"""Validacion con la FUENTE REAL de los arreglos de seguridad de la salida (2026-11-04, revision externa ALTA 1-4).

Montaje: fuente EA-PS 9500-20 por USB, salida sobre la resistencia de 1 ohm (como la validacion del 2-nov).
El osciloscopio no hace falta. Todo va por la API del panel (autor «claude»); el panel debe estar arrancado.

ENCIENDE LA SALIDA (como mucho 3 V / 0,6 A / 5 W sobre 1 ohm): lanzarlo SOLO con la confirmacion expresa del
usuario, cada vez:
    python validar_seguridad.py --proyecto PR00737-ECOCOOK

Que comprueba (OK / FALLO):
  1  APAGAR (la orden de la pagina) durante un plan: bloqueo, plan en pausa al momento, la salida NO se vuelve a
     encender aunque el plan llegue a una etapa «salida ON»; Continuar sin confirmar se rechaza; confirmado, enciende
  2  «local» (control devuelto al frontal) durante una rampa rapida (0,2 s por paso): el plan se pausa y no escribe
     mas consignas; PARAR apaga y termina
  3  desconectar con la salida encendida se rechaza
  4  «encender al iniciar» sin confirmar se rechaza
Al terminar: salida APAGADA y consignas como estaban. Los ensayos de prueba quedan en <proyecto>\ensayos\
_validacion_seguridad (no se borra nada sin permiso del usuario). Informe en la misma carpeta.
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

PANEL = "http://127.0.0.1:8765"
RES = []


def api(ruta, datos=None, error=False):
    req = urllib.request.Request(PANEL + ruta, headers={"X-Autor": "claude", "Content-Type": "application/json"},
                                 data=None if datos is None else json.dumps(datos).encode())
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
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


def ok(nombre, cond, detalle=""):
    RES.append((nombre, bool(cond), str(detalle)))
    print(("OK    " if cond else "FALLO ") + nombre + (f"  [{detalle}]" if detalle else ""), flush=True)


def esperar(cond, t):
    fin = time.time() + t
    while time.time() < fin:
        if cond():
            return True
        time.sleep(0.1)
    return cond()


def salida():
    return bool(est()["fuente"]["consignas"].get("salida"))


def plan_estado():
    e = est().get("ensayo") or {}
    return (e.get("plan") or {}).get("estado")


def iniciar(proyecto, nombre, etapas):
    plan = {"maximos": {"V": 3, "I": 0.6, "P": 5}, "etapas": etapas}
    return api("/api/ensayo/iniciar", {"proyecto": proyecto, "carpeta": "_validacion_seguridad", "nombre": nombre,
                                       "plan": plan, "confirmado": True, "pre_s": 2, "cola_s": 1,
                                       "apagar_al_terminar": True, "temperaturas": False})


def terminar_si_hay():
    if est().get("ensayo"):
        api("/api/ensayo/plan/parar", {}, error=True)
        api("/api/ensayo/terminar", {}, error=True)
        esperar(lambda: not est().get("ensayo"), 15)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--proyecto", required=True)
    ap.add_argument("--simulada", action="store_true", help="ensayar el propio script con la fuente simulada")
    a = ap.parse_args()
    e = est()
    f = e["fuente"]
    origen = "sim" if a.simulada else "auto"
    if f["estado"] != "conectada" or bool(f.get("simulada")) != a.simulada:
        api("/api/fuente/conectar", {"origen": origen})
        f = est()["fuente"]
    if bool(f.get("simulada")) != a.simulada or f["estado"] != "conectada":
        sys.exit("La fuente REAL no está conectada" if not a.simulada else "No se pudo conectar la simulada")
    if e.get("ensayo"):
        sys.exit("Hay un ensayo en marcha: termínalo antes")
    previas = {k: f["consignas"].get(k) for k in ("V_lim_V", "I_A", "P_W")}
    print("Consignas previas:", previas, flush=True)
    try:
        # 1. APAGAR durante el plan
        iniciar(a.proyecto, "seg1_apagar", [{"tipo": "consignas", "V": 3, "I": 0.5, "P": 5, "salida": "on"},
                                            {"tipo": "mantener", "duracion_s": 3},
                                            {"tipo": "consignas", "I": 0.4, "salida": "on"},
                                            {"tipo": "mantener", "duracion_s": 2}, {"tipo": "apagar"}])
        ok("1a el plan enciende", esperar(salida, 20))
        time.sleep(1)
        api("/api/fuente/salida", {"encender": False})
        ok("1b APAGAR deja el encendido bloqueado", est()["fuente"].get("bloqueo"), est()["fuente"].get("bloqueo"))
        ok("1c el plan se pausa al momento", esperar(lambda: plan_estado() == "pausado", 1.0))
        time.sleep(4)
        ok("1d la salida sigue APAGADA tras el fin de la etapa", not salida())
        c, d = api("/api/ensayo/plan/continuar", {}, error=True)
        ok("1e Continuar sin confirmar se rechaza", c == 409 and str(d).startswith("CONFIRMAR:"), d)
        api("/api/ensayo/plan/continuar", {"confirmado": True})
        ok("1f Continuar confirmado vuelve a encender", esperar(salida, 5))
        ok("1g el plan termina y apaga", esperar(lambda: not est().get("ensayo"), 20) and not salida())

        # 2. «local» durante una rampa rapida
        iniciar(a.proyecto, "seg2_local", [{"tipo": "consignas", "V": 3, "I": 0.1, "P": 5, "salida": "on"},
                                           {"tipo": "rampa", "magnitud": "I", "hasta": 0.6, "paso": 0.02,
                                            "duracion_s": 5},
                                           {"tipo": "consignas", "salida": "on"}, {"tipo": "apagar"}])
        # la rampa ya ha dado unos pasos (no vale la consigna que quedó de la prueba 1: hay que ver la etapa 2)
        def en_rampa():
            e = est()
            p = (e.get("ensayo") or {}).get("plan") or {}
            return p.get("estado") == "en marcha" and p.get("etapa") == 2 and                 0.2 <= (e["fuente"]["consignas"].get("I_A") or 0) <= 0.4
        ok("2a rampa en marcha", esperar(en_rampa, 25), est()["fuente"]["consignas"].get("I_A"))
        api("/api/fuente/local", {})
        ok("2b el plan se pausa", esperar(lambda: plan_estado() == "pausado", 2.0))
        i0 = est()["fuente"]["consignas"].get("I_A")
        time.sleep(3)
        i1 = est()["fuente"]["consignas"].get("I_A")
        ok("2c no escribe más consignas", i0 is not None and abs(i1 - i0) < 1e-6, f"{i0} -> {i1}")
        api("/api/ensayo/plan/parar", {})
        ok("2d PARAR apaga y termina", esperar(lambda: not est().get("ensayo"), 20) and not salida())

        # 3. desconectar con la salida ON
        api("/api/fuente/fijar", {"V_lim_V": 3, "I_A": 0.3, "P_W": 5})
        api("/api/fuente/salida", {"encender": True, "confirmado": True})
        ok("3a encendida a mano (con vigilante)", esperar(salida, 5) and est()["fuente"].get("vigilante_s"))
        c, d = api("/api/fuente/desconectar", {}, error=True)
        ok("3b desconectar con la salida ON se rechaza", c == 409, d)
        api("/api/fuente/salida", {"encender": False})
        ok("3c apagada", esperar(lambda: not salida(), 5))

        # 4. encender al iniciar sin confirmar
        c, d = api("/api/ensayo/iniciar", {"proyecto": a.proyecto, "carpeta": "_validacion_seguridad",
                                           "nombre": "seg4", "encender_al_iniciar": True, "temperaturas": False},
                   error=True)
        ok("4 «encender al iniciar» sin confirmar se rechaza", c == 409, d)
    finally:
        terminar_si_hay()
        api("/api/fuente/salida", {"encender": False}, error=True)
        if all(v is not None for v in previas.values()):
            api("/api/fuente/fijar", previas, error=True)
        print("Salida:", "ENCENDIDA (!)" if salida() else "apagada", flush=True)
    n = sum(1 for _, c, _ in RES if c)
    txt = f"Validación de seguridad {time.strftime('%Y-%m-%d %H:%M')}: {n} OK de {len(RES)}\n" + \
          "\n".join(("OK    " if c else "FALLO ") + p + (f"  [{d}]" if d else "") for p, c, d in RES)
    car = os.path.join(r"C:\Proyectos\Osciloscopio", a.proyecto, "ensayos", "_validacion_seguridad")
    os.makedirs(car, exist_ok=True)
    with open(os.path.join(car, time.strftime("validacion_seguridad_%Y%m%d_%H%M.txt")), "w", encoding="utf-8") as fh:
        fh.write(txt + "\n")
    print("\n" + txt.splitlines()[0])


if __name__ == "__main__":
    main()
