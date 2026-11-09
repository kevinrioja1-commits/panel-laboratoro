"""Ensayo programado (fase 5, propuesta aprobada por el usuario el 2026-11-03).

Es el mismo ensayo de la fase 4 (ensayo.py: misma carpeta y ficheros), pero el panel sigue una lista de etapas en vez
de que el usuario pulse. Sin escalones ni interpretacion: cada etapa queda como evento en fuente.csv y el plan entero,
con lo que paso en cada etapa, en ensayo.json.

Plan (JSON, reutilizable en <proyecto>\\ensayos\\_planes\\<nombre>.json):
  {"nombre": "...", "descripcion": "...",
   "maximos": {"V": 5, "I": 1.2, "P": 6},          techos del plan (no pueden pasar los topes del panel)
   "etapas": [
     {"tipo": "pausa", "mensaje": "Coloca los termopares y pulsa Continuar"},
     {"tipo": "consignas", "V": 3, "I": 0.1, "P": 5, "salida": "on"},    V/I/P opcionales; salida on | off | (nada)
     {"tipo": "rampa", "magnitud": "I", "hasta": 1.0, "paso": 0.1, "duracion_s": 600},   desde la consigna actual
     {"tipo": "mantener", "duracion_s": 300},          sin duracion_s: hasta pulsar Siguiente
     {"tipo": "captura", "nombre": "pico"},           captura del osciloscopio a capturas\\ del ensayo
     {"tipo": "marca", "texto": "..."},
     {"tipo": "apagar"}]}

Fase 4 (9-nov-2026, propuesta de coherencia aprobada): varias fuentes y condiciones.
  - Cada etapa de consignas, rampa o apagar dice a qué salida va con «fuente»: "ea" (por defecto, la de siempre),
    "spd.CH1", "spd.CH2" o "spd.CH3" (Siglent SPD3303X; CH3 solo on/off). {"tipo": "apagar", "fuente": "todo"}
    apaga todas las salidas de todas las fuentes.
  - "maximos_spd": {"V": 5, "I": 0.5}: techos del plan para la Siglent (obligatorio si la usa).
  - {"tipo": "esperar", "condicion": "cdaq.T0 >= 60", "max_s": 600, "si_no": "parar" | "seguir"}: espera a que una
    medida cumpla la condición (sin max_s: sin límite).
  - "guardas": ["cdaq.T0 >= 80", "ea.I > 2"]: si alguna se cumple en cualquier momento, se APAGA TODO y se para.
  Medidas: ea.V, ea.I, ea.P · spd.CH1.V, spd.CH1.I, spd.CH2.V, spd.CH2.I · cdaq.<canal> (p. ej. cdaq.T0, cdaq.V0) ·
  tc.<canal> (PicoLog / TC-08, p. ej. tc.TC1). Operadores: >=, <=, >, <.

Seguridad: el plan se comprueba entero antes de empezar (consignas frente a los maximos del plan, los maximos frente a
los topes del panel y la zona de trabajo de la fuente). Mientras corre, cada 0,2 s: sin comunicacion con la fuente ->
apaga y para; consignas o salida distintas de las del plan (cambio a mano, proteccion, vigilante) -> pausa con aviso
(Continuar vuelve a aplicar lo del plan). PARAR apaga la salida y termina el ensayo.
"""
import copy
import json
import os
import re
import threading
import time

import operator

import almacen
import siglent_modelo as SM
from fuente import ControlPerdido, EncendidoBloqueado

TIPOS = ("pausa", "consignas", "rampa", "mantener", "captura", "marca", "apagar", "esperar")
FUENTES = ("ea", "spd.CH1", "spd.CH2", "spd.CH3")
OPS = {">=": operator.ge, "<=": operator.le, ">": operator.gt, "<": operator.lt}
_COND = re.compile(r"^\s*([A-Za-z][\w.\-]*)\s*(>=|<=|>|<)\s*(-?\d+(?:[.,]\d+)?)\s*$")
MAGNITUDES = {"V": "V_lim_V", "I": "I_A", "P": "P_W"}     # nombre en el plan -> clave de fuente.consignas
MAX_PASOS_RAMPA = 1000
CADA_S = 0.2                 # vigilancia del plan mientras espera
GRACIA_S = 1.5               # tras una orden del plan, tiempo para que la lectura de la fuente la refleje


def _num(v, que):
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise ValueError(f"{que}: «{v}» no es un número")
    if x != x or x < 0:
        raise ValueError(f"{que}: no puede ser negativo")
    return x


def condicion(txt):
    """«cdaq.T0 >= 60» -> (medida, op, valor); ValueError si no se entiende."""
    m = _COND.match(str(txt or ""))
    if not m:
        raise ValueError(f"condición «{txt}» no válida (p. ej. «cdaq.T0 >= 60»: medida, >= <= > <, número)")
    return m.group(1), m.group(2), float(m.group(3).replace(",", "."))


def _fuente(e):
    return e.get("fuente") or "ea"


def _nombre_fuente(f):
    return {"ea": "", "todo": "todas las fuentes: "}.get(f, f"Siglent {f[4:]}: ")


def describir(e):
    """Texto corto de una etapa (eventos de fuente.csv, resumen y pagina)."""
    t = e.get("tipo")
    f = _fuente(e)
    if t == "esperar":
        lim = f" (máx. {_hms(e['max_s'])}; si no, {'sigue' if e.get('si_no') == 'seguir' else 'PARA'})" if e.get("max_s") else ""
        return f"esperar hasta {e['condicion']}{lim}"
    if f != "ea" and t in ("consignas", "rampa", "apagar"):
        if t == "apagar":
            return "apagar " + ("TODAS las salidas" if f == "todo" else f"Siglent {f[4:]}")
        pre = _nombre_fuente(f)
        if t == "consignas":
            partes = [f"{m} {e[m]:g} {u}" for m, u in (("V", "V"), ("I", "A")) if e.get(m) is not None]
            if e.get("salida") in ("on", "off"):
                partes.append(f"salida {e['salida'].upper()}")
            return pre + "consignas: " + ", ".join(partes)
        u = {"V": "V", "I": "A"}[e["magnitud"]]
        return (pre + f"rampa {e['magnitud']} hasta {e['hasta']:g} {u} en pasos de {e['paso']:g} {u}"
                f" durante {_hms(e['duracion_s'])}")
    if t == "pausa":
        return f"pausa: {e.get('mensaje') or 'pulsa Continuar'}"
    if t == "consignas":
        partes = [f"{m} {e[m]:g} {u}" for m, u in (("V", "V"), ("I", "A"), ("P", "W")) if e.get(m) is not None]
        if e.get("salida") in ("on", "off"):
            partes.append(f"salida {e['salida'].upper()}")
        return "consignas: " + ", ".join(partes)
    if t == "rampa":
        u = {"V": "V", "I": "A", "P": "W"}[e["magnitud"]]
        return (f"rampa {e['magnitud']} hasta {e['hasta']:g} {u} en pasos de {e['paso']:g} {u}"
                f" durante {_hms(e['duracion_s'])}")
    if t == "mantener":
        return f"mantener {_hms(e['duracion_s'])}" if e.get("duracion_s") else "mantener hasta pulsar Siguiente"
    if t == "captura":
        return f"captura del osciloscopio «{e['nombre']}»"
    if t == "marca":
        return f"marca: {e['texto']}"
    return "apagar la salida"


