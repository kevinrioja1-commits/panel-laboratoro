"""Prueba de los arreglos de seguridad del 2026-11-04 con la FUENTE SIMULADA, en el propio proceso (sin servidor,
sin equipos). Ensayos y registro de cambios en una carpeta temporal del scratchpad."""
import os
import sys
import tempfile
import time
import traceback

PANEL = r"C:\Proyectos\_HERRAMIENTAS\Panel_laboratorio"
sys.path.insert(0, PANEL)
TMP = tempfile.mkdtemp(prefix="seg_")   # %TEMP%: no deja nada en pruebas\

import registro
registro.CARPETA = os.path.join(TMP, "registro")
import almacen
almacen.BASE = TMP
PROY = "PR99999-PRUEBA"
os.makedirs(os.path.join(TMP, PROY), exist_ok=True)
import fuente as fmod
import ensayo as emod

resultados = []


def ok(nombre, cond, detalle=""):
    resultados.append((nombre, bool(cond), detalle))
    print(("OK    " if cond else "FALLO ") + nombre + (f"  [{detalle}]" if detalle else ""), flush=True)


class OscFalso:
    modo, idn, dl = "panel", "falso", {}
    def _datalogger_vivo(self): return False
    def leer(self, autor): return {"falso": True}


def nueva():
    reg = registro.Registro()
    f = fmod.Fuente(reg, {"vmax_V": 10, "imax_A": 2, "pmax_W": 20}, periodo=0.1)
    f.conectar("sim", "prueba")
    ens = emod.Ensayos(OscFalso(), f, reg)
    return reg, f, ens


def esperar(cond, t=5.0):
    fin = time.time() + t
    while time.time() < fin:
        if cond():
            return True
        time.sleep(0.05)
    return cond()


PLAN_BASE = {"maximos": {"V": 10, "I": 2, "P": 20}}


def iniciar(ens, etapas, **extra):
    p = dict(proyecto=PROY, nombre="prueba_seg", plan=dict(PLAN_BASE, etapas=etapas), confirmado=True,
             pre_s=0, cola_s=0, apagar_al_terminar=True, temperaturas=False)
    p.update(extra)
    return ens.iniciar(p, "usuario")


def plan_de(ens):
    return ens.actual["plan"] if ens.actual else None


def limpiar(f, ens):
    try:
        if ens.actual:
            ens.cerrar_panel()
    except Exception:
        pass
    try:
        f.desconectar("prueba", si_encendida="apagar")
    except Exception:
        pass


# ---------------------------------------------------------------- 1. APAGAR durante el plan
try:
    reg, f, ens = nueva()
    iniciar(ens, [{"tipo": "consignas", "V": 5, "I": 0.5, "P": 15, "salida": "on"},
                  {"tipo": "mantener", "duracion_s": 2},
                  {"tipo": "consignas", "I": 0.6, "salida": "on"},
                  {"tipo": "mantener", "duracion_s": 1}, {"tipo": "apagar"}])
    ej = plan_de(ens)
    ok("1a plan enciende", esperar(lambda: f.enlace.out, 5))
    time.sleep(0.5)
    f.salida(False, False, "usuario")                        # APAGAR de la pagina
    t = time.time()
    ok("1b APAGAR bloquea el encendido", f.bloqueo is not None, f.bloqueo)
    ok("1c el plan se pausa enseguida", esperar(lambda: ej.estado == "pausado", 1.0), f"{time.time() - t:.2f} s")
    time.sleep(3)                                             # pasa de sobra el fin del «mantener 2 s»
    ok("1d la salida sigue APAGADA (el plan no vuelve a encender)", not f.enlace.out)
    ok("1e el plan sigue en pausa", ej.estado == "pausado", ej.estado)
    try:
        ens.plan_mando("continuar", "usuario", False)
        ok("1f Continuar sin confirmar se rechaza", False)
    except ValueError as e:
        ok("1f Continuar sin confirmar se rechaza", str(e).startswith("CONFIRMAR:"), str(e)[:70])
    ens.plan_mando("continuar", "usuario", True)
    ok("1g Continuar confirmado vuelve a encender", esperar(lambda: f.enlace.out, 2))
    ok("1h y quita el bloqueo", f.bloqueo is None)
    ok("1i el plan termina y apaga", esperar(lambda: ens.actual is None, 10) and not f.enlace.out, ej.estado)
    limpiar(f, ens)
