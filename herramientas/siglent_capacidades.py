r"""Siglent SPD3303X: lo que de verdad hace cada orden SCPI, probado en el equipo (9-nov-2026).

ENCIENDE SALIDAS (autorizado por el usuario el 9-nov: «la fuente no tiene nada conectado así que puedes activar
tensión»). Prudente: con salida encendida solo 1-5 V y 0,1 A; los valores altos (32 V, 3,2 A) solo se programan y
se leen con la salida APAGADA. Al empezar guarda la configuración y al terminar (pase lo que pase) apaga todo,
lo comprueba MIDIENDO la tensión (no solo con el bit de estado: hay un fallo conocido en el que el estado dice
apagado y la salida sigue viva) y devuelve la configuración. No toca *SAV/*RCL (memorias del usuario) ni la red.

    python herramientas\siglent_capacidades.py
Escribe docs\siglent_capacidades_<fecha>.json
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from siglent_descubrir import abrir, estado, q        # noqa: E402

PAUSA_S = 0.3
R = {"pruebas": []}
DOCS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs")


def w(f, orden, pausa=PAUSA_S):
    f.write(orden)
    time.sleep(pausa)


def err(f):
    return q(f, "SYSTem:ERRor?")


def num(f, orden, intentos=3):
    """Con el temporizador en marcha la fuente puede contestar vacío (visto el 9-nov): se reintenta; None si no."""
    for _ in range(intentos):
        try:
            r = q(f, orden)
            if r:
                return float(r)
        except Exception:
            pass
        time.sleep(0.3)
    return None


def st(f):
    return estado(q(f, "SYSTem:STATus?"))


def anota(grupo, que, ok, detalle):
    R["pruebas"].append({"grupo": grupo, "que": que, "ok": ok, "detalle": detalle})
    print(f"{'OK   ' if ok else 'FALLO' if ok is False else 'INFO '} [{grupo}] {que}  [{detalle}]", flush=True)


def apagar_todo(f):
    for t in ("CH1", "CH2"):                  # primero el temporizador (si no, podría volver a encender)
        try:
            w(f, f"TIMEr {t},OFF", 0.5)
        except Exception:
            pass
    for c in ("CH1", "CH2", "CH3"):
        try:
            w(f, f"OUTPut {c},OFF", 1.0)      # 1 s entre canales (el fallo conocido aparece con órdenes seguidas)
        except Exception:
            pass
    time.sleep(0.5)
    v1, v2, e = num(f, "MEASure:VOLTage? CH1"), num(f, "MEASure:VOLTage? CH2"), st(f)
    return v1, v2, e


def main():
    f = abrir()
    print(q(f, "*IDN?"), flush=True)
    previo = {"CH1": (q(f, "CH1:VOLTage?"), q(f, "CH1:CURRent?")), "CH2": (q(f, "CH2:VOLTage?"), q(f, "CH2:CURRent?")),
              "estado": st(f), "timer": {f"CH{c},{g}": q(f, f"TIMEr:SET? CH{c},{g}") for c in (1, 2) for g in range(1, 6)}}
    R["previo"] = previo
    print("configuración previa:", previo, flush=True)
    if previo["estado"]["CH1_salida"] or previo["estado"]["CH2_salida"]:
        sys.exit("Alguna salida está ENCENDIDA: no sigo (no la he encendido yo)")
    try:
        # ---------------------------------------------------------- A · consignas: resolución y límites (salida OFF)
        g = "A consignas"
        for c in ("CH1", "CH2"):
            for v in ("0.000", "1.234", "12.345", "32.000"):
                w(f, f"{c}:VOLTage {v}")
                r, e = q(f, f"{c}:VOLTage?"), err(f)
                anota(g, f"{c} VOLT {v}", abs(float(r) - float(v)) < 0.0006 and e.startswith("0"), f"lee {r} · {e}")
            for v in ("32.001", "33", "-1"):
                antes = q(f, f"{c}:VOLTage?")
                w(f, f"{c}:VOLTage {v}")
                r, e = q(f, f"{c}:VOLTage?"), err(f)
                anota(g, f"{c} VOLT {v} (fuera de rango)", r == antes or not e.startswith("0"), f"queda {r} · {e}")
            for i in ("0.001", "1.234", "3.200"):
                w(f, f"{c}:CURRent {i}")
                r, e = q(f, f"{c}:CURRent?"), err(f)
                anota(g, f"{c} CURR {i}", abs(float(r) - float(i)) < 0.0006 and e.startswith("0"), f"lee {r} · {e}")
            for i in ("3.201", "4"):
                antes = q(f, f"{c}:CURRent?")
                w(f, f"{c}:CURRent {i}")
                r, e = q(f, f"{c}:CURRent?"), err(f)
                anota(g, f"{c} CURR {i} (fuera de rango)", r == antes or not e.startswith("0"), f"queda {r} · {e}")
        w(f, "INSTrument CH2")
        anota(g, "INSTrument CH2 (canal activo del frontal)", q(f, "INSTrument?") == "CH2", q(f, "INSTrument?"))
        w(f, "INSTrument CH1")
        # ---------------------------------------------------------- B · salida y medida (sin carga)
        g = "B salida"
        for c in ("CH1", "CH2"):
            w(f, f"{c}:VOLTage 1.000")
            w(f, f"{c}:CURRent 0.100")
            w(f, f"OUTPut {c},ON", 1.0)
            v, i, p, e = num(f, f"MEASure:VOLTage? {c}"), num(f, f"MEASure:CURRent? {c}"), num(f, f"MEASure:POWEr? {c}"), st(f)
            anota(g, f"{c} ON a 1,000 V / 0,1 A sin carga", abs(v - 1.0) < 0.02 and e[f"{c}_salida"] and e[c] == "CV",
                  f"mide {v} V {i} A {p} W · estado {e}")
            w(f, f"{c}:VOLTage 5.000", 1.0)
            v = num(f, f"MEASure:VOLTage? {c}")
            anota(g, f"{c} cambia a 5,000 V con la salida encendida", abs(v - 5.0) < 0.03, f"mide {v} V")
            w(f, f"OUTPut {c},OFF", 1.0)
            v, e = num(f, f"MEASure:VOLTage? {c}"), st(f)
            anota(g, f"{c} OFF: estado y tensión medida", (not e[f"{c}_salida"]) and v < 0.1, f"mide {v} V · {e}")
        w(f, "OUTPut CH3,ON", 1.0)
        e3, er3 = st(f), err(f)
        w(f, "OUTPut CH3,OFF", 1.0)
        anota(g, "CH3 ON/OFF por SCPI (tensión fija por conmutador; no se mide por SCPI)", er3.startswith("0"), er3)
        # ---------------------------------------------------------- C · fallo conocido: apagar seguido
        g = "C apagado seguido"
        for pausa in (0.05, 0.3):
            for c in ("CH1", "CH2"):
                w(f, f"{c}:VOLTage 1.000")
                w(f, f"OUTPut {c},ON", 0.5)
            time.sleep(1.0)
            w(f, "OUTPut CH1,OFF", pausa)
            w(f, "OUTPut CH2,OFF", 1.0)
            v1, v2, e = num(f, "MEASure:VOLTage? CH1"), num(f, "MEASure:VOLTage? CH2"), st(f)
            ok = v1 < 0.1 and v2 < 0.1 and not e["CH1_salida"] and not e["CH2_salida"]
            anota(g, f"CH1 OFF y CH2 OFF con {pausa * 1000:.0f} ms entre medias: apagadas DE VERDAD", ok,
                  f"miden {v1} V y {v2} V · estado {e}")
            apagar_todo(f)
        # ---------------------------------------------------------- D · modos: independiente / serie / paralelo
        g = "D modo"
        for n, nombre in ((1, "serie"), (2, "paralelo"), (0, "independiente")):
            w(f, f"OUTPut:TRACk {n}", 1.0)
            e, er = st(f), err(f)
            anota(g, f"OUTPut:TRACk {n} → {nombre}", e["modo"] == nombre and er.startswith("0"), f"{e['modo']} · {er}")
        # ---------------------------------------------------------- E · temporizador interno (secuencia en la fuente)
        g = "E temporizador"
        w(f, "TIMEr:SET CH1,1,1.000,0.100,3")
        w(f, "TIMEr:SET CH1,2,2.000,0.100,3")
        w(f, "TIMEr:SET CH1,3,0,0,0")
        r1, r2, er = q(f, "TIMEr:SET? CH1,1"), q(f, "TIMEr:SET? CH1,2"), err(f)
        anota(g, "programar 2 pasos (1 V 3 s, 2 V 3 s) y leerlos", "1.000" in r1 and "2.000" in r2 and er.startswith("0"), f"{r1} | {r2} | {er}")
        w(f, "OUTPut CH1,ON", 0.5)
        w(f, "TIMEr CH1,ON", 0.5)
        t0, vs = time.time(), []
        while time.time() - t0 < 7.5:
            vs.append((round(time.time() - t0, 1), num(f, "MEASure:VOLTage? CH1")))
        e = st(f)
        w(f, "TIMEr CH1,OFF", 0.5)
        apagar_todo(f)
        uno = any(v is not None and abs(v - 1.0) < 0.05 for _, v in vs)
        dos = any(v is not None and abs(v - 2.0) < 0.05 for _, v in vs)
        anota(g, "medidas vacías mientras corre el temporizador", None, f"{sum(1 for _, v in vs if v is None)} de {len(vs)}")
        anota(g, "el temporizador ejecuta los pasos en la fuente (1 V y luego 2 V)", uno and dos, f"{vs} · TIMER1 {e['TIMER1']}")
        # ---------------------------------------------------------- F · forma de onda en el frontal
        g = "F onda en pantalla"
        w(f, "OUTPut:WAVE CH1,ON", 0.5)
        e = st(f)
        w(f, "OUTPut:WAVE CH1,OFF", 0.5)
        anota(g, "OUTPut:WAVE CH1,ON/OFF (gráfica en el frontal)", e["CH1_onda"], e)
        # ---------------------------------------------------------- G · ritmo de órdenes
        g = "G ritmo"
        for pausa in (0.05, 0.1, 0.2):
            fallos = 0
            for k in range(10):
                v = f"{1 + k / 1000:.3f}"
                f.write(f"CH1:VOLTage {v}")
                time.sleep(pausa)
                f.write("CH1:VOLTage?")
                time.sleep(pausa)
                try:
                    if f.read_raw().decode().strip() != v:
                        fallos += 1
                except Exception:
                    fallos += 1
            anota(g, f"10 consignas con {pausa * 1000:.0f} ms entre órdenes", fallos == 0, f"{fallos} fallos")
        t0 = time.time()
        for _ in range(5):
            num(f, "MEASure:VOLTage? CH1")
        anota(g, "lecturas de medida por segundo (con la pausa de 300 ms)", None, f"{5 / (time.time() - t0):.2f} por s")
    except Exception as ex:
        anota("ERROR", "la prueba se ha cortado", False, f"{type(ex).__name__}: {ex}")
    finally:
        v1, v2, e = apagar_todo(f)
        w(f, "OUTPut:TRACk 0", 1.0)
        for c in ("CH1", "CH2"):
            w(f, f"{c}:VOLTage {previo[c][0]}")
            w(f, f"{c}:CURRent {previo[c][1]}")
        for k, v in previo["timer"].items():
            p = [x.strip() for x in v.split(",")]
            if len(p) == 3:
                w(f, f"TIMEr:SET {k},{p[0]},{p[1]},{p[2]}")
        fin = {"CH1": (q(f, "CH1:VOLTage?"), q(f, "CH1:CURRent?")), "CH2": (q(f, "CH2:VOLTage?"), q(f, "CH2:CURRent?")),
               "estado": st(f), "miden_V": (num(f, "MEASure:VOLTage? CH1"), num(f, "MEASure:VOLTage? CH2"))}
        R["final"] = fin
        print("estado final:", fin, flush=True)
        f.close()
    os.makedirs(DOCS, exist_ok=True)
    ruta = os.path.join(DOCS, time.strftime("siglent_capacidades_%Y%m%d_%H%M.json"))
    with open(ruta, "w", encoding="utf-8") as fh:
        json.dump(R, fh, ensure_ascii=False, indent=1)
    print("Escrito", ruta)


if __name__ == "__main__":
    main()