def _hms(s):
    s = int(round(s))
    return f"{s // 3600} h {s % 3600 // 60:02d} min" if s >= 3600 else (f"{s // 60} min {s % 60:02d} s" if s >= 60 else f"{s} s")


def _comprobar_ea(plan, topes, equipo, consignas_actuales, con_osc_registrando=False):
    """Valida y normaliza el plan. Devuelve (plan_normalizado, resumen) o lanza ValueError con TODOS los problemas.

    topes: limites del panel {vmax_V, imax_A, pmax_W}; equipo: nominales de la fuente {v_nom, i_nom, p_nom};
    consignas_actuales: las de la fuente ahora (punto de partida de la primera rampa si no hay consignas antes)."""
    errores = []
    if not isinstance(plan, dict):
        raise ValueError("El plan no es válido")
    p = copy.deepcopy(plan)
    mx = p.get("maximos") or {}
    tope = {"V": topes.get("vmax_V"), "I": topes.get("imax_A"), "P": topes.get("pmax_W")}
    nominal = {"V": equipo.get("v_nom"), "I": equipo.get("i_nom"), "P": equipo.get("p_nom")}
    maximos = {}
    for m, u in (("V", "V"), ("I", "A"), ("P", "W")):
        if mx.get(m) in (None, ""):
            errores.append(f"Falta el máximo de {m} del plan")
            continue
        try:
            maximos[m] = _num(mx[m], f"Máximo de {m}")
        except ValueError as e:
            errores.append(str(e))
            continue
        if tope[m] is not None and maximos[m] > tope[m]:
            errores.append(f"El máximo de {m} del plan ({maximos[m]:g} {u}) pasa el tope del panel ({tope[m]:g} {u})")
        if nominal[m] is not None and maximos[m] > nominal[m]:
            errores.append(f"El máximo de {m} del plan ({maximos[m]:g} {u}) pasa lo que da la fuente ({nominal[m]:g} {u})")
    p["maximos"] = maximos
    etapas = p.get("etapas") or []
    if not etapas:
        errores.append("El plan no tiene etapas")
    # se recorre el plan simulando las consignas para comprobar cada valor y estimar la duracion
    k = {m: consignas_actuales.get(c) for m, c in MAGNITUDES.items()}
    salida = bool(consignas_actuales.get("salida"))
    duracion, abierta, enciende = 0.0, False, False
    alcanzado = {"V": 0.0, "I": 0.0, "P": 0.0}
    fijadas, avisadas = set(), set()        # magnitudes que fija el plan; avisos ya dados de las que no fija
    norm = []
    for n, e in enumerate(etapas, 1):
        donde = f"Etapa {n}"
        if not isinstance(e, dict) or e.get("tipo") not in TIPOS:
            errores.append(f"{donde}: tipo de etapa no válido ({e.get('tipo') if isinstance(e, dict) else e})")
            continue
        e = {kk: v for kk, v in e.items() if v not in (None, "")}
        t = e["tipo"]
        try:
            if t == "consignas":
                for m in ("V", "I", "P"):
                    if m in e:
                        e[m] = _num(e[m], f"{donde}, {m}")
                if e.get("salida") not in (None, "on", "off"):
                    raise ValueError(f"{donde}: salida tiene que ser on, off o nada")
                if not any(m in e for m in ("V", "I", "P")) and "salida" not in e:
                    raise ValueError(f"{donde}: no fija nada")
                k.update({m: e[m] for m in ("V", "I", "P") if m in e})
                if e.get("salida") == "on":
                    salida, enciende = True, True
                elif e.get("salida") == "off":
                    salida = False
            elif t == "rampa":
                if e.get("magnitud") not in MAGNITUDES:
                    raise ValueError(f"{donde}: la rampa tiene que ser de V, I o P")
                m = e["magnitud"]
                e["hasta"] = _num(e.get("hasta"), f"{donde}, hasta")
                e["paso"] = _num(e.get("paso"), f"{donde}, paso")
                e["duracion_s"] = _num(e.get("duracion_s"), f"{donde}, duración")
                if e["paso"] <= 0:
                    raise ValueError(f"{donde}: el paso tiene que ser mayor que 0")
                if k[m] is None:
                    raise ValueError(f"{donde}: no se sabe desde dónde empieza la rampa (fija antes {m})")
                pasos = int(abs(e["hasta"] - k[m]) / e["paso"] + 0.999999)
                if pasos > MAX_PASOS_RAMPA:
                    raise ValueError(f"{donde}: {pasos} pasos son demasiados (máximo {MAX_PASOS_RAMPA}): sube el paso")
                if pasos == 0:
                    raise ValueError(f"{donde}: la rampa no se mueve (ya está en {e['hasta']:g})")
                e["desde"] = k[m]          # informativo: lo que se espera al llegar a esta etapa
                duracion += e["duracion_s"]
                k[m] = e["hasta"]
            elif t == "mantener":
                if "duracion_s" in e:
                    e["duracion_s"] = _num(e["duracion_s"], f"{donde}, duración")
                    duracion += e["duracion_s"]
                else:
                    abierta = True
            elif t == "pausa":
                abierta = True
                e["mensaje"] = str(e.get("mensaje") or "Pulsa Continuar")
            elif t == "captura":
                e["nombre"] = almacen._limpio(e.get("nombre") or "captura")
                if con_osc_registrando:
                    raise ValueError(f"{donde}: no se puede capturar mientras el osciloscopio registra en el ensayo "
                                     "(quita el osciloscopio del ensayo o la etapa de captura)")
            elif t == "marca":
                e["texto"] = str(e.get("texto") or "").strip()
                if not e["texto"]:
                    raise ValueError(f"{donde}: la marca no tiene texto")
            elif t == "apagar":
                salida = False
        except ValueError as err:
            errores.append(str(err))
            continue
        if t == "consignas":
            fijadas.update(m for m in ("V", "I", "P") if m in e)
        elif t == "rampa":
            fijadas.add(e["magnitud"])
        for m, u in (("V", "V"), ("I", "A"), ("P", "W")):
            # lo que fija el plan se comprueba siempre; lo que ya tenia la fuente, solo si la salida esta encendida
            # (apagada no hace nada: un plan de solo marcas y esperas no tiene por que tocar las consignas)
            cuenta = k[m] is not None and (m in fijadas or salida)
            if cuenta and m in maximos and k[m] > maximos[m] + 1e-9:
                if m in fijadas:
                    errores.append(f"{donde}: {m} = {k[m]:g} {u} pasa el máximo del plan ({maximos[m]:g} {u})")
                elif m not in avisadas:
                    avisadas.add(m)
                    errores.append(f"{donde}: con la salida encendida, la consigna que ya tiene la fuente ({m} = {k[m]:g} {u}) "
                                   f"pasa el máximo del plan ({maximos[m]:g} {u}): fíjala en el plan antes de encender")
            if cuenta:
                alcanzado[m] = max(alcanzado[m], k[m])
        e["texto"] = e.get("texto") if t == "marca" else None
        e = {kk: v for kk, v in e.items() if v is not None}
        norm.append(e)
    if errores:
        raise ValueError("El plan no se puede usar:\n- " + "\n- ".join(errores))
    p["etapas"] = norm
    texto = (f"{len(norm)} etapas; duración {'al menos ' if abierta else ''}{_hms(duracion)}"
             f"{' (hay pausas o esperas hasta Siguiente)' if abierta else ''}; "
             f"llega a {alcanzado['V']:g} V, {alcanzado['I']:g} A, {alcanzado['P']:g} W "
             f"(máximos del plan {maximos['V']:g} V, {maximos['I']:g} A, {maximos['P']:g} W)"
             f"{'; ENCIENDE la salida' if enciende else ''}")
    return p, {"duracion_s": duracion, "abierta": abierta, "alcanzado": alcanzado, "enciende": enciende,
               "texto": texto, "etapas": [describir(e) for e in norm]}