except Exception:
    ok("1 sin excepciones", False, traceback.format_exc())

# ---------------------------------------------------------------- 2. rampa rapida + «local» en la fuente
try:
    reg, f, ens = nueva()
    iniciar(ens, [{"tipo": "consignas", "V": 5, "I": 0.1, "P": 15, "salida": "on"},
                  {"tipo": "rampa", "magnitud": "I", "hasta": 1.0, "paso": 0.05, "duracion_s": 3},
                  {"tipo": "consignas", "salida": "on"}, {"tipo": "apagar"}])
    ej = plan_de(ens)
    ok("2a rampa en marcha", esperar(lambda: f.enlace.iset >= 0.2, 6), f"I = {f.enlace.iset}")
    f.enlace.dueno = "NONE"                                   # alguien pulsa «local» en el frontal
    i_al_quitar = f.enlace.iset
    ok("2b el plan se pausa (sin retomar el remoto)", esperar(lambda: ej.estado == "pausado", 1.5), ej.mensaje[:90])
    time.sleep(2)
    ok("2c no escribe más consignas", abs(f.enlace.iset - i_al_quitar) <= 0.05 + 1e-9,
       f"{i_al_quitar} -> {f.enlace.iset}")
    ok("2d la fuente sigue en local", f.enlace.dueno == "NONE")
    ens.plan_mando("parar", "usuario")
    ok("2e PARAR apaga y termina", esperar(lambda: ens.actual is None, 8) and not f.enlace.out)
    limpiar(f, ens)
except Exception:
    ok("2 sin excepciones", False, traceback.format_exc())

# ---------------------------------------------------------------- 3. proteccion durante el plan
try:
    reg, f, ens = nueva()
    iniciar(ens, [{"tipo": "consignas", "V": 5, "I": 0.5, "P": 15, "salida": "on"},
                  {"tipo": "mantener", "duracion_s": 1.5},
                  {"tipo": "consignas", "salida": "on"}, {"tipo": "mantener", "duracion_s": 1}, {"tipo": "apagar"}])
    ej = plan_de(ens)
    ok("3a plan enciende", esperar(lambda: f.enlace.out, 5))
    f.enlace.ocp = 0.01                                       # salta OC: la fuente apaga
    ok("3b protección -> bloqueo", esperar(lambda: f.bloqueo is not None, 2), f.bloqueo)
    f.enlace.ocp = 22.0
    time.sleep(3)
    ok("3c el plan no vuelve a encender", not f.enlace.out)
    ok("3d el plan queda en pausa", ej.estado == "pausado", ej.mensaje[:90])
    limpiar(f, ens)
except Exception:
    ok("3 sin excepciones", False, traceback.format_exc())

# ---------------------------------------------------------------- 4. «encender al iniciar» (ensayo manual)
try:
    reg, f, ens = nueva()
    f.fijar("usuario", V_lim_V=5, I_A=0.5, P_W=15)
    p = dict(proyecto=PROY, nombre="enc", pre_s=0, cola_s=0, apagar_al_terminar=True, temperaturas=False,
             encender_al_iniciar=True)
    try:
        ens.iniciar(dict(p), "claude")
        ok("4a sin confirmado se rechaza", False)
    except ValueError as e:
        ok("4a sin confirmado se rechaza", True, str(e)[:60])
    ok("4b no se creó el ensayo", ens.actual is None)
    ens.iniciar(dict(p, confirmado=True, consignas_confirmadas={"V_lim_V": 5, "I_A": 0.4, "P_W": f.consignas["P_W"]}),
                "usuario")
    esperar(lambda: ens.actual["encender"] != "pendiente", 4)
    ok("4c consignas cambiadas tras confirmar: NO enciende", not f.enlace.out, ens.actual["encender"])
    ens.terminar("usuario")
    esperar(lambda: ens.actual is None, 4)
    ens.iniciar(dict(p, confirmado=True, consignas_confirmadas={k: f.consignas[k] for k in ("V_lim_V", "I_A", "P_W")}),
                "usuario")
    ok("4d mismas consignas: enciende", esperar(lambda: f.enlace.out, 4), ens.actual["encender"])
    ens.terminar("usuario")
    esperar(lambda: ens.actual is None, 4)
    limpiar(f, ens)
