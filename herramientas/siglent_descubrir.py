r"""Siglent SPD3303X / X-E por NI-VISA (USB): qué responde de verdad a cada orden SCPI. SOLO LECTURA (9-nov-2026).

Método del panel para equipos con driver (norma del usuario): primero se pregunta al equipo, luego se contrasta con
la documentación y solo entonces se modela. Este script no cambia nada: solo consultas «?».
Peculiaridades (foro EEVblog, notas de Siglent y lo visto aquí el 9-nov):
  - solo «\n» como terminador y ~300 ms entre órdenes;
  - con query() la primera consulta se quedó COLGADA dentro del driver USB (el proceso no se podía ni matar):
    se escribe, se espera y se lee por separado (write + read_raw), que sí funciona.

    python herramientas\siglent_descubrir.py
Escribe docs\siglent_descubrimiento_<fecha>.json
"""
import json
import os
import sys
import time

import pyvisa

PAUSA_S = 0.3
DOCS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs")
# el recurso se busca por el identificador USB de la SPD3303X (sin el número de serie en el código)
RECURSO = next((r for r in pyvisa.ResourceManager().list_resources("USB?*::INSTR") if "0x0483::0x7540" in r), None)


def abrir():
    if not RECURSO:
        sys.exit("NI-VISA no ve la Siglent SPD3303X por USB")
    rm = pyvisa.ResourceManager()
    f = rm.open_resource(RECURSO, open_timeout=2000)
    f.write_termination = "\n"
    f.timeout = 1500
    return f


def q(f, orden):
    """Escribir y leer por separado, con pausa."""
    f.write(orden)
    time.sleep(PAUSA_S)
    return f.read_raw().decode("ascii", "replace").strip()


def preguntar(f, orden):
    time.sleep(PAUSA_S)
    try:
        r = q(f, orden)
    except Exception as e:
        r = f"SIN RESPUESTA ({type(e).__name__})"
    try:
        err = q(f, "SYSTem:ERRor?")
    except Exception as e:
        err = f"? ({type(e).__name__})"
    return r, err


def estado(v):
    """SYSTem:STATus? (hexadecimal) -> bits según la Quick Start de la SPD3303X."""
    v = int(v, 16)
    modo = {1: "independiente", 3: "serie", 2: "paralelo"}.get((v >> 2) & 3, f"? {(v >> 2) & 3}")
    return {"CH1": "CC" if v & 1 else "CV", "CH2": "CC" if v & 2 else "CV", "modo": modo,
            "CH1_salida": bool(v & 16), "CH2_salida": bool(v & 32), "TIMER1": bool(v & 64), "TIMER2": bool(v & 128),
            "CH1_onda": bool(v & 256), "CH2_onda": bool(v & 512)}


def main():
    f = abrir()
    ordenes = ["*IDN?", "SYSTem:VERSion?", "SYSTem:STATus?", "INSTrument?",
               "CH1:VOLTage?", "CH1:CURRent?", "CH2:VOLTage?", "CH2:CURRent?",
               "MEASure:VOLTage? CH1", "MEASure:CURRent? CH1", "MEASure:POWEr? CH1",
               "MEASure:VOLTage? CH2", "MEASure:CURRent? CH2", "MEASure:POWEr? CH2",
               "MEASure:VOLTage?", "OUTPut:TRACk?",
               "TIMEr:SET? CH1,1", "TIMEr:SET? CH1,2", "TIMEr:SET? CH1,5", "TIMEr:SET? CH2,1",
               "IPaddr?", "MASKaddr?", "GATEaddr?", "DHCP?"]
    res = {}
    for o in ordenes:
        r, err = preguntar(f, o)
        res[o] = {"respuesta": r, "error_tras": err}
        print(f"{o:24} -> {r!r:44} errores: {err}", flush=True)
    try:
        res["estado_decodificado"] = estado(res["SYSTem:STATus?"]["respuesta"])
        print("estado:", res["estado_decodificado"])
    except ValueError:
        pass
    f.close()
    os.makedirs(DOCS, exist_ok=True)
    ruta = os.path.join(DOCS, time.strftime("siglent_descubrimiento_%Y%m%d_%H%M.json"))
    with open(ruta, "w", encoding="utf-8") as fh:
        json.dump({"recurso": RECURSO, "pausa_s": PAUSA_S, "ordenes": res}, fh, ensure_ascii=False, indent=1)
    print("Escrito", ruta)


if __name__ == "__main__":
    main()