def usos(plan):
    """Qué fuentes mueve el plan: {"ea": bool, "spd": bool}."""
    ea = spd = False
    for e in plan.get("etapas") or []:
        if not isinstance(e, dict) or e.get("tipo") not in ("consignas", "rampa", "apagar"):
            continue
        f = _fuente(e)
        if f == "ea":
            ea = True
        elif f.startswith("spd."):
            spd = True
    return {"ea": ea, "spd": spd}


def comprobar(plan, topes, equipo, consignas_actuales, con_osc_registrando=False, spd=None, medidas=None,
              ea_conectada=True):
    """Valida y normaliza el plan (fase 4: varias fuentes y condiciones). Devuelve (plan, resumen) o ValueError con
    TODOS los problemas. spd: resumen de la Siglent (None = no conectada); medidas: nombres que se pueden usar en las
    condiciones (None = no se comprueban); ea_conectada: si la fuente EA está conectada."""
    if not isinstance(plan, dict):
        raise ValueError("El plan no es válido")
    p = copy.deepcopy(plan)
    errores = []
    etapas = p.get("etapas") or []
    for n, e in enumerate(etapas, 1):
        if isinstance(e, dict) and _fuente(e) not in FUENTES + ("todo",):
            errores.append(f"Etapa {n}: fuente «{e.get('fuente')}» no válida ({', '.join(FUENTES)} o todo)")
        if isinstance(e, dict) and _fuente(e) == "todo" and e.get("tipo") != "apagar":
            errores.append(f"Etapa {n}: «todo» solo vale para apagar")
    u = usos(p)
    # --- condiciones (esperar y guardas)
    def cond_ok(txt, donde):
        try:
            m, op, v = condicion(txt)
        except ValueError as x:
            errores.append(f"{donde}: {x}")
            return None
        if medidas is not None and m not in medidas:
            errores.append(f"{donde}: la medida «{m}» no existe ahora ({', '.join(sorted(medidas)) or 'ninguna'})")
        return f"{m} {op} {v:g}"
    guardas = []
    for k, g in enumerate(p.get("guardas") or [], 1):
        c = cond_ok(g, f"Guarda {k}")
        if c:
            guardas.append(c)
    p["guardas"] = guardas
    # --- EA: la comprobación de siempre, con las etapas que no son de la Siglent
    etapas_ea = [e for e in etapas if not (isinstance(e, dict) and (_fuente(e).startswith("spd.") or _fuente(e) == "todo"
                                                                     or e.get("tipo") == "esperar"))]
    res_ea = None
    if u["ea"]:
        if not ea_conectada:
            errores.append("El plan mueve la fuente EA y no está conectada")
        else:
            try:
                pe, res_ea = _comprobar_ea(dict(p, etapas=etapas_ea), topes, equipo, consignas_actuales, con_osc_registrando)
                p["maximos"] = pe["maximos"]
            except ValueError as x:
                errores.extend(l[2:] for l in str(x).split("\n")[1:])
    # --- Siglent: consignas y rampas frente a sus máximos del plan, los topes del panel y el equipo
    mx_spd = {}
    alc_spd = {"V": 0.0, "I": 0.0}
    enciende_spd = False
    if u["spd"]:
        if not spd or spd.get("estado") != "conectada":
            errores.append("El plan mueve la Siglent y no está conectada")
        elif spd.get("modo") != "independiente":
            errores.append(f"El plan usa la Siglent con sus canales independientes (ahora está en modo {spd.get('modo')})")
        mx = p.get("maximos_spd") or {}
        for m, un in (("V", "V"), ("I", "A")):
            try:
                mx_spd[m] = _num(mx.get(m), f"Máximo de {m} de la Siglent")
            except ValueError as x:
                errores.append(str(x) if mx.get(m) not in (None, "") else f"Falta el máximo de {m} de la Siglent (maximos_spd)")
                continue
            tope = (spd or {}).get("topes", {}).get(m, SM.V_MAX if m == "V" else SM.I_MAX)
            if mx_spd[m] > tope + 1e-9:
                errores.append(f"El máximo de {m} de la Siglent ({mx_spd[m]:g} {un}) pasa su tope del panel ({tope:g} {un})")
        p["maximos_spd"] = mx_spd
    k_spd = {c: {"V": ((spd or {}).get("canales") or {}).get(c, {}).get("V_cons"),
                 "I": ((spd or {}).get("canales") or {}).get(c, {}).get("I_cons")} for c in SM.CANALES}
    duracion, abierta = 0.0, False
    norm = []
    j_ea = 0
    for n, e in enumerate(etapas, 1):
        donde = f"Etapa {n}"
        if not isinstance(e, dict) or e.get("tipo") not in TIPOS:
            if not errores or not any(x.startswith(donde) for x in errores):
                errores.append(f"{donde}: tipo de etapa no válido ({e.get('tipo') if isinstance(e, dict) else e})")
            continue
        f, t = _fuente(e), e["tipo"]
        es_spd = f.startswith("spd.") and t in ("consignas", "rampa", "apagar")
        if not es_spd and not (f == "todo") and t != "esperar":
            # etapa de la EA o común: la normalizada por _comprobar_ea (mismo orden)
            if res_ea is not None and j_ea < len(pe["etapas"]):
                norm.append(pe["etapas"][j_ea])
                if t in ("rampa", "mantener"):
                    duracion += (pe["etapas"][j_ea].get("duracion_s") or 0)
                    abierta = abierta or (t == "mantener" and not pe["etapas"][j_ea].get("duracion_s"))
                if t == "pausa":
                    abierta = True
            else:
                ee = {kk: v for kk, v in e.items() if v not in (None, "")}
                if t == "captura":
                    ee["nombre"] = almacen._limpio(ee.get("nombre") or "captura")
                    if con_osc_registrando:
                        errores.append(f"{donde}: no se puede capturar mientras el osciloscopio registra en el ensayo")
                elif t == "marca" and not str(ee.get("texto") or "").strip():
                    errores.append(f"{donde}: la marca no tiene texto")
                elif t == "mantener" and "duracion_s" in ee:
                    try:
                        ee["duracion_s"] = _num(ee["duracion_s"], f"{donde}, duración")
                    except ValueError as x:
                        errores.append(str(x))
                elif t == "pausa":
                    ee["mensaje"] = str(ee.get("mensaje") or "Pulsa Continuar")
                norm.append(ee)
                e = ee
                if t == "pausa" or (t == "mantener" and not e.get("duracion_s")):
                    abierta = True
                elif t == "mantener":
                    duracion += float(e.get("duracion_s") or 0)
            j_ea += 1
            continue
        e = {kk: v for kk, v in e.items() if v not in (None, "")}
        try:
            if t == "esperar":
                c = cond_ok(e.get("condicion"), donde)
                if c is None:
                    continue
                e["condicion"] = c
                if "max_s" in e:
                    e["max_s"] = _num(e["max_s"], f"{donde}, tiempo máximo")
                    duracion += e["max_s"]
                else:
                    abierta = True
                e["si_no"] = e.get("si_no") or "parar"
                if e["si_no"] not in ("parar", "seguir"):
                    raise ValueError(f"{donde}: «si no se cumple» tiene que ser parar o seguir")
            elif f == "todo":
                pass
            elif t == "apagar":
                pass
            else:
                c = f[4:]
                if c == SM.CH3:
                    if t == "rampa" or any(m in e for m in ("V", "I")):
                        raise ValueError(f"{donde}: CH3 de la Siglent solo se enciende y apaga (su tensión va por el conmutador)")
                    if e.get("salida") == "on":
                        enciende_spd = True
                elif t == "consignas":
                    for m in ("V", "I"):
                        if m in e:
                            e[m] = _num(e[m], f"{donde}, {m}")
                            k_spd[c][m] = e[m]
                    if "P" in e:
                        raise ValueError(f"{donde}: la Siglent no tiene consigna de potencia")
                    if e.get("salida") not in (None, "on", "off"):
                        raise ValueError(f"{donde}: salida tiene que ser on, off o nada")
                    if not any(m in e for m in ("V", "I")) and "salida" not in e:
                        raise ValueError(f"{donde}: no fija nada")
                    if e.get("salida") == "on":
                        enciende_spd = True
                else:   # rampa
                    if e.get("magnitud") not in ("V", "I"):
                        raise ValueError(f"{donde}: la rampa de la Siglent tiene que ser de V o I")
                    m = e["magnitud"]
                    e["hasta"] = _num(e.get("hasta"), f"{donde}, hasta")
                    e["paso"] = _num(e.get("paso"), f"{donde}, paso")
                    e["duracion_s"] = _num(e.get("duracion_s"), f"{donde}, duración")
                    if e["paso"] <= 0:
                        raise ValueError(f"{donde}: el paso tiene que ser mayor que 0")
                    if k_spd[c][m] is None:
                        raise ValueError(f"{donde}: no se sabe desde dónde empieza la rampa (fija antes {m})")
                    pasos = int(abs(e["hasta"] - k_spd[c][m]) / e["paso"] + 0.999999)
                    if pasos > MAX_PASOS_RAMPA:
                        raise ValueError(f"{donde}: {pasos} pasos son demasiados (máximo {MAX_PASOS_RAMPA}): sube el paso")
                    if pasos == 0:
                        raise ValueError(f"{donde}: la rampa no se mueve (ya está en {e['hasta']:g})")
                    e["desde"] = k_spd[c][m]
                    k_spd[c][m] = e["hasta"]
                    duracion += e["duracion_s"]
                if c != SM.CH3:
                    for m, un in (("V", "V"), ("I", "A")):
                        v = k_spd[c][m]
                        if v is None:
                            continue
                        err = SM.comprobar({c: {m: v}}, topes=(spd or {}).get("topes"))
                        if err:
                            errores.append(f"{donde}: {err[0]}")
                        if m in mx_spd and v > mx_spd[m] + 1e-9:
                            errores.append(f"{donde}: Siglent {c} {m} = {v:g} {un} pasa el máximo del plan ({mx_spd[m]:g} {un})")
                        alc_spd[m] = max(alc_spd[m], v)
        except ValueError as x:
            errores.append(str(x))
            continue
        norm.append(e)
    if not etapas:
        errores.append("El plan no tiene etapas")
    if not u["ea"] and not u["spd"] and not any(isinstance(e, dict) and e.get("tipo") == "apagar" for e in etapas):
        pass                             # un plan de solo esperas, marcas y capturas también vale
    if errores:
        raise ValueError("El plan no se puede usar:\n- " + "\n- ".join(dict.fromkeys(errores)))
    p["etapas"] = norm
    enciende_ea = bool(res_ea and res_ea["enciende"])
    partes = [f"{len(norm)} etapas; duración {'al menos ' if abierta else ''}{_hms(duracion)}"
              + (" (hay pausas o esperas sin límite)" if abierta else "")]
    if res_ea:
        a = res_ea["alcanzado"]
        partes.append(f"EA llega a {a['V']:g} V, {a['I']:g} A, {a['P']:g} W (máx. del plan {p['maximos']['V']:g} V, "
                      f"{p['maximos']['I']:g} A, {p['maximos']['P']:g} W){' · ENCIENDE la EA' if enciende_ea else ''}")
    if u["spd"]:
        partes.append(f"Siglent llega a {alc_spd['V']:g} V, {alc_spd['I']:g} A (máx. del plan {mx_spd.get('V', 0):g} V, "
                      f"{mx_spd.get('I', 0):g} A){' · ENCIENDE la Siglent' if enciende_spd else ''}")
    if guardas:
        partes.append("guardas: " + ", ".join(guardas) + " → apagar todo")
    alcanzado = res_ea["alcanzado"] if res_ea else {"V": 0.0, "I": 0.0, "P": 0.0}
    return p, {"duracion_s": duracion, "abierta": abierta, "alcanzado": alcanzado, "alcanzado_spd": alc_spd,
               "enciende": enciende_ea or enciende_spd, "enciende_ea": enciende_ea, "enciende_spd": enciende_spd,
               "usa_ea": u["ea"], "usa_spd": u["spd"], "guardas": guardas,
               "texto": "; ".join(partes), "etapas": [describir(x) for x in norm]}


