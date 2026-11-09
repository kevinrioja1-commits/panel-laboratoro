r"""Lanza TODAS las pruebas pendientes de validar (registro: pendientes.json) con los equipos que hay conectados.

Lo que el usuario dice al llegar: «haz las pruebas pendientes, está conectado» = osciloscopio, fuente y temperaturas
(TC-08 o PicoLog), salvo que diga lo contrario («sin PicoLog» → --sin temp).

    python lanzar_pendientes.py                     todo conectado; las que encienden la salida NO se lanzan
    python lanzar_pendientes.py --sin temp          sin temperaturas (también: --sin picolog, --sin tc08)
    python lanzar_pendientes.py --sin osc --sin temp
    python lanzar_pendientes.py --con-salida        incluye las que ENCIENDEN la fuente (solo con su permiso expreso)
    python lanzar_pendientes.py --lista             solo dice qué se lanzaría y qué no (no lanza nada)
    python lanzar_pendientes.py --solo rediseno_v4  solo esa prueba (se puede repetir)
    opciones: --url http://127.0.0.1:8765

Qué hace:
  1. Comprueba que el panel responde y qué equipos ve de verdad. Si dices que algo está conectado y el panel no lo ve,
     lo dice (si es la fuente, intenta conectarla: conectar no enciende nada).
  2. Por cada prueba pendiente: si le falta un equipo que NECESITA, no se lanza (y dice cuál); si le falta uno que
     APROVECHA, se lanza y esas partes salen OMITIDAS. Las de tipo «regresion» (sin equipos) se lanzan siempre.
  3. Resumen por prueba (OK / FALLO / AVISO / OMITIDA), informe en pruebas\informes\pendientes_AAAAMMDD_HHMM.txt y
     estado actualizado en pendientes.json (validada = sin fallos y con todos sus equipos).
  4. Al final, las pruebas que necesitan la mano del usuario (tipo «manual»), con qué hacer.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

AQUI = os.path.dirname(os.path.abspath(__file__))
REGISTRO = os.path.join(AQUI, "pendientes.json")
NOMBRE = {"osc": "osciloscopio", "fuente": "fuente", "temp": "temperaturas (TC-08 / PicoLog)",
          "cdaq": "cDAQ-9178 (NI 9213 + NI 9201)",   # 9-nov: el cDAQ es un equipo aparte
          "siglent": "Siglent SPD3303X (segunda fuente)"}
ALIAS = {"osc": "osc", "osciloscopio": "osc", "mxo": "osc", "fuente": "fuente", "fte": "fuente", "ea": "fuente",
         "temp": "temp", "temperaturas": "temp", "picolog": "temp", "tc08": "temp", "tc-08": "temp", "termopares": "temp",
         "cdaq": "cdaq", "ni": "cdaq", "9213": "cdaq", "9201": "cdaq",
         "siglent": "siglent", "spd": "siglent", "spd3303x": "siglent"}
LINEA = re.compile(r"^\s*\[?(OK|FALLO|AVISO|OMITIDA)\s*\]?\s+(.*)$")   # «OK    x» y «  [OK   ] x»


def api(url, ruta, datos=None, timeout=20):
    req = urllib.request.Request(url + ruta, headers={"X-Autor": "claude", "Content-Type": "application/json"},
                                 data=None if datos is None else json.dumps(datos).encode())
    with urllib.request.urlopen(req, timeout=timeout) as r:
        c = r.read()
        return json.loads(c) if c else None


def ve_el_panel(E):
    """Qué equipos ve el panel de verdad, y por qué no si no."""
    o, f, t = E.get("osciloscopio") or {}, E.get("fuente") or {}, E.get("temperaturas") or {}
    pl, tc = E.get("picolog") or {}, E.get("tc08") or {}
    ve = {
        "osc": (o.get("modo") not in (None, "error", "desconectado") and bool(o.get("estado")),
                f"modo {o.get('modo')}" + (f" ({o.get('error')})" if o.get("error") else "")),
        "fuente": (f.get("estado") == "conectada", f"estado {f.get('estado')}" + (f" · {f.get('origen')}" if f.get("origen") else "")),
        "temp": (t.get("fuente") == "tc08" or pl.get("estado") == "capturando",
                 f"TC-08 {tc.get('estado')} · PicoLog {pl.get('estado')}"),
        "cdaq": ((E.get("cdaq") or {}).get("estado") in ("libre", "midiendo", "conectando"),
                 f"estado {(E.get('cdaq') or {}).get('estado')}"),
        # la Siglent: basta con que el panel la tenga configurada (sus pruebas la conectan ellas mismas)
        "siglent": ((E.get("siglent") or {}).get("estado") in ("conectada", "desconectada"),
                    f"estado {(E.get('siglent') or {}).get('estado')}"),
    }
    if "BANCO" in str(o.get("idn", "")):
        ve["osc"] = (True, "banco sin equipos")
    # 9-nov: antes buscaba «SIMUL» en todo el JSON y saltaba con "simulada": false
    if str(f.get("origen", "")).lower().startswith("sim") or f.get("simulada") is True \
            or "SIMUL" in str(f.get("idn", "")).upper():
        ve["fuente"] = (ve["fuente"][0], ve["fuente"][1] + " · ¡SIMULADA!")
    return ve


def lanzar(p, url):
    cmd = [sys.executable, "-u", os.path.join(AQUI, p["comando"][0])] + p["comando"][1:]
    if p["comando"][0] == "validar_rediseno.py" and url != "http://127.0.0.1:8765":
        cmd += ["--url", url]
    t0 = time.time()
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=AQUI)
    lineas = []
    for l in r.stdout.splitlines():
        m = LINEA.match(l)
        if m:
            lineas.append((m.group(1), m.group(2)))
    cuenta = {k: sum(1 for e, _ in lineas if e == k) for k in ("OK", "FALLO", "AVISO", "OMITIDA")}
    if r.returncode not in (0, None) and not cuenta["FALLO"]:
        motivo = (r.stderr.strip() or r.stdout.strip()).splitlines()[-1:] or ["sin salida"]
        lineas.append(("FALLO", f"terminó con código {r.returncode}: {motivo[0][:300]}"))
        cuenta["FALLO"] += 1
    return {"cuenta": cuenta, "lineas": lineas, "segundos": round(time.time() - t0), "salida": r.stdout[-4000:],
            "error": r.stderr[-2000:]}


def main():
    ap = argparse.ArgumentParser(description="Lanza las pruebas pendientes con los equipos conectados")
    ap.add_argument("--sin", action="append", default=[], help="equipo NO conectado: osc, fuente, temp (picolog, tc08)")
    ap.add_argument("--con-salida", action="store_true", help="incluye las que ENCIENDEN la fuente")
    ap.add_argument("--lista", action="store_true")
    ap.add_argument("--solo", action="append", default=[])
    ap.add_argument("--url", default="http://127.0.0.1:8765")
    a = ap.parse_args()
    url = a.url.rstrip("/")
    sin = set()
    for s in a.sin:
        for x in s.replace(",", " ").split():
            if x.lower() not in ALIAS:
                sys.exit(f"--sin {x}: no sé qué equipo es (osc, fuente, temp/picolog/tc08, cdaq)")
            sin.add(ALIAS[x.lower()])
    dice = {k: k not in sin for k in NOMBRE}

    reg = json.load(open(REGISTRO, encoding="utf-8"))
    pend = [p for p in reg["pruebas"] if p.get("estado") != "validada" and (not a.solo or p["id"] in a.solo)]

    # 1 · el panel y los equipos
    try:
        E = api(url, "/api/estado")
    except Exception as e:
        if not a.lista:
            sys.exit(f"El panel no responde en {url} ({e}). Arráncalo (arrancar_panel.bat) y repite.")
        print("(El panel no está arrancado: la lista supone que el panel ve lo que dices)", flush=True)
        E = {"osciloscopio": {"modo": "conectado", "estado": {"x": 1}}, "fuente": {"estado": "conectada"},
             "temperaturas": {"fuente": "tc08"}}
    if E.get("ensayo"):
        sys.exit("Hay un ensayo en marcha: termínalo antes de lanzar las pruebas")
    if ((E.get("osciloscopio") or {}).get("datalogger") or {}).get("en_marcha"):
        sys.exit("Hay un registro del osciloscopio en marcha: páralo antes")
    if dice["fuente"] and (E.get("fuente") or {}).get("estado") != "conectada" and not a.lista:
        print("La fuente no está conectada en el panel: la conecto (conectar NO enciende la salida)…", flush=True)
        try:
            api(url, "/api/fuente/conectar", {"origen": "auto"}, timeout=30)
            time.sleep(1.5)
            E = api(url, "/api/estado")
        except Exception as e:
            print(f"  no se pudo: {e}", flush=True)
    ve = ve_el_panel(E)
    # solo cuenta como validación el panel de verdad con equipos de verdad (no el banco ni la fuente simulada)
    real = url == "http://127.0.0.1:8765" and "BANCO" not in str((E.get("osciloscopio") or {}).get("idn", ""))
    if not real:
        print("(Banco / otra URL: las pruebas se lanzan pero NO cambian el estado de pendientes.json)", flush=True)
    hay = {}
    print("\nEQUIPOS", flush=True)
    for k, n in NOMBRE.items():
        v, por = ve[k]
        hay[k] = dice[k] and v
        nota = "conectado" if hay[k] else ("dices que NO está" if not dice[k] else f"dices que está, pero el panel NO lo ve ({por})")
        print(f"  {n:34} {nota}" + (f"  [{por}]" if hay[k] else ""), flush=True)
    if (E.get("osciloscopio") or {}).get("modo") == "manual":
        hay["osc"] = False
        print("  ¡El panel está en «Modo manual»! Las pruebas del osciloscopio no se lanzan: vuelve a modo panel.", flush=True)

    # 2 · qué se lanza
    plan, manuales = [], []
    print("\nPRUEBAS PENDIENTES", flush=True)
    for p in pend:
        falta = [NOMBRE[k] for k in p.get("necesita", []) if not hay[k]]
        parcial = [NOMBRE[k] for k in p.get("aprovecha", []) if not hay[k]]
        if p["tipo"] == "manual":
            manuales.append((p, falta))
            continue
        if falta:
            print(f"  ✗ {p['id']}: NO se lanza, necesita {', '.join(falta)}", flush=True)
            continue
        if p.get("enciende_salida") and not a.con_salida:
            print(f"  ⏸ {p['id']}: ENCIENDE la fuente → espera tu permiso (--con-salida)", flush=True)
            continue
        print(f"  ▶ {p['id']}: se lanza" + (f" (sin {', '.join(parcial)}: esas partes saldrán OMITIDAS)" if parcial else ""), flush=True)
        plan.append((p, parcial))
    for p, falta in manuales:
        print(f"  ✋ {p['id']}: necesita tu mano" + (f" y {', '.join(falta)}" if falta else ""), flush=True)
    if a.lista:
        return

    # 3 · lanzar (las que encienden la salida, al final)
    plan.sort(key=lambda x: bool(x[0].get("enciende_salida")))
    resumen = []
    for p, parcial in plan:
        print(f"\n===== {p['id']} · {p['titulo']}", flush=True)
        if p.get("montaje"):
            print(f"      montaje: {p['montaje']}", flush=True)
        r = lanzar(p, url)
        for e, t in r["lineas"]:
            if e != "OK":
                print(f"  {e:8}{t}", flush=True)
        c = r["cuenta"]
        print(f"  → {c['OK']} OK · {c['FALLO']} FALLO · {c['AVISO']} AVISO · {c['OMITIDA']} OMITIDA ({r['segundos']} s)", flush=True)
        if real and p["tipo"] != "regresion" and c["FALLO"] == 0 and c["OK"] > 0:
            p["estado"] = "validada" if not parcial and not c["OMITIDA"] else "validada_parcial"
        if real:
            p.setdefault("historial", []).append({"fecha": time.strftime("%Y-%m-%d %H:%M"), "equipos": [k for k in hay if hay[k]],
                                                  **c, "fallos": [t for e, t in r["lineas"] if e == "FALLO"][:20]})
        resumen.append((p, c, r))

    # 4 · informe y registro
    if real:
        tmp = REGISTRO + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(reg, ensure_ascii=False, indent=2) + "\n")
        os.replace(tmp, REGISTRO)
    os.makedirs(os.path.join(AQUI, "informes"), exist_ok=True)
    fi = os.path.join(AQUI, "informes", time.strftime("pendientes_%Y%m%d_%H%M.txt"))
    with open(fi, "w", encoding="utf-8") as f:
        f.write(f"Pruebas pendientes {time.strftime('%Y-%m-%d %H:%M')} · equipos: "
                f"{', '.join(NOMBRE[k] for k in hay if hay[k]) or 'ninguno'}\n\n")
        for p, c, r in resumen:
            f.write(f"== {p['id']} → {p['estado']} · {c['OK']} OK · {c['FALLO']} FALLO · {c['AVISO']} AVISO · {c['OMITIDA']} OMITIDA\n")
            f.write("\n".join(f"{e:8}{t}" for e, t in r["lineas"]) + "\n")
            if r["error"].strip():
                f.write("--- stderr (final)\n" + r["error"][-1500:] + "\n")
            f.write("\n")
    print("\nRESUMEN", flush=True)
    for p, c, r in resumen:
        print(f"  {'✔' if not c['FALLO'] else '✘'} {p['id']:30} {p['estado']:17} {c['OK']} OK · {c['FALLO']} FALLO · "
              f"{c['AVISO']} AVISO · {c['OMITIDA']} OMITIDA", flush=True)
        if p.get("despues") and not c["FALLO"]:
            print(f"      después: {p['despues']}", flush=True)
    if manuales:
        print("\nNECESITAN TU MANO (avísame cuando las hagas)", flush=True)
        for p, falta in manuales:
            print(f"  ✋ {p['id']}: {p['titulo']}" + (f"  [necesita {', '.join(falta)}]" if falta else ""), flush=True)
            print(f"     {p['instrucciones']}", flush=True)
    print(f"\nInforme: {fi}", flush=True)
    sys.exit(1 if any(c["FALLO"] for _, c, _ in resumen) else 0)


if __name__ == "__main__":
    main()