except Exception:
    ok("4 sin excepciones", False, traceback.format_exc())

# ---------------------------------------------------------------- 5. desconectar / cerrar con la salida ON
try:
    reg, f, ens = nueva()
    f.fijar("usuario", V_lim_V=5, I_A=0.5)
    f.salida(True, True, "usuario")
    ok("5a encendida (con vigilante)", f.enlace.out and f.vigilante is not None, f"vigilante {f.vigilante} s")
    try:
        f.desconectar("usuario")
        ok("5b desconectar con la salida ON se rechaza", False)
    except RuntimeError as e:
        ok("5b desconectar con la salida ON se rechaza", True, str(e)[:60])
    ok("5c sigue conectada y encendida", f.estado == "conectada" and f.enlace.out)
    sim = f.enlace
    f.desconectar("panel (cierre)", si_encendida="apagar")
    ok("5d cierre del panel: apaga", not sim.out)
    ok("5e y quita el vigilante después de apagar", sim.mon == "OFF")
except Exception:
    ok("5 sin excepciones", False, traceback.format_exc())

# ---------------------------------------------------------------- 6. no se enciende sin vigilante
try:
    reg, f, ens = nueva()
    f.fijar("usuario", V_lim_V=5, I_A=0.5)
    real = f.enlace.consultar
    f.enlace.consultar = lambda c: "OFF" if c.upper().strip() == "SYST:COMM:MON:ACT?" else real(c)
    f.vigilante = None
    try:
        f.salida(True, True, "usuario")
        ok("6 sin vigilante no enciende", False)
    except RuntimeError as e:
        ok("6 sin vigilante no enciende", not f.enlace.out, str(e)[:70])
    f.enlace.consultar = real
    limpiar(f, ens)
except Exception:
    ok("6 sin excepciones", False, traceback.format_exc())

# ---------------------------------------------------------------- 7. cerrar el panel en mitad de un plan
try:
    reg, f, ens = nueva()
    iniciar(ens, [{"tipo": "consignas", "V": 5, "I": 0.5, "P": 15, "salida": "on"},
                  {"tipo": "mantener", "duracion_s": 30}, {"tipo": "apagar"}], apagar_al_terminar=False)
    ok("7a plan enciende", esperar(lambda: f.enlace.out, 5))
    sim = f.enlace
    ens.cerrar_panel()
    f.desconectar("panel (cierre)", si_encendida="apagar")
    ok("7b al cerrar el panel la salida queda APAGADA", not sim.out)
    ok("7c ensayo cerrado", ens.actual is None)
except Exception:
    ok("7 sin excepciones", False, traceback.format_exc())

# ---------------------------------------------------------------- 8. Encender a mano tras APAGAR (sin plan)
try:
    reg, f, ens = nueva()
    f.fijar("usuario", V_lim_V=5, I_A=0.5)
    f.salida(True, True, "usuario")
    f.salida(False, False, "usuario")
    ok("8a APAGAR deja bloqueo", f.bloqueo is not None)
    try:
        f.salida(True, True, "plan")
        ok("8b un automatismo no puede encender", False)
    except fmod.EncendidoBloqueado:
        ok("8b un automatismo no puede encender", not f.enlace.out)
    f.salida(True, True, "usuario")
    ok("8c una persona sí (confirmando) y quita el bloqueo", f.enlace.out and f.bloqueo is None)
    limpiar(f, ens)
except Exception:
    ok("8 sin excepciones", False, traceback.format_exc())

n_ok = sum(1 for _, c, _ in resultados if c)
print(f"\n{n_ok} OK de {len(resultados)}   (carpeta temporal: {TMP})")
os._exit(0 if n_ok == len(resultados) else 1)