def medidas_disponibles(ens):
    """Nombres de medida que se pueden usar en las condiciones, con su valor actual (None = sin dato)."""
    return {k: v for k, v in _medidas(ens).items()}


def _medidas(ens):
    m = {}
    f = ens.fte
    if f and f.estado == "conectada":
        md = f.medida or {}
        m.update({"ea.V": md.get("V"), "ea.I": md.get("I"), "ea.P": md.get("P")})
    sp = getattr(ens, "spd", None)
    if sp and sp.estado == "conectada":
        for c in SM.CANALES:
            q = sp.canales.get(c) or {}
            m[f"spd.{c}.V"], m[f"spd.{c}.I"] = q.get("V"), q.get("I")
    d = getattr(ens, "daq", None)
    if d and d.estado == "midiendo":
        for c in d.canales:
            m[f"cdaq.{c['nombre']}"] = c.get("ultimo")
    pl = getattr(ens, "pl", None)
    if pl and pl.estado in ("capturando", "midiendo"):
        for c in pl.canales or []:
            m[f"tc.{c['nombre']}"] = c.get("ultimo")
    return m


# ------------------------------------------------------------------- planes guardados
def _dir_planes(proyecto):
    return os.path.join(almacen._dir_tipo(proyecto, "ensayos"), "_planes")


