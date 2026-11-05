"""Arranque del servidor con el cDAQ, SIN equipos (fase 2A). Cada escenario en su propio proceso:

  antes        el código de la rama main (git archive a una carpeta temporal): rutas y claves de /api/estado
  desactivado  config.json tal cual (cdaq «habilitado»: false): arranca igual que antes, más la clave «cdaq»
  sin_chasis   cdaq habilitado con NI-DAQmx real y sin chasis: «no_disponible», Tomar no tumba nada
  simulado     cdaq habilitado y simulado: mide, historia y registro (cdaq_SIMULADO.csv) marcados como simulados

Nada toca los equipos ni los ficheros reales: osciloscopio sin sincronizar, registro de cambios, almacén y
config.json redirigidos a %TEMP%. El servidor de cada escenario escucha en 127.0.0.1 en un puerto libre.
Uso: python pruebas\\prueba_cdaq_servidor.py
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

PANEL = r"C:\Proyectos\_HERRAMIENTAS\Panel_laboratorio"


def ok(nombre, cond, detalle="", res=None):
    if res is not None:
        res.append((nombre, bool(cond)))
    print(("OK    " if cond else "FALLO ") + nombre + (f"  [{detalle}]" if detalle else ""), flush=True)


# ====================================================================== proceso hijo: un escenario
def hijo(escenario, codigo, salida):
    tmp = tempfile.mkdtemp(prefix=f"srv_{escenario}_")
    sys.path.insert(0, codigo)
    os.chdir(codigo)
    import registro
    registro.CARPETA = os.path.join(tmp, "registro")
    import almacen
    almacen.BASE = tmp
    os.makedirs(os.path.join(tmp, "PR99999-PRUEBA"), exist_ok=True)
    import osciloscopio
    osciloscopio.Osciloscopio.iniciar_sincronizacion = lambda self, *a, **k: None   # sin hablar con el MXO
    osciloscopio.Osciloscopio.limpiar_vistas = lambda self, *a, **k: None          # sin borrar temporales reales
    _load = json.load

    def load(f, *a, **k):
        d = _load(f, *a, **k)
        if str(getattr(f, "name", "")).endswith("config.json") and escenario in ("sin_chasis", "simulado"):
            d["cdaq"]["habilitado"] = True
            d["cdaq"]["simulado"] = escenario == "simulado"
            d["cdaq"]["periodo_s"] = 0.2
            d["cdaq"]["tensiones"]["canales"]["0"]["activo"] = True
        return d
    json.load = load
    import servidor as S
    json.load = _load
    S.CONFIG = os.path.join(tmp, "config.json")         # guardar_cfg escribe aquí, nunca en el config.json real
    import uvicorn
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    puerto = s.getsockname()[1]
    s.close()
    srv = uvicorn.Server(uvicorn.Config(S.app, host="127.0.0.1", port=puerto, log_level="warning"))
    threading.Thread(target=srv.run, daemon=True).start()
    url = f"http://127.0.0.1:{puerto}"

    def pedir(ruta, cuerpo=None):
        req = urllib.request.Request(url + ruta, data=None if cuerpo is None else json.dumps(cuerpo).encode(),
                                     headers={"Content-Type": "application/json", "X-Autor": "claude"},
                                     method="GET" if cuerpo is None else "POST")
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    fin = time.time() + 20
    while time.time() < fin:
        try:
            pedir("/api/estado")
            break
        except Exception:
            time.sleep(0.2)
    out = {"rutas": sorted(f"{','.join(sorted(r.methods or []))} {r.path}" for r in S.app.routes
                           if hasattr(r, "methods")),
           "pasos": {}}
    st, e = pedir("/api/estado")
    out["estado_http"] = st
    out["claves_estado"] = sorted(e)
    out["tipos_estado"] = {k: type(v).__name__ for k, v in e.items()}
    out["claves_secciones"] = {k: sorted(v) for k, v in e.items() if isinstance(v, dict)}
    out["cdaq_inicial"] = e.get("cdaq", {}).get("estado")
    out["config_real_intacta"] = True
    if escenario == "sin_chasis":
        fin = time.time() + 10
        while time.time() < fin and pedir("/api/estado")[1]["cdaq"]["estado"] not in ("no_disponible", "error"):
            time.sleep(0.2)
        out["cdaq_detectado"] = pedir("/api/estado")[1]["cdaq"]["estado"]
        out["pasos"]["tomar"] = pedir("/api/cdaq/tomar", {})
        out["pasos"]["estado_tras_tomar"] = pedir("/api/estado")
        out["pasos"]["soltar"] = pedir("/api/cdaq/soltar", {})
        out["pasos"]["sin_autor"] = urllib.request.Request(url + "/api/cdaq/tomar", data=b"{}", method="POST")
        try:
            urllib.request.urlopen(out["pasos"]["sin_autor"], timeout=5)
            out["pasos"]["sin_autor"] = 200
        except urllib.error.HTTPError as x:
            out["pasos"]["sin_autor"] = x.code
    if escenario == "simulado":
        out["pasos"]["tomar"] = pedir("/api/cdaq/tomar", {})
        time.sleep(1.0)
        out["pasos"]["historia"] = pedir("/api/cdaq/historia?desde=0")
        out["pasos"]["config_mal"] = pedir("/api/cdaq/configurar", {"termopares": {"canales": {"0": {"tipo": "Z"}}}})
        out["pasos"]["reg_ini"] = pedir("/api/cdaq/registro/iniciar", {"proyecto": "PR99999-PRUEBA", "nombre": "prueba_cdaq",
                                                                        "pre_s": 5})
        time.sleep(1.0)
        out["pasos"]["estado_grabando"] = pedir("/api/estado")[1]["cdaq"]["grabando"]
        out["pasos"]["soltar_grabando"] = pedir("/api/cdaq/soltar", {})
        out["pasos"]["reg_fin"] = pedir("/api/cdaq/registro/parar", {})
        rel = out["pasos"]["reg_ini"][1].get("ruta", "")
        carpeta = os.path.join(tmp, "PR99999-PRUEBA", "registros", rel) if rel and not os.path.isabs(rel) else rel
        cand = [os.path.join(r, f) for r, _, fs in os.walk(tmp) for f in fs if f.startswith("cdaq")]
        out["ficheros"] = cand
        out["csv"] = open(cand[0], encoding="utf-8").read()[:3000] if cand else ""
        js = [os.path.join(r, f) for r, _, fs in os.walk(tmp) for f in fs if f == "registro.json"]
        out["registro_json"] = json.load(open(js[0], encoding="utf-8")) if js else None
        out["pasos"]["soltar"] = pedir("/api/cdaq/soltar", {})
        out["config_guardada"] = json.load(open(S.CONFIG, encoding="utf-8")).get("cdaq", {}).get("en_uso") \
            if os.path.exists(S.CONFIG) else None
    try:
        S.daq.cerrar()
    except Exception:
        pass
    with open(salida, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, default=str)
    sys.stdout.flush()
    os._exit(0)                       # sin el «shutdown» del panel (no tocar fuente ni osciloscopio)


# ====================================================================== proceso padre
def lanzar(escenario, codigo, tmp):
    salida = os.path.join(tmp, f"{escenario}.json")
    r = subprocess.run([sys.executable, "-u", os.path.abspath(__file__), "--hijo", escenario, codigo, salida],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
    if not os.path.exists(salida):
        print(r.stdout[-2000:], r.stderr[-3000:])
        return None
    return json.load(open(salida, encoding="utf-8"))


def main():
    res = []
    tmp = tempfile.mkdtemp(prefix="cdaq_srv_")
    cfg_real = open(os.path.join(PANEL, "config.json"), "rb").read()
    antes_dir = os.path.join(tmp, "main")
    os.makedirs(antes_dir)
    a = subprocess.run(f'git -C "{PANEL}" archive main | tar -x -C "{antes_dir}"', shell=True, capture_output=True)
    if a.returncode:
        print("OMITIDA comparación con main: no se pudo extraer (git archive)", a.stderr[-200:], flush=True)
        antes = None
    else:
        antes = lanzar("antes", antes_dir, tmp)
        ok("main: el servidor de antes arranca", antes and antes["estado_http"] == 200, res=res)
    des = lanzar("desactivado", PANEL, tmp)
    ok("desactivado: el servidor arranca y /api/estado responde 200", des and des["estado_http"] == 200, res=res)
    if des:
        ok("desactivado: el cDAQ sale «desactivado»", des["cdaq_inicial"] == "desactivado", des["cdaq_inicial"], res)
        if antes:
            nuevas = sorted(set(des["rutas"]) - set(antes["rutas"]))
            ok("igual que antes: todas las rutas de main siguen", set(antes["rutas"]) <= set(des["rutas"]), res=res)
            ok("igual que antes: solo se añaden rutas /api/cdaq", all("/api/cdaq/" in r for r in nuevas), str(nuevas), res)
            ok("igual que antes: /api/estado = las mismas claves + «cdaq»",
               set(des["claves_estado"]) == set(antes["claves_estado"]) | {"cdaq"}, str(des["claves_estado"]), res)
            iguales = all(des["claves_secciones"].get(k) == v for k, v in antes["claves_secciones"].items())
            ok("igual que antes: cada sección de /api/estado con las mismas claves", iguales, res=res)
    sin = lanzar("sin_chasis", PANEL, tmp)
    ok("sin chasis (habilitado): el servidor arranca", sin and sin["estado_http"] == 200, res=res)
    if sin:
        ok("sin chasis: el cDAQ aparece «no_disponible»", sin["cdaq_detectado"] == "no_disponible", sin["cdaq_detectado"], res)
        st, r = sin["pasos"]["tomar"]
        ok("sin chasis: Tomar responde (no tumba nada) y dice no disponible", st == 200 and r["estado"] == "no_disponible",
           f"{st} {r.get('estado')} {r.get('mensaje', '')[:60]}", res)
        st2, e2 = sin["pasos"]["estado_tras_tomar"]
        ok("sin chasis: el resto del panel sigue respondiendo", st2 == 200 and "fuente" in e2 and "osciloscopio" in e2, res=res)
        ok("sin chasis: Soltar deja «libre»", sin["pasos"]["soltar"][0] == 200 and sin["pasos"]["soltar"][1]["estado"] == "libre",
           res=res)
        ok("sin chasis: POST sin X-Autor rechazado (403)", sin["pasos"]["sin_autor"] == 403, str(sin["pasos"]["sin_autor"]), res)
    sim = lanzar("simulado", PANEL, tmp)
    ok("simulado: el servidor arranca", sim and sim["estado_http"] == 200, res=res)
    if sim:
        st, r = sim["pasos"]["tomar"]
        ok("simulado: Tomar -> midiendo, marcado simulado", st == 200 and r["estado"] == "midiendo" and r["simulado"],
           f"{st} {r.get('estado')}", res)
        st, h = sim["pasos"]["historia"]
        ok("simulado: historia con datos y marcada", st == 200 and h["simulado"] and any(h["datos"].values())
           and all(c["nombre"].startswith("SIM_") for c in h["canales"]), res=res)
        ok("simulado: configurar con tipo Z -> 409", sim["pasos"]["config_mal"][0] == 409, str(sim["pasos"]["config_mal"]), res)
        st, r = sim["pasos"]["reg_ini"]
        ok("simulado: registro iniciado", st == 200 and r.get("simulado") is True, str(r), res)
        ok("simulado: /api/estado dice que graba", bool(sim["pasos"]["estado_grabando"]), res=res)
        ok("simulado: no deja soltar mientras graba (409)", sim["pasos"]["soltar_grabando"][0] == 409, res=res)
        ok("simulado: registro parado", sim["pasos"]["reg_fin"][0] == 200, res=res)
        ok("simulado: fichero cdaq_SIMULADO.csv", any(f.endswith("cdaq_SIMULADO.csv") for f in sim["ficheros"]),
           str([os.path.basename(f) for f in sim["ficheros"]]), res)
        ok("simulado: el CSV avisa en la primera línea", sim["csv"].startswith("# *** DATOS SIMULADOS"), res=res)
        filas = [l for l in sim["csv"].splitlines() if l and not l.startswith("#")]
        ok("simulado: CSV con cabecera y filas", len(filas) >= 3 and filas[0].startswith("fecha_hora,t_unix_s,t_rel_s,SIM_T0_C"),
           filas[0] if filas else "", res)
        rj = sim["registro_json"] or {}
        ok("simulado: registro.json tipo cdaq, simulado y no probado con hardware",
           rj.get("tipo") == "cdaq" and rj.get("cdaq", {}).get("simulado") is True
           and rj.get("cdaq", {}).get("probado_con_hardware") is False, res=res)
        ok("simulado: la configuración se guarda en la copia temporal", sim["config_guardada"] is False,
           str(sim["config_guardada"]), res)
    ok("config.json real intacto", open(os.path.join(PANEL, "config.json"), "rb").read() == cfg_real, res=res)
    f = [r for r in res if not r[1]]
    print(f"\n{len(res) - len(f)} OK, {len(f)} FALLO  (temporal: {tmp})", flush=True)
    return 1 if f else 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--hijo":
        hijo(*sys.argv[2:5])
    else:
        sys.exit(main())