def listar(proyecto):
    d = _dir_planes(proyecto)
    if not os.path.isdir(d):
        return []
    return sorted(f[:-5] for f in os.listdir(d) if f.lower().endswith(".json"))


def leer(proyecto, nombre):
    with open(os.path.join(_dir_planes(proyecto), almacen._limpio(nombre) + ".json"), encoding="utf-8") as fh:
        return json.load(fh)


def guardar(proyecto, nombre, plan):
    nombre = almacen._limpio(nombre)
    d = _dir_planes(proyecto)
    os.makedirs(d, exist_ok=True)
    plan = dict(plan, nombre=nombre)
    with open(os.path.join(d, nombre + ".json"), "w", encoding="utf-8") as fh:
        json.dump(plan, fh, ensure_ascii=False, indent=1)
    return nombre


# ------------------------------------------------------------------- ejecucion
class _Parado(Exception):
    """PARAR (o un error) mientras el plan esperaba antes de dar una orden: se deja de ejecutar el plan."""


class _Saltar(Exception):
    """«Siguiente» pulsado en una pausa antes de una orden: se salta el resto de la etapa (p. ej. la rampa)."""


class Ejecucion:
    """Corre un plan ya comprobado dentro de un ensayo en marcha. La crea Ensayos.iniciar y la cierra Ensayos."""

    def __init__(self, ensayos, a, plan, autor):
        self.ens, self.fte, self.osc, self.reg = ensayos, ensayos.fte, ensayos.osc, ensayos.reg
        self.spd = getattr(ensayos, "spd", None)
        u = usos(plan)
        self.usa_ea, self.usa_spd = u["ea"], u["spd"]
        self.guardas = [condicion(g) for g in plan.get("guardas") or []]
        self.esperado_spd = {}               # {CH1: {"V", "I", "salida"}} lo que el plan dio a la Siglent
        self.a, self.plan, self.autor = a, plan, autor
        self.etapas = plan["etapas"]
        self.i = -1                          # etapa en curso (indice)
        self.estado = "esperando registro"   # esperando registro | en marcha | pausado | esperando usuario | terminado | parado | error
        self.mensaje = ""
        self.t_etapa = None                  # inicio de la etapa en curso
        self.fin_etapa = None                # fin previsto (etapas con tiempo)
        self.restante_pausa = None           # tiempo que quedaba al pausar
        self.historial = []                  # [{etapa, texto, inicio, fin, resultado}]
        self.esperado = None                 # consignas y salida que el plan dio a la fuente
        self._t_accion = 0.0
        self._pausado_etapa = 0.0            # tiempo en pausa dentro de la etapa en curso
        self._siguiente = threading.Event()
        self._parar = threading.Event()
        self._pausa = threading.Event()
        self._hilo = threading.Thread(target=self._correr, daemon=True)

    # -------------------------------------------------- mandos (desde la pagina)
    def arrancar(self):
        self._hilo.start()

    def pausar(self, autor):
        if self.estado in ("en marcha", "esperando usuario"):
            self._pausa.set()
            self._anotar(autor, "plan en pausa (pulsado Pausar)")

    def continuar(self, autor, confirmado=False):
        # tras APAGAR, una proteccion, el vigilante o un OFF en el frontal, Continuar volveria a ENCENDER: solo si la
        # persona lo confirma (revisión del 2026-11-04: antes el plan volvía a encender solo)
        if self.usa_ea and self.fte.bloqueo and self.esperado and self.esperado.get("salida"):
            if not confirmado:
                raise ValueError(f"CONFIRMAR: Continuar volverá a ENCENDER la salida de la fuente "
                                 f"(se apagó por: {self.fte.bloqueo}). ¿Seguro?")
            self.fte.desbloquear(autor)
        if self._pausa.is_set():
            self._pausa.clear()
            self._anotar(autor, "plan: continuar")
        elif self.estado == "esperando usuario":
            self._siguiente.set()
            self._anotar(autor, "plan: continuar")

    def siguiente(self, autor):
        if self.estado in ("en marcha", "esperando usuario", "pausado"):
            self._pausa.clear()
            self._siguiente.set()
            self._anotar(autor, "plan: saltar a la siguiente etapa")

    def parar(self, autor, motivo="PARAR pulsado"):
        if not self._parar.is_set():
            self._parar.set()
            self._anotar(autor, f"plan parado: {motivo}")

    def resumen(self):
        e = self.etapas[self.i] if 0 <= self.i < len(self.etapas) else None
        ahora = time.time()
        fin = self.fin_etapa
        if e and e["tipo"] == "rampa" and self.t_etapa and fin:      # lo que falta de la rampa entera, no del paso
            fin = max(fin, self.t_etapa + e["duracion_s"] + self._pausado_etapa)
        return {"nombre": self.plan.get("nombre", ""), "estado": self.estado, "mensaje": self.mensaje,
                "etapa": self.i + 1, "etapas": len(self.etapas), "texto": describir(e) if e else "",
                "lista": [describir(x) for x in self.etapas],
                "transcurrido_etapa_s": round(ahora - self.t_etapa, 1) if self.t_etapa else None,
                "queda_etapa_s": (round(self.restante_pausa, 1) if self.restante_pausa is not None else
                                  round(max(0.0, fin - ahora), 1) if fin else None),
                "maximos": self.plan.get("maximos")}

    def vivo(self):
        return self._hilo.is_alive()

    # -------------------------------------------------- ayudas
    def _anotar(self, autor, texto, detalle=None):
        self.reg.anotar(autor, "ensayo", texto, detalle)
        self.ens._marca(self.a, texto)        # en los ficheros de las fuentes que registran el ensayo (9-nov)

    def _aplicar(self, V=None, I=None, P=None, salida=None, reponer=False):
        """Ordenes a la fuente (por el mismo camino que la pagina: topes, confirmacion de encendido...).
        Antes de cada orden se vigila (sin la gracia de la lectura: bloqueo, protecciones, remoto) y se respeta una
        pausa en curso: una rampa rapida ya no sigue escribiendo consignas tras un APAGAR o un «local».
        reponer=True: lo llama Continuar (una persona) para volver a dejar la fuente como dice el plan."""
        if not reponer:
            r = self._esperar(0, marcar=False)
            if r == "parar":
                raise _Parado()
            if r == "siguiente":
                raise _Saltar()
        primera = self.esperado is None
        if primera:
            self.esperado = {"V": None, "I": None, "P": None, "salida": None}
        # el plan solo toma el remoto en su primera orden o cuando una persona pulsa Continuar
        tomar = primera or reponer
        try:
            if any(x is not None for x in (V, I, P)):
                self.fte.fijar("plan", V_lim_V=V, I_A=I, P_W=P, tomar_control=tomar)
            if salida is not None:
                self.fte.salida(salida, True, "plan", tomar_control=tomar)
        except (EncendidoBloqueado, ControlPerdido) as e:
            # lo que el plan queria queda como esperado: Continuar (confirmando si enciende) lo vuelve a aplicar
            for m, v in (("V", V), ("I", I), ("P", P)):
                if v is not None:
                    self.esperado[m] = v
            if salida is not None:
                self.esperado["salida"] = bool(salida)
            self._t_accion = time.time()
            self._pausar(str(e))
            r = self._esperar(0, marcar=False)
            if r == "parar":
                raise _Parado()
            if r == "siguiente":
                raise _Saltar()
            return
        k = self.fte.consignas
        for m, c in MAGNITUDES.items():
            if k.get(c) is not None:
                self.esperado[m] = k[c]
        if salida is not None:
            self.esperado["salida"] = bool(salida)
        self._t_accion = time.time()

    def _pausar(self, motivo):
        if not self._pausa.is_set():
            self._pausa.set()
            self.mensaje = (f"EN PAUSA: {motivo}. «Continuar» vuelve a aplicar lo del plan (si enciende, pide "
                            f"confirmación); PARAR apaga y termina")
            self._anotar("panel (automático)", f"plan en pausa: {motivo}")

    def _desviacion(self):
        """Motivo si las fuentes no estan como las dejo el plan (None si esta bien)."""
        if self.usa_spd:
            if not self.spd or self.spd.estado != "conectada":
                return "PERDIDA_SPD"
            if time.time() - self._t_accion >= GRACIA_S:
                for c, q in self.esperado_spd.items():
                    if c == SM.CH3:
                        continue
                    on = (self.spd.canales.get(c) or {}).get("salida")
                    if q.get("salida") is not None and on is not None and bool(on) != q["salida"]:
                        return f"la salida {c} de la Siglent se ha {'apagado' if q['salida'] else 'encendido'} fuera del plan"
        if not self.usa_ea:
            return None
        if self.fte.estado != "conectada":
            return "PERDIDA"
        if self.esperado is None:
            return None
        # lo grave, al momento (sin la gracia de la lectura): antes, en rampas de menos de 1,5 s por nivel, la
        # gracia se renovaba en cada paso y no se vigilaba nada (revisión del 2026-11-04)
        if self.esperado.get("salida") and self.fte.bloqueo:
            return f"salida apagada fuera del plan ({self.fte.bloqueo})"
        alarmas = [n for n, on in (self.fte.alarmas or {}).items() if on]
        if alarmas:
            return f"protección {', '.join(alarmas)} activa"
        if not self.fte.remoto:
            return "la fuente ha salido del control remoto (frontal, «local» o vigilante)"
        if time.time() - self._t_accion < GRACIA_S:
            return None
        k, m = self.fte.consignas, self.fte.medida
        for mag, c in MAGNITUDES.items():
            v = self.esperado.get(mag)
            if v is not None and k.get(c) is not None and abs(k[c] - v) > max(1e-6, 1e-4 * abs(v)):
                return f"la consigna de {mag} ha cambiado fuera del plan ({v:g} → {k[c]:g})"
        sal = self.esperado.get("salida")
        if sal is not None and m.get("salida") is not None and bool(m["salida"]) != sal:
            alarmas = [n for n, on in (self.fte.alarmas or {}).items() if on]
            causa = (f"protección {', '.join(alarmas)}" if alarmas else
                     "vigilante de la fuente o a mano" if not sal else "a mano")
            return f"la salida se ha {'apagado' if sal else 'encendido'} fuera del plan ({causa})"
        return None

    def _esperar(self, segundos=None, marcar=True):
        """Espera con vigilancia. Devuelve 'fin' (cumplido), 'siguiente' o 'parar'. Las pausas no cuentan.
        marcar=False: comprobacion antes de una orden (_aplicar), sin tocar el fin de la etapa que ve la pagina."""
        fin = time.time() + segundos if segundos is not None else None
        if marcar:
            self.fin_etapa = fin
        while True:
            if self._parar.is_set():
                return "parar"
            g = self._guarda()
            if g:
                self._por_guarda(g)
                return "parar"
            d = self._desviacion()
            if d in ("PERDIDA", "PERDIDA_SPD"):
                self._fallo("sin comunicación con la " + ("fuente EA" if d == "PERDIDA" else "Siglent"))
                return "parar"
            if d and not self._pausa.is_set():
                self._pausar(d)
            if self._pausa.is_set():
                quedaba = None if fin is None else max(0.0, fin - time.time())
                self.restante_pausa, previo, self.estado = quedaba, self.estado, "pausado"
                t_pausa = time.time()
                if marcar:
                    self.fin_etapa = None
                while self._pausa.is_set() and not self._parar.is_set():
                    if self.usa_ea and self.fte.estado != "conectada":
                        self._fallo("sin comunicación con la fuente")
                        return "parar"
                    if self.usa_spd and (not self.spd or self.spd.estado != "conectada"):
                        self._fallo("sin comunicación con la Siglent")
                        return "parar"
                    g = self._guarda()             # las guardas también vigilan en pausa
                    if g:
                        self._por_guarda(g)
                        return "parar"
                    if self._siguiente.is_set():
                        break
                    time.sleep(CADA_S)
                if self._parar.is_set():
                    return "parar"
                self._pausado_etapa += time.time() - t_pausa
                self.restante_pausa, self.estado, self.mensaje = None, previo, ""
                if fin is not None:
                    fin = time.time() + quedaba
                    if marcar:
                        self.fin_etapa = fin
                if self._siguiente.is_set():
                    self._siguiente.clear()
                    return "siguiente"
                try:                       # Continuar: se vuelven a dejar las fuentes como dice el plan
                    if self.esperado and self.usa_ea:
                        e = self.esperado
                        self._aplicar(V=e["V"], I=e["I"], P=e["P"], salida=e["salida"], reponer=True)
                    for c, q in list(self.esperado_spd.items()):
                        self._aplicar_spd(c, V=q.get("V"), I=q.get("I"), salida=q.get("salida"), reponer=True)
                except Exception as err:
                    self._fallo(f"no se pudo volver a aplicar el plan: {err}")
                    return "parar"
                continue
            if self._siguiente.is_set():
                self._siguiente.clear()
                return "siguiente"
            if fin is not None and time.time() >= fin:
                return "fin"
            # sin pasarse del fin: con 0,2 s fijos cada nivel de una rampa duraba hasta 0,2 s de mas
            time.sleep(CADA_S if fin is None else min(CADA_S, max(0.005, fin - time.time())))

    # -------------------------------------------------- fase 4: Siglent, condiciones y guardas
    def _valor(self, medida):
        return _medidas(self.ens).get(medida)

    def _guarda(self):
        """Primera guarda que se cumple (texto con el valor) o None."""
        for m, op, v in self.guardas:
            x = self._valor(m)
            if x is not None and OPS[op](x, v):
                return f"{m} {op} {v:g} (vale {x:.4g})"
        return None

    def _por_guarda(self, g):
        """Una guarda se ha cumplido: APAGAR TODO (todas las fuentes conectadas) y parar el plan."""
        apagado = []
        try:
            if self.fte.estado == "conectada":
                self.fte.salida(False, True, "plan (guarda)")
                apagado.append("EA")
        except Exception as x:
            apagado.append(f"EA NO ({x})")
        try:
            if self.spd and self.spd.estado == "conectada":
                self.spd.apagar_todo("plan (guarda)", f"guarda {g}")
                apagado.append("Siglent")
        except Exception as x:
            apagado.append(f"Siglent NO ({x})")
        self.estado, self.mensaje = "error", f"GUARDA: {g} → todo apagado ({', '.join(apagado) or 'nada encendido'})"
        self._anotar("panel (automático)", f"GUARDA cumplida: {g}: todo apagado", {"apagado": apagado})
        self._parar.set()

    def _aplicar_spd(self, canal, V=None, I=None, salida=None, reponer=False):
        """Órdenes a la Siglent por el mismo camino que la página (modelo validado: negativos, máximos, topes)."""
        if not reponer:
            r = self._esperar(0, marcar=False)
            if r == "parar":
                raise _Parado()
            if r == "siguiente":
                raise _Saltar()
        q = self.esperado_spd.setdefault(canal, {"V": None, "I": None, "salida": None})
        if canal != SM.CH3 and (V is not None or I is not None):
            self.spd.fijar("plan", canal, V=V, I=I)
            if V is not None:
                q["V"] = V
            if I is not None:
                q["I"] = I
        if salida is not None:
            self.spd.salida("plan", canal, bool(salida), confirmado=True)
            q["salida"] = bool(salida)
        self._t_accion = time.time()

    def _esperar_condicion(self, e):
        m, op, v = condicion(e["condicion"])
        t0 = time.time()
        fin = t0 + e["max_s"] if e.get("max_s") else None
        self.fin_etapa = fin
        self.mensaje = f"Esperando a que {e['condicion']}"
        while True:
            x = self._valor(m)
            if x is not None and OPS[op](x, v):
                self.mensaje = ""
                self._anotar("plan", f"cumplido: {e['condicion']} (vale {x:.4g} a los {time.time() - t0:.0f} s)")
                return "fin"
            if fin is not None and time.time() >= fin:
                self.mensaje = ""
                txt = f"no se ha cumplido {e['condicion']} en {_hms(e['max_s'])} (vale {x if x is None else round(x, 4)})"
                if e.get("si_no") == "seguir":
                    self._anotar("plan", txt + ": se sigue")
                    return "fin"
                self._fallo(txt)
                return "parar"
            r = self._esperar(CADA_S if fin is None else min(CADA_S, max(0.01, fin - time.time())), marcar=False)
            if r != "fin":
                self.mensaje = ""
                return r

    def _fallo(self, motivo):
        self.estado, self.mensaje = "error", f"PLAN PARADO: {motivo}"
        self._anotar("panel (automático)", f"PLAN PARADO por error: {motivo}")
        self._parar.set()

    # -------------------------------------------------- bucle
    def _correr(self):
        try:
            # como «encender al iniciar» de la fase 4: no se empieza hasta que se graba todo
            self.ens._esperar_registro(self.a)
            if self.ens.actual is not self.a or self.a["estado"] != "en marcha":
                self.estado = "parado"
                return
            self.estado = "en marcha"
            self._anotar(self.autor, f"plan «{self.plan.get('nombre', '')}» iniciado ({len(self.etapas)} etapas)")
            for self.i, e in enumerate(self.etapas):
                if self._parar.is_set():
                    break
                self._siguiente.clear()
                self.t_etapa, self.fin_etapa, self._pausado_etapa = time.time(), None, 0.0
                txt = describir(e)
                self._anotar("plan", f"etapa {self.i + 1}/{len(self.etapas)}: {txt}")
                h = {"etapa": self.i + 1, "texto": txt, "inicio": time.strftime("%Y-%m-%dT%H:%M:%S")}
                self.historial.append(h)
                try:
                    r = self._etapa(e)
                except _Saltar:
                    r = "siguiente"
                h.update(fin=time.strftime("%Y-%m-%dT%H:%M:%S"), resultado=r)
                if r == "parar":
                    break
            else:
                self.estado = "terminado"
                self._anotar("plan", "plan terminado")
        except _Parado:
            pass
        except Exception as err:
            self._fallo(f"{type(err).__name__}: {err}")
        finally:
            if self.estado not in ("terminado", "error"):
                self.estado = "parado"
            self.fin_etapa = None
            # el ensayo se termina siempre al acabar el plan (apaga si se eligio; con error o PARAR, apaga siempre)
            self.ens._fin_del_plan(self.a, apagar=self.estado != "terminado")

    def _etapa(self, e):
        t = e["tipo"]
        f = _fuente(e)
        if t == "esperar":
            return self._esperar_condicion(e)
        if f == "todo":                         # apagar todas las salidas de todas las fuentes
            if self.usa_ea or self.fte.estado == "conectada":
                self._aplicar(salida=False)
            if self.spd and self.spd.estado == "conectada":
                for c in (*SM.CANALES, SM.CH3):
                    self._aplicar_spd(c, salida=False)
            return "fin"
        if f.startswith("spd."):
            c = f[4:]
            if t == "consignas":
                sal = {"on": True, "off": False}.get(e.get("salida"))
                self._aplicar_spd(c, V=e.get("V"), I=e.get("I"), salida=sal)
                return "fin"
            if t == "apagar":
                self._aplicar_spd(c, salida=False)
                return "fin"
            m = e["magnitud"]                   # rampa de la Siglent: igual que la de la EA
            desde = (self.spd.canales.get(c) or {}).get(f"{m}_cons")
            if desde is None:
                raise RuntimeError(f"no se puede leer la consigna de {m} de la Siglent {c}")
            n = max(1, int(abs(e["hasta"] - desde) / e["paso"] + 0.999999))
            espera = e["duracion_s"] / n
            signo = 1 if e["hasta"] >= desde else -1
            for k in range(1, n + 1):
                r = self._esperar(espera)
                if r != "fin":
                    return r
                self._aplicar_spd(c, **{m: e["hasta"] if k == n else round(desde + signo * k * e["paso"], 6)})
            return "fin"
        if t == "consignas":
            sal = {"on": True, "off": False}.get(e.get("salida"))
            self._aplicar(V=e.get("V"), I=e.get("I"), P=e.get("P"), salida=sal)
            return "fin"
        if t == "apagar":
            self._aplicar(salida=False)
            return "fin"
        if t == "marca":
            return "fin"                        # ya quedo como evento al empezar la etapa
        if t == "captura":
            try:
                self.ens._capturar(e["nombre"], self.autor)
            except Exception as err:
                self._anotar("panel (automático)", f"captura «{e['nombre']}» NO hecha: {err}")
            return "fin"
        if t == "pausa":
            self.estado, self.mensaje = "esperando usuario", e["mensaje"]
            r = self._esperar(None)
            if self.estado == "esperando usuario":
                self.estado = "en marcha"
            self.mensaje = ""
            return r
        if t == "mantener":
            if e.get("duracion_s"):
                return self._esperar(e["duracion_s"])
            self.estado, self.mensaje = "esperando usuario", "Mantener: pulsa Siguiente para seguir"
            r = self._esperar(None)
            if self.estado == "esperando usuario":
                self.estado = "en marcha"
            self.mensaje = ""
            return r
        # rampa: desde la consigna actual hasta 'hasta' en pasos iguales repartidos en la duracion. Cada nivel dura
        # duracion/n empezando por el de partida, y el valor final se pone justo al acabar (como «de 0,1 a 0,5 A en
        # 20 s»); la etapa siguiente lo mantiene. Antes el primer paso se daba al empezar y el de partida no duraba
        # nada (visto en la primera prueba real, 2026-11-03)
        m = e["magnitud"]
        desde = self.fte.consignas.get(MAGNITUDES[m])
        if desde is None:
            raise RuntimeError(f"no se puede leer la consigna de {m}")
        n = max(1, int(abs(e["hasta"] - desde) / e["paso"] + 0.999999))
        espera = e["duracion_s"] / n
        signo = 1 if e["hasta"] >= desde else -1
        for k in range(1, n + 1):
            r = self._esperar(espera)
            if r != "fin":
                return r
            self._aplicar(**{m: e["hasta"] if k == n else round(desde + signo * k * e["paso"], 6)})
        return "fin"
